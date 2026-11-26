"""Tests for the _SimContext fast path (adaptive sim optimisation).

The context precomputes the frame scans, lagged ADV, PIT lookups and
indicator frames that simulate_track_window used to recompute per
(symbol, day). Each test compares the precomputed value against the
original per-day computation, so the optimisation cannot silently change
results. The true old-vs-new proof is the before/after dump comparison
on the real snapshot (harness under ~/workspace/adaptive-opt); these
tests pin the equivalences on synthetic fixtures.
"""
import unittest
from pathlib import Path

import numpy as np

import adaptive_engine as ae
from adaptive_fixtures import fresh_db, make_market, seed_pit

ROOT = Path(__file__).resolve().parent.parent


class SimContextTests(unittest.TestCase):
    def _market(self, n_days=120):
        return make_market(n_days=n_days)

    def test_lagged_adv_matches_lagged_adv_paise(self):
        days, df, idx = self._market()
        ctx = ae._SimContext(df, idx)
        rng = np.random.default_rng(3)
        syms = sorted(df['symbol'].astype(str).unique())
        for _ in range(40):
            sym = str(rng.choice(syms))
            day = str(rng.choice(days))
            hist = df[df['symbol'].astype(str) == sym]
            self.assertEqual(ctx.lagged_adv(sym, day), ae.lagged_adv_paise(hist, day),
                             f'ADV mismatch for {sym} {day}')
        # Before the 20-session lookback is satisfied -> None; after -> a value.
        self.assertIsNone(ctx.lagged_adv('AAA-EQ', days[5]))
        self.assertIsNotNone(ctx.lagged_adv('AAA-EQ', days[60]))
        # Cached: repeat calls agree.
        self.assertEqual(ctx.lagged_adv('AAA-EQ', days[60]), ctx.lagged_adv('AAA-EQ', days[60]))

    def test_bar_map_matches_boolean_mask_construction(self):
        days, df, idx = self._market()
        ctx = ae._SimContext(df, idx)
        for day in days[::17]:
            expected = {str(r['symbol']): r for _, r in df[df['trade_date'].astype(str) == day].iterrows()}
            got = ctx.bar_map_for_day(day)
            self.assertEqual(set(got), set(expected))
            for sym, row in expected.items():
                self.assertEqual(float(got[sym]['open']), float(row['open']))
                self.assertEqual(float(got[sym]['low']), float(row['low']))
                self.assertEqual(float(got[sym]['close']), float(row['close']))
        self.assertEqual(ctx.bar_map_for_day('1900-01-01'), {})

    def test_signal_precompute_matches_per_day(self):
        # For every (symbol, day), the precomputed row equals the last row
        # of the old per-day computation (truncated history -> back-adjust
        # -> evaluate_parameterized_strategy -> iloc[-1]).
        days, df, idx = self._market()
        ctx = ae._SimContext(df, idx)
        max_day = days[-1]
        for s in (1, 3):
            params = ae.validate_parameter_only_mutation(s, {})
            for sym in sorted(df['symbol'].astype(str).unique()):
                arrays = ctx.signal_arrays(s, params, None, sym, max_day)
                self.assertIsNotNone(arrays, f'no precompute for {s} {sym}')
                entry_arr, exit_arr, _qty_arr, _dates = arrays
                for day in days[::7]:
                    sym_hist = df[(df['symbol'] == sym) & (df['trade_date'].astype(str) <= day)]
                    if sym_hist.empty or str(sym_hist.iloc[-1]['trade_date']) != day:
                        continue
                    adj = ae.back_adjust_ohlc_pit(sym_hist, [], as_of_date=day)
                    last = ae.evaluate_parameterized_strategy(s, adj, params).iloc[-1]
                    pos = ctx.day_row_index(sym, day)
                    self.assertEqual(bool(entry_arr[pos]), bool(last.get('entry_condition', False)),
                                     f'entry mismatch s={s} {sym} {day}')
                    self.assertEqual(bool(exit_arr[pos]), bool(last.get('exit_condition', False)),
                                     f'exit mismatch s={s} {sym} {day}')

    def test_scaled_corp_action_forces_per_day_fallback(self):
        days, df, idx = self._market()
        split = {'symbol': 'AAA-EQ', 'kind': 'SPLIT', 'ex_date': days[60],
                 'ratio_num': 2, 'ratio_den': 1, 'announcement_date': days[55]}
        ctx = ae._SimContext(df, idx, [split])
        self.assertTrue(ctx.has_applicable_scaled_action('AAA-EQ', days[-1]))
        self.assertFalse(ctx.has_applicable_scaled_action('BBB-EQ', days[-1]))
        params = ae.validate_parameter_only_mutation(3, {})
        self.assertIsNone(ctx.signal_arrays(3, params, None, 'AAA-EQ', days[-1]))
        self.assertIsNotNone(ctx.signal_arrays(3, params, None, 'BBB-EQ', days[-1]))
        # A non-scaled action (dividend) never forces the fallback.
        div = {'symbol': 'AAA-EQ', 'kind': 'CASH_DIVIDEND', 'ex_date': days[60],
               'ratio_num': 1, 'ratio_den': 1, 'announcement_date': days[55]}
        ctx_div = ae._SimContext(df, idx, [div])
        self.assertFalse(ctx_div.has_applicable_scaled_action('AAA-EQ', days[-1]))
        self.assertIsNotNone(ctx_div.signal_arrays(3, params, None, 'AAA-EQ', days[-1]))
        # The sim still runs with the split in play (fallback path) and is
        # deterministic: a fresh context gives the identical result.
        db, ns = fresh_db(ROOT), 'eq'
        seed_pit(db, ns, days, sorted(df.symbol.unique()))
        ws, ev = days[-105:-45], days[-45:]
        r1 = ae.simulate_track_window(db, ns, 3, {}, df, idx, ws, ev,
                                      corp_actions=[split], sim_context=ctx)
        r2 = ae.simulate_track_window(db, ns, 3, {}, df, idx, ws, ev, corp_actions=[split])
        self.assertEqual(r1['closed_trades'], r2['closed_trades'])
        self.assertEqual(r1['daily_records'], r2['daily_records'])

    def test_context_built_for_a_different_frame_is_refused(self):
        days, df, idx = self._market()
        _d2, df2, idx2 = self._market()
        ctx = ae._SimContext(df, idx)
        db, ns = fresh_db(ROOT), 'eq'
        seed_pit(db, ns, days, sorted(df.symbol.unique()))
        with self.assertRaises(ValueError):
            ae.simulate_track_window(db, ns, 3, {}, df2, idx2, days[-60:-30], days[-30:],
                                     sim_context=ctx)

    def test_shared_context_and_fresh_context_give_identical_sims(self):
        db, ns = fresh_db(ROOT), 'eq'
        days, df, idx = make_market(n_days=330)
        seed_pit(db, ns, days, sorted(df.symbol.unique()))
        ws, ev = days[-105:-45], days[-45:]
        r_fresh = ae.simulate_track_window(db, ns, 3, {}, df, idx, ws, ev)
        ctx = ae._SimContext(df, idx)
        r_shared = ae.simulate_track_window(db, ns, 3, {}, df, idx, ws, ev, sim_context=ctx)
        self.assertEqual(r_shared['closed_trades'], r_fresh['closed_trades'])
        self.assertEqual(r_shared['daily_records'], r_fresh['daily_records'])
        self.assertEqual(r_shared['entry_skips'], r_fresh['entry_skips'])
        self.assertEqual(r_shared['open_positions'], r_fresh['open_positions'])
        self.assertTrue(any(r['open_positions'] > 0 for r in r_shared['daily_records']))


if __name__ == '__main__':
    unittest.main()
