import contextlib
from decimal import Decimal
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

    def test_http_errors_do_not_echo_private_body_or_retry(self):
        for status in (401, 422, 429, 529):
            error = urllib.error.HTTPError(jev.DEFAULT_BASE_URL + "/systemone", status, "private", {}, io.BytesIO(b"secret"))
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
            code, out, err, sent = self.run_cli(data, response=fail_third, extra=["--cache", cache])
            self.assertEqual((code, len(out.splitlines()), len(sent)), (1, 2, 3))
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
