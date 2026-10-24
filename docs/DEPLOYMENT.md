# Deployment and live verification (Studionet)

Palimpsest targets GenLayer **Studionet**. Direct Mode tests run the contract in-process against the pinned SDK; the live run is the first contact with real validators and a real LLM.

## 1. Codespace

Open the repository in a GitHub Codespace (the dev container installs Python 3.12 and `requirements.txt`), then:

```bash
python scripts/preflight.py
```

It checks source rules, runs `genvm-lint`, verifies the ABI (exactly one payable method, six constructor parameters), and runs every offline test. Do not deploy if it fails.

## 2. Deploy

Either let the runner deploy with the tested parameters:

```bash
python scripts/live_e2e.py --deploy
```

or deploy `contracts/palimpsest.py` yourself with the GenLayer CLI or Studio on Studionet, with these constructor arguments in order:

```
300000000000000, 200000000000000, 2400, 1500, 3600, 60
```

(stake_wei, challenge_bond_wei, reveal_window, challenge_window, judge_window, overlap_percent). Print them any time with `python scripts/live_e2e.py --print-deploy-args`.

The live run refuses a deployment whose windows are too short for the scripted story (reveal ≥ 1800 s, challenge ≥ 1200 s, judging ≥ 1800 s, overlap 50 to 80 percent) and tells you what to redeploy with.

## 3. Run

```bash
bash run.sh --deploy                       # everything in one command
bash run.sh --address 0xYourContract       # against your own deployment
python scripts/live_e2e.py --address 0x... --check-only   # tooling, RPC and contract check
```

The runner creates throw-away accounts (kept in `.live/accounts.json`, git-ignored, never commit it), asks Studionet to fund them with `sim_fundAccount`, and prints the addresses if automatic funding is refused so you can fund them by hand and re-run with `--resume <run id>`. Resume works only while the run's reveal and challenge windows are still open; after a long interruption start a fresh run.

Flags: `--wait-seconds` (per-transaction wait budget), `--rpc-url`, `--resume`, `--check-only`, `--deploy`, `--print-deploy-args`. Environment: `PALIMPSEST_ADDRESS`, `GENLAYER_RPC_URL`.

Studionet allows about 60 requests per minute per IP and each transaction needs roughly 40 seconds to finalize, so a full run takes about an hour.

## 4. Read the report

`reports/live-e2e-<timestamp>.md` (and `.json`) list every check, every transaction with an explorer link, the final registry and the validator findings (verdict, basis, flags, confidence, disclosed and unclear element masks, rationale).

Expected outcomes: `ada` settles; `bo` (paraphrase) is revoked on overlap; `cy` (different invention) keeps its claim after a rejected challenge and settles; `di` (copy that instructs the reviewer) is revoked on overlap or on the instruction attempt; `flo` settles unchallenged; `ghost` never reveals and its stake is burned. Credits are derived from the verdicts actually observed, then withdrawn as native GEN and compared to the wei.

A `FAIL` on a semantic check usually means the model disagreed with the scripted expectation: read the rationale in the findings table before suspecting the contract. Ledger and refusal checks do not depend on the model.

## Known unverified assumptions

* `sim_fundAccount` may not exist on your endpoint; fund the printed addresses manually if the runner says so.
* Receipt field names in `genlayer-py` are parsed defensively; an ambiguous receipt on a refused call is accepted only when the contract state is proven unchanged.
* The contract assumes transaction time is available through `datetime.now()` in consensus, as the pinned SDK provides.
