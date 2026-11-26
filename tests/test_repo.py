from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD_NAMES = re.compile(r"jev[-_](?:cli|classify)", re.I)
# Other owners' projects keep the old name; history files record what was true then.
COMPETITOR_LINK = re.compile(r"\b(?!BramVR/)[\w.-]+/jev-cli\b", re.I)
HISTORY = {"CHANGELOG.md", "docs/demo.md", "tests/test_repo.py"}


def repo_files(pattern):
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                          pattern], capture_output=True, encoding="utf-8", check=True).stdout
    return sorted({p for p in out.split("\0") if p and (ROOT / p).is_file()})


def stray_old_names(files):
    """Return "path:line: text" for each old project name outside history files and competitor links."""
    found = []
    for path, text in files:
        if path in HISTORY:
            continue
        for number, line in enumerate(text.split("\n"), 1):
            if OLD_NAMES.search(COMPETITOR_LINK.sub("", line)):
                found.append(f"{path}:{number}: {line.strip()}")
    return found


class RepoTests(unittest.TestCase):
    def test_old_project_names_appear_only_in_history_and_competitor_links(self):
        files = [(p, (ROOT / p).read_bytes().decode("utf-8", "replace")) for p in repo_files("*")]
        self.assertEqual(stray_old_names(files), [], "the project is jev-batch; rename these, or link another "
                         "owner's repo as owner/name; CHANGELOG.md and docs/demo.md keep history")


if __name__ == "__main__":
    unittest.main()
