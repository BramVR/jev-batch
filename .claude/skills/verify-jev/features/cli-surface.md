# CLI surface

A user gets help, clear usage errors, and local rejection of bad flags or question files before anything is sent.

## Sub-features

- `help` prints usage naming `classify`, `batch`, and `--questions`, exit 0.
- `usage-error` missing `--questions` exits 2 from argparse.
- `timeout-guard` nonfinite or nonpositive `--timeout` exits 1.
- `batch-input-guard` `batch --input` exits 1; batch reads stdin only.
- `questions-guard` empty mapping or unknown question type exits 1. Version and wrapper rules live in [question-files](./question-files.md).

## How to get to it (user POV)

- `python3 jev.py --help`
- `python3 jev.py classify|batch --questions <file> [flags]`

## Driving it with verify.py

Preconditions:

- Doctor passes. No network needed.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run cli-surface`. Every step prints `PASS`.
- **Proof.** The `cli-surface` section of `evidence/transcript.md` shows each command, exit code, and `jev: ...` stderr line.

## Gotchas

- Argparse errors exit 2; every other failure exits 1. Do not collapse them in assertions.
- Question validation happens before backend checks. A bad question file never reaches a request.
