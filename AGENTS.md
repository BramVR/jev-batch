# jev-batch agent notes

Read README.md for CLI contracts. `CLAUDE.md` is a symlink to this file.

## Gate

CI (`.github/workflows/ci.yml`) runs exactly these; run them before handoff:

```sh
python3 scripts/lint.py
python3 -m unittest discover -s tests -v
python3 .claude/skills/verify-jev/verify.py run
python3 .claude/skills/formal-verify-jev/check.py --mutants
python3 .claude/skills/formal-verify-jev/check.py --stale
python3 scripts/check_install.py  # needs network to fetch setuptools
```

## Rules

When a reviewer or the operator corrects a mistake, fix it and add or update a row. A row whose mistake can no longer happen gets deleted. A rule enforced only by this file is a repeat waiting to happen: move it up a level.

| Rule | Enforced by |
| --- | --- |
| A flag belongs to the commands that declare it in `build_parser()`. Never check flag/command combinations by hand; a flag on the wrong command must be an argparse usage error (exit 2). | Architecture: argparse subparsers, `allow_abbrev=False`. `tests/test_cli.py::test_each_command_rejects_flags_it_does_not_declare`; formal `flags-scoped` + mutants. |
| Text I/O names its encoding: `encoding="utf-8"` on `open`, `Path.open`, `read_text`, `write_text`, `TextIOWrapper`, `os.fdopen`; `subprocess.*` uses `encoding="utf-8"`, not `text=True`. | `scripts/lint.py` rule `implicit-encoding` (CI `lint` step); `tests/test_lint.py`. |
| `jev.py` stdio is UTF-8 whatever the locale. Callers never set `PYTHONIOENCODING`. | Architecture: `jev.main` reconfigures stdio. `tests/test_cli.py::test_stdio_is_utf8_whatever_the_locale`; formal `stdio-utf8` + mutant. |
| JSONL splits on `\n` only: iterate the file or stream. `str.splitlines()` also splits U+2028 inside JSON strings. | `scripts/lint.py` rule `jsonl-splitlines`: every call is an error naming the replacement. `tests/test_lint.py` replays the PR #14 line. |
| Help and error text derive facts from constants (`SCHEMA_VERSIONS`, `MAX_*`, `DEFAULT_*`). Send and `--dry-run` share `request_body()`. | `tests/test_cli.py` (patched `SCHEMA_VERSIONS`); formal `dry-run-measures-sent-bytes` + mutant; verify-jev `help states the full retry rule`. |
| Question files load only through `load_questions()`. | Formal `questions-eval-same-loader` and the summarize loader mutant. |
| After changing `jev.py` or `scripts/demo_sessions.py`, review `check.py` coverage, then update `SYNC_BLOBS`. | `check.py --stale` in CI. |
| The retry rule has one source: `RETRY_RULE`, built from `NEVER_RETRIED` and `ENDPOINT_UNAVAILABLE`, which `send()` also uses. `--retries` help prints it; README and SKILL.md state it verbatim. | `tests/test_cli.py::test_docs_state_the_retry_rule_the_code_runs` (fails on the PR #15 README and on a code-only `NEVER_RETRIED` change); verify-jev `help states the full retry rule`. |
| The lowest Python in the CI matrix is the floor: `MIN_PYTHON` in `jev.py` and `verify.py`, `requires-python` in `pyproject.toml`, the CI `install` job, and the `Python X.Y+` line in README and verify-jev SKILL.md. Below it, both scripts exit 1 with a version message. | `tests/test_cli.py::test_documented_python_floor_is_the_lowest_ci_version`; CI job `below-floor` runs both on 3.8; CI job `install` checks the installed command on the floor. |

## Exceptions

On the offending line only, with a human's approval:

```python
x = path.read_text()  # lint-allow: implicit-encoding reason="<why>" expires=YYYY-MM-DD approved-by=<github login>
```

Lint fails once `expires` passes. Agents do not fill in `approved-by` themselves.
