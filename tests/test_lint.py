from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lint  # noqa: E402

# Verbatim lines that reviewers flagged before lint existed.
PR9_FIXTURE = "path.write_text(questions if isinstance(questions, str) else json.dumps(questions))\n"
PR10_SEND = ('run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch"],\n'
             '                     input=payload, stdout=output, stderr=subprocess.PIPE, text=True)\n')
PR14_JSONL = ('results = [json.loads(line) for line in (folder / "results.jsonl")'
              '.read_text(encoding="utf-8").splitlines()]\n')


def rules(source, path="scripts/new.py"):
    return [error.split(": ")[1] for error in lint.lint(Path(path), source)]


class LintTests(unittest.TestCase):
    def test_flags_past_review_findings(self):
        self.assertEqual(rules(PR9_FIXTURE), ["implicit-encoding"])
        self.assertEqual(rules(PR10_SEND), ["implicit-encoding"])
        self.assertEqual(rules(PR14_JSONL), ["jsonl-splitlines"])

    def test_accepts_explicit_utf8_binary_and_file_iteration(self):
        self.assertEqual(rules('path.write_text(data, encoding="utf-8")\n'
                               'open(p, "rb").read()\n'
                               'path.open("wb")\n'
                               'subprocess.run(cmd, capture_output=True, encoding="utf-8")\n'
                               'subprocess.run(cmd, capture_output=True)\n'
                               'with path.open(encoding="utf-8") as f:\n    rows = [json.loads(x) for x in f]\n'), [])

    def test_splitlines_is_a_ratchet(self):
        path = "tests/test_cli.py"
        at_baseline = "x.splitlines()\n" * lint.SPLITLINES_BASELINE[path]
        self.assertEqual(rules(at_baseline, path), [])
        self.assertEqual(rules(at_baseline + "y.splitlines()\n", path), ["jsonl-splitlines"])
        self.assertIn("lower SPLITLINES_BASELINE", lint.lint(Path(path), "")[0])

    def test_exception_needs_reason_expiry_and_approver(self):
        later = (date.today() + timedelta(days=30)).isoformat()
        earlier = (date.today() - timedelta(days=1)).isoformat()
        ok = f'path.read_text()  # lint-allow: implicit-encoding reason="bytes are ASCII" expires={later} approved-by=BramVR\n'
        self.assertEqual(rules(ok), [])
        self.assertEqual(rules('path.read_text()  # lint-allow: implicit-encoding reason="x"\n'), ["implicit-encoding"])
        expired = ok.replace(later, earlier)
        self.assertIn("lint-allow expired", lint.lint(Path("scripts/new.py"), expired)[0])

    def test_repo_is_clean(self):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py")], capture_output=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stdout)


if __name__ == "__main__":
    unittest.main()
