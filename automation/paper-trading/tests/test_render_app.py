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
from datetime import datetime, timezone
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
    # 'breakers' is the Task 2 (render-deploy-03) crash-loop breaker map;
    # 'store_ok'/'store_age_s'/'reasons'/'peak_job_rss_mb'/
    # 'last_job_peak_rss_mb'/'job_rss_cap_mb' are the Task 3 fields.
    LIVE_KEYS = {
        'ok', 'mode', 'login_allowed', 'state', 'namespace', 'role',
        'runner_id', 'epoch', 'uptime_s', 'now_ist', 'session_open',
        'calendar_valid_through', 'calendar_days_left', 'worker',
        'last_tick_ist', 'last_lease_renew_age_s', 'jobs_today',
        'last_job', 'rss_mb', 'children_rss_mb', 'fyers_budget', 'auth',
        'git_sha', 'version', 'breakers', 'store_ok', 'store_age_s',
        'store_reads_total', 'reasons', 'peak_job_rss_mb',
        'last_job_peak_rss_mb',
        'job_rss_cap_mb',
        # Read-only support monitor (brief 23 Nov 2026).
        'support_monitor',
        # Full Support Agent on Render (brief Nov 2026).
        'support_agent',
        # Fyers token auto-refresh (brief 23 Nov 2026).
        'fyers_auth',
        # Full daily schedule (brief 23 Nov 2026).
        'schedule',
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
                                           'box_conflict_check',
                                           'quote_probe'})
        # worker subkeys (brief 6.3): state, pid, restarts_10m,
        # last_heartbeat_age_s (plus the kept rss_mb/restarts), and
        # Fix 2 (Grok box test): status, consecutive_failures,
        # last_quote_ok_at (null until the refresher fills them).
        for snap in (live, trial):
            self.assertEqual(
                set(snap['worker']),
                {'state', 'pid', 'rss_mb', 'restarts', 'restarts_10m',
                 'last_heartbeat_age_s', 'status',
                 'consecutive_failures', 'last_quote_ok_at'})
            self.assertEqual(set(snap['fyers_budget']),
                             {'day_used', 'per_day_limit'})
            self.assertEqual(set(snap['auth']), {'circuit_open'})
        self.assertEqual(set(trial['trial_budget']),
                         {'day_used', 'day_cap', 'minute_cap'})
        self.assertEqual(set(trial['box_conflict_check']),
                         {'paper_v2_heartbeat_present',
                          'paper_v2_heartbeat_age_s',
                          'paper_v2_active_runner'})
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


class _CountingStore:
    """Wraps a store and counts execute() calls (the per-request read
    test). Delegates everything else to the wrapped store."""

    def __init__(self, store):
        self._store = store
        self.count = 0

    def execute(self, sql, args=()):
        self.count += 1
        return self._store.execute(sql, args)

    def execute_many(self, statements):
        return self._store.execute_many(statements)

    def affected_rows(self, sql, args=()):
        return self._store.affected_rows(sql, args)

    def close(self):
        return self._store.close()


class HealthStoreTests(unittest.TestCase):
    """Task 3 (render-deploy-03): /health store-read fields come from a
    background refresher (at most once per RENDER_HEALTH_REFRESH_S),
    never from a per-request store read. Values that cannot be computed
    stay null with a reasons map, never a made-up zero."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'health.sqlite'
        self._old_refresh = os.environ.get('RENDER_HEALTH_REFRESH_S')
        os.environ['RENDER_HEALTH_REFRESH_S'] = '60'

    def tearDown(self):
        if self._old_refresh is None:
            os.environ.pop('RENDER_HEALTH_REFRESH_S', None)
        else:
            os.environ['RENDER_HEALTH_REFRESH_S'] = self._old_refresh
        self.tmp.cleanup()

    def _store(self):
        store = rs.SqliteStore(self.path)
        rs.migrate(store)
        return store

    def _today(self):
        from datetime import datetime
        return datetime.now(rh.IST).date().isoformat()

    def _seed(self, store, namespace='paper_render_trial_v1',
              with_monitor=True):
        """Seed a store the way production looks: three job runs today
        (scan ok, history_sync ok, eod killed by the memory guard),
        a worker heartbeat, both budget counters, the box lease + flag
        for paper_v2, and this namespace's own lease row."""
        import render_jobs as rj
        from datetime import datetime, timedelta, timezone
        today = self._today()
        rj.run_job(store, namespace, 'scan', today,
                   owner_id='render:h:1:1',
                   runner=lambda name: {'status': 'ok',
                                        'peak_rss_mb': 210})
        rj.run_job(store, namespace, 'history_sync', today,
                   owner_id='render:h:1:1',
                   runner=lambda name: {'status': 'ok',
                                        'peak_rss_mb': 95})
        rj.run_job(store, namespace, 'eod', today,
                   owner_id='render:h:1:1',
                   runner=lambda name: {'exit_code': -9, 'stdout_tail': '',
                                        'stderr_tail': '',
                                        'killed_reason': 'memory_cap',
                                        'rss_cap_mb': 350,
                                        'peak_rss_mb': 340})
        now_utc = datetime.now(timezone.utc)
        utc_s = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
        if with_monitor:
            # paper_monitor_health is a box table (schema.sql), not in
            # the render schema: the test creates it, as the box does
            # in production.
            store.execute(
                'CREATE TABLE IF NOT EXISTS paper_monitor_health('
                'namespace TEXT PRIMARY KEY, pid INTEGER NOT NULL,'
                'heartbeat_at TEXT NOT NULL, status TEXT NOT NULL,'
                'detail_json TEXT NOT NULL)')
            hb = datetime.now(rh.IST) - timedelta(seconds=12)
            store.execute(
                'INSERT INTO paper_monitor_health(namespace, pid,'
                ' heartbeat_at, status, detail_json) VALUES(?,?,?,?,?)',
                (namespace, 1234, hb.isoformat(), 'healthy',
                 json.dumps({'failures': 0})))
        for key, n in (('fyers_budget', 1234),
                       ('fyers_budget_trial', 57)):
            store.execute(
                'INSERT OR REPLACE INTO paper_runtime_kv(scope, key,'
                ' value, updated_at) VALUES(?,?,?,?)',
                ('_global', key, json.dumps(
                    {'day': today, 'requests': n, 'by_route': {},
                     'times': []}), utc_s))
        hb_box = (now_utc - timedelta(seconds=45)).strftime(
            '%Y-%m-%dT%H:%M:%S')
        store.execute(
            'INSERT OR REPLACE INTO paper_runner_lease(namespace,'
            ' owner_id, owner_kind, epoch, acquired_unix, expires_unix,'
            ' heartbeat_at, detail_json) VALUES(?,?,?,?,?,?,?,?)',
            ('paper_v2', 'box:h:1:1', 'box', 1,
             int(now_utc.timestamp()), int(now_utc.timestamp()) + 90,
             hb_box, '{}'))
        store.execute(
            'INSERT OR REPLACE INTO paper_runtime_kv(scope, key, value,'
            ' updated_at) VALUES(?,?,?,?)',
            ('paper_v2', 'active_runner', 'box', utc_s))
        store.execute(
            'INSERT OR REPLACE INTO paper_runner_lease(namespace,'
            ' owner_id, owner_kind, epoch, acquired_unix, expires_unix,'
            ' heartbeat_at, detail_json) VALUES(?,?,?,?,?,?,?,?)',
            (namespace, 'render:h:1:1', 'render', 3,
             int(now_utc.timestamp()), int(now_utc.timestamp()) + 90,
             utc_s, '{}'))
        return today

    def _sup(self, store, namespace='paper_render_trial_v1',
             mode='trial'):
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.store = store
        sup.namespace = namespace
        sup.mode = mode
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

        class FakeWorker:
            def describe(self):
                return {'state': 'running', 'pid': 123, 'rss_mb': 95,
                        'restarts': 0, 'restarts_10m': 0,
                        'last_heartbeat_age_s': None}
        sup.worker = FakeWorker()
        return sup

    def test_health_store_fields_filled_from_fake_store(self):
        store = self._store()
        try:
            today = self._seed(store)
            # A successful quote batch today, so last_quote_ok_at is
            # filled (Fix 2): the latest quote_batch_response event.
            from datetime import datetime as _dt, timezone as _tz
            store.execute(
                'CREATE TABLE IF NOT EXISTS paper_events('
                'namespace TEXT, event_type TEXT, session_day TEXT,'
                ' recorded_at TEXT, data_json TEXT)')
            store.execute(
                'INSERT INTO paper_events(namespace, event_type,'
                ' session_day, recorded_at, data_json)'
                ' VALUES(?,?,?,?,?)',
                ('paper_render_trial_v1', 'quote_batch_response', today,
                 _dt.now(_tz.utc).isoformat(), json.dumps(
                     {'response_received_at':
                      _dt.now(_tz.utc).isoformat(),
                      'symbols_count': 12})))
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            # A probe ran today, so quote_probe is filled (Task 1).
            import render_boot as rb
            rb.kv_put(store, 'paper_render_trial_v1', 'quote_probe',
                      {'status': 'ok', 'ltp_count': 2, 'http_status': 200,
                       'requests': 1})
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertEqual(snap['quote_probe']['status'], 'ok')
            self.assertEqual(snap['quote_probe']['ltp_count'], 2)
            # Worker heartbeat: seeded 12 s ago (allow drift).
            hb = snap['worker']['last_heartbeat_age_s']
            self.assertIsInstance(hb, int)
            self.assertGreaterEqual(hb, 0)
            self.assertLessEqual(hb, 30)
            # Worker status (Fix 2): healthy, 0 consecutive failures,
            # and a last successful quote just now.
            self.assertEqual(snap['worker']['status'], 'healthy')
            self.assertEqual(snap['worker']['consecutive_failures'], 0)
            self.assertIsNotNone(snap['worker']['last_quote_ok_at'])
            # Jobs today: 2 ok, 1 failed.
            self.assertEqual(snap['jobs_today'], {'ok': 2, 'failed': 1})
            # Last job: the eod, killed by the memory guard.
            last = snap['last_job']
            self.assertEqual(last['name'], 'eod')
            self.assertEqual(last['status'], 'failed')
            self.assertEqual(last['reason'], 'memory_cap')
            self.assertIsInstance(last['duration_s'], int)
            self.assertGreaterEqual(last['duration_s'], 0)
            self.assertTrue(last['started_at_ist'].startswith(today))
            # Budgets: the same counters the engine budget uses.
            self.assertEqual(snap['fyers_budget']['day_used'], 1234)
            self.assertEqual(snap['trial_budget']['day_used'], 57)
            # Box conflict: the box lease is heartbeating on paper_v2.
            box = snap['box_conflict_check']
            self.assertIs(box['paper_v2_heartbeat_present'], True)
            self.assertIsInstance(box['paper_v2_heartbeat_age_s'], int)
            self.assertLessEqual(box['paper_v2_heartbeat_age_s'], 120)
            self.assertEqual(box['paper_v2_active_runner'], 'box')
            # Peaks: the largest job-tree peak today, and the last
            # job's peak.
            self.assertEqual(snap['peak_job_rss_mb'], 340)
            self.assertEqual(snap['last_job_peak_rss_mb'], 340)
            self.assertEqual(snap['job_rss_cap_mb'], 350)
            # The refresher ran: store_ok true, age small, no reasons.
            self.assertIs(snap['store_ok'], True)
            self.assertIsInstance(snap['store_age_s'], int)
            self.assertLessEqual(snap['store_age_s'], 30)
            self.assertEqual(snap['reasons'], {})
            # Read counter (Task 4): the refresher sampled the store's
            # counter — an int, and positive: it includes this refresh's
            # own reads.
            self.assertIsInstance(snap['store_reads_total'], int)
            self.assertGreater(snap['store_reads_total'], 0)
            # The lease + jobs come from the cache too.
            self.assertEqual(snap['lease']['owner_id'], 'render:h:1:1')
            self.assertIn('scan', snap['jobs'])
            self.assertIn('history_sync', snap['jobs'])
            # ok:true is still the literal substring the monitor
            # matches.
            self.assertIn('"ok": true', json.dumps(snap))
        finally:
            store.close()

    def test_health_survives_store_failure(self):
        # A store that raises on every read: /health must still answer
        # 200, with store_ok false and the fields null with reasons.
        class BrokenStore:
            def execute(self, sql, args=()):
                raise RuntimeError('store_down')

            def execute_many(self, statements):
                raise RuntimeError('store_down')

            def close(self):
                pass
        sup = self._sup(BrokenStore())
        sup._refresh_health_store(force=True)
        snap = sup.snapshot('render')
        code, _payload = rh.health_response(snap)
        self.assertEqual(code, 200)
        self.assertIs(snap['store_ok'], False)
        self.assertIs(snap['last_job'], None)
        self.assertIs(snap['fyers_budget']['day_used'], None)
        self.assertIs(snap['trial_budget']['day_used'], None)
        self.assertIs(snap['worker']['last_heartbeat_age_s'], None)
        self.assertIs(snap['peak_job_rss_mb'], None)
        self.assertTrue(snap['reasons'])
        self.assertIn('jobs_today', snap['reasons'])
        self.assertIn('last_job', snap['reasons'])

    def test_health_does_not_query_store_per_request(self):
        store = self._store()
        try:
            self._seed(store)
            counting = _CountingStore(store)
            sup = self._sup(counting)
            sup._refresh_health_store(force=True)
            before = counting.count
            for _ in range(100):
                snap = sup.snapshot('render')
            self.assertEqual(snap['fyers_budget']['day_used'], 1234)
            # At most one more store read across 100 snapshots (a
            # refresh falling due); normally zero.
            self.assertLessEqual(counting.count - before, 1)
        finally:
            store.close()

    def test_health_no_secret_values(self):
        store = self._store()
        try:
            self._seed(store)
            # A canary that looks like a secret, planted where a
            # careless refresher would echo it: the token KV row and a
            # job's result_json. /health must never repeat it.
            store.execute(
                'INSERT INTO paper_runtime_kv(scope, key, value,'
                ' updated_at) VALUES(?,?,?,?)',
                ('_global', 'fyers_token',
                 'SECRET_CANARY_9f3a7b1c', '2026-11-20T00:00:00'))
            import render_jobs as rj
            rj.run_job(store, 'paper_render_trial_v1', 'scan',
                       self._today(), owner_id='render:h:1:1',
                       runner=lambda name: {
                           'status': 'ok',
                           'note': 'SECRET_CANARY_9f3a7b1c'})
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            raw = json.dumps(snap)
            self.assertNotIn('SECRET_CANARY_9f3a7b1c', raw)
            self.assertNotIn('libsql://', raw)
            self.assertNotIn('TURSO_AUTH_TOKEN', raw)
        finally:
            store.close()

    def test_health_trial_fields_only_in_trial_mode(self):
        store = self._store()
        try:
            self._seed(store)
            sup = self._sup(store, namespace='paper_v2', mode='live')
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertNotIn('trial_budget', snap)
            self.assertNotIn('box_conflict_check', snap)
            self.assertNotIn('quote_probe', snap)
            self.assertEqual(snap['fyers_budget']['day_used'], 1234)
            # Trial mode carries all three.
            sup_t = self._sup(store, mode='trial')
            sup_t._refresh_health_store(force=True)
            snap_t = sup_t.snapshot('render')
            self.assertIn('trial_budget', snap_t)
            self.assertIn('box_conflict_check', snap_t)
            self.assertIn('quote_probe', snap_t)
            self.assertIsNone(snap_t['quote_probe'])
            self.assertEqual(snap_t['trial_budget']['day_used'], 57)
        finally:
            store.close()

    def test_health_quote_probe_from_store(self):
        # Task 1 (one-shot quote probe): the refresher reads the probe
        # result the supervisor recorded in paper_runtime_kv and serves
        # it as /health.quote_probe (trial only). Absent is null with a
        # reason, never a made-up result.
        import render_boot as rb
        store = self._store()
        try:
            self._seed(store)
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertIsNone(snap['quote_probe'])
            self.assertEqual(snap['reasons'].get('quote_probe'),
                             'no_probe_run')
            probe = {'status': 'failed', 'ltp_count': 0,
                     'http_status': 401, 'fyers_code': None,
                     'error_code': 'token_unavailable_no_login',
                     'error_detail': 'no login in trial mode',
                     'duration_ms': 1200, 'requests': 1}
            rb.kv_put(store, 'paper_render_trial_v1', 'quote_probe', probe)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertEqual(snap['quote_probe'], probe)
            self.assertNotIn('quote_probe', snap['reasons'])
            # An unparseable row is null with a reason, not a crash.
            store.execute(
                'UPDATE paper_runtime_kv SET value=?'
                ' WHERE scope=? AND key=?',
                ('not json{', 'paper_render_trial_v1', 'quote_probe'))
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertIsNone(snap['quote_probe'])
            self.assertEqual(snap['reasons'].get('quote_probe'),
                             'probe_unparseable')
        finally:
            store.close()

    def test_health_refresher_reports_open_breaker(self):
        # The refresher must report the same open breakers the
        # scheduler sees (3 starts in the window): a breaker is
        # protective, so /health is degraded but ok:true, HTTP 200.
        # (Regression: the first version called job_starts() with a
        # row dict instead of (store, namespace, job, day), which
        # raised TypeError and silently left breakers empty.)
        import render_jobs as rj
        store = self._store()
        try:
            today = self._today()
            for _ in range(3):
                rj.run_job(store, 'paper_render_trial_v1', 'scan', today,
                           owner_id='render:h:1:1',
                           runner=lambda name: {
                               'exit_code': -9, 'stdout_tail': '',
                               'stderr_tail': '',
                               'killed_reason': 'memory_cap',
                               'rss_cap_mb': 350, 'peak_rss_mb': 340})
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertIn('scan', snap['breakers'])
            self.assertEqual(snap['breakers']['scan']['starts'], 3)
            self.assertEqual(snap['state'], 'degraded')
            self.assertEqual(snap['status'], 'degraded')
            self.assertIs(snap['ok'], True)
            code, _payload = rh.health_response(snap)
            self.assertEqual(code, 200)
        finally:
            store.close()

    def test_health_missing_monitor_table_leaves_only_heartbeat_null(self):
        # paper_monitor_health is a box table, not in the render
        # schema: a fresh render store has no such table. The refresher
        # must leave only the heartbeat null (with a reason), not fail
        # the whole refresh.
        store = self._store()
        try:
            self._seed(store, with_monitor=False)
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertIs(snap['store_ok'], True)
            self.assertIs(snap['worker']['last_heartbeat_age_s'], None)
            self.assertEqual(snap['reasons'].get(
                'worker_last_heartbeat_age_s'), 'table_missing')
            self.assertEqual(snap['fyers_budget']['day_used'], 1234)
            self.assertEqual(snap['jobs_today'], {'ok': 2, 'failed': 1})
        finally:
            store.close()

    def test_health_worker_status_failures_and_last_quote_ok(self):
        # Fix 2 (Grok box test): /health must show the worker is in
        # backoff, not just ok:true/restarts 0. The watch log rows
        # 09:29-11:26 were marked UNVERIFIED because /health hid the
        # backoff. The three fields come from the store refresher:
        # status + consecutive failures from the heartbeat row,
        # last_quote_ok_at from the latest quote_batch_response event.
        store = self._store()
        try:
            self._seed(store)
            ns = 'paper_render_trial_v1'
            store.execute(
                'UPDATE paper_monitor_health SET status=?,'
                ' detail_json=? WHERE namespace=?',
                ('backoff', json.dumps(
                    {'failures': 153, 'error': 'RuntimeError',
                     'error_detail': 'connection reset by peer'}),
                 ns))
            store.execute(
                'CREATE TABLE IF NOT EXISTS paper_events('
                'namespace TEXT, event_type TEXT, session_day TEXT,'
                ' recorded_at TEXT, data_json TEXT)')
            store.execute(
                'INSERT INTO paper_events(namespace, event_type,'
                ' session_day, recorded_at, data_json)'
                ' VALUES(?,?,?,?,?)',
                (ns, 'quote_batch_response', self._today(),
                 '2026-11-20T05:30:00+00:00', json.dumps(
                     {'response_received_at': '2026-11-20T05:30:00+00:00',
                      'symbols_count': 12})))
            # An older failed batch must not move last_quote_ok_at.
            store.execute(
                'INSERT INTO paper_events(namespace, event_type,'
                ' session_day, recorded_at, data_json)'
                ' VALUES(?,?,?,?,?)',
                (ns, 'quote_batch_failed', self._today(),
                 '2026-11-20T05:31:00+00:00', json.dumps(
                     {'error_code': 'RuntimeError'})))
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertEqual(snap['worker']['status'], 'backoff')
            self.assertEqual(snap['worker']['consecutive_failures'], 153)
            self.assertEqual(snap['worker']['last_quote_ok_at'],
                             '2026-11-20T11:00:00+05:30')
        finally:
            store.close()

    def test_health_worker_fields_null_when_uncomputable(self):
        # No heartbeat row and no quote response: the three fields
        # stay null with reasons, never a made-up zero or 'healthy'.
        store = self._store()
        try:
            self._seed(store, with_monitor=False)
            sup = self._sup(store)
            sup._refresh_health_store(force=True)
            snap = sup.snapshot('render')
            self.assertIs(snap['worker']['status'], None)
            self.assertIs(snap['worker']['consecutive_failures'], None)
            self.assertIs(snap['worker']['last_quote_ok_at'], None)
            self.assertIn('worker_status', snap['reasons'])
            self.assertIn('worker_consecutive_failures', snap['reasons'])
            self.assertIn('worker_last_quote_ok_at', snap['reasons'])
        finally:
            store.close()


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


class SupervisorTickTests(unittest.TestCase):
    """Read-usage guard: the supervisor main loop ticks every 60 s
    (was 5 s), so the /health store refresher and worker checks run
    far less often."""

    def test_tick_is_60_seconds(self):
        import inspect

        import render_app as ra
        self.assertEqual(ra.SUPERVISOR_TICK_SECONDS, 60)
        # The main loop must sleep the 60 s tick from the constant, not
        # a literal: reverting the loop to time.sleep(5) fails this test.
        # The sleep is sliced into 1 s waits so SIGTERM (which sets
        # sup.stopping) is honoured within ~1 s — a single 60 s
        # time.sleep() would retry after the handler (PEP 475) and
        # delay shutdown past the 25 s exit guarantee.
        src = inspect.getsource(ra.main)
        self.assertIn('range(SUPERVISOR_TICK_SECONDS)', src)
        self.assertIn('time.sleep(1)', src)
        self.assertNotIn('time.sleep(5)', src)
        self.assertNotIn('time.sleep(SUPERVISOR_TICK_SECONDS)', src)



class HealthRefreshTests(unittest.TestCase):
    """Read-usage guard: RENDER_HEALTH_REFRESH_S defaults to 300 s
    (was 60 s); an invalid value falls back to the same 300 s default
    as an unset one."""

    def setUp(self):
        self._old = os.environ.pop('RENDER_HEALTH_REFRESH_S', None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop('RENDER_HEALTH_REFRESH_S', None)
        else:
            os.environ['RENDER_HEALTH_REFRESH_S'] = self._old

    def test_default_is_300_seconds(self):
        self.assertEqual(rh.health_refresh_seconds(), 300)

    def test_explicit_value_is_respected(self):
        os.environ['RENDER_HEALTH_REFRESH_S'] = '60'
        self.assertEqual(rh.health_refresh_seconds(), 60)

    def test_invalid_value_falls_back_to_300(self):
        os.environ['RENDER_HEALTH_REFRESH_S'] = 'not-a-number'
        self.assertEqual(rh.health_refresh_seconds(), 300)

    def test_value_is_clamped_to_at_least_1(self):
        os.environ['RENDER_HEALTH_REFRESH_S'] = '0'
        self.assertEqual(rh.health_refresh_seconds(), 1)

class JobGuardTests(unittest.TestCase):
    """Task 2 (render-deploy-03): per-job memory guard + crash-loop
    breaker. The guard kills the job's process group over the cap and
    records failed/memory_cap; the breaker counts starts from
    paper_job_runs, stops scheduling while it is open (a rolling
    30-minute window — I23 on Render corrected the docstring, which
    used to say "rest of the IST day"), and /health stays 200 with
    state degraded."""

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

    def test_breaker_docstring_describes_rolling_window(self):
        # I23 on Render: the docstring must say rolling window, not
        # "rest of the IST day" — a day-long lockout would undo the
        # retry-until-EOD fix.
        import render_jobs as rj
        self.assertNotIn('rest of the IST day', rj.__doc__)
        self.assertIn('rolling window', rj.__doc__)

    def test_breaker_closes_when_window_passes(self):
        # I23 on Render: three starts in the window open the breaker;
        # once the oldest start ages out of the rolling 30-minute
        # window, it closes again (the job may be scheduled).
        import render_jobs as rj
        import time
        store = self._store()
        try:
            day = self._today_ist()
            now_unix = int(time.time())
            starts = [now_unix - 29 * 60, now_unix - 14 * 60, now_unix]
            store.execute(
                'INSERT INTO paper_job_runs(namespace, job, session_day,'
                ' attempt, status, owner_id, claimed_unix,'
                ' lease_until_unix, started_at, result_json)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('paper_v2', 'scan', day, len(starts), 'error',
                 'render:h:1:1', starts[-1], starts[-1] + 90,
                 datetime.fromtimestamp(
                     starts[-1], tz=timezone.utc).strftime(
                         '%Y-%m-%dT%H:%M:%S'),
                 json.dumps({'starts': starts})))
            self.assertTrue(rj.breaker_open(
                store, 'paper_v2', 'scan', day, now_unix=now_unix))
            self.assertFalse(rj.breaker_open(
                store, 'paper_v2', 'scan', day,
                now_unix=now_unix + 2 * 60))
        finally:
            store.close()

    def test_job_rss_cap_kills_child(self):
        import render_app as ra
        res = ra.run_job_streaming(self._alloc_child(80), None,
                                   timeout=30, rss_cap_mb=50)
        self.assertEqual(res.get('killed_reason'), 'memory_cap')
        self.assertEqual(res.get('rss_cap_mb'), 50)
        self.assertNotEqual(res['exit_code'], 0)
        # The guard's sampled peak: it must have exceeded the cap, or
        # the child would not have been killed.
        self.assertGreaterEqual(res.get('peak_rss_mb', 0), 50)

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

    def test_job_runner_applies_per_job_limits(self):
        # _job_runner passes a job's own timeout_s / rss_cap_mb through
        # to run_job_streaming (adaptive_eod only); a job without them
        # keeps the defaults (timeout 1800, rss_cap_mb None so the
        # global RENDER_JOB_RSS_CAP_MB applies inside run_job_streaming).
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.mode = 'live'
        sup.owner_id = 'test'
        seen = {}

        def fake_streaming(cmd, env, timeout=1800, rss_cap_mb=None):
            seen['cmd'] = cmd
            seen['timeout'] = timeout
            seen['rss_cap_mb'] = rss_cap_mb
            return {'exit_code': 0, 'stdout_tail': '', 'stderr_tail': ''}

        orig = ra.run_job_streaming
        ra.run_job_streaming = fake_streaming
        try:
            sup._job_runner({'name': 'adaptive_eod',
                             'command': 'adaptive-eod',
                             'timeout_s': 1200,
                             'rss_cap_mb': 407})('adaptive_eod')
            self.assertEqual(seen['cmd'][-1], 'adaptive-eod')
            self.assertEqual(seen['timeout'], 1200)
            self.assertEqual(seen['rss_cap_mb'], 407)
            sup._job_runner({'name': 'scan',
                             'command': 'scan'})('scan')
            self.assertEqual(seen['cmd'][-1], 'scan')
            self.assertEqual(seen['timeout'], 1800)
            self.assertIsNone(seen['rss_cap_mb'])
        finally:
            ra.run_job_streaming = orig

    def test_job_runner_surfaces_blocked_simulation(self):
        # The real _job_runner parses the engine child's JSON report:
        # a BLOCKED simulation_status rides along in the runner result
        # so run_job can record 'blocked'; any other report (or an
        # unparseable tail) leaves the result untouched.
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.mode = 'live'
        sup.owner_id = 'test'
        tail = {}

        def fake_streaming(cmd, env, timeout=1800, rss_cap_mb=None):
            return {'exit_code': 0, 'stdout_tail': tail.get('tail', ''),
                    'stderr_tail': ''}

        orig = ra.run_job_streaming
        ra.run_job_streaming = fake_streaming
        try:
            tail['tail'] = json.dumps(
                {'phase': 'eod', 'status': 'OK',
                 'simulation_status': 'BLOCKED_STALE_INPUT'}) + '\n'
            out = sup._job_runner({'name': 'adaptive_eod',
                                   'command': 'adaptive-eod'})('adaptive_eod')
            self.assertEqual(out.get('simulation_status'),
                             'BLOCKED_STALE_INPUT')
            tail['tail'] = json.dumps(
                {'phase': 'eod', 'status': 'OK',
                 'simulation_status': 'SIMULATED'})
            out = sup._job_runner({'name': 'adaptive_eod',
                                   'command': 'adaptive-eod'})('adaptive_eod')
            self.assertNotIn('simulation_status', out)
            tail['tail'] = 'not json at all'
            out = sup._job_runner({'name': 'scan',
                                   'command': 'scan'})('scan')
            self.assertNotIn('simulation_status', out)
        finally:
            ra.run_job_streaming = orig

    def test_run_job_records_blocked_for_blocked_simulation(self):
        # run_job maps a BLOCKED simulation_status to status 'blocked'
        # (not 'ok'): the day is not counted as done, and last_run
        # does not suppress a later retry.
        import render_full_schedule as rfs
        import render_jobs as rj
        store = self._store()
        try:
            rj.run_job(store, 'paper_v2', 'adaptive_eod',
                       self._today_ist(), owner_id='render:h:1:1',
                       runner=lambda name: {
                           'exit_code': 0,
                           'simulation_status':
                               'BLOCKED_PIT_SERIES_ARCHIVE_P1'})
            rows = store.execute(
                "SELECT status, result_json FROM paper_job_runs"
                " WHERE namespace='paper_v2' AND job='adaptive_eod'")
            self.assertEqual(rows[0]['status'], 'blocked')
            self.assertIn('BLOCKED_PIT_SERIES_ARCHIVE_P1',
                          rows[0]['result_json'])
            self.assertFalse(rfs.job_completed_today(
                store, 'paper_v2', 'adaptive_eod', self._today_ist()))
            self.assertEqual(rj.last_run_times(store, 'paper_v2'), {})
        finally:
            store.close()

    def _today_ist(self):
        # Today's date in IST: the session day the seeded breaker
        # runs must carry. _refresh_breakers reads today's IST date,
        # so runs seeded under a fixed past date never count and the
        # breaker never opens (this test failed on Sat 21 Nov 2026
        # IST with the runs dated 2026-11-20 — proven identical on
        # pristine base 0df291e).
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        return _dt.now(_ZI('Asia/Kolkata')).date().isoformat()

    def _sup_with_breaker(self, store, breaker_open=True):
        import render_app as ra
        import render_jobs as rj
        if breaker_open:
            # Seed the breaker with 3 killed starts (the incident: the
            # scan child is killed, it does not succeed). A failed run
            # does not count in last_run_times, so the job is still due
            # — and the breaker must stop it from starting again.
            for _ in range(3):
                rj.run_job(store, 'paper_v2', 'scan', self._today_ist(),
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
                store, 'paper_v2', self._today_ist(),
                max_starts=3, window_min=30))
            orig = rsch.load_schedule
            rsch.load_schedule = lambda mode='live': \
                self._due_scan_schedule()
            # The due-scan fixture is fake (a slot 5 min ago); the
            # calendar check inside due_jobs is incidental to this
            # test, so pin today to a trading day. Without this the
            # test fails on weekends: due_jobs returns [] and the
            # breaker is never refreshed.
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._manage_jobs()
            finally:
                rsch.load_schedule = orig
                rsch.is_trading_day = orig_day
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
                store, 'paper_v2', 'scan', self._today_ist(),
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
                store, 'paper_v2', 'scan', self._today_ist(),
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
            # Pin today to a trading day: the due-scan fixture is fake
            # and the calendar check inside due_jobs is incidental
            # here. Without this the test fails on weekends (due_jobs
            # returns [], breaker never refreshed).
            orig_day = rsch.is_trading_day
            rsch.is_trading_day = lambda day: True
            try:
                sup._manage_jobs()
            finally:
                rsch.load_schedule = orig
                rsch.is_trading_day = orig_day
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
