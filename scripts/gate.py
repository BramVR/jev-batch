#!/usr/bin/env python3
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RUN = re.compile(r"""^\s+(?:- )?["']?run["']?:\s*(.*)$""", re.M)
TOKEN = re.compile(r"[\w./=:-]+")


def plan(ci=CI):
    steps = []
    for value in RUN.findall(ci.read_text(encoding="utf-8")):
        if value[:1] in ("|", ">"):
            steps.append(("ci-only", value))
            continue
        argv = re.sub(r"\s+#.*$", "", value).split()
        direct = argv[:1] == ["python3"] and all(TOKEN.fullmatch(arg) for arg in argv)
        steps.append(("run" if direct else "unsupported", value))
    return steps


def tree():
    git = ["git", "-C", str(ROOT)]
    head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, check=True).stdout.strip()
    status = subprocess.run([*git, "status", "--porcelain", "-z"], capture_output=True, check=True).stdout
    diff = subprocess.run([*git, "diff", "HEAD"], capture_output=True, check=True).stdout
    digest = hashlib.sha256(head + status + diff)
    untracked = subprocess.run([*git, "ls-files", "-z", "--others", "--exclude-standard"], capture_output=True,
                               check=True).stdout
    for path in filter(None, untracked.split(b"\0")):
        entry = ROOT / path.decode()
        content = os.readlink(entry).encode() if entry.is_symlink() else entry.read_bytes() if entry.is_file() else b""
        digest.update(path + b"\0" + hashlib.sha256(content).digest())
    return head.decode(), status.count(b"\0"), digest.hexdigest()


def main(ci=CI):
    before = tree()
    results = []
    for kind, step in plan(ci):
        if kind != "run":
            results.append((step, "SKIP (CI-only)" if kind == "ci-only" else "FAIL (not a plain python3 call)", 0))
            continue
        print(f"== {step}", flush=True)
        start = time.monotonic()
        code = subprocess.run([sys.executable, *re.sub(r"\s+#.*$", "", step).split()[1:]], cwd=ROOT).returncode
        results.append((step, "PASS" if code == 0 else f"FAIL ({code})", time.monotonic() - start))
    print("== gate")
    for step, verdict, seconds in results:
        print(f"{verdict}  {step}  {seconds:.0f}s")
    changed = tree() != before
    if changed:
        print("FAIL  HEAD or the working tree changed while the gate ran; rerun it on the final tree")
    head, dirty, _ = before
    print(f"tree: HEAD {head}, {dirty} uncommitted paths")
    ran = [verdict for _, verdict, _ in results if not verdict.startswith("SKIP")]
    return 1 if changed or not ran or any(verdict != "PASS" for verdict in ran) else 0


if __name__ == "__main__":
    raise SystemExit(main())
