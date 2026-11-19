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


class OpsCtlTests(unittest.TestCase):
    """Task 7: migrate/status/handoff/keys/token/net-check/dry-run/footprint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'ctl7.sqlite'
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                  'PAPER_TEST_DATABASE', 'PAPER_STATE_DIR',
                  'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN', 'TOKEN_ENC_KEY'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _env(self, **kw):
        env = dict(os.environ)
        env.update({
            'PAPER_RUNTIME': 'render',
            'RENDER_MODE': 'live',
            'PAPER_TEST_DATABASE': str(self.db_path),
            'PAPER_STATE_DIR': self.tmp.name,
        })
        env.update(kw)
        return env

    def _run(self, *argv, **kw):
        return subprocess.run(
            [sys.executable, str(CTL), *argv],
            capture_output=True, text=True, env=self._env(**kw), timeout=180,
            cwd=str(HERE.parent))

    def test_migrate_cli(self):
        r = self._run('migrate')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out['ok'])
        self.assertIn('paper_runner_lease', out['tables'])
        self.assertIn('paper_job_runs', out['tables'])
        self.assertIn('paper_runtime_kv', out['tables'])
        # idempotent: a second migrate is still ok
        r = self._run('migrate')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_handoff_cli(self):
        self._run('migrate')
        r = self._run('handoff', '--to', 'render')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)['active_runner'], 'render')
        r = self._run('handoff', '--to', 'box')
        self.assertEqual(json.loads(r.stdout)['active_runner'], 'box')
        r = self._run('handoff', '--to', 'sideways')
        self.assertNotEqual(r.returncode, 0)

    def test_gen_key_cli(self):
        r = self._run('gen-key')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        key = json.loads(r.stdout)['TOKEN_ENC_KEY']
        self.assertEqual(len(key), 44)
        from cryptography.fernet import Fernet
        Fernet(key.encode())  # parses as a valid Fernet key

    def test_seed_budget_and_token_check_cli(self):
        self._run('migrate')
        r = self._run('seed-budget', '--requests', '12',
                      '--day', '2026-11-20')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self._run('budget-check')
        out = json.loads(r.stdout)
        self.assertEqual(out['box']['state']['requests'], 12)
        # no token stored and no enc key: token-check reports absent
        r = self._run('token-check')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertFalse(out['present'])

    def test_net_check_cli_local(self):
        import socket
        import threading
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def _accept():
            while not stop.is_set():
                try:
                    srv.settimeout(0.5)
                    conn, _ = srv.accept()
                    conn.close()
                except Exception:
                    pass
        th = threading.Thread(target=_accept, daemon=True)
        th.start()
        try:
            r = self._run('net-check', '--host',
                          '127.0.0.1:%d' % port)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = json.loads(r.stdout)
            self.assertTrue(out['hosts'][0]['reachable'])
            r = self._run('net-check', '--host', '127.0.0.1:1')
            out = json.loads(r.stdout)
            self.assertFalse(out['hosts'][0]['reachable'])
        finally:
            stop.set()
            srv.close()

    def test_dry_run_cli(self):
        r = self._run('dry-run')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['namespace'], 'paper_v2')
        self.assertFalse(out['fyers_login_allowed'])
        slots = {s['name']: s['time'] for s in out['slots']
                 if 'time' in s}
        self.assertEqual(slots['preflight'], '08:15')
        self.assertEqual(slots['scan'], '08:45')
        self.assertEqual(slots['eod'], '16:10')
        self.assertIn('record_outages', {s['name'] for s in out['slots']})

    def test_footprint_cli(self):
        self._run('migrate')
        r = self._run('footprint', '--seconds', '5')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertLess(out['total_peak_rss_mb'], 512)
        self.assertGreaterEqual(out['cpu_seconds'], 0)
        self.assertGreater(out['est_wall_at_0_1_cpu_seconds'], 0)
        self.assertTrue(out['free_tier_ok'])

    def test_status_cli(self):
        self._run('migrate')
        self._run('handoff', '--to', 'box')
        r = self._run('status')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['namespace'], 'paper_v2')
        self.assertEqual(out['active_runner'], 'box')


class DryRunSimTests(unittest.TestCase):
    """Fake-clock dry-run simulator (gate G7c/7d/7e/7l). Each test runs the
    CLI in a subprocess against a throwaway SQLite store, no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'dry.sqlite'
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                  'PAPER_TEST_DATABASE', 'PAPER_STATE_DIR',
                  'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN',
                  'FYERS_PIN', 'FYERS_TOTP_SECRET'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, *argv, trial=False, canary=False):
        env = dict(os.environ)
        env.update({
            'PAPER_RUNTIME': 'render',
            'PAPER_TEST_DATABASE': str(self.db_path),
            'PAPER_STATE_DIR': self.tmp.name,
        })
        if trial:
            env.update({
                'RENDER_MODE': 'trial',
                'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1',
            })
        else:
            env['RENDER_MODE'] = 'live'
        if canary:
            env.update({'FYERS_PIN': 'CANARY_PIN',
                        'FYERS_TOTP_SECRET': 'CANARY_TOTP'})
        return subprocess.run(
            [sys.executable, str(CTL), *argv],
            capture_output=True, text=True, env=env, timeout=300,
            cwd=str(HERE.parent))

    def test_fake_clock_day_each_job_once(self):
        # G7c: full compressed day, test namespace, no network, each job once.
        r = self._run('dry-run', '--fake-clock', '--minutes', '660')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d['namespace'].startswith('test_'))
        self.assertFalse(d['double_run_detected'])
        for name, v in d['jobs'].items():
            if v.get('enabled', True):
                self.assertEqual(v['runs'], 1, name)

    def test_kill_restart_catch_up(self):
        # G7d: kill the supervisor mid-day, restart on the same store:
        # jobs due must run once, done jobs must not rerun.
        r = self._run('dry-run', '--fake-clock', '--minutes', '660',
                      '--kill-restart-at', '16:30')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertFalse(d['double_run_detected'])
        self.assertGreaterEqual(d['catch_ups'], 1)

    def test_two_supervisors_one_active(self):
        # G7e: two supervisors, one store: only one active.
        r = self._run('dry-run', '--fake-clock', '--minutes', '120',
                      '--supervisors', '2')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['max_simultaneous_active'], 1)

    def test_trial_dry_run_with_fake_box(self):
        # G7l: trial dry-run next to a fake box runner — isolation, budget,
        # no login, and no secret (CANARY) value anywhere in the output.
        r = self._run('dry-run', '--fake-clock', '--trial', '--with-fake-box',
                      '--minutes', '660', trial=True, canary=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('CANARY', r.stdout)
        d = json.loads(r.stdout)
        self.assertEqual(d['mode'], 'trial')
        self.assertEqual(d['namespace'], 'paper_render_trial_v1')
        self.assertEqual(d['box_rows_changed'], 0)
        self.assertEqual(d['shared_table_rows_changed'], 0)
        self.assertEqual(d['login_calls'], 0)
        self.assertLessEqual(d['trial_budget']['day_used'], 1000)
        self.assertLessEqual(d['trial_budget']['peak_minute'], 8)
        self.assertLessEqual(d['trial_budget']['peak_second'], 1)
        self.assertEqual(d['max_simultaneous_active'], 2)
        self.assertFalse(d['double_run_detected'])
        self.assertIs(d['jobs']['preflight']['enabled'], False)
        self.assertIs(d['jobs']['sync_history']['enabled'], False)


class FixtureCtlTests(unittest.TestCase):
    """--fixture modes of the trial commands (gate G7l). The fixture is a
    small paper_v2 baseline at bar 2026-11-18, built fresh per invocation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'fix.sqlite'
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                  'PAPER_TEST_DATABASE', 'PAPER_STATE_DIR',
                  'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, *argv):
        env = dict(os.environ)
        env.update({
            'PAPER_RUNTIME': 'render',
            'RENDER_MODE': 'trial',
            'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1',
            'PAPER_TEST_DATABASE': str(self.db_path),
            'PAPER_STATE_DIR': self.tmp.name,
        })
        return subprocess.run(
            [sys.executable, str(CTL), *argv],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(HERE.parent))

    def test_trial_init_fixture(self):
        r = self._run('trial-init', '--namespace', 'paper_render_trial_v1',
                      '--bar', '2026-11-18', '--fixture')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertIs(d['watermarks_match_paper_v2'], True)

    def test_trial_init_fixture_bar_after_completed_day(self):
        r = self._run('trial-init', '--namespace', 'paper_render_trial_v1',
                      '--bar', '2026-12-31', '--fixture')
        self.assertNotEqual(r.returncode, 0)

    def test_trial_compare_fixture(self):
        r = self._run('trial-compare', '--namespace', 'paper_render_trial_v1',
                      '--session', '2026-11-20', '--fixture')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['day'], '2026-11-20')

    def test_trial_purge_fixture_refuses_live(self):
        r = self._run('trial-purge', '--namespace', 'paper_v2',
                      '--yes', '--fixture')
        self.assertNotEqual(r.returncode, 0)

    def test_trial_purge_fixture_dry_run_default(self):
        r = self._run('trial-purge', '--namespace', 'paper_render_trial_v1',
                      '--fixture')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertIs(d['dry_run'], True)

    def test_trial_purge_fixture_yes_isolated(self):
        r = self._run('trial-purge', '--namespace', 'paper_render_trial_v1',
                      '--yes', '--fixture')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['other_namespaces_changed'], 0)

    def test_budget_check_fixture(self):
        r = self._run('budget-check', '--fixture-trial', '1000',
                      '--fixture-box', '4000')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertIs(d['within_limits'], True)

    def test_budget_check_fixture_box_over(self):
        r = self._run('budget-check', '--fixture-trial', '1000',
                      '--fixture-box', '4001')
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()
