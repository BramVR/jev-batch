import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jev

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "eval"
QUESTIONS = {"route": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
             "urgent": {"type": "noul", "instructions": "Urgent?"}}


def label(id, value):
    return json.dumps({"id": id, "label": value})


def prediction(id, choice="a", confidence=0.75, question="route"):
    answer = {"type": "choice", "choice": choice}
    if confidence is not None:
        answer["confidence"] = confidence
    return json.dumps({"id": id, "answers": {question: answer}})


class EvalTests(unittest.TestCase):
    def run_eval(self, labels, predictions, questions=QUESTIONS, extra=("--question", "route")):
        with tempfile.TemporaryDirectory() as tmp:
            qpath, lpath = Path(tmp) / "q.json", Path(tmp) / "labels.jsonl"
            qpath.write_text(json.dumps(questions))
            lpath.write_text("".join(line + "\n" for line in labels))
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-only"}, clear=True), \
                 patch("sys.stdin", io.StringIO("".join(line + "\n" for line in predictions))), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("eval sent a request")), \
                 patch("jev.api_key", side_effect=AssertionError("eval read credentials")), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main(["eval", "--questions", str(qpath), "--labels", str(lpath), *extra])
        return code, json.loads(out.getvalue()) if code == 0 else out.getvalue(), err.getvalue()

    def assert_error(self, labels, predictions, message, **kwargs):
        code, out, err = self.run_eval(labels, predictions, **kwargs)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(message, err)

    def test_example_fixture_through_fresh_cli_is_exact_and_unchanged(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in EXAMPLE.iterdir()}
        with tempfile.TemporaryDirectory() as tmp, (EXAMPLE / "results.jsonl").open() as stdin:
            result = subprocess.run(
                [sys.executable, str(ROOT / "jev.py"), "eval",
                 "--questions", str(EXAMPLE / "questions.json"), "--labels", str(EXAMPLE / "labels.jsonl")],
                stdin=stdin, capture_output=True, text=True, timeout=30,
                env={"PATH": os.environ.get("PATH", ""), "HOME": tmp,
                     "JEV_BASE_URL": "https://api.typesafe.ai/v1", "TYPESAFE_API_KEY": "must-not-be-read"},
            )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"], {
            "labeled": 7, "scored": 6, "unscored": {"missing_prediction": 1, "unknown_choice": 0},
            "extra_predictions": 1, "calibrated": 5, "missing_confidence": 1})
        self.assertEqual((report["correct"], report["accuracy"]), (4, 4 / 6))
        self.assertEqual(report["confusion"], {
            "bug": {"bug": 1, "feature": 1, "question": 0},
            "feature": {"bug": 0, "feature": 2, "question": 0},
            "question": {"bug": 1, "feature": 0, "question": 1}})
        filled = {(b["low"], b["high"]): (b["count"], b["mean_confidence"], b["accuracy"])
                  for b in report["calibration"] if b["count"]}
        self.assertEqual(filled, {(0.5, 0.6): (1, 0.5, 1.0), (0.7, 0.8): (2, 0.75, 0.5),
                                  (0.9, 1.0): (2, 0.95, 0.5)})
        self.assertEqual(len(report["calibration"]), 10)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in EXAMPLE.iterdir()})

    def test_bin_endpoints_are_low_inclusive_and_one_is_in_top_bin(self):
        points = [0.0, 0.1, 0.0999, 0.3, 0.9, 1.0]
        code, report, _ = self.run_eval([label(i, "a") for i in range(len(points))],
                                        [prediction(i, confidence=c) for i, c in enumerate(points)])
        self.assertEqual(code, 0)
        self.assertEqual([b["count"] for b in report["calibration"]], [2, 1, 0, 1, 0, 0, 0, 0, 0, 2])
        self.assertEqual(report["calibration"][0]["mean_confidence"], 0.0999 / 2)

    def test_confidence_out_of_range_or_not_numeric_is_scored_but_not_calibrated(self):
        values = [None, "0.5", True, -0.1, 1.5, 1]
        code, report, _ = self.run_eval([label(i, "a") for i in range(len(values))],
                                        [prediction(i, confidence=c) for i, c in enumerate(values)])
        self.assertEqual(code, 0)
        self.assertEqual((report["counts"]["scored"], report["counts"]["calibrated"],
                          report["counts"]["missing_confidence"]), (6, 1, 5))
        self.assertEqual(report["calibration"][9]["count"], 1)

    def test_unknown_or_missing_predicted_choice_is_unscored(self):
        code, report, _ = self.run_eval(
            [label(1, "a"), label(2, "a"), label(3, "b"), label(4, "b")],
            [prediction(1, choice="c"), prediction(2, question="other"), prediction(3, choice=None),
             prediction(4, choice="b")])
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"]["unscored"], {"missing_prediction": 0, "unknown_choice": 3})
        self.assertEqual((report["counts"]["scored"], report["accuracy"]), (1, 1.0))

    def test_ids_join_on_exact_json_value(self):
        code, report, _ = self.run_eval([label(1, "a"), label("1", "b"), label({"k": [1]}, "a")],
                                        [prediction("1", choice="b"), prediction({"k": [1]})])
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"]["unscored"]["missing_prediction"], 1)
        self.assertEqual(report["correct"], 2)

    def test_empty_predictions_report_zero_scored_and_null_accuracy(self):
        code, report, _ = self.run_eval([label(1, "a")], [])
        self.assertEqual(code, 0)
        self.assertEqual((report["counts"]["scored"], report["accuracy"]), (0, None))
        self.assertTrue(all(b["mean_confidence"] is None and b["accuracy"] is None
                            for b in report["calibration"]))

    def test_invalid_inputs_exit_1_without_report(self):
        self.assert_error([], [prediction(1)], "labels file has no records")
        self.assert_error([label(1, "a"), label(1, "b")], [], "labels line 2: duplicate id")
        self.assert_error([label(1, "c")], [], "labels line 1: label must be one of the route options")
        self.assert_error([label(1, "a"), ""], [], "labels line 2: invalid JSON")
        self.assert_error([label(1, "a")], [prediction(1), prediction(1)], "predictions line 2: duplicate id")
        self.assert_error([label(1, "a")], ['{"answers": {}}'], "predictions line 1: expected an object with id")
        self.assert_error([label(1, "a")], ['{"id": 1, "answers": 3}'], "predictions line 1: expected answers object")
        self.assert_error([label(1, "a")], ["NaN"], "predictions line 1: invalid JSON")

    def test_question_selection(self):
        self.assert_error([label(1, "a")], [], "Choice question", extra=())
        self.assert_error([label(1, "a")], [], "Choice question", extra=("--question", "urgent"))
        code, report, _ = self.run_eval([label(1, "a")], [prediction(1)],
                                        questions={"route": QUESTIONS["route"]}, extra=())
        self.assertEqual((code, report["question"]), (0, "route"))

    def test_eval_requires_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            qpath = Path(tmp) / "q.json"
            qpath.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
                jev.main(["eval", "--questions", str(qpath)])
            self.assertEqual(exit_.exception.code, 2)
            self.assertIn("the following arguments are required: --labels", err.getvalue())


if __name__ == "__main__":
    unittest.main()
