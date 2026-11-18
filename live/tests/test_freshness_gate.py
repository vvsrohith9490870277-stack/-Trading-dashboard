"""Task 5 tests: freshness gate + PIT coverage from the expected calendar.

Fixtures are synthetic date sets (no prices, no real data). The gate's inputs
are plain sets/lists so no Turso, NSE or market snapshot is needed.
"""
import sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_daily as ad


def calendar(frm, to):
    return ad.expected_session_days(frm, to)


class FreshnessGateTests(unittest.TestCase):
    def test_stale_snapshot_two_sessions_behind_is_blocked(self):
        # (a) snapshot ends 2026-11-09, session day 2026-11-11.
        days = calendar('2026-11-02', '2026-11-11')
        covered = {d for d in days if d <= '2026-11-09'}
        out = ad.evaluate_freshness('2026-11-11', '2026-11-09', covered, sorted(covered), '2026-11-02')
        self.assertEqual(out['simulation_status'], 'BLOCKED_STALE_INPUT')
        self.assertFalse(out['input_freshness']['fresh'])
        self.assertIn('2026-11-09', str(out['input_freshness']['reasons']))
        self.assertEqual(out['input_freshness']['latest_completed_bar_in_snapshot'], '2026-11-09')
        self.assertFalse(out['input_freshness']['bhavcopy_for_session_day'])

    def test_full_fresh_fixture_is_unchanged_simulated(self):
        # (b) everything present through the session day.
        days = calendar('2026-11-02', '2026-11-11')
        out = ad.evaluate_freshness('2026-11-11', '2026-11-11', set(days), days, '2026-11-02')
        self.assertEqual(out['simulation_status'], 'SIMULATED')
        self.assertTrue(out['input_freshness']['fresh'])
        self.assertTrue(out['input_freshness']['bhavcopy_for_session_day'])
        self.assertEqual(out['input_freshness']['latest_bhavcopy_date'], '2026-11-11')

    def test_coverage_below_99_lists_missing_dates(self):
        # (c) one archive day missing out of ~44 -> coverage < 0.99.
        days = calendar('2026-09-01', '2026-11-11')
        missing = '2026-10-14'
        covered = {d for d in days if d != missing}
        out = ad.evaluate_freshness('2026-11-11', '2026-11-11', covered, days, '2026-09-01')
        self.assertEqual(out['simulation_status'], 'BLOCKED_PIT_SERIES_ARCHIVE_P1')
        self.assertIn(missing, out['pit_series_coverage']['missing_dates'])
        self.assertLess(out['pit_series_coverage']['coverage'], 0.99)
        self.assertEqual(out['pit_series_coverage']['missing_count'],
                         len(out['pit_series_coverage']['missing_dates']))

    def test_unlisted_no_session_day_is_not_counted_missing(self):
        # (d) a weekday with no index bar and no archive row, on/before the
        # latest bar, is a no_session day — not a missing archive day.
        days = calendar('2026-11-02', '2026-11-11')
        holiday = '2026-11-05'  # weekday, unlisted in config holidays
        covered = {d for d in days if d != holiday}
        index = [d for d in days if d != holiday]
        out = ad.evaluate_freshness('2026-11-11', '2026-11-11', covered, index, '2026-11-02')
        self.assertEqual(out['simulation_status'], 'SIMULATED')
        self.assertNotIn(holiday, out['pit_series_coverage']['missing_dates'])
        self.assertEqual(out['pit_series_coverage']['coverage'], 1.0)

    def test_same_evening_bhavcopy_absent_is_blocked_pit(self):
        # (e) session-day file not yet published: everything else present.
        days = calendar('2026-11-02', '2026-11-11')
        covered = {d for d in days if d != '2026-11-11'}
        out = ad.evaluate_freshness('2026-11-11', '2026-11-11', covered, days, '2026-11-02')
        self.assertEqual(out['simulation_status'], 'BLOCKED_PIT_SERIES_ARCHIVE_P1')
        self.assertFalse(out['input_freshness']['bhavcopy_for_session_day'])
        self.assertIn('2026-11-11', out['pit_series_coverage']['missing_dates'])

    def test_missing_dates_capped_at_50_with_true_count(self):
        days = calendar('2026-06-02', '2026-11-11')
        covered = set()
        out = ad.evaluate_freshness('2026-11-11', '2026-11-11', covered, days, '2026-06-02')
        self.assertEqual(out['simulation_status'], 'BLOCKED_PIT_SERIES_ARCHIVE_P1')
        self.assertEqual(len(out['pit_series_coverage']['missing_dates']), 50)
        self.assertGreater(out['pit_series_coverage']['missing_count'], 50)


if __name__ == '__main__':
    unittest.main()
