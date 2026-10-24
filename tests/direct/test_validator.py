"""Equivalence rules: what a validator must reproduce to agree with the leader."""

import json

import pytest

from tests.direct.support import (
    NOW_TS, PROMPT_HEAD, Entry, at, chart, deploy, judge, register, run_to_challenge,
)


def _leader(direct_vm, direct_deploy, actors, leader_verdict, count=3):
    contract = deploy(direct_vm, direct_deploy)
    senior = Entry("senior", actors[0],
                   elements=[f"SENIOR element number {i} of the claim" for i in range(count)])
    junior = Entry("junior", actors[1], verdict=leader_verdict)
    _, junior_seq = run_to_challenge(contract, direct_vm, senior, junior)
    judge(contract, direct_vm, actors[0], junior_seq)
    return contract


def _validator_sees(direct_vm, answer):
    direct_vm.clear_mocks()
    text = answer if isinstance(answer, str) else json.dumps(answer)
    direct_vm.mock_llm(PROMPT_HEAD, text)


def D(n, k, u=0, **kw):
    """A chart with k disclosed, u unclear and the rest absent, out of n elements."""
    return chart(["disclosed"] * k + ["unclear"] * u + ["absent"] * (n - k - u), **kw)


def test_same_upheld_overlap_with_different_prose_and_confidence_agrees(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    _validator_sees(direct_vm, chart(confidence=81, rationale="All three are disclosed."))
    assert direct_vm.run_validator() is True


def test_upheld_overlap_tolerates_one_borderline_element_but_not_two(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, D(5, 5), count=5)
    _validator_sees(direct_vm, D(5, 4))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, D(5, 3))        # still upheld (3 >= 3) but two elements apart
    assert direct_vm.run_validator() is False


def test_rejected_absence_tolerates_one_possible_element_but_not_two(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, D(5, 0), count=5)
    _validator_sees(direct_vm, D(5, 1))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, D(5, 1, 1))     # possible = 2, one element wider than 1? no: 2 vs 0
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, D(5, 2))        # possible = 2: two apart
    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("name,answer", [
    ("inconclusive_unclear", chart(("disclosed", "unclear", "absent"))),
    ("inconclusive_low_confidence", chart(confidence=60)),
    ("rejected", chart(("absent", "absent", "absent"))),
    ("junior_injection", chart(("absent",) * 3, junior="yes")),
    ("senior_injection", chart(senior="yes")),
])
def test_an_upheld_leader_is_not_confirmed_by_any_other_outcome_or_basis(
    direct_vm, direct_deploy, actors, name, answer
):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    _validator_sees(direct_vm, answer)
    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("name,answer", [
    ("upheld", chart()),
    ("inconclusive", chart(("disclosed", "unclear", "absent"))),
    ("senior_injection_instead", chart(senior="yes")),
    ("junior_injection_instead", chart(("absent",) * 3, junior="yes")),
])
def test_a_rejected_leader_is_not_confirmed_by_any_other_outcome_or_basis(
    direct_vm, direct_deploy, actors, name, answer
):
    _leader(direct_vm, direct_deploy, actors, "distinct")
    _validator_sees(direct_vm, answer)
    assert direct_vm.run_validator() is False


def test_manipulation_verdicts_agree_whatever_the_element_charts_say(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, "junior_injection")
    _validator_sees(direct_vm, chart(("disclosed", "unclear", "absent"), junior="yes", confidence=99))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, chart(senior="yes"))
    assert direct_vm.run_validator() is False


def test_senior_manipulation_leader_needs_the_same_flag(direct_vm, direct_deploy, actors):
    _leader(direct_vm, direct_deploy, actors, "senior_injection")
    _validator_sees(direct_vm, chart(("absent",) * 3, senior="yes"))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, chart(("absent",) * 3))
    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("leader", ["undecided", "low_confidence", "both_injection",
                                    "senior_unclear_flag", "junior_unclear_flag"])
def test_an_inconclusive_leader_agrees_with_any_inconclusive_validator(
    direct_vm, direct_deploy, actors, leader
):
    _leader(direct_vm, direct_deploy, actors, leader)
    _validator_sees(direct_vm, chart(confidence=50))
    assert direct_vm.run_validator() is True


@pytest.mark.parametrize("answer", [chart(), chart(("absent",) * 3),
                                    chart(("absent",) * 3, junior="yes")])
def test_an_inconclusive_leader_never_agrees_with_a_decisive_validator(
    direct_vm, direct_deploy, actors, answer
):
    _leader(direct_vm, direct_deploy, actors, "undecided")
    _validator_sees(direct_vm, answer)
    assert direct_vm.run_validator() is False


def test_validator_rejects_a_valid_analysis_when_its_own_model_returns_garbage(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    _validator_sees(direct_vm, "not json at all")
    assert direct_vm.run_validator() is False


def test_a_failed_leader_is_only_confirmed_by_another_failure(direct_vm, direct_deploy, actors):
    _leader(direct_vm, direct_deploy, actors, "raw:garbage")
    _validator_sees(direct_vm, chart())
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, "also garbage")
    assert direct_vm.run_validator() is True


FORGED_LEADER_RESULTS = {
    "outcome_contradicts_chart": {"kind": "analysis", "outcome": "upheld", "analysis": D(3, 0)},
    "upheld_below_threshold": {"kind": "analysis", "outcome": "upheld", "analysis": D(3, 1)},
    "rejected_but_derivative": {"kind": "analysis", "outcome": "rejected", "analysis": D(3, 3)},
    "decisive_despite_low_confidence": {"kind": "analysis", "outcome": "upheld",
                                        "analysis": D(3, 3, confidence=10)},
    "inconclusive_label_on_decisive_chart": {"kind": "analysis", "outcome": "inconclusive",
                                             "analysis": D(3, 3)},
    "unknown_outcome": {"kind": "analysis", "outcome": "banana", "analysis": D(3, 3)},
    "unknown_kind": {"kind": "oracle", "outcome": "upheld"},
    "missing_kind": {"outcome": "upheld", "analysis": D(3, 3)},
    "not_a_dict": ["upheld"],
    "none": None,
    "empty_failure_reason": {"kind": "failure", "reason": ""},
    "oversized_failure_reason": {"kind": "failure", "reason": "x" * 65},
    "non_string_failure_reason": {"kind": "failure", "reason": 7},
    "wrong_element_count": {"kind": "analysis", "outcome": "upheld", "analysis": D(4, 4)},
    "upheld_with_senior_unclear": {"kind": "analysis", "outcome": "upheld",
                                   "analysis": D(3, 3, senior="unclear")},
    "rejected_with_junior_unclear": {"kind": "analysis", "outcome": "rejected",
                                     "analysis": D(3, 0, junior="unclear")},
}


@pytest.mark.parametrize("name", sorted(FORGED_LEADER_RESULTS))
def test_forged_or_inconsistent_leader_results_are_never_confirmed(
    direct_vm, direct_deploy, actors, name
):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    _validator_sees(direct_vm, chart())                   # an honest, agreeing validator model
    assert direct_vm.run_validator(leader_result=FORGED_LEADER_RESULTS[name]) is False


def test_a_leader_that_raised_is_never_confirmed(direct_vm, direct_deploy, actors):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    _validator_sees(direct_vm, chart())
    assert direct_vm.run_validator(leader_error=Exception("leader crashed")) is False


def test_well_formed_failure_from_the_leader_is_accepted_only_alongside_a_failure(
    direct_vm, direct_deploy, actors
):
    _leader(direct_vm, direct_deploy, actors, "derivative")
    leader = {"kind": "failure", "reason": "anything_short"}
    _validator_sees(direct_vm, "garbage")
    assert direct_vm.run_validator(leader_result=leader) is True
    _validator_sees(direct_vm, chart())
    assert direct_vm.run_validator(leader_result=leader) is False
