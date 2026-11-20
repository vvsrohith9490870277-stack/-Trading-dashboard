"""B2 (ii): live-vs-replica parity recording under the APPROVED P2+E1 basis.

Approved by the user 2026-11-18 21:37 IST (Review 2): match key
(strategy, symbol, side, session day); Track A decides on D's close and fills
on D+1, compare on the fill day; price and equity are informational with
bps/percent differences and NO pass/fail tolerance until the user sets one;
qty reported, not matched; matched / live-only / replica-only counts per
strategy-day; rows only for reconciled + SIMULATED days with live fills;
idempotent replace; only adaptive_parity in the sidecar DB; no policy numbers.

What changed vs the D3 tests this file replaces (each old test is accounted
for here — nothing silently dropped):
- test_parity_basis_hold_writes_no_row / test_hold_rerun_still_writes_no_rows:
  the R4 hold is LIFTED on this branch only (the approved basis now exists),
  so record_for_session writes a row; replaced by
  test_full_match_records_informational_row and test_idempotent_rerun.
- test_one_fill_price_mismatch_is_not_a_match: a price mismatch is NOT a
  non-match under P2+E1 (price is informational); replaced by
  test_price_informational_only.
- test_qty_mismatch_and_skip_mismatch: qty is REPORTED, not a match
  condition; replaced by test_partial_overlap_counts (qty_diff in the report).
- test_equity_diff_beyond_tolerance_is_parity_ok_zero: there is NO tolerance
  under E1; replaced by test_equity_change_based_not_level.
- test_non_reconciled_or_missing_live_fills_writes_nothing: kept as
  test_no_false_evidence_rows (plus the protected-namespace guard).
- test_full_match_pure_comparison_agrees: the aobs.record_parity D3 path
  (exact match + 100 paise tolerance) is NOT used by the new recorder — it
  lives in adaptive_observation.py, which this branch does not touch — so
  the D3 parity_ok/streak assertions are dropped, not updated.
- test_other_strategy_fills_do_not_break_parity: kept as
  test_parity_key_includes_strategy.
- test_inherited_warm_start_trade_is_not_discordance: kept unchanged.
- TradeParityLabelTests moved to live/tests/test_track_a_parity.py.

Fixtures are clearly synthetic (AAA-EQ / BBB-EQ, round prices) and are never
presented as real data.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_logging as alog
import adaptive_observation as aobs
import parity_record as pr

NS = 'adaptive_v1'
DAY = '2026-11-11'
WS_END = '2026-11-09'


class FakeDB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row

    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)

    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def make_db():
    db = FakeDB()
    alog.init(db)
    aobs.init(db)
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
    args = dict(replica_closed_trades=replica_trades(), replica_open_positions=[],
                replica_entry_skips=[{'day': DAY, 'symbol': 'BBB-EQ', 'cause': 'UPPER_CIRCUIT_BUY_BLOCK'}],
                live_fills=live_fills(), warm_start_end=WS_END, session_day=DAY,
                replica_equity_paise=1000000, live_equity_paise=1000000, strategy=1)
    args.update(kw)
    return pr.compare_session_parity(**args)


def row(db, strategy=1):
    rows = db.query('SELECT * FROM adaptive_parity WHERE namespace=? AND strategy=?', (NS, strategy))
    return rows[0] if rows else None


class ParityRecordTests(unittest.TestCase):
    def test_parity_key_includes_strategy(self):
        # Condition 1 / amendment (a): key is (strategy, symbol, side, day).
        # Live fills for strategy 6 on the same day must not be compared
        # against strategy 1's replica fills.
        fills = live_fills() + [{'strategy': 6, 'symbol': 'ZZZ-EQ', 'side': 'BUY', 'qty': 3,
                                 'price': 77.0, 'filled_at': '2026-11-11T09:16:00+05:30'}]
        out = compare(live_fills=fills)
        self.assertEqual(out['counts'], {'matched': 2, 'live_only': 0, 'replica_only': 0})
        self.assertEqual(len(out['live_fills']), 2)  # only strategy 1 fills compared
        # Recording strategy 1 and strategy 6 separately gives two rows.
        db = make_db()
        record(db)
        out6 = pr.record_for_session(db, NS, 6, DAY, reconciled=True, simulated=True,
                                     live_fills=fills, replica_closed_trades=[], replica_open_positions=[],
                                     replica_entry_skips=[], warm_start_end=WS_END,
                                     replica_equity_paise=500000, live_equity_paise=500000)
        self.assertEqual(out6['status'], 'recorded')
        self.assertEqual(db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n'], 2)
        self.assertEqual(json.loads(row(db, 6)['details_json'])['counts'],
                         {'matched': 0, 'live_only': 1, 'replica_only': 0})

    def test_signal_day_vs_fill_day(self):
        # Condition 2: Track A decides on D's close (2026-11-10) and fills on
        # D+1 (2026-11-11). The replica stamps entry_date/exit_date with the
        # FILL day (the simulator fills at the open of t+1), and the live
        # filled_at date is the fill day too — so the comparison is made on
        # D+1, not D. Comparing on D must find no fills on either side.
        out = compare(session_day='2026-11-11')
        self.assertEqual(out['counts']['matched'], 2)
        # On D itself the AAA fill (signalled on D, filled on D+1) is absent
        # from both sides, so it is NOT matched on D — the comparison for it
        # happens on D+1. (The CCC BUY filled on D itself, from a signal on
        # 2026-11-09, is correctly replica-only on D.)
        out_d = compare(session_day='2026-11-10')
        self.assertEqual(out_d['counts'], {'matched': 0, 'live_only': 0, 'replica_only': 1})
        self.assertEqual([x['symbol'] for x in out_d['replica_only']], ['CCC-EQ'])
        self.assertNotIn('AAA-EQ', [x['symbol'] for x in out_d['replica_fills']])
        self.assertEqual(out_d['live_fills'], [])

    def test_price_informational_only(self):
        # Condition 3 / amendment (c): price is informational, not pass/fail.
        # A 50 bps live-vs-replica difference still matches on the key, and
        # both prices plus the bps difference are recorded.
        bad = live_fills()
        bad[0] = {**bad[0], 'price': 100.5}  # +50 bps vs replica 100.00
        out = compare(live_fills=bad)
        self.assertEqual(out['counts']['matched'], 2)
        m = [x for x in out['matched'] if x['symbol'] == 'AAA-EQ'][0]
        self.assertEqual(m['live_price'], 100.5)
        self.assertEqual(m['replica_price'], 100.0)
        self.assertAlmostEqual(m['price_bps_diff'], 50.0)
        db = make_db()
        record(db, live_fills=bad)
        r = row(db)
        self.assertIsNotNone(r)
        self.assertEqual(r['parity_ok'], 0)  # no pass/fail under P2+E1
        details = json.loads(r['details_json'])
        self.assertEqual(details['verdict'], 'informational_no_pass_fail')
        self.assertFalse(any('fail' in x.lower() for x in out['reasons']))

    def test_partial_overlap_counts(self):
        # Condition 4: partial overlap is expected — report matched /
        # live-only / replica-only counts per strategy-day, and report qty
        # (amendment (b)) instead of matching on it.
        live = [{'strategy': 1, 'symbol': 'AAA-EQ', 'side': 'BUY', 'qty': 12, 'price': 100.0,
                 'filled_at': '2026-11-11T09:15:00+05:30'},
                {'strategy': 1, 'symbol': 'DDD-EQ', 'side': 'BUY', 'qty': 7, 'price': 30.0,
                 'filled_at': '2026-11-11T09:15:00+05:30'}]
        out = compare(live_fills=live)
        self.assertEqual(out['counts'], {'matched': 1, 'live_only': 1, 'replica_only': 1})
        self.assertEqual([x['symbol'] for x in out['live_only']], ['DDD-EQ'])
        self.assertEqual([x['symbol'] for x in out['replica_only']], ['CCC-EQ'])
        aaa = [x for x in out['matched'] if x['symbol'] == 'AAA-EQ'][0]
        self.assertEqual(aaa['live_qty'], 12)
        self.assertEqual(aaa['replica_qty'], 10)
        self.assertEqual(aaa['qty_diff'], 2)

    def test_equity_change_based_not_level(self):
        # Condition 5 / amendment (d): only a CHANGE-based figure is
        # meaningful — never the level difference.
        # Case A: levels differ by 1000 paise but the day-over-day changes
        # are equal -> recorded diff is 0.
        out = compare(replica_equity_paise=1000000, live_equity_paise=999000,
                      replica_equity_prev_paise=900000, live_equity_prev_paise=899000)
        self.assertEqual(out['equity_change']['replica_change_paise'], 100000)
        self.assertEqual(out['equity_change']['live_change_paise'], 100000)
        self.assertEqual(out['equity_change']['diff_paise'], 0)
        # Case B: levels are EQUAL but the changes differ -> diff is recorded.
        out = compare(replica_equity_paise=1000000, live_equity_paise=1000000,
                      replica_equity_prev_paise=900000, live_equity_prev_paise=950000)
        self.assertEqual(out['equity_change']['diff_paise'], 50000)
        self.assertAlmostEqual(out['equity_change']['diff_inr'], 500.0)
        self.assertAlmostEqual(out['equity_change']['diff_pct'], 100.0)
        # Prev equities missing -> equity is not_evaluable; no level diff is
        # recorded as the equity figure.
        out = compare()
        self.assertIsNone(out['equity_change'])
        self.assertTrue(any('equity_change_not_evaluable' in r for r in out['reasons']))

    def test_no_false_evidence_rows(self):
        # Condition 6: rows only for reconciled + SIMULATED days with live
        # fills available. None of these writes a row.
        db = make_db()
        for kw in ({'reconciled': False}, {'simulated': False}, {'live_fills': None}):
            out = record(db, **kw)
            self.assertEqual(out['status'], 'not_evaluable', kw)
            self.assertIn('reason', out)
        self.assertEqual(db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n'], 0)

    def test_idempotent_rerun(self):
        # Condition 6: idempotent re-run replaces the row.
        db = make_db()
        self.assertEqual(record(db)['status'], 'recorded')
        changed = live_fills()
        changed[0] = {**changed[0], 'qty': 11}
        out = record(db, live_fills=changed)
        self.assertEqual(out['status'], 'recorded')
        self.assertEqual(db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n'], 1)
        details = json.loads(row(db)['details_json'])
        self.assertEqual(details['counts'], {'matched': 2, 'live_only': 0, 'replica_only': 0})
        aaa = [x for x in details['matched'] if x['symbol'] == 'AAA-EQ'][0]
        self.assertEqual(aaa['live_qty'], 11)
        self.assertEqual(aaa['qty_diff'], 1)

    def test_never_writes_protected_namespaces(self):
        # Condition 6: never write for paper_v1 / paper_demo_v1 / paper_v2 —
        # only adaptive_parity in the sidecar DB.
        db = make_db()
        db.batch([('CREATE TABLE sentinel(x TEXT)', ()), ("INSERT INTO sentinel VALUES('keep')", ())])
        for ns in ('paper_v1', 'paper_demo_v1', 'paper_v2'):
            out = pr.record_for_session(db, ns, 1, DAY, reconciled=True, simulated=True,
                                        live_fills=live_fills(), replica_closed_trades=replica_trades(),
                                        replica_open_positions=[], replica_entry_skips=[],
                                        warm_start_end=WS_END, replica_equity_paise=1000000,
                                        live_equity_paise=1000000)
            self.assertEqual(out, {'status': 'not_evaluable', 'reason': 'protected_namespace'})
        self.assertEqual(db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n'], 0)
        self.assertEqual(db.query('SELECT * FROM sentinel'), [{'x': 'keep'}])
        # And a legitimate recording writes ONLY the adaptive_parity table.
        record(db)
        self.assertEqual(db.query('SELECT COUNT(*) n FROM adaptive_parity')[0]['n'], 1)
        self.assertEqual(db.query('SELECT * FROM sentinel'), [{'x': 'keep'}])

    def test_full_match_records_informational_row(self):
        out = compare()
        self.assertEqual(out['counts'], {'matched': 2, 'live_only': 0, 'replica_only': 0})
        self.assertTrue(out['skips_match'])
        db = make_db()
        res = record(db)
        self.assertEqual(res['status'], 'recorded')
        r = row(db)
        self.assertEqual(r['fills_match'], 1)
        self.assertEqual(r['skips_match'], 1)
        self.assertEqual(r['qty_match'], 1)
        self.assertEqual(r['parity_ok'], 0)  # informational only, no verdict
        details = json.loads(r['details_json'])
        self.assertEqual(details['basis'], 'P2+E1_informational')
        self.assertEqual(details['verdict'], 'informational_no_pass_fail')

    def test_inherited_warm_start_trade_is_not_discordance(self):
        trades = replica_trades() + [
            {'symbol': 'OLD-EQ', 'entry_date': '2026-11-09', 'exit_date': '2026-11-11', 'qty': 7,
             'entry_price': 20.0, 'exit_price': 21.0, 'net_pnl_paise': 700, 'side': 'BUY'},
        ]
        out = compare(replica_closed_trades=trades)
        self.assertEqual(out['counts'], {'matched': 2, 'live_only': 0, 'replica_only': 0})

    def test_skip_mismatch_still_reported(self):
        # Live bought a symbol the replica skipped -> skips do not match.
        bought_skipped = live_fills() + [{'strategy': 1, 'symbol': 'BBB-EQ', 'side': 'BUY', 'qty': 1,
                                          'price': 10.0, 'filled_at': '2026-11-11T09:15:00+05:30'}]
        out = compare(live_fills=bought_skipped)
        self.assertFalse(out['skips_match'])
        self.assertEqual(out['counts']['live_only'], 1)


if __name__ == '__main__':
    unittest.main()
