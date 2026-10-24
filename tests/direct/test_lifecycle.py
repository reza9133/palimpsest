"""Normal lifecycles: registering, settling, forfeiting, and every challenge outcome."""

import pytest

from tests.direct.support import (
    BOND, CHALLENGE_WINDOW, JUDGE_WINDOW, NOW_TS, PERCENT, REVEAL_WINDOW, STAKE, Entry, addr, at,
    challenge, challenge_of, check_books, claim_of, commit, credit, deploy, deposit, expire,
    forfeit, judge, register, reveal, run_to_challenge, serve, settle,
)


def test_info_reports_the_configured_parameters(direct_vm, direct_deploy):
    contract = deploy(direct_vm, direct_deploy)
    info = contract.get_info()
    assert info["name"] == "Palimpsest"
    assert info["version"] == "0.1.0"
    assert info["stake_wei"] == STAKE
    assert info["challenge_bond_wei"] == BOND
    assert info["reveal_window_seconds"] == REVEAL_WINDOW
    assert info["challenge_window_seconds"] == CHALLENGE_WINDOW
    assert info["judge_window_seconds"] == JUDGE_WINDOW
    assert info["overlap_percent"] == PERCENT
    assert contract.get_claim_count() == 0


def test_commit_assigns_priority_in_registration_order_and_locks_the_stake(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    entries = [Entry(f"e{i}", actors[i]) for i in range(3)]
    direct_vm.warp(at(NOW_TS + 10))
    seqs = [commit(contract, direct_vm, entry) for entry in entries]
    assert seqs == [1, 2, 3]
    assert contract.get_claim_count() == 3
    accounting = check_books(contract, actors)
    assert accounting["total_locked_stakes"] == 3 * STAKE
    assert accounting["total_credits"] == 0
    claim = claim_of(contract, 2)
    assert claim["status"] == "committed"
    assert claim["author"].lower() == addr(actors[1])
    assert claim["committed_at"] == NOW_TS + 10
    assert claim["reveal_by"] == NOW_TS + 10 + REVEAL_WINDOW
    assert claim["title"] == "" and claim["abstract"] == "" and claim["elements"] == []


def test_find_claim_locates_a_claim_by_author_and_commitment(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    first, second = Entry("one", actors[0]), Entry("two", actors[1])
    commit(contract, direct_vm, first)
    commit(contract, direct_vm, second)
    assert contract.find_claim(addr(actors[1]), second.seal()) == 2
    assert contract.find_claim(addr(actors[0]), first.seal()) == 1
    with direct_vm.expect_revert():
        contract.find_claim(addr(actors[0]), second.seal())  # right hash, wrong author


def test_reveal_publishes_the_text_and_opens_the_challenge_window(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    direct_vm.warp(at(NOW_TS + 10))
    seq = commit(contract, direct_vm, entry)
    direct_vm.warp(at(NOW_TS + 100))
    reveal(contract, direct_vm, entry, seq)
    claim = claim_of(contract, seq)
    assert claim["status"] == "revealed"
    assert claim["title"] == entry.title
    assert claim["abstract"] == entry.abstract
    assert claim["elements"] == entry.elements
    assert claim["element_count"] == 3
    assert claim["revealed_at"] == NOW_TS + 100
    assert claim["challenge_until"] == NOW_TS + 100 + CHALLENGE_WINDOW
    assert claim["stake_wei"] == STAKE


def test_unchallenged_claim_settles_and_returns_its_stake(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    seq = register(contract, direct_vm, entry, NOW_TS + 60)
    until = claim_of(contract, seq)["challenge_until"]
    direct_vm.warp(at(until - 1))
    with direct_vm.expect_revert():
        settle(contract, direct_vm, actors[5], seq)
    direct_vm.warp(at(until))
    settle(contract, direct_vm, actors[5], seq)  # permissionless, exact boundary
    claim = claim_of(contract, seq)
    assert claim["status"] == "settled"
    assert claim["outcome"] == "settled_stake_returned"
    assert claim["stake_wei"] == 0
    assert credit(contract, actors[0]) == STAKE
    assert credit(contract, actors[5]) == 0
    assert check_books(contract, actors)["total_locked_stakes"] == 0
    with direct_vm.expect_revert():
        settle(contract, direct_vm, actors[0], seq)  # only once


def test_unrevealed_claim_burns_its_stake_after_the_deadline(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("late", actors[0])
    direct_vm.warp(at(NOW_TS + 10))
    seq = commit(contract, direct_vm, entry)
    deadline = claim_of(contract, seq)["reveal_by"]
    direct_vm.warp(at(deadline - 1))
    with direct_vm.expect_revert():
        forfeit(contract, direct_vm, actors[3], seq)
    direct_vm.warp(at(deadline))
    forfeit(contract, direct_vm, actors[3], seq)
    claim = claim_of(contract, seq)
    assert claim["status"] == "forfeited"
    assert claim["outcome"] == "unrevealed_stake_burned"
    accounting = check_books(contract, actors)
    assert accounting["total_commons"] == STAKE
    assert accounting["total_locked_stakes"] == 0
    assert credit(contract, actors[0]) == 0 and credit(contract, actors[3]) == 0
    with direct_vm.expect_revert():
        reveal(contract, direct_vm, entry, seq)  # too late, and no longer awaiting reveal
    with direct_vm.expect_revert():
        forfeit(contract, direct_vm, actors[3], seq)


def test_upheld_challenge_revokes_the_junior_and_pays_the_challenger(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1], verdict="derivative")
    senior_seq, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    assert claim_of(contract, junior_seq)["status"] == "challenged"
    assert challenge_of(contract, junior_seq)["pending"] is True
    judge(contract, direct_vm, actors[0], junior_seq)
    claim = claim_of(contract, junior_seq)
    assert claim["status"] == "revoked"
    assert claim["outcome"] == "revoked_as_derivative"
    assert claim["revoked_by"] == senior_seq
    assert claim["stake_wei"] == 0
    assert credit(contract, actors[0]) == STAKE + BOND   # junior's stake plus own bond back
    assert credit(contract, actors[1]) == 0
    record = challenge_of(contract, junior_seq)
    assert record["pending"] is False and record["basis"] == "overlap"
    assert record["disclosed_mask"] == 0b111 and record["pair_result"] == "upheld"
    accounting = check_books(contract, actors)
    assert accounting["total_locked_bonds"] == 0
    assert accounting["total_locked_stakes"] == STAKE  # only the senior's own stake remains


def test_two_of_three_elements_is_enough_at_sixty_percent(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1], verdict="partial")
    _, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    judge(contract, direct_vm, actors[1], junior_seq)  # the accused may also ask for judging
    assert claim_of(contract, junior_seq)["status"] == "revoked"
    assert challenge_of(contract, junior_seq)["disclosed_mask"] == 0b011
    assert challenge_of(contract, junior_seq)["needed_disclosures"] == 2


def test_rejected_challenge_pays_the_accused_and_the_claim_keeps_its_place(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1], verdict="distinct")
    senior_seq, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    judge(contract, direct_vm, actors[0], junior_seq)
    claim = claim_of(contract, junior_seq)
    assert claim["status"] == "revealed"
    assert claim["outcome"] == "challenge_rejected"
    assert claim["stake_wei"] == STAKE
    assert credit(contract, actors[1]) == BOND      # compensation from the failed challenger
    assert credit(contract, actors[0]) == 0         # the challenger's bond is gone
    assert challenge_of(contract, junior_seq)["pair_result"] == "rejected"
    assert challenge_of(contract, junior_seq)["basis"] == "absence"
    check_books(contract, actors)
    direct_vm.warp(at(claim["challenge_until"]))
    settle(contract, direct_vm, actors[7], junior_seq)
    assert credit(contract, actors[1]) == BOND + STAKE
    assert claim_of(contract, junior_seq)["status"] == "settled"
    check_books(contract, actors)


def test_inconclusive_verdicts_void_the_challenge_after_the_third_assessment(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1], verdict="undecided")
    _, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    for attempt in range(3):
        judge(contract, direct_vm, actors[0], junior_seq)
        record = challenge_of(contract, junior_seq)
        assert record["assessments"] == attempt + 1
        if attempt < 2:
            assert record["pending"] is True
            assert credit(contract, actors[0]) == 0
            direct_vm.warp(at(record["retry_at"]))
    claim = claim_of(contract, junior_seq)
    assert claim["status"] == "revealed"
    assert claim["outcome"] == "challenge_voided_no_decisive_verdict"
    assert credit(contract, actors[0]) == BOND      # bond refunded, nothing forfeited
    assert credit(contract, actors[1]) == 0
    assert claim["stake_wei"] == STAKE
    assert challenge_of(contract, junior_seq)["pair_result"] == "void"
    check_books(contract, actors)


def test_a_challenge_nobody_judges_expires_and_refunds_the_bond(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1])
    _, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    judge_by = challenge_of(contract, junior_seq)["judge_by"]
    assert judge_by == NOW_TS + 180 + JUDGE_WINDOW  # challenged at NOW_TS + 180
    direct_vm.warp(at(judge_by - 1))
    with direct_vm.expect_revert():
        expire(contract, direct_vm, actors[4], junior_seq)
    direct_vm.warp(at(judge_by))
    with direct_vm.expect_revert():
        judge(contract, direct_vm, actors[0], junior_seq)   # judging closes exactly here
    expire(contract, direct_vm, actors[4], junior_seq)
    claim = claim_of(contract, junior_seq)
    assert claim["status"] == "revealed"
    assert claim["outcome"] == "challenge_voided_judging_window_expired"
    assert credit(contract, actors[0]) == BOND
    check_books(contract, actors)


def test_a_challenge_whose_senior_lost_standing_can_be_voided_at_once(
    direct_vm, direct_deploy, actors
):
    """A chain: first claim revokes the second, which had itself challenged the third."""
    contract = deploy(direct_vm, direct_deploy)
    first = Entry("first", actors[0])
    second = Entry("second", actors[1], verdict="derivative")
    third = Entry("third", actors[2], verdict="distinct")
    first_seq = register(contract, direct_vm, first, NOW_TS + 60)
    second_seq = register(contract, direct_vm, second, NOW_TS + 120)
    third_seq = register(contract, direct_vm, third, NOW_TS + 180)
    serve(direct_vm, [first, second, third])
    direct_vm.warp(at(NOW_TS + 240))
    challenge(contract, direct_vm, second, third_seq, second_seq)
    challenge(contract, direct_vm, first, second_seq, first_seq)
    judge(contract, direct_vm, actors[0], second_seq)       # second is revoked as derivative
    assert claim_of(contract, second_seq)["status"] == "revoked"
    with direct_vm.expect_revert():
        judge(contract, direct_vm, actors[1], third_seq)    # its challenge no longer counts
    expire(contract, direct_vm, actors[9], third_seq)
    assert claim_of(contract, third_seq)["outcome"] == "challenge_voided_senior_lost_standing"
    assert credit(contract, actors[1]) == BOND               # the revoked author's bond comes back
    assert credit(contract, actors[0]) == STAKE + BOND       # second's stake and the own bond
    check_books(contract, actors)


def test_a_second_senior_can_challenge_after_the_first_challenge_failed(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    first = Entry("first", actors[0])
    second = Entry("second", actors[1])
    junior = Entry("junior", actors[2], verdict="distinct")
    first_seq = register(contract, direct_vm, first, NOW_TS + 60)
    second_seq = register(contract, direct_vm, second, NOW_TS + 120)
    junior_seq = register(contract, direct_vm, junior, NOW_TS + 180)
    serve(direct_vm, [junior])
    direct_vm.warp(at(NOW_TS + 240))
    challenge(contract, direct_vm, first, junior_seq, first_seq)
    judge(contract, direct_vm, actors[0], junior_seq)
    assert claim_of(contract, junior_seq)["status"] == "revealed"
    junior.verdict = "derivative"
    serve(direct_vm, [junior])
    challenge(contract, direct_vm, second, junior_seq, second_seq)
    judge(contract, direct_vm, actors[1], junior_seq)
    assert claim_of(contract, junior_seq)["status"] == "revoked"
    assert claim_of(contract, junior_seq)["revoked_by"] == second_seq
    assert claim_of(contract, junior_seq)["challenges_started"] == 2
    assert credit(contract, actors[1]) == STAKE + BOND
    assert credit(contract, actors[2]) == BOND               # from the first, failed challenge
    check_books(contract, actors)


def test_a_settled_senior_keeps_its_standing_forever(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    senior_seq = register(contract, direct_vm, senior, NOW_TS + 60)
    direct_vm.warp(at(NOW_TS + 60 + CHALLENGE_WINDOW))
    settle(contract, direct_vm, actors[0], senior_seq)
    junior = Entry("junior", actors[1], verdict="derivative")
    junior_seq = register(contract, direct_vm, junior, NOW_TS + 60 + CHALLENGE_WINDOW + 30 * 86400)
    serve(direct_vm, [junior])
    challenge(contract, direct_vm, senior, junior_seq, senior_seq)
    judge(contract, direct_vm, actors[0], junior_seq)
    assert claim_of(contract, junior_seq)["status"] == "revoked"
    assert credit(contract, actors[0]) == STAKE + STAKE + BOND  # returned stake + won stake + bond
    check_books(contract, actors)


def test_a_failed_judging_call_costs_the_budget_but_decides_nothing(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0])
    junior = Entry("junior", actors[1], verdict={"nonsense": True})
    _, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    judge(contract, direct_vm, actors[0], junior_seq)
    record = challenge_of(contract, junior_seq)
    assert record["pending"] is True
    assert record["judge_calls"] == 1 and record["assessments"] == 0
    assert record["reason"] == "malformed_or_unavailable_model_output"
    with direct_vm.expect_revert():
        judge(contract, direct_vm, actors[0], junior_seq)   # cooldown
    direct_vm.warp(at(record["retry_at"]))
    junior.verdict = "derivative"
    serve(direct_vm, [junior])
    judge(contract, direct_vm, actors[0], junior_seq)       # a later honest answer decides
    assert claim_of(contract, junior_seq)["status"] == "revoked"
    assert challenge_of(contract, junior_seq)["reason"] == ""


def test_withdrawing_credit_pays_out_once_and_clears_the_ledger(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    seq = register(contract, direct_vm, entry, NOW_TS + 60)
    direct_vm.warp(at(claim_of(contract, seq)["challenge_until"]))
    settle(contract, direct_vm, actors[0], seq)
    direct_vm.sender = actors[0]
    contract.withdraw_credit()
    assert credit(contract, actors[0]) == 0
    assert contract.get_accounting()["total_credits"] == 0
    with direct_vm.expect_revert():
        contract.withdraw_credit()
