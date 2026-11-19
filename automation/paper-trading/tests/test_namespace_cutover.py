"""paper_v2 cutover (Task 1): config namespace, protected books, support-agent defaults."""
import hashlib, inspect, json, sys, tempfile, unittest, uuid
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e
import exit_selftest as xst
import support_agent as sa

T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
NS_V1 = 'paper_v1'
NS_V2 = 'paper_v2'


class FakeAPI:
    def __init__(self, quotes):
        self._quotes = quotes
        self.calls = 0
        self.budget = {'day': '2026-11-09', 'requests': 10, 'remaining_today': 3000,
                       'per_minute': 0, 'per_second': 0,
                       'limits': {'per_day': 4000, 'per_minute': 40, 'per_second': 4,
                                  'reserve_for_session_monitor': 1500}}

    def quotes(self, symbols):
        self.calls += 1
        return {s: dict(self._quotes[s]) for s in symbols if s in self._quotes}

    def budget_snapshot(self):
        snap = dict(self.budget)
        snap['requests'] = int(self.budget.get('requests') or 0) + self.calls
        snap['remaining_today'] = int(self.budget.get('remaining_today') or 0) - self.calls
        return snap


class CutoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = e.ROOT / 'tests' / ('test_cutover_' + uuid.uuid4().hex + '.sqlite')
        cls.db = e.DB(str(cls.path))
        cls._prev_ns = e.NS
        cls._prev_now = e.now
        e.NS = 'test_fixture'
        e.now = lambda: T
        e.init(cls.db)
        cls.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)', ())])

    @classmethod
    def tearDownClass(cls):
        e.NS = cls._prev_ns
        e.now = cls._prev_now
        cls.path.unlink(missing_ok=True)

    def setUp(self):
        e.now = lambda: T
        self.ns = xst.TEST_NS
        e.NS = self.ns
        xst.cleanup(e, self.db, self.ns)
        # A protected paper_v2 row must survive the harness (paper_v1 is covered
        # by test_exit_selftest.py's NS_V1 seed).
        self.db.batch([
            ('DELETE FROM paper_positions WHERE namespace=?', (NS_V1,)),
            ('DELETE FROM paper_positions WHERE namespace=?', (NS_V2,)),
            ('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',
             (NS_V2, 1, 'TCS-EQ', 3, 100, T.isoformat(), 100, T.isoformat())),
        ])

    def tearDown(self):
        xst.cleanup(e, self.db, self.ns)
        e.NS = 'test_fixture'

    def test_repo_config_namespace_is_paper_v2(self):
        cfg = json.loads((e.ROOT / 'config.json').read_text())
        self.assertEqual(cfg['namespace'], 'paper_v2')

    def test_exit_selftest_protects_both_books(self):
        self.assertTrue({'paper_v1', 'paper_v2'} <= set(xst.PROTECTED))
        for ns in ('paper_v1', 'paper_v2'):
            with self.assertRaisesRegex(ValueError, 'protected'):
                xst._require_test_ns(ns)
            with self.assertRaisesRegex(ValueError, 'protected'):
                xst.cleanup(e, self.db, ns)
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V2, 'TCS-EQ'))[0]['qty'], 3)

    def test_exit_selftest_reports_live_ns_unchanged(self):
        q = {'symbol': 'RELIANCE-EQ', 'price': 101, 'open': 100, 'low': 99, 'high': 102,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=False, live=False,
                    quotes_seed={'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        self.assertEqual(r['live_ns'], 'paper_v2')
        self.assertTrue(r['live_ns_positions_unchanged'])
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V2, 'TCS-EQ'))[0]['qty'], 3)
        # Existing paper_v1 report keys are kept for other tools.
        self.assertIn('paper_v1_before', r)
        self.assertIn('paper_v1_after', r)
        self.assertIn('paper_v1_positions_unchanged', r)

    def test_support_agent_defaults_follow_config(self):
        for fn in (sa.build_nov9_replay_fixture, sa.run_virtual_clock_replay, sa.write_heartbeat):
            self.assertIsNone(inspect.signature(fn).parameters['namespace'].default)
        prev = sa.CFG['namespace']
        try:
            sa.CFG['namespace'] = 'paper_cutover_test_v1'
            with tempfile.TemporaryDirectory() as tmp:
                payload = sa.write_heartbeat(tmp)
                self.assertEqual(payload['namespace'], 'paper_cutover_test_v1')
                explicit = sa.write_heartbeat(tmp, 'paper_v1')
                self.assertEqual(explicit['namespace'], 'paper_v1')
        finally:
            sa.CFG['namespace'] = prev


if __name__ == '__main__':
    unittest.main()
