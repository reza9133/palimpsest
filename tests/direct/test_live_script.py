"""Run the live Studionet scenario against the Direct Mode contract.

The live script is the one piece that cannot be exercised without the real network, and a
bug in it would waste an hour of validator time. This test swaps the network for a small
simulated chain (40 s per transaction, about 1 s per read, validators mocked) and runs the
complete script, including its hostile calls, timing windows and report writer.
"""

import json

import pytest

from scripts import live_e2e
from scripts.live_claims import CLAIMS
from tests.direct.support import NOW_TS, PROMPT_HEAD, Entry, at, chart, deploy


class SimChain(live_e2e.Chain):
    NAMES = ["funder", "ghost", "ada", "bo", "cy", "di", "flo", "stranger"]

    def __init__(self, direct_vm, contract, actors, tx_seconds=40, read_seconds=1, before_write=None):
        self.before_write = before_write
        self.vm, self.contract = direct_vm, contract
        self.keys = {name: actors[index] for index, name in enumerate(self.NAMES)}
        self.clock = NOW_TS + 100
        self.tx_seconds, self.read_seconds = tx_seconds, read_seconds
        self.balances = {name: 0 for name in self.NAMES}
        self.log = []

    def _tick(self, seconds):
        self.clock += seconds
        self.vm.warp(at(self.clock))

    def now(self):
        return self.clock

    def sleep_until(self, timestamp, reason):
        if timestamp > self.clock:
            self._tick(timestamp - self.clock)

    def address(self, name):
        return "0x" + self.keys[name].hex()

    def balance(self, name_or_address):
        return self.balances[name_or_address]

    def fund(self, name, wei):
        self.balances[name] += wei
        return True

    def wait_for_balance(self, name, at_least, seconds):
        return self.balances[name]

    def read(self, function, args):
        self._tick(self.read_seconds)
        return getattr(self.contract, function)(*args)

    def write(self, name, function, args, value=0, label="", expect="success"):
        self._tick(self.tx_seconds)
        record = live_e2e.TxRecord(label or function, self.address(name), function, expected=expect,
                                   tx_hash="0x" + "00" * 31 + f"{len(self.log):02x}")
        self.log.append((name, function))
        if self.before_write:
            self.before_write(self, name, function, args)
        self.vm.sender, self.vm.value = self.keys[name], value
        credit_before = self.contract.get_available_credit(self.address(name))
        try:
            if value > self.balances[name]:
                raise RuntimeError("insufficient balance")
            getattr(self.contract, function)(*args)
        except Exception:  # noqa: BLE001 - any failure is a revert from the script's viewpoint
            record.result = "FINISHED_WITH_ERROR"
        else:
            record.result = "FINISHED_WITH_RETURN"
            self.balances[name] -= value
            if function == "withdraw_credit":
                self.balances[name] += credit_before
        finally:
            self.vm.value = 0
        record.seconds = self.tx_seconds
        return record


def _serve_model(direct_vm, verdicts):
    marker = {
        "bo": "Stepped shaking screens",
        "cy": "Solar tunnel dryer",
        "di": "Cascade of three sieves",
    }
    for name, verdict in verdicts.items():
        direct_vm.mock_llm(PROMPT_HEAD + r".*BEGIN_UNTRUSTED_JUNIOR\nTitle: [^\n]*" + marker[name],
                           json.dumps(verdict))


HONEST = {
    "bo": chart(["disclosed"] * 4),
    "cy": chart(["absent"] * 4),
    "di": chart(["disclosed"] * 4, junior="yes"),
}


def _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch, verdicts=HONEST, **chain_kwargs):
    before_write = chain_kwargs.pop("before_write", None)
    monkeypatch.setattr(live_e2e, "STATE_DIR", tmp_path / ".live")
    monkeypatch.setattr(live_e2e, "REPORT_DIR", tmp_path / "reports")
    contract = deploy(direct_vm, direct_deploy, **{
        "stake_wei": live_e2e.DEPLOY_ARGS["stake_wei"],
        "challenge_bond_wei": live_e2e.DEPLOY_ARGS["challenge_bond_wei"],
        "reveal_window": live_e2e.DEPLOY_ARGS["reveal_window"],
        "challenge_window": live_e2e.DEPLOY_ARGS["challenge_window"],
        "judge_window": live_e2e.DEPLOY_ARGS["judge_window"],
        "overlap_percent": live_e2e.DEPLOY_ARGS["overlap_percent"],
    })
    _serve_model(direct_vm, verdicts)
    chain = SimChain(direct_vm, contract, actors, before_write=before_write, **chain_kwargs)
    code = live_e2e.main([], chain_factory=lambda state: chain)
    reports = sorted((tmp_path / "reports").glob("live-e2e-*.json"))
    assert reports, "the script must always write a report"
    return code, json.loads(reports[-1].read_text()), chain


def _failures(payload):
    return [f"{c['section']}: {c['name']} (expected {c['expected']}, saw {c['observed']})"
            for c in payload["checks"] if not c["passed"]]


def test_the_full_live_scenario_passes_against_the_contract(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    code, payload, chain = _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch)
    assert _failures(payload) == []
    assert payload["fatal"] == ""
    assert code == 0
    assert payload["passed"] >= 130
    assert len(payload["transactions"]) >= 55
    registry = payload["sections"]["registry"]
    assert {n: r["status"] for n, r in registry.items()} == {
        "ghost": "forfeited", "ada": "settled", "bo": "revoked", "cy": "settled", "di": "revoked",
        "flo": "settled"}
    assert list((tmp_path / "reports").glob("live-e2e-*.md"))


def test_the_script_still_passes_when_the_network_is_twice_as_slow(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    code, payload, _ = _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch,
                            tx_seconds=80, read_seconds=2)
    assert _failures(payload) == []
    assert code == 0


def test_an_unexpected_model_verdict_is_reported_as_a_finding_not_a_crash(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    """If the model refuses to decide about bo, the semantic check fails but the ledger
    checks, which derive their expectations from the observed verdicts, still hold."""
    verdicts = dict(HONEST, bo=chart(["disclosed", "unclear", "absent", "absent"]))
    code, payload, _ = _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch, verdicts)
    failed = _failures(payload)
    assert code == 1 and payload["fatal"] == ""
    assert failed and all("bo" in line for line in failed), failed
    assert any("plain paraphrase" in line for line in failed)
    assert any("ledger credit equals the rule-derived amount" in c["name"] and c["passed"]
               for c in payload["checks"])
    assert not any("still locked" in line or "left unclaimed" in line for line in failed)
    registry = payload["sections"]["registry"]
    assert registry["bo"]["status"] == "settled"       # the rejected challenge let it settle


def test_a_deployment_with_windows_that_are_too_short_is_refused_up_front(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    monkeypatch.setattr(live_e2e, "STATE_DIR", tmp_path / ".live")
    monkeypatch.setattr(live_e2e, "REPORT_DIR", tmp_path / "reports")
    contract = deploy(direct_vm, direct_deploy, reveal_window=600)
    chain = SimChain(direct_vm, contract, actors)
    code = live_e2e.main([], chain_factory=lambda state: chain)
    payload = json.loads(sorted((tmp_path / "reports").glob("*.json"))[-1].read_text())
    assert code == 2 and "reveal window" in payload["fatal"]
    assert chain.log == []                      # nothing was sent


def test_the_scripted_texts_satisfy_the_contract_rules():
    from scripts.palimpsest_tools import validate_claim
    for name, item in CLAIMS.items():
        assert validate_claim(item["title"], item["abstract"], item["elements"]) == [], name


def test_a_model_that_does_not_flag_the_injection_still_yields_the_expected_verdict(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    verdicts = dict(HONEST, di=chart(["disclosed"] * 4))          # copy detected by overlap alone
    code, payload, _ = _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch, verdicts)
    assert _failures(payload) == [] and code == 0


def test_the_script_retries_judging_after_a_garbled_model_answer(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    state = {"judge_calls": 0}

    def flip(chain, name, function, args):
        if function == "judge_challenge" and name == "ada" and args[0] == 3:   # bo is claim 3
            state["judge_calls"] += 1
            if state["judge_calls"] == 2:
                _serve_model(chain.vm, {"bo": HONEST["bo"]})

    # The first answer for bo is garbage; the hook swaps in the honest chart before the retry.
    code, payload, chain = _run(
        direct_vm, direct_deploy, actors, tmp_path, monkeypatch,
        {"cy": HONEST["cy"], "di": HONEST["di"], "bo": {"nonsense": True}}, before_write=flip)
    assert _failures(payload) == [] and code == 0
    judge_txs = [t for t in payload["transactions"] if t["label"].startswith("judge bo")]
    assert len(judge_txs) == 2                                    # one failure, one decision


def test_inconclusive_verdicts_void_a_challenge_and_the_ledger_checks_still_hold(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch
):
    verdicts = dict(HONEST, cy=chart(["disclosed", "unclear", "unclear", "absent"]))
    code, payload, _ = _run(direct_vm, direct_deploy, actors, tmp_path, monkeypatch, verdicts)
    failed = _failures(payload)
    assert code == 1 and payload["fatal"] == ""
    assert failed and all("cy" in line for line in failed), failed
    assert any("cy (different invention)" in line for line in failed)
    credits = [c for c in payload["checks"] if "ledger credit equals" in c["name"]]
    assert credits and all(c["passed"] for c in credits)
    assert not any("still locked" in line or "left unclaimed" in line for line in failed)
    cy_judges = [t for t in payload["transactions"] if t["label"].startswith("judge cy")]
    assert len(cy_judges) == 3                                     # three assessments, then void


def test_check_only_verifies_the_contract_and_sends_nothing(
    direct_vm, direct_deploy, actors, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(live_e2e, "STATE_DIR", tmp_path / ".live")
    monkeypatch.setattr(live_e2e, "REPORT_DIR", tmp_path / "reports")
    contract = deploy(direct_vm, direct_deploy, **live_e2e.DEPLOY_ARGS)
    chain = SimChain(direct_vm, contract, actors)
    assert live_e2e.main(["--check-only"], chain_factory=lambda state: chain) == 0
    assert chain.log == []
    assert "needs" in capsys.readouterr().out


def test_print_deploy_args_matches_the_constructor_order(capsys):
    assert live_e2e.main(["--print-deploy-args"]) == 0
    out = capsys.readouterr().out
    assert "In order: 300000000000000, 200000000000000, 2400, 1500, 3600, 60" in out
