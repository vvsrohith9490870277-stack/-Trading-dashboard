"""Fix 3 (Grok box test, render-deploy-03): the db helper must start
when logs/ is missing.

The Render worker logged db_helper_start_failed FileNotFoundError
every cycle: .dockerignore excludes `logs`, and the Dockerfile never
creates the directory, so _start()'s open('logs/db-helper.log', 'a')
raised before the helper process could spawn. _start() must create
the directory first (mkdir parents=True), which fixes it in Docker,
on the box, and in tests, regardless of how the image was built.
"""
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import db_helper as dbh
import engine as e

CFG = {**e.CFG['db_helper'], 'query_timeout_seconds': 5,
       'batch_timeout_seconds': 5, 'start_timeout_seconds': 15,
       'retry_backoff_seconds': 0.01, 'shutdown_timeout_seconds': 3}

FAKE = textwrap.dedent('''
    import sys, json
    for line in sys.stdin:
        req = json.loads(line)
        sys.stdout.write(json.dumps(
            {'id': req['id'], 'ok': True, 'result': {}}) + '\\n')
        sys.stdout.flush()
''')


class DbHelperLogsDirTests(unittest.TestCase):
    def test_start_creates_logs_dir_when_missing(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.assertFalse((root / 'logs').exists())
        fake = root / 'fake_helper.py'
        fake.write_text(FAKE)
        real_popen = dbh.subprocess.Popen
        popen = lambda args, **kw: real_popen(
            [sys.executable, str(fake)], **kw)
        helper = dbh.Helper(
            root, None, CFG,
            per_call=lambda a: [{'via': 'per_call'}]
            if a['action'] == 'query' else None,
            popen=popen, sleep=lambda s: None)
        self.addCleanup(helper.close)
        self.assertTrue(helper._start())
        self.assertTrue(helper.alive())
        self.assertTrue((root / 'logs' / 'db-helper.log').exists())

    def test_start_succeeds_when_logs_dir_already_exists(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / 'logs').mkdir()
        fake = root / 'fake_helper.py'
        fake.write_text(FAKE)
        real_popen = dbh.subprocess.Popen
        popen = lambda args, **kw: real_popen(
            [sys.executable, str(fake)], **kw)
        helper = dbh.Helper(
            root, None, CFG,
            per_call=lambda a: [{'via': 'per_call'}]
            if a['action'] == 'query' else None,
            popen=popen, sleep=lambda s: None)
        self.addCleanup(helper.close)
        self.assertTrue(helper._start())
        self.assertTrue(helper.alive())


if __name__ == '__main__':
    unittest.main()
