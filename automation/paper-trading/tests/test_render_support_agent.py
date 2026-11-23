"""Tests for the full Support Agent on Render (brief Nov 2026).

The agent runs the box Support Agent's v3 production detector suite
(support_agent.run_v3_production_detectors) inside the Render
supervisor, reading the paper book from Turso (SELECT only) and
writing hash-chained findings to the append-only support_findings
table. No test here needs credentials: the store is a local
SqliteStore or a fake.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_app as rapp
import render_health as rh
import render_state as rs
import support_agent as sa
import support_agent_render as sar

IST = ZoneInfo('Asia/Kolkata')
NS = 'paper_v2'
# A trading day inside the verified calendar (config.json
# calendar_valid_from 2026-11-01 through 2026-12-31). 2026-11-23 is a
# Monday; regular session 09:15-15:30 IST. 2026-11-24 is a holiday
# (Guru Nanak Jayanti).
CONFIG = {
    'namespace': NS,
    'calendar_valid_from': '2026-11-01',
    'calendar_valid_through': '2026-12-31',
    'regular_session': ['09:15', '15:30'],
    'holidays': ['2026-11-24'],
    'special_sessions': {},
}
MONDAY_1030 = datetime(2026, 11, 23, 10, 30, tzinfo=IST)
MONDAY_1640 = datetime(2026, 11, 23, 16, 40, tzinfo=IST)
MONDAY_0800 = datetime(2026, 11, 23, 8, 0, tzinfo=IST)
MONDAY_1730 = datetime(2026, 11, 23, 17, 30, tzinfo=IST)
HOLIDAY_1030 = datetime(2026, 11, 24, 10, 30, tzinfo=IST)

PAPER_TABLES = (
    'CREATE TABLE IF NOT EXISTS paper_events'
    '(namespace TEXT,event_id TEXT,run_id TEXT,session_day TEXT,'
    'source_at TEXT,recorded_at TEXT,event_type TEXT,severity TEXT,'
    'outcome TEXT,data_json TEXT)',
    'CREATE TABLE IF NOT EXISTS paper_decisions'
    '(namespace TEXT,seq INTEGER,correlation_id TEXT,'
    'decision_type TEXT,strategy INTEGER,symbol TEXT,signal_id TEXT,'
    'cause TEXT,rule TEXT,execution_path TEXT,source_at TEXT,'
    'recorded_at TEXT,payload_json TEXT,prev_hash TEXT,entry_hash TEXT)',
    'CREATE TABLE IF NOT EXISTS paper_fills'
    '(namespace TEXT,id INTEGER,signal_id TEXT,strategy INTEGER,'
    'symbol TEXT,side TEXT,qty INTEGER,price REAL,quote_at TEXT,'
    'filled_at TEXT,reason TEXT,realized_pnl REAL,cashflow REAL,'
    'execution_path TEXT)',
    'CREATE TABLE IF NOT EXISTS paper_signals'
    '(namespace TEXT,id TEXT,strategy INTEGER,symbol TEXT,'
    'bar_date TEXT,side TEXT,status TEXT,reason TEXT,created_at TEXT,'
    'eligible_date TEXT)',
    'CREATE TABLE IF NOT EXISTS paper_price_bands'
    '(trade_date TEXT,symbol TEXT,asm_stage TEXT,gsm_stage TEXT,'
    'surveillance TEXT)',
)


def _event(store, event_id, event_type, at, run_id='run1', day='2026-11-23',
           namespace=NS, data=None):
    store.execute(
        'INSERT INTO paper_events(namespace,event_id,run_id,session_day,'
        'source_at,recorded_at,event_type,severity,outcome,data_json)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?)',
        (namespace, event_id, run_id, day, at, at, event_type, 'INFO',
         'ok', json.dumps(data or {})))


def _seed_clean_day(store):
    """A clean Monday: scan on time, dense monitor checks, EOD on time.

    Monitor checks every 60 s (worker_stale_seconds is 90) so no
    monitor_staleness fires. No fills, no decisions.
    """
    _event(store, 'scan-start', 'scan_started',
           '2026-11-23T08:45:00+05:30')
    _event(store, 'scan-done', 'scan_completed',
           '2026-11-23T08:50:00+05:30')
    t = datetime(2026, 11, 23, 9, 15, tzinfo=IST)
    end = datetime(2026, 11, 23, 15, 30, tzinfo=IST)
    i = 0
    while t <= end:
        _event(store, f'mon-{i}', 'monitor_completed', t.isoformat())
        t += timedelta(minutes=1)
        i += 1
    _event(store, 'eod-done', 'eod_completed',
           '2026-11-23T16:17:00+05:30')


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


class EnvTests(unittest.TestCase):
    """Env parsing: kill-switch and interval clamping."""

    def test_enabled_default_on(self):
        self.assertTrue(sar.agent_enabled({}))
        self.assertTrue(sar.agent_enabled({'SUPPORT_AGENT_ENABLED': '1'}))

    def test_enabled_off_only_on_exact_zero(self):
        self.assertFalse(sar.agent_enabled({'SUPPORT_AGENT_ENABLED': '0'}))
        self.assertTrue(sar.agent_enabled({'SUPPORT_AGENT_ENABLED': 'no'}))

    def test_interval_default_900(self):
        self.assertEqual(sar.agent_interval_seconds({}), 900)

    def test_interval_clamped(self):
        self.assertEqual(
            sar.agent_interval_seconds({'SUPPORT_AGENT_INTERVAL_S': '10'}),
            300)
        self.assertEqual(
            sar.agent_interval_seconds(
                {'SUPPORT_AGENT_INTERVAL_S': '99999'}),
            3600)
        self.assertEqual(
            sar.agent_interval_seconds(
                {'SUPPORT_AGENT_INTERVAL_S': 'bad'}),
            900)
        self.assertEqual(
            sar.agent_interval_seconds(
                {'SUPPORT_AGENT_INTERVAL_S': '600'}),
            600)


class WindowTests(unittest.TestCase):
    """The agent window: trading day, open-5min to 17:00 IST."""

    def test_in_session(self):
        self.assertTrue(sar.in_agent_window(CONFIG, MONDAY_1030))
        self.assertTrue(sar.in_agent_window(CONFIG, MONDAY_1640))

    def test_before_open_margin_is_out(self):
        self.assertFalse(sar.in_agent_window(CONFIG, MONDAY_0800))

    def test_after_1700_is_out(self):
        self.assertFalse(sar.in_agent_window(CONFIG, MONDAY_1730))

    def test_holiday_is_out(self):
        self.assertFalse(sar.in_agent_window(CONFIG, HOLIDAY_1030))

    def test_weekend_is_out(self):
        saturday = datetime(2026, 11, 28, 10, 30, tzinfo=IST)
        self.assertFalse(sar.in_agent_window(CONFIG, saturday))

    def test_outside_calendar_is_out(self):
        old = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
        self.assertFalse(sar.in_agent_window(CONFIG, old))


class AdapterTests(unittest.TestCase):
    """TursoAdapter: query/batch delegate; tables are ensured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(
            os.path.join(self.tmp.name, 'store.sqlite'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_query_delegates(self):
        self.store.execute(
            'CREATE TABLE t(x TEXT)', ())
        self.store.execute("INSERT INTO t VALUES('a')", ())
        adapter = sar.TursoAdapter(self.store)
        self.assertEqual(adapter.query('SELECT x FROM t', ()), [{'x': 'a'}])

    def test_ensure_support_tables(self):
        adapter = sar.TursoAdapter(self.store)
        self.assertTrue(sar.ensure_support_tables(adapter))
        rows = self.store.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('support_findings','support_episodes',"
            "'support_checkpoints','support_detector_runs')", ())
        self.assertEqual(
            sorted(r['name'] for r in rows),
            ['support_checkpoints', 'support_detector_runs',
             'support_episodes', 'support_findings'])

    def test_write_guard_rejects_trading_table(self):
        # The box agent's guard still enforces support-only writes
        # through the adapter.
        adapter = sar.TursoAdapter(self.store)
        sar.ensure_support_tables(adapter)
        with self.assertRaises(PermissionError):
            sa._write_support_only(adapter, [(
                'INSERT INTO paper_positions VALUES(1)', ())])


class AgentCycleTests(unittest.TestCase):
    """Full cycles against a real SqliteStore with seeded book data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'store.sqlite')
        self.store = rs.SqliteStore(self.db)
        for stmt in PAPER_TABLES:
            self.store.execute(stmt, ())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _agent(self, store=None):
        return sar.Agent(store or self.store, NS, CONFIG)

    def _detectors(self, day='2026-11-23'):
        rows = self.store.execute(
            'SELECT detector, severity, status FROM support_findings'
            ' WHERE session_day=? ORDER BY detector', (day,))
        return [(r['detector'], r['severity'], r['status']) for r in rows]

    def test_clean_day_has_no_sev1_sev2_findings(self):
        _seed_clean_day(self.store)
        agent = self._agent()
        state = agent.cycle(MONDAY_1640)
        self.assertIsNone(state['last_error'])
        self.assertEqual(state['runs_total'], 1)
        self.assertEqual(state['last_session_day'], '2026-11-23')
        detectors = self._detectors()
        sev12 = [d for d in detectors if d[1] in ('SEV1', 'SEV2')]
        self.assertEqual(sev12, [])
        # No sells and no exit self-test: the SEV4 exit-path finding
        # is the expected clean-day finding.
        self.assertIn(('exit_path_unexercised', 'SEV4', 'OPEN'), detectors)

    def test_scan_missed_when_no_scan_by_deadline(self):
        # Dense monitor checks but no scan at all: the 09:00 deadline
        # (08:45 + 15m grace) passes with nothing found.
        t = datetime(2026, 11, 23, 9, 15, tzinfo=IST)
        end = datetime(2026, 11, 23, 10, 30, tzinfo=IST)
        i = 0
        while t <= end:
            _event(self.store, f'mon-{i}', 'monitor_completed',
                   t.isoformat())
            t += timedelta(minutes=1)
            i += 1
        agent = self._agent()
        agent.cycle(MONDAY_1030)
        detectors = self._detectors()
        self.assertIn(('scan_missed', 'SEV2', 'OPEN'), detectors)

    def test_second_cycle_adds_no_duplicate_findings(self):
        _seed_clean_day(self.store)
        agent = self._agent()
        agent.cycle(MONDAY_1640)
        first = self.store.execute(
            'SELECT COUNT(*) AS n FROM support_findings', ())[0]['n']
        created_first = agent.findings_created_total
        agent.cycle(MONDAY_1640)
        second = self.store.execute(
            'SELECT COUNT(*) AS n FROM support_findings', ())[0]['n']
        # Re-runs add evidence rows only when evidence changed; the
        # finding count (latest per finding_id) must not grow.
        self.assertEqual(agent.runs_total, 2)
        self.assertEqual(agent.findings_created_total, created_first)
        self.assertEqual(second, first)

    def test_disabled_is_a_noop(self):
        _seed_clean_day(self.store)
        agent = self._agent()
        agent.enabled = False
        state = agent.cycle(MONDAY_1640)
        self.assertEqual(state['runs_total'], 0)
        self.assertIsNone(state['last_run_at'])
        # Nothing ran, so the findings table was never even created.
        rows = self.store.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
            " AND name='support_findings'", ())
        self.assertEqual(rows[0]['n'], 0)

    def test_outside_window_is_a_noop(self):
        _seed_clean_day(self.store)
        agent = self._agent()
        state = agent.cycle(MONDAY_0800)
        self.assertEqual(state['runs_total'], 0)
        state = agent.cycle(HOLIDAY_1030)
        self.assertEqual(state['runs_total'], 0)

    def test_cycle_never_writes_trading_tables(self):
        _seed_clean_day(self.store)
        rec = RecordingStore(self.store)
        agent = self._agent(store=rec)
        agent.cycle(MONDAY_1640)
        bad = []
        for sql in rec.statements:
            norm = ' '.join(sql.strip().split()).upper()
            if norm.startswith(('INSERT', 'UPDATE', 'DELETE',
                                'INSERT OR REPLACE')):
                if 'SUPPORT_' not in norm:
                    bad.append(sql)
        self.assertEqual(bad, [])

    def test_findings_are_hash_chained(self):
        _seed_clean_day(self.store)
        agent = self._agent()
        agent.cycle(MONDAY_1640)
        adapter = sar.TursoAdapter(self.store)
        failures = sa.verify_findings_chain(adapter, NS)
        self.assertEqual(failures, [])

    def test_state_shape(self):
        agent = self._agent()
        self.assertEqual(agent.state(), sar.default_state(enabled=True))
        agent.cycle(MONDAY_1030)
        state = agent.state()
        self.assertEqual(state['runs_total'], 1)
        self.assertEqual(state['last_run_at'],
                         MONDAY_1030.isoformat())
        self.assertIsNone(state['last_error'])


class HealthTests(unittest.TestCase):
    """The /health support_agent field."""

    def _snap(self, **kw):
        args = dict(mode='live', namespace='paper_v2',
                    active_runner='render', lease_held=True,
                    lease={'owner_id': 'render:h:1:1', 'epoch': 3},
                    worker={'state': 'running', 'pid': 123, 'rss_mb': 200,
                            'restarts': 0},
                    jobs={'scan': '2026-11-20T08:45:00'},
                    uptime_seconds=60, memory_mb=150)
        args.update(kw)
        return rh.build_snapshot(**args)

    def test_support_agent_in_payload(self):
        snap = self._snap(support_agent={
            'enabled': True, 'last_run_at': '2026-11-23T10:30:00+05:30',
            'last_findings_count': 2, 'findings_created_total': 2,
            'runs_total': 1, 'last_error': None,
            'last_session_day': '2026-11-23'})
        self.assertEqual(snap['support_agent']['runs_total'], 1)
        self.assertEqual(snap['support_agent']['last_findings_count'], 2)

    def test_support_agent_default_when_none(self):
        snap = self._snap()
        self.assertEqual(snap['support_agent'], {
            'enabled': False, 'last_run_at': None,
            'last_findings_count': 0, 'findings_created_total': 0,
            'runs_total': 0, 'last_error': None,
            'last_session_day': None})


class SupervisorWiringTests(unittest.TestCase):
    """The Supervisor carries the agent state like the monitor's."""

    def test_supervisor_has_support_agent_state(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = rs.SqliteStore(
                os.path.join(tmp.name, 'store.sqlite'))
            sup = rapp.Supervisor(store, NS, 'trial', 'test-owner')
            self.assertEqual(sup.support_agent_state,
                             sar.default_state(enabled=True))
            import support_monitor as sm
            self.assertEqual(sup.support_monitor_state,
                             sm.default_state(enabled=True))
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
