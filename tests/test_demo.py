import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import demo_sessions as demo


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

            def send(command, stdin, stdout, **kwargs):
                self.assertEqual(stdin.read(), raw)
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
