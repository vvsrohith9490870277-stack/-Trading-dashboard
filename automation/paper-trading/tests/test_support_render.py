"""Tests for A3: Render / namespace awareness, read-only (support_render.py).

No network: fetch and query functions are injected. Table/column names mirror
render_schema.sql on feat/render-deploy (paper_runner_lease, paper_job_runs,
paper_runtime_kv); if the deployed schema differs, the check reports
not_evaluable / lease_schema_mismatch instead of guessing.
A null health field means not_evaluable, never an alarm and never a pass.
"""
import json, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_render as sr

IST = timezone(timedelta(minutes=330))
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
NS = 'paper_v2'
DAY = '2026-11-20'


def _health(**over):
    h = {
        'ok': True, 'status': 'ok', 'mode': 'live', 'state': 'running',
        'namespace': NS, 'role': 'paper', 'runner_id': 'render-1', 'epoch': 7,
        'uptime_s': 3600, 'now_ist': '2026-11-20T10:00:00+05:30',
        'session_open': True, 'calendar_valid_through': '2026-12-31',
        'calendar_days_left': 41, 'active_runner': 'render',
        'lease': {'held': True, 'owner_id': 'render-1', 'epoch': 7},
        'worker': {'state': 'running', 'pid': 123, 'rss_mb': 110,
                   'restarts': 0, 'restarts_10m': 0, 'last_heartbeat_age_s': 10},
        'last_tick_ist': '2026-11-20T09:59:00+05:30',
        'last_lease_renew_age_s': 5,
        'jobs': ['scan', 'eod'], 'jobs_today': 1,
        'last_job': {'job': 'scan', 'status': 'ok'},
        'rss_mb': 120, 'children_rss_mb': 0,
        'fyers_budget': {'day_used': 100, 'per_day_limit': 10000},
        'auth': {'circuit_open': False},
        'login_allowed': True, 'git_sha': 'abc1234', 'version': '1',
        'trial_budget': None,
        'box_conflict_check': {'paper_v2_heartbeat_present': False,
                               'paper_v2_heartbeat_age_s': None},
    }
    h.update(over)
    return h


def _kv(active_runner='render'):
    return {'active_runner': active_runner}


def _lease(**over):
    lease = {'namespace': NS, 'owner_id': 'render-1', 'owner_kind': 'render',
             'epoch': 7, 'acquired_unix': int(NOW.timestamp()) - 3600,
             'expires_unix': int(NOW.timestamp()) + 3600,
             'heartbeat_at': '2026-11-20T10:00:00+05:30', 'detail_json': '{}'}
    lease.update(over)
    return lease


def _jobs(*rows):
    base = {'namespace': NS, 'job': 'scan', 'session_day': DAY, 'attempt': 1,
            'status': 'ok', 'owner_id': 'render-1',
            'claimed_unix': int(NOW.timestamp()) - 600,
            'lease_until_unix': int(NOW.timestamp()) + 300,
            'started_at': '2026-11-20T08:45:00+05:30',
            'finished_at': '2026-11-20T08:50:00+05:30', 'exit_code': 0,
            'result_json': '{}'}
    out = []
    for r in rows:
        row = dict(base)
        row.update(r)
        out.append(row)
    return out


def _alarms(out):
    return [f for f in out if f['severity'] != 'NOT_EVALUABLE']


def _not_evaluable(out):
    return [f for f in out if f['severity'] == 'NOT_EVALUABLE']


def _by_detector(out, detector):
    return [f for f in out if f['detector'] == detector]


class RenderTests(unittest.TestCase):
    def test_clean_render_health_no_findings(self):
        out = sr.evaluate(_health(), _kv(), _lease(), _jobs(), NOW, NS, True)
        self.assertEqual(_alarms(out), [])
        # Calendar has no senior-set threshold, so it is not_evaluable — not an alarm.
        self.assertTrue(any(f['detector'] == 'render_calendar_days_left'
                            for f in _not_evaluable(out)))

    def test_null_fields_are_not_evaluable_not_alarms(self):
        h = _health()
        h['fyers_budget'] = {'day_used': None, 'per_day_limit': 10000}
        h['box_conflict_check'] = {'paper_v2_heartbeat_present': None,
                                   'paper_v2_heartbeat_age_s': None}
        h['worker'] = dict(h['worker'], last_heartbeat_age_s=None, restarts_10m=None)
        out = sr.evaluate(h, _kv(), _lease(), _jobs(), NOW, NS, True)
        self.assertEqual(_alarms(out), [])
        self.assertGreaterEqual(len(_not_evaluable(out)), 3)

    def test_health_unreachable_critical_when_render_expected(self):
        out = sr.evaluate(None, _kv('render'), _lease(), _jobs(), NOW, NS, True)
        crit = _by_detector(out, 'render_health_unreachable')
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0]['severity'], 'SEV1')
        self.assertTrue(crit[0]['details']['action'])

    def test_health_unreachable_warn_when_box_expected(self):
        out = sr.evaluate(None, _kv('box'), _lease(), _jobs(), NOW, NS, True)
        warn = _by_detector(out, 'render_health_unreachable')
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]['severity'], 'SEV3')

    def test_worker_stopped_in_session_critical(self):
        h = _health(worker={'state': 'stopped', 'pid': None, 'rss_mb': 0,
                            'restarts': 1, 'restarts_10m': 1, 'last_heartbeat_age_s': 400})
        out = sr.evaluate(h, _kv('render'), _lease(), _jobs(), NOW, NS, True)
        stopped = _by_detector(out, 'render_worker_stopped')
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['severity'], 'SEV1')

    def test_lease_expired_critical(self):
        lease = _lease(expires_unix=int(NOW.timestamp()) - 10)
        out = sr.evaluate(_health(), _kv('render'), lease, _jobs(), NOW, NS, True)
        conf = _by_detector(out, 'lease_conflict')
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]['severity'], 'SEV1')
        self.assertEqual(conf[0]['details']['reason'], 'lease_expired')

    def test_lease_kv_mismatch_critical(self):
        lease = _lease(owner_kind='box')
        out = sr.evaluate(_health(), _kv('render'), lease, _jobs(), NOW, NS, True)
        conf = _by_detector(out, 'lease_conflict')
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]['severity'], 'SEV1')
        self.assertEqual(conf[0]['details']['reason'], 'lease_kv_mismatch')

    def test_box_heartbeat_while_render_active_critical(self):
        h = _health(box_conflict_check={'paper_v2_heartbeat_present': True,
                                        'paper_v2_heartbeat_age_s': 30})
        out = sr.evaluate(h, _kv('render'), _lease(), _jobs(), NOW, NS, True)
        conf = _by_detector(out, 'lease_conflict')
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]['severity'], 'SEV1')
        self.assertEqual(conf[0]['details']['reason'], 'box_heartbeat_while_render_active')

    def test_two_owners_same_job_day_critical(self):
        jobs = _jobs({'owner_id': 'render-1'}, {'owner_id': 'render-2'})
        out = sr.evaluate(_health(), _kv('render'), _lease(), jobs, NOW, NS, True)
        conf = _by_detector(out, 'lease_conflict')
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]['severity'], 'SEV1')
        self.assertEqual(conf[0]['details']['reason'], 'two_owners_same_job_day')

    def test_failed_scan_job_critical(self):
        jobs = _jobs({'job': 'scan', 'status': 'failed', 'exit_code': 1},
                     {'job': 'history_sync', 'status': 'failed', 'exit_code': 2})
        out = sr.evaluate(_health(), _kv('render'), _lease(), jobs, NOW, NS, True)
        failed = _by_detector(out, 'render_job_failed')
        self.assertEqual(len(failed), 2)
        by_job = {f['details']['job']: f for f in failed}
        self.assertEqual(by_job['scan']['severity'], 'SEV1')
        self.assertEqual(by_job['history_sync']['severity'], 'SEV3')

    def test_missing_tables_not_evaluable(self):
        out = sr.evaluate(_health(), None, None, None, NOW, NS, True)
        self.assertEqual(_alarms(out), [])
        self.assertTrue(any(f['details'].get('reason') == 'render_tables_absent'
                            for f in _not_evaluable(out)))
        # The query layer classifies a missing table vs a changed schema.
        def missing_table(sql, args):
            raise RuntimeError('no such table: paper_runner_lease')
        state = sr.query_render_state(missing_table, NS, DAY)
        self.assertTrue(state['tables_absent'])
        self.assertFalse(state['schema_mismatch'])

        def changed_schema(sql, args):
            raise RuntimeError('no such column: paper_runner_lease.epoch')
        state = sr.query_render_state(changed_schema, NS, DAY)
        self.assertTrue(state['schema_mismatch'])
        self.assertFalse(state['tables_absent'])

    def test_namespace_aware_trial_vs_paper_v2(self):
        # Trial day: Render serves paper_render_trial_v1, the box runs paper_v2.
        # A stopped Render worker is not an incident for paper_v2.
        h = _health(namespace='paper_render_trial_v1',
                    worker={'state': 'stopped', 'pid': None, 'rss_mb': 0,
                            'restarts': 1, 'restarts_10m': 1, 'last_heartbeat_age_s': 900})
        out = sr.evaluate(h, _kv('box'), _lease(), _jobs(), NOW, NS, True)
        self.assertEqual([f for f in _alarms(out) if f['severity'] == 'SEV1'], [])
        self.assertEqual(_by_detector(out, 'render_worker_stopped'), [])
        # Even with KV saying render for paper_v2, a health payload for the
        # trial namespace must not raise paper_v2 worker alarms.
        out2 = sr.evaluate(h, _kv('render'), _lease(), _jobs(), NOW, NS, True)
        self.assertEqual(_by_detector(out2, 'render_worker_stopped'), [])

    def test_support_render_is_read_only(self):
        seen = []

        def stub_query(sql, args=()):
            seen.append(sql)
            if not sql.strip().upper().startswith('SELECT'):
                raise PermissionError('write attempted')
            if 'paper_runtime_kv' in sql:
                return [{'key': 'active_runner', 'value': 'render'}]
            if 'paper_runner_lease' in sql:
                return [_lease()]
            if 'paper_job_runs' in sql:
                return _jobs({})
            return []

        state = sr.query_render_state(stub_query, NS, DAY)
        self.assertEqual(state['kv'], {'active_runner': 'render'})
        self.assertEqual(state['lease']['owner_id'], 'render-1')
        self.assertEqual(len(state['jobs']), 1)
        self.assertTrue(seen)
        self.assertTrue(all(s.strip().upper().startswith('SELECT') for s in seen))
        # Source scan: no write verbs in the module outside comments.
        src = (Path(__file__).resolve().parents[1] / 'support_render.py').read_text()
        code_lines = [ln for ln in src.splitlines()
                      if not ln.strip().startswith('#')]
        for verb in ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER'):
            self.assertNotIn(verb, '\n'.join(code_lines), verb)

    def test_fetch_health_retries_once_then_reports_unreachable(self):
        calls = []

        class Resp:
            status = 200

            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectionError('cold start')
            return Resp({'ok': True})

        status, payload = sr.fetch_health('https://example.invalid/health', opener=flaky)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {'ok': True})
        self.assertEqual(len(calls), 2)  # one retry, then success

        def down(url, timeout=None):
            raise ConnectionError('down')

        status, payload = sr.fetch_health('https://example.invalid/health', opener=down)
        self.assertIsNone(status)
        self.assertIsNone(payload)


class RenderRunnerWiringTests(unittest.TestCase):
    """A successful Render scan/EOD job satisfies the box scan/EOD expectation."""

    def test_render_scan_and_eod_satisfy_expectations(self):
        import sqlite3
        import support_agent as sa

        class MemDB:
            def __init__(self):
                self.c = sqlite3.connect(':memory:')
                self.c.row_factory = sqlite3.Row

            def batch(self, stm):
                with self.c:
                    for sql, args in stm:
                        self.c.execute(sql, args)

            def query(self, sql, args=()):
                return [dict(r) for r in self.c.execute(sql, args).fetchall()]

        db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
            if sql.strip():
                db.batch([(sql.strip(), ())])
        store = sa.SupportStore(':memory:')
        # No box scan / EOD events at all: without the Render flags both fire.
        sa.run_v3_production_detectors(store, NS, DAY, now_at=f'{DAY}T16:35:00+05:30',
                                       trading_db=db, observation_only=True)
        dets = {f['detector'] for f in sa.current_findings(store, NS, DAY)}
        self.assertIn('scan_missed', dets)
        self.assertIn('eod_missed', dets)
        store.close()
        # With the Render flags the same session is clean of both.
        store2 = sa.SupportStore(':memory:')
        sa.run_v3_production_detectors(store2, NS, DAY, now_at=f'{DAY}T16:35:00+05:30',
                                       trading_db=db, observation_only=True,
                                       render_scan_completed=True, render_eod_completed=True)
        dets2 = {f['detector'] for f in sa.current_findings(store2, NS, DAY)}
        self.assertNotIn('scan_missed', dets2)
        self.assertNotIn('eod_missed', dets2)
        store2.close()
        db.c.close()


if __name__ == '__main__':
    unittest.main()
