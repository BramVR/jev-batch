# jev-batch agent notes

Read README.md for CLI contracts. `CLAUDE.md` is a symlink to this file.

## Gate

Run before handoff, on the tree you will push:

```sh
python3 scripts/gate.py  # check_install.py needs network to fetch setuptools
```

It runs every `run:` step in `.github/workflows/ci.yml` in order under the interpreter that runs it (`uv run --python 3.9 scripts/gate.py` gates the floor). It exits 1 if a step fails, if a step is not a plain `python3 ...` call, or if HEAD or the working tree changes during the run. Multi-line `run: |` steps, such as the Python 3.8 `below-floor` job, stay CI-only. Report its last line, which names the HEAD it gated.

## Rules

When a reviewer or the operator corrects a mistake, fix it and add or update a row. A row whose mistake can no longer happen gets deleted. A rule enforced only by this file is a repeat waiting to happen: move it up a level.

| Rule | Enforced by |
| --- | --- |
| A flag belongs to the commands that declare it in `build_parser()`. Never check flag/command combinations by hand; a flag on the wrong command must be an argparse usage error (exit 2). | Architecture: argparse subparsers, `allow_abbrev=False`. `tests/test_cli.py::test_each_command_rejects_flags_it_does_not_declare`; formal `flags-scoped` + mutants. |
| Text I/O names its encoding: `encoding="utf-8"` on `open`, `Path.open`, `read_text`, `write_text`, `TextIOWrapper`, `os.fdopen`; `subprocess.*` uses `encoding="utf-8"`, not `text=True`. | `scripts/lint.py` rule `implicit-encoding` (CI `lint` step) over tracked and untracked files; `tests/test_lint.py`. |
| `jev.py` stdio is UTF-8 whatever the locale. Callers never set `PYTHONIOENCODING`. | Architecture: `jev.main` reconfigures stdio. `tests/test_cli.py::test_stdio_is_utf8_whatever_the_locale`; formal `stdio-utf8` + mutant. |
| JSONL splits on `\n` only: iterate the file or stream. `str.splitlines()` also splits U+2028 inside JSON strings. | `scripts/lint.py` rule `jsonl-splitlines`: every call is an error naming the replacement. `tests/test_lint.py` replays the PR #14 line. |
| Help and error text derive facts from constants (`SCHEMA_VERSIONS`, `MAX_*`, `DEFAULT_*`). Send and `--dry-run` share `request_body()`. | `tests/test_cli.py` (patched `SCHEMA_VERSIONS`); formal `dry-run-measures-sent-bytes` + mutant; verify-jev `help states the full retry rule`. |
| The local gate is the CI step list, run on the tree you push. Never retype the steps or loop over them in a shell. | Architecture: `scripts/gate.py` reads `ci.yml`. `tests/test_gate.py` fails on a step it cannot run and on a tree that changes mid-run. |
| Question files load only through `load_questions()`. | Formal `questions-eval-same-loader` and the summarize loader mutant. |
| After changing `jev.py` or `scripts/demo_sessions.py`, review `check.py` coverage, then update `SYNC_BLOBS`. | `check.py --stale` in CI. |
| The retry rule has one source: `RETRY_RULE`, built from `NEVER_RETRIED` and `ENDPOINT_UNAVAILABLE`, which `send()` also uses. `--retries` help prints it; README and SKILL.md state it verbatim. | `tests/test_cli.py::test_docs_state_the_retry_rule_the_code_runs` (fails on the PR #15 README and on a code-only `NEVER_RETRIED` change); verify-jev `help states the full retry rule`. |
| Exit codes have one source: `EXIT_CODES` in `jev.py`, with `GATE_FALSE` and `GATE_ABSTAIN` for 10 and 11, never bare literals. Top-level and `classify` `--help` render it; README's `## Exit codes` table states it row by row. | `tests/test_cli.py::test_readme_and_help_list_the_exit_codes_the_code_returns`; formal `gate-exit` + mutants. |
| The lowest Python in the CI matrix is the floor: `MIN_PYTHON` in `jev.py` and `verify.py`, `requires-python` in `pyproject.toml`, the CI `install` job, and the `Python X.Y+` line in README and verify-jev SKILL.md. Below it, both scripts exit 1 with a version message. | `tests/test_cli.py::test_documented_python_floor_is_the_lowest_ci_version`; CI job `below-floor` runs both on 3.8; CI job `install` checks the installed command on the floor. |

## Exceptions

On the offending line only, with a human's approval:

```python
x = path.read_text()  # lint-allow: implicit-encoding reason="<why>" expires=YYYY-MM-DD approved-by=<github login>
```

Lint fails once `expires` passes. Agents do not fill in `approved-by` themselves.
