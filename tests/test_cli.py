import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import email.utils
import io
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import threading
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

import jev

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = {"route": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}
RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {"route": {"type": "choice", "choice": "a", "confidence": 0.75,
                          "probabilities": {"a": 0.875, "b": 0.125}}},
    "usage": {"input_tokens": 100, "output_tokens": 20},
    "future_field": [1, 2],
}


class CliTests(unittest.TestCase):
    def run_cli(self, data, command="batch", response=RESPONSE, extra=(), env=None, questions=QUESTIONS):
        sent = []

        def http(request, timeout):
            sent.append((request, timeout))
            body = response(request) if callable(response) else response
            if isinstance(body, Exception):
                raise body
            return contextlib.closing(io.BytesIO(json.dumps(body).encode()))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(questions if isinstance(questions, str) else json.dumps(questions), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-only", **(env or {})}, clear=True), \
                 patch("sys.stdin", io.StringIO(data)), \
                 patch("urllib.request.urlopen", side_effect=http), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main([command, "--questions", str(path), *extra])
            return code, out.getvalue(), err.getvalue(), sent

    def test_batch_preserves_ids_probabilities_metadata_and_request(self):
        records = [{"id": "session-α", "state": {"text": "hello"}},
                   {"id": 42, "state": ["world"]}]
        code, out, err, sent = self.run_cli("\n".join(map(json.dumps, records)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([json.loads(x) for x in out.splitlines()],
                         [{"id": r["id"], **RESPONSE} for r in records])
        for (req, timeout), record in zip(sent, records):
            self.assertEqual(req.full_url, "https://opencode.ai/zen/v1/systemone")
            self.assertEqual(req.get_method(), "POST")
            self.assertIsNone(req.get_header("Authorization"))
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(req.get_header("User-agent"), "jev-cli/0.1")
            self.assertEqual(json.loads(req.data), {"model": "jev-1.13-free", "state": record["state"], "questions": QUESTIONS})
            self.assertEqual(timeout, 60)

    def test_default_never_reads_typesafe_credentials(self):
        with patch("jev.api_key") as key, patch("subprocess.run") as keychain:
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}')
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(sent[0][0].get_header("Authorization"))
        key.assert_not_called()
        keychain.assert_not_called()

    def test_env_and_flag_precedence(self):
        env = {"JEV_BASE_URL": "http://localhost:1234/custom/", "JEV_MODEL": "env-model"}
        for extra, url, model in (
            ([], "http://localhost:1234/custom/systemone", "env-model"),
            (["--base-url", "https://opencode.ai/zen/v1", "--model", "flag-model"],
             "https://opencode.ai/zen/v1/systemone", "flag-model"),
        ):
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}', extra=extra, env=env)
            self.assertEqual((code, err), (0, ""))
            request = sent[0][0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(json.loads(request.data)["model"], model)
            self.assertIsNone(request.get_header("Authorization"))

    def test_typesafe_credentials_on_selected_origin_only(self):
        for url in ("https://api.typesafe.ai/v1", "https://api.typesafe.ai.evil.example/v1",
                    "http://api.typesafe.ai/v1", "https://opencode.ai/zen/v1"):
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}', extra=["--base-url", url])
            self.assertEqual((code, err), (0, ""))
            expected = "Bearer test-only" if url == "https://api.typesafe.ai/v1" else None
            self.assertEqual(sent[0][0].get_header("Authorization"), expected)
        with patch("sys.platform", "darwin"), patch("subprocess.run", return_value=
                subprocess.CompletedProcess([], 0, "keychain-key\n")) as keychain:
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}',
                extra=["--base-url", "https://api.typesafe.ai/v1"], env={"TYPESAFE_API_KEY": ""})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent[0][0].get_header("Authorization"), "Bearer keychain-key")
        self.assertEqual(keychain.call_count, 1)

    def test_box_and_assets_backend_support(self):
        for extra in ([], ["--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.13.0"]):
            with patch.dict(QUESTIONS["route"], {"type": "box"}):
                code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}', extra=extra)
            self.assertEqual((code, out, sent), (1, "", []))
            self.assertIn("free Jev 1.13 is unsupported", err)
        record = '{"id":1,"state":"ok","assets":{}}'
        self.assertEqual(self.run_cli(record)[0], 1)
        with patch.dict(QUESTIONS["route"], {"type": "box"}):
            code, _, err, sent = self.run_cli(record,
                extra=["--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent[0][0].get_header("Typesafe-danger-zone-images"), "true")
        self.assertEqual(json.loads(sent[0][0].data)["assets"], {})

    def test_single(self):
        code, out, err, sent = self.run_cli('{"id":null,"state":"text"}', "classify")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"id": None, **RESPONSE})
        self.assertEqual(len(sent), 1)

    def test_bad_second_record_stops_with_prior_output(self):
        code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}\n{"state":"bad"}\n')
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("line 2", err)
        self.assertEqual(len(sent), 1)

    def http_error(self, status, body=b"secret", headers=None):
        return urllib.error.HTTPError(jev.DEFAULT_BASE_URL + "/systemone", status, "private",
                                      headers or {}, io.BytesIO(body))

    def test_permanent_http_errors_fail_once_without_echoing_body(self):
        # The unavailable phrase never makes auth or validation failures retryable.
        for status, body in ((400, b"secret"), (404, b"secret"), (401, b"secret Endpoint is unavailable"),
                             (403, b"secret Endpoint is unavailable"), (422, b"secret Endpoint is unavailable")):
            code, out, err, sent = self.run_cli('{"id":1,"state":"private"}', "classify", self.http_error(status, body))
            self.assertEqual((code, out), (1, ""))
            self.assertIn(f"HTTP {status}; not retryable", err)
            self.assertNotIn("secret", err)
            self.assertNotIn("private", err)
            self.assertEqual(len(sent), 1)

    def test_transient_errors_retry_up_to_the_limit(self):
        cases = [lambda _: self.http_error(429), lambda _: self.http_error(503), lambda _: self.http_error(529),
                 lambda _: self.http_error(404, b'secret {"error":{"message":"Endpoint is unavailable"}}'),
                 lambda _: urllib.error.URLError("refused"), lambda _: TimeoutError()]
        with patch("random.uniform", return_value=0):
            for error in cases:
                for retries in (0, 2):
                    code, out, err, sent = self.run_cli('{"id":1,"state":"private"}', "classify", error,
                                                        extra=["--retries", str(retries)])
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(len(sent), retries + 1)
                    self.assertIn(f"gave up after {retries + 1} attempt", err)
                    self.assertNotIn("secret", err)

    def test_exhausted_batch_line_keeps_flushed_prefix(self):
        calls = []

        def http(request, timeout):
            calls.append(json.loads(request.data)["state"])
            if len(calls) > 1:
                raise self.http_error(503)
            return contextlib.closing(io.BytesIO(json.dumps(RESPONSE).encode()))

        with tempfile.TemporaryDirectory() as tmp, patch("random.uniform", return_value=0):
            questions = Path(tmp) / "q.json"
            questions.write_text(json.dumps(QUESTIONS))
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdin", io.StringIO('{"id":1,"state":"a"}\n{"id":2,"state":"b"}\n{"id":3,"state":"c"}\n')), \
                 patch("urllib.request.urlopen", side_effect=http), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main(["batch", "--questions", str(questions), "--retries", "2"])
        self.assertEqual(code, 1)
        self.assertEqual([json.loads(x)["id"] for x in out.getvalue().splitlines()], [1])
        self.assertEqual(err.getvalue(), "jev: batch stopped at line 2; System One HTTP 503; gave up after 3 attempts\n")
        self.assertEqual(calls, ["a", "b", "b", "b"])

    def test_concurrency_bound_and_input_order(self):
        lock, active, peak, calls = threading.Lock(), [0], [0], []

        def http(request, timeout):
            state = json.loads(request.data)["state"][0]
            with lock:
                calls.append(state)
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.02 * (5 - state % 5))
                return contextlib.closing(io.BytesIO(json.dumps({"answers": {}, "n": state}).encode()))
            finally:
                with lock:
                    active[0] -= 1

        records = "".join(json.dumps({"id": i, "state": [i]}) + "\n" for i in range(12))
        with tempfile.TemporaryDirectory() as tmp:
            questions = Path(tmp) / "q.json"
            questions.write_text(json.dumps(QUESTIONS))
            for concurrency in (1, 3):
                out = io.StringIO()
                peak[0] = 0
                with patch("sys.stdin", io.StringIO(records)), patch("urllib.request.urlopen", side_effect=http), \
                     contextlib.redirect_stdout(out):
                    code = jev.main(["batch", "--questions", str(questions), "--concurrency", str(concurrency)])
                self.assertEqual(code, 0)
                self.assertEqual([json.loads(x) for x in out.getvalue().splitlines()],
                                 [{"id": i, "answers": {}, "n": i} for i in range(12)])
                self.assertEqual(peak[0], concurrency)

    def test_invalid_retry_and_concurrency_options_fail_before_sending(self):
        for extra, message in ((["--concurrency", "0"], "concurrency must be between 1 and 32"),
                               (["--concurrency", "33"], "concurrency must be between 1 and 32"),
                               (["--retries", "-1"], "retries must be between 0 and 10"),
                               (["--retries", "11"], "retries must be between 0 and 10")):
            code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}', extra=extra)
            self.assertEqual((code, out, sent), (1, "", []))
            self.assertIn(message, err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            jev.main(["batch", "--questions", "q.json", "--concurrency", "two"])
        self.assertEqual(exit_.exception.code, 2)
        for command in ("eval", "summarize"):
            for extra in (["--retries", "1"], ["--concurrency", "2"]):
                code, out, err, sent = self.run_cli("", command, extra=extra)
                self.assertEqual((code, out, sent), (1, "", []))
                self.assertIn(f"{command} sends no requests; --retries and --concurrency are for classify and batch", err)
        code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}\n', extra=["--dry-run", "--concurrency", "2", "--retries", "0"])
        self.assertEqual((code, err, sent), (0, "", []))
        self.assertTrue(json.loads(out)["dry_run"])

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(jev.retry_after_seconds("7"), 7)
        self.assertIsNone(jev.retry_after_seconds("soon"))
        self.assertIsNone(jev.retry_after_seconds("-1"))
        self.assertEqual(jev.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0)
        later = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        self.assertTrue(80 < jev.retry_after_seconds(later) <= 90)
        self.assertEqual(jev.backoff_seconds(1, 120), jev.BACKOFF_CAP_SECONDS)
        with patch("random.uniform", side_effect=lambda low, high: high):
            self.assertEqual([jev.backoff_seconds(n, None) for n in (1, 2, 3, 8)], [0.5, 1, 2, jev.BACKOFF_CAP_SECONDS])

    def test_invalid_json_state_and_timeout(self):
        for raw in ('secret malformed', '{"id":1,"state":null}', '{"id":1,"state":[NaN]}', '{"id":1,"state":[1e400]}'):
            code, out, err, sent = self.run_cli(raw)
            self.assertEqual((code, out, sent), (1, "", []))
            self.assertNotIn("secret", err)
        self.assertEqual(self.run_cli('{}', extra=['--timeout', 'nan'])[0], 1)

    def test_response_collision_and_nonobject(self):
        for response in ([], {"answers": {}, "id": "collision"}):
            code, out, _, _ = self.run_cli('{"id":1,"state":"ok"}', response=response)
            self.assertEqual((code, out), (1, ""))

    def test_missing_key_and_keychain_precedence(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch("sys.platform", "linux"):
            with self.assertRaisesRegex(jev.JevError, "missing TYPESAFE"):
                jev.api_key()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}), patch("subprocess.run") as run:
            self.assertEqual(jev.api_key(), "env-key")
            run.assert_not_called()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "keychain-key\n")) as run:
            self.assertEqual(jev.api_key(), "keychain-key")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "typesafe", "-w"])

    def test_process_cli_against_mock_http_server(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(RESPONSE).encode())

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as tmp:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                path = Path(tmp) / "q.json"
                path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
                base_url = f"http://127.0.0.1:{server.server_port}/v1/"
                run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "classify", "--questions", str(path),
                                      "--base-url", base_url, "--model", "jev-1.13-free"],
                                     cwd=ROOT, env={"PATH": os.environ["PATH"], "TYPESAFE_API_KEY": "test-only"},
                                     input='{"id":"process","state":"hello"}', capture_output=True, text=True, timeout=10)
                self.assertEqual((run.returncode, run.stderr), (0, ""))
                self.assertEqual(json.loads(run.stdout), {"id": "process", **RESPONSE})
                self.assertEqual(received, [("/v1/systemone", None,
                                            {"model": "jev-1.13-free", "state": "hello", "questions": QUESTIONS})])
            finally:
                server.shutdown()
                worker.join()

    def test_process_batch_retries_429_then_503_in_input_order(self):
        script = [(429, {"Retry-After": "1"}), (503, {})]
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                state = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["state"]
                status, headers = script.pop(0) if script else (200, {})
                received.append((state, status))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(json.dumps(RESPONSE).encode() if status == 200 else b"secret upstream body")

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as tmp:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                path = Path(tmp) / "q.json"
                path.write_text(json.dumps(QUESTIONS))
                started = time.monotonic()
                run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--questions", str(path),
                                      "--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                                      "--concurrency", "2", "--retries", "2"],
                                     cwd=ROOT, env={"PATH": os.environ["PATH"]},
                                     input="".join(json.dumps({"id": i, "state": f"s{i}"}) + "\n" for i in range(3)),
                                     capture_output=True, text=True, timeout=20)
                elapsed = time.monotonic() - started
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        self.assertEqual([json.loads(x) for x in run.stdout.splitlines()], [{"id": i, **RESPONSE} for i in range(3)])
        self.assertEqual([status for _, status in received], [429, 503, 200, 200, 200])
        self.assertEqual(sorted(state for state, status in received if status == 200), ["s0", "s1", "s2"])
        self.assertGreaterEqual(elapsed, 1)

    def test_malformed_question_type_is_clean_error(self):
        with patch.dict(QUESTIONS["route"], {"type": []}):
            code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}')
        self.assertEqual((code, out, sent), (1, "", []))
        self.assertIn("supported question types", err)

    def test_versioned_example_sends_same_questions_as_legacy(self):
        legacy = json.loads((ROOT / "examples/failure-patterns.json").read_text())
        versioned = json.loads((ROOT / "examples/failure-patterns.v1.json").read_text())
        self.assertEqual(versioned, {"schema_version": 1, "questions": legacy})
        bodies = []
        for name in ("failure-patterns.json", "failure-patterns.v1.json"):
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}',
                                              questions=(ROOT / "examples" / name).read_text())
            self.assertEqual((code, err), (0, ""))
            bodies.append(json.loads(sent[0][0].data))
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["questions"], legacy)

    def test_bad_question_documents_fail_before_http(self):
        private = "PRIVATE-question-text"
        question = {"type": "noul", "instructions": private}
        cases = {
            json.dumps({"schema_version": 2, "questions": {"q": question}}): "unsupported questions schema_version",
            json.dumps({"schema_version": "1", "questions": {"q": question}}): "unsupported questions schema_version",
            json.dumps({"schema_version": True, "questions": {"q": question}}): "unsupported questions schema_version",
            json.dumps({"schema_version": 1}): "exactly schema_version and questions",
            json.dumps({"schema_version": 1, "questions": {"q": question}, "note": private}): "exactly schema_version and questions",
            json.dumps({"schema_version": 1, "questions": {}}): "nonempty API question mapping",
            json.dumps({"schema_version": 1, "questions": [question]}): "nonempty API question mapping",
            json.dumps({"schema_version": 1, "questions": {"q": {"type": "x", "note": private}}}): "supported question types",
            "{}": "nonempty API question mapping",
            "[]": "nonempty API question mapping",
            '{"q": ' + json.dumps(question)[:-1]: "readable UTF-8 JSON",
            '{"q": {"type": "noul", "instructions": 1e999}}': "readable UTF-8 JSON",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}', questions=text)
                self.assertEqual((code, out, sent), (1, "", []))
                self.assertTrue(err.startswith("jev: "))
                self.assertIn(message, err)
                self.assertNotIn(private, err)

    def test_unsupported_version_message_lists_supported_versions(self):
        text = json.dumps({"schema_version": 3, "questions": QUESTIONS})
        with patch("jev.SCHEMA_VERSIONS", (1, 2)):
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}', questions=text)
        self.assertEqual((code, sent), (1, []))
        self.assertIn("supported: 1, 2,", err)
    def summarize(self, lines, *extra, questions=None):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "results.jsonl"
            raw = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
            source.write_text(raw, encoding="utf-8")
            args = [sys.executable, str(ROOT / "jev.py"), "summarize", "--input", str(source), *extra]
            if questions is not None:
                (Path(tmp) / "q.json").write_text(json.dumps(questions))
                args += ["--questions", str(Path(tmp) / "q.json")]
            run = subprocess.run(args, env={"PATH": os.environ["PATH"], "HOME": tmp, "PYTHONIOENCODING": "utf-8"},
                                 capture_output=True, text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
            self.assertEqual(source.read_text(encoding="utf-8"), raw)
            return run

    def test_summarize_counts_examples_and_confidence_bins(self):
        def result(id_, choice, **answer):
            return {"id": id_, "answers": {"route": {"type": "choice", "choice": choice, **answer,
                                                     "probabilities": {"a": 0.5, "b": 0.5}}}}
        lines = [result(1, "a", confidence=0), result("two", "a", confidence=1), result(None, "a", confidence=0.1),
                 result(4, "a", confidence=0.0999), result(5, "b", confidence=0.95), result(6, "a"),
                 result(7, "b", confidence="high"), result(8, "b", confidence=True), result(9, "b", confidence=1.5),
                 result(10, "b", confidence=-0.1)]
        run = self.summarize(lines, "--question", "route", questions=QUESTIONS)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["records"], 10)
        self.assertEqual(summary["counts"], {"a": 5, "b": 5})
        self.assertEqual(summary["examples"], {"a": [1, "two", None], "b": [5, 7, 8]})
        bins = summary["confidence"]["bins"]
        self.assertEqual(list(bins)[0], "0.0-0.1")
        self.assertEqual((bins["0.0-0.1"], bins["0.1-0.2"], bins["0.9-1.0"]), (2, 1, 2))
        self.assertEqual(sum(bins.values()), 5)
        self.assertEqual({k: summary["confidence"][k] for k in ("missing", "malformed", "out_of_range")},
                         {"missing": 1, "malformed": 2, "out_of_range": 2})
        self.assertEqual(summary["diagnostics"], ['line 7: confidence "high" is not a number',
                                                  "line 8: confidence true is not a number",
                                                  "line 9: confidence 1.5 is outside [0, 1]",
                                                  "line 10: confidence -0.1 is outside [0, 1]"])
        self.assertEqual(run.stderr.splitlines(), ["jev: " + d for d in summary["diagnostics"]])

    def test_summarize_selection_empty_input_and_unsupported_answers(self):
        both = {"id": 1, "answers": {"x": {"type": "choice", "choice": "a"}, "y": {"type": "choice", "choice": "b"}}}
        run = self.summarize([both])
        self.assertEqual((run.returncode, run.stdout), (1, ""))
        self.assertIn("select one question with --question; input has x, y", run.stderr)
        self.assertEqual(json.loads(self.summarize([both], "--question", "y").stdout)["counts"], {"b": 1})
        empty = json.loads(self.summarize([], questions=QUESTIONS).stdout)
        self.assertEqual((empty["question"], empty["records"], empty["counts"], empty["examples"]),
                         ("route", 0, {"a": 0, "b": 0}, {"a": [], "b": []}))
        self.assertEqual(sum(empty["confidence"]["bins"].values()), 0)
        for answer in ({"type": "score", "score": 1}, {"type": "noul", "noul": 0.5}, {"type": "box", "boxes": []}):
            run = self.summarize([{"id": 1, "answers": {"q": answer}}])
            self.assertEqual((run.returncode, run.stdout), (1, ""))
            self.assertIn("line 1; 'q' is not a Choice answer", run.stderr)
        run = self.summarize([], questions={"q": {"type": "noul"}})
        self.assertIn("Choice questions only", run.stderr)

    def test_summarize_seeds_inferred_question_and_keeps_unicode_separators(self):
        questions = {**QUESTIONS, "other": {"type": "noul"}}
        run = self.summarize([{"id": "a\u2028b", "answers": {"route": {"choice": "b"}}}], questions=questions)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual((summary["counts"], summary["examples"]), ({"a": 0, "b": 1}, {"a": [], "b": ["a\u2028b"]}))

    def test_summarize_seeds_labels_from_legacy_and_versioned_question_files(self):
        line = {"id": 1, "answers": {"failure_pattern": {"type": "choice", "choice": "fine", "confidence": 0.5}}}
        legacy = json.loads((ROOT / "examples/failure-patterns.json").read_text())
        counts = []
        for name in ("failure-patterns.json", "failure-patterns.v1.json"):
            run = self.summarize([line], "--questions", str(ROOT / "examples" / name))
            self.assertEqual(run.returncode, 0, run.stderr)
            counts.append(json.loads(run.stdout)["counts"])
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(list(counts[1]), list(legacy["failure_pattern"]["criteria"]))
        run = self.summarize([line], questions={"schema_version": 2, "questions": legacy})
        self.assertEqual((run.returncode, run.stdout), (1, ""))
        self.assertIn("unsupported questions schema_version", run.stderr)

    def test_summarize_reads_no_credentials_and_sends_nothing(self):
        line = json.dumps({"id": 1, "answers": {"route": {"type": "choice", "choice": "a", "confidence": 0.5}}})
        out = io.StringIO()
        with patch("jev.api_key") as key, patch("subprocess.run") as keychain, \
             patch("urllib.request.urlopen") as http, patch("sys.stdin", io.StringIO(line + "\n")), \
             contextlib.redirect_stdout(out):
            self.assertEqual(jev.main(["summarize", "--base-url", "https://api.typesafe.ai/v1"]), 0)
        key.assert_not_called()
        keychain.assert_not_called()
        http.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["confidence"]["bins"]["0.5-0.6"], 1)

    def test_summarize_bad_line_stops_with_line_number(self):
        with patch("sys.stdin", io.StringIO('{"id":1,"answers":{"q":{"choice":"a"}}}\nnot json\n')), \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(jev.main(["summarize", "--question", "q"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "jev: summarize stopped at line 2; invalid JSON record\n")

    def test_classify_and_batch_still_require_questions(self):
        for command in ("classify", "batch"):
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), command], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL)
            self.assertEqual(run.returncode, 2)
            self.assertIn("--questions is required", run.stderr)

    def test_cache_second_run_sends_nothing_and_reattaches_current_ids(self):
        with tempfile.TemporaryDirectory() as cache:
            records = [{"id": "a", "state": "one"}, {"id": "b", "state": ["two"]}]
            code, first, err, sent = self.run_cli("\n".join(map(json.dumps, records)), extra=["--cache", cache])
            self.assertEqual((code, err, len(sent)), (0, "", 2))
            renamed = [{**r, "id": f"new-{r['id']}"} for r in records]
            code, second, err, sent = self.run_cli("\n".join(map(json.dumps, renamed)), extra=["--cache", cache])
            self.assertEqual((code, err, sent), (0, "", []))
            self.assertEqual([json.loads(x) for x in second.splitlines()],
                             [{"id": r["id"], **RESPONSE} for r in renamed])
            self.assertEqual([{**json.loads(x), "id": None} for x in first.splitlines()],
                             [{**json.loads(x), "id": None} for x in second.splitlines()])
            code, out, err, sent = self.run_cli(json.dumps(records[0]), "classify", extra=["--cache", cache])
            self.assertEqual((code, err, sent, json.loads(out)), (0, "", [], {"id": "a", **RESPONSE}))

    def test_cache_resume_sends_only_missing_records(self):
        records = [{"id": i, "state": f"s{i}"} for i in range(4)]
        data = "\n".join(map(json.dumps, records))

        def fail_third(request):
            return urllib.error.HTTPError("x", 503, "busy", {}, io.BytesIO(b"")) if json.loads(request.data)["state"] == "s2" else RESPONSE

        with tempfile.TemporaryDirectory() as cache:
            with patch("random.uniform", return_value=0):
                code, out, err, sent = self.run_cli(data, response=fail_third, extra=["--cache", cache, "--retries", "1"])
            self.assertEqual((code, len(out.splitlines())), (1, 2))
            self.assertEqual([json.loads(r.data)["state"] for r, _ in sent], ["s0", "s1", "s2", "s2"])
            self.assertIn("line 3", err)
            code, out, err, sent = self.run_cli(data, extra=["--cache", cache])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual([json.loads(r.data)["state"] for r, _ in sent], ["s2", "s3"])
            self.assertEqual([json.loads(x)["id"] for x in out.splitlines()], [0, 1, 2, 3])

    def test_cache_misses_on_any_semantic_change(self):
        typesafe = ["--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"]
        base = '{"id":1,"state":"ok"}'
        with tempfile.TemporaryDirectory() as cache:
            for record, extra in ((base, []), (base, typesafe), ('{"id":1,"state":"ok","assets":{}}', typesafe)):
                self.assertEqual(len(self.run_cli(record, extra=["--cache", cache, *extra])[3]), 1)
            for record, extra in (
                (base, ["--base-url", "http://localhost:9/v1"]),
                (base, ["--model", "other-model"]),
                ('{"id":1,"state":"changed"}', []),
                ('{"id":1,"state":"ok","assets":{"img":{"type":"image"}}}', typesafe),
            ):
                self.assertEqual(len(self.run_cli(record, extra=["--cache", cache, *extra])[3]), 1, (record, extra))
            with patch.dict(QUESTIONS["route"], {"instructions": "changed"}):
                self.assertEqual(len(self.run_cli(base, extra=["--cache", cache])[3]), 1)
            self.assertEqual(self.run_cli(base, extra=["--cache", cache])[3], [])

    def test_cache_failures_never_hit_and_corrupt_entries_refetch(self):
        record = '{"id":1,"state":"ok"}'
        failures = (urllib.error.HTTPError("x", 500, "err", {}, io.BytesIO(b"")), [], {"answers": []},
                    {"answers": {}, "id": "collision"})
        with tempfile.TemporaryDirectory() as cache:
            for response in failures:
                self.assertEqual(self.run_cli(record, response=response, extra=["--cache", cache])[0], 1)
            self.assertEqual([p for p in Path(cache).rglob("*") if p.is_file()], [])
            self.assertEqual(len(self.run_cli(record, extra=["--cache", cache])[3]), 1)
            [entry] = Path(cache).rglob("*.json")
            self.assertEqual(entry.stat().st_mode & 0o777, 0o600)
            stored = json.loads(entry.read_text())
            for corrupt in ("{trunc", "[]", json.dumps({**stored, "version": 2}), json.dumps({**stored, "key": "x"}),
                            json.dumps({**stored, "response": {"answers": {}, "id": 1}}), "\udcff"):
                entry.write_text(corrupt, errors="surrogateescape")
                code, out, err, sent = self.run_cli(record, extra=["--cache", cache])
                self.assertEqual((code, err, len(sent), json.loads(out)), (0, "", 1, {"id": 1, **RESPONSE}))
                self.assertEqual(json.loads(entry.read_text()), stored)

    def test_unwritable_cache_warns_and_keeps_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("")
            code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}', extra=["--cache", str(blocker)])
        self.assertEqual((code, len(sent), json.loads(out)), (0, 1, {"id": 1, **RESPONSE}))
        self.assertIn("could not write cache entry", err)

    def dry_run(self, data, *extra, command="batch"):
        with patch("jev.api_key") as key, patch("subprocess.run") as keychain, patch("sys.platform", "darwin"):
            code, out, err, sent = self.run_cli(data, command, extra=["--dry-run", *extra],
                                                env={"TYPESAFE_API_KEY": ""})
        self.assertEqual(sent, [])
        key.assert_not_called()
        keychain.assert_not_called()
        return code, out, err

    def test_dry_run_typesafe_cost_without_credentials_or_http(self):
        code, out, err = self.dry_run('{"id":"a","state":"ok"}\n{"id":"b","state":"okay"}\n',
                                      "--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        bodies = [json.dumps({"model": "jev-1.14.0", "state": state, "questions": QUESTIONS}) for state in ("ok", "okay")]
        tokens = [-(-len(body) // 4) for body in bodies]
        self.assertEqual([(r["line"], r["id"], r["input_tokens_estimate"]) for r in report["records"]],
                         [(1, "a", tokens[0]), (2, "b", tokens[1])])
        self.assertEqual(report["total_input_tokens_estimate"], sum(tokens))
        self.assertEqual(Decimal(report["estimated_cost_usd"]), Decimal("0.042") * sum(tokens) / 1000000)
        self.assertNotEqual(Decimal(report["estimated_cost_usd"]), 0)
        self.assertEqual(report["backend"], {"endpoint": "https://api.typesafe.ai/v1/systemone", "model": "jev-1.14.0"})
        self.assertEqual((report["pricing"]["status"], report["pricing"]["currency"], report["pricing"]["date"],
                          report["pricing"]["input_usd_per_million_tokens"], report["pricing"]["output_usd_per_million_tokens"]),
                         ("priced", "USD", "2026-11-14", "0.042", "0"))
        self.assertIn("not a tokenizer count", report["token_method"])

    def test_dry_run_free_default_costs_zero(self):
        code, out, err = self.dry_run('{"id":1,"state":"hello"}', command="classify")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual((report["pricing"]["status"], report["estimated_cost_usd"]), ("free", "0"))
        self.assertEqual(report["backend"]["model"], "jev-1.13-free")
        self.assertGreater(report["total_input_tokens_estimate"], 0)

    def test_dry_run_unknown_price_is_null(self):
        for base_url, model in (("https://api.typesafe.ai/v1", "mystery-model"), ("http://localhost:9/v1", "jev-1.14.0"),
                                ("http://api.typesafe.ai/v1", "jev-1.14.0")):
            code, out, err = self.dry_run('{"id":1,"state":"ok"}', "--base-url", base_url, "--model", model)
            self.assertEqual((code, err), (0, ""))
            report = json.loads(out)
            self.assertEqual((report["pricing"]["status"], report["pricing"]["input_usd_per_million_tokens"],
                              report["pricing"]["source"], report["estimated_cost_usd"]), ("unknown", None, None, None))
            self.assertEqual(report["total_input_tokens_estimate"], report["records"][0]["input_tokens_estimate"])

    def test_dry_run_measures_the_exact_unicode_bytes_sent(self):
        record = '{"id":"u","state":"日本語🙂 refund ünï"}'
        _, _, _, sent = self.run_cli(record, "classify")
        body = sent[0][0].data
        code, out, _ = self.dry_run(record, command="classify")
        self.assertEqual(code, 0)
        row = json.loads(out)["records"][0]
        self.assertEqual(row["request_bytes"], len(body))
        self.assertEqual(row["input_tokens_estimate"], -(-len(body) // 4))
        self.assertIn(b"\\u65e5", body)

    def test_dry_run_more_questions_raise_overhead(self):
        one = json.loads(self.dry_run('{"id":1,"state":"ok"}')[1])["records"][0]
        self.assertEqual(one["question_tokens_estimate"], -(-len(json.dumps(QUESTIONS)) // 4))
        with patch.dict(QUESTIONS, {"billing": {"type": "noul", "instructions": "Is this about billing?"}}):
            two = json.loads(self.dry_run('{"id":1,"state":"ok"}')[1])["records"][0]
        self.assertGreater(two["question_tokens_estimate"], one["question_tokens_estimate"])
        self.assertGreater(two["input_tokens_estimate"], one["input_tokens_estimate"])
        self.assertEqual(two["state_tokens_estimate"], one["state_tokens_estimate"])

    def test_dry_run_assets_make_cost_unknown(self):
        extra = ("--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0")
        plain = json.loads(self.dry_run('{"id":1,"state":"ok"}', *extra)[1])
        code, out, _ = self.dry_run('{"id":1,"state":"ok","assets":{"img":{"data_base64":"AAAA"}}}', *extra)
        report = json.loads(out)
        self.assertEqual((code, report["estimated_cost_usd"]), (0, None))
        self.assertIs(report["records"][0]["assets_not_estimated"], True)
        self.assertEqual(report["total_input_tokens_estimate"], plain["total_input_tokens_estimate"])
        self.assertEqual(report["records"][0]["request_bytes"] - plain["records"][0]["request_bytes"],
                         len(', "assets": {"img": {"data_base64": "AAAA"}}'))
        self.assertEqual(self.dry_run('{"id":1,"state":"ok","assets":{}}')[0], 1)

    def test_dry_run_invalid_line_prints_no_report(self):
        for bad in ('{"state":"bad"}', "not json"):
            code, out, err = self.dry_run('{"id":1,"state":"ok"}\n' + bad + "\n")
            self.assertEqual((code, out), (1, ""))
            self.assertIn("jev: batch stopped at line 2;", err)
        code, out, err = self.dry_run('{"id":1}', command="classify")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("record must be an object with id and state", err)

    def test_dry_run_is_deterministic(self):
        data = "\n".join(json.dumps({"id": i, "state": {"text": "x" * i, "note": "é"}}) for i in range(5))
        first, second = self.dry_run(data)[1], self.dry_run(data)[1]
        self.assertEqual(first, second)
        report = json.loads(first)
        self.assertEqual(report["total_input_tokens_estimate"],
                         sum(r["input_tokens_estimate"] for r in report["records"]))

    def test_dry_run_ignores_cache(self):
        records = '{"id":"a","state":"one"}\n{"id":"b","state":"two"}\n'
        plain = self.dry_run(records)[1]
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            self.assertEqual(self.dry_run(records, "--cache", str(empty))[1], plain)
            self.assertFalse(empty.exists())
            warm = Path(tmp) / "warm"
            self.assertEqual(len(self.run_cli(records, extra=["--cache", str(warm)])[3]), 2)
            before = {p: p.read_bytes() for p in warm.rglob("*") if p.is_file()}
            self.assertEqual(self.dry_run(records, "--cache", str(warm))[1], plain)
            self.assertEqual({p: p.read_bytes() for p in warm.rglob("*") if p.is_file()}, before)

    def test_dry_run_process_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(QUESTIONS))
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--dry-run", "--questions", str(path),
                                  "--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"],
                                 cwd=ROOT, env={"PATH": os.environ["PATH"]}, input='{"id":"p","state":"hello"}\n',
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        report = json.loads(run.stdout)
        self.assertEqual(report["records"][0]["id"], "p")
        self.assertNotEqual(Decimal(report["estimated_cost_usd"]), 0)

    def test_help_as_process(self):
        result = subprocess.run([sys.executable, str(ROOT / "jev.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("batch", result.stdout)
        self.assertIn("sends nothing and reads no credentials", " ".join(result.stdout.split()))


if __name__ == "__main__":
    unittest.main()
