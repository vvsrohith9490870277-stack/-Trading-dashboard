"""C1 protective stop for S1-S6 (Grok Decision 1, approved by VVS).

Shadow only: the stop is evaluated in engine._scan and recorded in
paper_shadow_signals; it never creates a live signal, fill or exit.
Tests pin: the stop is the tighter of entry - 2*ATR(14) and entry * 0.90;
ATR matches C107's Wilder convention; a gap-down open below the stop
fills at the open; live signals are identical with the stop on vs off;
indicator exits are unchanged; S7 is untouched.
"""
import json
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adaptive_candidates as ac
import engine as e
import protective_stops as ps
import test_engine as fixtures


def frame(close=100., spread=1., last=None):
    dates = pd.bdate_range(end='2026-11-09', periods=300).strftime('%Y-%m-%d')

    def f(c):
        return pd.DataFrame({'trade_date': dates, 'open': c, 'high': c + spread,
                             'low': c - spread, 'close': c, 'volume': 100.})

    s = f(close)
    s['symbol'] = 'RELIANCE-EQ'
    if last is not None:
        s.loc[s.index[-1], ['open', 'high', 'low', 'close']] = last
    return s


def bench():
    dates = pd.bdate_range(end='2026-11-09', periods=300).strftime('%Y-%m-%d')
    return pd.DataFrame({'trade_date': dates, 'open': 100., 'high': 101.,
                         'low': 99., 'close': 100., 'volume': 100.})


class ProtectiveStopRuleTests(unittest.TestCase):
    def test_stop_is_entry_minus_2atr_when_atr_small(self):
        # 2*ATR = 4% of entry, tighter than the 10% cap: stop at -4%.
        self.assertEqual(ps.stop_level(100, 2.0), 96.0)
        self.assertEqual(ps.stop_level(100.09, 2.0), 96.09)

    def test_stop_capped_at_10pct_when_atr_large(self):
        # 2*ATR = 12% of entry, wider than the cap: stop at -10%.
        self.assertEqual(ps.stop_level(100, 6.0), 90.0)
        self.assertEqual(ps.stop_level(100.09, 12.0), 90.08)

    def test_stop_boundary_when_2atr_equals_10pct(self):
        self.assertEqual(ps.stop_level(100, 5.0), 90.0)

    def test_stop_rejects_invalid_inputs(self):
        for bad in (None, 0, -1, float('nan'), float('inf')):
            self.assertIsNone(ps.stop_level(100, bad))
            self.assertIsNone(ps.stop_level(bad, 2.0))

    def test_atr_matches_c107_wilder_convention(self):
        # Varying frame: the ewm shortcut would differ, so compare the full
        # series against adaptive_candidates._atr_wilder element by element.
        rng = np.random.default_rng(7)
        closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, 300))
        f = pd.DataFrame({'trade_date': pd.bdate_range(end='2026-11-09', periods=300).strftime('%Y-%m-%d'),
                          'open': closes, 'high': closes * 1.005, 'low': closes * 0.995,
                          'close': closes, 'volume': 100.})
        mine = ps.atr_wilder(f['high'], f['low'], f['close'], 14)
        theirs = ac._atr_wilder(f['high'], f['low'], f['close'], 14)
        np.testing.assert_allclose(mine.to_numpy(float), theirs.to_numpy(float), rtol=0, atol=0, equal_nan=True)

    def test_atr14_at_entry_present_and_missing(self):
        self.assertAlmostEqual(ps.atr14_at_entry(frame(), '2026-11-06'), 2.0, places=6)
        self.assertAlmostEqual(ps.atr14_at_entry(frame(), '2026-11-09'), 2.0, places=6)
        self.assertIsNone(ps.atr14_at_entry(frame(), '2026-11-01'))  # no such bar
        self.assertIsNone(ps.atr14_at_entry(frame().iloc[:10], '2026-11-06'))  # too short
        self.assertIsNone(ps.atr14_at_entry(pd.DataFrame(), '2026-11-06'))

    def test_rule_set_file_matches_module(self):
        rules = json.loads((Path(ps.__file__).parent / 'strategy_rules_stops_v1.json').read_text())
        self.assertEqual(rules['version'], ps.VERSION)
        self.assertEqual(rules['version'], 'strategy_stops_s1_s6_2atr_10pct_cap_2026_11_26_v1')
        self.assertEqual(sorted(rules['applies_to_strategies']), [1, 2, 3, 4, 5, 6])
        self.assertEqual(rules['stop_atr_period'], 14)
        self.assertEqual(rules['stop_atr_mult'], 2.0)
        self.assertEqual(rules['stop_max_loss_fraction'], 0.10)
        self.assertTrue(rules['shadow'])
        self.assertEqual(ps.STOP_STRATEGIES, {1, 2, 3, 4, 5, 6})

    def test_gap_down_open_fills_at_open_not_stop(self):
        # Pure pricing: open below the stop fills at the open; a low touching
        # the stop fills at the stop level; no touch fills at the LTP.
        ref, path = e.execution_reference({'open': 90., 'low': 89., 'price': 90.}, stop=True, stop_level=96.09, side='SELL')
        self.assertEqual((ref, path), (90.0, 'FEED_OPEN'))
        ref, path = e.execution_reference({'open': 98., 'low': 95., 'price': 97.}, stop=True, stop_level=96.09, side='SELL')
        self.assertEqual((ref, path), (96.09, 'LTP_LEGACY'))
        ref, path = e.execution_reference({'open': 98., 'low': 97., 'price': 97.}, stop=True, stop_level=96.09, side='SELL')
        self.assertEqual((ref, path), (97.0, 'LTP_LEGACY'))


class ProtectiveStopScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PaperTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.PaperTests.tearDownClass.__func__(cls)

    setUp = fixtures.PaperTests.setUp
    seed_adv = fixtures.PaperTests.seed_adv
    signal = fixtures.PaperTests.signal
    quote = fixtures.PaperTests.quote

    def held_position(self, strategy, entry_at='2026-11-06T09:15:00+05:30', high_water=None):
        self.assertTrue(e.fill(self.db, self.signal(strategy=strategy), self.quote(), fixtures.T)['filled'])
        self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=? AND symbol=?',
                        (entry_at, e.NS, strategy, 'RELIANCE-EQ'))])
        if high_water is not None:
            self.db.batch([('UPDATE paper_positions SET high_water=? WHERE namespace=? AND strategy=? AND symbol=?',
                            (high_water, e.NS, strategy, 'RELIANCE-EQ'))])
        self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)', (e.NS, strategy, 'RELIANCE-EQ', '2026-11-06'))])

    def run_scan(self, s, indicator_exit=False):
        def indicator(i, g):
            return g.assign(entry_condition=False, exit_condition=indicator_exit)

        with patch.object(e, 'now', return_value=fixtures.T.replace(hour=16)), patch.object(e, 'universe', return_value=['RELIANCE-EQ']), patch.object(e, 'read_history', return_value=s), patch.object(e.pf, 'benchmark', return_value=bench()), patch.object(e.pf, 'evaluate', return_value={'status': 'PASS'}), patch.object(e, 'indicator', side_effect=indicator):
            return e.scan(self.db, None)

    def shadow_rows(self, strategy=None):
        q = "SELECT * FROM paper_shadow_signals WHERE namespace=? AND status='pending'"
        args = [e.NS]
        if strategy is not None:
            q += ' AND strategy=?'
            args.append(strategy)
        return self.db.query(q, tuple(args))

    def test_shadow_sell_queued_when_close_touches_stop(self):
        # Flat 100 frame: ATR(14) = 2, entry 100.09 -> stop 96.09. Last-bar
        # close 95 touches it, so a shadow SELL is queued for the next
        # session (2026-11-11) and NO live signal is created.
        self.held_position(4)
        r = self.run_scan(frame(last=[96., 96., 95., 95.]))
        self.assertEqual(r['shadow_signals_queued'], 1)
        self.assertEqual(r['signals_queued'], 0)
        rows = self.shadow_rows(4)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['symbol'], 'RELIANCE-EQ')
        self.assertEqual(row['side'], 'SELL')
        self.assertEqual(row['bar_date'], '2026-11-09')
        self.assertEqual(row['stop_level'], 96.09)
        self.assertEqual(row['entry_price'], 100.09)
        self.assertEqual(row['entry_at'], '2026-11-06T09:15:00+05:30')
        self.assertEqual(row['eligible_date'], '2026-11-11')
        self.assertTrue(row['reason'].startswith('protective_stop:'))
        live = self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND strategy=4 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'", (e.NS,))
        self.assertEqual(live, [])
        self.assertEqual(len(self.db.positions()), 1)
        ev = self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='shadow_stop_queued'", (e.NS,))
        self.assertEqual(len(ev), 1)
        self.assertAlmostEqual(json.loads(ev[0]['data_json'])['threshold'], 96.09)

    def test_no_shadow_sell_when_close_above_stop(self):
        self.held_position(4)
        r = self.run_scan(frame(last=[99., 99., 98., 98.]))
        self.assertEqual(r['shadow_signals_queued'], 0)
        self.assertEqual(r['signals_queued'], 0)
        self.assertEqual(self.shadow_rows(), [])
        ev = self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='shadow_stop_queued'", (e.NS,))
        self.assertEqual(ev, [])

    def test_stop_capped_at_10pct_in_shadow(self):
        # Wide bars: TR 12, ATR(14) = 12, so 2*ATR = 24 > 10% of entry:
        # the stop is capped at entry * 0.90 = 90.08, and a close at 89
        # touches it.
        self.held_position(4)
        r = self.run_scan(frame(spread=6., last=[90., 90., 89., 89.]))
        self.assertEqual(r['shadow_signals_queued'], 1)
        rows = self.shadow_rows(4)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stop_level'], 90.08)

    def test_indicator_exit_unchanged_and_no_shadow_sell(self):
        # The indicator fires on the last bar: the live SELL is queued and
        # the stop is NOT also queued (live exit takes precedence).
        self.held_position(4)
        r = self.run_scan(frame(last=[96., 96., 95., 95.]), indicator_exit=True)
        self.assertEqual(r['shadow_signals_queued'], 0)
        self.assertEqual(r['signals_queued'], 1)
        self.assertEqual(self.shadow_rows(), [])
        live = self.db.query("SELECT side,reason FROM paper_signals WHERE namespace=? AND strategy=4 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'", (e.NS,))
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0]['reason'].startswith('daily_exit'))

    def test_s7_untouched(self):
        # S7 already has its 10% trail: a close at 95 is above the 90.0
        # trail, and the C1 stop (96.09) must NOT be applied to S7.
        self.held_position(7, high_water=100.0)
        r = self.run_scan(frame(last=[96., 96., 95., 95.]))
        self.assertEqual(r['shadow_signals_queued'], 0)
        self.assertEqual(self.shadow_rows(), [])
        live = self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND strategy=7 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'", (e.NS,))
        self.assertEqual(live, [])

    def test_no_duplicate_shadow_sell_on_second_scan(self):
        self.held_position(4)
        self.run_scan(frame(last=[96., 96., 95., 95.]))
        r = self.run_scan(frame(last=[96., 96., 95., 95.]))
        self.assertEqual(r['shadow_signals_queued'], 0)
        self.assertEqual(len(self.shadow_rows(4)), 1)

    def test_live_signals_identical_with_stop_on_vs_off(self):
        # Same setup, same scan, with and without the C1 stop: the live
        # paper_signals rows must be byte-identical. (The stop is off when
        # the rule set excludes the strategy.)
        self.held_position(4)
        r = self.run_scan(frame(last=[96., 96., 95., 95.]))
        self.assertEqual(r['signals_queued'], 0)
        with_stop = self.db.query("SELECT strategy,symbol,side,status,reason,eligible_date FROM paper_signals WHERE namespace=? ORDER BY strategy,symbol,side", (e.NS,))
        with patch.object(ps, 'STOP_STRATEGIES', frozenset()):
            r2 = self.run_scan(frame(last=[96., 96., 95., 95.]))
        self.assertEqual(r2['signals_queued'], 0)
        without_stop = self.db.query("SELECT strategy,symbol,side,status,reason,eligible_date FROM paper_signals WHERE namespace=? ORDER BY strategy,symbol,side", (e.NS,))
        self.assertEqual(with_stop, without_stop)
        self.assertEqual(len(self.shadow_rows(4)), 1)


class ProtectiveStopFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PaperTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.PaperTests.tearDownClass.__func__(cls)

    setUp = fixtures.PaperTests.setUp
    seed_adv = fixtures.PaperTests.seed_adv
    signal = fixtures.PaperTests.signal
    quote = fixtures.PaperTests.quote

    def queued_shadow(self):
        self.assertTrue(e.fill(self.db, self.signal(strategy=4), self.quote(), fixtures.T)['filled'])
        self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=4 AND symbol=?',
                        ('2026-11-06T09:15:00+05:30', e.NS, 'RELIANCE-EQ'))])
        self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)', (e.NS, 4, 'RELIANCE-EQ', '2026-11-06'))])
        s = frame(last=[96., 96., 95., 95.])

        def indicator(i, g):
            return g.assign(entry_condition=False, exit_condition=False)

        with patch.object(e, 'now', return_value=fixtures.T.replace(hour=16)), patch.object(e, 'universe', return_value=['RELIANCE-EQ']), patch.object(e, 'read_history', return_value=s), patch.object(e.pf, 'benchmark', return_value=bench()), patch.object(e.pf, 'evaluate', return_value={'status': 'PASS'}), patch.object(e, 'indicator', side_effect=indicator):
            r = e.scan(self.db, None)
        self.assertEqual(r['shadow_signals_queued'], 1)
        rows = self.db.query("SELECT * FROM paper_shadow_signals WHERE namespace=? AND status='pending'", (e.NS,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_gap_down_open_fills_at_open_and_leaves_live_position_open(self):
        row = self.queued_shadow()
        t2 = fixtures.T.replace(day=11, hour=10)
        q = {'symbol': 'RELIANCE-EQ', 'price': 90., 'open': 90., 'low': 89., 'quote_at': t2.isoformat()}
        r = e.fill_shadow_stop(self.db, row, q, t2)
        self.assertTrue(r['filled'])
        self.assertEqual(r['price'], 90.0)
        self.assertEqual(r['execution_path'], 'FEED_OPEN')
        after = self.db.query("SELECT status FROM paper_shadow_signals WHERE namespace=? AND id=?", (e.NS, row['id']))
        self.assertEqual(after[0]['status'], 'filled')
        # The live position is still open; the shadow exit consumed the
        # shadow mirror only.
        pos = self.db.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]['qty'], 500)
        shadow_pos = self.db.query("SELECT qty FROM paper_shadow_positions WHERE namespace=? AND strategy=4 AND symbol='RELIANCE-EQ'", (e.NS,))
        self.assertEqual(shadow_pos[0]['qty'], 0)
        closed = self.db.query("SELECT net_pnl_paise FROM paper_closed_trades WHERE namespace=? AND book='shadow' AND strategy=4", (e.NS,))
        self.assertEqual(len(closed), 1)
        self.assertLess(closed[0]['net_pnl_paise'], 0)
        live_fills = self.db.query("SELECT price FROM paper_fills WHERE namespace=? AND signal_id=?", (e.NS, row['id']))
        self.assertEqual(live_fills, [])
        ev = self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='shadow_stop_filled'", (e.NS,))
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0]['data_json'])['price'], 90.0)

    def test_low_touch_fills_at_stop_level(self):
        row = self.queued_shadow()
        t2 = fixtures.T.replace(day=11, hour=10)
        q = {'symbol': 'RELIANCE-EQ', 'price': 97., 'open': 98., 'low': 95., 'quote_at': t2.isoformat()}
        r = e.fill_shadow_stop(self.db, row, q, t2)
        self.assertTrue(r['filled'])
        self.assertEqual(r['price'], 96.09)

    def test_fill_is_noop_before_eligible_date(self):
        row = self.queued_shadow()
        t2 = fixtures.T.replace(day=10, hour=10)
        q = {'symbol': 'RELIANCE-EQ', 'price': 90., 'open': 90., 'low': 89., 'quote_at': t2.isoformat()}
        r = e.fill_shadow_stop(self.db, row, q, t2)
        self.assertFalse(r['filled'])
        self.assertEqual(r['reason'], 'not_yet_eligible')
        after = self.db.query("SELECT status FROM paper_shadow_signals WHERE namespace=? AND id=?", (e.NS, row['id']))
        self.assertEqual(after[0]['status'], 'pending')

    def test_monitor_fills_shadow_and_leaves_live_position_open(self):
        row = self.queued_shadow()
        t2 = fixtures.T.replace(day=11, hour=10)

        class Feed:
            def quotes(self, syms):
                return {'RELIANCE-EQ': {'symbol': 'RELIANCE-EQ', 'price': 90., 'open': 90., 'low': 89.,
                                         'quote_at': t2.isoformat()}}

        with patch.object(e, 'now', return_value=t2):
            r = e.monitor(self.db, Feed())
        self.assertEqual(r['shadow_fills'], 1)
        self.assertEqual(r['fills'], 0)
        after = self.db.query("SELECT status FROM paper_shadow_signals WHERE namespace=? AND id=?", (e.NS, row['id']))
        self.assertEqual(after[0]['status'], 'filled')
        pos = self.db.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]['qty'], 500)


if __name__ == '__main__':
    unittest.main()
