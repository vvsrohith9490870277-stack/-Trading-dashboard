"""Tests for Tasks 1-2 (muse-tonight-20-nov): one-shot Fyers quote probe
on boot (trial only) and one-shot trial scan/EOD via RENDER_RUN_ONCE.

The probe runs in a short-lived child (render_quote_probe.py) so the
supervisor never imports numpy/pandas for a one-shot diagnostic; the
supervisor spawns the child, parses its JSON and records the result in
paper_runtime_kv (key 'quote_probe') and in an audit event
'render_quote_probe'. /health serves it under 'quote_probe' (trial only).

The run-once job goes through the normal job runner (paper_job_runs,
RSS cap, crash-loop breaker) even though the job is in
RENDER_DISABLED_JOBS, and is keyed by date+job so a restart never
repeats it.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import audit as al
import render_boot as rb
import render_health as rh
import render_jobs as rj
import render_schedule as rsch
import render_state as rs

TRIAL_NS = 'paper_render_trial_v1'


class FakeApi:
    """A MarketData stand-in: records quotes() calls and a budget."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self._budget = {'quotes': 0, 'depth': 0}

    def quotes(self, symbols):
        self.calls += 1
        self._budget['quotes'] += 1
        if self.error is not None:
            raise self.error
        return dict(self.result or {})

    def budget_snapshot(self):
        return {'by_route': dict(self._budget),
                'requests': sum(self._budget.values())}


TOKEN_OK = {'present': True, 'has_refresh': True, 'access_len': 32,
            'issued_at': '2026-11-20T10:00:00+05:30'}
TOKEN_MISSING = {'present': False, 'has_refresh': False,
                 'access_len': None, 'issued_at': None}


def _quotes_ok():
    return {
        'RELIANCE': {'symbol': 'RELIANCE', 'price': 1359.0,
                     'quote_at': '2026-11-20T10:00:00+05:30',
                     'provider_status': 'ok', 'source': 'FYERS_QUOTES'},
        'TCS': {'symbol': 'TCS', 'price': 4201.5,
                'quote_at': '2026-11-20T10:00:00+05:30',
                'provider_status': 'ok', 'source': 'FYERS_QUOTES'},
    }


class GuardTests(unittest.TestCase):
    def test_guard_ok_trial_v1(self):
        ok, reason = rb.guard_ok('trial', TRIAL_NS)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_guard_refuses_live_mode(self):
        ok, reason = rb.guard_ok('live', TRIAL_NS)
        self.assertFalse(ok)
        self.assertEqual(reason, 'not_trial_mode')

    def test_guard_refuses_wrong_namespace(self):
        for ns in ('paper_v2', 'paper_render_trial_v2', 'paper_v1', ''):
            ok, reason = rb.guard_ok('trial', ns)
            self.assertFalse(ok, ns)
            self.assertEqual(reason, 'wrong_namespace')


class EnvTests(unittest.TestCase):
    def test_probe_enabled(self):
        self.assertTrue(rb.probe_enabled({'RENDER_QUOTE_PROBE_ON_BOOT': '1'}))
        for env in ({}, {'RENDER_QUOTE_PROBE_ON_BOOT': '0'},
                    {'RENDER_QUOTE_PROBE_ON_BOOT': ''},
                    {'RENDER_QUOTE_PROBE_ON_BOOT': 'yes'}):
            self.assertFalse(rb.probe_enabled(env), env)

    def test_run_once_requested(self):
        self.assertEqual(rb.run_once_requested({'RENDER_RUN_ONCE': 'scan'}),
                         'scan')
        self.assertEqual(rb.run_once_requested({'RENDER_RUN_ONCE': 'eod'}),
                         'eod')
        for env in ({}, {'RENDER_RUN_ONCE': ''},
                    {'RENDER_RUN_ONCE': 'bogus'},
                    {'RENDER_RUN_ONCE': 'sync_history'}):
            self.assertIsNone(rb.run_once_requested(env), env)


class KvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(Path(self.tmp.name) / 'kv.sqlite')
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_kv_round_trip(self):
        self.assertIsNone(rb.kv_get(self.store, TRIAL_NS, 'quote_probe'))
        rb.kv_put(self.store, TRIAL_NS, 'quote_probe',
                  {'status': 'ok', 'ltp_count': 2})
        got = rb.kv_get(self.store, TRIAL_NS, 'quote_probe')
        self.assertEqual(got, {'status': 'ok', 'ltp_count': 2})
        # Overwrite: the latest probe result wins.
        rb.kv_put(self.store, TRIAL_NS, 'quote_probe', {'status': 'failed'})
        self.assertEqual(rb.kv_get(self.store, TRIAL_NS, 'quote_probe'),
                         {'status': 'failed'})

    def test_kv_bad_json_is_none(self):
        self.store.execute(
            'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
            ' VALUES(?, ?, ?, ?)',
            (TRIAL_NS, 'quote_probe', 'not json{', '2026-11-20T00:00:00'))
        self.assertIsNone(rb.kv_get(self.store, TRIAL_NS, 'quote_probe'))


class RunOnceMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(Path(self.tmp.name) / 'kv.sqlite')
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_marker_keyed_by_date_and_job(self):
        self.assertEqual(rb.run_once_marker_key('scan', '2026-11-21'),
                         'run_once:scan:2026-11-21')
        self.assertIsNone(rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                           '2026-11-21'))
        marker = rb.mark_run_once(self.store, TRIAL_NS, 'scan',
                                  '2026-11-21', {'status': 'ok'})
        self.assertEqual(marker['job'], 'scan')
        self.assertEqual(marker['day'], '2026-11-21')
        self.assertEqual(marker['status'], 'ok')
        got = rb.run_once_done(self.store, TRIAL_NS, 'scan', '2026-11-21')
        self.assertEqual(got['status'], 'ok')
        # A different day is a different marker (a re-run tomorrow works).
        self.assertIsNone(rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                           '2026-11-22'))
        # A different job is a different marker.
        self.assertIsNone(rb.run_once_done(self.store, TRIAL_NS, 'eod',
                                           '2026-11-21'))


class TrialJobTests(unittest.TestCase):
    def test_trial_job_scan_and_eod(self):
        scan = rsch.trial_job(name='scan')
        self.assertEqual(scan['name'], 'scan')
        self.assertEqual(scan['command'], 'scan')
        self.assertTrue(scan['enabled'])
        eod = rsch.trial_job(name='eod')
        self.assertEqual(eod['name'], 'eod')
        self.assertEqual(eod['command'], 'eod')
        self.assertTrue(eod['enabled'])

    def test_trial_job_respects_enabled_flag(self):
        # sync_history is enabled:false in the trial schedule: the
        # run-once bypasses RENDER_DISABLED_JOBS only, never the
        # schedule's own enabled flag.
        self.assertIsNone(rsch.trial_job(name='sync_history'))
        self.assertIsNone(rsch.trial_job(name='preflight'))
        self.assertIsNone(rsch.trial_job(name='no_such_job'))

    def test_trial_job_disabled_in_file(self):
        data = json.loads((HERE.parent / 'render_schedule.json').read_text())
        data['modes']['trial']['jobs'][1]['enabled'] = False  # scan
        with tempfile.NamedTemporaryFile('w', suffix='.json',
                                        delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            self.assertIsNone(rsch.trial_job(path=path, name='scan'))
        finally:
            Path(path).unlink(missing_ok=True)


class ProbeRunTests(unittest.TestCase):
    def test_success_stores_ok_and_ltp_count(self):
        import render_quote_probe as rqp
        api = FakeApi(result=_quotes_ok())
        res = rqp.run_probe(api=api, token_meta=dict(TOKEN_OK))
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['ltp_count'], 2)
        self.assertEqual(res['symbols'], ['RELIANCE', 'TCS'])
        self.assertEqual(res['requests'], 1)
        self.assertEqual(api.calls, 1, 'the probe calls quotes() once')
        self.assertIsNone(res['error_code'])
        self.assertIsNone(res['error_detail'])
        self.assertTrue(res['token_present'])
        self.assertIn('duration_ms', res)
        self.assertIn('at', res)

    def test_failure_stores_real_redacted_message(self):
        import render_quote_probe as rqp
        err = RuntimeError('market_data_transport_failed')
        err.detail = ('connection reset by peer while reading '
                      '/data/quotes')
        api = FakeApi(error=err)
        res = rqp.run_probe(api=api, token_meta=dict(TOKEN_OK))
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['error_code'], 'market_data_transport_failed')
        # The real message, not the bare type name 'RuntimeError'.
        self.assertIn('connection reset by peer', res['error_detail'])
        self.assertIn('/data/quotes', res['error_detail'])
        self.assertNotEqual(res['error_code'], 'RuntimeError')
        self.assertEqual(res['ltp_count'], 0)
        self.assertEqual(res['requests'], 1)
        self.assertEqual(api.calls, 1)

    def test_failure_redacts_secrets(self):
        import render_quote_probe as rqp
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'
        err = RuntimeError('market_data_transport_failed')
        err.detail = 'bad token ' + jwt
        res = rqp.run_probe(api=FakeApi(error=err),
                            token_meta=dict(TOKEN_OK))
        self.assertNotIn(jwt, res['error_detail'])
        self.assertNotIn('eyJ', res['error_detail'])

    def test_token_missing_makes_no_fyers_call(self):
        import render_quote_probe as rqp
        api = FakeApi(result=_quotes_ok())
        res = rqp.run_probe(api=api, token_meta=dict(TOKEN_MISSING))
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['error_code'], 'token_missing')
        self.assertEqual(res['requests'], 0)
        self.assertEqual(api.calls, 0,
                         'no token means no Fyers call at all')
        self.assertFalse(res['token_present'])

    def test_fyers_code_extracted(self):
        import render_quote_probe as rqp
        res = rqp.run_probe(
            api=FakeApi(error=RuntimeError('market_data_error_-8')),
            token_meta=dict(TOKEN_OK))
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['fyers_code'], -8)
        self.assertEqual(res['error_code'], 'market_data_error_-8')

    def test_http_status_from_recorder(self):
        import render_quote_probe as rqp
        recorder = {'status': 401}
        res = rqp.run_probe(
            api=FakeApi(error=RuntimeError('token_unavailable_no_login')),
            token_meta=dict(TOKEN_OK), status_recorder=recorder)
        self.assertEqual(res['http_status'], 401)
        self.assertEqual(res['error_code'], 'token_unavailable_no_login')

    def test_partial_quotes_count_only_priced(self):
        import render_quote_probe as rqp
        result = _quotes_ok()
        result['TCS'] = {'symbol': 'TCS', 'price': None,
                         'provider_status': 'missing'}
        res = rqp.run_probe(api=FakeApi(result=result),
                            token_meta=dict(TOKEN_OK))
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['ltp_count'], 1)


class RecordingGetTests(unittest.TestCase):
    def test_recorder_captures_quotes_status_only(self):
        import render_quote_probe as rqp

        class Resp:
            def __init__(self, status):
                self.status_code = status

        recorder = {'status': None}
        wrapped = rqp._recording_get(
            lambda url, **kw: Resp(401), recorder)
        self.assertEqual(
            wrapped('https://api-t1.fyers.in/data/quotes',
                    params={}).status_code, 401)
        self.assertEqual(recorder['status'], 401)
        # A non-quotes URL (e.g. a token refresh) must not overwrite it.
        wrapped('https://api-t2.fyers.in/api/v3/token', params={})
        self.assertEqual(recorder['status'], 401)

    def test_recorder_passthrough_on_transport_error(self):
        import render_quote_probe as rqp
        recorder = {'status': None}

        def boom(url, **kw):
            raise ConnectionError('connection reset by peer')

        wrapped = rqp._recording_get(boom, recorder)
        with self.assertRaises(ConnectionError):
            wrapped('https://api-t1.fyers.in/data/quotes', params={})
        self.assertIsNone(recorder['status'])


class RealMarketDataProbeTests(unittest.TestCase):
    """The real MarketData path (mocked requests only): the probe uses
    exactly the code the worker uses, so a 401 must surface as the
    token error with the real HTTP status, not a bare 'RuntimeError'."""

    def _api(self):
        import engine as e
        api = e.MarketData.__new__(e.MarketData)
        api.env = {'FYERS_APP_ID': 'fixture',
                   'FYERS_ACCESS_TOKEN': 'fixture'}
        api.last = 0
        api.refreshed = False
        tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        api.budget = e.RequestBudget(tmp.name)
        return api

    def _trial_env(self):
        old = {}
        for k, v in (('PAPER_RUNTIME', 'render'),
                     ('RENDER_MODE', 'trial')):
            old[k] = os.environ.get(k)
            os.environ[k] = v
        self.addCleanup(lambda: [os.environ.pop(k, None)
                                 if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])

    def test_401_surfaces_token_error_and_status(self):
        import engine as e
        import render_quote_probe as rqp
        self._trial_env()
        api = self._api()
        recorder = {'status': None}

        class Resp:
            status_code = 401

            def json(self):
                return {'s': 'error', 'code': -8}

        def fake_get(url, **kw):
            return Resp()

        orig_get = e.requests.get
        e.requests.get = rqp._recording_get(fake_get, recorder)
        try:
            with patch.object(e.time, 'sleep'):
                res = rqp.run_probe(api=api, token_meta=dict(TOKEN_OK),
                                    status_recorder=recorder)
        finally:
            e.requests.get = orig_get
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['http_status'], 401)
        self.assertEqual(res['error_code'], 'token_unavailable_no_login')
        self.assertEqual(res['requests'], 1)

    def test_success_path_with_real_market_data(self):
        import engine as e
        import render_quote_probe as rqp
        self._trial_env()
        api = self._api()
        recorder = {'status': None}

        class Resp:
            status_code = 200

            def json(self):
                return {'s': 'ok', 'd': [
                    {'n': 'NSE:RELIANCE-EQ', 's': 'ok', 'v': {
                        'symbol': 'RELIANCE', 'lp': 1359.0,
                        'tt': 1795192800, 'open_price': 1350.0}},
                    {'n': 'NSE:TCS-EQ', 's': 'ok', 'v': {
                        'symbol': 'TCS', 'lp': 4201.5,
                        'tt': 1795192800, 'open_price': 4190.0}},
                ]}

        orig_get = e.requests.get
        e.requests.get = rqp._recording_get(
            lambda url, **kw: Resp(), recorder)
        try:
            with patch.object(e.time, 'sleep'):
                res = rqp.run_probe(api=api, token_meta=dict(TOKEN_OK),
                                    status_recorder=recorder)
        finally:
            e.requests.get = orig_get
        self.assertEqual(res['status'], 'ok', res)
        self.assertEqual(res['http_status'], 200)
        self.assertEqual(res['ltp_count'], 2)
        self.assertEqual(res['requests'], 1)


class AuditFieldTests(unittest.TestCase):
    def test_clean_keeps_probe_fields(self):
        payload = al.clean({'status': 'failed',
                            'error_code': 'token_unavailable_no_login',
                            'error_detail': 'no login in trial mode',
                            'duration_ms': 42, 'symbols_count': 2,
                            'symbols': ['RELIANCE', 'TCS'],
                            'http_status': 401, 'fyers_code': -8,
                            'ltp_count': 0, 'requests': 1})
        self.assertEqual(payload['http_status'], 401)
        self.assertEqual(payload['fyers_code'], -8)
        self.assertEqual(payload['ltp_count'], 0)
        self.assertEqual(payload['requests'], 1)
        self.assertEqual(payload['error_code'],
                         'token_unavailable_no_login')

    def test_classified_error_keeps_new_reasons(self):
        for reason in ('token_unavailable_no_login',
                       'market_data_request_failed', 'token_missing'):
            self.assertEqual(al.classified_error(RuntimeError(reason)),
                             reason)


class EmitProbeAuditTests(unittest.TestCase):
    def _store_with_events(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = rs.SqliteStore(Path(tmp.name) / 'audit.sqlite')
        rs.migrate(store)
        store.execute_many([(s, ()) for s in al.SCHEMA.split(';')
                            if s.strip()])
        return store

    def test_emit_writes_render_quote_probe_event(self):
        store = self._store_with_events()
        try:
            result = {'status': 'failed', 'symbols': ['RELIANCE', 'TCS'],
                      'ltp_count': 0, 'http_status': 401, 'fyers_code': None,
                      'error_code': 'token_unavailable_no_login',
                      'error_detail': 'no login in trial mode',
                      'duration_ms': 1200, 'requests': 1,
                      'token_present': True, 'at': '2026-11-20T22:30:00+05:30'}
            self.assertTrue(rb.emit_probe_audit(store, TRIAL_NS, result))
            rows = store.execute(
                "SELECT event_type, data_json FROM paper_events"
                " WHERE namespace=?", (TRIAL_NS,))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['event_type'], 'render_quote_probe')
            data = json.loads(rows[0]['data_json'])
            self.assertEqual(data['error_code'],
                             'token_unavailable_no_login')
            self.assertEqual(data['http_status'], 401)
            self.assertEqual(data['symbols'], ['RELIANCE', 'TCS'])
        finally:
            store.close()

    def test_emit_fails_closed_without_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = rs.SqliteStore(Path(tmp.name) / 'noevents.sqlite')
        rs.migrate(store)
        try:
            # No paper_events table: the audit write fails, the probe
            # result itself (kv) is unaffected — emit returns False.
            self.assertFalse(rb.emit_probe_audit(
                store, TRIAL_NS, {'status': 'ok', 'ltp_count': 2}))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()


class _SupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(Path(self.tmp.name) / 'sup.sqlite')
        rs.migrate(self.store)
        self.store.execute_many([(s, ()) for s in al.SCHEMA.split(';')
                                 if s.strip()])
        self.old = {}
        for k in ('RENDER_QUOTE_PROBE_ON_BOOT', 'RENDER_RUN_ONCE',
                  'RENDER_DISABLED_JOBS'):
            self.old[k] = os.environ.pop(k, None)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _sup(self, mode='trial', namespace=TRIAL_NS):
        import render_app as ra
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.store = self.store
        sup.namespace = namespace
        sup.mode = mode
        sup.owner_id = 'render:h:1:1'
        sup._job_thread = None
        sup._boot_actions_done = False
        return sup

    def _today(self):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        return _dt.now(_ZI('Asia/Kolkata')).date().isoformat()


class SupervisorProbeTests(_SupBase):
    def _child_ok(self):
        result = {'status': 'ok', 'symbols': ['RELIANCE', 'TCS'],
                  'ltp_count': 2, 'http_status': 200, 'fyers_code': None,
                  'error_code': None, 'error_detail': None,
                  'duration_ms': 900, 'requests': 1, 'token_present': True,
                  'at': '2026-11-20T22:30:00+05:30'}
        return {'exit_code': 0, 'stdout_tail': json.dumps(result),
                'stderr_tail': '', 'peak_rss_mb': 42}, result

    def test_probe_off_by_default(self):
        sup = self._sup()
        sup._run_probe_child = lambda: (_ for _ in ()).throw(
            AssertionError('child must not run when the env is unset'))
        self.assertIsNone(sup._maybe_run_quote_probe())
        self.assertIsNone(rb.kv_get(self.store, TRIAL_NS, 'quote_probe'))

    def test_probe_refused_live_mode(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        sup = self._sup(mode='live', namespace='paper_v2')
        sup._run_probe_child = lambda: (_ for _ in ()).throw(
            AssertionError('child must not run outside trial mode'))
        res = sup._maybe_run_quote_probe()
        self.assertEqual(res['status'], 'refused')
        self.assertEqual(res['reason'], 'not_trial_mode')
        self.assertIsNone(rb.kv_get(self.store, 'paper_v2', 'quote_probe'))

    def test_probe_refused_wrong_namespace(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        sup = self._sup(namespace='paper_render_trial_v2')
        sup._run_probe_child = lambda: (_ for _ in ()).throw(
            AssertionError('child must not run outside paper_render_trial_v1'))
        res = sup._maybe_run_quote_probe()
        self.assertEqual(res['status'], 'refused')
        self.assertEqual(res['reason'], 'wrong_namespace')

    def test_probe_runs_once_per_boot(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        child, result = self._child_ok()
        calls = []
        sup = self._sup()
        sup._run_probe_child = lambda: calls.append(1) or child
        sup._run_boot_actions()
        sup._run_boot_actions()  # second tick: must not re-probe
        self.assertEqual(len(calls), 1)
        stored = rb.kv_get(self.store, TRIAL_NS, 'quote_probe')
        self.assertEqual(stored['status'], 'ok')
        self.assertEqual(stored['ltp_count'], 2)
        self.assertEqual(stored['peak_rss_mb'], 42)
        self.assertTrue(stored['audit_ok'])
        rows = self.store.execute(
            "SELECT event_type FROM paper_events WHERE namespace=?",
            (TRIAL_NS,))
        self.assertEqual([r['event_type'] for r in rows],
                         ['render_quote_probe'])

    def test_probe_child_failure_recorded(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'
        sup = self._sup()
        sup._run_probe_child = lambda: {
            'exit_code': 1, 'stdout_tail': '',
            'stderr_tail': 'Traceback: bad token ' + jwt,
            'peak_rss_mb': 60}
        res = sup._maybe_run_quote_probe()
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['error_code'], 'probe_child_failed')
        self.assertNotIn(jwt, res['error_detail'])
        self.assertNotIn('eyJ', res['error_detail'])
        stored = rb.kv_get(self.store, TRIAL_NS, 'quote_probe')
        self.assertEqual(stored['error_code'], 'probe_child_failed')

    def test_probe_child_timeout_recorded(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        sup = self._sup()

        def _raise():
            raise subprocess.TimeoutExpired(cmd=['probe'], timeout=120)

        sup._run_probe_child = _raise
        res = sup._maybe_run_quote_probe()
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['error_code'], 'probe_timeout')

    def test_probe_child_bad_json_recorded(self):
        os.environ['RENDER_QUOTE_PROBE_ON_BOOT'] = '1'
        sup = self._sup()
        sup._run_probe_child = lambda: {
            'exit_code': 0, 'stdout_tail': 'not json at all',
            'stderr_tail': '', 'peak_rss_mb': 10}
        res = sup._maybe_run_quote_probe()
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['error_code'], 'probe_child_failed')


class FakeRunner:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, job_name):
        self.calls.append(job_name)
        if self.fail:
            raise RuntimeError('scan_failed')
        return {'exit_code': 0, 'stdout_tail': '', 'stderr_tail': '',
                'peak_rss_mb': 120}


class SupervisorRunOnceTests(_SupBase):
    def test_run_once_off_by_default(self):
        sup = self._sup()
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        self.assertIsNone(sup._maybe_run_once_job())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                           self._today()))

    def test_run_once_refused_live_mode(self):
        os.environ['RENDER_RUN_ONCE'] = 'scan'
        sup = self._sup(mode='live', namespace='paper_v2')
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        res = sup._maybe_run_once_job()
        self.assertEqual(res['status'], 'refused')
        self.assertEqual(runner.calls, [])

    def test_run_once_scan_runs_and_marks_done(self):
        # scan is in RENDER_DISABLED_JOBS (the trial runs worker-only):
        # the scheduled run stays blocked, but the one-shot still runs.
        os.environ['RENDER_RUN_ONCE'] = 'scan'
        os.environ['RENDER_DISABLED_JOBS'] = 'scan,eod'
        self.assertNotIn('scan', [j['name'] for j in
                                  rsch.load_schedule(mode='trial')['jobs']])
        sup = self._sup()
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        res = sup._maybe_run_once_job()
        self.assertEqual(res['status'], 'started')
        sup._job_thread.join(timeout=10)
        self.assertEqual(runner.calls, ['scan'])
        marker = rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                  self._today())
        self.assertIsNotNone(marker)
        self.assertEqual(marker['status'], 'ok')
        rows = self.store.execute(
            "SELECT status FROM paper_job_runs WHERE namespace=?"
            " AND job='scan'", (TRIAL_NS,))
        self.assertEqual(rows[0]['status'], 'ok')

    def test_run_once_second_boot_is_noop(self):
        os.environ['RENDER_RUN_ONCE'] = 'eod'
        sup = self._sup()
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        self.assertEqual(sup._maybe_run_once_job()['status'], 'started')
        sup._job_thread.join(timeout=10)
        self.assertEqual(runner.calls, ['eod'])
        # A second boot (new supervisor, same store) does not repeat it.
        sup2 = self._sup()
        runner2 = FakeRunner()
        sup2._job_runner = lambda job: runner2
        res = sup2._maybe_run_once_job()
        self.assertEqual(res['status'], 'already_done')
        self.assertEqual(runner2.calls, [])

    def test_run_once_breaker_open_skips(self):
        os.environ['RENDER_RUN_ONCE'] = 'scan'
        day = self._today()
        for _ in range(3):
            rj.run_job(self.store, TRIAL_NS, 'scan', day,
                       owner_id='render:h:1:1',
                       runner=lambda name: {
                           'exit_code': -9, 'stdout_tail': '',
                           'stderr_tail': '', 'killed_reason': 'memory_cap',
                           'rss_cap_mb': 350})
        self.assertTrue(rj.breaker_open(self.store, TRIAL_NS, 'scan', day))
        sup = self._sup()
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        res = sup._maybe_run_once_job()
        self.assertEqual(res['status'], 'skipped')
        self.assertEqual(res['reason'], 'breaker_open')
        self.assertEqual(runner.calls, [])
        self.assertIsNone(rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                           day))

    def test_run_once_failing_runner_marks_error(self):
        os.environ['RENDER_RUN_ONCE'] = 'scan'
        sup = self._sup()
        runner = FakeRunner(fail=True)
        sup._job_runner = lambda job: runner
        self.assertEqual(sup._maybe_run_once_job()['status'], 'started')
        sup._job_thread.join(timeout=10)
        marker = rb.run_once_done(self.store, TRIAL_NS, 'scan',
                                  self._today())
        self.assertEqual(marker['status'], 'error')
        rows = self.store.execute(
            "SELECT status FROM paper_job_runs WHERE namespace=?"
            " AND job='scan'", (TRIAL_NS,))
        self.assertEqual(rows[0]['status'], 'error')

    def test_run_once_invalid_job_is_ignored(self):
        os.environ['RENDER_RUN_ONCE'] = 'bogus'
        sup = self._sup()
        runner = FakeRunner()
        sup._job_runner = lambda job: runner
        self.assertIsNone(sup._maybe_run_once_job())
        self.assertEqual(runner.calls, [])
