"""B4: candidate_compare tests.

Uses the committed hand-built fixture
tests/fixtures/adaptive/compare_case/ (trades_historical.json +
trades_forward.json). Expected numbers below are hand-computed from the
fixture trades, not recomputed by the module under test.
"""
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / 'fixtures' / 'adaptive' / 'compare_case'
LIVE = HERE.parents[2] / 'live'
sys.path.insert(0, str(LIVE))

import candidate_compare as cc

ROW_FIELDS = {'trades', 'win_rate', 'avg_net_return_pct', 'avg_r',
              'max_drawdown_pct', 'net_pnl_inr', 'avg_hold_days', 'exposure',
              'overlap_with_seven', 'sample_size'}


def _hash_tree(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(d).glob('*')) if p.is_file()}


class CompareMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hist, fwd = cc.load_offline_fixture(FIXTURE)
        cls.report = cc.build_report(hist, fwd, '2026-01-15')

    def test_metrics_exact_on_hand_built_trades(self):
        # Strategy 1 historical: +5000 (R 2.5, ret 5.0%) and -1200 (R -2.4, ret -4.8%).
        s1 = self.report['historical_replay']['strategies']['1']
        self.assertEqual(s1['trades'], 2)
        self.assertEqual(s1['win_rate'], 0.5)
        self.assertAlmostEqual(s1['avg_net_return_pct'], 0.1, places=4)
        self.assertAlmostEqual(s1['avg_r'], 0.05, places=4)
        self.assertAlmostEqual(s1['max_drawdown_pct'], 0.0024, places=4)
        self.assertEqual(s1['net_pnl_inr'], 38.0)
        self.assertEqual(s1['avg_hold_days'], 5.5)
        self.assertEqual(s1['exposure'], 0.55)
        # Strategy 2 historical: +1500 (R 0.9375) and -2200 (R -2.2).
        s2 = self.report['historical_replay']['strategies']['2']
        self.assertEqual(s2['trades'], 2)
        self.assertAlmostEqual(s2['avg_net_return_pct'], -1.2625, places=4)
        self.assertAlmostEqual(s2['avg_r'], -0.6313, places=4)
        self.assertAlmostEqual(s2['max_drawdown_pct'], 0.0044, places=4)
        self.assertEqual(s2['net_pnl_inr'], -7.0)
        # Strategy 3 historical: single +9000 trade, R 2.25, ret 4.5%.
        s3 = self.report['historical_replay']['strategies']['3']
        self.assertEqual((s3['trades'], s3['win_rate'], s3['avg_net_return_pct'],
                          s3['avg_r'], s3['max_drawdown_pct'], s3['net_pnl_inr']),
                         (1, 1.0, 4.5, 2.25, 0.0, 90.0))
        # Candidate 101 historical: +7500 (R 3.75) and -500 (R -0.8333).
        c101 = self.report['historical_replay']['strategies']['101']
        self.assertEqual(c101['trades'], 2)
        self.assertAlmostEqual(c101['avg_net_return_pct'], 2.9167, places=4)
        self.assertAlmostEqual(c101['avg_r'], 1.4583, places=4)
        self.assertAlmostEqual(c101['max_drawdown_pct'], 0.001, places=4)
        self.assertEqual(c101['net_pnl_inr'], 70.0)

    def test_insufficient_sample_label(self):
        # Every fixture row has <30 trades.
        for panel in ('historical_replay', 'forward_shadow'):
            for key, row in self.report[panel]['strategies'].items():
                self.assertEqual(row['sample_label'], 'insufficient_sample', (panel, key))
                self.assertEqual(row['sample_size'], row['trades'])
        # A synthetic 30-trade panel is sufficient.
        trades = [{'symbol': 'AAA-EQ', 'entry_date': '2026-01-02', 'exit_date': '2026-01-03',
                   'qty': 1, 'entry_price': 100.0, 'exit_price': 101.0,
                   'net_pnl_paise': 100, 'atr14_at_entry_paise': 10} for _ in range(30)]
        rows = cc.strategy_metrics({'1': trades}, cc.SEVEN, 20, 50_000_000)
        self.assertEqual(rows['1']['sample_label'], 'sufficient')
        self.assertEqual(rows['1']['trades'], 30)

    def test_seven_and_candidates_in_one_table(self):
        rows = self.report['strategies']
        for k in map(str, range(1, 8)):
            self.assertIn(k, rows)
        self.assertIn('101', rows)
        self.assertIn('102', rows)
        for key, row in rows.items():
            self.assertTrue(ROW_FIELDS <= set(row), key)
            self.assertEqual(set(row['overlap_with_seven']), {'count', 'pct'})
        md = cc.render_markdown(self.report)
        for k in ('1', '7', '101', '102'):
            self.assertIn(f'| {k} |', md)

    def test_overlap_calculation(self):
        hist = self.report['historical_replay']['strategies']
        # S1 and S2 share (AAA-EQ, 2026-01-02); S3 shares nothing.
        self.assertEqual(hist['1']['overlap_with_seven']['count'], 1)
        self.assertEqual(hist['2']['overlap_with_seven']['count'], 1)
        self.assertEqual(hist['3']['overlap_with_seven']['count'], 0)
        # C101's first trade shares S1/S2's entry; its second does not.
        self.assertEqual(hist['101']['overlap_with_seven']['count'], 1)
        # Merged C101 overlaps twice: historical entry + forward entry.
        self.assertEqual(self.report['strategies']['101']['overlap_with_seven']['count'], 2)
        self.assertEqual(self.report['strategies']['1']['overlap_with_seven']['count'], 1)

    def test_panels_are_separated_with_counts(self):
        self.assertEqual(self.report['historical_replay']['days'], 20)
        self.assertEqual(self.report['historical_replay']['trades'], 8)
        self.assertEqual(self.report['forward_shadow']['days'], 5)
        self.assertEqual(self.report['forward_shadow']['trades'], 2)
        self.assertEqual(self.report['mode'], 'shadow_simulated_observation_only')


class CompareReportTests(unittest.TestCase):
    def test_report_is_read_only_and_marked_shadow(self):
        before = _hash_tree(FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'cmp'
            cc.main(['--offline-fixture', str(FIXTURE), '--session', '2026-01-15',
                     '--out', str(out)])
            data = json.loads((out / 'compare.json').read_text())
            self.assertEqual(data['mode'], 'shadow_simulated_observation_only')
            self.assertIn('shadow, simulated, observation only, no promotion', data['statement'])
            self.assertIn('historical_replay', data)
            self.assertIn('forward_shadow', data)
            self.assertIn('strategies', data)
            md = (out / 'COMPARE.md').read_text()
            self.assertIn('shadow, simulated, observation only, no promotion', md)
        # Fixture untouched by the run.
        self.assertEqual(before, _hash_tree(FIXTURE))
        # No order / Turso / trading-namespace code in the module.
        src = Path(cc.__file__).read_text()
        for token in ('createClient', 'libsql', 'paper_order', 'place_order',
                      'submit_order', 'fyers', 'paper_v1', 'paper_v2', 'paper_demo_v1'):
            self.assertNotIn(token, src)

    def test_no_ranking_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'cmp'
            cc.main(['--offline-fixture', str(FIXTURE), '--session', '2026-01-15',
                     '--out', str(out)])
            blob = (out / 'COMPARE.md').read_text() + json.dumps(
                json.loads((out / 'compare.json').read_text()))
            for pat in (r'\bpromote\b', r'\bbetter than\b', r'\bwinner\b',
                        r'\bbest strategy\b', r'\branked\b', r'\branking\b',
                        r'\boutperform'):
                self.assertIsNone(re.search(pat, blob, re.I), pat)


if __name__ == '__main__':
    unittest.main()
