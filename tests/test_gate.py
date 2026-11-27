import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gate  # noqa: E402


class GateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def script(self, name, source):
        path = self.tmp / name
        path.write_text(source, encoding="utf-8")
        return f"python3 {path}"

    def run_gate(self, workflow):
        ci = self.tmp / "ci.yml"
        ci.write_text(workflow, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(ci)
        return code, out.getvalue()

    def test_runs_every_step_and_fails_if_any_fails(self):
        fail, ok = self.script("fail.py", "raise SystemExit(3)\n"), self.script("ok.py", "print('second')\n")
        code, out = self.run_gate(
            "jobs:\n"
            "  check:\n"
            "    steps:\n"
            f"      - run: {fail}\n"
            f"      - \"run\": {ok}  # a YAML comment\n"
            "  below-floor:\n"
            "    steps:\n"
            "      - run: |\n"
            f"          {fail}\n")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL (3)  {fail}", out)
        self.assertIn(f"PASS  {ok}", out)
        self.assertIn("SKIP (CI-only)  |", out)
        self.assertRegex(out, r"tree: HEAD [0-9a-f]{7,}, \d+ uncommitted paths")

    def test_a_tree_that_changes_during_the_run_fails(self):
        repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        (repo / "scratch").mkdir()
        (repo / "scratch" / "note.txt").write_text("x = 1\n", encoding="utf-8")
        (repo / "dangling").symlink_to(repo / "missing")
        for name, source in (("edit.py", f"open({str(repo / 'scratch' / 'note.txt')!r}, 'a').write('y')\n"),
                             ("rename.py", f"import os; os.rename({str(repo / 'scratch' / 'note.txt')!r}, "
                                           f"{str(repo / 'scratch' / 'note.py')!r})\n")):
            with self.subTest(name), mock.patch.object(gate, "ROOT", repo):
                code, out = self.run_gate(f"steps:\n  - run: {self.script(name, source)}\n")
            self.assertEqual(code, 1)
            self.assertIn("changed while the gate ran", out)
            self.assertRegex(out.rstrip().split("\n")[-1], r"^tree: HEAD [0-9a-f]{7,}, 2 uncommitted paths$")

    def test_a_step_it_cannot_run_directly_fails_instead_of_being_skipped(self):
        ok = self.script("ok.py", "")
        for step in (f"{ok} && false", f"{ok} arg;false", f"\"{ok}\"", f"{ok} $HOME", "make check"):
            code, out = self.run_gate(f"steps:\n  - run: {ok}\n  - run: {step}\n")
            self.assertEqual(code, 1, step)
            self.assertIn("FAIL (not a plain python3 call)", out, step)

    def test_a_workflow_with_no_runnable_steps_fails(self):
        self.assertEqual(self.run_gate("jobs: {}\n")[0], 1)
        self.assertEqual(self.run_gate("steps:\n  - run: |\n      true\n")[0], 1)

    def test_every_ci_step_runs_or_is_a_ci_only_block(self):
        kinds = [kind for kind, _ in gate.plan()]
        self.assertNotIn("unsupported", kinds)
        self.assertGreater(kinds.count("run"), 0)


if __name__ == "__main__":
    unittest.main()
