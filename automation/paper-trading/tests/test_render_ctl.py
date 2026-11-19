"""Tests for the render control CLI (Task 10 trial commands here; Task 7
adds migrate/status/handoff/keys/token/net-check/dry-run/footprint tests)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_lease as rl
import render_state as rs

CTL = HERE.parent / 'render_ctl.py'


class TrialCtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'ctl.sqlite'
        self.store = rs.SqliteStore(self.db_path)
        rs.migrate(self.store)
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                  'PAPER_TEST_DATABASE', 'PAPER_STATE_DIR',
                  'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _env(self):
        env = dict(os.environ)
        env.update({
            'PAPER_RUNTIME': 'render',
            'RENDER_MODE': 'trial',
            'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1',
            'PAPER_TEST_DATABASE': str(self.db_path),
            'PAPER_STATE_DIR': self.tmp.name,
        })
        return env

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(CTL), *argv],
            capture_output=True, text=True, env=self._env(), timeout=120,
            cwd=str(HERE.parent))

    def test_trial_init_cli(self):
        r = self._run('trial-init', '--namespace', 'paper_render_trial_v1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out['ok'])
        rows = self.store.execute(
            "SELECT value FROM paper_runtime_kv WHERE scope=?"
            " AND key='trial_init'", ('paper_render_trial_v1',))
        self.assertEqual(len(rows), 1)

    def test_trial_init_cli_refuses_live(self):
        r = self._run('trial-init', '--namespace', 'paper_v2')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_trial_purge_cli(self):
        self._run('trial-init', '--namespace', 'paper_render_trial_v1')
        rl.set_active_runner(self.store, 'paper_render_trial_v1', 'render')
        r = self._run('trial-purge', '--namespace', 'paper_render_trial_v1',
                      '--state-only')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)['ok'])
        self.assertIsNone(rl.get_active_runner(
            self.store, 'paper_render_trial_v1'))

    def test_budget_check_cli(self):
        r = self._run('budget-check')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['trial']['limits']['per_day'], 1000)
        self.assertEqual(out['trial']['limits']['per_minute'], 8)
        self.assertEqual(out['box']['limits']['per_day'], 4000)
        self.assertIsNone(out['trial']['state'])
        self.assertIsNone(out['box']['state'])


if __name__ == '__main__':
    unittest.main()
