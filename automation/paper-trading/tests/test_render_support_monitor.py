"""Tests for the read-only support monitor (brief 23 Nov 2026).

The monitor reads the paper book from Turso (SELECT only) and the
service's own /health in-process, and records findings in the
append-only support_monitor_findings table. No test here needs
credentials: the store is a local SqliteStore or a fake.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_app as rapp
import render_health as rh
import render_state as rs
import support_monitor as sm

IST = ZoneInfo('Asia/Kolkata')
NS = 'paper_v2'
# A trading day inside the verified calendar (config.json
# calendar_valid_from 2026-11-01 through 2026-12-31). 2026-11-23 is a
# Monday; regular session 09:15-15:30 IST.
CONFIG = {
    'namespace': NS,
    'calendar_valid_from': '2026-11-01',
    'calendar_valid_through': '2026-12-31',
    'regular_session': ['09:15', '15:30'],
    'holidays': [],
    'special_sessions': {},
}
MONDAY_1030 = datetime(2026, 11, 23, 10, 30, tzinfo=IST)
MONDAY_0800 = datetime(2026, 11, 23, 8, 0, tzinfo=IST)


def _base_snapshot(**over):
    """A clean in-session snapshot with no findings."""
    snap = {
        'now_ist': MONDAY_1030, 'in_session': True,
        'namespace_configured': NS, 'namespace_effective': NS,
        'heartbeat_age_s': 30, 'health_ok': True, 'quote_probe': None,
        'positions': [], 'negative_equity': [],
        'fills_without_decision': [], 's7_exit_decisions': [],
        'fetch_errors': [],
    }
    snap.update(over)
    return snap


def _pos(strategy=1, symbol='RELIANCE-EQ', qty=10, mark_price=100.0,
         mark_at=None, high_water=110.0, updated_at=None):
    return {
        'strategy': strategy, 'symbol': symbol, 'qty': qty,
        'mark_price': mark_price,
        'mark_at': (mark_at or MONDAY_1030).isoformat(),
        'high_water': high_water,
        'updated_at': (updated_at or MONDAY_1030).isoformat(),
    }


class RecordingStore:
    """Wraps a store and records every SQL statement issued."""

    def __init__(self, inner):
        self.inner = inner
        self.statements = []

    def execute(self, sql, args=()):
        self.statements.append(sql)
        return self.inner.execute(sql, args)

    def execute_many(self, statements):
        for sql, _args in statements:
            self.statements.append(sql)
        return self.inner.execute_many(statements)

    def affected_rows(self, sql, args=()):
        self.statements.append(sql)
        return self.inner.affected_rows(sql, args)

    def read_count(self):
        return self.inner.read_count()

    def server_time(self):
        return self.inner.server_time()


class CheckHeartbeatTests(unittest.TestCase):
    """Check (a): worker heartbeat stale > 3 min during the session."""

    def test_stale_heartbeat_flags(self):
        f = sm.check(_base_snapshot(heartbeat_age_s=181))
        self.assertEqual([x['code'] for x in f],
                         ['worker_heartbeat_stale'])
        self.assertEqual(f[0]['severity'], 'ERROR')

    def test_fresh_heartbeat_ok(self):
        self.assertEqual(sm.check(_base_snapshot(heartbeat_age_s=180)),
                         [])
        self.assertEqual(sm.check(_base_snapshot(heartbeat_age_s=30)),
                         [])

    def test_no_heartbeat_row_is_not_stale(self):
        # No heartbeat row yet: the worker never reported, not a stale
        # heartbeat. No finding.
        self.assertEqual(
            sm.check(_base_snapshot(heartbeat_age_s=None)), [])

    def test_stale_heartbeat_out_of_session_is_ignored(self):
        snap = _base_snapshot(heartbeat_age_s=3600, in_session=False)
        self.assertEqual(sm.check(snap), [])


class CheckHealthTests(unittest.TestCase):
    """Check (b): /health ok=false or quote_probe errors."""

    def test_health_not_ok_flags(self):
        f = sm.check(_base_snapshot(health_ok=False))
        self.assertEqual([x['code'] for x in f], ['health_not_ok'])
        self.assertEqual(f[0]['severity'], 'ERROR')

    def test_health_ok_or_unknown_is_fine(self):
        self.assertEqual(sm.check(_base_snapshot(health_ok=True)), [])
        self.assertEqual(sm.check(_base_snapshot(health_ok=None)), [])

    def test_failed_quote_probe_flags(self):
        probe = {'status': 'failed', 'error_code': 'token_missing'}
        f = sm.check(_base_snapshot(quote_probe=probe))
        self.assertEqual([x['code'] for x in f], ['quote_probe_error'])

    def test_ok_quote_probe_is_fine(self):
        probe = {'status': 'ok', 'ltp_count': 3}
        self.assertEqual(
            sm.check(_base_snapshot(quote_probe=probe)), [])
        self.assertEqual(sm.check(_base_snapshot(quote_probe=None)), [])


class CheckStaleMarkTests(unittest.TestCase):
    """Check (c): open position with no mark updated > 10 min."""

    def test_stale_mark_flags(self):
        old = MONDAY_1030 - timedelta(minutes=11)
        pos = _pos(mark_at=old, updated_at=old)
        f = sm.check(_base_snapshot(positions=[pos]))
        self.assertEqual([x['code'] for x in f], ['stale_mark'])
        self.assertEqual(f[0]['severity'], 'WARN')
        self.assertEqual(f[0]['symbol'], 'RELIANCE-EQ')

    def test_fresh_mark_ok(self):
        pos = _pos(mark_at=MONDAY_1030 - timedelta(minutes=9))
        self.assertEqual(
            sm.check(_base_snapshot(positions=[pos])), [])

    def test_mark_at_none_falls_back_to_updated_at(self):
        # mark_at None but updated_at recent: not stale.
        pos = _pos(mark_at=None, updated_at=MONDAY_1030)
        pos['mark_at'] = None
        self.assertEqual(
            sm.check(_base_snapshot(positions=[pos])), [])
        # mark_at None and updated_at old: stale.
        old = MONDAY_1030 - timedelta(minutes=11)
        pos = _pos(mark_at=None, updated_at=old)
        pos['mark_at'] = None
        f = sm.check(_base_snapshot(positions=[pos]))
        self.assertEqual([x['code'] for x in f], ['stale_mark'])

    def test_closed_position_is_ignored(self):
        old = MONDAY_1030 - timedelta(minutes=60)
        pos = _pos(qty=0, mark_at=old, updated_at=old)
        self.assertEqual(
            sm.check(_base_snapshot(positions=[pos])), [])

    def test_stale_mark_out_of_session_is_ignored(self):
        old = MONDAY_1030 - timedelta(minutes=60)
        pos = _pos(mark_at=old, updated_at=old)
        snap = _base_snapshot(positions=[pos], in_session=False)
        self.assertEqual(sm.check(snap), [])


class CheckS7StopTests(unittest.TestCase):
    """Check (d): S7 mark at/below its active stop, no exit decision."""

    def _s7_pos(self, mark_price=99.0, high_water=110.0, mark_at=None):
        # stop = 0.90 * 110 = 99.0; mark 99.0 is at the stop.
        return _pos(strategy=7, symbol='TCS-EQ', mark_price=mark_price,
                    high_water=high_water,
                    mark_at=mark_at or MONDAY_1030)

    def test_stop_breached_no_exit_flags(self):
        f = sm.check(_base_snapshot(positions=[self._s7_pos()]))
        self.assertEqual([x['code'] for x in f],
                         ['s7_stop_breached_no_exit'])
        self.assertEqual(f[0]['severity'], 'ERROR')
        self.assertEqual(f[0]['detail']['stop'], 99.0)

    def test_exit_decision_after_mark_clears_it(self):
        pos = self._s7_pos()
        dec = [{'symbol': 'TCS-EQ',
                'recorded_at': MONDAY_1030.isoformat()}]
        snap = _base_snapshot(positions=[pos], s7_exit_decisions=dec)
        self.assertEqual(sm.check(snap), [])

    def test_exit_decision_before_mark_does_not_clear_it(self):
        # An EXIT decision recorded before the breaching mark is stale;
        # the breach is newer than the decision, so it still flags.
        pos = self._s7_pos()
        dec = [{'symbol': 'TCS-EQ',
                'recorded_at': (MONDAY_1030 - timedelta(
                    minutes=30)).isoformat()}]
        snap = _base_snapshot(positions=[pos], s7_exit_decisions=dec)
        self.assertEqual([x['code'] for x in sm.check(snap)],
                         ['s7_stop_breached_no_exit'])

    def test_mark_above_stop_is_fine(self):
        self.assertEqual(
            sm.check(_base_snapshot(positions=[self._s7_pos(100.0)])),
            [])

    def test_non_s7_position_is_ignored(self):
        pos = _pos(strategy=4, mark_price=50.0, high_water=110.0)
        self.assertEqual(
            sm.check(_base_snapshot(positions=[pos])), [])


class CheckBookIntegrityTests(unittest.TestCase):
    """Check (e): negative quantity/cash, fill without a decision."""

    def test_negative_quantity_flags(self):
        pos = _pos(qty=-5)
        f = sm.check(_base_snapshot(positions=[pos]))
        self.assertEqual([x['code'] for x in f], ['negative_quantity'])
        self.assertEqual(f[0]['severity'], 'CRITICAL')

    def test_negative_equity_flags(self):
        snap = _base_snapshot(negative_equity=[
            {'strategy': 1, 'day': '2026-11-23', 'equity_paise': -100}])
        f = sm.check(snap)
        self.assertEqual([x['code'] for x in f],
                         ['negative_cash_equity'])
        self.assertEqual(f[0]['severity'], 'CRITICAL')

    def test_fill_without_decision_flags(self):
        snap = _base_snapshot(fills_without_decision=[
            {'signal_id': 'sig-1', 'strategy': 2, 'symbol': 'INFY-EQ',
             'filled_at': MONDAY_1030.isoformat()}])
        f = sm.check(snap)
        self.assertEqual([x['code'] for x in f],
                         ['fill_without_decision'])
        self.assertEqual(f[0]['symbol'], 'INFY-EQ')


class CheckNamespaceTests(unittest.TestCase):
    """Check (f): namespace != configured one."""

    def test_mismatch_flags(self):
        snap = _base_snapshot(namespace_effective='paper_v1')
        f = sm.check(snap)
        self.assertEqual([x['code'] for x in f],
                         ['namespace_mismatch'])
        self.assertEqual(f[0]['severity'], 'CRITICAL')

    def test_match_is_fine(self):
        self.assertEqual(sm.check(_base_snapshot()), [])

    def test_mismatch_flags_even_out_of_session(self):
        snap = _base_snapshot(namespace_effective='paper_v1',
                              in_session=False)
        self.assertEqual([x['code'] for x in sm.check(snap)],
                         ['namespace_mismatch'])


class CheckTursoErrorTests(unittest.TestCase):
    """Check (g): Turso errors in the last cycle."""

    def test_fetch_error_flags(self):
        snap = _base_snapshot(fetch_errors=[
            {'query': 'heartbeat', 'error': 'TursoHttpError'}])
        f = sm.check(snap)
        self.assertEqual([x['code'] for x in f], ['turso_error'])
        self.assertEqual(f[0]['severity'], 'ERROR')
        self.assertEqual(f[0]['detail']['queries'],
                         [{'query': 'heartbeat',
                           'error': 'TursoHttpError'}])

    def test_multiple_fetch_errors_collapse_to_one_finding(self):
        snap = _base_snapshot(fetch_errors=[
            {'query': 'heartbeat', 'error': 'TursoHttpError'},
            {'query': 'positions', 'error': 'TursoHttpError'},
            {'query': 'fills_without_decision', 'error': 'Timeout'}])
        f = sm.check(snap)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]['code'], 'turso_error')
        self.assertEqual(
            [q['query'] for q in f[0]['detail']['queries']],
            ['heartbeat', 'positions', 'fills_without_decision'])

    def test_no_fetch_error_is_fine(self):
        self.assertEqual(sm.check(_base_snapshot()), [])


class FetchSnapshotTests(unittest.TestCase):
    """fetch_snapshot against a real SqliteStore with fake book data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'store.sqlite')
        self.store = rs.SqliteStore(self.db)
        rs.migrate(self.store)
        schema = Path(HERE.parent, 'schema.sql').read_text()
        # Only the paper-book tables the monitor reads (plus the
        # render tables migrate() already created).
        for stmt in (
                'CREATE TABLE IF NOT EXISTS paper_positions'
                '(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,'
                'symbol TEXT NOT NULL,qty INTEGER NOT NULL,'
                'entry_price REAL NOT NULL,entry_at TEXT NOT NULL,'
                'high_water REAL NOT NULL,mark_price REAL,mark_at TEXT,'
                'updated_at TEXT NOT NULL,'
                'PRIMARY KEY(namespace,strategy,symbol))',
                'CREATE TABLE IF NOT EXISTS paper_decisions'
                '(namespace TEXT NOT NULL,seq INTEGER NOT NULL,'
                'correlation_id TEXT NOT NULL,'
                "decision_type TEXT NOT NULL,strategy INTEGER,"
                'symbol TEXT,signal_id TEXT,cause TEXT,rule TEXT,'
                'execution_path TEXT,source_at TEXT NOT NULL,'
                'recorded_at TEXT NOT NULL,prev_hash TEXT NOT NULL,'
                'entry_hash TEXT NOT NULL,payload_json TEXT NOT NULL,'
                'PRIMARY KEY(namespace,seq))',
                'CREATE TABLE IF NOT EXISTS paper_fills'
                '(id INTEGER PRIMARY KEY AUTOINCREMENT,'
                'namespace TEXT NOT NULL,signal_id TEXT NOT NULL,'
                'strategy INTEGER NOT NULL,symbol TEXT NOT NULL,'
                'side TEXT NOT NULL,qty INTEGER NOT NULL,'
                'price REAL NOT NULL,quote_at TEXT NOT NULL,'
                'filled_at TEXT NOT NULL,reason TEXT NOT NULL,'
                'realized_pnl REAL NOT NULL,cashflow REAL NOT NULL,'
                'execution_path TEXT NOT NULL,'
                'UNIQUE(namespace,signal_id))',
                'CREATE TABLE IF NOT EXISTS paper_monitor_health'
                '(namespace TEXT PRIMARY KEY,pid INTEGER NOT NULL,'
                'heartbeat_at TEXT NOT NULL,status TEXT NOT NULL,'
                'detail_json TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS paper_equity_closes'
                '(namespace TEXT NOT NULL,book TEXT NOT NULL,'
                'strategy INTEGER NOT NULL,day TEXT NOT NULL,'
                'equity_paise INTEGER NOT NULL,'
                'PRIMARY KEY(namespace,book,strategy,day))',
        ):
            self.store.execute(stmt, ())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fetch(self, **over):
        health = {'ok': True, 'quote_probe': None}
        snap, reads = sm.fetch_snapshot(
            self.store, NS, NS, CONFIG, health, MONDAY_1030)
        snap.update(over)
        return snap, reads

    def test_empty_book_returns_empty_snapshot(self):
        snap, reads = self._fetch()
        self.assertEqual(snap['positions'], [])
        self.assertEqual(snap['fills_without_decision'], [])
        self.assertEqual(snap['heartbeat_age_s'], None)
        self.assertLessEqual(reads, 10)
        self.assertEqual(sm.check(snap), [])

    def test_heartbeat_age_is_computed(self):
        hb = (MONDAY_1030 - timedelta(minutes=5)).isoformat()
        self.store.execute(
            'INSERT INTO paper_monitor_health'
            ' (namespace, pid, heartbeat_at, status, detail_json)'
            ' VALUES (?, 1, ?, ?, ?)',
            (NS, hb, 'healthy', '{}'))
        snap, _reads = self._fetch()
        self.assertEqual(snap['heartbeat_age_s'], 300)
        codes = [x['code'] for x in sm.check(snap)]
        self.assertIn('worker_heartbeat_stale', codes)

    def test_stale_position_and_s7_breach_are_found(self):
        old = (MONDAY_1030 - timedelta(minutes=20)).isoformat()
        self.store.execute(
            'INSERT INTO paper_positions'
            ' (namespace, strategy, symbol, qty, entry_price,'
            ' entry_at, high_water, mark_price, mark_at, updated_at)'
            ' VALUES (?, 7, ?, 10, 100.0, ?, 110.0, 98.0, ?, ?)',
            (NS, 'TCS-EQ', MONDAY_1030.isoformat(), old, old))
        snap, reads = self._fetch()
        self.assertEqual(len(snap['positions']), 1)
        self.assertLessEqual(reads, 10)
        codes = [x['code'] for x in sm.check(snap)]
        self.assertIn('stale_mark', codes)
        self.assertIn('s7_stop_breached_no_exit', codes)

    def test_fill_without_decision_is_found(self):
        self.store.execute(
            'INSERT INTO paper_fills'
            ' (namespace, signal_id, strategy, symbol, side, qty,'
            ' price, quote_at, filled_at, reason, realized_pnl,'
            ' cashflow, execution_path)'
            ' VALUES (?, ?, 2, ?, ?, 10, 100.0, ?, ?, ?, 0, 0, ?)',
            (NS, 'sig-9', 'INFY-EQ', 'BUY',
             MONDAY_1030.isoformat(), MONDAY_1030.isoformat(),
             'entry', 'FEED_OPEN'))
        snap, _reads = self._fetch()
        self.assertEqual(len(snap['fills_without_decision']), 1)
        codes = [x['code'] for x in sm.check(snap)]
        self.assertIn('fill_without_decision', codes)

    def test_fill_with_entry_decision_is_not_flagged(self):
        self.store.execute(
            'INSERT INTO paper_decisions'
            ' (namespace, seq, correlation_id, decision_type,'
            ' strategy, symbol, signal_id, source_at, recorded_at,'
            ' prev_hash, entry_hash, payload_json)'
            " VALUES (?, 1, 'c1', 'ENTRY', 2, 'INFY-EQ', 'sig-9',"
            ' ?, ?, ?, ?, ?)',
            (NS, MONDAY_1030.isoformat(), MONDAY_1030.isoformat(),
             '0' * 64, '0' * 64, '{}'))
        self.store.execute(
            'INSERT INTO paper_fills'
            ' (namespace, signal_id, strategy, symbol, side, qty,'
            ' price, quote_at, filled_at, reason, realized_pnl,'
            ' cashflow, execution_path)'
            ' VALUES (?, ?, 2, ?, ?, 10, 100.0, ?, ?, ?, 0, 0, ?)',
            (NS, 'sig-9', 'INFY-EQ', 'BUY',
             MONDAY_1030.isoformat(), MONDAY_1030.isoformat(),
             'entry', 'FEED_OPEN'))
        snap, _reads = self._fetch()
        self.assertEqual(snap['fills_without_decision'], [])

    def test_missing_table_is_a_fetch_error_not_a_crash(self):
        # Drop the fills table: the cycle must record a Turso error
        # finding, not raise.
        self.store.execute('DROP TABLE paper_fills', ())
        snap, _reads = self._fetch()
        self.assertTrue(any(
            e['query'] == 'fills_without_decision'
            for e in snap['fetch_errors']))
        codes = [x['code'] for x in sm.check(snap)]
        self.assertIn('turso_error', codes)


class SinkTests(unittest.TestCase):
    """Findings sink: append-only table, dedup, structured log lines."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'store.sqlite')
        self.store = rs.SqliteStore(self.db)
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_table_and_insert(self):
        sm.ensure_findings_table(self.store)
        dedup = {}
        findings = sm.check(_base_snapshot(
            heartbeat_age_s=600, health_ok=False))
        buf = io.StringIO()
        with redirect_stdout(buf):
            n = sm.sink_findings(self.store, NS, findings, MONDAY_1030,
                                 dedup)
        self.assertEqual(n, 2)
        rows = self.store.execute(
            'SELECT severity, code, namespace FROM'
            ' support_monitor_findings ORDER BY code', ())
        self.assertEqual(
            [(r['severity'], r['code'], r['namespace']) for r in rows],
            [('ERROR', 'health_not_ok', NS),
             ('ERROR', 'worker_heartbeat_stale', NS)])
        lines = [json.loads(x) for x in buf.getvalue().splitlines()
                 if x.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['event'], 'support_monitor_finding')

    def test_dedup_within_30_minutes(self):
        sm.ensure_findings_table(self.store)
        dedup = {}
        findings = sm.check(_base_snapshot(heartbeat_age_s=600))
        self.assertEqual(
            sm.sink_findings(self.store, NS, findings, MONDAY_1030,
                             dedup), 1)
        # Same code+symbol 10 minutes later: skipped.
        later = MONDAY_1030 + timedelta(minutes=10)
        self.assertEqual(
            sm.sink_findings(self.store, NS, findings, later, dedup),
            0)
        # 31 minutes later: written again.
        later = MONDAY_1030 + timedelta(minutes=31)
        self.assertEqual(
            sm.sink_findings(self.store, NS, findings, later, dedup),
            1)
        rows = self.store.execute(
            'SELECT COUNT(*) AS n FROM support_monitor_findings', ())
        self.assertEqual(rows[0]['n'], 2)

    def test_dedup_is_per_code_and_symbol(self):
        sm.ensure_findings_table(self.store)
        dedup = {}
        pos1 = _pos(symbol='AAA-EQ', mark_at=MONDAY_1030 - timedelta(
            minutes=20), updated_at=MONDAY_1030 - timedelta(minutes=20))
        pos2 = _pos(symbol='BBB-EQ', mark_at=MONDAY_1030 - timedelta(
            minutes=20), updated_at=MONDAY_1030 - timedelta(minutes=20))
        findings = sm.check(_base_snapshot(positions=[pos1, pos2]))
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            sm.sink_findings(self.store, NS, findings, MONDAY_1030,
                             dedup), 2)


class RunnerTests(unittest.TestCase):
    """The runner: exceptions never propagate; disabled is a no-op."""

    def _monitor(self, store=None, health=None, env=None):
        if store is None:
            tmp = tempfile.TemporaryDirectory()
            store = rs.SqliteStore(os.path.join(tmp.name, 's.sqlite'))
            rs.migrate(store)
            self.addCleanup(tmp.cleanup)
            self.addCleanup(store.close)
        return sm.Monitor(
            store, NS, CONFIG,
            lambda: health if health is not None else {'ok': True},
            now_fn=lambda: MONDAY_1030)

    def test_exception_in_fetch_does_not_propagate(self):
        class BrokenStore:
            def execute(self, *a, **k):
                raise RuntimeError('boom')
            def execute_many(self, *a, **k):
                raise RuntimeError('boom')
            def read_count(self):
                return 0
        mon = self._monitor(store=BrokenStore())
        state = mon.cycle()
        self.assertEqual(state['last_error'], 'RuntimeError')
        # A second cycle still does not raise.
        state = mon.cycle()
        self.assertEqual(state['last_error'], 'RuntimeError')

    def test_exception_in_health_snapshot_fn_does_not_propagate(self):
        def _bad_health():
            raise RuntimeError('health boom')
        tmp = tempfile.TemporaryDirectory()
        store = rs.SqliteStore(os.path.join(tmp.name, 's.sqlite'))
        rs.migrate(store)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        mon = sm.Monitor(store, NS, CONFIG, _bad_health,
                         now_fn=lambda: MONDAY_1030)
        state = mon.cycle()
        self.assertIsNone(state['last_error'])
        self.assertIsNotNone(state['last_run_at'])

    def test_cycle_outside_session_does_nothing(self):
        mon = self._monitor()
        mon.now_fn = lambda: MONDAY_0800  # 08:00, before the session
        state = mon.cycle()
        self.assertIsNone(state['last_run_at'])
        self.assertEqual(state['reads_total'], 0)

    def test_cycle_in_session_runs_and_counts_reads(self):
        mon = self._monitor()
        state = mon.cycle()
        self.assertIsNotNone(state['last_run_at'])
        self.assertGreater(state['reads_total'], 0)
        self.assertLessEqual(state['reads_total'], 10)
        self.assertIsNone(state['last_error'])

    def test_disabled_monitor_is_a_noop(self):
        self.assertFalse(sm.monitor_enabled(
            {'SUPPORT_MONITOR_ENABLED': '0'}))
        self.assertTrue(sm.monitor_enabled(
            {'SUPPORT_MONITOR_ENABLED': '1'}))
        self.assertTrue(sm.monitor_enabled({}))
        old = os.environ.get('SUPPORT_MONITOR_ENABLED')
        os.environ['SUPPORT_MONITOR_ENABLED'] = '0'
        try:
            mon = self._monitor()
            self.assertFalse(mon.enabled)
            state = mon.cycle()
            self.assertEqual(state['reads_total'], 0)
            self.assertIsNone(state['last_run_at'])
            self.assertFalse(state['enabled'])
            self.assertIsNone(
                sm.start_monitor_thread(
                    mon.store, NS, CONFIG, lambda: {'ok': True},
                    lambda s: None, lambda: True)[0])
        finally:
            if old is None:
                os.environ.pop('SUPPORT_MONITOR_ENABLED', None)
            else:
                os.environ['SUPPORT_MONITOR_ENABLED'] = old

    def test_serve_forever_runs_cycles_and_stops(self):
        mon = self._monitor()
        calls = {'n': 0}
        orig_cycle = mon.cycle

        def _counting_cycle(now=None):
            calls['n'] += 1
            return orig_cycle(now)

        mon.cycle = _counting_cycle
        stop_after = {'n': 3}

        def _stop():
            return calls['n'] >= stop_after['n']

        # No real sleep: the loop slices are instant.
        mon.serve_forever(_stop, sleep_fn=lambda s: None)
        self.assertEqual(calls['n'], 3)


class ReadOnlySQLTests(unittest.TestCase):
    """No SQL other than SELECT and the findings INSERT is issued."""

    def test_only_select_and_findings_insert(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        inner = rs.SqliteStore(os.path.join(tmp.name, 's.sqlite'))
        rs.migrate(inner)
        self.addCleanup(inner.close)
        store = RecordingStore(inner)
        # Seed one stale position so the cycle writes a finding.
        inner.execute(
            'CREATE TABLE IF NOT EXISTS paper_positions'
            '(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,'
            'symbol TEXT NOT NULL,qty INTEGER NOT NULL,'
            'entry_price REAL NOT NULL,entry_at TEXT NOT NULL,'
            'high_water REAL NOT NULL,mark_price REAL,mark_at TEXT,'
            'updated_at TEXT NOT NULL,'
            'PRIMARY KEY(namespace,strategy,symbol))', ())
        old = (MONDAY_1030 - timedelta(minutes=20)).isoformat()
        inner.execute(
            'INSERT INTO paper_positions'
            ' (namespace, strategy, symbol, qty, entry_price,'
            ' entry_at, high_water, mark_price, mark_at, updated_at)'
            ' VALUES (?, 1, ?, 10, 100.0, ?, 110.0, 100.0, ?, ?)',
            (NS, 'RELIANCE-EQ', MONDAY_1030.isoformat(), old, old))
        # Seed the other tables the monitor reads so no fetch errors
        # mask the SQL assertion (empty tables are fine).
        for stmt in (
                'CREATE TABLE IF NOT EXISTS paper_decisions'
                '(namespace TEXT NOT NULL,seq INTEGER NOT NULL,'
                'correlation_id TEXT NOT NULL,'
                "decision_type TEXT NOT NULL,strategy INTEGER,"
                'symbol TEXT,signal_id TEXT,cause TEXT,rule TEXT,'
                'execution_path TEXT,source_at TEXT NOT NULL,'
                'recorded_at TEXT NOT NULL,prev_hash TEXT NOT NULL,'
                'entry_hash TEXT NOT NULL,payload_json TEXT NOT NULL,'
                'PRIMARY KEY(namespace,seq))',
                'CREATE TABLE IF NOT EXISTS paper_fills'
                '(id INTEGER PRIMARY KEY AUTOINCREMENT,'
                'namespace TEXT NOT NULL,signal_id TEXT NOT NULL,'
                'strategy INTEGER NOT NULL,symbol TEXT NOT NULL,'
                'side TEXT NOT NULL,qty INTEGER NOT NULL,'
                'price REAL NOT NULL,quote_at TEXT NOT NULL,'
                'filled_at TEXT NOT NULL,reason TEXT NOT NULL,'
                'realized_pnl REAL NOT NULL,cashflow REAL NOT NULL,'
                'execution_path TEXT NOT NULL,'
                'UNIQUE(namespace,signal_id))',
                'CREATE TABLE IF NOT EXISTS paper_monitor_health'
                '(namespace TEXT PRIMARY KEY,pid INTEGER NOT NULL,'
                'heartbeat_at TEXT NOT NULL,status TEXT NOT NULL,'
                'detail_json TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS paper_equity_closes'
                '(namespace TEXT NOT NULL,book TEXT NOT NULL,'
                'strategy INTEGER NOT NULL,day TEXT NOT NULL,'
                'equity_paise INTEGER NOT NULL,'
                'PRIMARY KEY(namespace,book,strategy,day))',
        ):
            inner.execute(stmt, ())
        store.statements.clear()
        mon = sm.Monitor(store, NS, CONFIG, lambda: {'ok': True},
                         now_fn=lambda: MONDAY_1030)
        state = mon.cycle()
        self.assertEqual(state['last_findings_count'], 1)
        allowed = ('SELECT', 'WITH')
        for sql in store.statements:
            head = sql.strip().upper()
            if head.startswith(allowed):
                continue
            if head.startswith('CREATE TABLE IF NOT EXISTS'
                               ' SUPPORT_MONITOR_FINDINGS'):
                continue
            if head.startswith('INSERT INTO SUPPORT_MONITOR_FINDINGS'):
                continue
            self.fail('unexpected SQL issued by the monitor: %r' % sql)
        # And the trading tables are untouched: the position is still
        # there, unchanged, and no fill/decision appeared.
        rows = inner.execute(
            'SELECT qty, mark_price FROM paper_positions', ())
        self.assertEqual([(r['qty'], r['mark_price']) for r in rows],
                         [(10, 100.0)])
        self.assertEqual(inner.execute(
            'SELECT COUNT(*) AS n FROM paper_fills', ())[0]['n'], 0)
        self.assertEqual(inner.execute(
            'SELECT COUNT(*) AS n FROM paper_decisions', ())[0]['n'], 0)


class ConstantsTests(unittest.TestCase):
    """Worker tick, health refresh and monitor interval are unchanged."""

    def test_worker_tick_and_refresh_unchanged(self):
        self.assertEqual(rapp.SUPERVISOR_TICK_SECONDS, 60)
        old = os.environ.pop('RENDER_HEALTH_REFRESH_S', None)
        try:
            self.assertEqual(rh.health_refresh_seconds(), 300)
        finally:
            if old is not None:
                os.environ['RENDER_HEALTH_REFRESH_S'] = old

    def test_monitor_interval_default(self):
        old = os.environ.pop('SUPPORT_MONITOR_INTERVAL_S', None)
        try:
            self.assertEqual(sm.monitor_interval_seconds(), 300)
            self.assertEqual(
                sm.monitor_interval_seconds(
                    {'SUPPORT_MONITOR_INTERVAL_S': '60'}), 60)
            self.assertEqual(
                sm.monitor_interval_seconds(
                    {'SUPPORT_MONITOR_INTERVAL_S': 'bogus'}), 300)
        finally:
            if old is not None:
                os.environ['SUPPORT_MONITOR_INTERVAL_S'] = old


class HealthFieldTests(unittest.TestCase):
    """The /health support_monitor field shape."""

    def test_build_snapshot_carries_support_monitor(self):
        snap = rh.build_snapshot(
            mode='live', namespace=NS, active_runner='render',
            lease_held=True, lease={'owner_id': 'r', 'epoch': 1},
            worker={'state': 'running', 'pid': 1, 'rss_mb': 100},
            jobs={}, uptime_seconds=10, memory_mb=100)
        self.assertEqual(set(snap['support_monitor']), {
            'enabled', 'last_run_at', 'last_findings_count',
            'reads_total', 'last_error'})
        self.assertFalse(snap['support_monitor']['enabled'])
        state = {'enabled': True, 'last_run_at': '2026-11-23T10:30:00',
                 'last_findings_count': 2, 'reads_total': 15,
                 'last_error': None}
        snap = rh.build_snapshot(
            mode='live', namespace=NS, active_runner='render',
            lease_held=True, lease={'owner_id': 'r', 'epoch': 1},
            worker={'state': 'running', 'pid': 1, 'rss_mb': 100},
            jobs={}, uptime_seconds=10, memory_mb=100,
            support_monitor=state)
        self.assertEqual(snap['support_monitor'], state)


class SessionCrossCheckTests(unittest.TestCase):
    """support_monitor.session_window_for agrees with render_app's."""

    def test_agrees_with_render_app_session_window(self):
        days = [
            datetime(2026, 11, 23).date(),  # Monday, trading day
            datetime(2026, 11, 24).date(),  # Tuesday
            datetime(2026, 11, 28).date(),  # Saturday
            datetime(2026, 11, 29).date(),  # Sunday
            datetime(2026, 12, 25).date(),  # holiday (in config)
            datetime(2026, 10, 15).date(),  # before calendar start
            datetime(2027, 1, 5).date(),    # after calendar end
        ]
        for day in days:
            with self.subTest(day=day):
                self.assertEqual(
                    sm.session_window_for(CONFIG, day),
                    rapp.session_window(CONFIG, day))

    def test_monitor_window_margins(self):
        # Regular 09:15-15:30 session -> monitor window 09:10-15:40.
        self.assertTrue(sm.in_monitor_window(
            CONFIG, datetime(2026, 11, 23, 9, 10, tzinfo=IST)))
        self.assertTrue(sm.in_monitor_window(
            CONFIG, datetime(2026, 11, 23, 15, 39, tzinfo=IST)))
        self.assertFalse(sm.in_monitor_window(
            CONFIG, datetime(2026, 11, 23, 9, 9, tzinfo=IST)))
        self.assertFalse(sm.in_monitor_window(
            CONFIG, datetime(2026, 11, 23, 15, 40, tzinfo=IST)))
        self.assertFalse(sm.in_monitor_window(
            CONFIG, datetime(2026, 11, 28, 10, 30, tzinfo=IST)))


if __name__ == '__main__':
    unittest.main()
