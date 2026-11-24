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
              'max_drawdown_pct', 'net_pnl_inr', 'net_pnl_paise', 'avg_hold_days', 'exposure',
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
        # 1R = qty x 2 x ATR(14) at entry (Grok Q3 answer, plan §9): every
        # R-multiple below is half the 1x-ATR value, e.g. S1 +5000/(10x200)
        # = 2.5 -> 1.25 and -1200/(5x100) = -2.4 -> -1.2, avg 0.025.
        # Strategy 1 historical: +5000 (R 1.25, ret 5.0%) and -1200 (R -1.2, ret -4.8%).
        s1 = self.report['historical_replay']['strategies']['1']
        self.assertEqual(s1['trades'], 2)
        self.assertEqual(s1['win_rate'], 0.5)
        self.assertAlmostEqual(s1['avg_net_return_pct'], 0.1, places=4)
        self.assertAlmostEqual(s1['avg_r'], 0.025, places=4)
        self.assertAlmostEqual(s1['max_drawdown_pct'], 0.0024, places=4)
        self.assertEqual(s1['net_pnl_inr'], 38.0)
        self.assertEqual(s1['net_pnl_paise'], 3800)
        self.assertEqual(s1['avg_hold_days'], 5.5)
        self.assertEqual(s1['exposure'], 0.55)
        # Strategy 2 historical: +1500 (R 0.4688) and -2200 (R -1.1).
        s2 = self.report['historical_replay']['strategies']['2']
        self.assertEqual(s2['trades'], 2)
        self.assertAlmostEqual(s2['avg_net_return_pct'], -1.2625, places=4)
        self.assertAlmostEqual(s2['avg_r'], -0.3156, places=4)
        self.assertAlmostEqual(s2['max_drawdown_pct'], 0.0044, places=4)
        self.assertEqual(s2['net_pnl_inr'], -7.0)
        self.assertEqual(s2['net_pnl_paise'], -700)
        # Strategy 3 historical: single +9000 trade, R 1.125, ret 4.5%.
        s3 = self.report['historical_replay']['strategies']['3']
        self.assertEqual((s3['trades'], s3['win_rate'], s3['avg_net_return_pct'],
                          s3['avg_r'], s3['max_drawdown_pct'], s3['net_pnl_inr'],
                          s3['net_pnl_paise']),
                         (1, 1.0, 4.5, 1.125, 0.0, 90.0, 9000))
        # Candidate 101 historical: +7500 (R 1.875) and -500 (R -0.4167).
        c101 = self.report['historical_replay']['strategies']['101']
        self.assertEqual(c101['trades'], 2)
        self.assertAlmostEqual(c101['avg_net_return_pct'], 2.9167, places=4)
        self.assertAlmostEqual(c101['avg_r'], 0.7292, places=4)
        self.assertAlmostEqual(c101['max_drawdown_pct'], 0.001, places=4)
        self.assertEqual(c101['net_pnl_inr'], 70.0)
        self.assertEqual(c101['net_pnl_paise'], 7000)
        # Candidate 102 historical: single +400 trade, R 1.0
        # (400/(10 x 2 x 20)).
        c102 = self.report['historical_replay']['strategies']['102']
        self.assertEqual((c102['trades'], c102['avg_r'], c102['net_pnl_inr'],
                          c102['net_pnl_paise']), (1, 1.0, 4.0, 400))
        # Forward shadow panel: S1 +900 (R 0.225), C101 -1100 (R -0.275).
        fwd = self.report['forward_shadow']['strategies']
        self.assertAlmostEqual(fwd['1']['avg_r'], 0.225, places=4)
        self.assertAlmostEqual(fwd['101']['avg_r'], -0.275, places=4)
        # Merged (both panels): S1 avg R (1.25 - 1.2 + 0.225)/3 = 0.0917,
        # net 4700 paise; C101 (1.875 - 0.4167 - 0.275)/3 = 0.3944, net 5900.
        merged = self.report['strategies']
        self.assertAlmostEqual(merged['1']['avg_r'], 0.0917, places=4)
        self.assertEqual(merged['1']['net_pnl_paise'], 4700)
        self.assertAlmostEqual(merged['101']['avg_r'], 0.3944, places=4)
        self.assertEqual(merged['101']['net_pnl_paise'], 5900)

    def test_c106_key_flows_through_compare(self):
        # Candidate 106 needs no compare-code change: strategy_metrics and
        # build_report iterate whatever strategy keys they are given. A 106
        # row flows through with the same fields, R and overlap as 101/102.
        trades = [{'symbol': 'AAA-EQ', 'entry_date': '2026-01-02', 'exit_date': '2026-01-03',
                   'qty': 10, 'entry_price': 100.0, 'exit_price': 102.0,
                   'net_pnl_paise': 2000, 'atr14_at_entry_paise': 200},
                  {'symbol': 'BBB-EQ', 'entry_date': '2026-01-04', 'exit_date': '2026-01-05',
                   'qty': 10, 'entry_price': 100.0, 'exit_price': 99.0,
                   'net_pnl_paise': -1000, 'atr14_at_entry_paise': 200}]
        rows = cc.strategy_metrics({'1': [], '106': trades}, cc.SEVEN, 20, 50_000_000)
        r106 = rows['106']
        self.assertEqual(r106['trades'], 2)
        self.assertEqual(r106['win_rate'], 0.5)
        self.assertEqual(r106['net_pnl_paise'], 1000)
        self.assertEqual(r106['sample_label'], 'insufficient_sample')
        self.assertTrue(ROW_FIELDS <= set(r106))
        report = cc.build_report(
            {'window_days': 20, 'starting_capital_paise': 50_000_000, 'strategies': {'106': trades}},
            {'window_days': 5, 'starting_capital_paise': 50_000_000, 'strategies': {}},
            '2026-01-15')
        self.assertIn('106', report['strategies'])
        self.assertIn('| 106 |', cc.render_markdown(report))

    def test_c107_key_flows_through_compare(self):
        # Candidate 107 needs no compare-code change: strategy_metrics and
        # build_report iterate whatever strategy keys they are given. A 107
        # row flows through with the same fields, R and overlap as 101/102.
        # The losing trade (-1R) is a stop exit: entry 100, ATR(14) at entry
        # 2, stop 96, exit 96.
        trades = [{'symbol': 'AAA-EQ', 'entry_date': '2026-01-02', 'exit_date': '2026-01-03',
                   'qty': 10, 'entry_price': 100.0, 'exit_price': 102.0,
                   'net_pnl_paise': 2000, 'atr14_at_entry_paise': 200},
                  {'symbol': 'BBB-EQ', 'entry_date': '2026-01-04', 'exit_date': '2026-01-05',
                   'qty': 10, 'entry_price': 100.0, 'exit_price': 96.0,
                   'net_pnl_paise': -1000, 'atr14_at_entry_paise': 200}]
        rows = cc.strategy_metrics({'1': [], '107': trades}, cc.SEVEN, 20, 50_000_000)
        r107 = rows['107']
        self.assertEqual(r107['trades'], 2)
        self.assertEqual(r107['win_rate'], 0.5)
        self.assertEqual(r107['net_pnl_paise'], 1000)
        self.assertEqual(r107['sample_label'], 'insufficient_sample')
        self.assertTrue(ROW_FIELDS <= set(r107))
        report = cc.build_report(
            {'window_days': 20, 'starting_capital_paise': 50_000_000, 'strategies': {'107': trades}},
            {'window_days': 5, 'starting_capital_paise': 50_000_000, 'strategies': {}},
            '2026-01-15')
        self.assertIn('107', report['strategies'])
        self.assertIn('| 107 |', cc.render_markdown(report))
    def test_c109_key_flows_through_compare(self):
        # Candidate 109 needs no compare-code change: strategy_metrics and
        # build_report iterate whatever strategy keys they are given. A 109
        # row flows through with the same fields, R and overlap as 101/102.
        # The trades are ATR-sized: qty 250 at ATR(14) Rs 10, so 1R =
        # 250 x 2 x 1000 paise = Rs 5,000 (the risk budget).
        trades = [{'symbol': 'AAA-EQ', 'entry_date': '2026-01-02', 'exit_date': '2026-01-03',
                   'qty': 250, 'entry_price': 108.0, 'exit_price': 110.0,
                   'net_pnl_paise': 250000, 'atr14_at_entry_paise': 1000},
                  {'symbol': 'BBB-EQ', 'entry_date': '2026-01-04', 'exit_date': '2026-01-05',
                   'qty': 250, 'entry_price': 108.0, 'exit_price': 107.0,
                   'net_pnl_paise': -125000, 'atr14_at_entry_paise': 1000}]
        rows = cc.strategy_metrics({'1': [], '109': trades}, cc.SEVEN, 20, 50_000_000)
        r109 = rows['109']
        self.assertEqual(r109['trades'], 2)
        self.assertEqual(r109['win_rate'], 0.5)
        self.assertEqual(r109['net_pnl_paise'], 125000)
        self.assertAlmostEqual(r109['avg_r'], 0.125, places=4)
        self.assertAlmostEqual(r109['avg_net_return_pct'], 2.3148, places=4)
        self.assertEqual(r109['sample_label'], 'insufficient_sample')
        self.assertTrue(ROW_FIELDS <= set(r109))
        report = cc.build_report(
            {'window_days': 20, 'starting_capital_paise': 50_000_000, 'strategies': {'109': trades}},
            {'window_days': 5, 'starting_capital_paise': 50_000_000, 'strategies': {}},
            '2026-01-15')
        self.assertIn('109', report['strategies'])
        self.assertIn('| 109 |', cc.render_markdown(report))

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
        # Raw paise P&L is shown alongside the INR figure.
        self.assertIn('net P&L (paise)', md)
        self.assertIn('4700', md)  # merged S1 net_pnl_paise

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
