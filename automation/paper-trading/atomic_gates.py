"""Atomic Gates 1-9, 250-Placebo with Spawn-Time Selection Process,
Incremental Superiority vs Track C, and Track C Probation.

Implements Section 4, A5, A8.6, B3, and B5 of the Definitive Coding-Ready Plan:
- Gate 1: Sample size + >= 10 new discordant trades (inherited counted separately) + distinct entry weeks
- Gate 2: Paired daily active return d_t = R_var,t - R_base,t (including cash), cost+slippage hurdle,
          t_HAC >= effective_critical_value(K_eff), and required residualized d_t check
- Gate 3: Guardrails (Max DD <= 1.10x baseline, single-trade contribution <= 35%)
- Gate 4: Benchmark alpha (binding when residualized check or beta stability requires it)
- Gate 5: Stationarity across evaluation sub-windows
- Gate 6: Parameter plateau (>= 75% retention on improvement over baseline; neighbors excluded from K_cand)
- Gate 7: Microstructure stress (1.5x/liquidity-scaled slippage, circuit carry-forward, gap-aware stops,
          indicator back-adjustment, EQ-only new entries, higher slippage on BE/BZ)
- Gate 8: Placebo M = 250 seeded runs; each run repeats exact spawn-time screening procedure across
          candidates and jitters ONLY discordant trades; must beat 90th percentile
- Gate 9: Minimum effect size
- Incremental superiority vs Track C: paired d_t test with stated threshold (e.g. >= +0.35 bps/day),
  counted as an eligible trial in K_cand
- Track C probation: longer for trend than fast swing; tolerance band calibrated to ~2*sigma of
  historical tracking error
"""
import math
from datetime import datetime, timezone
import numpy as np

import adaptive_engine as ae
import adaptive_logging as alog
import adaptive_placebo as aplc
import adaptive_policy as apol
import residualized_check as rc
import trial_counting as tc

AP = apol.POLICY
PLACEBO_RUNS_M = AP['placebo_runs']
PLACEBO_PERCENTILE = AP['placebo_percentile']
PLATEAU_RETENTION_MIN = AP['plateau_retention_min']
MAX_DD_MULTIPLIER = AP['max_dd_multiplier']
MAX_SINGLE_TRADE_CONTRIBUTION = AP['max_single_trade_share']
DEFAULT_TRACK_C_SUPERIORITY_BPS = AP['track_c_superiority_bps_per_day']  # plan example: +0.35 bps/day
PROBATION_SIGMA_MULTIPLIER = AP['probation_sigma_multiplier']
MICROSTRUCTURE_LOCKS = (
    'slippage_1_5x_liquidity_scaled', 'circuit_carry_forward', 'gap_aware_stops',
    'indicator_back_adjustment', 'eq_only_new_entries', 'higher_slippage_be_bz',
)
LIVE_TRACK_C_STATUSES = ('champion', 'probation')


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def compute_max_drawdown_from_returns(returns):
    """Compute maximum drawdown fraction from a 1D daily return series."""
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return 0.0
    wealth = np.cumprod(1.0 + r)
    peaks = np.maximum.accumulate(np.r_[1.0, wealth])[1:]
    dds = np.maximum(0.0, (peaks - wealth) / np.maximum(1e-12, peaks))
    return float(np.max(dds))


def evaluate_gate1_sample_and_discordant(
    var_trades,
    base_trades,
    warm_start_end_date,
    min_discordant_trades=ae.MIN_DISCORDANT_TRADES,
    min_distinct_weeks=ae.MIN_DISTINCT_ENTRY_WEEKS,
    min_total_trades=AP['gate1_min_total_trades'],
):
    """Gate 1: Sample size + >= 10 new discordant trades (inherited counted separately) + distinct entry weeks."""
    disc = ae.classify_discordant_trades(var_trades, base_trades, warm_start_end_date=warm_start_end_date)
    total_var_trades = len(var_trades)
    passed = (
        total_var_trades >= int(min_total_trades)
        and disc['new_discordant_count'] >= int(min_discordant_trades)
        and disc['distinct_new_entry_weeks'] >= int(min_distinct_weeks)
    )
    return {
        'passed': bool(passed),
        'total_variant_trades': total_var_trades,
        'min_total_trades': int(min_total_trades),
        'inherited_discordant_trades': disc['inherited_discordant_count'],
        'new_discordant_trades': disc['new_discordant_count'],
        'min_discordant_trades': int(min_discordant_trades),
        'distinct_entry_weeks': disc['distinct_new_entry_weeks'],
        'min_distinct_weeks': int(min_distinct_weeks),
    }


def evaluate_gate3_guardrails(
    r_var,
    r_base,
    var_trades,
    max_dd_multiplier=MAX_DD_MULTIPLIER,
    max_single_trade_share=MAX_SINGLE_TRADE_CONTRIBUTION,
    min_dd_baseline_floor=AP['gate3_min_dd_baseline_floor'],
):
    """Gate 3: Guardrails (DD <= 1.10x baseline DD, single-trade contribution <= 35%)."""
    dd_var = compute_max_drawdown_from_returns(r_var)
    dd_base = compute_max_drawdown_from_returns(r_base)
    dd_limit = float(max_dd_multiplier) * max(dd_base, float(min_dd_baseline_floor))
    dd_ok = dd_var <= dd_limit + 1e-12

    positive_pnls = [max(0.0, float(t.get('net_pnl_paise', 0))) for t in var_trades]
    total_pos = sum(positive_pnls)
    max_single = max(positive_pnls) if positive_pnls else 0.0
    single_share = (max_single / total_pos) if total_pos > 0 else 1.0
    single_ok = (len(positive_pnls) > 0) and (single_share <= float(max_single_trade_share) + 1e-12)

    return {
        'passed': bool(dd_ok and single_ok),
        'dd_var': dd_var,
        'dd_base': dd_base,
        'dd_limit': dd_limit,
        'dd_ok': bool(dd_ok),
        'max_single_trade_share': single_share,
        'max_single_trade_limit': float(max_single_trade_share),
        'single_trade_ok': bool(single_ok),
    }


def evaluate_gate5_stationarity(d_t):
    """Gate 5: Stationarity of active returns across first/second half and lag-1 autoregression (< 0.95)."""
    d = np.asarray(d_t, dtype=float)
    n = len(d)
    if n < AP['gate5_min_observations']:
        return {'passed': False, 'reason': 'insufficient_length_for_stationarity', 'n': n}
    mid = n // 2
    first_half_mean = float(np.mean(d[:mid]))
    second_half_mean = float(np.mean(d[mid:]))
    overall_mean = float(np.mean(d))

    # Lag-1 autocorrelation must be bounded away from unit root (< 0.95)
    centered = d - overall_mean
    denom = float(np.dot(centered, centered))
    rho1 = float(np.dot(centered[1:], centered[:-1]) / denom) if denom > 1e-16 else 0.0
    non_unit_root = abs(rho1) < AP['gate5_max_abs_lag1_autocorr']

    # Both halves must not collapse (neither half catastrophic relative to overall active edge)
    halves_consistent = (first_half_mean > 0.0 and second_half_mean > 0.0) or (
        overall_mean > 0.0 and min(first_half_mean, second_half_mean) >= AP['gate5_min_half_mean_vs_overall'] * abs(overall_mean)
    )
    passed = bool(overall_mean > 0.0 and non_unit_root and halves_consistent)
    return {
        'passed': passed,
        'first_half_mean': first_half_mean,
        'second_half_mean': second_half_mean,
        'lag1_autocorr': rho1,
        'non_unit_root': non_unit_root,
        'halves_consistent': halves_consistent,
    }


def evaluate_gate6_parameter_plateau(candidate_improvement, neighbor_improvements, min_retention=None):
    """Gate 6: Parameter plateau with the D5 rule from policy (plateau_aggregation: median retention >=
    plateau_retention_min and no neighbour below min_neighbour_improvement). Neighbours are PLATEAU_NEIGHBOR and
    excluded from K_cand. Use adaptive_placebo.evaluate_gate6_with_simulation() to generate and simulate them."""
    if min_retention is not None and float(min_retention) != PLATEAU_RETENTION_MIN:
        raise ValueError('gate6_retention_comes_from_policy')
    return aplc.aggregate_plateau(candidate_improvement, neighbor_improvements or [])


def evaluate_gate7_microstructure_stress(
    stressed_r_var,
    r_base,
    microstructure_checks=None,
):
    """Gate 7: Microstructure stress (1.5x/liquidity-scaled slippage, circuit carry-forward,
    gap-aware stops, indicator back-adjustment, EQ-only new entries, higher slippage on BE/BZ).
    """
    d_stress = rc.compute_paired_active_returns(stressed_r_var, r_base)
    stress_mean = float(np.mean(d_stress))
    # Each lock must be evidenced by the stressed simulation run (simulate_track_window()['microstructure_locks']).
    # A lock that is not reported is treated as NOT verified; nothing is assumed True.
    supplied = dict(microstructure_checks or {})
    checks = {k: bool(supplied.get(k, False)) for k in MICROSTRUCTURE_LOCKS}
    missing = [k for k in MICROSTRUCTURE_LOCKS if k not in supplied]
    all_locks_active = all(checks.values())
    passed = bool(stress_mean > 0.0 and all_locks_active)
    return {
        'passed': passed,
        'stressed_d_t_mean': stress_mean,
        'stressed_d_t_mean_bps': stress_mean * 10_000.0,
        'locks_verified': checks,
        'locks_missing_evidence': missing,
        'all_locks_active': all_locks_active,
    }


def evaluate_gate8_from_cache(gate8_cache, d_t, placebo_seed=AP['base_seed']):
    """Gate 8 for the final gate run: cached-pool re-screening (adaptive_placebo.evaluate_gate8_cached).

    gate8_cache = {'candidate_index': int, 'pool': [member caches], 'sym_returns': {symbol: daily returns}}.
    Missing cache, or a cached candidate d_t that differs from the gate's d_t, fails closed: the placebo must be run
    on the same trades that produced the evaluated returns.
    """
    if not gate8_cache:
        return {'passed': False, 'reason': 'gate8_requires_cached_pool_rescreening'}
    pool = gate8_cache['pool']
    cand = pool[int(gate8_cache['candidate_index'])]
    if len(cand['d_t']) != len(d_t) or not np.allclose(cand['d_t'], d_t, rtol=0.0, atol=1e-12):
        return {'passed': False, 'reason': 'gate8_cache_does_not_reproduce_candidate_d_t'}
    return aplc.evaluate_gate8_cached(int(gate8_cache['candidate_index']), pool, gate8_cache['sym_returns'], seed=placebo_seed,
                                      return_equivalence=True)


def evaluate_gate9_minimum_effect_size(d_t, min_effect_size_daily=AP['gate9_min_effect_size_daily']):
    """Gate 9: Minimum effect size (mean daily active return clears minimum actionable edge, e.g., >= 2.0 bps/day)."""
    mean_d = float(np.mean(np.asarray(d_t, dtype=float)))
    req = float(min_effect_size_daily)
    passed = mean_d >= req - 1e-12
    return {
        'passed': bool(passed),
        'd_t_mean': mean_d,
        'd_t_mean_bps': mean_d * 10_000.0,
        'min_effect_size_daily': req,
        'min_effect_size_bps': req * 10_000.0,
    }


def evaluate_incremental_superiority_vs_track_c(
    r_challenger,
    r_champion,
    stated_threshold_bps=DEFAULT_TRACK_C_SUPERIORITY_BPS,
    existing_candidate_records=None,
    challenger_spawn_id=None,
):
    """Evaluate incremental superiority of Track B challenger vs current Track C champion.

    - Uses paired d_t = R_challenger,t - R_champion,t against the stated threshold (default +0.35 bps/day).
    - Explicitly registers/counts this comparison as an eligible trial in K_cand.
    """
    d_c = rc.compute_paired_active_returns(r_challenger, r_champion)
    hac = rc.newey_west_t_hac(d_c)
    threshold_daily = float(stated_threshold_bps) / 10_000.0
    clears_threshold = hac['mean'] >= threshold_daily - 1e-12

    trial_record = {
        'spawn_id': f'track_c_cmp_{challenger_spawn_id or alog.compute_hash(list(map(float, d_c)))[:12]}',
        'candidate_kind': 'TRACK_C_COMPARISON',
        'eligible_for_selection': 1,
    }
    pool = list(existing_candidate_records or []) + [trial_record]
    kc_info = tc.count_k_cand(pool)

    return {
        'passed': bool(clears_threshold and hac['t_hac'] > 0.0),
        'mean_active_return_vs_track_c': hac['mean'],
        'mean_active_return_bps': hac['mean'] * 10_000.0,
        'stated_threshold_bps': float(stated_threshold_bps),
        't_hac_vs_track_c': hac['t_hac'],
        'counted_as_trial': True,
        'trial_record': trial_record,
        'updated_k_cand': kc_info['k_cand'],
    }


def evaluate_track_c_probation(
    db,
    namespace,
    spawn_id,
    strategy,
    realized_d_t,
    historical_d_t_for_sigma,
    sigma_multiplier=PROBATION_SIGMA_MULTIPLIER,
    probation_id=None,
):
    """Evaluate Track C probation: longer window for Trend than Fast Swing; tolerance band ~2*sigma of tracking error."""
    s = int(strategy)
    tier = ae.strategy_tier(s)
    required_days = AP['probation_days_trend'] if tier == 'TREND' else AP['probation_days_fast_swing']

    hist = np.asarray(historical_d_t_for_sigma, dtype=float)
    realized = np.asarray(realized_d_t, dtype=float)
    if len(hist) < 2:
        # No silent default tracking error: the tolerance band must be calibrated on real history.
        raise ValueError('probation_requires_at_least_2_historical_d_t_observations')
    hist_te = float(np.std(hist, ddof=1))
    days_monitored = len(realized)
    cum_active = float(np.sum(realized)) if days_monitored > 0 else 0.0
    mean_active = float(np.mean(realized)) if days_monitored > 0 else 0.0

    # 2*sigma tolerance band on daily mean tracking error over days_monitored (or single-day floor)
    effective_n = max(1, days_monitored)
    tolerance_band = float(sigma_multiplier) * hist_te / math.sqrt(effective_n)
    within_band = mean_active >= -tolerance_band

    if not within_band:
        status = 'FAILED_PROBATION'
    elif days_monitored >= required_days:
        status = 'PASSED_PROBATION'
    else:
        status = 'IN_PROBATION'

    pid = str(probation_id or f'prob_{spawn_id}')
    stamp = _utc_now()
    db.batch([(
        'INSERT OR REPLACE INTO adaptive_probation('
        'namespace,probation_id,spawn_id,strategy,tolerance_band_sigma,historical_tracking_error,'
        'tracking_error_limit,days_monitored,required_days,cumulative_active_return,status,meta_kill_triggered,'
        'details_json,updated_at'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            namespace, pid, str(spawn_id), s, float(sigma_multiplier), hist_te,
            -tolerance_band, days_monitored, required_days, cum_active,
            status, 0,
            alog.stable_json({'tier': tier, 'mean_active_return': mean_active, 'within_2sigma_band': within_band}),
            stamp,
        ),
    )])

    alog.append_log(
        db, namespace, correlation_id=str(spawn_id), spawn_id=str(spawn_id),
        track='C', strategy=s, category='PROBATION',
        decision_type='PROBATION_PASS' if status == 'PASSED_PROBATION' else ('PROBATION_FAIL' if status == 'FAILED_PROBATION' else 'PROBATION_START'),
        named_cause=status.lower(),
        rule_fired='track_c_probation_2sigma_tracking_error_band',
        conditions={
            'tier': tier,
            'days_monitored': days_monitored,
            'required_days': required_days,
            'historical_tracking_error': hist_te,
            'tolerance_band_2sigma': tolerance_band,
            'mean_active_return': mean_active,
            'within_band': within_band,
        },
        source_at=stamp,
    )
    return {
        'probation_id': pid,
        'spawn_id': str(spawn_id),
        'strategy': s,
        'tier': tier,
        'required_days': required_days,
        'days_monitored': days_monitored,
        'historical_tracking_error': hist_te,
        'tolerance_band': tolerance_band,
        'mean_active_return': mean_active,
        'cumulative_active_return': cum_active,
        'within_band': within_band,
        'status': status,
    }


def evaluate_all_atomic_gates(
    db,
    namespace,
    spawn_id,
    strategy,
    decision_day,
    r_var,
    r_base,
    index_returns,
    var_trades,
    base_trades,
    warm_start_end_date,
    neighbor_improvements,
    stressed_r_var,
    k_cand=1,
    rho_bar=0.0,
    r_track_c=None,
    track_c_threshold_bps=DEFAULT_TRACK_C_SUPERIORITY_BPS,
    cost_slippage_hurdle=AP['gate2_cost_slippage_hurdle_daily'],
    min_effect_size_daily=AP['gate9_min_effect_size_daily'],
    placebo_seed=AP['base_seed'],
    observation_only=True,
    microstructure_checks=None,
    gate8_cache=None,
):
    """Evaluate all 9 Atomic Gates (plus incremental superiority vs Track C when Track C exists) on the final decision day.

    Gates run once: the spawn must have been released by evaluate_stopping_and_window() (status EVALUATING) and must
    not already have a gate evaluation. If a live Track C champion exists for the strategy, r_track_c is mandatory and
    the comparison is counted as an extra eligible trial in K_cand before Gate 2's effective critical value is computed.
    """
    sp_rows = db.query('SELECT track,status FROM adaptive_spawns WHERE namespace=? AND spawn_id=?', (namespace, str(spawn_id)))
    if not sp_rows:
        raise KeyError(f'spawn_not_found:{spawn_id}')
    if sp_rows[0]['status'] != 'EVALUATING':
        raise PermissionError(f"gates_require_completed_window_status_EVALUATING_got_{sp_rows[0]['status']}")
    if db.query('SELECT 1 AS x FROM adaptive_gate_evaluations WHERE namespace=? AND spawn_id=? LIMIT 1', (namespace, str(spawn_id))):
        raise PermissionError(f'gates_already_evaluated_once:{spawn_id}')
    spawn_track = sp_rows[0]['track']
    live_champion = db.query(
        f"SELECT spawn_id FROM adaptive_tracks WHERE namespace=? AND track='C' AND strategy=? AND status IN ({','.join('?' * len(LIVE_TRACK_C_STATUSES))}) AND spawn_id<>?",
        (namespace, int(strategy), *LIVE_TRACK_C_STATUSES, str(spawn_id)),
    )
    if live_champion and r_track_c is None:
        raise ValueError(f"track_c_champion_exists_incremental_superiority_required:{live_champion[0]['spawn_id']}")
    # C-11: Gate 2 reads the cumulative K_cand across spawn rounds; a smaller caller value can never lower it.
    k_cand_cumulative = tc.cumulative_k_cand(db, namespace, int(strategy))['k_cand']
    k_cand_base = max(int(k_cand), int(k_cand_cumulative))
    k_cand_for_gate2 = k_cand_base + (1 if r_track_c is not None else 0)

    d_t = rc.compute_paired_active_returns(r_var, r_base)

    g1 = evaluate_gate1_sample_and_discordant(var_trades, base_trades, warm_start_end_date=warm_start_end_date)
    g2_g4 = rc.evaluate_gate2_and_gate4(
        r_var=r_var,
        r_base=r_base,
        index_returns=index_returns,
        k_cand=k_cand_for_gate2,
        rho_bar=rho_bar,
        cost_slippage_hurdle=cost_slippage_hurdle,
    )
    g3 = evaluate_gate3_guardrails(r_var, r_base, var_trades)
    g5 = evaluate_gate5_stationarity(d_t)
    g6 = evaluate_gate6_parameter_plateau(
        candidate_improvement=g2_g4['d_t_mean'],
        neighbor_improvements=neighbor_improvements,
    )
    g7 = evaluate_gate7_microstructure_stress(stressed_r_var=stressed_r_var, r_base=r_base, microstructure_checks=microstructure_checks)
    g8 = evaluate_gate8_from_cache(gate8_cache, d_t, placebo_seed)
    g9 = evaluate_gate9_minimum_effect_size(d_t, min_effect_size_daily=min_effect_size_daily)

    track_c_cmp = None
    track_c_ok = True
    if r_track_c is not None:
        track_c_cmp = evaluate_incremental_superiority_vs_track_c(
            r_challenger=r_var,
            r_champion=r_track_c,
            stated_threshold_bps=track_c_threshold_bps,
            challenger_spawn_id=str(spawn_id),
        )
        track_c_ok = track_c_cmp['passed']

    gates_summary = {
        'gate_1_sample_and_discordant': g1['passed'],
        'gate_2_active_return_and_residualized': g2_g4['gate2_passed'],
        'gate_3_guardrails': g3['passed'],
        'gate_4_benchmark_alpha': g2_g4['gate4_passed'],
        'gate_5_stationarity': g5['passed'],
        'gate_6_parameter_plateau': g6['passed'],
        'gate_7_microstructure_stress': g7['passed'],
        'gate_8_placebo_250': g8['passed'],
        'gate_9_min_effect_size': g9['passed'],
        'incremental_superiority_vs_track_c': track_c_ok,
    }
    overall_passed = all(gates_summary.values())
    status = ('OBSERVATION_ONLY' if observation_only else 'PASSED') if overall_passed else 'FAILED'
    eval_id = f'eval_{spawn_id}_{decision_day}'
    stamp = _utc_now()

    full_details = {
        'summary': gates_summary,
        'gate_1': g1,
        'gate_2_and_4': g2_g4,
        'gate_3': g3,
        'gate_5': g5,
        'gate_6': g6,
        'gate_7': g7,
        'gate_8': g8,
        'gate_9': g9,
        'track_c_comparison': track_c_cmp,
        'track_c_champion_present': bool(live_champion),
        'k_cand_supplied': int(k_cand),
        'k_cand_cumulative_from_spawns': int(k_cand_cumulative),
        'k_cand_including_track_c_comparison': k_cand_for_gate2,
    }
    dhash = alog.compute_hash(full_details)

    db.batch([
        (
            'INSERT OR REPLACE INTO adaptive_gate_evaluations('
            'namespace,eval_id,spawn_id,correlation_id,strategy,decision_day,k_cand,rho_bar,k_eff,'
            'effective_critical_value,t_hac,d_t_mean,residual_alpha,residual_beta,beta_stable,gate4_binding,'
            'gates_json,overall_passed,status,observation_only,data_hash,recorded_at'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                namespace, eval_id, str(spawn_id), str(spawn_id), int(strategy), str(decision_day),
                g2_g4['k_cand'], g2_g4['rho_bar'], g2_g4['k_eff'], g2_g4['effective_critical_value'],
                g2_g4['t_hac'], g2_g4['d_t_mean'],
                g2_g4['residualized_check']['alpha'], g2_g4['residualized_check']['beta'],
                int(g2_g4['residualized_check']['beta_stable']), int(g2_g4['gate4_binding']),
                alog.stable_json(full_details), int(overall_passed), status, int(bool(observation_only)),
                dhash, stamp,
            ),
        ),
        (
            'UPDATE adaptive_spawns SET k_cand=?,rho_bar=?,k_eff=?,effective_critical_value=?,inherited_discordant_trades=?,new_discordant_trades=?,distinct_entry_weeks=?,status=?,decided_at=? WHERE namespace=? AND spawn_id=?',
            (
                g2_g4['k_cand'], g2_g4['rho_bar'], g2_g4['k_eff'], g2_g4['effective_critical_value'],
                g1['inherited_discordant_trades'], g1['new_discordant_trades'], g1['distinct_entry_weeks'],
                status, stamp, namespace, str(spawn_id),
            ),
        ),
    ])

    alog.append_log(
        db, namespace, correlation_id=str(spawn_id), spawn_id=str(spawn_id),
        track=spawn_track, strategy=int(strategy),
        category='GATE_EVALUATION',
        decision_type='GATE_PASS' if overall_passed else 'GATE_FAIL',
        named_cause='all_atomic_gates_passed' if overall_passed else 'atomic_gate_failed',
        rule_fired='atomic_gates_1_to_9_final_decision_day',
        conditions={
            'decision_day': str(decision_day),
            'gates': gates_summary,
            'k_eff': g2_g4['k_eff'],
            'effective_critical_value': g2_g4['effective_critical_value'],
            't_hac': g2_g4['t_hac'],
            'residual_alpha': g2_g4['residualized_check']['alpha'],
        },
        execution_details={
            'eval_id': eval_id,
            'overall_passed': overall_passed,
            'status': status,
            'observation_only': bool(observation_only),
            'placebo_threshold_t': g8.get('placebo_threshold_t'),
        },
        input_data=full_details,
        source_at=stamp,
    )
    return {
        'eval_id': eval_id,
        'spawn_id': str(spawn_id),
        'overall_passed': overall_passed,
        'status': status,
        'gates_summary': gates_summary,
        'details': full_details,
        'data_hash': dhash,
    }
