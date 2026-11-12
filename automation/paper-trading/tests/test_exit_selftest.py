import json, sys, unittest, uuid
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e
import exit_selftest as xst

T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
NS_V1 = 'paper_v1'


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


class ExitSelftestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = e.ROOT / 'tests' / ('test_xst_' + uuid.uuid4().hex + '.sqlite')
        cls.db = e.DB(str(cls.path))
        e.NS = 'test_fixture'
        e.now = lambda: T
        e.init(cls.db)
        cls.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)',())])

    @classmethod
    def tearDownClass(cls):
        cls.path.unlink(missing_ok=True)

    def setUp(self):
        e.now = lambda: T
        self.ns = xst.TEST_NS
        e.NS = self.ns
        xst.cleanup(e, self.db, self.ns)
        # A protected paper_v1 row must survive the harness.
        self.db.batch([
            ('DELETE FROM paper_positions WHERE namespace=?', (NS_V1,)),
            ('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',
             (NS_V1, 1, 'TCS-EQ', 3, 100, T.isoformat(), 100, T.isoformat())),
        ])

    def tearDown(self):
        xst.cleanup(e, self.db, self.ns)
        e.NS = 'test_fixture'

    def test_uses_own_lock_not_engine_lock(self):
        self.assertFalse(e.uses_engine_lock('exit-selftest'))
        self.assertTrue(e.uses_engine_lock('scan'))

    def test_cleanup_refuses_paper_v1(self):
        with self.assertRaisesRegex(ValueError, 'protected'):
            xst.cleanup(e, self.db, 'paper_v1')
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V1, 'TCS-EQ'))[0]['qty'], 3)

    def test_s7_trail_touch_uses_production_monitor_and_fill(self):
        # low 99 <= level ~99.5 < open 100, last 101 → trail_touch, fill at level (then sell slippage).
        q = {'symbol': 'RELIANCE-EQ', 'price': 101, 'open': 100, 'low': 99, 'high': 102,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=False, live=False, quotes_seed={'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        case = {c['name']: c for c in r['cases']}
        self.assertTrue(case['s7_trail_stop']['passed'], case['s7_trail_stop'])
        self.assertEqual(case['s7_trail_stop']['rule'], 'trail_touch')
        self.assertEqual(case['s7_trail_stop']['execution_path'], 'LTP_LEGACY')
        self.assertEqual(case['s7_trail_stop']['reference_price'], case['s7_trail_stop']['level'])
        # qty=1 can round modeled sell slippage to 0 paise; the fill must not be better than the level.
        self.assertLessEqual(case['s7_trail_stop']['exit_price'], case['s7_trail_stop']['level'])
        costs = self.db.query("SELECT total_paise FROM paper_fill_costs WHERE namespace=? AND side='SELL' AND symbol=?", (self.ns, 'RELIANCE-EQ'))
        self.assertTrue(costs and costs[0]['total_paise'] > 0)
        ev = self.db.query("SELECT outcome FROM paper_events WHERE namespace=? AND event_type='paper_order_result'", (self.ns,))
        self.assertTrue(any(x['outcome'] == 'filled' for x in ev))
        self.assertFalse(self.db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0', (self.ns, 'RELIANCE-EQ')))
        # paper_v1 untouched
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V1, 'TCS-EQ'))[0]['qty'], 3)

    def test_pending_sell_fills_official_open_on_first_fresh_quote(self):
        q = {'symbol': 'HDFCBANK-EQ', 'price': 105, 'open': 100, 'low': 99, 'high': 106,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        # RELIANCE quote with level unhittable so only the pending SELL fires if we only seeded HDFC — harness seeds both; make RELIANCE not trigger:
        rq = {'symbol': 'RELIANCE-EQ', 'price': 110, 'open': 110, 'low': 109, 'high': 111,
              'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'HDFCBANK-EQ': q, 'RELIANCE-EQ': rq})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=False, live=False,
                    quotes_seed={'HDFCBANK-EQ': q, 'RELIANCE-EQ': rq})
        case = {c['name']: c for c in r['cases']}
        self.assertTrue(case['pending_sell_exit']['passed'], case['pending_sell_exit'])
        self.assertEqual(case['pending_sell_exit']['execution_path'], 'FEED_OPEN')
        self.assertEqual(case['pending_sell_exit']['reference_price'], 100)

    def test_stale_quote_is_ignored_then_fresh_quote_exits(self):
        fresh = {'symbol': 'HDFCBANK-EQ', 'price': 105, 'open': 100, 'low': 99, 'high': 106,
                 'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        stale = dict(fresh, quote_at=(T - timedelta(seconds=120)).isoformat())
        rq = {'symbol': 'RELIANCE-EQ', 'price': 110, 'open': 110, 'low': 109, 'high': 111,
              'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'HDFCBANK-EQ': fresh, 'RELIANCE-EQ': rq})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=False, live=False,
                    quotes_seed={'HDFCBANK-EQ': stale, 'RELIANCE-EQ': rq}, stale_first=True, later_quotes={'HDFCBANK-EQ': fresh, 'RELIANCE-EQ': rq})
        case = {c['name']: c for c in r['cases']}
        self.assertTrue(case['stale_quote_ignored']['passed'], case['stale_quote_ignored'])
        self.assertTrue(case['pending_sell_exit']['passed'], case['pending_sell_exit'])

    def test_no_duplicate_exit_and_cleanup_drops_only_test_ns(self):
        q = {'symbol': 'RELIANCE-EQ', 'price': 101, 'open': 100, 'low': 99, 'high': 102,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=True, live=False,
                    quotes_seed={'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        self.assertTrue(r['cleanup']['removed'])
        self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_positions WHERE namespace=?', (self.ns,))[0]['n'], 0)
        self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_fills WHERE namespace=?', (self.ns,))[0]['n'], 0)
        self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_events WHERE namespace=?', (self.ns,))[0]['n'], 0)
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V1, 'TCS-EQ'))[0]['qty'], 3)
        self.assertTrue(any(c['name'] == 'no_duplicate_exit' and c['passed'] for c in r['cases']), r['cases'])

    def test_refuses_to_run_if_budget_too_tight(self):
        q = {'symbol': 'RELIANCE-EQ', 'price': 100, 'open': 100, 'low': 99, 'quote_at': T.isoformat(), 'provider_status': 'ok'}
        api = FakeAPI({'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        api.budget['remaining_today'] = 1510
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=40, poll_seconds=0, cleanup_after=True, live=False,
                    quotes_seed={'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        self.assertEqual(r['status'], 'budget_reserve_would_be_breached')
        self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_positions WHERE namespace=?', (self.ns,))[0]['n'], 0)


if __name__ == '__main__':
    unittest.main()
