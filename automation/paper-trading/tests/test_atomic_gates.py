import sqlite3, sys, unittest
from datetime import date, timedelta
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_fixtures import gate8_cache_from_series
import adaptive_engine as ae
import adaptive_logging as alog
import atomic_gates as ag
import pit_archive as pit


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


def _build_trades(n_new=12, n_inherited=3):
    base_trades = []
    var_trades = []
    # Inherited trades during warm-start (on or before 2025-05-30)
    for i in range(n_inherited):
        base_trades.append({
            'symbol': f'INH{i}-EQ',
            'entry_date': f'2025-05-{10 + i:02d}',
            'exit_date': f'2025-06-{1 + i:02d}',
            'qty': 20,
            'net_pnl_paise': 20_000,
        })
        var_trades.append({
            'symbol': f'INH{i}-EQ',
            'entry_date': f'2025-05-{10 + i:02d}',
            'exit_date': f'2025-06-{2 + i:02d}',
            'qty': 20,
            'net_pnl_paise': 25_000,
        })
    # Evaluation-window discordant trades across 6 distinct weeks
    start_d = date(2025, 6, 2)
    for i in range(n_new):
        ed = (start_d + timedelta(days=i * 4)).isoformat()
        xd = (start_d + timedelta(days=i * 4 + 3)).isoformat()
        var_trades.append({
            'symbol': f'SYM{i}-EQ',
            'entry_date': ed,
            'exit_date': xd,
            'qty': 25,
            'net_pnl_paise': 15_000 + (i % 3) * 5_000,
        })
    return var_trades, base_trades


class AtomicGatesTests(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
            if sql.strip():
                self.db.batch([(sql.strip(), ())])
        pit.init(self.db)
        alog.init(self.db)

    def test_all_nine_atomic_gates_250_placebo_and_track_c_superiority_pass_on_valid_challenger(self):
        ns = 'test_gates'
        sp = ae.spawn_candidate(self.db, ns, strategy=3, params={'rsi_entry': 36.0}, track='B')
        sid = sp['spawn_id']

        rng = np.random.default_rng(12345)
        n = 60
        r_idx = rng.normal(0.0004, 0.006, size=n)
        r_base = 0.7 * r_idx + rng.normal(0.0001, 0.002, size=n)
        # 20 concordant days (0 active return) and 40 discordant days with consistent positive alpha
        disc_edge = np.zeros(n)
        disc_days = np.arange(0, n, 2)  # 30 discordant days
        disc_edge[disc_days] = 0.0018 + rng.normal(0.0, 0.0004, size=len(disc_days))
        r_var = r_base + disc_edge
        stressed_r_var = r_base + 0.75 * disc_edge
        r_track_c = r_base + 0.30 * disc_edge  # Challenger beats Track C by > +0.35 bps/day

        var_trades, base_trades = _build_trades(n_new=12, n_inherited=3)
        mean_d = float(np.mean(r_var - r_base))
        neighbor_improvements = [mean_d * 0.85, mean_d * 0.82, mean_d * 0.79]

        ready = ae.evaluate_stopping_and_window(self.db, ns, sid, days_elapsed=45, new_discordant_trades=12, distinct_entry_weeks=6)
        self.assertTrue(ready['ready_for_final_gate_evaluation'])
        res = ag.evaluate_all_atomic_gates(
            self.db,
            namespace=ns,
            spawn_id=sid,
            strategy=3,
            decision_day='2025-08-29',
            r_var=r_var,
            r_base=r_base,
            index_returns=r_idx,
            var_trades=var_trades,
            base_trades=base_trades,
            warm_start_end_date='2025-05-30',
            neighbor_improvements=neighbor_improvements,
            stressed_r_var=stressed_r_var,
            k_cand=2,
            rho_bar=0.4,
            r_track_c=r_track_c,
            track_c_threshold_bps=0.35,
            observation_only=True,
            microstructure_checks=dict.fromkeys(ag.MICROSTRUCTURE_LOCKS, True),
            gate8_cache=gate8_cache_from_series(r_var, r_base),
        )
        self.assertTrue(res['overall_passed'])
        self.assertEqual(res['status'], 'OBSERVATION_ONLY')
        for gate_name, passed in res['gates_summary'].items():
            self.assertTrue(passed, f'Expected {gate_name} to pass')

        # Verify Gate 8 ran M = 250 placebo runs and jittered only discordant days
        g8 = res['details']['gate_8']
        self.assertEqual(g8['m_runs'], 250)
        self.assertEqual(g8['new_discordant_trades'], 30)  # only the 30 discordant trades are jittered
        self.assertEqual(g8['pool_size'], 3)  # spawn-time screening re-run over the whole pool
        self.assertEqual(g8['cache_equivalence_max_abs_diff'], 0.0)
        self.assertGreater(g8['observed_t_hac'], g8['placebo_threshold_t'])

        # Verify Track C comparison counted as a trial
        tc_cmp = res['details']['track_c_comparison']
        self.assertTrue(tc_cmp['passed'])
        self.assertTrue(tc_cmp['counted_as_trial'])
        self.assertGreaterEqual(tc_cmp['mean_active_return_bps'], 0.35)

    def test_track_c_probation_longer_for_trend_and_2sigma_tolerance_band(self):
        ns = 'test_prob'
        hist_d_t = np.array([0.0005, -0.0004, 0.0006, -0.0003, 0.0004, -0.0005] * 10)
        # Fast swing requires 30 days; Trend requires 60 days
        fast_prob = ag.evaluate_track_c_probation(
            self.db, ns, spawn_id='sp_fast', strategy=3,
            realized_d_t=np.full(30, 0.0002), historical_d_t_for_sigma=hist_d_t,
        )
        self.assertEqual(fast_prob['required_days'], 30)
        self.assertEqual(fast_prob['status'], 'PASSED_PROBATION')

        trend_prob = ag.evaluate_track_c_probation(
            self.db, ns, spawn_id='sp_trend', strategy=1,
            realized_d_t=np.full(30, 0.0002), historical_d_t_for_sigma=hist_d_t,
        )
        self.assertEqual(trend_prob['required_days'], 60)
        self.assertEqual(trend_prob['status'], 'IN_PROBATION')

        # Severe negative tracking error beyond 2*sigma fails probation
        fail_prob = ag.evaluate_track_c_probation(
            self.db, ns, spawn_id='sp_fail', strategy=1,
            realized_d_t=np.full(20, -0.0050), historical_d_t_for_sigma=hist_d_t,
        )
        self.assertEqual(fail_prob['status'], 'FAILED_PROBATION')
        self.assertFalse(fail_prob['within_band'])


if __name__ == '__main__':
    unittest.main()
