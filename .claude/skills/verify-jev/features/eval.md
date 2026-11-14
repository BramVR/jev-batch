# Eval against hand labels

`eval` scores saved batch output for one Choice question against a hand-labeled `{id,label}` JSONL file and prints one JSON report. It is offline: no request, no credential read, input files only read.

## Sub-features

- `eval-metrics` exact `counts`, `correct`, `accuracy`, `confusion[label][choice]`, and ten fixed calibration buckets with `count`, `mean_confidence`, `accuracy`.
- `eval-denominators` missing predictions and unknown choices are unscored; extra predictions are counted and ignored; missing or out-of-range confidence is scored but not calibrated.
- `eval-offline` nothing reaches System One even with `JEV_BASE_URL` set and a key in the env.
- `eval-input-errors` duplicate IDs in either file, a label outside criteria keys, an empty labels file, and malformed lines exit 1 with no stdout and name the file and line.
- `eval-question-files` `--questions` accepts either question-file form through the same loader as `classify` and `batch`; a version-1 document scores identically and an unknown `schema_version` exits 1 before scoring.
- `eval-flags` eval requires `--labels`; `--question` must name a Choice question (optional when the mapping has one); `--labels` and `--question` are rejected on `classify` and `batch`. `--cache` is rejected on eval, which sends nothing to cache.

## How to get to it (user POV)

- `python3 jev.py eval --questions q.json --labels labels.jsonl [--question name] < results.jsonl`
- Repo fixture: `examples/eval/` (README "Evaluate against hand labels").

## Driving it with verify.py

Preconditions:

- Doctor passes. `choice.json`, `choice.v1.json`, `v2.json`, `labels.jsonl`, `dup-labels.jsonl`, and `bad-labels.jsonl` are written into `work/`; predictions come from `EVAL_RESULTS` on stdin.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run eval`. Every step prints `PASS`.
- **Metrics proof.** `scores fixture exactly, sends nothing` checks the hand-computed report in `eval_ok()` and that the stub saw zero requests.
- **Unit cover.** Bin endpoints, confidence types, ID join by exact JSON value, empty predictions, and fixture immutability live in `tests/test_eval.py`. Offline and credential isolation over bounded inputs live in `formal-verify-jev` (`eval-*` properties).

## Gotchas

- IDs join by canonical JSON, so `1` and `"1"` are different records.
- Calibration buckets are low-inclusive on literal float edges `0.1 ... 0.9`; `1.0` lands in the top bucket.
