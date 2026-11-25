---
name: verify-jev
description: "Drive the jev-batch command line (jev.py classify/batch including --dry-run estimates, eval, summarize, scripts/demo_sessions.py) end to end against a fresh copy and capture evidence. Use after changing jev.py, the demo script or its redaction, flags, backend selection, cache, pricing, or output, or when asked to prove the CLI works."
---

# Verify jev-batch

`jev.py` is a short-lived stdlib CLI. There is no server to keep alive. `verify.py` copies the code under test into an empty dir, runs each step as `python3 jev.py ...` or `python3 scripts/demo_sessions.py ...` in a fresh process with a scrubbed env, and points `--base-url` at a loopback stub of System One so the request the CLI actually sends is observable. Live calls to OpenCode are opt-in and limited to four tiny synthetic records, six requests unless the CLI retries a transient failure.

Helper: `.claude/skills/verify-jev/verify.py` (stdlib only, Python 3.9+). Run every command from the repo root.

## Doctor

```sh
python3 .claude/skills/verify-jev/verify.py doctor
```

Read-only. Below `MIN_PYTHON`, every `verify.py` command, doctor included, exits 1 with `verify.py needs Python X.Y+; this is <version>` before it does anything. Otherwise doctor reports Python version, git state, whether loopback can host the stub, whether `opencode.ai` resolves, and leftover scratch dirs. Exit 1 means do not drive yet. Run it first whenever a run looks wrong.

## Launch and drive

```sh
python3 .claude/skills/verify-jev/verify.py run                     # all offline journeys, worktree build
python3 .claude/skills/verify-jev/verify.py run batch eval summarize  # chosen journeys
python3 .claude/skills/verify-jev/verify.py run demo-redaction      # demo redaction, synthetic HOME
python3 .claude/skills/verify-jev/verify.py run --ref HEAD          # committed code only
python3 .claude/skills/verify-jev/verify.py run --live live-opencode  # 6 real OpenCode requests
```

- `--ref worktree` (default) copies tracked plus untracked-unignored files, so uncommitted edits are tested. Any other value is a git ref exported with `git archive`.
- Each run gets `.private/verify/<run-id>/` with `build/`, `work/` (fixture files, `HOME`), and `evidence/`.
- Each step prints `PASS` or `FAIL` with the reason. Exit 0 only if every step passed.
- Journeys are data in `journeys()` and `LIVE` in `verify.py`. A step's optional `before(work)` hook prepares or checks files in `work/` before it runs; returning `False` fails the step. The feature map below says what each proves. Add a step there when you add a user-facing behavior.
- Ad-hoc driving, for a case no journey covers: `run --keep-build cli-surface`, then from `.private/verify/<run-id>/work/` run `python3 ../build/jev.py ...` yourself and save the command, stdout, stderr, and exit code under that run's `evidence/`. The stub is gone by then, so offline request checks belong in a new journey step instead.

Live rules. Only `--live` touches the network. `--dry-run` steps target real hostnames for pricing but never connect, and only `opencode.ai/zen/v1/systemone` with the synthetic records in `LIVE`. Never feed session data, `.private/demo/` files, or any real user text to a live step. Never point a live step at TypeSafe; that needs a real key.

## Evidence

- `evidence/transcript.md` holds every step's command, stdin, exit code, stdout, stderr, and the requests the stub received (path, JSON body, selected headers).
- `evidence/results.json` holds the same data plus the ref, `HEAD`, and whether live ran.
- A proof cites the run id and the transcript section for the feature. Request shape is proven from the stub's view, not from the CLI's own output.
- Every step also fails on a traceback, on the stub's private error body appearing in output, or on the sentinel key appearing in stdout. A step's `files` check reads the build after it runs, such as the demo's saved sample. A step with `min_seconds` also fails when the run finished sooner, which proves a `Retry-After` wait.
- Stub queue entries are `(status, body, delay)` with an optional fourth item of response headers. Each recorded request carries `in_flight`, the number of requests the stub was handling when it arrived.

## Cleanup

`run` deletes `build/` and `work/` when it finishes, pass or fail; the stub dies with the process. After a crash or `--keep-build`:

```sh
python3 .claude/skills/verify-jev/verify.py cleanup
```

It removes only `build/` and `work/` under `.private/verify/`. Evidence stays. `.private/` is git-ignored; never commit it.

## Features

The map in [features/README.md](features/README.md) is the maintained list of user-facing behavior and how each is proven. A change to a feature drives every entry point the map lists for it. Keep it current with `/maintain-verification-skill`.
