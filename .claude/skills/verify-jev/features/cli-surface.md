# CLI surface

A user gets help, clear usage errors, and local rejection of bad flags or question files before anything is sent.

## Sub-features

- `help` prints usage naming `classify`, `batch`, `summarize`, `--questions`, `--question`, and `--dry-run`, exit 0.
- `usage-error` `classify`, `batch`, or `eval` without `--questions` exits 2 from argparse.
- `timeout-guard` nonfinite or nonpositive `--timeout` exits 1.
- `batch-input-guard` `batch --input` and `eval --input` exit 1; both read stdin only.
- `questions-guard` empty mapping or unknown question type exits 1, with or without `--dry-run`. Version and wrapper rules live in [question-files](./question-files.md).
- `retry-concurrency-guard` `--concurrency` outside 1..32 or `--retries` outside 0..10 exits 1 with no request sent; a non-integer value exits 2 from argparse.

## How to get to it (user POV)

- `python3 jev.py --help`
- `python3 jev.py classify|batch|eval --questions <file> [flags]`

## Driving it with verify.py

Preconditions:

- Doctor passes. No network needed.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run cli-surface`. Every step prints `PASS`.
- **Proof.** The `cli-surface` section of `evidence/transcript.md` shows each command, exit code, and `jev: ...` stderr line.

## Gotchas

- Argparse errors exit 2; every other failure exits 1. Do not collapse them in assertions.
- Question validation happens before backend checks. A bad question file never reaches a request.
