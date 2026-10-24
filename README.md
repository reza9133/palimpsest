# Palimpsest

**A priority registry for ideas on GenLayer, where validators chart one claim against another and deterministic code turns the chart into a verdict.**

Authors seal a claim (title, abstract and two to eight numbered core elements) as a SHA-256 commitment and lock a stake. Registration order is the priority order. After revealing, a claim sits in a challenge window during which only the author of an *earlier* claim may challenge it, by posting a bond. GenLayer validators then build a *claim chart*: for each core element of the senior claim, does the junior claim's own text disclose it?

| Verdict | When | What happens |
| --- | --- | --- |
| **upheld** | disclosed elements reach the overlap threshold | junior claim is revoked, its stake and the challenger's bond go to the challenger |
| **rejected** | disclosed + unclear elements cannot reach the threshold | challenge fails, challenger's bond goes to the accused author |
| **inconclusive** | anything in between, low confidence, or murky manipulation flags | nothing is decided; after three inconclusive assessments the challenge is voided and the bond refunded |

An unchallenged claim *settles*: it becomes a final priority record and its stake returns. A claim sealed but never revealed burns its stake into the contract (nobody, including the deployer, can withdraw it), which makes pre-committing guesses expensive.

> Palimpsest records what a bounded validator assessment found about the overlap between two sealed texts, in the order they were sealed. It does not certify novelty, truth, authorship or legal validity, and a revocation means "substantially overlaps an earlier-sealed claim", not "proven copying".

## Why it is built this way

* **The model never decides.** It only fills a chart (`disclosed` / `absent` / `unclear` per element, plus two manipulation flags and a confidence). A pure function turns the chart into upheld / rejected / inconclusive, so the rule is auditable and unit-tested.
* **Validators must reproduce the finding, not the prose.** A decisive verdict needs the same outcome and the same basis; overlap or absence verdicts must also agree within one element. Failures and inconclusive answers never authorise anything.
* **Instruction attempts lose.** If a claim tries to steer the automated reviewer, it loses, but only when the other side is clean. Overlap needs a clean senior text, absence needs a clean junior text.
* **Generic seniority claims cannot weaponise the registry.** The prompt tells the model to answer `unclear` for elements so generic that any text in the field would disclose them, which can only produce an inconclusive result.
* **Pull payments only.** Every payout is an internal credit; owners withdraw their own credit in one transfer. No settlement step emits a transfer, and one account's problem cannot block another's payout.
* **No admin.** There is no owner, pause switch or fee. Parameters are fixed at deployment.

## Repository layout

```
contracts/palimpsest.py      the single deployable contract
scripts/palimpsest_tools.py  client helpers: normalisation, commitments, local validation
scripts/live_e2e.py          live Studionet end-to-end run with a written report
scripts/live_claims.py       the texts used by the live run
scripts/preflight.py         release gate: source rules, lint, ABI, every offline test
run.sh                       one command for a GitHub Codespace
tests/direct/                Direct Mode tests (normal, validation, judging, validators, adversarial, fuzz)
tests/direct/test_live_script.py  runs the live script against a simulated chain
artifacts/palimpsest.abi.json     generated ABI, checked by the gate
docs/DESIGN.md, docs/DEPLOYMENT.md
```

## Quick start (GitHub Codespace)

```bash
bash run.sh --deploy                    # offline gate, deploy with tested parameters, live e2e, report
bash run.sh --address 0xYourContract    # test a contract you deployed yourself
bash run.sh --address 0x... --check-only
```

`run.sh` installs the pinned tooling, runs `python scripts/preflight.py` (lint, ABI check and every offline test), then `python scripts/live_e2e.py`, and prints the path of the report written to `reports/`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for details, including deploying by hand.

Constructor arguments the live run is designed around (`python scripts/live_e2e.py --print-deploy-args`):

| Argument | Value | Meaning |
| --- | --- | --- |
| `stake_wei` | 300000000000000 | stake locked per claim (0.0003 GEN) |
| `challenge_bond_wei` | 200000000000000 | bond per challenge (0.0002 GEN) |
| `reveal_window` | 2400 | seconds to reveal after sealing |
| `challenge_window` | 1500 | seconds after reveal during which a claim can be challenged |
| `judge_window` | 3600 | seconds to get a pending challenge decided |
| `overlap_percent` | 60 | share of senior elements that must be disclosed |

For production-style use choose days rather than minutes, and a stake and bond that make frivolous claims and challenges costly.

## Contract API

Write methods: `deposit_credit` (payable), `withdraw_credit`, `commit_claim`, `reveal_claim`, `forfeit_unrevealed`, `settle_claim`, `challenge_claim`, `judge_challenge`, `expire_challenge`.
Views: `get_info`, `get_accounting`, `get_available_credit`, `get_claim_count`, `find_claim`, `get_claim`, `get_challenge`.

Sealing a claim safely, locally:

```python
from scripts.palimpsest_tools import commitment, new_salt, validate_claim

salt = new_salt()                      # keep it secret until the reveal
assert validate_claim(title, abstract, elements) == []
sealed = commitment(my_address, title, abstract, elements, salt)
# deposit_credit(value >= stake), commit_claim(sealed), later reveal_claim(seq, title, abstract, "\n".join(elements), salt)
```

Never send the text or salt to anything before the reveal. Text is normalised (whitespace collapsed) before hashing; zero-width, bidirectional-override and control characters are refused.

## Tests

* **Normal paths** (`test_lifecycle.py`): register, reveal, settle, forfeit, upheld, rejected, inconclusive then void, expiry, chains of challenges, multiple seniors, withdrawals.
* **Validation** (`test_validation.py`): every constructor bound, every field limit at and past its boundary, hidden characters in every field, every refused caller and state, with the books proven unchanged after each revert.
* **Judging** (`test_judging.py`): the full decision table, the confidence boundary, the threshold for eleven element-count and percentage combinations, malformed model output, budgets, cooldown doubling, prompt hardening against forged delimiters.
* **Validators** (`test_validator.py`): what must match for consensus, and forged leader results that must never be confirmed.
* **Adversarial** (`test_adversarial.py`): commitment copying and front-running, guess hoarding, sybil self-dealing, griefing, last-second challenges, injection, oversized input, ledger abuse.
* **Properties** (`test_properties.py`): twelve seeded random runs of 140 calls each; after every call the ledger must balance, value must be conserved, and every failure must be an expected user error.
* **Live script** (`test_live_script.py`): the whole live scenario against a simulated chain, including a slower network, unexpected model verdicts, a garbled answer and voided challenges.

Run everything offline with `python scripts/preflight.py`. A mutation pass over forty deliberate bugs in the contract was caught by the suite in every case except one equivalent mutant (a redundant credit check).

## Live end-to-end run

Six authors on Studionet: one senior claim, a plain paraphrase of it, an unrelated invention, a copy that tries to instruct the reviewer, an unrelated claim nobody challenges, and one that is sealed and never revealed. The run checks priority order, hidden text, reveals, three real validator judgments, eighteen hostile calls, expiry, settlement, burning, rule-derived credit amounts and real native-GEN withdrawals. Reports go to `reports/live-e2e-<timestamp>.md` and `.json`. Expect about an hour.

A refused call counts as refused only if the receipt reports an error, or the receipt is ambiguous and the contract state is proven unchanged. A call the receipt reports as successful is never counted as refused. Real LLM output is probabilistic: a failed semantic check is a finding to read, not automatically a contract bug, and the ledger checks derive their expectations from the verdicts actually observed.

## Known limits

* Near-duplicate elements written by a senior can only be handled by the model (the prompt asks it to mark restated elements `unclear`); exact duplicates are rejected on-chain.
* Seniority is by sealing order, not by independent invention: a later claim that overlaps an earlier one can be revoked even if its author never saw it.
* Judging reads only on-chain text; there is no web evidence in this contract.
* Forfeited stakes are burned inside the contract permanently.
* Direct Mode tests run the contract in-process against the pinned SDK; the first live run is the first contact with real validators.

## License

MIT.
