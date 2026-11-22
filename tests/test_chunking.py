import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import jev

QUESTIONS = {"route": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}
CAPACITY = 10
BUDGET = jev.REQUEST_OVERHEAD_TOKENS + len(json.dumps(QUESTIONS, ensure_ascii=False).encode()) + CAPACITY


def answer(a, b, choice="a"):
    return {"model": "jev-1.13-free", "usage": {"input_tokens": 7},
            "answers": {"route": {"type": "choice", "choice": choice, "confidence": max(a, b),
                                  "probabilities": {"a": a, "b": b}}}}


class ChunkingTests(unittest.TestCase):
    def run_cli(self, data, responses, command="classify", budget=BUDGET, questions=QUESTIONS, extra=()):
        sent = []
        replies = iter(responses)

        def http(request, timeout):
            sent.append(json.loads(request.data)["state"])
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return contextlib.closing(io.BytesIO(json.dumps(reply).encode()))

        extra = ([] if budget is None else ["--chunk-tokens", str(budget)]) + ["--retries", "0", *extra]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(questions), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch("sys.stdin", io.StringIO(data)), \
                 patch("urllib.request.urlopen", side_effect=http), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main([command, "--questions", str(path), *extra])
        return code, out.getvalue(), err.getvalue(), sent

    def test_inputs_within_capacity_keep_existing_output(self):
        for state in ("", "x" * CAPACITY):
            record = json.dumps({"id": "small", "state": state})
            chunked = self.run_cli(record, [answer(0.75, 0.25)])
            plain = self.run_cli(record, [answer(0.75, 0.25)], budget=None)
            self.assertEqual(chunked, plain)
            self.assertEqual(chunked[3], [state])
            self.assertEqual(json.loads(chunked[1]), {"id": "small", **answer(0.75, 0.25)})

    def test_one_byte_over_capacity_splits_without_overlap(self):
        state = "0123456789X"
        code, out, err, sent = self.run_cli(json.dumps({"id": 7, "state": state}),
                                            [answer(0.75, 0.25), answer(0.75, 0.25)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent, ["0123456789", "X"])
        result = json.loads(out)
        self.assertEqual(result["id"], 7)
        self.assertEqual([(c["index"], c["start"], c["end"], c["estimated_tokens"]) for c in result["chunks"]],
                         [(0, 0, 10, 10), (1, 10, 11, 1)])
        self.assertEqual(result["chunking"], {"token_budget": BUDGET, "state_capacity": CAPACITY,
                                              "estimator": "utf8-bytes", "overlap": 0, "count": 2})

    def test_unicode_never_splits_a_code_point(self):
        for state, expected in (("é" * 6, ["é" * 5, "é"]), ("😀" * 3, ["😀😀", "😀"]),
                                ("abcdefghi😀", ["abcdefghi", "😀"])):
            code, out, _, sent = self.run_cli(json.dumps({"id": state, "state": state}),
                                              [answer(0.5, 0.5)] * len(expected))
            self.assertEqual(code, 0)
            self.assertEqual(sent, expected)
            self.assertTrue(all(len(s.encode()) <= CAPACITY for s in sent))
            result = json.loads(out)
            self.assertEqual(result["id"], state)
            self.assertEqual("".join(state[c["start"]:c["end"]] for c in result["chunks"]), state)

    def test_mean_probability_choice_and_confidence_are_hand_computable(self):
        chunks = [answer(0.75, 0.25, "a"), answer(0.125, 0.875, "b")]
        code, out, err, _ = self.run_cli(json.dumps({"id": "r", "state": "x" * 20}), chunks)
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        # a = (0.75 + 0.125) / 2, b = (0.25 + 0.875) / 2
        self.assertEqual(result["answers"], {"route": {"type": "choice", "choice": "b", "confidence": 0.5625,
                                                       "probabilities": {"a": 0.4375, "b": 0.5625}}})
        self.assertEqual([c["response"] for c in result["chunks"]], chunks)
        self.assertEqual(result["aggregation"]["confidence"], "mean probability of chosen label")

    def test_winner_uses_exact_mean_not_rounded_sums(self):
        chunks = [answer(0.5, 0.5), answer(0.5, 0.5), answer(0.4999999999999999, 0.5000000000000001)]
        code, out, _, _ = self.run_cli(json.dumps({"id": 1, "state": "x" * 30}), chunks)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["answers"]["route"]["choice"], "b")

    def test_ties_go_to_criteria_order(self):
        for criteria, winner in (({"a": "A", "b": "B"}, "a"), ({"b": "B", "a": "A"}, "b")):
            questions = {"route": {"type": "choice", "criteria": criteria}}
            budget = jev.REQUEST_OVERHEAD_TOKENS + len(json.dumps(questions).encode()) + CAPACITY
            code, out, _, _ = self.run_cli(json.dumps({"id": 1, "state": "x" * 20}),
                                           [answer(0.75, 0.25), answer(0.25, 0.75)],
                                           budget=budget, questions=questions)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["answers"]["route"]["choice"], winner)
            self.assertEqual(json.loads(out)["answers"]["route"]["confidence"], 0.5)

    def test_failed_chunk_fails_record_and_stops_batch(self):
        error = urllib.error.HTTPError("http://x", 500, "private", {}, io.BytesIO(b"secret"))
        records = "\n".join(json.dumps({"id": i, "state": "x" * 20}) for i in (1, 2))
        code, out, err, sent = self.run_cli(records, [answer(0.75, 0.25)] * 3 + [error], command="batch")
        self.assertEqual(code, 1)
        self.assertEqual([json.loads(line)["id"] for line in out.splitlines()], [1])
        self.assertIn("line 2; chunk 2/2: System One HTTP 500; gave up after 1 attempt", err)
        self.assertNotIn("secret", err)
        self.assertEqual(len(sent), 4)

    def test_chunk_requests_retry_and_run_under_batch_concurrency(self):
        error = urllib.error.HTTPError("http://x", 503, "busy", {"Retry-After": "0"}, io.BytesIO(b""))
        with patch("jev.random.uniform", return_value=0):
            code, out, err, sent = self.run_cli(json.dumps({"id": 1, "state": "a" * 10 + "b" * 10}),
                                                [answer(0.75, 0.25), error, answer(0.125, 0.875)], extra=["--retries", "1"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent, ["a" * 10, "b" * 10, "b" * 10])
        self.assertEqual(json.loads(out)["answers"]["route"]["choice"], "b")
        records = "\n".join(json.dumps({"id": i, "state": "x" * 20}) for i in range(4))
        code, out, err, sent = self.run_cli(records, [answer(0.5, 0.5)] * 8, command="batch", extra=["--concurrency", "3"])
        self.assertEqual((code, err, len(sent)), (0, "", 8))
        self.assertEqual([json.loads(line)["id"] for line in out.splitlines()], [0, 1, 2, 3])

    def test_dry_run_estimates_every_chunk(self):
        code, out, err, sent = self.run_cli(json.dumps({"id": 1, "state": "x" * 25}), [], extra=["--dry-run"])
        self.assertEqual((code, err, sent), (0, "", []))
        row = json.loads(out)["records"][0]
        single = jev.estimate({"id": None, "state": "x" * 10}, QUESTIONS, jev.Backend(jev.DEFAULT_BASE_URL, jev.DEFAULT_MODEL))
        self.assertEqual(row["chunks"], 3)
        self.assertEqual(row["question_tokens_estimate"], 3 * single["question_tokens_estimate"])

    def test_bad_chunk_answer_stops_before_next_chunk(self):
        bad = answer(0.75, 0.25)
        bad["answers"]["route"]["probabilities"] = {"a": 1.0}
        for first, message in ((bad, "criteria labels"), ({"answers": {}}, "criteria labels"),
                               ({"answers": {"route": {"probabilities": {"a": True, "b": 0}}}}, "criteria labels")):
            code, out, err, sent = self.run_cli(json.dumps({"id": 1, "state": "x" * 20}), [first, answer(0.75, 0.25)])
            self.assertEqual((code, out, len(sent)), (1, "", 1))
            self.assertIn("chunk 1/2: chunk answer probabilities must cover exactly the " + message, err)

    def test_unparseable_chunk_response_names_the_chunk(self):
        def http(request, timeout):
            return contextlib.closing(io.BytesIO(b"not json"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            err = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"id": 1, "state": "x" * 20}))), \
                 patch("urllib.request.urlopen", side_effect=http), contextlib.redirect_stderr(err):
                code = jev.main(["classify", "--questions", str(path), "--chunk-tokens", str(BUDGET), "--retries", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "jev: chunk 1/2: System One returned invalid JSON\n")

    def test_cache_reuses_chunks_and_never_stores_bad_labels(self):
        bad = answer(0.75, 0.25)
        bad["answers"]["route"]["probabilities"] = {"a": 1.0}
        record = json.dumps({"id": 1, "state": "a" * 10 + "b" * 10})
        with tempfile.TemporaryDirectory() as cache:
            first = self.run_cli(record, [answer(0.75, 0.25), bad], extra=["--cache", cache])
            self.assertEqual((first[0], len(first[3])), (1, 2))
            second = self.run_cli(record, [answer(0.125, 0.875)], extra=["--cache", cache])
            self.assertEqual((second[0], second[2]), (0, ""))
            self.assertEqual(len(second[3]), 1, "only the chunk that failed validation is sent again")
            third = self.run_cli(record, [], extra=["--cache", cache])
            self.assertEqual((third[0], third[1], third[3]), (0, second[1], []))

    def test_unsupported_inputs_rejected_before_any_request(self):
        score = {"route": {"type": "score", "criteria": ["low", "high"]}}
        cases = (
            ({"id": 1, "state": {"text": "x"}}, BUDGET, QUESTIONS, "string state"),
            ({"id": 1, "state": ["x"]}, BUDGET, QUESTIONS, "string state"),
            ({"id": 1, "state": "x", "assets": {}}, BUDGET, QUESTIONS, "without assets"),
            ({"id": 1, "state": "x"}, BUDGET, score, "only choice questions"),
            ({"id": 1, "state": "x"}, 0, QUESTIONS, "between 1 and 32000"),
            ({"id": 1, "state": "x"}, 32001, QUESTIONS, "between 1 and 32000"),
            ({"id": 1, "state": "x"}, BUDGET - CAPACITY + 3, QUESTIONS, "no room for state"),
        )
        for record, budget, questions, message in cases:
            code, out, err, sent = self.run_cli(json.dumps(record), [], budget=budget, questions=questions)
            self.assertEqual((code, out, sent), (1, "", []), message)
            self.assertIn(message, err)


if __name__ == "__main__":
    unittest.main()
