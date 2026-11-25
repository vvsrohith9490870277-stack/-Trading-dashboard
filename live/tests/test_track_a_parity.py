"""B2: track_a_parity.compare() behaviour under the P2+E1 informational basis.

The trade-level comparison is 'compared' exactly when an adaptive_parity row
exists for the session — including an informational P2+E1 row (parity_ok=0,
verdict informational_no_pass_fail), because the row IS the evidence that the
comparison ran. The sleeve-equity copy check (exact EOD equity/positions
equality) is a separate check and is unchanged by B2.

TradeParityLabelTests moved here from live/tests/test_parity_record.py
(it tests track_a_parity.compare, not parity_record).
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
from track_a_parity import compare as parity_compare


class TrackAParityTests(unittest.TestCase):
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

    def test_informational_parity_row_counts_as_compared(self):
        # A P2+E1 row carries no pass/fail verdict (parity_ok=0), but the
        # comparison still ran, so trade_parity is 'compared', not
        # 'not_evaluable'. The row count is what compare() reads.
        live = {'sleeves': {}, 'open_positions_now': {'total': 0, 'by_strategy': {}}}
        out = parity_compare(live, self._adaptive(parity_rows=1))
        self.assertEqual(out['trade_parity'], 'compared')
        self.assertFalse(any('No adaptive_parity row' in r for r in out['reasons']))

    def test_sleeve_equity_copy_mismatch_still_reported(self):
        # Unchanged B2 behaviour: the EOD sleeve copy must match exactly.
        live = {'sleeves': {1: {'equity_paise': 1000000, 'cash_paise': 500000,
                              'market_value_paise': 500000, 'open_positions': 1}},
                'open_positions_now': {'total': 1, 'by_strategy': {1: 1}}}
        adaptive = self._adaptive(
            copied_equity_rows=[{'strategy': 1, 'equity_paise': 999999, 'cash_paise': 500000,
                                 'market_value_paise': 500000, 'open_positions': 1,
                                 'trade_date': '2026-11-11'}],
            parity_rows=1)
        out = parity_compare(live, adaptive)
        self.assertFalse(out['sleeve_equity_all_match'])
        self.assertTrue(any('S1' in r for r in out['reasons']))
        self.assertEqual(out['trade_parity'], 'compared')

    def test_blocked_simulation_is_not_evaluable(self):
        live = {'sleeves': {}, 'open_positions_now': {'total': 0, 'by_strategy': {}}}
        out = parity_compare(live, self._adaptive(
            daily_report={'simulation_status': 'BLOCKED_PIT_SERIES_ARCHIVE_P1'}, parity_rows=0))
        self.assertEqual(out['trade_parity'], 'not_evaluable')


if __name__ == '__main__':
    unittest.main()
