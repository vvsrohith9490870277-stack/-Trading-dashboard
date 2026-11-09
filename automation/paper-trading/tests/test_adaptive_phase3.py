"""Phase 3 (Adaptive Completion Plan v2): Gate 6 generated neighbours + D5 median rule; Gate 8 cached-pool placebos,
cache equivalence, fail-closed paths and the E2 null acceptance (500 nulls, point <= 12%, one-sided UCB <= 15%)."""
import sys, unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_placebo as aplc
import adaptive_policy as apol
import atomic_gates as ag
from adaptive_fixtures import gate8_cache_from_series

P = apol.POLICY


class Gate6Tests(unittest.TestCase):
    def test_neighbours_one_param_at_a_time_from_policy_steps(self):
        gen = aplc.generate_plateau_neighbours(3, {'rsi_entry': 35.0})
        steps = P['plateau_step_sizes']['3']
        self.assertEqual(len(gen['neighbours']) + len(gen['dropped']), 2 * P['plateau_aggregation']['neighbour_radius_steps'] * len(steps))
        for nb in gen['neighbours']:
            changed = [k for k in nb['params'] if nb['params'][k] != gen['centre'][k]]
            self.assertEqual(changed, [nb['param']])
            self.assertEqual(nb['candidate_kind'], 'PLATEAU_NEIGHBOR')
        vals = sorted(nb['params']['rsi_entry'] for nb in gen['neighbours'] if nb['param'] == 'rsi_entry')
        self.assertEqual(vals, [35.0 - steps['rsi_entry'], 35.0 + steps['rsi_entry']])

    def test_ratio_params_pushed_out_of_domain_are_dropped_not_replaced(self):
        gen = aplc.generate_plateau_neighbours(7, {'near_high_ratio': 0.995})
        self.assertTrue(any(d['param'] == 'near_high_ratio' and d['reason'] == 'out_of_domain' for d in gen['dropped']))

    def test_median_rule_knife_edge_fails_smooth_passes(self):
        knife = ag.evaluate_gate6_parameter_plateau(1.0, [0.1, 0.2, 1.0, 0.1])
        smooth = ag.evaluate_gate6_parameter_plateau(1.0, [0.8, 0.9, 0.85, 0.78])
        negative = ag.evaluate_gate6_parameter_plateau(1.0, [0.9, 0.9, 0.9, -0.01])
        self.assertFalse(knife['passed'])
        self.assertTrue(smooth['passed'])
        self.assertFalse(negative['passed'])  # a neighbour below min_neighbour_improvement fails
        self.assertTrue(smooth['neighbours_excluded_from_k_cand'])
        with self.assertRaises(ValueError):
            ag.evaluate_gate6_parameter_plateau(1.0, [0.9], min_retention=0.1)

    def test_gate6_with_simulation_smooth_vs_knife_edge_surface(self):
        n = 60
        rng = np.random.default_rng(5)
        r_base = rng.normal(0, 0.01, n)

        def surface(knife):
            def sim(p):
                dist = abs(p['rsi_entry'] - 35.0) + abs(p['rsi_exit'] - 60.0) + abs(p['rsi_period'] - 14) + abs(p['sma_exit'] - 5) + abs(p['sma_trend'] - 200) / 10.0
                edge = (0.002 if dist == 0 else 0.0002) if knife else 0.002 / (1.0 + 0.05 * dist)
                return r_base + edge
            return sim
        good = aplc.evaluate_gate6_with_simulation(3, {'rsi_entry': 35.0}, surface(False), r_base)
        bad = aplc.evaluate_gate6_with_simulation(3, {'rsi_entry': 35.0}, surface(True), r_base)
        self.assertTrue(good['passed'], good)
        self.assertFalse(bad['passed'], bad)
        with self.assertRaises(ValueError):
            aplc.evaluate_gate6_with_simulation(3, {'rsi_entry': 35.0}, lambda p: np.zeros(n + (p != {'rsi_entry': 35.0} and 1)), r_base)


def _null_case(rng, rho, hold_rng, n=45, n_sym=8, n_pool=3, n_base=12, n_new=(10, 18), w=0.1):
    rets = {}
    for s in range(n_sym):
        e = rng.normal(0, 0.015, n)
        y = np.zeros(n)
        for i in range(n):
            y[i] = (rho * y[i - 1] if i else 0.0) + e[i]
        rets[f'S{s}'] = y

    def rand_trades(k):
        return [{'symbol': f'S{rng.integers(n_sym)}', 'entry_idx': int(rng.integers(0, n - 2)), 'hold': int(rng.integers(*hold_rng)),
                 'weight': w} for _ in range(k)]
    base = rand_trades(n_base)
    pool = []
    for _ in range(n_pool):
        keep = [t for t in base if rng.random() > 0.3]
        pool.append(aplc.build_member_cache(keep + rand_trades(int(rng.integers(*n_new))), base, rets, n))
    cand = int(np.argmax([aplc.hac_t_rows(m['d_t'])[0] for m in pool]))  # the screening picks the best member
    return cand, pool, rets


class Gate8Tests(unittest.TestCase):
    def test_missing_cache_and_mismatched_cache_fail_closed(self):
        rng = np.random.default_rng(1)
        r_base = rng.normal(0, 0.01, 60)
        r_var = r_base.copy(); r_var[::2] += 0.002
        d = r_var - r_base
        self.assertEqual(ag.evaluate_gate8_from_cache(None, d)['reason'], 'gate8_requires_cached_pool_rescreening')
        cache = gate8_cache_from_series(r_var, r_base)
        self.assertEqual(ag.evaluate_gate8_from_cache(cache, d + 1e-6)['reason'], 'gate8_cache_does_not_reproduce_candidate_d_t')

    def test_too_few_discordant_trades_fails_closed_even_with_huge_edge(self):
        n = 60
        r_base = np.zeros(n); r_var = np.zeros(n)
        r_var[:P['min_new_discordant_trades'] - 1] = 0.05
        res = ag.evaluate_gate8_from_cache(gate8_cache_from_series(r_var, r_base), r_var - r_base)
        self.assertFalse(res['passed'])
        self.assertEqual(res['reason'], 'too_few_discordant_trades_fail_closed')

    def test_planted_edge_passes_and_cache_equals_full_recompute(self):
        rng = np.random.default_rng(20261110)
        n = 60
        r_base = rng.normal(0, 0.01, n)
        r_var = r_base.copy(); r_var[::2] += 0.004 + rng.normal(0, 0.0005, n // 2)
        res = ag.evaluate_gate8_from_cache(gate8_cache_from_series(r_var, r_base), r_var - r_base)
        self.assertTrue(res['passed'], res)
        self.assertEqual(res['m_runs'], P['placebo_runs'])
        self.assertEqual(res['cache_equivalence_max_abs_diff'], 0.0)
        self.assertGreater(res['cache_equivalence_samples'], 0)

    def test_exact_placebo_run_count_enforced(self):
        cand, pool, rets = _null_case(np.random.default_rng(3), 0.0, (3, 10))
        with self.assertRaises(ValueError):
            aplc.evaluate_gate8_cached(cand, pool, rets, m_runs=P['placebo_runs'] - 1)

    def test_e2_null_acceptance_500_nulls_two_regimes(self):
        for rho, hold in ((0.0, (3, 10)), (0.3, (20, 40))):  # iid and heavy-overlap autocorrelated
            rng = np.random.default_rng(123)
            n_nulls = P['gate8_null_count']
            fp = 0
            for k in range(n_nulls):
                cand, pool, rets = _null_case(rng, rho, hold)
                fp += aplc.evaluate_gate8_cached(cand, pool, rets, seed=1000 + k)['passed']
            acc = aplc.null_acceptance(fp, n_nulls)
            self.assertTrue(acc['passed'], (rho, hold, acc))

    def test_clopper_pearson_upper_bound(self):
        self.assertAlmostEqual(aplc.binomial_upper_bound(0, 500), 1 - 0.05 ** (1 / 500), places=6)
        self.assertGreater(aplc.binomial_upper_bound(49, 500), 49 / 500)
        self.assertFalse(aplc.null_acceptance(70, 500)['passed'])
        self.assertFalse(aplc.null_acceptance(1, 100)['passed'])  # too few nulls


if __name__ == '__main__':
    unittest.main()
