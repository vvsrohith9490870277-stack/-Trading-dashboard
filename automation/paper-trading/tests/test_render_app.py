"""Tests for Task 6: supervisor, web server, health and SIGTERM (render_app)."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_health as rh
import render_lease as rl
import render_state as rs

APP = HERE.parent / 'render_app.py'


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HealthTests(unittest.TestCase):
    def test_health_ok_when_idle(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='box',
            lease_held=False, lease=None, worker={'state': 'stopped'},
            jobs={}, uptime_seconds=10, memory_mb=120)
        code, payload = rh.health_response(snap)
        self.assertEqual(code, 200)
        self.assertEqual(payload['status'], 'idle_not_active_runner')

    def test_health_200_when_render_active_and_lease_held(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True,
            lease={'owner_id': 'render:h:1:1', 'epoch': 1},
            worker={'state': 'running', 'pid': 123, 'rss_mb': 200},
            jobs={'scan': '2026-11-20T08:45:00'},
            uptime_seconds=60, memory_mb=150)
        code, payload = rh.health_response(snap)
        self.assertEqual(code, 200)
        self.assertEqual(payload['status'], 'ok')

    def test_health_503_when_lease_lost(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=False, lease=None, worker={'state': 'stopped'},
            jobs={}, uptime_seconds=60, memory_mb=150)
        code, payload = rh.health_response(snap)
        self.assertEqual(code, 503)
        self.assertEqual(payload['status'], 'lease_lost')

    def test_memory_guard_threshold(self):
        self.assertTrue(rh.memory_over_limit(500))
        self.assertTrue(rh.memory_over_limit(451))
        self.assertFalse(rh.memory_over_limit(450))
        self.assertFalse(rh.memory_over_limit(100))


class CheckNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run_check(self, extra_env):
        env = dict(os.environ)
        env.update(extra_env)
        env['PAPER_RUNTIME'] = 'render'
        return subprocess.run(
            [sys.executable, str(APP), '--check-namespace-only'],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(HERE.parent))

    def test_check_namespace_only_live_ok(self):
        # config.json on this branch carries paper_v2; live mode must pass.
        r = self._run_check({'RENDER_MODE': 'live'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('paper_v2', r.stdout)

    def test_check_namespace_only_trial_bad(self):
        r = self._run_check({'RENDER_MODE': 'trial',
                             'PAPER_NAMESPACE_OVERRIDE': 'paper_v2'})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_check_namespace_only_trial_ok(self):
        r = self._run_check({'RENDER_MODE': 'trial',
                             'PAPER_NAMESPACE_OVERRIDE':
                             'paper_render_trial_v1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('paper_render_trial_v1', r.stdout)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'app.sqlite'
        self.store = rs.SqliteStore(self.db_path)
        rs.migrate(self.store)
        self.port = _free_port()
        self.proc = None
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                  'PAPER_TEST_DATABASE', 'PAPER_STATE_DIR', 'PORT',
                  'PAPER_FAKE_WORKER', 'RENDER_OWNER_ID'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.store.close()
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
            'PAPER_FAKE_WORKER': '1',
            'PORT': str(self.port),
        })
        env.update(kw)
        return env

    def _wait_health(self, expect_status, timeout=30):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        'http://127.0.0.1:%d/health' % self.port,
                        timeout=2) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                payload = json.loads(e.read())
                code = e.code
            except Exception as ex:
                last = ex
                time.sleep(0.5)
                continue
            else:
                code = resp.status
            if payload.get('status') == expect_status:
                return code, payload
            last = payload
            time.sleep(0.5)
        raise AssertionError(
            'health never reached %r: %r' % (expect_status, last))

    def test_supervisor_runs_fake_worker_and_serves_health(self):
        rl.set_active_runner(self.store, 'paper_v2', 'render')
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)], env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        code, payload = self._wait_health('ok')
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['namespace'], 'paper_v2')
        # the fake worker must be running and the lease held
        self.assertEqual(payload['worker']['state'], 'running')
        lease = rl.get_lease(self.store, 'paper_v2')
        self.assertIsNotNone(lease)
        self.assertEqual(payload['lease']['owner_id'], lease['owner_id'])

    def test_supervisor_idles_when_box_active(self):
        rl.set_active_runner(self.store, 'paper_v2', 'box')
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)], env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        code, payload = self._wait_health('idle_not_active_runner')
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload['status'], 'idle_not_active_runner')
        self.assertEqual(payload['worker']['state'], 'stopped')
        self.assertIsNone(rl.get_lease(self.store, 'paper_v2'))

    def test_sigterm_exits_zero_within_25s(self):
        rl.set_active_runner(self.store, 'paper_v2', 'render')
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)], env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        code, payload = self._wait_health('ok')
        self.assertEqual(code, 200, payload)
        started = time.time()
        self.proc.send_signal(signal.SIGTERM)
        rc = self.proc.wait(timeout=30)
        elapsed = time.time() - started
        self.assertEqual(rc, 0, self.proc.stdout.read())
        self.assertLess(elapsed, 25)


if __name__ == '__main__':
    unittest.main()
