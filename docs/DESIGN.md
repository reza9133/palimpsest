# Design notes

## State machine

```
            commit_claim            reveal_claim                  settle_claim (window over)
 (nothing) ────────────▶ COMMITTED ─────────────▶ REVEALED ─────────────────────────────────▶ SETTLED
                            │  forfeit_unrevealed      │  ▲
                            ▼  (reveal deadline)       │  │ rejected / voided
                        FORFEITED          challenge_claim │
                                                   ▼  │
                                              CHALLENGED ── upheld ─▶ REVOKED
```

* `COMMITTED`: stake locked, text hidden. Reveal closes at `reveal_by` (exclusive).
* `REVEALED`: text public, challenge window open until `challenge_until` (exclusive for challenges, inclusive for settlement).
* `CHALLENGED`: exactly one challenge pending; a bond is locked; judging must happen before `ch_judge_by`.
* `SETTLED`, `REVOKED`, `FORFEITED`: terminal. Only `SETTLED`, `REVEALED` and `CHALLENGED` claims have standing as seniors.

A claim can be challenged at most three times, never twice by the same senior, and never after its challenge window closes. A pending challenge delays settlement by at most one judging window.

## Ledger

Four buckets sum to what the contract owes: `credits` (withdrawable), `locked_stakes`, `locked_bonds`, `commons` (burned, never withdrawable). `get_accounting` reports them with the real balance. Every effect happens after all validation, because a revert must leave nothing behind.

| Event | Stake | Bond |
| --- | --- | --- |
| settle | back to author | n/a |
| forfeit | to commons | n/a |
| upheld | junior's stake to challenger | refunded to challenger |
| rejected | stays locked until the claim settles | to the accused author |
| voided (inconclusive, expired, senior lost standing) | stays locked | refunded to challenger |

## Judgment

Validators see the senior title, abstract and numbered elements, and the junior title, abstract and elements, each inside quoted untrusted blocks whose delimiters are neutralised if the text tries to forge them. They return one JSON object: a per-element chart, two instruction-attempt flags, a confidence and a short rationale.

Decision rules, in order:

1. confidence below 80: inconclusive
2. senior instructs the reviewer and junior is clean: rejected; junior instructs and senior is clean: upheld; both or an unclear flag next to a yes: inconclusive
3. disclosed ≥ need and the senior flag is `no`: upheld (basis `overlap`)
4. disclosed + unclear < need and the junior flag is `no`: rejected (basis `absence`)
5. otherwise inconclusive

`need = max(1, ceil(count × percent / 100))`. Consensus requires the same outcome and basis; `overlap` verdicts must have disclosed counts within one, `absence` verdicts possible counts within one. Any malformed model output is a failure that costs one judging call but decides nothing. Per challenge: at most six calls, three semantic assessments, a doubling cooldown from 60 seconds, and only the two parties may spend the budget. After the judging window or an exhausted budget anyone can void the challenge.

## Threat model

| Threat | Mitigation |
| --- | --- |
| Copying a sealed commitment | commitment binds the author address; the copy can never be revealed and burns |
| Front-running a reveal | priority is fixed at sealing; a later copy is junior and challengeable |
| Pre-committing guesses of others' ideas | every unrevealed claim burns its stake |
| Hidden or reordered text | zero-width, bidi and control characters refused in every text field |
| Prompt injection | quoted untrusted blocks, delimiter neutralisation, instruction flags that make the manipulator lose |
| Generic senior elements | prompt tells the model to answer `unclear`, which cannot authorise a verdict |
| Challenge griefing | only earlier authors may challenge; failed challenges pay the accused; three-challenge cap; bounded delay |
| Sybil self-dealing | value is conserved: a pair gains nothing by challenging itself |
| Budget burning | only the two parties may request judging |
| Settlement DoS | pull payments; all permissionless calls pay the rightful owner, never the caller |
| Stuck challenges | expiry after the judging window, on budget exhaustion, or when the senior lost standing |

## Not in scope

Novelty or truth verification, identity of authors, legal effect of any record, off-chain evidence, appeals after a decisive verdict.
