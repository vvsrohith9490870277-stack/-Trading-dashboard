"""Independent audit regressions for the adaptive (Track B/C) engine (2026-11-09).

Each test pins a defect found in 82dfdd4..4a6c382 so it cannot come back:
- cost model copied magic fee numbers (wrong DP, no stamp duty, sell charges on buys) instead of the frozen tables
- gates could be run before the window finished, more than once, and Gate 7 rubber-stamped its locks
- a live Track C champion could be skipped and the Track C comparison was not counted in Gate 2's K_cand
- INCONCLUSIVE could be re-opened; Track B slot reuse overwrote a live challenger row; fractional integer params
- missing PIT Bhavcopy SERIES silently became EQ
- simulate_track_window (the engine core) had no test at all
"""
import copy, json, subprocess, sys, unittest
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_engine as ae
import adaptive_policy as apol
import adaptive_promotion as ap
import atomic_gates as ag
import calibration as cal
import capital as cap
from adaptive_fixtures import calibration_partitions, fresh_db, gate8_cache_from_series, make_market, seed_pit
from test_atomic_gates import _build_trades

LOCKS = dict.fromkeys(ag.MICROSTRUCTURE_LOCKS, True)


def _node_fees(expr):
    out = subprocess.run(['node', '--input-type=module', '-e', f"import {{estimate,executionSlippage}} from './fees.mjs';console.log(JSON.stringify({expr}))"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _passing_series(seed=12345, n=60):
    rng = np.random.default_rng(seed)
    r_idx = rng.normal(0.0004, 0.006, size=n)
    r_base = 0.7 * r_idx + rng.normal(0.0001, 0.002, size=n)
    edge = np.zeros(n)
    days = np.arange(0, n, 2)
    edge[days] = 0.0018 + rng.normal(0.0, 0.0004, size=len(days))
    return r_idx, r_base, r_base + edge, r_base + 0.75 * edge, edge


class AdaptiveAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db(ROOT)
        self.ns = 'audit'

    def _gates(self, sid, strategy=3, **kw):
        r_idx, r_base, r_var, stressed, _ = _passing_series()
        var_trades, base_trades = _build_trades(n_new=12, n_inherited=3)
        mean_d = float(np.mean(r_var - r_base))
        args = dict(r_var=r_var, r_base=r_base, index_returns=r_idx, var_trades=var_trades, base_trades=base_trades,
                    warm_start_end_date='2025-05-30', neighbor_improvements=[mean_d * 0.85, mean_d * 0.82],
                    stressed_r_var=stressed, k_cand=2, rho_bar=0.4, observation_only=True, microstructure_checks=LOCKS)
        args.update(kw)
        args.setdefault('gate8_cache', gate8_cache_from_series(args['r_var'], args['r_base']))
        return ag.evaluate_all_atomic_gates(self.db, self.ns, spawn_id=sid, strategy=strategy, decision_day='2025-08-29', **args)

    def _release(self, sid):
        r = ae.evaluate_stopping_and_window(self.db, self.ns, sid, days_elapsed=45, new_discordant_trades=12, distinct_entry_weeks=6)
        self.assertTrue(r['ready_for_final_gate_evaluation'])

    # ---- cost model parity with the frozen Track A tables --------------------------------------------------------
    def test_fill_charges_and_slippage_match_frozen_fees_mjs_exactly(self):
        for turnover in (4_987_650, 123_456, 19_999_999, 50_000_00):
            js = _node_fees(f"[estimate({{turnover:{turnover}n,side:'BUY'}}),estimate({{turnover:{turnover}n,side:'SELL',dp:true}})]")
            self.assertEqual(ae.fill_charges_paise(turnover, 'BUY'), js[0])
            self.assertEqual(ae.fill_charges_paise(turnover, 'SELL', overnight_dp=True), js[1])
        self.assertEqual(ae.fill_charges_paise(4_987_650, 'BUY')['dp'], 0)
        self.assertGreater(ae.fill_charges_paise(4_987_650, 'BUY')['stamp'], 0)
        for adv, side, stress in ((2_500_000_000, 'BUY', False), (25_000_000_000, 'SELL', True), (2_000_000_000_0, 'SELL', False)):
            js = _node_fees(f"executionSlippage({{advPaise:{adv},orderPaise:4987650,referencePaise:99753,side:'{side}',stress:{str(stress).lower()}}})")
            self.assertEqual(ae.slippage_fill_paise(99753, 4_987_650, adv, side, stress)['price_paise'], js['price_paise'])

    def test_adaptive_sources_carry_no_copied_fee_or_policy_literals(self):
        src = (ROOT / 'adaptive_engine.py').read_text()
        for literal in ('1475 + 266', '0.0000345', '200_000_000', '* 0.003', '* 0.001', '0.18)'):
            self.assertNotIn(literal, src)
        self.assertEqual(ae.BASELINE_PARAMS[7]['trail_ratio'], float(ae.ENGINE_CONFIG['strategy_07_trail_fraction']))
        self.assertEqual(ag.PLACEBO_RUNS_M, apol.POLICY['placebo_runs'])
        self.assertEqual(cal.POWER_FLOOR, apol.POLICY['power_floor'])

    def test_policy_loader_fails_closed_on_bad_or_unlabelled_values(self):
        good = json.loads(apol.POLICY_PATH.read_text())
        import tempfile
        for mutate in (lambda p: p.update(placebo_runs=0), lambda p: p.update(alpha=0.7),
                       lambda p: p['provenance']['plan'].remove('placebo_runs'), lambda p: p.update(trend_strategies=[1, 2, 4, 6])):
            bad = copy.deepcopy(good)
            mutate(bad)
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(bad, f)
            with self.assertRaises(RuntimeError):
                apol.load(f.name)

    def test_harness_hash_covers_gate_code_and_policy(self):
        for name in ('atomic_gates.py', 'adaptive_policy.json', 'calibration.py', 'freeze_lock.json', '../../server/automation-read.mjs'):
            self.assertIn(name, ae.HARNESS_FILES)

    # ---- gates once, on the final decision day ---------------------------------------------------------------------
    def test_gates_refuse_to_run_before_window_completes_and_only_once(self):
        sid = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 36.0}, track='B')['spawn_id']
        with self.assertRaises(PermissionError):
            self._gates(sid)
        early = ae.evaluate_stopping_and_window(self.db, self.ns, sid, days_elapsed=30, new_discordant_trades=15, distinct_entry_weeks=5)
        self.assertFalse(early['ready_for_final_gate_evaluation'])
        with self.assertRaises(PermissionError):
            self._gates(sid)
        self._release(sid)
        self.assertTrue(self._gates(sid)['overall_passed'])
        with self.assertRaises(PermissionError):
            self._gates(sid)

    def test_inconclusive_is_terminal(self):
        sid = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 36.0}, track='B')['spawn_id']
        ae.evaluate_stopping_and_window(self.db, self.ns, sid, days_elapsed=45, new_discordant_trades=4, distinct_entry_weeks=1)
        inc = ae.evaluate_stopping_and_window(self.db, self.ns, sid, days_elapsed=60, new_discordant_trades=4, distinct_entry_weeks=1)
        self.assertEqual(inc['status'], 'INCONCLUSIVE')
        again = ae.evaluate_stopping_and_window(self.db, self.ns, sid, days_elapsed=70, new_discordant_trades=40, distinct_entry_weeks=9)
        self.assertEqual(again['status'], 'INCONCLUSIVE')
        self.assertFalse(again['ready_for_final_gate_evaluation'])

    def test_gate7_requires_lock_evidence(self):
        _, r_base, _, stressed, _ = _passing_series()
        self.assertFalse(ag.evaluate_gate7_microstructure_stress(stressed, r_base)['passed'])
        partial = dict(LOCKS, higher_slippage_be_bz=False)
        self.assertFalse(ag.evaluate_gate7_microstructure_stress(stressed, r_base, partial)['passed'])
        self.assertTrue(ag.evaluate_gate7_microstructure_stress(stressed, r_base, LOCKS)['passed'])

    # ---- Track C -------------------------------------------------------------------------------------------------
    def test_live_champion_forces_superiority_test_and_counts_it_in_k_cand(self):
        champ = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 38.0}, track='C', candidate_kind='TRACK_C_COMPARISON')
        sid = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 36.0}, track='B')['spawn_id']
        self._release(sid)
        with self.assertRaises(ValueError):
            self._gates(sid)
        _, r_base, _, _, edge = _passing_series()
        res = self._gates(sid, r_track_c=r_base + 0.30 * edge)
        self.assertEqual(res['details']['k_cand_including_track_c_comparison'], 3)
        self.assertEqual(res['details']['gate_2_and_4']['k_cand'], 3)
        self.assertTrue(res['details']['track_c_champion_present'])
        self.assertNotEqual(champ['spawn_id'], sid)

    def test_promotion_blocked_when_champion_appeared_after_gate_run(self):
        sid = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 36.0}, track='B')['spawn_id']
        self._release(sid)
        self.assertTrue(self._gates(sid)['overall_passed'])
        ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 38.0}, track='C', candidate_kind='TRACK_C_COMPARISON')
        res = ap.check_promotion_prerequisites(self.db, self.ns, 3, sid, observation_only=False)
        self.assertIn('incremental_superiority_vs_current_track_c_not_tested', res['blockers'])
        self.assertFalse(res['can_promote'])

    def test_track_b_slot_reuse_never_overwrites_live_challenger(self):
        a = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 36.0}, track='B', spawned_at='2025-01-01T00:00:00+00:00')
        b = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 37.0}, track='B', spawned_at='2025-01-02T00:00:00+00:00')
        self.db.batch([("UPDATE adaptive_tracks SET status='retired' WHERE namespace=? AND spawn_id=?", (self.ns, a['spawn_id']))])
        c = ae.spawn_candidate(self.db, self.ns, strategy=3, params={'rsi_entry': 38.0}, track='B', spawned_at='2025-01-03T00:00:00+00:00')
        live = {r['spawn_id'] for r in self.db.query("SELECT spawn_id FROM adaptive_tracks WHERE namespace=? AND track='B' AND status='challenger'", (self.ns,))}
        self.assertEqual(live, {b['spawn_id'], c['spawn_id']})

    def test_integer_parameters_reject_fractional_values(self):
        with self.assertRaises(ValueError):
            ae.validate_parameter_only_mutation(1, {'entry_lookback': 20.5})
        self.assertEqual(ae.validate_parameter_only_mutation(1, {'entry_lookback': 22.0})['entry_lookback'], 22)

    def test_probation_and_tuning_refuse_to_invent_sigma(self):
        with self.assertRaises(ValueError):
            ag.evaluate_track_c_probation(self.db, self.ns, 'x', 3, realized_d_t=[0.001] * 5, historical_d_t_for_sigma=[0.001])
        with self.assertRaises(ValueError):
            parts, old, _, _ = calibration_partitions(5)
            cal.tune_thresholds_on_partition(parts, old[:1], [0.1], [0.001])

    def test_calibration_rerun_cannot_reset_spent_vault(self):
        rng = np.random.default_rng(3)
        parts, old, _, hold = calibration_partitions(120)
        run = cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.0008, 120), 0.0005, old, parts, window_days=45, calibration_id='cal_fixed')
        cal.touch_holdout_vault_once(self.db, self.ns, 'vault1', run['calibration_id'], holdout_passed=False, holdout_dates=hold)
        with self.assertRaises(PermissionError):
            cal.run_calibration_go_no_go(self.db, self.ns, 3, rng.normal(0, 0.0008, 120), 0.0005, old, parts, window_days=45, calibration_id='cal_fixed')

    # ---- challenger code path == frozen Track A indicators -------------------------------------------------------
    def test_challenger_code_path_with_baseline_values_matches_frozen_indicators(self):
        orig = copy.deepcopy(ae.BASELINE_PARAMS)
        try:
            for seed in range(6):
                _, df, _ = make_market(n_days=420, symbols=('AAA-EQ',), seed=seed)
                for s in range(1, 8):
                    frozen = ae.evaluate_parameterized_strategy(s, df, {})
                    ae.BASELINE_PARAMS[s] = {k: (v * 2 if not isinstance(v, int) else v + 1) for k, v in orig[s].items()}
                    challenger_path = ae.evaluate_parameterized_strategy(s, df, dict(orig[s]))
                    ae.BASELINE_PARAMS[s] = orig[s]
                    for col in ('entry_condition', 'exit_condition'):
                        a = frozen[col].fillna(False).astype(bool).to_numpy()
                        b = challenger_path[col].fillna(False).astype(bool).to_numpy()
                        self.assertTrue((a == b).all(), f'strategy {s} {col} seed {seed}')
        finally:
            ae.BASELINE_PARAMS.update(orig)

    # ---- the simulator itself ------------------------------------------------------------------------------------
    def _sim(self, series='EQ', volume=300_000, params=None):
        days, df, idx = make_market()
        syms = sorted(df.symbol.unique())
        seed_pit(self.db, self.ns, days, syms, series=series)
        return ae.simulate_track_window(self.db, self.ns, 3, params or {}, df, idx, days[-105:-45], days[-45:]), df

    def test_simulation_fills_next_open_with_frozen_costs_and_reports_locks(self):
        res, df = self._sim()
        self.assertGreater(len(res['closed_trades']), 0)
        self.assertEqual(len(res['daily_records']), 45)
        self.assertEqual(res['microstructure_locks'].keys(), set(ag.MICROSTRUCTURE_LOCKS))
        for t in res['closed_trades']:
            self.assertGreater(t['exit_date'], t['entry_date'])  # signal at close t -> fill at a later open
            bar = df[(df.symbol == t['symbol']) & (df.trade_date == t['entry_date'])].iloc[0]
            self.assertGreaterEqual(t['entry_price'], float(bar['open']))  # BUY slippage never improves the price
            self.assertTrue(t['exit_slippage_applied'])
        e0 = res['starting_equity']
        self.assertEqual(e0['starting_equity_net_haircut_paise'], e0['starting_equity_gross_paise'] - e0['exit_haircut_paise'])

    def test_unknown_pit_series_blocks_new_entries(self):
        res, _ = self._sim(series=None)
        self.assertEqual(res['closed_trades'], [])
        self.assertEqual(res['open_positions'], [])
        self.assertTrue(all(s['cause'] == 'PIT_SERIES_UNKNOWN' for s in res['entry_skips']))
        self.assertEqual(ae.pit.get_pit_prefilter_universe(self.db, self.ns, res['daily_records'][0]['trade_date'], require_eq_series=True)['eligible_symbols'], [])

    def test_adv_floor_from_frozen_capital_policy_blocks_illiquid_entries(self):
        days, df, idx = make_market(volume=10)
        syms = sorted(df.symbol.unique())
        seed_pit(self.db, self.ns, days, syms)
        res = ae.simulate_track_window(self.db, self.ns, 3, {}, df, idx, days[-105:-45], days[-45:])
        self.assertEqual(res['closed_trades'], [])
        self.assertTrue(res['entry_skips'])
        self.assertTrue(all(s['cause'] == cap.POLICY['adv_filter_cause'] for s in res['entry_skips']))


if __name__ == '__main__':
    unittest.main()
