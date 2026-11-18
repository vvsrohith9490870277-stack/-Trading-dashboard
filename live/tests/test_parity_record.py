"""Task 4 tests: real Track A replica parity, offline with synthetic fixtures.

Fixtures are clearly synthetic (AAA-EQ / BBB-EQ, round prices) and are never
presented as real data. Comparison basis (per the engine's own definition in
adaptive_engine.classify_discordant_trades): only trades whose entry_date is
after warm_start_end count — warm-start-inherited entries/exits are excluded.
"""
import json, sqlite3, sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_logging as alog
import adaptive_observation as aobs
import parity_record as pr
from track_a_parity import compare as parity_compare

NS = 'adaptive_v1'
DAY = '2026-11-11'
WS_END = '2026-11-09'


class FakeDB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:'); self.c.row_factory = sqlite3.Row
    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)
    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def make_db():
    db = FakeDB(); alog.init(db); aobs.init(db)
    return db


def replica_trades():
    return [
        {'symbol': 'AAA-EQ', 'entry_date': '2026-11-11', 'exit_date': '2026-11-12', 'qty': 10,
         'entry_price': 100.0, 'exit_price': 101.0, 'net_pnl_paise': 1000, 'side': 'BUY'},
        {'symbol': 'CCC-EQ', 'entry_date': '2026-11-10', 'exit_date': '2026-11-11', 'qty': 5,
         'entry_price': 50.0, 'exit_price': 52.0, 'net_pnl_paise': 1000, 'side': 'BUY'},
    ]


def live_fills():
    return [
        {'strategy': 1, 'symbol': 'AAA-EQ', 'side': 'BUY', 'qty': 10, 'price': 100.0,
         'filled_at': '2026-11-11T09:15:00+05:30'},
        {'strategy': 1, 'symbol': 'CCC-EQ', 'side': 'SELL', 'qty': 5, 'price': 52.0,
         'filled_at': '2026-11-11T09:15:00+05:30'},
    ]


def record(db, **kw):
    args = dict(reconciled=True, simulated=True, live_fills=live_fills(),
                replica_closed_trades=replica_trades(), replica_open_positions=[],
                replica_entry_skips=[{'day': DAY, 'symbol': 'BBB-EQ', 'cause': 'UPPER_CIRCUIT_BUY_BLOCK'}],
                warm_start_end=WS_END, replica_equity_paise=1000000, live_equity_paise=1000000)
    args.update(kw)
    return pr.record_for_session(db, NS, 1, DAY, **args)


class ParityRecordTests(unittest.TestCase):
    def test_full_match_records_parity_ok_and_streak_increments(self):
        db = make_db()
        out = record(db)
        self.assertEqual(out['status'], 'compared')
        self.assertTrue(out['parity_ok'])
        self.assertTrue(out['fills_match']); self.assertTrue(out['skips_match']); self.assertTrue(out['qty_match'])
        rows = db.query('SELECT * FROM adaptive_parity WHERE namespace=? AND strategy=?', (NS, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['parity_ok'], 1)
        # Streak over the one counted session is 1.
        self.assertEqual(aobs.parity_streak(db, NS, 1, [DAY])['streak'], 1)

    def test_inherited_warm_start_trade_is_not_discordance(self):
        db = make_db()
        trades = replica_trades() + [
            {'symbol': 'OLD-EQ', 'entry_date': '2026-11-09', 'exit_date': '2026-11-11', 'qty': 7,
             'entry_price': 20.0, 'exit_price': 21.0, 'net_pnl_paise': 700, 'side': 'BUY'},
        ]
        out = record(db, replica_closed_trades=trades)
        self.assertEqual(out['status'], 'compared')
        self.assertTrue(out['parity_ok'], out)  # live never held OLD-EQ; its warm-start exit is excluded

    def test_one_fill_price_mismatch_records_parity_ok_zero_and_streak_resets(self):
        db = make_db()
        bad = live_fills(); bad[0] = {**bad[0], 'price': 100.5}
        out = record(db, live_fills=bad)
        self.assertEqual(out['status'], 'compared')
        self.assertFalse(out['parity_ok'])
        self.assertFalse(out['fills_match'])
        self.assertEqual(aobs.parity_streak(db, NS, 1, [DAY])['streak'], 0)

    def test_qty_mismatch_and_skip_mismatch(self):
        db = make_db()
        bad = live_fills(); bad[0] = {**bad[0], 'qty': 11}
        out = record(db, live_fills=bad)
        self.assertFalse(out['qty_match']); self.assertFalse(out['parity_ok'])
        # Live bought a symbol the replica skipped -> skips do not match.
        bought_skipped = live_fills() + [{'strategy': 1, 'symbol': 'BBB-EQ', 'side': 'BUY', 'qty': 1,
                                          'price': 10.0, 'filled_at': '2026-11-11T09:15:00+05:30'}]
        out = record(db, live_fills=bought_skipped)
        self.assertFalse(out['skips_match']); self.assertFalse(out['parity_ok'])

    def test_equity_diff_beyond_tolerance_is_parity_ok_zero(self):
        db = make_db()
        out = record(db, live_equity_paise=1000000 - 101)  # tolerance is 100 paise
        self.assertEqual(out['equity_diff_paise'], 101)
        self.assertFalse(out['parity_ok'])
        out = record(db, live_equity_paise=1000000 - 100)
        self.assertTrue(out['parity_ok'])

    def test_non_reconciled_or_missing_live_fills_writes_nothing(self):
        db = make_db()
        for kw in ({'reconciled': False}, {'live_fills': None}, {'simulated': False}):
            out = record(db, **kw)
            self.assertEqual(out['status'], 'not_evaluable', kw)
            self.assertIn('reason', out)
        rows = db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n']
        self.assertEqual(rows, 0)

    def test_idempotent_rerun_does_not_duplicate_rows(self):
        db = make_db()
        record(db); record(db)
        rows = db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n']
        self.assertEqual(rows, 1)


class TradeParityLabelTests(unittest.TestCase):
    def _adaptive(self, **kw):
        d = {'copied_equity_rows': [], 'daily_report': {'simulation_status': 'SIMULATED'},
             'daily_returns_rows': 5, 'bhavcopy_archive_days': ['2026-11-11'], 'parity_rows': 0}
        d.update(kw)
        return d

    def test_compared_only_when_trade_level_comparison_ran(self):
        live = {'sleeves': {}, 'open_positions_now': {'total': 0, 'by_strategy': {}}}
        out = parity_compare(live, self._adaptive(parity_rows=0))
        self.assertEqual(out['trade_parity'], 'not_evaluable')
        self.assertTrue(any('parity' in r for r in out['reasons']))
        out = parity_compare(live, self._adaptive(parity_rows=1))
        self.assertEqual(out['trade_parity'], 'compared')


if __name__ == '__main__':
    unittest.main()
