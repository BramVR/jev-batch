import contextlib
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    def run_cli(self, data, command="batch", response=RESPONSE, extra=()):
        sent = []

        def http(request, timeout):
            sent.append((request, timeout))
            if isinstance(response, Exception):
                raise response
            return contextlib.closing(io.BytesIO(json.dumps(response).encode()))

        with tempfile.TemporaryDirectory() as tmp:
            questions = Path(tmp) / "q.json"
            questions.write_text(json.dumps(QUESTIONS))
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-only"}), \
                 patch("sys.stdin", io.StringIO(data)), \
                 patch("urllib.request.urlopen", side_effect=http), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = jev.main([command, "--questions", str(questions), *extra])
            return code, out.getvalue(), err.getvalue(), sent

    def test_batch_preserves_ids_probabilities_metadata_and_request(self):
        records = [{"id": "session-α", "state": {"text": "hello"}},
                   {"id": 42, "state": ["world"]}]
        code, out, err, sent = self.run_cli("\n".join(map(json.dumps, records)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([json.loads(x) for x in out.splitlines()],
                         [{"id": r["id"], **RESPONSE} for r in records])
        for (req, timeout), record in zip(sent, records):
            self.assertEqual(req.full_url, "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.get_header("Authorization"), "Bearer test-only")
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(json.loads(req.data), {"model": "jev-1.14.0", "state": record["state"], "questions": QUESTIONS})
            self.assertEqual(timeout, 60)

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

    def test_http_errors_do_not_echo_private_body_or_retry(self):
        for status in (401, 422, 429, 529):
            error = urllib.error.HTTPError(jev.ENDPOINT, status, "private", {}, io.BytesIO(b"secret"))
            code, out, err, sent = self.run_cli('{"id":1,"state":"private"}', "classify", error)
            self.assertEqual((code, out), (1, ""))
            self.assertIn(str(status), err)
            self.assertNotIn("secret", err)
            self.assertNotIn("private", err)
            self.assertEqual(len(sent), 1)

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
                path.write_text(json.dumps(QUESTIONS))
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/systemone"
                bootstrap = f"import jev; jev.ENDPOINT={endpoint!r}; raise SystemExit(jev.main())"
                run = subprocess.run([sys.executable, "-c", bootstrap, "classify", "--questions", str(path)],
                                     cwd=ROOT, env={**os.environ, "TYPESAFE_API_KEY": "test-only"},
                                     input='{"id":"process","state":"hello"}', capture_output=True, text=True, timeout=10)
                self.assertEqual((run.returncode, run.stderr), (0, ""))
                self.assertEqual(json.loads(run.stdout), {"id": "process", **RESPONSE})
                self.assertEqual(received, [("/v1/systemone", "Bearer test-only",
                                            {"model": "jev-1.14.0", "state": "hello", "questions": QUESTIONS})])
            finally:
                server.shutdown()
                worker.join()

    def test_help_as_process(self):
        result = subprocess.run([sys.executable, str(ROOT / "jev.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("batch", result.stdout)


if __name__ == "__main__":
    unittest.main()
