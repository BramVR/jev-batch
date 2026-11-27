import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_prose  # noqa: E402

PR41_SLASH = "- Real CLI against a local stub: noul 0.2 / 0.4 / 0.5 with `--fail-under 0.5` exits 10 / 10 / 0."
PR19_TITLE = "refactor!: per-command flags, UTF-8 stdio, encoding/JSONL lint, AGENTS.md rule table"
PR16_COUNT = "- `verify.py run chunk`: 28/28 PASS"
GOOD = """Closes #1.

## Why

The gate ran by hand.

## What changed

- `scripts/gate.py` runs each `run:` step in `.github/workflows/ci.yml`, see https://example.com/a/b.
  - A nested item with `a/b` in code stays legal.

```sh
python3 scripts/gate.py --x -> y
```

## Verification

- 12 of 12 tests pass.
"""


def rules(title, body):
    return [problem.split(": ")[1] for problem in check_prose.check(title, body)]


class ProseTests(unittest.TestCase):
    def test_flags_past_pr_text(self):
        self.assertEqual(rules(PR19_TITLE, GOOD), ["slash"])
        self.assertEqual(rules(None, GOOD + PR41_SLASH + "\n"), ["slash"])
        self.assertEqual(rules(None, GOOD + PR16_COUNT + "\n"), ["slash"])

    def test_code_urls_fences_and_list_markers_are_not_prose(self):
        self.assertEqual(rules("feat(gate): run `ci.yml` steps", GOOD), [])

    def test_each_writing_rule(self):
        for text, rule in (("It failed — twice.", "dash"), ("It failed -- twice.", "dash"),
                           ("It said “no”.", "smart-quote"), ("Input -> output.", "arrow"),
                           ("We leverage the cache.", "ai-word"), ("Additionally, it exits.", "ai-word"),
                           ("Run it in order to pass.", "filler"), ("Flags, etc.", "filler"),
                           ("Let me know if it breaks.", "chatbot")):
            self.assertEqual(rules(None, GOOD + text + "\n"), [rule], text)

    def test_body_shape(self):
        self.assertEqual(rules(None, GOOD.replace("## What changed", "## What")), ["heading"])
        self.assertEqual(rules(None, GOOD.replace("## Why", "## Summary")), ["heading", "heading"])
        self.assertEqual(rules(None, GOOD.replace("## Verification", "## Scope")), [])
        self.assertEqual(rules(None, GOOD + "\n## Why\n\nAgain.\n"), ["heading"])
        self.assertEqual(rules(None, GOOD + "- line\n" * 30), ["length"])
        self.assertEqual(rules(None, GOOD + "```md\n## Summary\n```\n"), [])
        self.assertEqual(rules(None, "```md\n## Why\n```\n"), ["heading"])

    def test_pr_env_with_an_empty_body_fails(self):
        with mock.patch.dict("os.environ", {"PR_TITLE": "fix: x", "PR_BODY": ""}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = check_prose.main(["--pr-env"])
        self.assertEqual(code, 1)
        self.assertIn("first heading must be '## Why'", out.getvalue())


if __name__ == "__main__":
    unittest.main()
