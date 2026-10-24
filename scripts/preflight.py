"""Release gate: source rules, lint, ABI schema, and every offline test."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
CONTRACT = CONTRACTS / "palimpsest.py"
SCHEMA = ROOT / "artifacts" / "palimpsest.abi.json"
GENVM_VERSION = "v0.2.16"
WRITE_SCHEMA = "--write-schema" in sys.argv
SKIP_TESTS = "--skip-tests" in sys.argv

SOURCES = sorted(CONTRACTS.glob("*.py"))
if SOURCES != [CONTRACT]:
    raise SystemExit(f"release gate requires only contracts/palimpsest.py; found {[p.name for p in SOURCES]}")

raw = CONTRACT.read_bytes()
source = raw.decode("utf-8")
lines = source.splitlines()
version_match = re.search(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"\s*$', source, re.MULTILINE)
header = re.fullmatch(r"# v(\d+\.\d+\.\d+)", lines[0]) if lines else None
if not version_match or not header or header.group(1) != version_match.group(1):
    raise SystemExit("source header and VERSION must match")
if len(lines) < 2 or not re.fullmatch(r'# \{ "Depends": "py-genlayer:[a-z0-9]+" \}', lines[1]):
    raise SystemExit("second line must pin the py-genlayer dependency")
for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
    if "==" not in line or "rc" in line.lower() or "dev" in line.lower():
        raise SystemExit(f"release dependency must be pinned to a stable version: {line}")

ast.parse(source, filename=str(CONTRACT))
subprocess.run([sys.executable, "-m", "compileall", "-q", "contracts", "tests", "scripts"],
               cwd=ROOT, check=True)

lint = shutil.which("genvm-lint") or shutil.which("genvm-lint.exe")
if not lint:
    candidate = Path(sys.executable).with_name("genvm-lint.exe" if os.name == "nt" else "genvm-lint")
    if candidate.exists():
        lint = str(candidate)
if not lint:
    raise SystemExit("genvm-lint is required; install requirements.txt")

env = os.environ.copy()
env["GENVM_VERSION"] = GENVM_VERSION
subprocess.run([lint, "check", str(CONTRACT), "--json"], cwd=ROOT, check=True, env=env)

with tempfile.TemporaryDirectory() as temp_dir:
    generated = Path(temp_dir) / "palimpsest.abi.json"
    subprocess.run([lint, "schema", str(CONTRACT), "--output", str(generated)],
                   cwd=ROOT, check=True, env=env)
    abi = json.loads(generated.read_text())
    payable = sorted(name for name, method in abi["methods"].items() if method.get("payable"))
    if payable != ["deposit_credit"]:
        raise SystemExit(f"only deposit_credit may be payable; found {payable}")
    if len(abi["ctor"]["params"]) != 6:
        raise SystemExit("constructor must take the six documented parameters")
    if WRITE_SCHEMA:
        SCHEMA.parent.mkdir(exist_ok=True)
        SCHEMA.write_bytes(generated.read_bytes())
        print(f"wrote {SCHEMA.relative_to(ROOT)}")
    elif SCHEMA.exists() and generated.read_bytes() != SCHEMA.read_bytes():
        raise SystemExit("generated ABI differs from tracked artifacts/palimpsest.abi.json "
                         "(run: python scripts/preflight.py --write-schema)")
    schema_state = "matches tracked file" if SCHEMA.exists() else "generated OK (no tracked copy yet)"

passed = 0
if not SKIP_TESTS:
    test_env = os.environ.copy()
    test_env["GENVM_VERSION"] = GENVM_VERSION
    test = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"],
                          cwd=ROOT, capture_output=True, text=True, env=test_env)
    print(test.stdout, end="")
    print(test.stderr, end="", file=sys.stderr)
    if test.returncode:
        raise SystemExit(test.returncode)
    summary = test.stdout + "\n" + test.stderr
    match = re.search(r"(?:^|\s)(\d+) passed(?:,\s*(\d+) skipped)?(?:,\s*(\d+) failed)?\s+in\s", summary)
    if not match:
        raise SystemExit("pytest summary count could not be verified")
    passed, skipped, failed = (int(value or 0) for value in match.groups())
    if not passed or skipped or failed:
        raise SystemExit(f"release gate requires passing tests and zero skips: {passed=}, {skipped=}, {failed=}")

print(f"contract_version={version_match.group(1)}")
print(f"contract_sha256={hashlib.sha256(raw).hexdigest()}")
print(f"contract_source_bytes={len(raw)}")
print(f"offline_tests={passed if not SKIP_TESTS else 'skipped'}")
print("deployable_contract_sources=1")
print("lint=PASS")
print(f"schema={schema_state}")
print("preflight=PASS")
