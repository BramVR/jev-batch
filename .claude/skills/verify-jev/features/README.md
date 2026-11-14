# jev-cli verification map

The maintained source for verifying what a jev-cli user can observe. Read this index, then the feature file, then run its journey.

## Baseline preconditions

- `python3 .claude/skills/verify-jev/verify.py doctor` exits 0.
- Each journey runs on a fresh copy under `.private/verify/<run-id>/build` with env reduced to `PATH`, `HOME`, `LANG`, plus what the step sets.
- Fixture files (`noul.json`, `choice.json`, `box.json`, `empty.json`, `weird.json`, `record.json`, `results.jsonl`, plus the question-file fixtures `noul.v1.json`, `choice.v1.json`, `v2.json`, `extra.v1.json`, `empty.v1.json`, `broken.json`, and eval's `labels.jsonl`, `dup-labels.jsonl`, `bad-labels.jsonl`) are written into the run's `work/` dir, which is the CLI's cwd. The same dir is `HOME` and holds one synthetic `.codex/sessions/synthetic.jsonl` for the demo journey.
- Offline journeys reach System One only through the loopback stub via `--base-url` or `JEV_BASE_URL`.

## Driving conventions

- Drive through `verify.py run <journey>`. Steps are literal `python3 jev.py ...` commands.
- Assert request shape from the stub's recorded requests, and output from stdout bytes.
- Use the sentinel `TYPESAFE_API_KEY` only to prove it is not sent. Never a real key.

## Proof and skip reporting

- CLI proof is command, stdin, stdout, stderr, and exit code from `evidence/transcript.md`.
- Request proof is the stub's recorded path, body, and headers for that step.
- Report a skipped live journey as skipped. An offline stub pass does not prove the live backend.

## Features

- [CLI surface](./cli-surface.md) covers help, usage errors, and local validation of questions and flags.
- [Question files](./question-files.md) covers version-1 and legacy question files, identical requests, and pre-HTTP rejection of bad files.
- [Classify one record](./classify.md) covers stdin and `--input`, verbatim output, and single-request failures.
- [Batch JSONL](./batch.md) covers ordering, duplicates, and stop-at-first-failure with preserved prefix.
- [Eval against hand labels](./eval.md) covers exact metrics, denominators, offline guarantee, and input errors.
- [Summarize results](./summarize.md) covers offline label counts, example IDs, confidence bins, and their diagnostics.
- [Response cache](./cache.md) covers `--cache` reruns with zero requests, misses on changed requests, resume after interrupt, and failed or corrupt entries.
- [Backend selection](./backend-select.md) covers env and flag precedence, Box gating, and credential isolation.
- [Demo redaction](./demo-redaction.md) covers `scripts/demo_sessions.py` redaction before save and before send, the old-sample second pass, opt-out, and the sample limit.
- [Dry run](./dry-run.md) covers offline token and cost estimates, unknown prices, and validation without credentials or requests.
- [Live OpenCode](./live-opencode.md) covers three synthetic requests to the free default backend.
