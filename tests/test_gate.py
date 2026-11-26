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
    def run_gate(self, workflow):
        with tempfile.TemporaryDirectory() as tmp:
            ci = Path(tmp) / "ci.yml"
            ci.write_text(workflow, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = gate.main(ci)
        return code, out.getvalue()

    def test_runs_every_step_and_fails_if_any_fails(self):
        code, out = self.run_gate(
            "jobs:\n"
            "  check:\n"
            "    steps:\n"
            "      - run: python3 -c \"import sys; sys.exit(3)\"\n"
            "      - run: python3 -c \"print('second')\"  # a YAML comment\n"
            "  below-floor:\n"
            "    steps:\n"
            "      - run: |\n"
            "          python3 -c \"import sys; sys.exit(4)\"\n")
        self.assertEqual(code, 1)
        self.assertIn("FAIL (3)  python3 -c \"import sys; sys.exit(3)\"", out)
        self.assertIn("PASS  python3 -c \"print('second')\"", out)
        self.assertIn("SKIP (CI-only)  |", out)
        self.assertRegex(out, r"tree: HEAD [0-9a-f]{7,}, \d+ uncommitted paths")

    def test_a_tree_that_changes_during_the_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                            "--allow-empty", "-m", "init"], check=True)
            edit = f"python3 -c \"open({str(repo / 'new.py')!r}, 'a').write('y = 2')\""
            with mock.patch.object(gate, "ROOT", repo):
                code, out = self.run_gate(f"steps:\n  - run: {edit}\n")
        self.assertEqual(code, 1)
        self.assertIn("changed while the gate ran", out)
        self.assertRegex(out.rstrip().split("\n")[-1], r"^tree: HEAD [0-9a-f]{7,}, 1 uncommitted paths$")

    def test_a_step_it_cannot_run_directly_fails_instead_of_being_skipped(self):
        for step in ("python3 -c 'pass' && false", "\"python3 -c 'pass'\"", "make check"):
            code, out = self.run_gate(f"steps:\n  - run: python3 -c 'pass'\n  - run: {step}\n")
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
