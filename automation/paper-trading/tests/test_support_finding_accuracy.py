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

    def ev(self, at, event_type, data=None, outcome='ok'):
        self.n += 1
        self.tdb.batch([(
            'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (NS, f'e{self.n:05d}', 'r', DAY, at, at, event_type, 'INFO', outcome, json.dumps(data or {})),
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


if __name__ == '__main__':
    unittest.main()
