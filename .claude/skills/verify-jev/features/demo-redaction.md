# Demo redaction

`scripts/demo_sessions.py` redacts secrets, bearer and API tokens, emails, and home paths from session excerpts before it saves `.private/demo/sample.jsonl`, and again on the outgoing copy at `--send`. `--no-redact` opts out of both. The patterns and limits live in README `### Redaction`.

## Sub-features

- `redact-prepare` prepare saves only placeholders and prints `redaction.policy` plus per-rule `redaction.counts`, never matched text. Counts are complete placeholders in the saved excerpts, so a match past the 4,000-character cap, or a placeholder the cap cuts, is not counted.
- `redact-send` send leaves the saved sample unchanged, sends no fixture value, and reports zero second-pass counts for a freshly prepared sample.
- `redact-old-sample` send redacts the outgoing copy of a raw sample made before redaction existed, and the file on disk stays raw.
- `redact-opt-out` `--no-redact` sends the sample as extracted and reports policy `disabled`.
- `demo-utf8` send pipes the sample to `jev.py batch` as UTF-8 and pins the child's stdio to UTF-8, so non-ASCII text arrives intact even when the inherited `PYTHONIOENCODING` or locale is ASCII.
- `demo-limit` `--limit` outside 1..20 exits 2 before any request.

## How to get to it (user POV)

- `python3 scripts/demo_sessions.py` then, after review and authorization, `python3 scripts/demo_sessions.py --send`.

## Driving it with verify.py

Preconditions:

- Doctor passes. `HOME` is the run's `work/`, which holds one synthetic `~/.codex/sessions/synthetic.jsonl` built from `DEMO_TEXT`. Never point this journey at a real home or a real `.private/demo/`.
- `JEV_BASE_URL` sends the demo's inner `jev.py batch` to the stub. The script writes `.private/demo/` inside the fresh build, not the repo.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run demo-redaction`. Every step prints `PASS`.
- **Leak proof.** The stub's recorded bodies for the send steps contain none of `DEMO_SECRETS`. The `--no-redact` step shows the opposite on purpose, so the check can fail.
- **Can fail.** `run --ref 8de76f2 demo-redaction` (before redaction) fails the first four steps, and its transcript shows the fixture values in the stub bodies. `run --ref 9ac1b3e demo-redaction` (before the UTF-8 fix) fails the UTF-8 step with `api_exit_code` 1 and no stub request.
- **Unit cover.** Nested values, repeated values, false positives, redaction before the 4,000-character cap, counts past the cap, and the UTF-8 send live in `tests/test_demo.py`.

## Gotchas

- Steps run in order and share the build's `.private/demo/`. The old-sample and UTF-8 steps overwrite the sample with `setup` before they run.
- The `--limit 21` step passes on code without redaction too. It guards the existing limit, not redaction.
