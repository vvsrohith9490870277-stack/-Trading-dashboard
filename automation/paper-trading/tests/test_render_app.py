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


class HealthFieldsTests(unittest.TestCase):
    """Task 2 (render-deploy-02): /health carries every brief-01
    Task 6.3 field, with exactly those names."""

    # Top-level keys a live snapshot must have (trial adds two more).
    # 'breakers' is the Task 2 (render-deploy-03) crash-loop breaker map.
    LIVE_KEYS = {
        'ok', 'mode', 'login_allowed', 'state', 'namespace', 'role',
        'runner_id', 'epoch', 'uptime_s', 'now_ist', 'session_open',
        'calendar_valid_through', 'calendar_days_left', 'worker',
        'last_tick_ist', 'last_lease_renew_age_s', 'jobs_today',
        'last_job', 'rss_mb', 'children_rss_mb', 'fyers_budget', 'auth',
        'git_sha', 'version', 'breakers',
        # kept from the original snapshot:
        'status', 'active_runner', 'lease', 'jobs', 'uptime_seconds',
        'memory_mb',
    }

    def _snap(self, mode='live', **kw):
        args = dict(mode=mode, namespace='paper_v2',
                    active_runner='render', lease_held=True,
                    lease={'owner_id': 'render:h:1:1', 'epoch': 3},
                    worker={'state': 'running', 'pid': 123, 'rss_mb': 200,
                            'restarts': 0},
                    jobs={'scan': '2026-11-20T08:45:00'},
                    uptime_seconds=60, memory_mb=150)
        args.update(kw)
        return rh.build_snapshot(**args)

    def test_health_has_all_brief_fields(self):
        live = self._snap(mode='live')
        self.assertEqual(set(live), self.LIVE_KEYS)
        trial = self._snap(mode='trial')
        self.assertEqual(set(trial),
                         self.LIVE_KEYS | {'trial_budget',
                                           'box_conflict_check'})
        # worker subkeys (brief 6.3): state, pid, restarts_10m,
        # last_heartbeat_age_s (plus the kept rss_mb/restarts)
        for snap in (live, trial):
            self.assertEqual(
                set(snap['worker']),
                {'state', 'pid', 'rss_mb', 'restarts', 'restarts_10m',
                 'last_heartbeat_age_s'})
            self.assertEqual(set(snap['fyers_budget']),
                             {'day_used', 'per_day_limit'})
            self.assertEqual(set(snap['auth']), {'circuit_open'})
        self.assertEqual(set(trial['trial_budget']),
                         {'day_used', 'day_cap', 'minute_cap'})
        self.assertEqual(set(trial['box_conflict_check']),
                         {'paper_v2_heartbeat_present',
                          'paper_v2_heartbeat_age_s'})
        # the keyword form a monitor matches (documented): "ok": true
        self.assertIn('"ok": true', json.dumps(live))
        self.assertIn('"ok": true', json.dumps(trial))

    def test_health_ok_matches_http_status(self):
        ok_snap = self._snap(mode='live')
        code, payload = rh.health_response(ok_snap)
        self.assertEqual(code, 200)
        self.assertIs(payload['ok'], True)
        bad_snap = self._snap(mode='live', active_runner='render',
                              lease_held=False, lease=None,
                              worker={'state': 'stopped'})
        code, payload = rh.health_response(bad_snap)
        self.assertEqual(code, 503)
        self.assertIs(payload['ok'], False)

    def test_health_login_allowed_false_in_trial_true_in_live_with_creds(self):
        old = {}
        for k in ('FYERS_AUTO_LOGIN', 'FYERS_CLIENT_ID', 'FYERS_SECRET_ID',
                  'FYERS_PIN', 'FYERS_TOTP_SECRET', 'RENDER_GIT_COMMIT'):
            old[k] = os.environ.pop(k, None)
        os.environ.update({
            'FYERS_AUTO_LOGIN': '1', 'FYERS_CLIENT_ID': 'c',
            'FYERS_SECRET_ID': 's', 'FYERS_PIN': 'p',
            'FYERS_TOTP_SECRET': 't'})
        try:
            live = self._snap(mode='live')
            self.assertIs(live['login_allowed'], True)
            trial = self._snap(mode='trial')
            self.assertIs(trial['login_allowed'], False)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_health_no_secret_values(self):
        old = {}
        for k in ('FYERS_AUTO_LOGIN', 'FYERS_CLIENT_ID', 'FYERS_SECRET_ID',
                  'FYERS_PIN', 'FYERS_TOTP_SECRET', 'TURSO_AUTH_TOKEN',
                  'TURSO_DATABASE_URL', 'TOKEN_ENC_KEY'):
            old[k] = os.environ.pop(k, None)
        os.environ.update({
            'FYERS_AUTO_LOGIN': '1', 'FYERS_CLIENT_ID': 'CANARY_CLIENT',
            'FYERS_SECRET_ID': 'CANARY_SECRET', 'FYERS_PIN': 'CANARY_PIN',
            'FYERS_TOTP_SECRET': 'CANARY_TOTP',
            'TURSO_AUTH_TOKEN': 'CANARY_TURSO_TOKEN',
            'TURSO_DATABASE_URL': 'libsql://CANARY_TURSO_HOST',
            'TOKEN_ENC_KEY': 'CANARY_ENC_KEY'})
        try:
            for mode in ('live', 'trial'):
                body = json.dumps(self._snap(mode=mode))
                for canary in ('CANARY_CLIENT', 'CANARY_SECRET',
                               'CANARY_PIN', 'CANARY_TOTP',
                               'CANARY_TURSO_TOKEN', 'CANARY_TURSO_HOST',
                               'CANARY_ENC_KEY'):
                    self.assertNotIn(canary, body, mode)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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

    def test_supervisor_trial_health_keys(self):
        # G7n at unit level: the real process in trial mode serves every
        # brief 6.3 key, login_allowed false, and the "ok": true keyword.
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)],
            env=self._env(RENDER_MODE='trial',
                          PAPER_NAMESPACE_OVERRIDE='paper_render_trial_v1'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        code, payload = self._wait_health('idle_not_active_runner')
        self.assertEqual(code, 200, payload)
        self.assertIs(payload['ok'], True)
        self.assertEqual(payload['mode'], 'trial')
        self.assertEqual(payload['namespace'], 'paper_render_trial_v1')
        self.assertIs(payload['login_allowed'], False)
        self.assertIn('trial_budget', payload)
        self.assertIn('box_conflict_check', payload)
        for key in ('state', 'role', 'runner_id', 'epoch', 'uptime_s',
                    'now_ist', 'session_open', 'calendar_valid_through',
                    'calendar_days_left', 'last_tick_ist',
                    'last_lease_renew_age_s', 'jobs_today', 'last_job',
                    'rss_mb', 'children_rss_mb', 'fyers_budget', 'auth',
                    'git_sha', 'version'):
            self.assertIn(key, payload, key)
        self.assertIn('restarts_10m',
                      payload['worker'])
        self.assertIn('last_heartbeat_age_s', payload['worker'])
        raw = urllib.request.urlopen(
            'http://127.0.0.1:%d/health' % self.port, timeout=2).read()
        self.assertIn(b'"ok": true', raw)

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


class JobGuardTests(unittest.TestCase):
    """Task 2 (render-deploy-03): per-job memory guard + crash-loop
    breaker. The guard kills the job's process group over the cap and
    records failed/memory_cap; the breaker counts starts from
    paper_job_runs, stops scheduling for the rest of the IST day, and
    /health stays 200 with state degraded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'jobs.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        store = rs.SqliteStore(self.path)
        rs.migrate(store)
        return store

    def _alloc_child(self, mb=80):
        # Allocates ~mb of RAM, then sleeps: over a small cap, under a
        # large one. The RSS includes the interpreter (~15 MB).
        return [sys.executable, '-c',
                'import time; b = bytearray(%d * 1024 * 1024);'
                ' time.sleep(30)' % mb]

    def test_job_rss_cap_kills_child(self):
        import render_app as ra
        res = ra.run_job_streaming(self._alloc_child(80), None,
                                   timeout=30, rss_cap_mb=50)
        self.assertEqual(res.get('killed_reason'), 'memory_cap')
        self.assertEqual(res.get('rss_cap_mb'), 50)
        self.assertNotEqual(res['exit_code'], 0)

    def test_job_rss_cap_records_failed(self):
        import render_app as ra
        import render_jobs as rj
        store = self._store()
        try:
            out = rj.run_job(
                store, 'paper_v2', 'scan', '2026-11-20',
                owner_id='render:h:1:1',
                runner=lambda name: ra.run_job_streaming(
                    self._alloc_child(80), None, timeout=30,
                    rss_cap_mb=50))
            self.assertEqual(out.get('killed_reason'), 'memory_cap')
            rows = store.execute(
                "SELECT status, exit_code, result_json FROM paper_job_runs"
                " WHERE namespace='paper_v2' AND job='scan'")
            self.assertEqual(rows[0]['status'], 'failed')
            self.assertIn('memory_cap', rows[0]['result_json'])
        finally:
            store.close()

    def _sup_with_breaker(self, store, breaker_open=True):
        import render_app as ra
        import render_jobs as rj
        if breaker_open:
            # Seed the breaker with 3 killed starts (the incident: the
            # scan child is killed, it does not succeed). A failed run
            # does not count in last_run_times, so the job is still due
            # — and the breaker must stop it from starting again.
            for _ in range(3):
                rj.run_job(store, 'paper_v2', 'scan', '2026-11-20',
                           owner_id='render:h:1:1',
                           runner=lambda name: {
                               'exit_code': -9, 'stdout_tail': '',
                               'stderr_tail': '',
                               'killed_reason': 'memory_cap',
                               'rss_cap_mb': 350})
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.store = store
        sup.namespace = 'paper_v2'
        sup.mode = 'live'
        sup.breakers = {}
        sup._job_thread = None
        return sup

    def _due_scan_schedule(self):
        # A schedule whose scan slot was 5 minutes ago (inside the
        # 30-min catch-up window), so the scan is due right now.
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        now = _dt.now(_ZI('Asia/Kolkata')) - __import__(
            'datetime').timedelta(minutes=5)
        return {'timezone': 'Asia/Kolkata', 'catch_up_minutes': 30,
                'jobs': [{'name': 'scan', 'time': now.strftime('%H:%M'),
                          'enabled': True}]}

    def test_breaker_stops_scheduling(self):
        import render_app as ra
        import render_jobs as rj
        import render_schedule as rsch
        store = self._store()
        try:
            sup = self._sup_with_breaker(store, breaker_open=True)
            self.assertIn('scan', rj.open_breakers(
                store, 'paper_v2', '2026-11-20',
                max_starts=3, window_min=30))
            orig = rsch.load_schedule
            rsch.load_schedule = lambda mode='live': \
                self._due_scan_schedule()
            try:
                sup._manage_jobs()
            finally:
                rsch.load_schedule = orig
            self.assertIsNone(sup._job_thread,
                              'breaker-open job must not start')
            self.assertIn('scan', sup.breakers)
        finally:
            store.close()

    def test_health_degraded_with_breakers(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True,
            lease={'owner_id': 'render:h:1:1', 'epoch': 1},
            worker={'state': 'running', 'pid': 123, 'rss_mb': 200},
            jobs={'scan': '2026-11-20T08:45:00'},
            uptime_seconds=60, memory_mb=150,
            breakers={'scan': {'starts': 3, 'window_min': 30,
                               'max_starts': 3}})
        code, payload = rh.health_response(snap)
        self.assertEqual(code, 200, payload)
        self.assertIs(payload['ok'], True)
        self.assertEqual(payload['status'], 'degraded')
        self.assertIn('scan', payload['breakers'])
        # The same inputs without a breaker are ok, not degraded.
        snap2 = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True,
            lease={'owner_id': 'render:h:1:1', 'epoch': 1},
            worker={'state': 'running', 'pid': 123, 'rss_mb': 200},
            jobs={'scan': '2026-11-20T08:45:00'},
            uptime_seconds=60, memory_mb=150)
        self.assertEqual(snap2['status'], 'ok')

    def test_incident_replay_breaker(self):
        # Incident replay: the scan child is killed 3x in 10 minutes
        # (memory_cap), with an instance restart between starts. After
        # the 3rd kill the breaker is open, the 4th start never happens,
        # the worker keeps running, and /health is degraded at HTTP 200.
        import render_app as ra
        import render_jobs as rj
        import render_schedule as rsch

        def kill_run(store):
            return rj.run_job(
                store, 'paper_v2', 'scan', '2026-11-20',
                owner_id='render:h:1:1',
                runner=lambda name: {'exit_code': -9,
                                     'stdout_tail': '',
                                     'stderr_tail': '',
                                     'killed_reason': 'memory_cap',
                                     'rss_cap_mb': 350})

        store = self._store()
        try:
            for _ in range(3):
                kill_run(store)
                # instance restart: close and reopen the store
                store.close()
                store = self._store()
            self.assertTrue(rj.breaker_open(
                store, 'paper_v2', 'scan', '2026-11-20',
                max_starts=3, window_min=30))
            sup = ra.Supervisor.__new__(ra.Supervisor)
            sup.store = store
            sup.namespace = 'paper_v2'
            sup.mode = 'live'
            sup.owner_id = 'render:h:1:1'
            sup.config = {'calendar_valid_from': '2026-01-01',
                          'calendar_valid_through': '2026-12-31',
                          'regular_session': ['09:15', '15:30'],
                          'holidays': [], 'special_sessions': {}}
            sup.lease_held = True
            sup.stopping = False
            sup.store_error = None
            sup.started_at = time.time() - 60
            sup.last_tick_at = time.time()
            sup.last_lease_renew_at = time.time()
            sup.last_flag = 'render'
            sup.breakers = {}
            sup._job_thread = None

            class FakeWorker:
                def describe(self):
                    return {'state': 'running', 'pid': 123, 'rss_mb': 95,
                            'restarts': 0, 'restarts_10m': 0,
                            'last_heartbeat_age_s': 5}
            sup.worker = FakeWorker()
            # No 4th start: the due scan is skipped while the breaker
            # is open.
            orig = rsch.load_schedule
            rsch.load_schedule = lambda mode='live': \
                self._due_scan_schedule()
            try:
                sup._manage_jobs()
            finally:
                rsch.load_schedule = orig
            self.assertIsNone(sup._job_thread)
            # /health: worker running, state degraded, HTTP 200.
            snap = sup.snapshot('render')
            self.assertEqual(snap['worker']['state'], 'running')
            self.assertEqual(snap['status'], 'degraded')
            self.assertIs(snap['ok'], True)
            code, _payload = rh.health_response(snap)
            self.assertEqual(code, 200)
            self.assertIn('scan', snap['breakers'])
        finally:
            store.close()


class WorkerWindowTests(unittest.TestCase):
    """Senior fix 20 Nov: the worker starts only in the session window."""

    def _sup(self):
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.config = {'calendar_valid_from': '2026-01-01',
                      'calendar_valid_through': '2026-12-31',
                      'regular_session': ['09:15', '15:30'],
                      'holidays': [], 'special_sessions': {}}
        return sup

    def test_worker_window(self):
        import os
        from datetime import datetime
        import render_app as ra
        old = os.environ.pop('PAPER_FAKE_WORKER', None)
        try:
            sup = self._sup()
            d = lambda h, m: datetime(2026, 11, 20, h, m, tzinfo=ra.IST)
            self.assertFalse(sup._worker_window_open(d(7, 40)))
            self.assertTrue(sup._worker_window_open(d(9, 13)))
            self.assertTrue(sup._worker_window_open(d(12, 0)))
            self.assertFalse(sup._worker_window_open(d(15, 30)))
            self.assertFalse(sup._worker_window_open(
                datetime(2026, 11, 21, 10, 0, tzinfo=ra.IST)))  # Saturday
        finally:
            if old is not None:
                os.environ['PAPER_FAKE_WORKER'] = old
