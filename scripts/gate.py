#!/usr/bin/env python3
"""Run the CI gate locally: every `run:` step in ci.yml, in order, under this interpreter.

`uv run --python 3.9 scripts/gate.py` gates the floor. Multi-line `run: |` steps, such as the
Python 3.8 `below-floor` job, stay CI-only. A one-line step that is not a plain `python3 ...` call
fails instead of being skipped. Every step runs even after a failure; exit 1 if any failed, or if
HEAD or the working tree changed while the gate ran.
"""
import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RUN = re.compile(r"^\s+(?:- )?run:\s*(.*)$", re.M)
SHELL = {"&&", "||", "|", ";", "&", ">", ">>", "<"}


def plan(ci=CI):
    """Return (kind, command) per `run:` step: kind is "run", "ci-only", or "unsupported"."""
    steps = []
    for value in RUN.findall(ci.read_text(encoding="utf-8")):
        if value[:1] in ("|", ">"):
            steps.append(("ci-only", value))
            continue
        try:
            argv = shlex.split(value, comments=True)
        except ValueError:
            argv = []
        direct = argv[:1] == ["python3"] and not SHELL.intersection(argv) and not value.startswith(("'", '"'))
        steps.append(("run" if direct else "unsupported", value))
    return steps


def tree():
    """Return (short HEAD, uncommitted path count, digest of HEAD plus every uncommitted change)."""
    git = ["git", "-C", str(ROOT)]
    head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, check=True).stdout.strip()
    status = subprocess.run([*git, "status", "--porcelain", "-z"], capture_output=True, check=True).stdout
    diff = subprocess.run([*git, "diff", "HEAD"], capture_output=True, check=True).stdout
    return head.decode(), status.count(b"\0"), hashlib.sha256(head + status + diff).hexdigest()


def main(ci=CI):
    before = tree()
    results = []
    for kind, step in plan(ci):
        if kind != "run":
            results.append((step, "SKIP (CI-only)" if kind == "ci-only" else "FAIL (not a plain python3 call)", 0))
            continue
        print(f"== {step}", flush=True)
        start = time.monotonic()
        code = subprocess.run([sys.executable, *shlex.split(step, comments=True)[1:]], cwd=ROOT).returncode
        results.append((step, "PASS" if code == 0 else f"FAIL ({code})", time.monotonic() - start))
    print("== gate")
    for step, verdict, seconds in results:
        print(f"{verdict}  {step}  {seconds:.0f}s")
    head, dirty, _ = before
    print(f"tree: HEAD {head}, {dirty} uncommitted paths")
    if tree() != before:
        print("FAIL  HEAD or the working tree changed while the gate ran; rerun it on the final tree")
        return 1
    ran = [verdict for _, verdict, _ in results if not verdict.startswith("SKIP")]
    return 1 if not ran or any(verdict != "PASS" for verdict in ran) else 0


if __name__ == "__main__":
    raise SystemExit(main())
