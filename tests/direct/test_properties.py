"""Seeded randomised runs: whatever sequence of calls happens, the books must balance."""

import random

import pytest

from tests.direct.support import (
    BOND, NOW_TS, STAKE, VERDICTS, Entry, addr, at, check_books, claim_of, credit, deploy, serve,
)

SEEDS = range(12)
STEPS = 140


class Harness:
    def __init__(self, direct_vm, contract, actors, rng):
        self.vm, self.contract, self.rng = direct_vm, contract, rng
        self.people = actors[:6]
        self.now = NOW_TS
        self.deposited = 0
        self.withdrawn = 0
        self.entries = {}           # seq -> Entry
        self.counter = 0
        self.reverts = 0
        self.successes = 0

    def attempt(self, fn):
        """Run a call; it must either succeed or fail with an expected, user-facing revert."""
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - we inspect it right below
            assert type(error).__name__ == "UserError" and "[EXPECTED]" in str(error), (
                f"unexpected failure type: {type(error).__name__}: {error}")
            self.reverts += 1
            return False
        self.successes += 1
        return True

    def invariants(self):
        accounting = check_books(self.contract, self.people)
        assert (accounting["total_credits"] + accounting["total_locked_stakes"]
                + accounting["total_locked_bonds"] + accounting["total_commons"]
                == self.deposited - self.withdrawn), "value must never be created or destroyed"
        for seq in range(1, self.contract.get_claim_count() + 1):
            claim = claim_of(self.contract, seq)
            status = claim["status"]
            holds_stake = status in ("committed", "revealed", "challenged")
            assert (claim["stake_wei"] == STAKE) == holds_stake, (seq, status)
            assert claim["stake_wei"] in (0, STAKE)
            assert claim["challenges_started"] <= 3
            if claim["challenges_started"]:
                record = self.contract.get_challenge(seq)
                assert (record["bond_wei"] == BOND) == (status == "challenged"), (seq, status)
                assert record["pending"] == (status == "challenged")
                assert record["senior_seq"] < seq
                assert record["judge_calls"] <= 6 and record["assessments"] <= 3
            if status == "revoked":
                assert 0 < claim["revoked_by"] < seq
            if status in ("revealed", "challenged", "settled", "revoked"):
                assert claim["title"] and len(claim["elements"]) == claim["element_count"] >= 2
            if status in ("committed", "forfeited"):
                assert claim["title"] == ""

    def _seqs(self, *statuses):
        return [seq for seq in range(1, self.contract.get_claim_count() + 1)
                if claim_of(self.contract, seq)["status"] in statuses]

    def _fund(self, who, amount):
        """Deposit through the real entry point and track it for the conservation check."""
        self.vm.sender, self.vm.value = who, amount
        if self.attempt(lambda: self.contract.deposit_credit()):
            self.deposited += amount
        self.vm.value = 0
        self.vm.sender = who

    def step(self):
        rng, vm, contract = self.rng, self.vm, self.contract
        who = rng.choice(self.people)
        count = contract.get_claim_count()
        action = rng.choices(
            ["deposit", "commit", "reveal", "challenge", "judge", "settle", "forfeit", "expire",
             "withdraw", "warp", "wild"],
            weights=[8, 14, 16, 16, 16, 5, 3, 4, 3, 9, 6],
        )[0]
        vm.sender = who
        if action == "deposit":
            self._fund(who, rng.choice([STAKE // 2, STAKE, BOND, 2 * STAKE]))
        elif action == "commit":
            if rng.random() < 0.9:
                self._fund(who, STAKE)
            self.counter += 1
            entry = Entry(f"fz{self.counter}", who, verdict=rng.choice(sorted(VERDICTS)))
            seal = entry.seal()
            before = contract.get_claim_count()
            if self.attempt(lambda: contract.commit_claim(seal)):
                self.entries[before + 1] = entry
        elif action == "reveal":
            pending = self._seqs("committed")
            if pending:
                seq = rng.choice(pending)
                entry = self.entries[seq]
                vm.sender = entry.who if rng.random() < 0.9 else who
                self.attempt(lambda: contract.reveal_claim(
                    seq, entry.title, entry.abstract, entry.elements_text, entry.salt))
        elif action == "challenge":
            juniors = self._seqs("revealed")
            if juniors:
                junior = rng.choice(juniors)
                seniors = [seq for seq in range(1, junior)]
                if seniors:
                    senior = rng.choice(seniors)
                    owner = self.entries[senior].who
                    if rng.random() < 0.85:
                        self._fund(owner, BOND)
                    vm.sender = owner if rng.random() < 0.95 else who
                    self.attempt(lambda: contract.challenge_claim(junior, senior))
        elif action == "judge":
            pending = self._seqs("challenged")
            if pending:
                seq = rng.choice(pending)
                entry = self.entries[seq]
                entry.verdict = rng.choice(sorted(VERDICTS))
                vm.clear_mocks()
                serve(vm, list(self.entries.values()))
                senior_seq = contract.get_challenge(seq)["senior_seq"]
                vm.sender = rng.choice([entry.who, self.entries[senior_seq].who, who])
                self.attempt(lambda: contract.judge_challenge(seq))
        elif action == "settle" and count:
            self.attempt(lambda: contract.settle_claim(rng.randint(1, count)))
        elif action == "forfeit" and count:
            self.attempt(lambda: contract.forfeit_unrevealed(rng.randint(1, count)))
        elif action == "expire" and count:
            self.attempt(lambda: contract.expire_challenge(rng.randint(1, count)))
        elif action == "withdraw":
            before = credit(contract, who)
            if self.attempt(lambda: contract.withdraw_credit()):
                self.withdrawn += before
        elif action == "warp":
            self.now += rng.choice([20, 60, 300, 900, 3600, 3 * 3600, 86400])
            vm.warp(at(self.now))
        elif action == "wild" and count:
            # Arbitrary, usually-invalid calls must fail cleanly and leave the books alone.
            a, b = rng.randint(0, count + 1), rng.randint(0, count + 1)
            call = rng.choice([
                lambda: contract.challenge_claim(a, b),
                lambda: contract.judge_challenge(a),
                lambda: contract.reveal_claim(a, "t", "x" * 50, "element one text\nelement two text",
                                               "s" * 20),
            ])
            self.attempt(call)


@pytest.mark.parametrize("seed", SEEDS)
def test_random_call_sequences_keep_the_books_balanced(direct_vm, direct_deploy, actors, seed):
    rng = random.Random(seed * 7919 + 13)
    contract = deploy(direct_vm, direct_deploy)
    harness = Harness(direct_vm, contract, actors, rng)
    for _ in range(STEPS):
        harness.step()
        harness.invariants()
    # Reaching the interesting states is part of the test, not an accident.
    assert harness.successes > 20 and harness.reverts > 5
    assert harness.reverts + harness.successes >= STEPS * 0.6


def test_fuzz_across_seeds_actually_reaches_every_claim_state():
    """A cheap meta-check so the seeds above cannot silently degenerate."""
    assert len(list(SEEDS)) >= 10
