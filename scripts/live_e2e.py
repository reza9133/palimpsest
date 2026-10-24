#!/usr/bin/env python3
"""End-to-end live test of Palimpsest on GenLayer Studionet, with a written report.

Run it from a GitHub Codespace (or any machine with internet access):

    python scripts/live_e2e.py --deploy                     # deploy with the tested parameters
    python scripts/live_e2e.py --address 0xYourContract     # or test a contract you deployed

It drives one complete story through the real network with real validators and a real LLM:
six authors seal claims, one never reveals, a senior author challenges three later claims
(a plain paraphrase, an unrelated invention, and a copy that tries to instruct the
reviewer), validators chart every element, the contract applies the verdicts, windows
expire, claims settle or burn, and winners withdraw native GEN. Between those steps it
fires about eighteen hostile calls that must be refused without changing anything.

Everything is asserted from state read back out of the contract. A revert is accepted only
when the receipt says so or, if the receipt is ambiguous, when the contract state is proven
unchanged; a call that the receipt reports as successful is never treated as refused.

Reports (Markdown and JSON) are written to ./reports/. The whole run takes about an hour
because every Studionet transaction needs roughly 40 seconds to finalize.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live_claims import CLAIMS, JUNIORS, REGISTRATION_ORDER  # noqa: E402
from scripts.palimpsest_tools import clean, commitment as make_commitment, new_salt  # noqa: E402

CONTRACT_PATH = ROOT / "contracts" / "palimpsest.py"
REPORT_DIR = ROOT / "reports"
STATE_DIR = ROOT / ".live"
EXPLORER_TX = "https://explorer-studio.genlayer.com/tx/"
EXPLORER_ADDRESS = "https://explorer-studio.genlayer.com/address/"
DEFAULT_RPC = "https://studio.genlayer.com/api"
GEN = 10**18
MIN_RPC_GAP_SECONDS = 1.2  # Studionet allows 60 requests per minute per IP

# Constructor arguments the live run is designed around (and what --deploy uses).
DEPLOY_ARGS = {
    "stake_wei": 300_000_000_000_000,        # 0.0003 GEN
    "challenge_bond_wei": 200_000_000_000_000,  # 0.0002 GEN
    "reveal_window": 2400,
    "challenge_window": 1500,
    "judge_window": 3600,
    "overlap_percent": 60,
}
# Smallest windows (seconds) in which the scripted story still fits at ~40 s per transaction.
MIN_REVEAL, MIN_CHALLENGE, MIN_JUDGE = 1800, 1200, 1800

ACCOUNTS = ["funder"] + REGISTRATION_ORDER + ["stranger"]
CHALLENGES_BY_ADA = len(JUNIORS)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------- results


@dataclass
class TxRecord:
    label: str
    account: str
    function: str
    tx_hash: str = ""
    status: str = ""
    result: str = ""
    seconds: float = 0.0
    expected: str = "success"
    note: str = ""

    @property
    def link(self) -> str:
        return EXPLORER_TX + self.tx_hash if self.tx_hash else ""

    @property
    def succeeded(self) -> bool | None:
        text = self.result.upper()
        if "FINISHED_WITH_RETURN" in text:
            return True
        if "FINISHED_WITH_ERROR" in text or "ERROR" in text:
            return False
        return None


@dataclass
class Check:
    name: str
    passed: bool
    expected: str
    observed: str
    section: str = ""


@dataclass
class Report:
    started: str = field(default_factory=utc_now)
    finished: str = ""
    environment: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    txs: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    fatal: str = ""

    def check(self, section: str, name: str, passed: bool, expected: Any, observed: Any) -> bool:
        record = Check(name, bool(passed), str(expected), str(observed), section)
        self.checks.append(record)
        log(("PASS " if passed else "FAIL ") + f"{section}: {name} (expected {expected}, saw {observed})")
        return bool(passed)

    @property
    def all_passed(self) -> bool:
        return not self.fatal and bool(self.checks) and all(c.passed for c in self.checks)


# -------------------------------------------------------------------------- chain access


class ChainError(RuntimeError):
    pass


class Chain:
    """What the scenario needs from a network. Real and simulated chains implement it."""

    def now(self) -> int: ...
    def sleep_until(self, timestamp: int, reason: str) -> None: ...
    def address(self, name: str) -> str: ...
    def write(self, name: str, function: str, args: list, value: int = 0,
              label: str = "", expect: str = "success") -> TxRecord: ...
    def read(self, function: str, args: list) -> Any: ...
    def balance(self, name_or_address: str) -> int: ...
    def fund(self, name: str, wei: int) -> bool: ...
    def wait_for_balance(self, name: str, at_least: int, seconds: int) -> int: ...


class GenLayerChain(Chain):
    """Studionet through genlayer-py. Only thin, defensive wrappers live here."""

    def __init__(self, contract_address: str, state: dict, rpc_url: str | None, wait_seconds: int):
        try:
            from eth_account import Account
            from genlayer_py import create_account, create_client
            from genlayer_py.chains import studionet
        except ImportError as error:  # pragma: no cover - environment dependent
            raise ChainError(
                "genlayer-py is not installed. Run: python -m pip install -r requirements.txt"
            ) from error
        self._Account = Account
        self._create_account = create_account
        self.contract = contract_address
        self.state = state
        self.rpc_url = rpc_url or self._chain_rpc(studionet)
        self.wait_seconds = wait_seconds
        self._last_rpc = 0.0
        self.accounts: dict[str, Any] = {}
        for name in ACCOUNTS:
            key = state["keys"].get(name)
            if key is None:
                account = create_account()
                state["keys"][name] = self._key_hex(account)
            else:
                account = Account.from_key(key)
            self.accounts[name] = account
        self.client = create_client(chain=studionet, account=self.accounts["funder"])

    @staticmethod
    def _chain_rpc(chain) -> str:
        try:
            return chain.rpc_urls["default"]["http"][0]
        except Exception:  # noqa: BLE001
            return DEFAULT_RPC

    @staticmethod
    def _key_hex(account) -> str:
        key = getattr(account, "key", None) or getattr(account, "_private_key")
        text = key.hex() if hasattr(key, "hex") else str(key)
        return text if text.startswith("0x") else "0x" + text

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_rpc
        if gap < MIN_RPC_GAP_SECONDS:
            time.sleep(MIN_RPC_GAP_SECONDS - gap)
        self._last_rpc = time.monotonic()

    def _rpc(self, method: str, params: list) -> Any:
        self._throttle()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            self.rpc_url, data=body,
            headers={"content-type": "application/json", "accept": "application/json",
                     "user-agent": "palimpsest-live-e2e/0.1 (+python-urllib)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise ChainError(f"{method}: HTTP {error.code} from {self.rpc_url}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ChainError(f"{method}: cannot reach {self.rpc_url}: {error}") from error
        if "error" in payload:
            raise ChainError(f"{method}: {payload['error']}")
        return payload.get("result")

    def now(self) -> int:
        return int(time.time())

    def sleep_until(self, timestamp: int, reason: str) -> None:
        remaining = timestamp - self.now()
        if remaining > 0:
            log(f"waiting {remaining}s for {reason}")
        while True:
            remaining = timestamp - self.now()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 30))

    def address(self, name: str) -> str:
        return self.accounts[name].address

    def balance(self, name_or_address: str) -> int:
        address = self.address(name_or_address) if name_or_address in self.accounts else name_or_address
        return int(self._rpc("eth_getBalance", [address, "latest"]), 16)

    def fund(self, name: str, wei: int) -> bool:
        address = self.address(name)
        for params in ([address, wei], [address, hex(wei)]):
            try:
                self._rpc("sim_fundAccount", params)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def wait_for_balance(self, name: str, at_least: int, seconds: int) -> int:
        deadline = time.time() + seconds
        value = self.balance(name)
        while value < at_least and time.time() < deadline:
            time.sleep(8)
            value = self.balance(name)
        return value

    # ---- contract calls
    def _wait(self, tx_hash: str):
        interval_ms = 4000
        retries = max(10, int(self.wait_seconds * 1000 / interval_ms))
        try:
            return self.client.wait_for_transaction_receipt(
                transaction_hash=tx_hash, wait_until="finalized", interval=interval_ms,
                retries=retries,
            )
        except TypeError:
            from genlayer_py.types import TransactionStatus
            return self.client.wait_for_transaction_receipt(
                transaction_hash=tx_hash, status=TransactionStatus.FINALIZED,
                interval=interval_ms, retries=retries,
            )

    @staticmethod
    def _get(node: Any, key: str) -> Any:
        return node.get(key) if isinstance(node, dict) else getattr(node, key, None)

    @classmethod
    def _execution_outcome(cls, receipt: Any) -> tuple[str, str]:
        """(FINISHED_WITH_RETURN | FINISHED_WITH_ERROR | "", raw evidence).

        Only the final leader receipt and the top-level execution result are consulted.
        Validator receipts and nested history can contain errors that consensus overruled,
        and the consensus vote (for example MAJORITY_AGREE) never says whether a call reverted.
        An empty answer means "ambiguous"; the scenario then falls back on proven state.
        """
        leader_text = ""
        consensus = cls._get(receipt, "consensus_data")
        leaders = cls._get(consensus, "leader_receipt") if consensus is not None else None
        if isinstance(leaders, (list, tuple)) and leaders:
            leader_text = str(cls._get(leaders[0], "execution_result") or "").upper()
        top = cls._get(receipt, "tx_execution_result_name")
        top_text = str(getattr(top, "name", top) or "").upper()
        evidence = f"leader={leader_text or '-'}, top={top_text or '-'}"
        if leader_text in ("SUCCESS", "ERROR"):
            return ("FINISHED_WITH_RETURN" if leader_text == "SUCCESS" else "FINISHED_WITH_ERROR"), evidence
        if top_text in ("FINISHED_WITH_RETURN", "FINISHED_WITH_ERROR"):
            return top_text, evidence
        return "", evidence

    def _dump_receipt(self, tx_hash: str, receipt: Any) -> None:
        try:
            folder = STATE_DIR / "receipts"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{tx_hash}.json").write_text(json.dumps(receipt, indent=2, default=str)[:60000])
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _transient(error: Exception) -> bool:
        text = str(error)
        return any(marker in text for marker in ("502", "503", "504", "Bad gateway", "invalid JSON",
                                                 "timed out", "Connection"))

    @staticmethod
    def _field(receipt: Any, *names: str) -> str:
        for name in names:
            value = receipt.get(name) if isinstance(receipt, dict) else getattr(receipt, name, None)
            if value not in (None, ""):
                return str(getattr(value, "name", value))
        return ""

    def write(self, name: str, function: str, args: list, value: int = 0,
              label: str = "", expect: str = "success") -> TxRecord:
        record = TxRecord(label or function, self.address(name), function, expected=expect)
        started = time.monotonic()
        try:
            tx_hash = ""
            for attempt in range(4):
                self._throttle()
                try:
                    tx_hash = self.client.write_contract(
                        address=self.contract, function_name=function, account=self.accounts[name],
                        args=args, value=value,
                    )
                    break
                except Exception as error:  # noqa: BLE001
                    if attempt == 3 or not self._transient(error):
                        raise
                    time.sleep(5 * (attempt + 1))
            record.tx_hash = str(tx_hash)
            receipt = self._wait(record.tx_hash)
            self._dump_receipt(record.tx_hash, receipt)
            record.status = self._field(receipt, "status_name", "statusName", "status")
            record.result, evidence = self._execution_outcome(receipt)
            vote = self._field(receipt, "result_name", "resultName")
            record.note = f"consensus={vote or '?'}; {evidence}"
        except Exception as error:  # noqa: BLE001
            record.note = f"{type(error).__name__}: {error}"
            if expect == "success":
                record.result = record.result or "SUBMISSION_ERROR"
            else:
                # A call the node refuses to even simulate or accept counts as refused.
                record.result = record.result or "SUBMISSION_ERROR"
        record.seconds = round(time.monotonic() - started, 1)
        return record

    def read(self, function: str, args: list) -> Any:
        for attempt in range(4):
            self._throttle()
            try:
                return self.client.read_contract(address=self.contract, function_name=function,
                                                 args=args)
            except Exception as error:  # noqa: BLE001
                if attempt == 3 or not self._transient(error):
                    raise
                time.sleep(5 * (attempt + 1))

    # ---- deployment (best effort convenience)
    def deploy(self, code: str, args: list) -> str:
        self._throttle()
        tx_hash = str(self.client.deploy_contract(code=code, args=args,
                                                  account=self.accounts["funder"]))
        receipt = self._wait(tx_hash)
        found = _find_address(receipt)
        if not found:
            raise ChainError(
                f"Deployment {tx_hash} finalized but the contract address could not be read "
                "from the receipt. Copy it from the Studionet explorer and pass --address."
            )
        return found


def _find_address(node: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    keys = ("contract_address", "contractAddress", "created_address", "recipient", "to_address")
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.startswith("0x") and len(value) == 42 \
                    and int(value, 16) != 0:
                return value
        for value in node.values():
            found = _find_address(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_address(value, depth + 1)
            if found:
                return found
    return ""


def git_output(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return ""


def popcount(value: int) -> int:
    return bin(int(value)).count("1")


# ------------------------------------------------------------------------------ scenario


class Scenario:
    def __init__(self, chain: Chain, report: Report, state: dict, state_path: Path | None):
        self.chain = chain
        self.report = report
        self.state = state
        self.state_path = state_path
        self.state.setdefault("done", [])
        self.state.setdefault("seqs", {})
        self.state.setdefault("deadlines", {})
        self.info: dict = {}
        self.ghost_wrong_hidden_title = ""

    # ---- helpers
    def save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))

    def done(self, key: str) -> bool:
        return key in self.state["done"]

    def mark(self, key: str) -> None:
        if key not in self.state["done"]:
            self.state["done"].append(key)
        self.save()

    def tx(self, name: str, function: str, args: list, *, value: int = 0, label: str = "",
           expect: str = "success", section: str = "") -> TxRecord:
        record = self.chain.write(name, function, args, value=value, label=label or function,
                                  expect=expect)
        self.report.txs.append(record)
        if expect == "success":
            log(f"tx {record.label}: {record.result or 'UNKNOWN'} [{record.note}] ({record.seconds}s)")
            if record.succeeded is False or record.result == "SUBMISSION_ERROR":
                self.report.check(section or "transactions", f"{record.label} executed", False,
                                  "FINISHED_WITH_RETURN", record.result or record.note)
        else:
            log(f"tx {record.label}: expected revert, saw {record.result or 'UNKNOWN'} [{record.note}]")
        return record

    def view(self, function: str, args: list | None = None):
        return self.chain.read(function, args or [])

    def addr(self, name: str) -> str:
        return self.chain.address(name)

    def seq(self, name: str) -> int:
        return int(self.state["seqs"][name])

    def claim(self, name: str) -> dict:
        return self.view("get_claim", [self.seq(name)])

    def challenge_info(self, name: str) -> dict:
        return self.view("get_challenge", [self.seq(name)])

    def credit(self, name: str) -> int:
        return int(self.view("get_available_credit", [self.addr(name)]))

    @property
    def stake(self) -> int:
        return int(self.info["stake_wei"])

    @property
    def bond(self) -> int:
        return int(self.info["challenge_bond_wei"])

    def salt(self, name: str) -> str:
        return self.state["salts"][name]

    def commitment_for(self, name: str) -> str:
        item = CLAIMS[name]
        return make_commitment(self.addr(name), item["title"], item["abstract"], item["elements"],
                               self.salt(name))

    def elements_text(self, name: str) -> str:
        return "\n".join(CLAIMS[name]["elements"])

    def require_time_before(self, key: str, margin: int, doing: str) -> None:
        """Stop with a clear message instead of racing a deadline that is about to pass."""
        deadline = int(self.state["deadlines"].get(key, 0))
        if deadline and self.chain.now() > deadline - margin:
            raise ChainError(
                f"The {key} deadline of this run is too close or already past while {doing}. "
                "Do not --resume an old run: start a fresh one (omit --resume)."
            )

    def fingerprint(self, watch: list[str]) -> str:
        """Everything a refused call must leave exactly as it was."""
        snapshot: dict = {
            "accounting": self.view("get_accounting"),
            "claims": self.view("get_claim_count"),
        }
        for name in watch:
            claim = self.claim(name)
            snapshot[name] = [claim["status"], claim["stake_wei"], claim["challenges_started"],
                              claim["outcome"], claim["title"]]
        return json.dumps(snapshot, sort_keys=True, default=str)

    def revert_check(self, section: str, name: str, who: str, function: str, args: list,
                     watch: list[str], value: int = 0) -> None:
        """A call that must not change anything.

        Refused means: the receipt reports an error (or the node refused the call), or the
        receipt is ambiguous and the state is proven unchanged. A receipt that reports success
        is never accepted as a refusal.
        """
        before = self.fingerprint(watch)
        record = self.tx(who, function, args, value=value, label=name, expect="revert",
                         section=section)
        after = self.fingerprint(watch)
        unchanged = before == after
        by_receipt = record.succeeded is False or record.result == "SUBMISSION_ERROR"
        ambiguous = record.succeeded is None
        refused = (by_receipt or ambiguous) and unchanged
        how = ("receipt reports an error" if by_receipt
               else "receipt ambiguous, state unchanged" if ambiguous and unchanged
               else f"receipt says {record.result or 'unknown'}")
        self.report.check(section, f"{name}: refused", refused,
                          "error in receipt (or refused at submission) and no state change",
                          how if refused else f"{how}; state {'unchanged' if unchanged else 'CHANGED'}")
        self.report.check(section, f"{name}: state unchanged", unchanged, "unchanged",
                          "unchanged" if unchanged else "CHANGED")

    # ---- phases
    def funding_needs(self) -> dict:
        needs = {"funder": 10**14, "stranger": 10**14}
        for name in REGISTRATION_ORDER:
            extra = CHALLENGES_BY_ADA * self.bond if name == "ada" else 0
            needs[name] = self.stake + extra + 10**14
        return needs

    def print_funding_plan(self) -> None:
        log("test accounts (kept in .live/accounts.json, which is git-ignored):")
        for name, wei in self.funding_needs().items():
            log(f"  {name:9s} {self.chain.address(name)}  needs {wei / GEN:.5f} GEN")

    def baseline(self) -> dict:
        """Contract-wide accounting before this run touched anything (others may share it)."""
        if "baseline" not in self.state:
            accounting = self.view("get_accounting")
            self.state["baseline"] = {k: int(accounting[k]) for k in (
                "total_credits", "total_locked_stakes", "total_locked_bonds", "total_commons",
                "accounted_liabilities")}
            self.state["baseline_claims"] = int(self.view("get_claim_count"))
            self.save()
        return self.state["baseline"]

    def delta(self, accounting: dict, key: str) -> int:
        return int(accounting[key]) - int(self.state.get("baseline", {}).get(key, 0))

    def phase_preflight(self) -> None:
        section = "0. Preflight"
        info = self.view("get_info")
        self.info = {k: info[k] for k in info}
        self.report.check(section, "contract answers get_info as Palimpsest",
                          info.get("name") == "Palimpsest", "Palimpsest", info.get("name"))
        self.report.environment["contract_version"] = info.get("version")
        self.report.environment["parameters"] = json.dumps({
            k: info[k] for k in ("stake_wei", "challenge_bond_wei", "reveal_window_seconds",
                                 "challenge_window_seconds", "judge_window_seconds",
                                 "overlap_percent")})
        problems = []
        if int(info["reveal_window_seconds"]) < MIN_REVEAL:
            problems.append(f"reveal window must be >= {MIN_REVEAL}s")
        if int(info["challenge_window_seconds"]) < MIN_CHALLENGE:
            problems.append(f"challenge window must be >= {MIN_CHALLENGE}s")
        if int(info["judge_window_seconds"]) < MIN_JUDGE:
            problems.append(f"judging window must be >= {MIN_JUDGE}s")
        if not 50 <= int(info["overlap_percent"]) <= 80:
            problems.append("overlap percentage must be between 50 and 80 for the scripted story")
        self.report.check(section, "deployed windows are long enough for the scripted story",
                          not problems, "no problems", "; ".join(problems) or "none")
        if problems:
            raise ChainError(
                "This deployment cannot run the scripted story: " + "; ".join(problems)
                + ". Deploy with: " + json.dumps(DEPLOY_ARGS) + " (or use --deploy)."
            )

    def phase_funding(self) -> None:
        section = "1. Funding"
        self.baseline()
        self.print_funding_plan()
        needs = self.funding_needs()
        for name, wei in needs.items():
            if self.chain.balance(name) >= wei:
                continue
            self.chain.fund(name, wei * 2)
        shortfalls = {n: w for n, w in needs.items() if self.chain.balance(n) < w}
        for name, wei in needs.items():
            have = self.chain.balance(name)
            self.report.check(section, f"{name} holds enough GEN ({wei / GEN:.5f})",
                              have >= wei, f">= {wei}", have)
        if shortfalls:
            lines = "\n".join(f"  {self.chain.address(n)}  needs {w / GEN:.5f} GEN"
                              for n, w in shortfalls.items())
            raise ChainError(
                "Automatic funding is not available on this network or was refused. Send GEN to:\n"
                f"{lines}\nthen re-run with --resume {self.state['run_id']}"
            )

    def register(self, name: str) -> None:
        section = "2. Registration"
        if self.done(f"registered:{name}"):
            return
        stake = self.stake + (CHALLENGES_BY_ADA * self.bond if name == "ada" else 0)
        self.tx(name, "deposit_credit", [], value=stake, label=f"{name} deposits funds",
                section=section)
        commitment = self.commitment_for(name)
        self.tx(name, "commit_claim", [commitment], label=f"{name} seals a claim", section=section)
        seq = int(self.view("find_claim", [self.addr(name), commitment]))
        self.state["seqs"][name] = seq
        claim = self.view("get_claim", [seq])
        self.report.check(section, f"{name}: sealed claim is on the registry", claim["status"] == "committed",
                          "committed", claim["status"])
        self.report.check(section, f"{name}: stake is locked", claim["stake_wei"] == self.stake,
                          self.stake, claim["stake_wei"])
        self.report.check(section, f"{name}: author is the sender",
                          claim["author"].lower() == self.addr(name).lower(), self.addr(name),
                          claim["author"])
        self.report.check(section, f"{name}: text stays hidden until reveal",
                          claim["title"] == "" and claim["elements"] == [], "empty",
                          claim["title"] or claim["elements"])
        self.state["deadlines"][f"reveal_by:{name}"] = int(claim["reveal_by"])
        self.mark(f"registered:{name}")

    def reveal(self, name: str) -> None:
        section = "2. Registration"
        if self.done(f"revealed:{name}"):
            return
        item = CLAIMS[name]
        self.require_time_before(f"reveal_by:{name}", 90, f"revealing {name}")
        self.tx(name, "reveal_claim", [self.seq(name), item["title"], item["abstract"],
                                       self.elements_text(name), self.salt(name)],
                label=f"{name} reveals", section=section)
        claim = self.claim(name)
        self.report.check(section, f"{name}: revealed", claim["status"] == "revealed", "revealed",
                          claim["status"])
        self.report.check(section, f"{name}: stored text equals the sealed text",
                          claim["title"] == clean(item["title"])
                          and claim["abstract"] == clean(item["abstract"])
                          and claim["elements"] == [clean(e) for e in item["elements"]],
                          "identical", "identical" if claim["title"] == clean(item["title"]) else "DIFFERENT")
        self.report.check(section, f"{name}: challenge window opened for the configured length",
                          claim["challenge_until"] - claim["revealed_at"]
                          == int(self.info["challenge_window_seconds"]),
                          int(self.info["challenge_window_seconds"]),
                          claim["challenge_until"] - claim["revealed_at"])
        self.state["deadlines"][f"challenge_until:{name}"] = int(claim["challenge_until"])
        self.mark(f"revealed:{name}")

    def phase_register(self) -> None:
        for name in REGISTRATION_ORDER:
            self.register(name)
            if name != "ghost":
                self.reveal(name)
        section = "2. Registration"
        seqs = [self.seq(n) for n in REGISTRATION_ORDER]
        self.report.check(section, "priority numbers follow registration order",
                          seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
                          "strictly increasing", seqs)
        accounting = self.view("get_accounting")
        expected = len(REGISTRATION_ORDER) * self.stake
        self.report.check(section, "locked stakes grew by exactly one stake per claim",
                          self.delta(accounting, "total_locked_stakes") == expected, expected,
                          self.delta(accounting, "total_locked_stakes"))

    def phase_open_challenges(self) -> None:
        section = "3. Challenges"
        ada = self.seq("ada")
        for name in JUNIORS:
            if self.done(f"challenged:{name}"):
                continue
            self.require_time_before(f"challenge_until:{name}", 150, f"challenging {name}")
            self.tx("ada", "challenge_claim", [self.seq(name), ada], label=f"ada challenges {name}",
                    section=section)
            claim = self.claim(name)
            record = self.challenge_info(name)
            self.report.check(section, f"{name}: claim is under challenge",
                              claim["status"] == "challenged", "challenged", claim["status"])
            self.report.check(section, f"{name}: challenger is ada and the bond is locked",
                              record["challenger"].lower() == self.addr("ada").lower()
                              and record["bond_wei"] == self.bond,
                              f"ada / {self.bond}", f"{record['challenger'][:10]} / {record['bond_wei']}")
            self.report.check(section, f"{name}: senior claim is ada's",
                              record["senior_seq"] == ada, ada, record["senior_seq"])
            self.mark(f"challenged:{name}")
        accounting = self.view("get_accounting")
        self.report.check(section, "three bonds are locked",
                          self.delta(accounting, "total_locked_bonds") == CHALLENGES_BY_ADA * self.bond,
                          CHALLENGES_BY_ADA * self.bond, self.delta(accounting, "total_locked_bonds"))

    def phase_hostile_calls(self) -> None:
        """Eighteen calls that must all be refused, with the books proven untouched."""
        s = "4. Hostile calls"
        if self.done("hostile"):
            return
        ghost, ada, bo, flo = (self.seq(n) for n in ("ghost", "ada", "bo", "flo"))
        gi = CLAIMS["ghost"]
        rc = self.revert_check
        # Time-sensitive first: the unrevealed claim's window must still be open.
        self.require_time_before("reveal_by:ghost", 240, "testing the early forfeit")
        rc(s, "forfeiting a claim whose reveal window is still open", "stranger",
           "forfeit_unrevealed", [ghost], ["ghost"])
        # Reveal abuse, against the claim that stays sealed.
        rc(s, "reveal with a wrong salt", "ghost", "reveal_claim",
           [ghost, gi["title"], gi["abstract"], self.elements_text("ghost"),
            "wrong-salt-wrong-salt-1234"], ["ghost"])
        rc(s, "reveal with a changed title", "ghost", "reveal_claim",
           [ghost, gi["title"] + " v2", gi["abstract"], self.elements_text("ghost"),
            self.salt("ghost")], ["ghost"])
        rc(s, "reveal with a hidden zero-width character", "ghost", "reveal_claim",
           [ghost, gi["title"] + "\u200b", gi["abstract"], self.elements_text("ghost"),
            self.salt("ghost")], ["ghost"])
        rc(s, "reveal by someone who is not the author", "stranger", "reveal_claim",
           [ghost, gi["title"], gi["abstract"], self.elements_text("ghost"), self.salt("ghost")],
           ["ghost"])
        rc(s, "reveal of an already revealed claim", "ada", "reveal_claim",
           [ada, CLAIMS["ada"]["title"], CLAIMS["ada"]["abstract"], self.elements_text("ada"),
            self.salt("ada")], ["ada"])
        # Commit abuse.
        rc(s, "the same author seals the same commitment twice", "ghost", "commit_claim",
           [self.commitment_for("ghost")], ["ghost"])
        rc(s, "sealing without credit for the stake", "stranger", "commit_claim",
           ["0x" + "ab" * 32], [])
        # Time abuse.
        rc(s, "forfeiting a claim that was revealed", "stranger", "forfeit_unrevealed", [flo],
           ["flo"])
        rc(s, "settling before the challenge window closes", "stranger", "settle_claim", [flo],
           ["flo"])
        # Challenge abuse.
        rc(s, "a stranger challenges in someone else's name", "stranger", "challenge_claim",
           [flo, ada], ["flo", "ada"])
        rc(s, "a later claim challenges an earlier one", "bo", "challenge_claim", [ada, bo],
           ["ada", "bo"])
        rc(s, "a claim that is still sealed tries to challenge", "ghost", "challenge_claim",
           [flo, ghost], ["flo", "ghost"])
        rc(s, "challenging a claim that is already under challenge", "ada", "challenge_claim",
           [bo, ada], ["bo"])
        # Judging abuse.
        rc(s, "a stranger asks for judging", "stranger", "judge_challenge", [bo], ["bo"])
        rc(s, "judging a claim that has no challenge", "ada", "judge_challenge", [flo], ["flo"])
        rc(s, "voiding a challenge that is still being judged", "stranger", "expire_challenge",
           [bo], ["bo"])
        rc(s, "withdrawing with no credit", "stranger", "withdraw_credit", [], [])
        self.mark("hostile")

    def judge_until_decided(self, name: str) -> dict:
        section = "5. Validator judgment"
        seq = self.seq(name)
        for attempt in range(1, 4):
            if self.claim(name)["status"] != "challenged":
                break
            self.tx("ada", "judge_challenge", [seq], label=f"judge {name} (try {attempt})",
                    section=section)
            if self.claim(name)["status"] != "challenged":
                break
            record = self.challenge_info(name)
            self.chain.sleep_until(int(record["retry_at"]) + 15, f"the judging cooldown for {name}")
        return self.challenge_info(name)

    def phase_judgment(self) -> None:
        section = "5. Validator judgment"
        findings = self.report.sections.setdefault(section, {"findings": {}})["findings"]
        for name in JUNIORS:
            if self.done(f"judged:{name}"):
                continue
            record = self.judge_until_decided(name)
            claim = self.claim(name)
            findings[name] = dict(record, status=claim["status"], outcome=claim["outcome"])
            self.mark(f"judged:{name}")
        need = int(self.challenge_info("bo")["needed_disclosures"])

        bo, cy, di = (self.claim(n) for n in JUNIORS)
        rbo, rcy, rdi = (self.challenge_info(n) for n in JUNIORS)
        self.report.check(section, "bo (plain paraphrase) was revoked as derivative",
                          bo["status"] == "revoked" and bo["outcome"] == "revoked_as_derivative",
                          "revoked / revoked_as_derivative", f"{bo['status']} / {bo['outcome']}")
        self.report.check(section, "bo's verdict rests on element overlap with enough disclosures",
                          rbo["basis"] == "overlap" and popcount(rbo["disclosed_mask"]) >= need,
                          f"overlap with >= {need} disclosed",
                          f"{rbo['basis'] or '-'} with {popcount(rbo['disclosed_mask'])} disclosed")
        self.report.check(section, "cy (different invention) kept its claim",
                          cy["status"] == "revealed" and cy["outcome"] == "challenge_rejected",
                          "revealed / challenge_rejected", f"{cy['status']} / {cy['outcome']}")
        self.report.check(section, "cy's verdict rests on absence of the senior elements",
                          rcy["basis"] == "absence", "absence", rcy["basis"] or "-")
        self.report.check(section, "di (copy that instructs the reviewer) was revoked",
                          di["status"] == "revoked", "revoked", di["status"])
        self.report.check(section, "di's verdict rests on overlap or on the instruction attempt",
                          rdi["basis"] in ("overlap", "junior_manipulation"),
                          "overlap or junior_manipulation", rdi["basis"] or "-")
        revoked_by = [c["revoked_by"] for c in (bo, cy, di) if c["status"] == "revoked"]
        self.report.check(section, "every revoked claim names ada's claim as its senior",
                          bool(revoked_by) and all(v == self.seq("ada") for v in revoked_by),
                          self.seq("ada"), revoked_by or "no claim was revoked")
        self.report.check(section, "decisive verdicts used one validator assessment each",
                          all(r["assessments"] >= 1 and not r["pending"] for r in (rbo, rcy, rdi)),
                          "decided", [r["pending"] for r in (rbo, rcy, rdi)])
        for name, record in zip(JUNIORS, (rbo, rcy, rdi)):
            log(f"finding {name}: {record['pair_result']} basis={record['basis'] or '-'} "
                f"flags={record['instruction_flags']} confidence={record['confidence']} "
                f"disclosed={record['disclosed_mask']:#b} unclear={record['unclear_mask']:#b} "
                f"- {record['rationale']}")

    def phase_after_judgment_hostile(self) -> None:
        """Replays against decided challenges. Which claims were revoked depends on what the
        model decided, so each call is aimed at claims whose observed state makes it invalid."""
        s = "6. Hostile calls after judgment"
        if self.done("hostile2"):
            return
        rc = self.revert_check
        statuses = {n: self.claim(n)["status"] for n in JUNIORS}
        decided = [n for n in JUNIORS if statuses[n] != "challenged"]
        revoked = [n for n in JUNIORS if statuses[n] == "revoked"]
        if decided:
            rc(s, f"a decided challenge on {decided[0]} cannot be judged again", "ada",
               "judge_challenge", [self.seq(decided[0])], [decided[0]])
            rc(s, f"the pair ada/{decided[-1]} cannot be challenged again", "ada", "challenge_claim",
               [self.seq(decided[-1]), self.seq("ada")], [decided[-1]])
        if revoked:
            rc(s, f"revoked claim {revoked[0]} cannot be settled", "stranger", "settle_claim",
               [self.seq(revoked[0])], [revoked[0]])
            rc(s, f"revoked claim {revoked[-1]} cannot be challenged again", "ada", "challenge_claim",
               [self.seq(revoked[-1]), self.seq("ada")], [revoked[-1]])
        self.report.check(s, "replay checks were aimed at decided and revoked claims",
                          bool(decided), "at least one decided challenge",
                          f"decided={decided}, revoked={revoked}")
        self.mark("hostile2")

    def phase_time(self) -> None:
        section = "7. Windows expire"
        ghost = self.seq("ghost")
        names = ["ada", "bo", "cy", "di", "flo"]
        # A challenge nobody could decide (not expected) is voided once its judging window ends.
        for name in names:
            if self.claim(name)["status"] == "challenged" and not self.done(f"expired:{name}"):
                judge_by = int(self.challenge_info(name)["judge_by"])
                self.chain.sleep_until(judge_by + 20, f"the judging window of {name} to close")
                self.tx("stranger", "expire_challenge", [self.seq(name)],
                        label=f"stranger voids the undecided challenge on {name}", section=section)
                self.mark(f"expired:{name}")
        to_settle = [n for n in names if self.claim(n)["status"] == "revealed"]
        ghost_by = int(self.state["deadlines"]["reveal_by:ghost"])
        latest = max([int(self.claim(n)["challenge_until"]) for n in to_settle] + [ghost_by])
        self.chain.sleep_until(latest + 20, "every reveal and challenge window to close")
        if not self.done("forfeited"):
            self.tx("stranger", "forfeit_unrevealed", [ghost],
                    label="stranger burns the unrevealed stake", section=section)
            claim = self.view("get_claim", [ghost])
            self.report.check(section, "ghost: stake burned", claim["status"] == "forfeited"
                              and claim["stake_wei"] == 0, "forfeited / 0",
                              f"{claim['status']} / {claim['stake_wei']}")
            self.mark("forfeited")
        for name in to_settle:
            if self.done(f"settled:{name}"):
                continue
            self.tx("stranger", "settle_claim", [self.seq(name)], label=f"stranger settles {name}",
                    section=section)
            claim = self.claim(name)
            self.report.check(section, f"{name}: settled and stake released",
                              claim["status"] == "settled" and claim["stake_wei"] == 0,
                              "settled / 0", f"{claim['status']} / {claim['stake_wei']}")
            self.mark(f"settled:{name}")
        self.revert_check(section, "settling a claim twice", "stranger", "settle_claim",
                          [self.seq("flo")], ["flo"])
        self.revert_check(section, "forfeiting a claim twice", "stranger", "forfeit_unrevealed",
                          [ghost], ["ghost"])
        self.revert_check(section, "revealing after the deadline", "ghost", "reveal_claim",
                          [ghost, CLAIMS["ghost"]["title"], CLAIMS["ghost"]["abstract"],
                           self.elements_text("ghost"), self.salt("ghost")], ["ghost"])
        accounting = self.view("get_accounting")
        self.report.check(section, "exactly one burned stake joined the commons",
                          self.delta(accounting, "total_commons") == self.stake, self.stake,
                          self.delta(accounting, "total_commons"))
        self.report.check(section, "nothing of this run is still locked",
                          self.delta(accounting, "total_locked_stakes") == 0
                          and self.delta(accounting, "total_locked_bonds") == 0, "0 / 0",
                          f"{self.delta(accounting, 'total_locked_stakes')} / "
                          f"{self.delta(accounting, 'total_locked_bonds')}")

    def expected_credits(self) -> dict:
        """What the rules say each account should now hold, derived from the observed verdicts."""
        expected = {name: 0 for name in REGISTRATION_ORDER}
        for name in REGISTRATION_ORDER:
            if self.claim(name)["status"] == "settled":
                expected[name] += self.stake
        for name in JUNIORS:
            result = self.challenge_info(name)["pair_result"]
            if result == "upheld":
                expected["ada"] += self.stake + self.bond
            elif result == "rejected":
                expected[name] += self.bond
            elif result == "void":
                expected["ada"] += self.bond
        return expected

    def phase_withdraw(self) -> None:
        section = "8. Native GEN withdrawals"
        expected = self.expected_credits()
        self.report.sections[section] = {"expected_credits": expected}
        for name, wei in expected.items():
            self.report.check(section, f"{name}: ledger credit equals the rule-derived amount",
                              self.credit(name) == wei, wei, self.credit(name))
        for name, wei in expected.items():
            if wei == 0 or self.done(f"withdrawn:{name}"):
                continue
            before = self.chain.balance(name)
            self.tx(name, "withdraw_credit", [], label=f"{name} withdraws credit", section=section)
            after = self.chain.wait_for_balance(name, before + wei, 240)
            self.report.check(section, f"{name} received {wei} wei", after - before == wei, wei,
                              after - before)
            self.report.check(section, f"{name}: ledger credit cleared", self.credit(name) == 0, 0,
                              self.credit(name))
            self.mark(f"withdrawn:{name}")
        empty = [name for name, wei in expected.items() if wei == 0]
        for name in empty[:2]:
            self.revert_check(section, f"{name} has nothing to withdraw", name, "withdraw_credit",
                              [], [])
        accounting = self.view("get_accounting")
        self.report.check(section, "no credit of this run is left unclaimed",
                          self.delta(accounting, "total_credits") == 0, 0,
                          self.delta(accounting, "total_credits"))
        self.report.check(section, "the only liability left from this run is the burned stake",
                          self.delta(accounting, "accounted_liabilities") == self.stake, self.stake,
                          self.delta(accounting, "accounted_liabilities"))
        self.report.sections[section]["accounting"] = accounting

    def collect_registry(self) -> None:
        rows = {}
        for name in REGISTRATION_ORDER:
            claim = self.claim(name)
            rows[name] = {"seq": claim["seq"], "status": claim["status"], "outcome": claim["outcome"],
                          "title": claim["title"] or "(never revealed)"}
        self.report.sections["registry"] = rows

    def run(self) -> None:
        for phase in (self.phase_preflight, self.phase_funding, self.phase_register,
                      self.phase_open_challenges, self.phase_hostile_calls, self.phase_judgment,
                      self.phase_after_judgment_hostile, self.phase_time, self.phase_withdraw):
            phase()
        self.collect_registry()


# -------------------------------------------------------------------------------- report


def write_report(report: Report, directory: Path, contract: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = directory / f"live-e2e-{stamp}.json"
    md_path = directory / f"live-e2e-{stamp}.md"
    passed = sum(1 for c in report.checks if c.passed)
    failed = len(report.checks) - passed
    payload = {
        "started": report.started, "finished": report.finished, "contract": contract,
        "environment": report.environment, "passed": passed, "failed": failed,
        "fatal": report.fatal, "checks": [c.__dict__ for c in report.checks],
        "transactions": [dict(t.__dict__, link=t.link) for t in report.txs],
        "sections": report.sections,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    verdict = "PASS" if report.all_passed else "FAIL"
    lines = [
        f"# Palimpsest live end-to-end report - {verdict}", "",
        "- Network: GenLayer Studionet",
        f"- Contract: [{contract}]({EXPLORER_ADDRESS}{contract})",
        f"- Started / finished (UTC): {report.started} / {report.finished}",
        f"- Checks: **{passed} passed, {failed} failed**",
    ]
    for key, value in report.environment.items():
        lines.append(f"- {key}: `{value}`")
    if report.fatal:
        lines += ["", "## Run aborted", "", "```", report.fatal, "```"]
    lines += ["", "## Checks", "", "| Result | Section | Check | Expected | Observed |",
              "| --- | --- | --- | --- | --- |"]
    for check in report.checks:
        mark = "PASS" if check.passed else "**FAIL**"
        row = [mark, check.section, check.name, check.expected, check.observed]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |")
    lines += ["", "## Transactions", "", "| # | Step | Account | Function | Result | Status | Seconds | Tx |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for number, tx in enumerate(report.txs, 1):
        link = f"[{tx.tx_hash[:12]}...]({tx.link})" if tx.tx_hash else (tx.note or "-")
        expected = " (expected revert)" if tx.expected == "revert" else ""
        lines.append(f"| {number} | {tx.label}{expected} | `{tx.account[:10]}...` | `{tx.function}` | "
                     f"{tx.result or '-'} | {tx.status or '-'} | {tx.seconds} | {link} |")
    registry = report.sections.get("registry")
    if registry:
        lines += ["", "## Final registry", "", "| Author | Priority | Status | Outcome | Title |",
                  "| --- | --- | --- | --- | --- |"]
        for name, row in registry.items():
            lines.append(f"| {name} | {row['seq']} | {row['status']} | {row['outcome'] or '-'} | "
                         f"{str(row['title']).replace('|', '/')} |")
    findings = report.sections.get("5. Validator judgment", {}).get("findings", {})
    if findings:
        lines += ["", "## Validator findings", "",
                  "| Claim | Verdict | Basis | Instruction flags (senior/junior) | Confidence | "
                  "Disclosed | Unclear | Rationale |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for name, row in findings.items():
            lines.append(
                f"| {name} | {row['pair_result']} | {row['basis'] or '-'} | {row['instruction_flags'] or '-'} | "
                f"{row['confidence']} | {int(row['disclosed_mask']):#06b} | {int(row['unclear_mask']):#06b} | "
                f"{str(row['rationale']).replace('|', '/')} |")
    lines += ["", "## Notes", "",
              "- Pass/fail is decided from state read back from the contract, not from receipts alone.",
              "- A step marked *expected revert* passes only if the receipt reports an error or the "
              "receipt is ambiguous and the contract state is proven unchanged.",
              "- Verdicts come from real validators and a real LLM; a failed semantic check can mean "
              "the model disagreed with the scripted expectation, so read the rationale before "
              "suspecting the contract. The ledger checks derive their expectations from the verdicts "
              "actually observed, so they stay valid either way."]
    md_path.write_text("\n".join(lines) + "\n")
    return md_path, json_path


# ---------------------------------------------------------------------------------- main


def environment_info(chain_name: str) -> dict:
    info = {"python": platform.python_version(), "platform": platform.platform(),
            "network": chain_name, "git_commit": git_output("rev-parse", "HEAD") or "unknown",
            "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()}
    for package in ("genlayer-py", "genlayer-test"):
        try:
            from importlib.metadata import version
            info[package] = version(package)
        except Exception:  # noqa: BLE001
            info[package] = "not installed"
    return info


def load_state(resume: str | None) -> tuple[dict, Path]:
    if resume:
        candidate = STATE_DIR / f"run-{resume}.json"
        if not candidate.exists():
            raise ChainError(f"No saved run {resume} in {STATE_DIR}")
        return json.loads(candidate.read_text()), candidate
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    state = {"run_id": run_id, "salts": {n: new_salt() for n in REGISTRATION_ORDER}, "keys": {}}
    keys_file = STATE_DIR / "accounts.json"
    if keys_file.exists():
        state["keys"] = json.loads(keys_file.read_text())
    return state, STATE_DIR / f"run-{run_id}.json"


def main(argv: list[str] | None = None, chain_factory: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--address", default=os.environ.get("PALIMPSEST_ADDRESS"),
                        help="deployed Palimpsest contract (or env PALIMPSEST_ADDRESS)")
    parser.add_argument("--deploy", action="store_true",
                        help="deploy contracts/palimpsest.py with the tested parameters first")
    parser.add_argument("--print-deploy-args", action="store_true",
                        help="print the constructor arguments this run is designed around, then stop")
    parser.add_argument("--wait-seconds", type=int, default=900, help="per-transaction wait budget")
    parser.add_argument("--rpc-url", default=os.environ.get("GENLAYER_RPC_URL"))
    parser.add_argument("--resume", help="run id from a previous (interrupted) run")
    parser.add_argument("--check-only", action="store_true",
                        help="verify tooling, RPC reachability and the contract, then stop")
    args = parser.parse_args(argv)

    if args.print_deploy_args:
        print(json.dumps(DEPLOY_ARGS, indent=2))
        print("In order: " + ", ".join(str(v) for v in DEPLOY_ARGS.values()))
        return 0

    report = Report()
    report.environment = environment_info("studionet")
    contract = args.address or ""
    exit_code = 1
    state: dict = {}
    try:
        state, state_path = load_state(args.resume)
        if not contract and not args.deploy and not chain_factory:
            raise ChainError("Pass --address of your deployed Palimpsest contract, or use --deploy.")
        chain = (chain_factory(state) if chain_factory
                 else GenLayerChain(contract or "0x" + "0" * 40, state, args.rpc_url, args.wait_seconds))
        if args.deploy and not chain_factory:
            log("deploying contracts/palimpsest.py with " + json.dumps(DEPLOY_ARGS))
            contract = chain.deploy(CONTRACT_PATH.read_text(), list(DEPLOY_ARGS.values()))
            chain.contract = contract
            log(f"deployed at {contract}")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "accounts.json").write_text(json.dumps(state["keys"], indent=2))
        scenario = Scenario(chain, report, state, state_path)
        if args.check_only:
            scenario.phase_preflight()
            scenario.print_funding_plan()
            log("check-only run finished: tooling and contract are reachable")
            return 0 if report.all_passed else 1
        scenario.run()
        exit_code = 0 if report.all_passed else 1
    except ChainError as error:
        report.fatal = str(error)
        exit_code = 2
    except KeyboardInterrupt:
        report.fatal = "Interrupted. Resume with --resume " + str(state.get("run_id", ""))
        exit_code = 130
    except Exception:  # noqa: BLE001
        report.fatal = traceback.format_exc()
        exit_code = 2
    finally:
        report.finished = utc_now()
        if report.checks or report.fatal:
            md_path, json_path = write_report(report, REPORT_DIR, contract or "(none)")
            log(f"report written: {os.path.relpath(md_path)} and {json_path.name}")
        if report.fatal:
            log("ABORTED: " + report.fatal.strip().splitlines()[-1])
        passed = sum(1 for c in report.checks if c.passed)
        log(f"{passed}/{len(report.checks)} checks passed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
