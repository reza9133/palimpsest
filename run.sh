#!/usr/bin/env bash
# One command for a GitHub Codespace: install tooling, run the offline gate, then run the
# live end-to-end test on Studionet and write the report to ./reports/.
#
#   bash run.sh --deploy                    deploy with the tested parameters, then test it
#   bash run.sh --address 0xYourContract    test a contract you already deployed
#   bash run.sh --address 0x... --check-only   only verify tooling, RPC and the contract
#   bash run.sh --skip-offline ...          skip the offline gate (not recommended)
set -euo pipefail
cd "$(dirname "$0")"

export GENVM_VERSION="${GENVM_VERSION:-v0.2.16}"
SKIP_OFFLINE=0
PASSTHROUGH=()
for arg in "$@"; do
  if [ "$arg" = "--skip-offline" ]; then SKIP_OFFLINE=1; else PASSTHROUGH+=("$arg"); fi
done

PY="${PYTHON:-python3}"
if ! "$PY" -c "import genlayer_py, gltest" >/dev/null 2>&1; then
  echo ">> installing pinned GenLayer tooling"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
fi

if [ "$SKIP_OFFLINE" -eq 0 ]; then
  echo ">> offline release gate (lint, ABI, every adversarial and property test)"
  "$PY" scripts/preflight.py
fi

echo ">> live end-to-end run on Studionet"
set +e
"$PY" scripts/live_e2e.py "${PASSTHROUGH[@]}"
STATUS=$?
set -e

LATEST="$(ls -t reports/live-e2e-*.md 2>/dev/null | head -n 1 || true)"
if [ -n "$LATEST" ]; then
  echo ">> report: $LATEST"
fi
exit "$STATUS"
