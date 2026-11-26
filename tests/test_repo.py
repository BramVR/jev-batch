import ast
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD_NAMES = re.compile(r"jev[-_](?:cli|classify)", re.I)
COMPETITOR_LINK = re.compile(r"\b(?!BramVR/)[\w.-]+/jev-cli\b", re.I)
HISTORY = {"CHANGELOG.md", "docs/demo.md", "tests/test_repo.py"}


def repo_files(pattern):
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                          pattern], capture_output=True, encoding="utf-8", check=True).stdout
    return sorted({p for p in out.split("\0") if p and (ROOT / p).is_file()})


def stray_old_names(files):
    found = []
    for path, text in files:
        if path in HISTORY:
            continue
        for number, line in enumerate(text.split("\n"), 1):
            if OLD_NAMES.search(COMPETITOR_LINK.sub("", line)):
                found.append(f"{path}:{number}: {line.strip()}")
    return found


def imported_modules(text):
    names = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def not_stdlib(names):
    probe = "import importlib.util, sys; print(' '.join(n for n in sys.argv[1:] if importlib.util.find_spec(n) is None))"
    run = subprocess.run([sys.executable, "-I", "-S", "-c", probe, *sorted(names)], capture_output=True,
                         encoding="utf-8", check=True)
    return set(run.stdout.split())


class RepoTests(unittest.TestCase):
    def test_old_project_names_appear_only_in_history_and_competitor_links(self):
        files = [(p, (ROOT / p).read_bytes().decode("utf-8", "replace")) for p in repo_files("*")]
        self.assertEqual(stray_old_names(files), [], "the project is jev-batch; rename these, or link another "
                         "owner's repo as owner/name; CHANGELOG.md and docs/demo.md keep history")

    def test_python_imports_only_the_standard_library(self):
        paths = repo_files("*.py")
        local = {Path(p).stem for p in paths} | {Path(p).parts[0] for p in paths}
        imported = {}
        for path in paths:
            for name in imported_modules((ROOT / path).read_text(encoding="utf-8")) - local:
                imported.setdefault(name, path)
        missing = not_stdlib(imported)
        self.assertEqual({imported[n]: n for n in missing}, {}, "jev-batch has no dependencies; use the standard "
                         "library, or ask before adding a dependency")


if __name__ == "__main__":
    unittest.main()
