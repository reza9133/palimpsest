"""Shared scenario helpers for the Palimpsest Direct Mode tests."""

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.palimpsest_tools import commitment as client_commitment  # noqa: E402

CONTRACT = "contracts/palimpsest.py"
RUNNER = "v0.2.16"
NOW = "2026-10-01T12:00:00Z"
NOW_TS = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())
HOUR = 3600
DAY = 24 * HOUR

STAKE = 3 * 10**14
BOND = 2 * 10**14
REVEAL_WINDOW = HOUR
CHALLENGE_WINDOW = DAY
JUDGE_WINDOW = DAY
PERCENT = 60
NEED = 2  # ceil(3 * 60 / 100) for the default three-element claims

# Every comparison prompt starts with this sentence.
PROMPT_HEAD = r"(?s)You are independently comparing two registered claims"
CONFIDENT = 92


def at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def addr(value):
    return "0x" + value.hex().lower().removeprefix("0x")


def chart(elements=("disclosed", "disclosed", "disclosed"), senior="no", junior="no",
          confidence=CONFIDENT, rationale="Each element was compared with the junior text."):
    return {
        "elements": list(elements),
        "senior_instruction_attempt": senior,
        "junior_instruction_attempt": junior,
        "confidence": confidence,
        "rationale": rationale,
    }


# Verdicts a mocked model can give about an entry when it is the JUNIOR claim.
VERDICTS = {
    "derivative": chart(),                                           # 3/3 -> upheld
    "partial": chart(("disclosed", "disclosed", "absent")),          # 2/3 -> upheld
    "borderline": chart(("disclosed", "absent", "absent")),          # 1/3 -> rejected
    "distinct": chart(("absent", "absent", "absent")),               # rejected
    "undecided": chart(("disclosed", "unclear", "absent")),          # inconclusive
    "low_confidence": chart(confidence=79),                          # inconclusive
    "junior_injection": chart(("absent", "absent", "absent"), junior="yes"),  # upheld
    "senior_injection": chart(senior="yes"),                         # rejected
    "both_injection": chart(senior="yes", junior="yes"),             # inconclusive
    "senior_unclear_flag": chart(senior="unclear"),                  # inconclusive
    "junior_unclear_flag": chart(("absent", "absent", "absent"), junior="unclear"),  # inconclusive
}


@dataclass
class Entry:
    """One author's claim plus the verdict a mocked model gives when it is the junior."""

    name: str
    who: bytes
    verdict: object = "distinct"
    title: str = ""
    abstract: str = ""
    elements: list = field(default_factory=list)
    salt: str = ""

    def __post_init__(self):
        if not self.title:
            self.title = f"{self.marker}: graded membrane filtration"
        if not self.abstract:
            self.abstract = (
                f"{self.marker} describes a staged membrane filter that separates particles "
                f"by size, recirculates what it retains and records every stage."
            )
        if not self.elements:
            self.elements = [
                f"{self.marker} E1 a graded membrane separates coarse particles first",
                f"{self.marker} E2 the retained fraction recirculates through a second stage",
                f"{self.marker} E3 particle size is logged at every stage for audit",
            ]
        if not self.salt:
            self.salt = hashlib.sha256(f"salt-{self.name}".encode()).hexdigest()[:40]

    @property
    def marker(self):
        return f"CLAIM-{self.name.upper()}"

    @property
    def elements_text(self):
        return "\n".join(self.elements)

    def verdict_json(self):
        """The text the mocked model returns. "raw:..." verdicts are returned verbatim."""
        if isinstance(self.verdict, str) and self.verdict.startswith("raw:"):
            return self.verdict[4:]
        verdict = VERDICTS[self.verdict] if isinstance(self.verdict, str) else self.verdict
        return json.dumps(verdict)

    def seal(self, **overrides):
        return client_commitment(
            addr(self.who),
            overrides.get("title", self.title),
            overrides.get("abstract", self.abstract),
            overrides.get("elements", self.elements),
            overrides.get("salt", self.salt),
        )


def deploy(direct_vm, direct_deploy, **overrides):
    direct_vm.warp(NOW)
    params = dict(stake_wei=STAKE, challenge_bond_wei=BOND, reveal_window=REVEAL_WINDOW,
                  challenge_window=CHALLENGE_WINDOW, judge_window=JUDGE_WINDOW,
                  overlap_percent=PERCENT)
    params.update(overrides)
    return direct_deploy(CONTRACT, sdk_version=RUNNER, **params)


def deposit(contract, direct_vm, who, amount):
    direct_vm.sender = who
    direct_vm.value = amount
    assert contract.deposit_credit() == amount
    direct_vm.value = 0


def commit(contract, direct_vm, entry, stake=STAKE, topup=True, **overrides):
    if topup:
        deposit(contract, direct_vm, entry.who, stake)
    direct_vm.sender = entry.who
    return contract.commit_claim(entry.seal(**overrides))


def reveal(contract, direct_vm, entry, seq, who=None, **overrides):
    direct_vm.sender = who if who is not None else entry.who
    contract.reveal_claim(
        seq,
        overrides.get("title", entry.title),
        overrides.get("abstract", entry.abstract),
        overrides.get("elements_text", entry.elements_text),
        overrides.get("salt", entry.salt),
    )


def serve(direct_vm, entries):
    """Mock the model for every entry, keyed on the junior block's title marker."""
    for entry in entries:
        direct_vm.mock_llm(
            PROMPT_HEAD + r".*BEGIN_UNTRUSTED_JUNIOR\nTitle: [^\n]*" + entry.marker,
            entry.verdict_json(),
        )


def challenge(contract, direct_vm, senior_entry, junior_seq, senior_seq, bond=BOND, topup=True):
    if topup:
        deposit(contract, direct_vm, senior_entry.who, bond)
    direct_vm.sender = senior_entry.who
    contract.challenge_claim(junior_seq, senior_seq)


def judge(contract, direct_vm, caller, junior_seq):
    direct_vm.sender = caller
    contract.judge_challenge(junior_seq)


def settle(contract, direct_vm, caller, seq):
    direct_vm.sender = caller
    contract.settle_claim(seq)


def forfeit(contract, direct_vm, caller, seq):
    direct_vm.sender = caller
    contract.forfeit_unrevealed(seq)


def expire(contract, direct_vm, caller, seq):
    direct_vm.sender = caller
    contract.expire_challenge(seq)


def credit(contract, who):
    return contract.get_available_credit(addr(who))


def claim_of(contract, seq):
    return contract.get_claim(seq)


def challenge_of(contract, seq):
    return contract.get_challenge(seq)


def register(contract, direct_vm, entry, at_ts=None):
    """Commit and immediately reveal an entry; returns its priority number."""
    if at_ts is not None:
        direct_vm.warp(at(at_ts))
    seq = commit(contract, direct_vm, entry)
    reveal(contract, direct_vm, entry, seq)
    return seq


def run_to_challenge(contract, direct_vm, senior, junior, start=NOW_TS + 60):
    """Register senior then junior, serve the model, open a challenge. Returns (s, j)."""
    senior_seq = register(contract, direct_vm, senior, start)
    junior_seq = register(contract, direct_vm, junior, start + 60)
    serve(direct_vm, [junior, senior])
    direct_vm.warp(at(start + 120))
    challenge(contract, direct_vm, senior, junior_seq, senior_seq)
    return senior_seq, junior_seq


def check_books(contract, addresses):
    """Assert the whole ledger is internally consistent and return the accounting."""
    accounting = contract.get_accounting()
    held = sum(credit(contract, who) for who in addresses)
    assert held == accounting["total_credits"], "per-account credits must sum to the aggregate"
    stakes = 0
    bonds = 0
    for seq in range(1, contract.get_claim_count() + 1):
        claim = contract.get_claim(seq)
        stakes += claim["stake_wei"]
        if claim["challenges_started"]:
            bonds += contract.get_challenge(seq)["bond_wei"]
    assert stakes == accounting["total_locked_stakes"], "locked stakes must match unreleased claims"
    assert bonds == accounting["total_locked_bonds"], "locked bonds must match pending challenges"
    assert accounting["accounted_liabilities"] == (
        accounting["total_credits"] + accounting["total_locked_stakes"]
        + accounting["total_locked_bonds"] + accounting["total_commons"]
    )
    return accounting
