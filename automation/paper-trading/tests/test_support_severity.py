"""Tests for A2: severity rules — every CRITICAL condition carries a one-line action.

Required CRITICAL (SEV1) conditions (brief §4 A2):
1. worker stall / heartbeat gap not explained by a host pause (monitor_staleness)
2. missed 08:45 scan (scan_missed)
3. missed EOD (eod_missed)
4. fill on a stale quote (fill_on_stale_quote) — quote age above the engine's
   own maximum, config.json quote_max_age_seconds (60 s, enforced at
   engine.py:365); no number invented here
5. Fyers auth or budget block (fyers_auth_or_budget_block) — real event
   reasons from engine.py: auth_circuit_open_30min, token_refresh_failed,
   market_data_budget_exhausted_* (engine.py:151-200)
6. audit spool not replayed (audit_spool_unreplayed) — only_in_audit > 0
   after the session ended; only_in_turso is ignored (expected, harmless)
7. lease / active-runner conflict (lease_conflict) — driven by A3's
   support_render.evaluate; here only the severity-table rule is asserted,
   the triggering fixture lives in tests/test_support_render.py
"""
import json, re, sqlite3, unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_agent as sa
import support_severity as sev

DAY = '2026-11-19'
NS = 'paper_v2'
NOW = f'{DAY}T16:35:00+05:30'


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


def _trading_db():
    db = MemDB()
    for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
        if sql.strip():
            db.batch([(sql.strip(), ())])
    return db


def _insert_event(db, event_id, event_type, outcome, source_at, data, run_id='r1', severity='WARN'):
    db.batch([(
        'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (NS, event_id, run_id, DAY, source_at, source_at, event_type, severity, outcome, json.dumps(data)),
    )])


def _run(db):
    store = sa.SupportStore(':memory:')
    sa.run_v3_production_detectors(store, NS, DAY, now_at=NOW, trading_db=db, observation_only=True)
    findings = sa.current_findings(store, NS, DAY)
    store.close()
    return {f['detector']: f for f in findings}


def _assert_critical(self, by_det, detector):
    f = by_det.get(detector)
    self.assertIsNotNone(f, f'{detector} finding missing')
    self.assertEqual(f['severity'], 'SEV1', detector)
    action = f['details'].get('action')
    self.assertTrue(action, f'{detector} has no action')
    self.assertLessEqual(len(action), 140, detector)
    self.assertNotIn('\n', action)


class SeverityTests(unittest.TestCase):
    def test_worker_stall_unexplained_is_critical(self):
        db = _trading_db()
        # Heartbeat gap 09:16 -> 09:18 (120 s > 90 s stale threshold), no host pause.
        _insert_event(db, 'mon-1', 'monitor_completed', 'ok', f'{DAY}T09:16:00+05:30', {'check': 'ok'}, severity='INFO')
        _insert_event(db, 'mon-2', 'monitor_completed', 'ok', f'{DAY}T09:18:00+05:30', {'check': 'ok'}, severity='INFO')
        by_det = _run(db)
        _assert_critical(self, by_det, 'monitor_staleness')
        self.assertFalse(by_det['monitor_staleness']['details']['gaps'][0]['explained_by_host_pause'])
        db.c.close()

    def test_missed_scan_is_critical(self):
        db = _trading_db()
        by_det = _run(db)
        _assert_critical(self, by_det, 'scan_missed')
        db.c.close()

    def test_missed_eod_is_critical(self):
        db = _trading_db()
        _insert_event(db, 'scan-1', 'scan_completed', 'ok', f'{DAY}T08:50:00+05:30', {}, severity='INFO')
        by_det = _run(db)
        _assert_critical(self, by_det, 'eod_missed')
        db.c.close()

    def test_fill_on_stale_quote_is_critical(self):
        db = _trading_db()
        db.batch([(
            'INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (NS, 'sig-1', 1, 'RELIANCE-EQ', '2026-11-18', 'BUY', 'filled', 'fixture', f'{DAY}T09:30:00+05:30', DAY),
        ), (
            'INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (NS, 'sig-1', 1, 'RELIANCE-EQ', 'BUY', 500, 100.0, f'{DAY}T09:30:00+05:30', f'{DAY}T09:32:00+05:30', 'fixture', 0.0, -50000.0, 'LTP_LEGACY'),
        )])
        by_det = _run(db)
        _assert_critical(self, by_det, 'fill_on_stale_quote')
        det = by_det['fill_on_stale_quote']['details']
        self.assertEqual(det['count'], 1)
        self.assertEqual(det['max_quote_age_seconds'], 60)
        self.assertEqual(det['samples'][0]['quote_age_seconds'], 120)
        db.c.close()

    def test_fyers_auth_or_budget_block_is_critical(self):
        db = _trading_db()
        _insert_event(db, 'auth-1', 'worker_error', 'failed', f'{DAY}T10:00:00+05:30',
                      {'reason': 'auth_circuit_open_30min'})
        _insert_event(db, 'auth-2', 'worker_error', 'failed', f'{DAY}T11:00:00+05:30',
                      {'reason': 'token_refresh_failed'})
        by_det = _run(db)
        _assert_critical(self, by_det, 'fyers_auth_or_budget_block')
        self.assertEqual(by_det['fyers_auth_or_budget_block']['details']['count'], 2)
        db.c.close()

    def test_audit_spool_unreplayed_is_critical(self):
        res = sa.audit_spool_status(only_in_audit=3, only_in_turso=1209, session_ended=True)
        self.assertIsNotNone(res)
        self.assertEqual(res['detector'], 'audit_spool_unreplayed')
        self.assertEqual(res['severity'], 'SEV1')
        self.assertTrue(res['details']['action'])
        self.assertLessEqual(len(res['details']['action']), 140)
        # Session not ended yet: no finding, even with spool rows pending.
        self.assertIsNone(sa.audit_spool_status(3, 1209, session_ended=False))

    def test_lease_conflict_rule_is_critical(self):
        # The triggering fixture lives in tests/test_support_render.py (A3);
        # here the severity table itself must rate lease_conflict CRITICAL.
        rule = sev.rule_for('lease_conflict')
        self.assertIsNotNone(rule)
        self.assertEqual(rule['severity'], 'SEV1')
        self.assertTrue(rule['action'])
        self.assertLessEqual(len(rule['action']), 140)

    def test_no_critical_on_clean_session(self):
        db = _trading_db()
        _insert_event(db, 'scan-1', 'scan_completed', 'ok', f'{DAY}T08:50:00+05:30', {}, severity='INFO')
        _insert_event(db, 'eod-1', 'eod_completed', 'ok', f'{DAY}T16:20:00+05:30', {}, severity='INFO')
        # Monitor checks every 60 s from 09:15 to 15:30 (gaps below the 90 s threshold).
        from datetime import datetime, timedelta
        t = datetime(2026, 11, 19, 9, 15)
        i = 0
        while t <= datetime(2026, 11, 19, 15, 30):
            _insert_event(db, f'mon-{i}', 'monitor_completed', 'ok',
                          t.strftime('%Y-%m-%dT%H:%M:%S+05:30'), {'check': 'ok'}, severity='INFO')
            t += timedelta(seconds=60)
            i += 1
        by_det = _run(db)
        critical = [d for d, f in by_det.items() if f['severity'] == 'SEV1']
        self.assertEqual(critical, [])
        self.assertNotIn('scan_missed', by_det)
        self.assertNotIn('eod_missed', by_det)
        db.c.close()

    def test_spool_unreplayed_ignores_only_in_turso(self):
        # 19 Nov real numbers: only_in_turso was 1209 and harmless (engine-log
        # events are merged into Turso); only_in_audit is the alarm side.
        self.assertIsNone(sa.audit_spool_status(only_in_audit=0, only_in_turso=1209, session_ended=True))
        self.assertIsNotNone(sa.audit_spool_status(only_in_audit=1, only_in_turso=1209, session_ended=True))

    def test_action_is_advice_not_command(self):
        forbidden = ('rm ', 'kill ', 'install ', 'git ', 'sudo ', 'chmod ', 'pip ', '/usr/', '/workspace', 'systemctl')
        for detector, rule in sorted(sev.RULES.items()):
            action = rule['action']
            self.assertTrue(action, detector)
            self.assertLessEqual(len(action), 140, detector)
            self.assertNotIn('\n', action, detector)
            for bad in forbidden:
                self.assertNotIn(bad, action, f'{detector}: action contains {bad!r}')

    def test_every_detector_has_a_rule(self):
        src = (Path(__file__).resolve().parents[1] / 'support_agent.py').read_text()
        names = set(re.findall(r"emit\(\s*'([a-z0-9_]+)'", src))
        names |= set(re.findall(
            r"record_episode\(\s*db,\s*namespace,\s*session_day,\s*'[^']+',\s*'([a-z0-9_]+)'", src))
        names |= set(re.findall(r"\brecord\('([a-z0-9_]+)'", src))
        # Detectors emitted dynamically or from other modules, not via a literal emit().
        names |= {'install_aborted_process_active', 'install_rolled_back',
                  'fill_on_stale_quote', 'fyers_auth_or_budget_block',
                  'audit_spool_unreplayed', 'lease_conflict', 'box_host_pause'}
        missing = sorted(n for n in names if sev.rule_for(n) is None)
        self.assertEqual(missing, [], f'detectors without a severity rule: {missing}')
        for n in sorted(names):
            rule = sev.rule_for(n)
            self.assertTrue(rule['action'], n)
            self.assertLessEqual(len(rule['action']), 140, n)


if __name__ == '__main__':
    unittest.main()
