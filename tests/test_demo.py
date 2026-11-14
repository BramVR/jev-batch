from collections import Counter
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import demo_sessions as demo

REPO = Path(__file__).resolve().parents[1]
# Synthetic look-alikes only; each must vanish from saved samples, summaries, and HTTP bodies.
FIXTURE_SECRETS = {
    'bearer': 'synthBearerTokenValue0123456789',
    'token': 'sk-synthetic0000aaaa1111bbbb2222',
    'github': 'ghp_synthetic0000000000000000000000',
    'assignment': 'synthPasswordValue42',
    'email': 'fixture.person@example.test',
    'unix_user': 'fixtureunixuser',
    'windows_user': 'FixtureWinUser',
}
FIXTURE_TEXT = (f"curl -H 'Authorization: Bearer {FIXTURE_SECRETS['bearer']}' with key {FIXTURE_SECRETS['token']} "
                f"and {FIXTURE_SECRETS['github']}; password={FIXTURE_SECRETS['assignment']}; mail "
                f"{FIXTURE_SECRETS['email']}; see /Users/{FIXTURE_SECRETS['unix_user']}/notes and "
                f"C:\\Users\\{FIXTURE_SECRETS['windows_user']}\\Desktop and /home/{FIXTURE_SECRETS['unix_user']}/x")
RESPONSE = {'model': 'stub', 'answers': {'failure_pattern': {'choice': 'fine', 'probabilities': {'fine': 1.0}}}}


def assert_clean(test, text):
    for name, value in FIXTURE_SECRETS.items():
        test.assertNotIn(value, text, name)


def session_line(kind, payload):
    return json.dumps({'type': kind, 'payload': payload}) + '\n'


class Capture:
    """Loopback System One that records every request body it receives."""

    def __enter__(self):
        bodies = self.bodies = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers['Content-Length'])).decode())
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(RESPONSE).encode())

            def log_message(self, *args):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class DemoTests(unittest.TestCase):
    def test_send_reuses_exact_sample_and_summarizes_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.private/demo'
            folder.mkdir(parents=True)
            records = [{'id': f'synthetic-{i}', 'state': 'test'} for i in range(8)]
            raw = ''.join(json.dumps(r, indent=None) + '\n' for r in records)
            sample = folder / 'sample.jsonl'
            sample.write_text(raw)
            before = hashlib.sha256(sample.read_bytes()).digest()
            (root / 'examples').mkdir()
            (root / 'examples/failure-patterns.json').write_text(json.dumps({
                'failure_pattern': {'criteria': {'fine': 'Fine', 'loop': 'Loop'}}}))

            def send(command, input, stdout, **kwargs):
                self.assertEqual(input, raw)
                for record in records:
                    stdout.write(json.dumps({'id': record['id'], 'answers': {
                        'failure_pattern': {'choice': 'fine'}}}) + '\n')
                return subprocess.CompletedProcess(command, 0, stderr='')

            with patch.object(demo, 'ROOT', root), patch.object(demo, 'prepare_sample') as prepare, \
                    patch('subprocess.run', side_effect=send), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(demo.main(['--send']), 0)
            prepare.assert_not_called()
            self.assertEqual(hashlib.sha256(sample.read_bytes()).digest(), before)
            summary = json.loads((folder / 'summary.json').read_text())
            self.assertEqual(summary['sample_count'], 8)
            self.assertEqual(summary['classification_counts'], {'fine': 8, 'loop': 0})
            self.assertEqual(summary['example_ids_by_category'], {
                'fine': ['synthetic-0', 'synthetic-1', 'synthetic-2'], 'loop': []})
            self.assertEqual(summary['unclassified'], 0)

    def test_limit_and_missing_sample_rejected_before_send(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(demo, 'ROOT', Path(tmp)), \
                patch('subprocess.run') as send, patch.object(demo, 'prepare_sample') as prepare, \
                contextlib.redirect_stderr(io.StringIO()):
            for args in (['--limit', '0'], ['--limit', '21'], ['--send']):
                with self.assertRaises(SystemExit) as exc:
                    demo.main(args)
                self.assertEqual(exc.exception.code, 2)
            send.assert_not_called()
            prepare.assert_not_called()

    def test_sample_above_limit_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.private/demo'
            folder.mkdir(parents=True)
            sample = folder / 'sample.jsonl'
            raw = ''.join(json.dumps({'id': i, 'state': 'test'}) + '\n' for i in range(9))
            sample.write_text(raw)
            with patch.object(demo, 'ROOT', root), patch('subprocess.run') as send, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                demo.main(['--send'])
            send.assert_not_called()
            self.assertEqual(sample.read_text(), raw)


class RedactionTests(unittest.TestCase):
    def test_nested_values_redacted_with_ids_keys_and_structure_kept(self):
        records = [{'id': 'user@example.test', 'state': {
            'password=' + 'k' * 9: [FIXTURE_TEXT, {'deep': [FIXTURE_TEXT, 7, None, True]}], 'n': 1.5}}]
        redacted, meta = demo.redact_records(records)
        self.assertEqual(redacted[0]['id'], 'user@example.test')
        state = redacted[0]['state']
        self.assertEqual(list(state), list(records[0]['state']))
        self.assertEqual(state['n'], 1.5)
        self.assertEqual(state['password=' + 'k' * 9][1]['deep'][1:], [7, None, True])
        self.assertEqual(state['password=' + 'k' * 9][0], state['password=' + 'k' * 9][1]['deep'][0])
        assert_clean(self, json.dumps(state))
        for placeholder in ('[REDACTED:bearer]', '[REDACTED:token]', '[REDACTED:secret]', '[REDACTED:email]',
                            '[REDACTED:home]'):
            self.assertIn(placeholder, json.dumps(state))
        self.assertEqual(meta, {'policy': demo.REDACTION_POLICY, 'counts': {
            'assignment': 2, 'bearer': 2, 'email': 2, 'home_path': 6, 'token': 4}})
        assert_clean(self, json.dumps(meta))
        self.assertEqual(records[0]['state']['password=' + 'k' * 9][0], FIXTURE_TEXT)

    def test_repeated_values_each_replaced_and_second_pass_is_noop(self):
        text = f"{FIXTURE_SECRETS['email']} then {FIXTURE_SECRETS['email']} and {FIXTURE_SECRETS['email']}"
        counts = Counter()
        once = demo.redact(text, counts)
        self.assertEqual(once, '[REDACTED:email] then [REDACTED:email] and [REDACTED:email]')
        self.assertEqual(counts, {'email': 3})
        again = Counter()
        self.assertEqual(demo.redact(once, again), once)
        self.assertEqual(again, Counter())

    def test_ordinary_text_unchanged(self):
        for text in ('The token is useful; a secret sauce; password reset flow.',
                     'version 1.2.3 at 3:00, ratio a/b, user@ host, 50% off',
                     'see https://example.com/home/page and src/Users/model.py',
                     'Bearer short', 'sk-short', 'C:\\Program Files\\App'):
            counts = Counter()
            self.assertEqual(demo.redact(text, counts), text)
            self.assertEqual(counts, Counter())


class DemoRedactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'repo'
        self.home = Path(tmp.name) / 'home'
        (self.root / 'examples').mkdir(parents=True)
        shutil.copy2(REPO / 'jev.py', self.root / 'jev.py')
        shutil.copy2(REPO / 'examples/failure-patterns.json', self.root / 'examples/failure-patterns.json')
        sessions = self.home / '.codex/sessions'
        sessions.mkdir(parents=True)
        straddle = 'x' * 3990 + ' ' + FIXTURE_SECRETS['token']
        for number, text in enumerate([FIXTURE_TEXT, straddle]):
            (sessions / f'{number}.jsonl').write_text(
                session_line('session_meta', {'id': f'synthetic-{number}', 'cwd': '/elsewhere'})
                + session_line('response_item', {'type': 'message', 'role': 'user',
                                                 'content': [{'type': 'input_text', 'text': text}]}))
        self.sample = self.root / '.private/demo/sample.jsonl'

    def demo(self, args, base_url='http://127.0.0.1:9/v1'):
        out = io.StringIO()
        env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', ''), 'JEV_BASE_URL': base_url}
        with patch.object(demo, 'ROOT', self.root), patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out):
            code = demo.main(args)
        return code, out.getvalue()

    def test_prepare_then_send_never_transmits_fixture_values(self):
        code, out = self.demo([])
        self.assertEqual(code, 0)
        saved = self.sample.read_text()
        assert_clean(self, saved + out + (self.root / '.private/demo/summary.json').read_text())
        self.assertNotIn('sk-synth', saved, 'redacted before the 4000-character cap')
        self.assertIn('[REDACTED:token]', saved)
        summary = json.loads(out)
        self.assertEqual(summary['redaction']['policy'], demo.REDACTION_POLICY)
        self.assertEqual(summary['redaction']['counts'], {
            'assignment': 1, 'bearer': 1, 'email': 1, 'home_path': 3, 'token': 3})
        before = self.sample.read_bytes()
        with Capture() as server:
            code, out = self.demo(['--send'], server.url)
        self.assertEqual(code, 0)
        self.assertEqual(self.sample.read_bytes(), before)
        self.assertEqual(len(server.bodies), 2)
        assert_clean(self, ''.join(server.bodies) + out + (self.root / '.private/demo/summary.json').read_text())
        self.assertIn('[REDACTED:home]', ''.join(server.bodies))
        self.assertEqual(json.loads(out)['redaction']['counts'], {})

    def test_send_redacts_outgoing_copy_of_older_unredacted_sample(self):
        self.sample.parent.mkdir(parents=True)
        raw = json.dumps({'id': 'old-1', 'state': {'messages': [{'role': 'user', 'text': FIXTURE_TEXT}]}}) + '\n'
        self.sample.write_text(raw)
        with Capture() as server:
            code, out = self.demo(['--send'], server.url)
        self.assertEqual(code, 0)
        self.assertEqual(self.sample.read_text(), raw)
        assert_clean(self, ''.join(server.bodies) + out)
        self.assertEqual(json.loads(server.bodies[0])['state']['messages'][0]['role'], 'user')
        self.assertEqual(json.loads(out)['redaction']['counts']['bearer'], 1)

    def test_no_redact_is_explicit_opt_out(self):
        code, out = self.demo(['--no-redact'])
        self.assertIn(FIXTURE_SECRETS['email'], self.sample.read_text())
        self.assertEqual(json.loads(out)['redaction'], {'policy': 'disabled', 'counts': {}})
        with Capture() as server:
            self.demo(['--send', '--no-redact'], server.url)
        self.assertIn(FIXTURE_SECRETS['email'], ''.join(server.bodies))
