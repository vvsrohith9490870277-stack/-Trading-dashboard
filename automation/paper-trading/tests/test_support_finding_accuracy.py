"""Accuracy fixes after the 11 Nov 2026 16:45 Support Agent report.

- One finding per feed per session (reruns / 15:40 dry-run must not double 403s)
- Signal-to-fill latency is measured from the 09:15 official open, not pre-open scan time
- Recovered conditions (NSE 403 later ok; rate-limit stall then EOD) are RESOLVED, not left OPEN
"""
import json, sqlite3, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_agent as sa

DAY = '2026-11-11'
NS = 'paper_v1'


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


class FindingAccuracy(unittest.TestCase):
    def setUp(self):
        self.tdb = MemDB()
        for sql in (sa.ROOT / 'schema.sql').read_text(encoding='utf8').split(';'):
            if sql.strip():
                self.tdb.batch([(sql.strip(), ())])
        self.store = sa.SupportStore(':memory:')
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.tdb.c.close()

    def ev(self, at, event_type, data=None, outcome='ok', ns=NS, side=None, symbol=None, extra=None):
        self.n += 1
        payload = dict(data or {})
        if side:
            payload.setdefault('side', side)
        if symbol:
            payload.setdefault('symbol', symbol)
        if extra:
            payload.update(extra)
        self.tdb.batch([(
            'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ns, f'e{self.n:05d}', 'r', DAY, at, at, event_type, 'INFO', outcome, json.dumps(payload)),
        )])

    def fill(self, at, side='BUY', symbol='AAA-EQ', ns=NS, price=100.0, qty=1):
        self.n += 1
        self.tdb.batch([(
            'INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ns, f's{self.n}', 1, symbol, side, qty, price, at, at, 'test', 0, 0, 'FEED_OPEN'),
        )])

    def run_det(self, now='2026-11-11T16:45:00+05:30'):
        return sa.run_v3_production_detectors(
            self.store, NS, DAY, now_at=now, trading_db=self.tdb, observation_only=True,
        )

    def findings(self, detector=None):
        rows = sa.current_findings(self.store, NS, DAY)
        if detector:
            rows = [f for f in rows if f['detector'] == detector]
        return rows

    def test_403_deduped_across_events_and_reruns_then_recovered(self):
        payload_403 = {'sources': {
            'corporate_actions': {'status': 'unavailable', 'error': 'http_403'},
            'asm': {'status': 'unavailable', 'error': 'http_403'},
            'gsm': {'status': 'unavailable', 'error': 'http_403'},
        }}
        self.ev('2026-11-11T08:29:05+05:30', 'reference_feeds', payload_403)
        self.ev('2026-11-11T09:07:53+05:30', 'reference_feeds', payload_403)
        # Mid-session dry-run style pass (15:40) should mint 3 findings, not 6
        self.run_det('2026-11-11T15:40:00+05:30')
        f403 = self.findings('feed_http_403')
        self.assertEqual(len(f403), 3)
        self.assertEqual({f['details']['feed'] for f in f403}, {'corporate_actions', 'asm', 'gsm'})
        self.assertTrue(all(f['details']['occurrence_count'] == 2 for f in f403))
        # Official 16:45 rerun must not double them, and the 15:41 success resolves them
        self.ev('2026-11-11T15:41:02+05:30', 'reference_feeds', {'sources': {
            'corporate_actions': {'status': 'ok', 'rows_written': 36, 'kinds': ['CASH_DIVIDEND']},
            'asm': {'status': 'ok'},
            'gsm': {'status': 'ok'},
            'series': {'status': 'ok'},
        }})
        self.run_det('2026-11-11T16:45:00+05:30')
        f403 = self.findings('feed_http_403')
        self.assertEqual(len(f403), 3)
        self.assertTrue(all(f['status'] == 'RESOLVED' for f in f403))
        self.assertTrue(all(f['details'].get('recovered') for f in f403))
        self.assertEqual(f403[0]['details']['recovered_at'], '2026-11-11T15:41:02+05:30')

    def test_collapse_duplicate_finding_ids_from_prior_rerun(self):
        # Simulate the pre-fix store: two finding_ids for the same correlation (08:29 vs 09:07)
        sa.append_finding(self.store, NS, 'feed_http_403', f'feed_http_403:{DAY}:asm',
                          '2026-11-11T08:29:05+05:30', DAY, severity='SEV3', details={'feed': 'asm'})
        sa.append_finding(self.store, NS, 'feed_http_403', f'feed_http_403:{DAY}:asm',
                          '2026-11-11T09:07:53+05:30', DAY, severity='SEV3', details={'feed': 'asm'})
        # Before the detector runs, both exist (append_finding now reuses correlation, so the
        # second call should already have collapsed. Force a second id by inserting directly
        # only if append already deduped — assert that path, and also collapse leftover rows.)
        open_rows = [f for f in self.findings('feed_http_403') if f['status'] == 'OPEN']
        self.assertEqual(len(open_rows), 1, 'append_finding must reuse the first_event_at identity')
        # Seed a leftover second id the way the 15:40 dry-run did before the fix
        other_first = '2026-11-11T09:07:53+05:30'
        other_fid = sa.finding_id('feed_http_403', f'feed_http_403:{DAY}:asm', other_first)
        # Bypass append_finding identity reuse by writing a row with the old hash
        self.store.batch([(
            'INSERT INTO support_findings(namespace,finding_id,status_seq,detector,correlation_id,first_event_at,session_day,severity,status,observation_only,notified,details_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (NS, other_fid, 1, 'feed_http_403', f'feed_http_403:{DAY}:asm', other_first, DAY, 'SEV3', 'OPEN', 1, 0, '{"feed":"asm"}', '2026-11-11T15:40:00+05:30'),
        )])
        self.assertEqual(len([f for f in self.findings('feed_http_403') if f['status'] == 'OPEN']), 2)
        sa.collapse_duplicate_findings(self.store, NS, DAY, recorded_at='2026-11-11T16:45:00+05:30')
        latest = self.findings('feed_http_403')
        open_rows = [f for f in latest if f['status'] == 'OPEN']
        resolved = [f for f in latest if f['status'] == 'RESOLVED']
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(len(resolved), 1)
        self.assertTrue(resolved[0]['details'].get('deduplicated'))
        self.assertEqual(resolved[0]['details']['kept_finding_id'], open_rows[0]['finding_id'])

    def test_open_fill_latency_measured_from_0915(self):
        # Pre-open signal, official-open fill ~2 minutes after 09:15 — not a latency incident.
        self.tdb.batch([
            ('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-maha', 1, 'MAHABANK-EQ', '2026-11-10', 'BUY', 'filled', 'daily_entry:beta_momentum_v1',
              '2026-11-11T08:51:55+05:30', DAY)),
            ('INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-maha', 1, 'MAHABANK-EQ', 'BUY', 561, 89.05, '2026-11-11T09:16:08+05:30',
              '2026-11-11T09:16:47+05:30', 'daily_entry:beta_momentum_v1', 0.0, -89.05, 'FEED_OPEN')),
            ('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-dlf', 1, 'DLF-EQ', '2026-11-10', 'BUY', 'filled', 'daily_entry:beta_momentum_v1',
              '2026-11-11T08:51:55+05:30', DAY)),
            ('INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-dlf', 1, 'DLF-EQ', 'BUY', 66, 750.63, '2026-11-11T09:16:01+05:30',
              '2026-11-11T09:17:00.217744+05:30', 'daily_entry:beta_momentum_v1', 0.0, -750.63, 'FEED_OPEN')),
        ])
        self.ev('2026-11-11T09:16:00+05:30', 'monitor_completed')
        self.run_det()
        self.assertEqual(self.findings('signal_to_fill_latency'), [])
        # A same-session fill 10 minutes after 09:15 would still be late
        self.tdb.batch([
            ('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-late', 1, 'DLF-EQ', '2026-11-10', 'BUY', 'filled', 'daily_entry:beta_momentum_v1',
              '2026-11-11T08:51:55+05:30', DAY)),
            ('INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (NS, 'sig-late', 1, 'DLF-EQ', 'BUY', 66, 750.63, '2026-11-11T09:30:00+05:30',
              '2026-11-11T09:30:00+05:30', 'daily_entry:beta_momentum_v1', 0.0, -750.63, 'FEED_OPEN')),
        ])
        self.run_det()
        late = self.findings('signal_to_fill_latency')
        self.assertEqual(len(late), 1)
        self.assertAlmostEqual(late[0]['details']['max_latency_seconds'], 15 * 60, delta=1)
        self.assertTrue(late[0]['details']['samples'][0]['latency_start'].startswith('2026-11-11T09:15:00'))

    def test_rate_limit_stall_resolved_after_eod_even_with_later_failfast(self):
        # Long 429-style stalls, then EOD, then a short fail-fast error must not leave the episode OPEN.
        self.ev('2026-11-11T15:00:00+05:30', 'quote_batch_failed',
                {'error_code': 'RuntimeError', 'duration_ms': 130000}, outcome='failed')
        self.ev('2026-11-11T15:09:00+05:30', 'quote_batch_failed',
                {'error_code': 'RuntimeError', 'duration_ms': 130800}, outcome='failed')
        self.ev('2026-11-11T15:09:10+05:30', 'worker_failure',
                {'error_code': 'unclassified_condition'}, outcome='unclassified_condition')
        self.ev('2026-11-11T16:26:29+05:30', 'eod_completed', {'ledger_reconciled': True})
        self.ev('2026-11-11T16:41:00+05:30', 'quote_batch_failed',
                {'error_code': 'RuntimeError', 'duration_ms': 321}, outcome='failed')
        self.run_det()
        qb = self.findings('quote_batch_failed')
        self.assertEqual(len(qb), 1)
        self.assertEqual(qb[0]['status'], 'RESOLVED')
        self.assertTrue(qb[0]['details'].get('recovered'))
        tax = self.findings('error_taxonomy_gap')
        self.assertEqual(len(tax), 1)
        self.assertEqual(tax[0]['status'], 'RESOLVED')
        self.assertTrue(tax[0]['details'].get('recovered'))

    def test_latency_clock_helper(self):
        start = sa.parse_ist('2026-11-11T09:15:00+05:30')
        pre = sa.parse_ist('2026-11-11T08:51:55+05:30')
        mid = sa.parse_ist('2026-11-11T10:00:00+05:30')
        self.assertEqual(sa.latency_clock_start(pre, start), start)
        self.assertEqual(sa.latency_clock_start(mid, start), mid)

    def test_scan_started_on_time_and_finished_before_open_is_not_missed(self):
        # 12 Nov shape: started 08:50 IST, completed 09:01 IST (past 09:00 grace, before 09:15 open).
        self.ev('2026-11-11T08:50:51+05:30', 'scan_started')
        self.ev('2026-11-11T09:01:27+05:30', 'scan_completed')
        self.run_det('2026-11-11T11:15:00+05:30')
        self.assertEqual(self.findings('scan_missed'), [])

    def test_scan_completed_after_open_without_on_time_start_is_missed(self):
        self.ev('2026-11-11T09:13:40+05:30', 'scan_completed')
        self.run_det('2026-11-11T11:15:00+05:30')
        self.assertEqual(len(self.findings('scan_missed')), 1)

    def test_monitor_started_within_grace_is_not_worker_down(self):
        self.ev('2026-11-11T09:15:40+05:30', 'monitor_started')
        self.ev('2026-11-11T09:17:08+05:30', 'monitor_completed')
        self.ev('2026-11-11T09:18:15+05:30', 'monitor_completed')
        self.run_det('2026-11-11T11:15:00+05:30')
        self.assertEqual(self.findings('worker_down'), [])
        # No completion-to-completion gap above 120s, so no staleness either.
        self.assertEqual(self.findings('monitor_staleness'), [])

    def test_monitor_staleness_still_flags_a_real_completion_gap(self):
        self.ev('2026-11-11T09:15:40+05:30', 'monitor_started')
        self.ev('2026-11-11T09:17:00+05:30', 'monitor_completed')
        self.ev('2026-11-11T10:24:00+05:30', 'monitor_completed')  # 67-minute gap, not <2 min
        self.run_det('2026-11-11T11:15:00+05:30')
        stale = self.findings('monitor_staleness')
        self.assertEqual(len(stale), 1)
        self.assertGreater(stale[0]['details']['max_check_gap_seconds'], 120)

    def test_exit_selftest_sell_prevents_exit_path_unexercised(self):
        # paper_v1 had only buys; isolated selftest exercised the live SELL/trail path.
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='DLF-EQ')
        self.fill('2026-11-11T10:46:28+05:30', side='SELL', symbol='HDFCBANK-EQ', ns='paper_exit_test_v1')
        self.ev('2026-11-11T10:46:38+05:30', 'paper_fill_attempt', ns='paper_exit_test_v1',
                extra={'result': {'filled': True, 'side': 'SELL', 'symbol': 'RELIANCE-EQ', 'rule': 'trail_touch'}})
        self.ev('2026-11-11T10:47:29+05:30', 'command_completed', ns='paper_exit_test_v1', extra={'command': 'exit-selftest', 'status': 'ok'})
        self.run_det('2026-11-11T16:45:00+05:30')
        self.assertEqual(self.findings('exit_path_unexercised'), [])

    def test_exit_path_unexercised_when_selftest_absent(self):
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='DLF-EQ')
        self.run_det('2026-11-11T16:45:00+05:30')
        self.assertEqual(len(self.findings('exit_path_unexercised')), 1)

    def test_exit_selftest_survives_cleanup_when_only_payload_namespace_remains(self):
        # Production shape after --cleanup-only: Turso paper_exit_test_* rows are gone,
        # and engine.jsonl rows were merged under paper_v1 with the real namespace
        # and command only inside data_json.
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='ENGINERSIN-EQ', price=310.16)
        self.ev(
            '2026-11-11T10:47:29+05:30',
            'command_completed',
            ns='paper_v1',
            extra={
                'at_IST': '2026-11-11T10:47:29+05:30',
                'namespace': 'paper_exit_test_v1',
                'kind': 'command_completed',
                'command': 'exit-selftest',
                'status': 'ok',
            },
        )
        self.ev(
            '2026-11-11T10:46:49+05:30',
            'paper_fill_attempt',
            ns='paper_v1',
            extra={
                'namespace': 'paper_exit_test_v1',
                'kind': 'paper_fill_attempt',
                'symbol': 'RELIANCE-EQ',
                'result': {'filled': True, 'side': 'SELL', 'symbol': 'RELIANCE-EQ', 'rule': 'trail_touch'},
            },
        )
        self.run_det('2026-11-11T16:45:00+05:30')
        self.assertEqual(self.findings('exit_path_unexercised'), [])

    def test_exit_selftest_durable_record_survives_missing_events(self):
        import os, tempfile
        from pathlib import Path as P
        import support_exit_evidence as evd
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='TANLA-EQ', price=695.36)
        with tempfile.TemporaryDirectory() as td:
            path = P(td) / 'exit-selftest-evidence.json'
            evd.write_durable_evidence(path, {
                'session_day': DAY,
                'exercised': True,
                'sells': 4,
                'stop_or_target_hits': 2,
                'exit_selftest_commands': 2,
                'samples': [{'at': '2026-11-11T10:47:29+05:30', 'namespace': 'paper_exit_test_v1', 'command': 'exit-selftest'}],
                'source': 'test',
            }, session_day=DAY)
            old = os.environ.get('SUPPORT_EXIT_SELFTEST_EVIDENCE')
            os.environ['SUPPORT_EXIT_SELFTEST_EVIDENCE'] = str(path)
            try:
                self.run_det('2026-11-11T16:45:00+05:30')
            finally:
                if old is None:
                    os.environ.pop('SUPPORT_EXIT_SELFTEST_EVIDENCE', None)
                else:
                    os.environ['SUPPORT_EXIT_SELFTEST_EVIDENCE'] = old
        self.assertEqual(self.findings('exit_path_unexercised'), [])

    def test_prior_exit_path_finding_is_resolved_when_selftest_evidence_appears(self):
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='DELTACORP-EQ', price=83.55)
        self.run_det('2026-11-11T16:45:00+05:30')
        open_rows = [f for f in self.findings('exit_path_unexercised') if f['status'] == 'OPEN']
        self.assertEqual(len(open_rows), 1)
        self.ev(
            '2026-11-11T11:22:35+05:30',
            'command_completed',
            ns='paper_v1',
            extra={'namespace': 'paper_exit_test_v1', 'kind': 'command_completed', 'command': 'exit-selftest', 'status': 'ok'},
        )
        self.run_det('2026-11-11T16:46:00+05:30')
        rows = self.findings('exit_path_unexercised')
        self.assertTrue(rows)
        self.assertEqual(rows[0]['status'], 'RESOLVED')

    def test_session_ledger_fills_use_paper_fills_price_not_order_event_quote(self):
        self.fill('2026-11-11T09:16:00+05:30', side='BUY', symbol='ENGINERSIN-EQ', price=310.16, qty=161)
        self.fill('2026-11-11T09:16:11+05:30', side='BUY', symbol='TANLA-EQ', price=695.36, qty=71)
        self.ev('2026-11-11T09:16:00+05:30', 'paper_order_executed',
                data={'side': 'BUY', 'quantity': 161, 'price': 310.0}, symbol='ENGINERSIN-EQ')
        self.ev('2026-11-11T09:16:11+05:30', 'paper_order_executed',
                data={'side': 'BUY', 'quantity': 71, 'price': 695.0}, symbol='TANLA-EQ')
        fills = sa.session_ledger_fills(self.tdb, NS, DAY)
        by_sym = {f['symbol']: f['fill_price'] for f in fills}
        self.assertEqual(by_sym['ENGINERSIN-EQ'], 310.16)
        self.assertEqual(by_sym['TANLA-EQ'], 695.36)
        self.assertNotIn(310.0, by_sym.values())
        self.assertNotIn(695.0, by_sym.values())


if __name__ == '__main__':
    unittest.main()
