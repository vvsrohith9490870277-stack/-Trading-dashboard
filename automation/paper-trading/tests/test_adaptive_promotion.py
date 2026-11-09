import sqlite3, sys, unittest
from datetime import date, timedelta
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_fixtures import gate8_cache_from_series
import adaptive_engine as ae
import adaptive_logging as alog
import adaptive_promotion as ap
import atomic_gates as ag
import calibration as cal
import pit_archive as pit
from tests.test_atomic_gates import _build_trades
from tests.test_calibration_and_truncation import _make_dataset_with_future_split
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_fixtures import calibration_partitions, ci_success_payload, seed_observation_evidence, trading_days
import adaptive_ci
import adaptive_policy as apol


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


class AdaptivePromotionTests(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
            if sql.strip():
                self.db.batch([(sql.strip(), ())])
        pit.init(self.db)
        alog.init(self.db)

    def test_observation_only_and_promotion_locks_enforce_calibration_and_ci_lookahead(self):
        self.assertTrue(ap.default_observation_only())
        ns = 'test_promo'
        sp = ae.spawn_candidate(self.db, ns, strategy=3, params={'rsi_entry': 36.0}, track='B')
        sid = sp['spawn_id']

        rng = np.random.default_rng(12345)
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

        ready = ae.evaluate_stopping_and_window(self.db, ns, sid, days_elapsed=45, new_discordant_trades=12, distinct_entry_weeks=6)
        self.assertTrue(ready['ready_for_final_gate_evaluation'])
        gate_res = ag.evaluate_all_atomic_gates(
            self.db, ns, spawn_id=sid, strategy=3, decision_day='2025-08-29',
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

        # 1. Blocked while observation_only=True
        b1 = ap.attempt_promotion(self.db, ns, sid, observation_only=True)
        self.assertFalse(b1['promoted'])
        self.assertIn('observation_only_mode_enabled', b1['prerequisites']['blockers'])

        # 2. Even with observation_only=False, blocked until observation period, calibration, and CI lookahead pass
        b2 = ap.attempt_promotion(self.db, ns, sid, observation_only=False)
        self.assertFalse(b2['promoted'])
        self.assertIn('observation_only_period_not_completed', b2['prerequisites']['blockers'])
        self.assertIn('calibration_go_power_floor_not_passed', b2['prerequisites']['blockers'])
        self.assertIn('ci_lookahead_truncation_test_not_passed', b2['prerequisites']['blockers'])

        # 3. Observation evidence (derived from logs), calibration GO + passed holdout, CI proven for the exact SHA
        sessions = trading_days(apol.POLICY['observation_min_sessions'], start=date(2025, 9, 1))
        seed_observation_evidence(self.db, ns, 3, sessions)
        st = ap.record_observation_period_completed(self.db, ns, strategy=3)
        self.assertTrue(st['completed'])
        parts, old_dates, _, hold_dates = calibration_partitions(120)
        cal_run = cal.run_calibration_go_no_go(
            self.db, ns, strategy=3,
            real_d_t=rng.normal(0.0, 0.0008, size=120),
            smallest_actionable_edge=0.0005,
            d_t_dates=old_dates, partitions=parts,
            window_days=45,
        )
        b3 = ap.attempt_promotion(self.db, ns, sid, observation_only=False)
        self.assertIn('holdout_vault_not_touched', b3['prerequisites']['blockers'])
        cal.touch_holdout_vault_once(self.db, ns, 'vault_s3', cal_run['calibration_id'], True, holdout_dates=hold_dates)

        sha = 'a' * 40
        # A stored "CI passed" value can no longer unlock promotion: CI must be proven at promotion time.
        self.db.batch([("INSERT INTO paper_meta(namespace,key,value) VALUES(?,?,?)", (ns, 'adaptive_ci_lookahead_passed', '1'))])
        self.assertFalse(hasattr(ap, 'record_ci_lookahead_pass'))
        with mock.patch.object(adaptive_ci, '_http_get_json', side_effect=OSError('offline')):
            b4 = ap.attempt_promotion(self.db, ns, sid, observation_only=False, ci_sha=sha)
        self.assertFalse(b4['promoted'])
        self.assertEqual(b4['prerequisites']['blockers'], ['ci_lookahead_truncation_test_not_passed'])

        # 4. Now promotion succeeds and places candidate as the sole Track C champion for Strategy 3
        with mock.patch.object(adaptive_ci, '_http_get_json', return_value=ci_success_payload(sha)), \
                mock.patch.object(adaptive_ci, 'run_local_truncation_suite', return_value={'passed': True}):
            promo = ap.attempt_promotion(self.db, ns, sid, observation_only=False, ci_sha=sha)
        self.assertTrue(promo['promoted'], promo['prerequisites'])
        self.assertEqual(promo['track'], 'C')
        self.assertEqual(promo['status'], 'PROMOTED')

        champions = self.db.query(
            "SELECT * FROM adaptive_tracks WHERE namespace=? AND track='C' AND strategy=3 AND status='champion'",
            (ns,),
        )
        self.assertEqual(len(champions), 1)
        self.assertEqual(champions[0]['spawn_id'], sid)


if __name__ == '__main__':
    unittest.main()
