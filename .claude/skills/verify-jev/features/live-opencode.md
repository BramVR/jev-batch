# Live OpenCode

The default backend, free OpenCode `jev-1.13-free` at `https://opencode.ai/zen/v1/systemone`, answers real Noul and Choice questions without a key.

## Sub-features

- `live-noul` one classify returns `answers.billing.noul` in [0, 1] with the ID preserved.
- `live-choice-batch` a two-line batch returns IDs in order and a full probability map over exactly the criteria labels, summing to 1 within 0.01.

## How to get to it (user POV)

- `python3 jev.py classify --questions noul.json` with no base URL flag or env.

## Driving it with verify.py

Preconditions:

- Doctor reports `opencode.ai resolves`.
- Inputs are the synthetic records in `LIVE` in `verify.py`. Nothing else.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run --live live-opencode`. Three requests total. Both steps print `PASS`.
- **Proof.** The transcript shows the real response, including `model: jev-1.13-free` and `usage.opencode.billing_source: free`.

## Gotchas

- The free model returns coarse values, often integer `0`/`1` probabilities and `0.99` for Noul. Checks accept ints.
- Network flakiness fails the step with `connection failed or timed out`. The CLI never retries, so rerun once before calling it a regression.
- Never add session data or real user text to `LIVE`.
