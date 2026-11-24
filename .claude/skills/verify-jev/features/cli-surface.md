# CLI surface

A user gets help, clear usage errors, and local rejection of bad flags or question files before anything is sent.

## Sub-features

- `help` top-level `--help` names all four commands; `batch --help` lists `--questions`, `--dry-run`, and `--concurrency` but not `--labels`; `summarize --help` lists `--question` but not `--retries`.
- `help-retry-rule` the `--retries` help names every retry case: 429, 5xx, connection errors, timeouts, and the `Endpoint is unavailable` body on statuses other than 401/403/422. Keep it in step with README when the policy changes.
- `usage-error` `classify`, `batch`, or `eval` without `--questions`, or `eval` without `--labels`, exits 2 from argparse.
- `flag-scope` each command declares only its own flags, so a flag on the wrong command exits 2 with `unrecognized arguments` and sends nothing: `--input` on `batch` or `eval`, `--concurrency` on `classify` or `summarize`, `--retries` on `eval`.
- `timeout-guard` nonfinite or nonpositive `--timeout` exits 1.
- `questions-guard` empty mapping or unknown question type exits 1, with or without `--dry-run`. Version and wrapper rules live in [question-files](./question-files.md).
- `python-floor` below `MIN_PYTHON`, any `jev.py` invocation, `--help` included, exits 1 with `jev.py needs Python X.Y+; this is <version>` on stderr before parsing arguments. verify.py cannot drive it, because it would need an interpreter below the floor, and verify.py itself refuses one. CI job `below-floor` in `.github/workflows/ci.yml` proves it on 3.8. Locally, check only that the floor interpreter (the lowest CI matrix version) runs `jev.py --help` with exit 0.
- `retry-concurrency-guard` `--concurrency` outside 1..32 or `--retries` outside 0..10 exits 1 with no request sent; a non-integer value exits 2 from argparse. `--dry-run` accepts in-range values and ignores them; out-of-range values still exit 1. Base URL rules are in [Backend selection](./backend-select.md).

## How to get to it (user POV)

- `python3 jev.py --help`, `python3 jev.py batch --help`
- `python3 jev.py classify|batch|eval --questions <file> [flags]`

## Driving it with verify.py

Preconditions:

- Doctor passes. No network needed.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run cli-surface`. Every step prints `PASS`.
- **Proof.** The `cli-surface` section of `evidence/transcript.md` shows each command, exit code, and `jev: ...` stderr line.

## Gotchas

- Argparse errors, including a flag the command does not declare, exit 2; every other failure exits 1. Do not collapse them in assertions.
- Question validation happens before backend checks. A bad question file never reaches a request.
