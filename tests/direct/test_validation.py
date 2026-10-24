"""Input validation: every malformed call must revert without touching the books."""

import pytest

from tests.direct.support import (
    BOND, CHALLENGE_WINDOW, NOW_TS, REVEAL_WINDOW, STAKE, Entry, addr, at, challenge, challenge_of,
    check_books, claim_of, commit, credit, deploy, deposit, register, reveal, serve, settle,
)

DAY = 86400


# --------------------------------------------------------------------- constructor


@pytest.mark.parametrize("overrides", [
    {"stake_wei": 10**14 - 1},
    {"challenge_bond_wei": 10**14 - 1},
    {"reveal_window": 119},
    {"reveal_window": 30 * DAY + 1},
    {"challenge_window": 119},
    {"challenge_window": 90 * DAY + 1},
    {"judge_window": 119},
    {"judge_window": 14 * DAY + 1},
    {"overlap_percent": 49},
    {"overlap_percent": 101},
])
def test_out_of_range_constructor_arguments_are_refused(direct_vm, direct_deploy, overrides):
    with pytest.raises(Exception):
        deploy(direct_vm, direct_deploy, **overrides)


@pytest.mark.parametrize("overrides", [
    {"stake_wei": 10**14},
    {"challenge_bond_wei": 10**14},
    {"reveal_window": 120, "challenge_window": 120, "judge_window": 120},
    {"reveal_window": 30 * DAY, "challenge_window": 90 * DAY, "judge_window": 14 * DAY},
    {"overlap_percent": 50},
    {"overlap_percent": 100},
])
def test_boundary_constructor_arguments_are_accepted(direct_vm, direct_deploy, overrides):
    contract = deploy(direct_vm, direct_deploy, **overrides)
    assert contract.get_info()["name"] == "Palimpsest"


# ------------------------------------------------------------------------- commit


@pytest.mark.parametrize("text", [
    "", "0x", "0x1234", "12" * 32, "0x" + "g" * 64, "0x" + "1" * 63, "0x" + "1" * 65,
    " ", "0x" + "1" * 64 + "00",
])
def test_malformed_commitments_revert(direct_vm, direct_deploy, actors, text):
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, actors[0], STAKE)
    direct_vm.sender = actors[0]
    with direct_vm.expect_revert():
        contract.commit_claim(text)
    assert contract.get_claim_count() == 0
    assert credit(contract, actors[0]) == STAKE


def test_uppercase_hex_and_surrounding_spaces_are_normalised(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    deposit(contract, direct_vm, actors[0], STAKE)
    direct_vm.sender = actors[0]
    seq = contract.commit_claim("  0x" + entry.seal()[2:].upper() + " ")
    assert claim_of(contract, seq)["commitment"] == entry.seal()


def test_committing_needs_the_full_stake_in_credit_and_never_borrows(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    deposit(contract, direct_vm, actors[0], STAKE - 1)
    direct_vm.sender = actors[0]
    with direct_vm.expect_revert():
        contract.commit_claim(entry.seal())
    assert contract.get_claim_count() == 0
    assert credit(contract, actors[0]) == STAKE - 1
    deposit(contract, direct_vm, actors[0], 1)
    direct_vm.sender = actors[0]
    contract.commit_claim(entry.seal())  # control: exactly enough
    assert credit(contract, actors[0]) == 0
    assert contract.get_accounting()["total_locked_stakes"] == STAKE


def test_the_same_author_cannot_register_the_same_commitment_twice(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0])
    commit(contract, direct_vm, entry)
    deposit(contract, direct_vm, actors[0], STAKE)
    direct_vm.sender = actors[0]
    with direct_vm.expect_revert():
        contract.commit_claim(entry.seal())
    assert contract.get_claim_count() == 1
    assert credit(contract, actors[0]) == STAKE      # the second stake was never taken


def test_deposits_must_be_positive_and_withdrawals_need_credit(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    direct_vm.sender = actors[0]
    direct_vm.value = 0
    with direct_vm.expect_revert():
        contract.deposit_credit()
    with direct_vm.expect_revert():
        contract.withdraw_credit()
    assert contract.get_accounting()["total_credits"] == 0


# ------------------------------------------------------------------------- reveal


def _committed(direct_vm, direct_deploy, actors, **entry_fields):
    contract = deploy(direct_vm, direct_deploy)
    entry = Entry("a", actors[0], **entry_fields)
    direct_vm.warp(at(NOW_TS + 10))
    seq = commit(contract, direct_vm, entry)
    return contract, entry, seq


def _reveal_is_rejected(direct_vm, contract, entry, seq, message):
    with direct_vm.expect_revert(message):
        reveal(contract, direct_vm, entry, seq)
    assert claim_of(contract, seq)["status"] == "committed"
    assert claim_of(contract, seq)["title"] == ""


@pytest.mark.parametrize("title,message", [
    (" \n\t ", "Invalid title"),
    ("t" * 121, "Invalid title"),
])
def test_bad_titles_are_refused(direct_vm, direct_deploy, actors, title, message):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors, title=title)
    _reveal_is_rejected(direct_vm, contract, entry, seq, message)


@pytest.mark.parametrize("abstract", ["a" * 39, "a" * 1201, "   " + "b" * 38 + "   "])
def test_bad_abstract_lengths_are_refused(direct_vm, direct_deploy, actors, abstract):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors, abstract=abstract)
    _reveal_is_rejected(direct_vm, contract, entry, seq, "Invalid abstract")


@pytest.mark.parametrize("title,abstract,elements", [
    ("t" * 120, "a" * 40, ["e" * 12, "f" * 12]),
    ("t", "a" * 1200, ["e" * 240, "f" * 240]),
    ("t", "a" * 40, [f"element number {i:02d} text" for i in range(8)]),
])
def test_exact_length_boundaries_are_accepted(
    direct_vm, direct_deploy, actors, title, abstract, elements
):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors, title=title,
                                      abstract=abstract, elements=elements)
    reveal(contract, direct_vm, entry, seq)
    assert claim_of(contract, seq)["status"] == "revealed"
    assert claim_of(contract, seq)["element_count"] == len(elements)


@pytest.mark.parametrize("elements,message", [
    (["only one element here"], "Invalid element count"),
    ([f"element number {i:02d} text" for i in range(9)], "Invalid element count"),
    (["short", "another long enough element"], "Invalid element length"),
    (["e" * 241, "another long enough element"], "Invalid element length"),
    (["The Same Element Text", "the same   element text"], "Duplicate element"),
])
def test_bad_element_lists_are_refused(direct_vm, direct_deploy, actors, elements, message):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors, elements=elements)
    _reveal_is_rejected(direct_vm, contract, entry, seq, message)


def test_blank_lines_and_windows_line_endings_in_elements_are_tolerated(
    direct_vm, direct_deploy, actors
):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    reveal(contract, direct_vm, entry, seq,
           elements_text="\r\n" + entry.elements[0] + "\r\n\r\n" + entry.elements[1] + "\n"
           + entry.elements[2] + "\r\n")
    assert claim_of(contract, seq)["elements"] == entry.elements


def test_whitespace_in_text_fields_is_normalised_before_sealing_and_storing(
    direct_vm, direct_deploy, actors
):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    reveal(contract, direct_vm, entry, seq,
           title="  " + entry.title.replace(" ", "  \t ") + "\n",
           abstract=entry.abstract.replace(" ", "\n  "))
    assert claim_of(contract, seq)["title"] == entry.title
    assert claim_of(contract, seq)["abstract"] == entry.abstract


@pytest.mark.parametrize("hidden", [
    "\u200b", "\u200d", "\u202e", "\u2066", "\ufeff", "\u00ad", "\u2060", "\x00", "\x07", "\x7f",
    "\x1b",
])
@pytest.mark.parametrize("where", ["title", "abstract", "elements"])
def test_hidden_and_control_characters_are_refused_everywhere(
    direct_vm, direct_deploy, actors, hidden, where
):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    overrides = {
        "title": {"title": entry.title + hidden},
        "abstract": {"abstract": entry.abstract + hidden},
        "elements": {"elements_text": entry.elements_text + hidden},
    }[where]
    with direct_vm.expect_revert("Hidden or control characters"):
        reveal(contract, direct_vm, entry, seq, **overrides)
    assert claim_of(contract, seq)["status"] == "committed"


@pytest.mark.parametrize("field,value", [
    ("title", "something else entirely"),
    ("abstract", "x" + "y" * 60),
    ("elements_text", "first different element\nsecond different element"),
    ("salt", "z" * 32),
])
def test_any_changed_field_breaks_the_commitment(direct_vm, direct_deploy, actors, field, value):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    with direct_vm.expect_revert("Reveal does not match"):
        reveal(contract, direct_vm, entry, seq, **{field: value})
    assert claim_of(contract, seq)["status"] == "committed"


@pytest.mark.parametrize("salt", ["short", "a" * 15, "a" * 65, "has space in it 123456", "bad/char/salt-1234567"])
def test_malformed_salts_are_refused(direct_vm, direct_deploy, actors, salt):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    with direct_vm.expect_revert("Invalid salt"):
        reveal(contract, direct_vm, entry, seq, salt=salt)


def test_reveal_deadline_is_exclusive_and_double_reveal_reverts(direct_vm, direct_deploy, actors):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    deadline = claim_of(contract, seq)["reveal_by"]
    direct_vm.warp(at(deadline))
    with direct_vm.expect_revert("Reveal window has closed"):
        reveal(contract, direct_vm, entry, seq)
    direct_vm.warp(at(deadline - 1))
    reveal(contract, direct_vm, entry, seq)               # the last second still counts
    with direct_vm.expect_revert():
        reveal(contract, direct_vm, entry, seq)
    assert claim_of(contract, seq)["status"] == "revealed"


def test_only_the_author_can_reveal_and_unknown_claims_revert(direct_vm, direct_deploy, actors):
    contract, entry, seq = _committed(direct_vm, direct_deploy, actors)
    with direct_vm.expect_revert("Only the author may reveal"):
        reveal(contract, direct_vm, entry, seq, who=actors[1])
    for bad in (0, 2, 10**6):
        with direct_vm.expect_revert("Claim not found"):
            reveal(contract, direct_vm, entry, bad)
    assert claim_of(contract, seq)["status"] == "committed"


def test_unknown_claim_reads_revert(direct_vm, direct_deploy, actors):
    contract, _, seq = _committed(direct_vm, direct_deploy, actors)
    for bad in (0, seq + 1):
        with direct_vm.expect_revert():
            contract.get_claim(bad)
        with direct_vm.expect_revert():
            contract.get_challenge(bad)
    with direct_vm.expect_revert():
        contract.get_challenge(seq)                          # never challenged
    with direct_vm.expect_revert():
        contract.get_available_credit("not an address")
    with direct_vm.expect_revert():
        contract.get_available_credit("0x" + "00" * 20)
    with direct_vm.expect_revert():
        contract.find_claim(addr(actors[0]), "0x" + "ab" * 32)


# ----------------------------------------------------------------------- challenge


def _trio(direct_vm, direct_deploy, actors):
    """Three revealed claims: seniors at 1 and 2, junior at 3."""
    contract = deploy(direct_vm, direct_deploy)
    entries = [Entry("one", actors[0]), Entry("two", actors[1]),
               Entry("three", actors[2], verdict="distinct")]
    seqs = [register(contract, direct_vm, e, NOW_TS + 60 * (i + 1)) for i, e in enumerate(entries)]
    serve(direct_vm, entries)
    direct_vm.warp(at(NOW_TS + 300))
    return contract, entries, seqs


def _rejected(direct_vm, contract, caller_entry, junior_seq, senior_seq, message=None, topup=True,
              status="revealed"):
    if topup:
        deposit(contract, direct_vm, caller_entry.who, BOND)
    direct_vm.sender = caller_entry.who
    before = contract.get_accounting()
    with direct_vm.expect_revert(message):
        contract.challenge_claim(junior_seq, senior_seq)
    assert contract.get_accounting() == before
    assert claim_of(contract, junior_seq)["status"] == status


def test_a_stranger_or_the_accused_cannot_challenge_in_someone_elses_name(
    direct_vm, direct_deploy, actors
):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    stranger = Entry("stranger", actors[7])
    _rejected(direct_vm, contract, stranger, 3, 1, "Only the senior claim's author")
    _rejected(direct_vm, contract, entries[2], 3, 1, "Only the senior claim's author")
    _rejected(direct_vm, contract, entries[1], 3, 1, "Only the senior claim's author")
    challenge(contract, direct_vm, entries[0], 3, 1)       # control


def test_only_the_earlier_claim_may_challenge_the_later_one(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    with direct_vm.expect_revert("earlier claim"):
        challenge(contract, direct_vm, entries[2], 1, 3)    # later challenging earlier
    with direct_vm.expect_revert("earlier claim"):
        challenge(contract, direct_vm, entries[1], 2, 2)    # a claim against itself
    assert claim_of(contract, 1)["status"] == "revealed"
    assert claim_of(contract, 2)["status"] == "revealed"


def test_unknown_claims_cannot_be_challenged(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    for junior, senior in ((0, 1), (4, 1), (3, 0), (3, 99)):
        with direct_vm.expect_revert("Claim not found"):
            challenge(contract, direct_vm, entries[0], junior, senior)


def test_an_author_cannot_challenge_their_own_other_claim(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    first = Entry("first", actors[0])
    second = Entry("second", actors[0], verdict="derivative")
    first_seq = register(contract, direct_vm, first, NOW_TS + 60)
    second_seq = register(contract, direct_vm, second, NOW_TS + 120)
    serve(direct_vm, [second])
    with direct_vm.expect_revert("your own claim"):
        challenge(contract, direct_vm, first, second_seq, first_seq)
    assert credit(contract, actors[0]) == BOND               # topped up, never taken
    assert claim_of(contract, second_seq)["status"] == "revealed"


def test_claims_without_standing_cannot_challenge(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    unrevealed = Entry("ghost", actors[0])
    direct_vm.warp(at(NOW_TS + 10))
    ghost_seq = commit(contract, direct_vm, unrevealed)
    junior = Entry("junior", actors[1])
    junior_seq = register(contract, direct_vm, junior, NOW_TS + 60)
    with direct_vm.expect_revert("no standing"):
        challenge(contract, direct_vm, unrevealed, junior_seq, ghost_seq)   # still sealed
    direct_vm.warp(at(NOW_TS + 10 + REVEAL_WINDOW))
    direct_vm.sender = actors[5]
    contract.forfeit_unrevealed(ghost_seq)
    with direct_vm.expect_revert("no standing"):
        challenge(contract, direct_vm, unrevealed, junior_seq, ghost_seq)   # forfeited
    assert claim_of(contract, junior_seq)["status"] == "revealed"


def test_a_claim_that_is_not_freshly_revealed_cannot_be_challenged(
    direct_vm, direct_deploy, actors
):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    challenge(contract, direct_vm, entries[0], 3, 1)
    _rejected(direct_vm, contract, entries[1], 3, 2, "cannot be challenged now",
              status="challenged")                            # already under challenge
    sealed = Entry("sealed", actors[8])
    direct_vm.warp(at(NOW_TS + 400))
    sealed_seq = commit(contract, direct_vm, sealed)
    deposit(contract, direct_vm, actors[0], BOND)
    direct_vm.sender = actors[0]
    with direct_vm.expect_revert("cannot be challenged now"):
        contract.challenge_claim(sealed_seq, 1)               # junior still sealed


def test_the_challenge_window_is_exclusive_at_its_end(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    until = claim_of(contract, 3)["challenge_until"]
    direct_vm.warp(at(until))
    _rejected(direct_vm, contract, entries[0], 3, 1, "Challenge window has closed")
    direct_vm.warp(at(until - 1))
    challenge(contract, direct_vm, entries[0], 3, 1, topup=False)    # last second still counts


def test_settled_and_revoked_claims_cannot_be_challenged_again(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(claim_of(contract, 3)["challenge_until"]))
    settle(contract, direct_vm, actors[9], 3)
    with direct_vm.expect_revert("cannot be challenged now"):
        challenge(contract, direct_vm, entries[0], 3, 1)


def test_a_pair_can_only_be_challenged_once_and_the_claim_has_a_challenge_cap(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    seniors = [Entry(f"s{i}", actors[i]) for i in range(4)]
    junior = Entry("junior", actors[5], verdict="distinct")
    for index, entry in enumerate(seniors):
        register(contract, direct_vm, entry, NOW_TS + 60 * (index + 1))
    junior_seq = register(contract, direct_vm, junior, NOW_TS + 400)
    serve(direct_vm, [junior])
    direct_vm.warp(at(NOW_TS + 500))

    def lose_a_challenge(index):
        challenge(contract, direct_vm, seniors[index], junior_seq, index + 1)
        direct_vm.sender = seniors[index].who
        contract.judge_challenge(junior_seq)                  # rejected each time
        assert claim_of(contract, junior_seq)["status"] == "revealed"

    lose_a_challenge(0)
    _rejected(direct_vm, contract, seniors[0], junior_seq, 1, "already challenged")
    lose_a_challenge(1)
    lose_a_challenge(2)
    assert claim_of(contract, junior_seq)["challenges_started"] == 3
    _rejected(direct_vm, contract, seniors[3], junior_seq, 4, "Challenge limit reached")
    _rejected(direct_vm, contract, seniors[0], junior_seq, 1, "Challenge limit reached")
    check_books(contract, actors)


def test_challenging_needs_the_full_bond_in_credit(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    deposit(contract, direct_vm, actors[0], BOND - 1)
    direct_vm.sender = actors[0]
    with direct_vm.expect_revert("Insufficient available credit"):
        contract.challenge_claim(3, 1)
    assert claim_of(contract, 3)["status"] == "revealed"
    assert contract.get_accounting()["total_locked_bonds"] == 0
    deposit(contract, direct_vm, actors[0], 1)
    direct_vm.sender = actors[0]
    contract.challenge_claim(3, 1)
    assert contract.get_accounting()["total_locked_bonds"] == BOND
    assert challenge_of(contract, 3)["challenger"].lower() == addr(actors[0])


# ------------------------------------------------------------------- judge / expire


def test_judging_and_expiring_need_a_pending_challenge(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    for seq in (1, 3):
        direct_vm.sender = actors[0]
        with direct_vm.expect_revert("no pending challenge"):
            contract.judge_challenge(seq)
        with direct_vm.expect_revert("no pending challenge"):
            contract.expire_challenge(seq)
    for bad in (0, 99):
        with direct_vm.expect_revert("Claim not found"):
            contract.judge_challenge(bad)
        with direct_vm.expect_revert("Claim not found"):
            contract.expire_challenge(bad)


def test_settling_and_forfeiting_need_the_right_state(direct_vm, direct_deploy, actors):
    contract, entries, _ = _trio(direct_vm, direct_deploy, actors)
    direct_vm.sender = actors[4]
    with direct_vm.expect_revert("not awaiting reveal"):
        contract.forfeit_unrevealed(1)                        # already revealed
    challenge(contract, direct_vm, entries[0], 3, 1)
    direct_vm.warp(at(claim_of(contract, 3)["challenge_until"] + 10))
    with direct_vm.expect_revert("cannot be settled"):
        settle(contract, direct_vm, actors[4], 3)             # challenged: must be decided first
    for bad in (0, 99):
        with direct_vm.expect_revert("Claim not found"):
            contract.settle_claim(bad)
        with direct_vm.expect_revert("Claim not found"):
            contract.forfeit_unrevealed(bad)
