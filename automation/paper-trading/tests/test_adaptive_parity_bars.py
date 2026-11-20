"""B2 (i): offline bar-level parity — adaptive Track A replica vs real Track A.

Synthetic bars only (symbol SYN-EQ, clearly marked synthetic, never presented
as real data): 400 sessions, rise / sharp dip / recovery / shallow pullback /
rise, so every frozen strategy 1-7 produces at least one entry and one exit
signal edge.

Real Track A signal path: engine.indicator(strategy, df) — engine.py:466-481
(strategy 1 inline Donchian at :478-479; strategies 2-7 delegate to
strategy_indicators.strategy_N at :480-481, the frozen file).
Replica path: adaptive_engine.evaluate_parameterized_strategy(strategy, df,
BASELINE_PARAMS[strategy]) — baseline params delegate to the same frozen
functions (adaptive_engine.py:235-243).

For each strategy 1-7: identical entry/exit condition series, identical
rising-edge signal dates, identical fill day (next session after the signal
close) and side. Any divergence is a finding — this test does NOT fix Track A.
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import adaptive_engine as ae
import engine as track_a
from adaptive_fixtures import fresh_db, seed_pit, trading_days


def synthetic_bars(n=400, seed=11):
    """Synthetic 400-session bars (marked synthetic). Regimes: steady rise,
    sharp dip, recovery, shallow pullback, rise."""
    rng = np.random.default_rng(seed)
    days = trading_days(n)
    closes = []
    px = 1000.0
    for i in range(n):
        if i < 200:
            r = 0.008
        elif i < 230:
            r = -0.025
        elif i < 300:
            r = 0.012
        elif i < 315:
            r = -0.004
        else:
            r = 0.010
        r += rng.normal(0, 0.002)
        px = px * (1 + r)
        closes.append(round(px, 2))
    rows = []
    for i, d in enumerate(days):
        c = closes[i]
        o = closes[i - 1] if i else c
        rows.append({'symbol': 'SYN-EQ', 'trade_date': d, 'open': o,
                     'high': round(max(o, c) * 1.005, 2), 'low': round(min(o, c) * 0.995, 2),
                     'close': c, 'volume': 300000, 'synthetic': True})
    return pd.DataFrame(rows)


def _bool_series(frame, col):
    return frame[col].fillna(False).astype(bool).to_numpy()


def _rising_edges(frame, col):
    cond = _bool_series(frame, col)
    return [i for i in range(1, len(cond)) if cond[i] and not cond[i - 1]]


class BarParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = synthetic_bars()
        assert cls.df['synthetic'].all()

    def test_real_and_replica_signals_identical_for_all_seven(self):
        for s in range(1, 8):
            with self.subTest(strategy=s):
                real = track_a.indicator(s, self.df)
                replica = ae.evaluate_parameterized_strategy(s, self.df, dict(ae.BASELINE_PARAMS[s]))
                for col in ('entry_condition', 'exit_condition'):
                    self.assertTrue((_bool_series(real, col) == _bool_series(replica, col)).all(),
                                    f'strategy {s}: {col} differs between real Track A and replica')
                real_en, rep_en = _rising_edges(real, 'entry_condition'), _rising_edges(replica, 'entry_condition')
                real_ex, rep_ex = _rising_edges(real, 'exit_condition'), _rising_edges(replica, 'exit_condition')
                self.assertEqual(real_en, rep_en, f'strategy {s}: entry signal dates differ')
                self.assertEqual(real_ex, rep_ex, f'strategy {s}: exit signal dates differ')
                self.assertGreater(len(real_en), 0, f'strategy {s}: no entry signal on fixture')
                self.assertGreater(len(real_ex), 0, f'strategy {s}: no exit signal on fixture')
                days = self.df['trade_date'].astype(str).tolist()
                real_fills = [(days[i + 1], 'BUY') for i in real_en if i + 1 < len(days)]
                rep_fills = [(days[i + 1], 'BUY') for i in rep_en if i + 1 < len(days)]
                self.assertEqual(real_fills, rep_fills, f'strategy {s}: entry fill days/sides differ')
                real_exits = [(days[i + 1], 'SELL') for i in real_ex if i + 1 < len(days)]
                rep_exits = [(days[i + 1], 'SELL') for i in rep_ex if i + 1 < len(days)]
                self.assertEqual(real_exits, rep_exits, f'strategy {s}: exit fill days/sides differ')

    def test_simulated_fills_follow_signal_dates_plus_one_session(self):
        # The replica simulator fills a close-of-day-t signal at the open of
        # t+1, so every closed trade's entry/exit date must be a signal fill day.
        days = self.df['trade_date'].astype(str).tolist()
        idx = pd.DataFrame([{'symbol': 'NIFTY500', 'trade_date': d, 'open': 1000 + i,
                             'high': 1001 + i, 'low': 999 + i, 'close': 1000 + i, 'volume': 1}
                            for i, d in enumerate(days)])
        for s, ws in ((1, 110), (3, 60)):
            with self.subTest(strategy=s):
                real = track_a.indicator(s, self.df)
                entry_fills = {days[i + 1] for i in _rising_edges(real, 'entry_condition') if i + 1 < len(days)}
                exit_fills = {days[i + 1] for i in _rising_edges(real, 'exit_condition') if i + 1 < len(days)}
                db = fresh_db(ROOT)
                seed_pit(db, 'parity', days, ['SYN-EQ'], series='EQ')
                res = ae.simulate_track_window(db, 'parity', s, dict(ae.BASELINE_PARAMS[s]),
                                               self.df, idx, days[:ws], days[ws:])
                self.assertGreater(len(res['closed_trades']), 0, f'strategy {s}: no closed trades')
                for t in res['closed_trades']:
                    self.assertIn(t['entry_date'], entry_fills,
                                  f"strategy {s}: entry {t['entry_date']} is not a signal fill day")
                    self.assertIn(t['exit_date'], exit_fills,
                                  f"strategy {s}: exit {t['exit_date']} is not a signal fill day")


if __name__ == '__main__':
    unittest.main()
