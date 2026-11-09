import sqlite3, sys, unittest
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adaptive_logging as alog
import calibration as cal
import pit_archive as pit
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_fixtures import calibration_partitions


class MemDB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row

    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)

    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def _make_dataset_with_future_split(days=260, start_date=date(2025, 1, 1)):
    dates = []
    cur = start_date
    while len(dates) < days:
        if cur.weekday() < 5 and cur.isoformat() != '2026-11-08':
            dates.append(cur.isoformat())
        cur += timedelta(days=1)

    split_ex_date = dates[-5]  # 5 sessions before the end: 2:1 split
    idx_rows = []
    stock_rows = []
    idx_px = 1000.0
    st_px = 800.0
    for i, d in enumerate(dates):
        idx_ret = 0.0012 if i % 2 == 0 else -0.0005
        idx_px *= (1.0 + idx_ret)
        idx_rows.append({
            'trade_date': d,
            'symbol': 'NIFTY500',
            'open': idx_px * 0.998,
            'high': idx_px * 1.004,
            'low': idx_px * 0.996,
            'close': idx_px,
            'volume': 1_000_000,
        })
        st_ret = idx_ret * 1.5 + 0.001
        if d == split_ex_date:
            st_px = (st_px * (1.0 + st_ret)) / 2.0
        else:
            st_px = st_px * (1.0 + st_ret)
        stock_rows.append({
            'trade_date': d,
            'symbol': 'RELIANCE-EQ',
            'open': st_px * 0.997,
            'high': st_px * 1.006,
            'low': st_px * 0.994,
            'close': st_px,
            'volume': 200_000 if d < split_ex_date else 400_000,
        })
    corp_actions = [{
        'symbol': 'RELIANCE-EQ',
        'ex_date': split_ex_date,
        'kind': 'SPLIT',
        'ratio_num': 2,
        'ratio_den': 1,
    }]
    return dates, pd.DataFrame(stock_rows), pd.DataFrame(idx_rows), corp_actions


class CalibrationAndTruncationTests(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
            if sql.strip():
                self.db.batch([(sql.strip(), ())])
        pit.init(self.db)
        alog.init(self.db)

    def test_mde_injected_edge_and_30pct_power_floor_go_no_go(self):
        ns = 'test_cal'
        rng = np.random.default_rng(7)
        # Low-noise d_t where a +5 bps/day actionable edge has high power (>> 30%)
        real_d_t_low_noise = rng.normal(0.0, 0.0008, size=120)
        parts, old_dates, _, _ = calibration_partitions(120)
        dated = dict(d_t_dates=old_dates, partitions=parts)
        go_res = cal.run_calibration_go_no_go(
            self.db, ns, strategy=3,
            real_d_t=real_d_t_low_noise,
            smallest_actionable_edge=0.0005,
            window_days=45,
            k_eff=2.0,
            **dated,
        )
        self.assertEqual(go_res['go_no_go'], 'GO')
        self.assertGreaterEqual(go_res['measured_power'], 0.30)

        # High-noise d_t where a tiny +0.5 bps/day edge has power < 30% -> NO_GO_MONITOR_ONLY
        real_d_t_high_noise = rng.normal(0.0, 0.0080, size=120)
        nogo_res = cal.run_calibration_go_no_go(
            self.db, ns, strategy=3,
            real_d_t=real_d_t_high_noise,
            smallest_actionable_edge=0.00005,
            window_days=45,
            k_eff=3.0,
            lengthen_on_low_power=False,
            **dated,
        )
        self.assertEqual(nogo_res['go_no_go'], 'NO_GO_MONITOR_ONLY')
        self.assertEqual(nogo_res['pipeline_mode'], 'MONITOR_ONLY')
        self.assertLess(nogo_res['measured_power'], 0.30)
        self.assertFalse(nogo_res['automatic_promotion_enabled'])

        # Lengthen window option when power < 30%
        lengthen_res = cal.run_calibration_go_no_go(
            self.db, ns, strategy=3,
            real_d_t=real_d_t_high_noise,
            smallest_actionable_edge=0.00005,
            window_days=45,
            k_eff=3.0,
            lengthen_on_low_power=True,
            **dated,
        )
        self.assertEqual(lengthen_res['go_no_go'], 'NO_GO_LENGTHEN_WINDOW')
        self.assertGreater(lengthen_res['recommended_window_days'], 45)

    def test_oldest_partition_tuning_and_single_touch_holdout_vault(self):
        ns = 'test_vault'
        d_t = np.random.default_rng(11).normal(0.0, 0.0008, size=120)
        parts, old_dates, mid_dates, hold_dates = calibration_partitions(len(d_t))

        # Tuning on oldest partition succeeds (enforced by the dates of the data, not by a name)
        tuned = cal.tune_thresholds_on_partition(parts, old_dates, [0.00005, 0.0001, 0.0002], d_t)
        self.assertTrue(tuned['oldest_partition_verified'])

        # Tuning with any date from a newer partition or the holdout vault raises PermissionError
        with self.assertRaises(PermissionError):
            cal.tune_thresholds_on_partition(parts, old_dates[:-1] + [mid_dates[0]], [0.0001], d_t)
        with self.assertRaises(PermissionError):
            cal.tune_thresholds_on_partition(parts, old_dates[:-1] + [hold_dates[0]], [0.0001], d_t)

        cal_run = cal.run_calibration_go_no_go(
            self.db, ns, strategy=1, real_d_t=d_t, smallest_actionable_edge=0.0005, window_days=75,
            d_t_dates=old_dates, partitions=parts,
        )
        self.assertEqual(cal_run['go_no_go'], 'GO')
        # A holdout touch must use holdout-partition dates
        with self.assertRaises(PermissionError):
            cal.touch_holdout_vault_once(self.db, ns, 'vault_bad', cal_run['calibration_id'], True, holdout_dates=mid_dates)
        spent = cal.touch_holdout_vault_once(
            self.db, ns, vault_id='vault_s1', calibration_id=cal_run['calibration_id'], holdout_passed=False, holdout_dates=hold_dates
        )
        self.assertTrue(spent['vault_spent'])
        self.assertEqual(spent['status'], 'SPENT_MONITOR_ONLY_UNTIL_FRESH_HOLDOUT')
        self.assertFalse(cal.holdout_eligibility(self.db, ns, 1)['eligible'])

        # Second touch on the same holdout vault is strictly blocked
        with self.assertRaises(PermissionError):
            cal.touch_holdout_vault_once(
                self.db, ns, vault_id='vault_s1', calibration_id=cal_run['calibration_id'], holdout_passed=True, holdout_dates=hold_dates
            )

    def test_meta_kill_switch_triggers_when_champion_underperforms_track_a(self):
        ns = 'test_mk'
        self.db.batch([(
            "INSERT INTO adaptive_tracks(namespace,track,strategy,slot_index,spawn_id,status,params_json,allocated_capital_paise,cash_paise,starting_equity_net_haircut_paise,created_at,updated_at) "
            "VALUES('test_mk','C',2,1,'sp_c_2','champion','{}',50000000,50000000,50000000,'2026-11-09','2026-11-09')",
            (),
        )])
        r_base = np.full(30, 0.0010)
        r_champ = np.full(30, -0.0005)  # Consistently loses to Track A
        mk = cal.evaluate_meta_kill_switch(self.db, ns, strategy=2, champion_r_series=r_champ, track_a_r_series=r_base, min_days=20)
        self.assertTrue(mk['meta_kill_triggered'])
        tr = self.db.query("SELECT status FROM adaptive_tracks WHERE namespace=? AND track='C' AND strategy=2", (ns,))[0]
        self.assertEqual(tr['status'], 'killed')

    def test_automated_lookahead_truncation_cuts_both_price_and_corporate_actions_at_t(self):
        dates, stock_df, idx_df, corp_actions = _make_dataset_with_future_split(days=260)
        # Cut before the split ex-date, on the split ex-date, and after the split ex-date
        cut_dates = [dates[-10], dates[-6], dates[-5], dates[-2]]
        res = cal.run_lookahead_truncation_test(
            stock_df=stock_df,
            index_df=idx_df,
            corp_actions=corp_actions,
            cut_dates=cut_dates,
            strategies=range(1, 8),
        )
        self.assertTrue(res['passed'], f"Truncation mismatches: {res['mismatches']}")
        self.assertEqual(len(res['mismatches']), 0)
        self.assertEqual(res['checks_run'], len(cut_dates) * 9)  # 1 prefilter + 1 back-adjusted history + 7 strategies


if __name__ == '__main__':
    unittest.main()
