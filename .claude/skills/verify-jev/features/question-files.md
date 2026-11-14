# Question files

A user passes `--questions` a version-1 document (`{"schema_version": 1, "questions": {...}}`) or a legacy raw mapping, and both send the same API questions. Bad files fail before any request with a `jev: ...` line that never echoes file contents.

## Sub-features

- `legacy-mapping` a raw mapping is sent as `questions` unchanged.
- `v1-unwrap` a version-1 document sends only its `questions` mapping. The request body keys stay exactly `model`, `questions`, `state`.
- `version-reject` an unknown or non-integer `schema_version` exits 1 with `unsupported questions schema_version; supported: 1`.
- `wrapper-shape` missing `questions` or any extra top-level key exits 1 with `must contain exactly schema_version and questions`, and the extra value is not echoed.
- `empty-reject` an empty mapping, wrapped or not, exits 1 with `questions must be a nonempty API question mapping`.
- `json-reject` invalid JSON, non-UTF-8, or nonfinite numbers exit 1 with `questions file must be readable UTF-8 JSON`.

`--dry-run` loads question files the same way; the `dry-run` journey proves a version-1 file is priced on the unwrapped mapping.

## How to get to it (user POV)

- `python3 jev.py classify|batch --questions examples/failure-patterns.v1.json`
- `python3 jev.py classify|batch --questions examples/failure-patterns.json`
- `eval` reads `--questions` through the same loader; its steps live in [eval](./eval.md).
- `python3 jev.py summarize --questions <either form>` loads the file the same way; the `summarize` journey drives that path.

## Driving it with verify.py

Preconditions:

- Doctor passes. No network needed.
- Fixtures `noul.v1.json`, `choice.v1.json` (used by eval), `v2.json`, `extra.v1.json` (extra value is the stub's private body sentinel), `empty.v1.json`, and `broken.json` live in `work/`.

- **All steps.** Run `python3 .claude/skills/verify-jev/verify.py run question-files`. Every step prints `PASS`.
- **Proof.** The `question-files` transcript section shows identical `questions` in the stub bodies for `noul.json` and `noul.v1.json`, and zero stub requests for each rejection.
- **Live.** `live classify noul` in `--live` sends `noul.v1.json` to real OpenCode, so the real API accepts the unwrapped mapping.

## Gotchas

- A top-level `schema_version` key always means a versioned document. A legacy question cannot use that name.
- `schema_version` is never sent. Request identity is the unwrapped mapping, so the two forms are indistinguishable from the stub's view by design.
- The pre-feature build rejects every rejection step with the wrong message, so `run --ref 8de76f2 question-files` is a quick proof that the journey can fail.
