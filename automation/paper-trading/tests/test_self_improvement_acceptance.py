import sqlite3
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_fixtures import calibration_partitions, ci_success_payload, gate8_cache_from_series, seed_observation_evidence, trading_days
from datetime import date
from unittest import mock
import adaptive_ci
import adaptive_policy as apol
import adaptive_engine as ae
import adaptive_logging as alog
import adaptive_promotion as ap
import atomic_gates as ag
import calibration as cal
import pit_archive as pit
import trial_counting as tc
from tests.test_atomic_gates import _build_trades
from tests.test_calibration_and_truncation import _make_dataset_with_future_split


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


class TestSelfImprovementAcceptance(unittest.TestCase):
    """Full Section B1-B6 end-to-end acceptance test suite for the Self-Improvement 3-Tier system."""

    def setUp(self):
        self.db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text(encoding='utf-8').split(';'):
            if sql.strip():
                self.db.batch([(sql.strip(), ())])
        pit.init(self.db)
        alog.init(self.db)

    def test_b1_b2_b3_b4_b5_b6_full_self_improvement_lifecycle(self):
        ns = 'acceptance_v1'

        # B1: Automated look-ahead truncation test (cutting both price and corporate-action data at t)
        dates, stock_df, idx_df, corp_actions = _make_dataset_with_future_split(days=260)
        trunc_rep = cal.run_lookahead_truncation_test(stock_df, idx_df, corp_actions, [dates[-5], dates[-2]])
        self.assertTrue(trunc_rep['passed'])
        self.assertEqual(len(trunc_rep['mismatches']), 0)
        self.assertGreater(trunc_rep["checks_run"], 0)  # CI pass is now proven per-SHA via public check-runs, not a DB flag

        # B1: PIT Bhavcopy SERIES archive & EQ-only new entry enforcement
        pit.ingest_bhavcopy_series(
            self.db,
            [
                {'symbol': 'RELIANCE-EQ', 'series': 'EQ', 'close': 2500.0},
                {'symbol': 'TATAMOTORS-EQ', 'series': 'BE', 'close': 900.0},
            ],
            trade_date='2025-05-02',
        )
        self.assertEqual(pit.get_pit_series(self.db, 'RELIANCE-EQ', '2025-05-02', as_of_date='2025-05-02'), 'EQ')
        self.assertEqual(pit.get_pit_series(self.db, 'TATAMOTORS-EQ', '2025-05-02', as_of_date='2025-05-02'), 'BE')

        # B2: 3-tier engine, warm-start (60d Fast Swing / 100-120d Trend), net-of-haircut starting equity
        sp_fast = ae.spawn_candidate(self.db, ns, strategy=3, params={'rsi_entry': 35.0}, track='B')
        sp_trend = ae.spawn_candidate(self.db, ns, strategy=1, params={'entry_lookback': 22}, track='B')
        self.assertEqual(sp_fast['warm_start_days'], 60)
        self.assertEqual(sp_fast['eval_window_target_days'], 45)
        self.assertIn(sp_trend['warm_start_days'], range(100, 121))
        self.assertEqual(sp_trend['eval_window_target_days'], 75)

        val = ae.value_starting_equity_net_of_haircut(
            cash_paise=30_000_000,
            open_positions=[{'symbol': 'RELIANCE-EQ', 'qty': 80, 'entry_price': 2400.0, 'mark_price': 2500.0, 'series': 'EQ'}],
        )
        self.assertGreater(val['exit_haircut_paise'], 0)
        self.assertEqual(
            val['starting_equity_net_haircut_paise'],
            val['starting_equity_gross_paise'] - val['exit_haircut_paise'],
        )

        # B3: Linear K_eff formula and Gate 2 effective critical value link
        keff_info = tc.compute_k_eff(k_cand=4, rho_bar=0.5)
        self.assertAlmostEqual(keff_info['k_eff'], 2.5, places=6)
        link_info = tc.evaluate_gate2_critical_link(t_hac=2.20, k_cand=4, rho_bar=0.5, alpha=0.05)
        self.assertGreaterEqual(link_info['effective_critical_value'], 1.40)

        # B3 & B5: Atomic Gates 1-9, residualized d_t check, 250-placebo with selection & discordant-only jitter
        rng = np.random.default_rng(20261109)
        n = 60
        r_idx = rng.normal(0.0004, 0.006, size=n)
        r_base = 0.7 * r_idx + rng.normal(0.0001, 0.002, size=n)
        disc_edge = np.zeros(n)
        disc_days = np.arange(0, n, 2)
        disc_edge[disc_days] = 0.0018 + rng.normal(0.0, 0.0004, size=len(disc_days))
        r_var = r_base + disc_edge
        stressed_r_var = r_base + 0.75 * disc_edge
        var_trades, base_trades = _build_trades(n_new=12, n_inherited=3)
        mean_d = float(np.mean(r_var - r_base))

        ready = ae.evaluate_stopping_and_window(self.db, ns, sp_fast['spawn_id'], days_elapsed=45, new_discordant_trades=12, distinct_entry_weeks=6)
        self.assertTrue(ready['ready_for_final_gate_evaluation'])
        gate_res = ag.evaluate_all_atomic_gates(
            self.db, ns, spawn_id=sp_fast['spawn_id'], strategy=3, decision_day='2025-08-29',
            r_var=r_var, r_base=r_base, index_returns=r_idx,
            var_trades=var_trades, base_trades=base_trades,
            warm_start_end_date='2025-05-30',
            neighbor_improvements=[mean_d * 0.85, mean_d * 0.82],
            stressed_r_var=stressed_r_var,
            k_cand=2, rho_bar=0.4, observation_only=True,
            microstructure_checks=dict.fromkeys(ag.MICROSTRUCTURE_LOCKS, True),
            gate8_cache=gate8_cache_from_series(r_var, r_base),
        )
        self.assertTrue(gate_res['overall_passed'])
        self.assertTrue(gate_res['details']['gate_2_and_4']['residualized_check']['passed'])
        self.assertEqual(gate_res['details']['gate_8']['m_runs'], 250)

        # B4: Calibration go/no-go (power floor, HAC/bootstrap agreement), date-enforced oldest-partition tuning,
        # single-touch holdout vault, observation sessions derived from logs, CI proven for the exact SHA
        parts, old_dates, _, hold_dates = calibration_partitions(n)
        tuned = cal.tune_thresholds_on_partition(parts, old_dates, [0.00005, 0.0001, 0.0002], r_var - r_base)
        self.assertTrue(tuned['oldest_partition_verified'])

        parts120, old120, _, hold120 = calibration_partitions(120)
        cal_res = cal.run_calibration_go_no_go(
            self.db, ns, strategy=3,
            real_d_t=rng.normal(0.0, 0.0008, size=120),
            smallest_actionable_edge=0.0005,
            d_t_dates=old120, partitions=parts120,
            window_days=45,
        )
        self.assertEqual(cal_res['go_no_go'], 'GO')
        self.assertGreaterEqual(cal_res['measured_power'], 0.30)
        cal.touch_holdout_vault_once(self.db, ns, 'vault_acc', cal_res['calibration_id'], True, holdout_dates=hold120)

        seed_observation_evidence(self.db, ns, 3, trading_days(apol.POLICY['observation_min_sessions'], start=date(2025, 9, 1)))
        self.assertTrue(ap.record_observation_period_completed(self.db, ns, strategy=3)['completed'])
        sha = 'd' * 40
        with mock.patch.object(adaptive_ci, '_http_get_json', return_value=ci_success_payload(sha)), \
                mock.patch.object(adaptive_ci, 'run_local_truncation_suite', return_value={'passed': True}):
            promo = ap.attempt_promotion(self.db, ns, sp_fast['spawn_id'], observation_only=False, ci_sha=sha)
        self.assertTrue(promo['promoted'])
        self.assertEqual(promo['track'], 'C')

        # B6: Reconstructable SHA-256 hash-chained decision log verification
        chain = alog.verify_hash_chain(self.db, ns)
        self.assertTrue(chain['ok'])
        self.assertGreaterEqual(chain['count'], 5)
        recon = alog.export_reconstructed_record(self.db, ns, spawn_id=sp_fast['spawn_id'])
        self.assertTrue(recon['reconstructable'])
        self.assertTrue(recon['chain_verified'])
        self.assertIsNotNone(recon['spawn_record'])
        self.assertGreaterEqual(len(recon['gate_evaluations']), 1)


if __name__ == '__main__':
    unittest.main()
