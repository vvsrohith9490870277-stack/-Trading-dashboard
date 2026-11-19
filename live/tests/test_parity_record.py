"""Task 4 tests: real Track A replica parity, offline with synthetic fixtures.

Fixtures are clearly synthetic (AAA-EQ / BBB-EQ, round prices) and are never
presented as real data. Comparison basis (per the engine's own definition in
adaptive_engine.classify_discordant_trades): only trades whose entry_date is
after warm_start_end count — warm-start-inherited entries/exits are excluded.

R4 HOLD: record_for_session writes no row until Grok approves a basis, so the
comparison assertions in this file drive the pure compare_session_parity
directly; parity_ok / streak assertions drive aobs.record_parity directly.
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


def compare(**kw):
    """Pure comparison for one session (R4: record_for_session is held, so the
    comparison assertions live here, on compare_session_parity directly)."""
    args = dict(replica_closed_trades=replica_trades(), replica_open_positions=[],
                replica_entry_skips=[{'day': DAY, 'symbol': 'BBB-EQ', 'cause': 'UPPER_CIRCUIT_BUY_BLOCK'}],
                live_fills=live_fills(), warm_start_end=WS_END, session_day=DAY,
                replica_equity_paise=1000000, live_equity_paise=1000000, strategy=1)
    args.update(kw)
    return pr.compare_session_parity(**args)


class ParityRecordTests(unittest.TestCase):
    def test_parity_basis_hold_writes_no_row(self):
        # R4: until Grok approves a comparison basis, record_for_session
        # writes no adaptive_parity row, even on the full SIMULATED fixture.
        db = make_db()
        out = record(db)
        self.assertEqual(out, {'status': 'not_evaluable', 'reason': 'parity_basis_pending_review'})
        rows = db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n']
        self.assertEqual(rows, 0)

    def test_full_match_pure_comparison_agrees(self):
        # Pure comparison (was: record_for_session wrote parity_ok=1).
        out = compare()
        self.assertTrue(out['fills_match']); self.assertTrue(out['skips_match']); self.assertTrue(out['qty_match'])
        self.assertEqual(out['equity_diff_paise'], 0)
        self.assertEqual(out['reasons'], [])
        # The parity_ok / streak rule itself (aobs.record_parity) is unchanged:
        # an exact match still records parity_ok=1 and a streak of 1.
        db = make_db()
        self.assertTrue(aobs.record_parity(db, NS, 1, DAY, out['fills_match'], out['skips_match'],
                                           out['qty_match'], out['equity_diff_paise']))
        rows = db.query('SELECT * FROM adaptive_parity WHERE namespace=? AND strategy=?', (NS, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['parity_ok'], 1)
        self.assertEqual(aobs.parity_streak(db, NS, 1, [DAY])['streak'], 1)

    def test_other_strategy_fills_do_not_break_parity(self):
        # R3: live fills for another strategy on the same day must not be
        # compared against this strategy's replica fills (pure comparison).
        fills = live_fills() + [{'strategy': 6, 'symbol': 'ZZZ-EQ', 'side': 'BUY', 'qty': 3,
                                 'price': 77.0, 'filled_at': '2026-11-11T09:16:00+05:30'}]
        out = compare(live_fills=fills)
        self.assertTrue(out['fills_match'], out)
        self.assertEqual(len(out['live_fills']), 2)  # only strategy 1 fills compared

    def test_inherited_warm_start_trade_is_not_discordance(self):
        trades = replica_trades() + [
            {'symbol': 'OLD-EQ', 'entry_date': '2026-11-09', 'exit_date': '2026-11-11', 'qty': 7,
             'entry_price': 20.0, 'exit_price': 21.0, 'net_pnl_paise': 700, 'side': 'BUY'},
        ]
        out = compare(replica_closed_trades=trades)
        self.assertTrue(out['fills_match'], out)  # live never held OLD-EQ; its warm-start exit is excluded

    def test_one_fill_price_mismatch_is_not_a_match(self):
        bad = live_fills(); bad[0] = {**bad[0], 'price': 100.5}
        out = compare(live_fills=bad)
        self.assertFalse(out['fills_match'])
        self.assertIn('replica fills for the session differ from live fills', out['reasons'])
        db = make_db()
        self.assertFalse(aobs.record_parity(db, NS, 1, DAY, out['fills_match'], out['skips_match'],
                                            out['qty_match'], out['equity_diff_paise']))
        self.assertEqual(aobs.parity_streak(db, NS, 1, [DAY])['streak'], 0)

    def test_qty_mismatch_and_skip_mismatch(self):
        bad = live_fills(); bad[0] = {**bad[0], 'qty': 11}
        out = compare(live_fills=bad)
        self.assertFalse(out['qty_match']); self.assertFalse(out['fills_match'])
        # Live bought a symbol the replica skipped -> skips do not match.
        bought_skipped = live_fills() + [{'strategy': 1, 'symbol': 'BBB-EQ', 'side': 'BUY', 'qty': 1,
                                          'price': 10.0, 'filled_at': '2026-11-11T09:15:00+05:30'}]
        out = compare(live_fills=bought_skipped)
        self.assertFalse(out['skips_match'])

    def test_equity_diff_beyond_tolerance_is_parity_ok_zero(self):
        out = compare(live_equity_paise=1000000 - 101)  # tolerance is 100 paise
        self.assertEqual(out['equity_diff_paise'], 101)
        db = make_db()
        self.assertFalse(aobs.record_parity(db, NS, 1, DAY, out['fills_match'], out['skips_match'],
                                            out['qty_match'], out['equity_diff_paise']))
        out = compare(live_equity_paise=1000000 - 100)
        self.assertTrue(aobs.record_parity(db, NS, 1, DAY, out['fills_match'], out['skips_match'],
                                           out['qty_match'], out['equity_diff_paise']))

    def test_non_reconciled_or_missing_live_fills_writes_nothing(self):
        db = make_db()
        for kw in ({'reconciled': False}, {'live_fills': None}, {'simulated': False}):
            out = record(db, **kw)
            self.assertEqual(out['status'], 'not_evaluable', kw)
            self.assertIn('reason', out)
        rows = db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n']
        self.assertEqual(rows, 0)

    def test_hold_rerun_still_writes_no_rows(self):
        # R4: re-running the held recorder never creates a row.
        db = make_db()
        record(db); record(db)
        rows = db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n']
        self.assertEqual(rows, 0)


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
