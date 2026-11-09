"""CI look-ahead truncation suite (E6, plan v2 P2 item 2). Run by the `adaptive-truncation` CI job and re-run locally by
adaptive_ci.run_local_truncation_suite() before any promotion.

- independent raw cuts: the truncated dataset is rebuilt from raw records, not a view of the full frame;
- late-announced corporate action: ex_date before t but announced after t must NOT be applied at t;
- future sentinel: absurd rows appended after the last session must not change any signal at t;
- mutation tests: a planted look-ahead evaluator (shift(-1)) and a centred rolling window MUST be caught.
"""
import sys, unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_engine as ae
import calibration as cal
from adaptive_fixtures import trading_days


def _dataset(n=260, seed=5):
    rng = np.random.default_rng(seed)
    days = trading_days(n)
    rows, idx = [], []
    for k, sym in enumerate(('AAA-EQ', 'BBB-EQ')):
        px = 500.0 + 100 * k
        for i, d in enumerate(days):
            r = 0.0008 + 0.015 * np.sin(i / 4.0 + k) + rng.normal(0, 0.006)
            o = px
            px = max(20.0, px * (1 + r))
            if sym == 'AAA-EQ' and d == days[-8]:
                px /= 2.0  # 2:1 split on days[-8], announced late (days[-3])
            rows.append({'symbol': sym, 'trade_date': d, 'open': round(o, 2), 'high': round(max(o, px) * 1.004, 2),
                         'low': round(min(o, px) * 0.996, 2), 'close': round(px, 2), 'volume': 250_000})
    for i, d in enumerate(days):
        idx.append({'symbol': 'NIFTY500', 'trade_date': d, 'open': 1000 + i, 'high': 1001 + i, 'low': 999 + i, 'close': 1000 + i, 'volume': 1})
    corp = [
        {'symbol': 'AAA-EQ', 'ex_date': days[-8], 'announcement_date': days[-3], 'kind': 'SPLIT', 'ratio_num': 2, 'ratio_den': 1},
        {'symbol': 'BBB-EQ', 'ex_date': days[-2], 'kind': 'BONUS', 'ratio_num': 3, 'ratio_den': 2},  # future vs most cuts
    ]
    return days, pd.DataFrame(rows), pd.DataFrame(idx), corp


def _with_future_sentinel(days, df, idx, n=5):
    last = pd.Timestamp(days[-1])
    extra_days = [d.date().isoformat() for d in pd.bdate_range(last + pd.Timedelta(days=1), periods=n)]
    sent = [{'symbol': s, 'trade_date': d, 'open': 1e7, 'high': 1e7, 'low': 1.0, 'close': 1e7, 'volume': 10 ** 12}
            for s in df['symbol'].unique() for d in extra_days]
    isent = [{'symbol': 'NIFTY500', 'trade_date': d, 'open': 1e7, 'high': 1e7, 'low': 1.0, 'close': 1e7, 'volume': 1} for d in extra_days]
    return pd.concat([df, pd.DataFrame(sent)], ignore_index=True), pd.concat([idx, pd.DataFrame(isent)], ignore_index=True)


class TruncationCITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days, cls.df, cls.idx, cls.corp = _dataset()
        cls.cuts = [cls.days[-12], cls.days[-8], cls.days[-6], cls.days[-3], cls.days[-2]]

    def test_independent_raw_cuts_with_future_sentinel_pass_for_all_strategies(self):
        df, idx = _with_future_sentinel(self.days, self.df, self.idx)
        res = cal.run_lookahead_truncation_test(df, idx, self.corp, self.cuts)
        self.assertTrue(res['passed'], res['mismatches'][:3])
        self.assertEqual(res['checks_run'], len(self.cuts) * 2 * (2 + len(ae.BASELINE_PARAMS)))

    def test_late_announced_corporate_action_is_not_applied_before_announcement(self):
        sym = self.df[self.df.symbol == 'AAA-EQ']
        ex, ann = self.days[-8], self.days[-3]
        before = ae.back_adjust_ohlc_pit(sym, self.corp, as_of_date=self.days[-6])  # after ex_date, before announcement
        raw = sym[sym.trade_date <= self.days[-6]].sort_values('trade_date')
        np.testing.assert_allclose(before['close'].to_numpy(float), raw['close'].to_numpy(float))
        after = ae.back_adjust_ohlc_pit(sym, self.corp, as_of_date=ann)
        pre_ex = after[after.trade_date < ex]['close'].to_numpy(float)
        raw_pre = sym[sym.trade_date < ex].sort_values('trade_date')['close'].to_numpy(float)
        np.testing.assert_allclose(pre_ex, raw_pre / 2.0)
        self.assertEqual(ae.corp_action_known_date(self.corp[0]), ann)

    def test_mutation_planted_lookahead_evaluator_is_caught(self):
        def peeking(strategy, df, params):
            out = ae.evaluate_parameterized_strategy(strategy, df, params)
            out['entry_condition'] = out['close'].shift(-1) > out['close']  # uses tomorrow's close
            return out
        res = cal.run_lookahead_truncation_test(self.df, self.idx, self.corp, self.cuts[:2], strategies=[1], signal_fn=peeking)
        self.assertFalse(res['passed'])
        self.assertTrue(any(m['stage'] == 'signal_or_back_adjust' for m in res['mismatches']))

    def test_mutation_centred_window_is_caught(self):
        def centred(strategy, df, params):
            out = ae.evaluate_parameterized_strategy(strategy, df, params)
            out['exit_condition'] = out['close'] < out['close'].rolling(5, center=True).mean()
            return out
        df, idx = _with_future_sentinel(self.days, self.df, self.idx)
        res = cal.run_lookahead_truncation_test(df, idx, self.corp, [self.days[-1]], strategies=[3], signal_fn=centred)
        self.assertFalse(res['passed'])

    def test_empty_run_is_not_a_pass(self):
        res = cal.run_lookahead_truncation_test(self.df, self.idx, self.corp, ['1990-01-01'])
        self.assertEqual(res['checks_run'], 0)
        self.assertFalse(res['passed'])


if __name__ == '__main__':
    unittest.main()
