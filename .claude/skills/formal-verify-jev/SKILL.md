---
name: formal-verify-jev
description: "Exhaustively check jev.py contracts over bounded input spaces: System One request shape and credential isolation, exact probability passthrough, JSONL batch prefix and flush invariants. Use when changing jev.py classify/main/Backend/read_json/api_key, or before trusting a change to request, number, or batch handling."
---

# Formal verification of jev.py contracts

`check.py` enumerates every combination in a stated finite domain and runs the real `jev.main` on each, in-process, with `urllib.request.urlopen` replaced by a scripted responder. No network, no deps. It reports each property in the code's terms and flags any change in known findings.

Tool choice. Python has no practical code-level verifier for this program: CrossHair-style symbolic execution stalls in `json` and `urllib`, and nothing here is concurrent, so a TLA+ model adds nothing. Bounded exhaustive enumeration over hand-picked equivalence classes drives the real code instead of a model of it. The pass covers the enumerated domain, not every string.

## Run

```sh
python3 .claude/skills/formal-verify-jev/check.py              # all properties, batch depth 3, ~4s
python3 .claude/skills/formal-verify-jev/check.py --mutants    # also prove each property can fail, ~30s
python3 .claude/skills/formal-verify-jev/check.py --depth 4 --only batch   # deeper batch, ~20s
python3 .claude/skills/formal-verify-jev/check.py --stale      # covered files changed since SYNC_COMMIT?
```

Exit 0 and `PASS` means no new violations, every known finding still reproduces, and, with `--mutants`, every mutant was caught.

## Does my change touch this?

Covered file: `jev.py`, all of `Backend`, `api_key`, `read_json`, `classify`, `main`. Sync commit: `SYNC_COMMIT` in `check.py`. Run `--stale`. Any diff in `jev.py` since then means run the full check with `--mutants`.

## Properties

Each property is one sentence of promise and one of harm.

- `batch-prefix`. Batch stdout is exactly `{"id": id_i, ...response_i}` for every line before the first failing line, in order. Otherwise a resumed batch silently loses or duplicates records.
- `batch-one-post-per-line`. One request per line that passed local validation, none for invalid lines, none after a failure, no retries. Otherwise records are double-billed or sent after the user was told the batch stopped.
- `batch-flush-before-next`. Line i is flushed before request i+1 starts. Otherwise a crash or timeout loses results already paid for.
- `batch-exit`. Exit 0 with empty stderr, or exit 1 with one line `jev: batch stopped at line N; ...` naming the first failing line. Otherwise users resume from the wrong line.
- `batch-no-body-leak`. HTTP error and unparseable response bodies never reach stdout or stderr. Otherwise private API text leaks into logs.
- `credential-read-only-for-typesafe` and `auth-only-to-typesafe`. Credentials are read, and `Authorization` is sent, only when urllib's own parse of the endpoint is `https` to `api.typesafe.ai`. Otherwise a TypeSafe key goes to another host.
- `auth-sent-to-typesafe-when-keyed`. With a key and that exact target, `Authorization: Bearer <key>` is sent. Otherwise TypeSafe users get silent 401s.
- `request-body`, `request-endpoint`, `request-method`, `request-timeout`, `request-content-type`, `request-user-agent`. The single POST goes to `base.rstrip("/") + "/systemone"` with body exactly `{model, state, questions}` plus `assets` when the record has them, and extra record fields are never sent. Otherwise private fields leak or the API rejects the call.
- `images-header-iff-images` and `box-rejected-locally-on-free`. `TypeSafe-Danger-Zone-Images: true` is sent exactly when there are assets or a Box question, and free OpenCode or Jev 1.13 rejects them before any request. Otherwise image data reaches a text-only backend.
- `numbers-passthrough-exact`. Every finite number in the response, including `5e-324`, `0.30000000000000004`, `-0.0`, `2**64`, comes out with the same type and float bits, and keys keep their order. `numbers-nonfinite-rejected`: `NaN`, `Infinity`, and overflowing literals like `1e999` anywhere in a response mean exit 1 and no stdout. Otherwise downstream code reads altered probabilities or invalid JSON.
- `*-clean-exit`. No input in scope escapes `main` as an uncaught exception.

Domain and bounds, all in `check.py`: `LINE_KINDS` (17 line outcomes) at every sequence length up to `--depth`; `BASE_URLS` x `MODELS` x `QUESTION_SETS` x `RECORDS` x key present or absent; `FINITE` + `NONFINITE` in three response slots for both commands. The fake `urlopen` builds an `http.client.HTTPConnection` from the request host, so urllib's host validation runs as in production.

Not covered: real sockets and TLS, `api_key`'s Keychain path (mocked as absent), argparse beyond what the cases hit, huge inputs, and live API behavior. `verify-jev` drives the real process and network path.

## Reading results

- `FAIL <property>: <case>` is a new violation with its first counterexample. The case string is the input: batch line kinds in order, or the base URL, model, question IDs, record, and key flag. Reproduce it by running `jev.py` with those inputs before calling it a bug.
- A bug in the code makes a property fail on a plausible input. A modeling error makes it fail because the oracle in `check.py` is wrong, for example a fixture serialized differently from what a user would write. Fix the oracle only when the README contract says the code's behavior is correct, and say so in the commit.
- `KNOWN <finding>` is an accepted current violation in `KNOWN_FINDINGS`. `FIXED? <finding>` means it stopped reproducing: confirm the fix, then delete its entry.
- `MUTANT MISSED` means a property can no longer fail on the break it guards. `MUTANT STALE` means the anchor text in `MUTANTS` no longer exists in `jev.py`. Update the anchor to the equivalent code; never delete a mutant to get green.

## Known findings at the sync commit

- F1. A tab or newline in `--base-url` makes `Backend` see `api.typesafe.ai` while urllib sees a different host. `jev.py` reads credentials (env key or a Keychain lookup), then `http.client` raises `InvalidURL`, which escapes `main` as a traceback. No key is sent; the request never leaves.
- F2. In `batch`, an API response that is not UTF-8, not JSON, or holds nonfinite numbers is reported as `invalid JSON record`, blaming the input line.

## When a change is intended

1. Update the oracle and domain in `check.py` to the new contract, with README in step.
2. Add a mutant for any new guard and confirm `--mutants` catches it.
3. Run the full check. When it describes the code at the new commit, set `SYNC_COMMIT` to that commit's short hash.

Keep it aligned after unchecked changes with `/maintain-formal-verification-skill`.
