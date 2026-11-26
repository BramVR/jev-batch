import argparse
import contextlib
from decimal import Decimal
import io
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import threading
import json
import os
import re
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


def leaf_commands(parser, path=()):
    """Every runnable command path, nested ones included, such as ("ask", "choice"), mapped to its parser."""
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {path: parser}
    return {leaf: sub for name, child in subs[0].choices.items()
            for leaf, sub in leaf_commands(child, (*path, name)).items()}


class CliTests(unittest.TestCase):
    def run_argv(self, argv, data="", response=RESPONSE, env=None):
        sent = []

        def http(request, timeout):
            sent.append((request, timeout))
            body = response(request) if callable(response) else response
            if isinstance(body, Exception):
                raise body
            return contextlib.closing(io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()))

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-only", **(env or {})}, clear=True), \
             patch("sys.stdin", io.StringIO(data)), \
             patch("urllib.request.urlopen", side_effect=http), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = jev.main(argv)
        return code, out.getvalue(), err.getvalue(), sent

    def run_cli(self, data, command="batch", response=RESPONSE, extra=(), env=None, questions=QUESTIONS):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(questions if isinstance(questions, str) else json.dumps(questions), encoding="utf-8")
            return self.run_argv([command, "--questions", str(path), *extra], data, response, env)

    def test_batch_preserves_ids_probabilities_metadata_and_request(self):
        records = [{"id": "session-α", "state": {"text": "hello"}},
                   {"id": 42, "state": ["world"]}]
        code, out, err, sent = self.run_cli("\n".join(map(json.dumps, records)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([json.loads(x) for x in io.StringIO(out)],
                         [{"id": r["id"], **RESPONSE} for r in records])
        for (req, timeout), record in zip(sent, records):
            self.assertEqual(req.full_url, "https://opencode.ai/zen/v1/systemone")
            self.assertEqual(req.get_method(), "POST")
            self.assertIsNone(req.get_header("Authorization"))
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(req.get_header("User-agent"), "jev-batch/" + jev.__version__)
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

    def test_unsendable_base_url_rejected_before_credentials(self):
        for url in ("https://api.typesafe.\nai/v1", "https://api.typesafe.ai\t/@evil.example/v1",
                    "https://api.typesafe.ai/v1 ", " https://api.typesafe.ai/v1", "https://api.typesafe.ai/v1\x7f",
                    "https://api.typesafe.ai/vé1"):
            for command, extra in (("classify", []), ("batch", []), ("classify", ["--dry-run"]), ("batch", ["--dry-run"])):
                with self.subTest(url=url, command=command, extra=extra), \
                     patch("jev.api_key") as key, patch("subprocess.run") as keychain:
                    code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}\n', command,
                                                        extra=["--base-url", url, "--model", "jev-1.14.0", *extra])
                    self.assertEqual((code, out, sent), (1, "", []))
                    self.assertEqual(err, "jev: base URL must be printable ASCII without spaces or control characters\n")
                    key.assert_not_called()
                    keychain.assert_not_called()

    def test_bad_api_response_blames_the_api_not_the_record(self):
        for body in (b"not json", b"\xff", b'{"answers":{"q":Infinity}}'):
            with self.subTest(body=body[:20]):
                replies = iter((RESPONSE, body))
                code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}\n{"id":2,"state":"ok"}\n',
                                                    response=lambda r: next(replies))
                self.assertEqual((code, len(sent)), (1, 2))
                self.assertEqual([json.loads(x)["id"] for x in io.StringIO(out)], [1])
                self.assertEqual(err, "jev: batch stopped at line 2; System One returned invalid JSON\n")
                code, out, err, _ = self.run_cli('{"id":1,"state":"ok"}', "classify", response=body)
                self.assertEqual((code, out, err), (1, "", "jev: System One returned invalid JSON\n"))

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
        self.assertEqual([json.loads(x)["id"] for x in io.StringIO(out)], [1])
        self.assertTrue(out.endswith("\n"))
        self.assertIn("line 2", err)
        self.assertEqual(len(sent), 1)

    def http_error(self, status, body=b"secret", headers=None):
        return urllib.error.HTTPError(jev.DEFAULT_BASE_URL + "/systemone", status, "private",
                                      headers or {}, io.BytesIO(body))

    def test_permanent_http_errors_fail_once_without_echoing_body(self):
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
            questions.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdin", io.StringIO('{"id":1,"state":"a"}\n{"id":2,"state":"b"}\n{"id":3,"state":"c"}\n')), \
                 patch("urllib.request.urlopen", side_effect=http), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main(["batch", "--questions", str(questions), "--retries", "2"])
        self.assertEqual(code, 1)
        self.assertEqual([json.loads(x)["id"] for x in io.StringIO(out.getvalue())], [1])
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
            questions.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            for concurrency in (1, 3):
                out = io.StringIO()
                peak[0] = 0
                with patch("sys.stdin", io.StringIO(records)), patch("urllib.request.urlopen", side_effect=http), \
                     contextlib.redirect_stdout(out):
                    code = jev.main(["batch", "--questions", str(questions), "--concurrency", str(concurrency)])
                self.assertEqual(code, 0)
                self.assertEqual([json.loads(x) for x in io.StringIO(out.getvalue())],
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
        code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}\n', extra=["--dry-run", "--concurrency", "2", "--retries", "0"])
        self.assertEqual((code, err, sent), (0, "", []))
        self.assertTrue(json.loads(out)["dry_run"])

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
                                     input='{"id":"process","state":"hello"}', capture_output=True, encoding="utf-8", timeout=10)
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
                path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
                started = time.monotonic()
                run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--questions", str(path),
                                      "--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                                      "--concurrency", "2", "--retries", "2"],
                                     cwd=ROOT, env={"PATH": os.environ["PATH"]},
                                     input="".join(json.dumps({"id": i, "state": f"s{i}"}) + "\n" for i in range(3)),
                                     capture_output=True, encoding="utf-8", timeout=20)
                elapsed = time.monotonic() - started
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        self.assertEqual([json.loads(x) for x in io.StringIO(run.stdout)], [{"id": i, **RESPONSE} for i in range(3)])
        self.assertEqual([status for _, status in received], [429, 503, 200, 200, 200])
        self.assertEqual(sorted(state for state, status in received if status == 200), ["s0", "s1", "s2"])
        self.assertGreaterEqual(elapsed, 1)

    def test_malformed_question_type_is_clean_error(self):
        with patch.dict(QUESTIONS["route"], {"type": []}):
            code, out, err, sent = self.run_cli('{"id":1,"state":"ok"}')
        self.assertEqual((code, out, sent), (1, "", []))
        self.assertIn("supported question types", err)

    def test_versioned_example_sends_same_questions_as_legacy(self):
        legacy = json.loads((ROOT / "examples/failure-patterns.json").read_text(encoding="utf-8"))
        versioned = json.loads((ROOT / "examples/failure-patterns.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(versioned, {"schema_version": 1, "questions": legacy})
        bodies = []
        for name in ("failure-patterns.json", "failure-patterns.v1.json"):
            code, _, err, sent = self.run_cli('{"id":1,"state":"ok"}',
                                              questions=(ROOT / "examples" / name).read_text(encoding="utf-8"))
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
                (Path(tmp) / "q.json").write_text(json.dumps(questions), encoding="utf-8")
                args += ["--questions", str(Path(tmp) / "q.json")]
            run = subprocess.run(args, env={"PATH": os.environ["PATH"], "HOME": tmp},
                                 capture_output=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
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
        self.assertEqual(run.stderr.removesuffix("\n").split("\n"), ["jev: " + d for d in summary["diagnostics"]])

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
        legacy = json.loads((ROOT / "examples/failure-patterns.json").read_text(encoding="utf-8"))
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
             patch.dict(os.environ, {"JEV_BASE_URL": "https://api.typesafe.ai/v1"}), contextlib.redirect_stdout(out):
            self.assertEqual(jev.main(["summarize"]), 0)
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
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), command], capture_output=True, encoding="utf-8",
                                 stdin=subprocess.DEVNULL)
            self.assertEqual(run.returncode, 2)
            self.assertIn("the following arguments are required: --questions", run.stderr)

    def test_each_command_rejects_flags_it_does_not_declare(self):
        commands = leaf_commands(jev.build_parser())
        declared = {name: {flag for action in sub._actions for flag in action.option_strings}
                    for name, sub in commands.items()}
        # Each required positional gets "x", each required flag "k=x", which also parses as -o KEY=DESCRIPTION.
        required = {name: [arg for action in sub._actions if action.required
                           for arg in ((action.option_strings[0], "k=x") if action.option_strings else ("x",))]
                    for name, sub in commands.items()}
        every = set().union(*declared.values())
        self.assertIn("--concurrency", every - declared[("classify",)])
        self.assertLessEqual({("ask", "noul"), ("ask", "choice"), ("ask", "score")}, set(commands))
        self.assertLessEqual({"-o", "-l", "--expect", "--min-confidence"}, every - declared[("ask", "noul")])
        for name, flags in declared.items():
            for flag in sorted(every - flags):
                with patch("sys.stdin", io.StringIO("")), patch("urllib.request.urlopen", side_effect=AssertionError), \
                     patch("jev.api_key", side_effect=AssertionError), contextlib.redirect_stderr(io.StringIO()) as err, \
                     self.assertRaises(SystemExit) as exit_:
                    jev.main([*name, *required[name], flag, "1"])
                self.assertEqual(exit_.exception.code, 2, (name, flag))
                self.assertIn(f"unrecognized arguments: {flag}", err.getvalue(), (name, flag))

    def test_cache_second_run_sends_nothing_and_reattaches_current_ids(self):
        with tempfile.TemporaryDirectory() as cache:
            records = [{"id": "a", "state": "one"}, {"id": "b", "state": ["two"]}]
            code, first, err, sent = self.run_cli("\n".join(map(json.dumps, records)), extra=["--cache", cache])
            self.assertEqual((code, err, len(sent)), (0, "", 2))
            renamed = [{**r, "id": f"new-{r['id']}"} for r in records]
            code, second, err, sent = self.run_cli("\n".join(map(json.dumps, renamed)), extra=["--cache", cache])
            self.assertEqual((code, err, sent), (0, "", []))
            self.assertEqual([json.loads(x) for x in io.StringIO(second)],
                             [{"id": r["id"], **RESPONSE} for r in renamed])
            self.assertEqual([{**json.loads(x), "id": None} for x in io.StringIO(first)],
                             [{**json.loads(x), "id": None} for x in io.StringIO(second)])
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
            self.assertEqual((code, [json.loads(x)["id"] for x in io.StringIO(out)]), (1, [0, 1]))
            self.assertTrue(out.endswith("\n"))
            self.assertEqual([json.loads(r.data)["state"] for r, _ in sent], ["s0", "s1", "s2", "s2"])
            self.assertIn("line 3", err)
            code, out, err, sent = self.run_cli(data, extra=["--cache", cache])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual([json.loads(r.data)["state"] for r, _ in sent], ["s2", "s3"])
            self.assertEqual([json.loads(x)["id"] for x in io.StringIO(out)], [0, 1, 2, 3])

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
            stored = json.loads(entry.read_text(encoding="utf-8"))
            for corrupt in ("{trunc", "[]", json.dumps({**stored, "version": 2}), json.dumps({**stored, "key": "x"}),
                            json.dumps({**stored, "response": {"answers": {}, "id": 1}}), "\udcff"):
                entry.write_text(corrupt, errors="surrogateescape", encoding="utf-8")
                code, out, err, sent = self.run_cli(record, extra=["--cache", cache])
                self.assertEqual((code, err, len(sent), json.loads(out)), (0, "", 1, {"id": 1, **RESPONSE}))
                self.assertEqual(json.loads(entry.read_text(encoding="utf-8")), stored)

    def test_unwritable_cache_warns_and_keeps_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("", encoding="utf-8")
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
            path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--dry-run", "--questions", str(path),
                                  "--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"],
                                 cwd=ROOT, env={"PATH": os.environ["PATH"]}, input='{"id":"p","state":"hello"}\n',
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        report = json.loads(run.stdout)
        self.assertEqual(report["records"][0]["id"], "p")
        self.assertNotEqual(Decimal(report["estimated_cost_usd"]), 0)

    def test_stdio_is_utf8_whatever_the_locale(self):
        env = {"PATH": os.environ["PATH"], "PYTHONIOENCODING": "ascii", "LC_ALL": "C"}
        result = {"id": "s\u00e9\u2028", "answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.5}}}
        run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "summarize"], env=env, capture_output=True,
                             input=(json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"), timeout=10)
        self.assertEqual((run.returncode, run.stderr), (0, b""))
        self.assertEqual(json.loads(run.stdout.decode("utf-8"))["examples"], {"a": [result["id"]]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
            run = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--dry-run", "--questions", str(path)],
                                 env=env, capture_output=True, timeout=10,
                                 input=(json.dumps({"id": "r\u00e9", "state": "caf\u00e9"}, ensure_ascii=False) + "\n").encode("utf-8"))
        self.assertEqual((run.returncode, run.stderr), (0, b""))
        self.assertEqual(json.loads(run.stdout.decode("utf-8"))["records"][0]["id"], "r\u00e9")

    def test_help_as_process(self):
        result = subprocess.run([sys.executable, str(ROOT / "jev.py"), "--help"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertIn("batch", result.stdout)
        result = subprocess.run([sys.executable, str(ROOT / "jev.py"), "batch", "--help"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertIn("sends nothing and reads no credentials", " ".join(result.stdout.split()))

    def test_version_as_process(self):
        result = subprocess.run([sys.executable, str(ROOT / "jev.py"), "--version"], capture_output=True, encoding="utf-8")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, f"jev.py {jev.__version__}\n", ""))

    def test_version_is_the_newest_changelog_release(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        newest = re.search(r"^## (\d+\.\d+\.\d+)$", changelog, re.MULTILINE)
        self.assertIsNotNone(newest, "CHANGELOG.md has no ## X.Y.Z heading")
        self.assertEqual(jev.__version__, newest[1])

    def test_docs_state_the_retry_rule_the_code_runs(self):
        docs = {doc: " ".join((ROOT / doc).read_text(encoding="utf-8").split()) for doc in ("README.md", "SKILL.md")}
        for doc, text in docs.items():
            self.assertIn(jev.RETRY_RULE, text, f"{doc} must state jev.RETRY_RULE verbatim")
        self.assertIn(f"(default {jev.DEFAULT_RETRIES}, max {jev.MAX_RETRIES}) after {jev.RETRY_RULE}", docs["README.md"])

    def test_documented_python_floor_is_the_lowest_ci_version(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        matrix = re.search(r"python: \[(.*)\]", ci)[1]
        lowest = min(tuple(map(int, v.strip(' "').split("."))) for v in matrix.split(","))
        self.assertEqual(jev.MIN_PYTHON, lowest)
        floor = "Python {}.{}+".format(*lowest)
        verify = ROOT / ".claude/skills/verify-jev"
        self.assertIn(f"MIN_PYTHON = {lowest}", (verify / "verify.py").read_text(encoding="utf-8"))
        for doc in (ROOT / "README.md", verify / "SKILL.md"):
            self.assertIn(floor, doc.read_text(encoding="utf-8"), f"{doc.name} must state the CI floor")
        self.assertIn('requires-python = ">={}.{}"'.format(*lowest), (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        install_python = re.search(r"^  install:\n.*?python-version: \"([0-9.]+)\"", ci, re.MULTILINE | re.DOTALL)[1]
        self.assertEqual(install_python, "{}.{}".format(*lowest), "CI job install must run on the floor")


    # Gate fixtures: one question of each gateable type, plus a mixed mapping that needs --question.
    GATE_QUESTIONS = {
        "noul": {"spam": {"type": "noul", "instructions": "Spam?"}},
        "choice": QUESTIONS,
        "score": {"level": {"type": "score", "criteria": ["low", "high"]}},
        "mixed": {"spam": {"type": "noul", "instructions": "Spam?"}, **QUESTIONS},
    }

    def usage_error(self, argv):
        """Exit code and stderr of a run argparse must reject before reading input or sending."""
        with patch("sys.stdin", io.StringIO("")), patch("urllib.request.urlopen", side_effect=AssertionError), \
             patch("jev.api_key", side_effect=AssertionError), contextlib.redirect_stderr(io.StringIO()) as err, \
             self.assertRaises(SystemExit) as exit_:
            jev.main(argv)
        return exit_.exception.code, err.getvalue()

    @staticmethod
    def gate_response(name, **answer):
        return {"model": "m", "answers": {name: answer}, "usage": {"input_tokens": 1}}

    def test_gate_exit_codes(self):
        noul = lambda value: ("noul", self.gate_response("spam", type="noul", noul=value))
        choice = lambda winner, confidence: ("choice", self.gate_response(
            "route", type="choice", choice=winner, confidence=confidence,
            probabilities={"a": confidence, "b": 1 - confidence}))
        score = lambda confidence: ("score", self.gate_response(
            "level", type="score", score=1, legend="high", confidence=confidence))
        band = ["--abstain-band", "0.4,0.6"]
        table = [
            (noul(0.5), ["--fail-under", "0.5"], 0),
            (noul(0.4999), ["--fail-under", "0.5"], jev.GATE_FALSE),
            (noul(0), ["--fail-under", "0"], 0),
            (noul(1), ["--fail-under", "1"], 0),
            (noul(0.9999), ["--fail-under", "1"], jev.GATE_FALSE),
            (noul(0.4), band, jev.GATE_ABSTAIN),
            (noul(0.6), band, jev.GATE_ABSTAIN),
            (noul(0.3999), band, 0),
            (noul(0.6001), band, 0),
            (noul(0.5), ["--abstain-band", "0.5,0.5"], jev.GATE_ABSTAIN),
            (noul(0.45), [*band, "--fail-under", "0.5"], jev.GATE_ABSTAIN),
            (noul(0.3), [*band, "--fail-under", "0.5"], jev.GATE_FALSE),
            (noul(0.7), [*band, "--fail-under", "0.5"], 0),
            (noul(1), [], 0),
            (choice("a", 0.75), ["--expect", "a"], 0),
            (choice("a", 0.75), ["--expect", "b"], jev.GATE_FALSE),
            (choice("a", 0.75), ["--min-confidence", "0.75"], 0),
            (choice("a", 0.75), ["--min-confidence", "0.7501"], jev.GATE_FALSE),
            (choice("a", 0.75), ["--expect", "a", "--min-confidence", "0.8"], jev.GATE_FALSE),
            (choice("b", 0.9), ["--expect", "a", "--min-confidence", "0.8"], jev.GATE_FALSE),
            (score(0.6), ["--min-confidence", "0.5"], 0),
            (score(0.6), ["--min-confidence", "0.7"], jev.GATE_FALSE),
            (("mixed", self.gate_response("spam", type="noul", noul=0.2)), ["--question", "spam", "--fail-under", "0.5"],
             jev.GATE_FALSE),
            (("mixed", self.gate_response("route", type="choice", choice="b", confidence=1)),
             ["--question", "route", "--expect", "b"], 0),
        ]
        for (kind, response), flags, expected in table:
            with self.subTest(answer=response["answers"], flags=flags):
                code, out, err, sent = self.run_cli('{"id":"g","state":"x"}', "classify", response, flags,
                                                    questions=self.GATE_QUESTIONS[kind])
                self.assertEqual((code, err, len(sent)), (expected, "", 1))
                # The full response is printed whatever the gate decides.
                self.assertEqual(json.loads(out), {"id": "g", **response})

    def test_gate_on_an_unreadable_answer_prints_the_response_and_exits_1(self):
        for answer in ({"type": "noul"}, {"type": "noul", "noul": True}, {"type": "noul", "noul": "0.9"}, None):
            response = {"model": "m", "answers": {} if answer is None else {"spam": answer}}
            with self.subTest(answer=answer):
                code, out, err, sent = self.run_cli('{"id":1,"state":"x"}', "classify", response,
                                                    ["--fail-under", "0.5"], questions=self.GATE_QUESTIONS["noul"])
                self.assertEqual((code, len(sent)), (1, 1))
                self.assertEqual(json.loads(out), {"id": 1, **response})
                self.assertEqual(err, "jev: cannot evaluate the gate; answer 'spam' has no numeric noul\n")

    def test_gate_values_out_of_range_are_usage_errors(self):
        for flags in (["--fail-under", "1.5"], ["--fail-under", "-0.1"], ["--fail-under", "nan"],
                      ["--fail-under", "inf"], ["--fail-under", "x"], ["--min-confidence", "2"],
                      ["--abstain-band", "0.6,0.4"], ["--abstain-band", "0.4"], ["--abstain-band", "0.1,0.2,0.3"],
                      ["--abstain-band", "a,b"], ["--abstain-band", "0.4,1.1"], ["--abstain-band", "nan,0.5"]):
            with self.subTest(flags=flags):
                code, err = self.usage_error(["classify", "--questions", "q.json", *flags])
                self.assertEqual(code, 2)
                self.assertIn(f"argument {flags[0]}:", err)

    def test_gate_that_cannot_fit_the_questions_fails_before_credentials_or_requests(self):
        cases = [
            ("noul", ["--expect", "a"], "--expect gates choice questions; 'spam' is noul"),
            ("noul", ["--min-confidence", "0.5"], "--min-confidence gates choice or score questions; 'spam' is noul"),
            ("choice", ["--fail-under", "0.5"], "--fail-under gates noul questions; 'route' is choice"),
            ("choice", ["--abstain-band", "0.4,0.6"], "--abstain-band gates noul questions; 'route' is choice"),
            ("score", ["--expect", "low"], "--expect gates choice questions; 'level' is score"),
            ("choice", ["--expect", "c"], "--expect 'c' is not a criteria key of 'route'"),
            ("mixed", ["--fail-under", "0.5"], "gate flags need --question when the questions file has several questions"),
            ("mixed", ["--question", "nope", "--fail-under", "0.5"], "--question 'nope' is not in the questions file"),
            ("mixed", ["--question", "route", "--fail-under", "0.5"], "--fail-under gates noul questions; 'route' is choice"),
        ]
        for kind, flags, message in cases:
            for dry_run in ([], ["--dry-run"]):
                with self.subTest(kind=kind, flags=flags, dry_run=dry_run), patch("jev.api_key") as key:
                    code, out, err, sent = self.run_cli(
                        '{"id":1,"state":"x"}', "classify", AssertionError,
                        [*flags, *dry_run, "--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"],
                        questions=self.GATE_QUESTIONS[kind])
                    self.assertEqual((code, out, err, sent), (1, "", f"jev: {message}\n", []))
                    key.assert_not_called()

    def test_gate_dry_run_validates_and_exits_0_without_a_request(self):
        code, out, err, sent = self.run_cli('{"id":1,"state":"x"}', "classify", AssertionError,
                                            ["--dry-run", "--fail-under", "0.5", "--abstain-band", "0.4,0.6"],
                                            questions=self.GATE_QUESTIONS["noul"])
        self.assertEqual((code, err, sent), (0, "", []))
        self.assertTrue(json.loads(out)["dry_run"])

    def test_chunked_record_gates_on_the_aggregated_answer(self):
        capacity = 4
        budget = 512 + len(json.dumps(QUESTIONS, ensure_ascii=False).encode()) + capacity
        replies = iter([{"answers": {"route": {"type": "choice", "choice": "a", "confidence": 0.9,
                                               "probabilities": {"a": 0.9, "b": 0.1}}}},
                        {"answers": {"route": {"type": "choice", "choice": "b", "confidence": 0.8,
                                               "probabilities": {"a": 0.2, "b": 0.8}}}}])
        # Mean probabilities are a 0.55, b 0.45: the winner is a at confidence 0.55. Each chunk alone passes 0.6.
        code, out, err, sent = self.run_cli('{"id":1,"state":"abcdefgh"}', "classify", lambda r: next(replies),
                                            ["--chunk-tokens", str(budget), "--min-confidence", "0.6"])
        self.assertEqual((code, err, len(sent)), (jev.GATE_FALSE, "", 2))
        self.assertEqual(json.loads(out)["answers"]["route"]["choice"], "a")

    def test_batch_eval_and_summarize_take_no_gate_flags(self):
        gates = (("--fail-under", "0.5"), ("--expect", "a"), ("--min-confidence", "0.5"), ("--abstain-band", "0.4,0.6"))
        commands = {"batch": ["--questions", "q.json"], "eval": ["--questions", "q.json", "--labels", "l.jsonl"],
                    "summarize": []}
        for command, required in commands.items():
            # eval and summarize declare their own --question; batch must not take it.
            flags = gates + ((("--question", "spam"),) if command == "batch" else ())
            for flag, value in flags:
                with self.subTest(command=command, flag=flag):
                    code, err = self.usage_error([command, *required, flag, value])
                    self.assertEqual(code, 2)
                    self.assertIn(f"unrecognized arguments: {flag}", err)

    def test_readme_and_help_list_the_exit_codes_the_code_returns(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = re.search(r"^## Exit codes\n(.*?)(?=^## )", readme, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(section, "README.md needs an ## Exit codes section")
        rows = re.findall(r"^\| `?(\d+)`? \| (.*?) \|$", section[1], re.MULTILINE)
        self.assertEqual(rows, [(str(code), meaning) for code, meaning in jev.EXIT_CODES.items()],
                         "README exit-code table must match jev.EXIT_CODES row by row")
        for argv in (["--help"], ["classify", "--help"], ["ask", "noul", "--help"]):
            result = subprocess.run([sys.executable, str(ROOT / "jev.py"), *argv], capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0)
            for code, meaning in jev.EXIT_CODES.items():
                self.assertIn(f"{code:<5}{meaning}", result.stdout, argv)


    # ask: (sub-command flags, the question README says each type builds). The question text is "Q?".
    ASK = {
        "noul": ([], {"type": "noul", "instructions": "Q?"}),
        "choice": (["-o", "a=Pay = money", "-o", "b="], {"type": "choice", "instructions": "Q?",
                                                         "criteria": {"a": "Pay = money", "b": ""}}),
        "score": (["-l", "low", "-l", "high"], {"type": "score", "instructions": "Q?", "criteria": ["low", "high"]}),
    }
    ASK_ANSWERS = {"noul": {"type": "noul", "noul": 0.875},
                   "choice": {"type": "choice", "choice": "a", "confidence": 0.75, "probabilities": {"a": 0.75, "b": 0.25}},
                   "score": {"type": "score", "score": 1, "legend": "high", "confidence": 0.5}}

    def ask(self, kind, *extra, data="", response=None, env=None):
        response = response or self.gate_response("answer", **self.ASK_ANSWERS[kind])
        return self.run_argv(["ask", kind, "Q?", *self.ASK[kind][0], *extra], data, response, env)

    def test_ask_sends_and_prints_what_classify_does_for_the_same_question_and_record(self):
        for kind, (_, question) in self.ASK.items():
            for state in ("Charged twice", " padded\r\n ünï ✓\n"):
                with tempfile.TemporaryDirectory() as tmp:
                    state_file = Path(tmp) / "state.txt"
                    with state_file.open("w", encoding="utf-8", newline="") as handle:
                        handle.write(state)
                    sources = {"--state": (["--state", state], ""), "--state-file": (["--state-file", str(state_file)], ""),
                               "stdin": ([], state)}
                    response = self.gate_response("answer", **self.ASK_ANSWERS[kind])
                    classified = self.run_cli(json.dumps({"id": None, "state": state}), "classify", response,
                                              questions={"answer": question})
                    for source, (flags, data) in sources.items():
                        with self.subTest(kind=kind, state=state, source=source):
                            code, out, err, sent = self.ask(kind, *flags, data=data)
                            self.assertEqual((code, out, err), classified[:3])
                            self.assertEqual(json.loads(sent[0][0].data),
                                             {"model": jev.DEFAULT_MODEL, "state": state, "questions": {"answer": question}})
                            (request, timeout), = sent
                            (expected, expected_timeout), = classified[3]
                            self.assertEqual((request.full_url, request.data, request.header_items(), timeout),
                                             (expected.full_url, expected.data, expected.header_items(), expected_timeout))

    def test_ask_stdin_is_sent_unchanged_by_the_process(self):
        state = "line one\r\nline two\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps({"answer": self.ASK["noul"][1]}), encoding="utf-8")
            runs = [subprocess.run([sys.executable, str(ROOT / "jev.py"), *argv, "--dry-run"], capture_output=True,
                                   input=data.encode("utf-8"), timeout=10)
                    for argv, data in ((["ask", "noul", "Q?"], state),
                                       (["classify", "--questions", str(path)], json.dumps({"id": None, "state": state})))]
        self.assertEqual([(r.returncode, r.stderr) for r in runs], [(0, b"")] * 2)
        self.assertEqual(runs[0].stdout, runs[1].stdout)

    def test_ask_value_prints_only_the_answer_field(self):
        for kind, expected in (("noul", "0.875\n"), ("choice", "a\n"), ("score", "1\n")):
            with self.subTest(kind=kind):
                self.assertEqual(self.ask(kind, "--value", data="x")[:3], (0, expected, ""))
        broken = [("noul", {"type": "noul"}, "numeric noul"), ("noul", {"type": "noul", "noul": True}, "numeric noul"),
                  ("choice", {"type": "choice", "choice": 1}, "string choice"),
                  ("score", {"type": "score", "score": "1"}, "numeric score")]
        for kind, answer, missing in broken:
            with self.subTest(answer=answer):
                code, out, err, sent = self.ask(kind, "--value", data="x", response=self.gate_response("answer", **answer))
                self.assertEqual((code, out, len(sent)), (1, "", 1))
                self.assertEqual(err, f"jev: cannot print --value; answer 'answer' has no {missing}\n")

    def test_ask_dry_run_sends_nothing_reads_no_key_and_ignores_value(self):
        typesafe = ["--base-url", "https://api.typesafe.ai/v1", "--model", "jev-1.14.0"]
        for kind, (_, question) in self.ASK.items():
            with self.subTest(kind=kind), patch("jev.api_key") as key:
                code, out, err, sent = self.ask(kind, "--dry-run", "--value", *typesafe, data="Charged twice")
                _, expected, _, _ = self.run_cli('{"id":null,"state":"Charged twice"}', "classify", AssertionError,
                                                 ["--dry-run", *typesafe], questions={"answer": question})
                self.assertEqual((code, err, sent), (0, "", []))
                self.assertEqual(out, expected)
                self.assertTrue(json.loads(out)["dry_run"])
                key.assert_not_called()

    def test_ask_empty_state_exits_1_before_credentials_or_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.txt"
            empty.write_text("", encoding="utf-8")
            for source in ([], ["--state", ""], ["--state-file", str(empty)]):
                for dry_run in ([], ["--dry-run"]):
                    with self.subTest(source=source, dry_run=dry_run), patch("jev.api_key") as key:
                        code, out, err, sent = self.ask("noul", *source, *dry_run, "--base-url",
                                                        "https://api.typesafe.ai/v1", response=AssertionError)
                        self.assertEqual((code, out, sent), (1, "", []))
                        self.assertEqual(err, "jev: state is empty; pass --state TEXT, --state-file PATH, or text on stdin\n")
                        key.assert_not_called()

    def test_ask_usage_errors(self):
        cases = [
            (["ask", "choice", "Q?"], "the following arguments are required: -o"),
            (["ask", "score", "Q?"], "the following arguments are required: -l"),
            (["ask", "noul"], "the following arguments are required: QUESTION"),
            (["ask"], "the following arguments are required"),
            (["ask", "noul", "Q?", "-o", "a=A"], "unrecognized arguments: -o"),
            (["ask", "noul", "Q?", "-l", "low"], "unrecognized arguments: -l"),
            (["ask", "noul", "Q?", "--state", "x", "--state-file", "x.txt"], "not allowed with argument --state"),
            (["ask", "choice", "Q?", "-o", "=A"], "is not KEY=DESCRIPTION"),
            (["ask", "choice", "Q?", "-o", "A"], "is not KEY=DESCRIPTION"),
            (["ask", "choice", "Q?", "-o", "a=A", "-o", "a=B"], "argument -o: KEY 'a' given twice"),
            (["ask", "noul", "Q?", "--questions", "q.json"], "unrecognized arguments: --questions"),
            (["ask", "noul", "Q?", "--chunk-tokens", "100"], "unrecognized arguments: --chunk-tokens"),
            (["ask", "noul", "Q?", "--question", "answer"], "unrecognized arguments: --question"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                code, err = self.usage_error(argv)
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_ask_takes_only_the_gate_flags_that_fit_its_type(self):
        gates = {"--fail-under": "0.5", "--expect": "a", "--min-confidence": "0.5", "--abstain-band": "0.4,0.6"}
        fits = {"noul": {"--fail-under", "--abstain-band"}, "choice": {"--expect", "--min-confidence"},
                "score": {"--min-confidence"}}
        for kind, taken in fits.items():
            for flag, value in gates.items():
                argv = ["ask", kind, "Q?", *self.ASK[kind][0], flag, value]
                with self.subTest(kind=kind, flag=flag):
                    if flag in taken:
                        code, _, err, sent = self.ask(kind, flag, value, data="x")
                        self.assertEqual((err, len(sent)), ("", 1))
                        self.assertIn(code, (0, jev.GATE_FALSE, jev.GATE_ABSTAIN))
                    else:
                        code, err = self.usage_error(argv)
                        self.assertEqual(code, 2)
                        self.assertIn(f"unrecognized arguments: {flag}", err)

    def test_ask_gate_exit_codes_with_and_without_value(self):
        noul = lambda value: self.gate_response("answer", type="noul", noul=value)
        choice = self.gate_response("answer", type="choice", choice="a", confidence=0.75)
        score = self.gate_response("answer", type="score", score=1, confidence=0.5)
        table = [
            ("noul", noul(0.2), ["--fail-under", "0.5"], jev.GATE_FALSE, "0.2\n"),
            ("noul", noul(0.5), ["--fail-under", "0.5"], 0, "0.5\n"),
            ("noul", noul(0.45), ["--abstain-band", "0.4,0.6", "--fail-under", "0.5"], jev.GATE_ABSTAIN, "0.45\n"),
            ("choice", choice, ["--expect", "b"], jev.GATE_FALSE, "a\n"),
            ("choice", choice, ["--expect", "a", "--min-confidence", "0.75"], 0, "a\n"),
            ("score", score, ["--min-confidence", "0.6"], jev.GATE_FALSE, "1\n"),
        ]
        for kind, response, flags, expected, value in table:
            for show in ([], ["--value"]):
                with self.subTest(kind=kind, flags=flags, show=show):
                    code, out, err, sent = self.ask(kind, *flags, *show, data="x", response=response)
                    self.assertEqual((code, err, len(sent)), (expected, "", 1))
                    self.assertEqual(out, value if show else json.dumps({"id": None, **response}) + "\n")

    def test_ask_expect_outside_the_options_fails_before_credentials_or_requests(self):
        for dry_run in ([], ["--dry-run"]):
            with self.subTest(dry_run=dry_run), patch("jev.api_key") as key:
                code, out, err, sent = self.ask("choice", "--expect", "c", *dry_run, "--base-url",
                                                "https://api.typesafe.ai/v1", data="x", response=AssertionError)
                self.assertEqual((code, out, err, sent), (1, "", "jev: --expect 'c' is not a criteria key of 'answer'\n", []))
                key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
