"""Calibration Harness (MDE, Injected Edge, ~30% Go/No-Go Power Floor, Oldest-Partition Tuning,
One-Touch Holdout Vault, Meta-Kill Switch) and Automated Look-Ahead Truncation Test (Price + Corporate Actions).

Implements Section 5, Section 6, A6, A7, A8.7, B1, and B4 of the Definitive Coding-Ready Plan.
"""
import json, math, uuid
from datetime import datetime, timezone
from statistics import NormalDist
import numpy as np
import pandas as pd

import adaptive_engine as ae
import adaptive_logging as alog
import adaptive_policy as apol
import pit_archive as pit
import prefilter as pf
import residualized_check as rc
import trial_counting as tc

AP = apol.POLICY
POWER_FLOOR = AP['power_floor']
OLDEST_PARTITION_NAME = 'OLDEST_CALIBRATION_PARTITION'


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---- C-09 / E7: autocorrelation-aware sigma, block-bootstrap cross-check, date-enforced partitions ---------------

def hac_sigma_with_sensitivity(real_d_t):
    """Long-run (Newey-West HAC) sigma of d_t at each hac_bandwidth_grid multiplier of the automatic lag.

    The conservative (largest) sigma is used for MDE/power; the spread is reported as bandwidth sensitivity.
    """
    d = np.asarray(real_d_t, dtype=float)
    if len(d) < 2:
        raise ValueError('real_d_t_requires_at_least_2_observations')
    rows = []
    for m in AP['hac_bandwidth_grid']:
        h = rc.newey_west_t_hac(d, bandwidth_multiplier=m)
        rows.append({'bandwidth_multiplier': float(m), 'max_lag': int(h['max_lag']), 'sigma': float(h['long_run_sigma'])})
    sigmas = [r['sigma'] for r in rows]
    hi, lo = max(sigmas), min(sigmas)
    return {
        'sigma_hac': hi,
        'sigma_iid': float(np.std(d, ddof=1)),
        'bandwidth_sensitivity': rows,
        'bandwidth_relative_spread': ((hi - lo) / hi) if hi > 0 else 0.0,
    }


def _circular_block_indices(n, block, n_draws, rng):
    b = max(1, min(int(block), n))
    n_blocks = int(math.ceil(n / float(b)))
    starts = rng.integers(0, n, size=(int(n_draws), n_blocks))
    return ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(int(n_draws), -1)[:, :n]


def block_bootstrap_sigma(real_d_t, block_size=AP['bootstrap_block_size'], n_boot=AP['bootstrap_n_boot'], seed=AP['base_seed']):
    """Long-run sigma implied by the circular block bootstrap: sd(bootstrap means) * sqrt(n)."""
    d = np.asarray(real_d_t, dtype=float)
    n = len(d)
    if n < 2:
        raise ValueError('real_d_t_requires_at_least_2_observations')
    idx = _circular_block_indices(n, block_size, n_boot, np.random.default_rng(int(seed)))
    means = d[idx].mean(axis=1)
    return {'sigma_bootstrap': float(np.std(means, ddof=1) * math.sqrt(n)), 'block_size': int(block_size), 'n_boot': int(n_boot)}


def cross_check_sigma(real_d_t, seed=AP['base_seed']):
    """HAC vs block bootstrap. Relative disagreement above hac_bootstrap_max_disagreement => fail closed (NO-GO)."""
    hac = hac_sigma_with_sensitivity(real_d_t)
    boot = block_bootstrap_sigma(real_d_t, seed=seed)
    a, b = hac['sigma_hac'], boot['sigma_bootstrap']
    ref = max(a, b)
    disagreement = abs(a - b) / ref if ref > 0 else 0.0
    return {
        **hac, **boot,
        'relative_disagreement': disagreement,
        'max_disagreement': AP['hac_bootstrap_max_disagreement'],
        'agree': disagreement <= AP['hac_bootstrap_max_disagreement'],
        'sigma_used': ref,  # conservative: the larger of the two estimates
    }


def validate_partitions(partitions):
    """Partitions are explicit date ranges [{'name','start','end'}], ordered oldest -> newest, non-overlapping.

    The oldest is the only tuning partition; the newest is the holdout vault.
    """
    if not partitions or len(partitions) < 2:
        raise ValueError('partitions_require_at_least_tuning_and_holdout')
    rows = []
    for p in partitions:
        st, en = str(p['start'])[:10], str(p['end'])[:10]
        pd.Timestamp(st), pd.Timestamp(en)
        if st > en:
            raise ValueError(f"partition_start_after_end:{p['name']}")
        rows.append({'name': str(p['name']), 'start': st, 'end': en})
    ordered = sorted(rows, key=lambda r: r['start'])
    if [r['name'] for r in ordered] != [r['name'] for r in rows]:
        raise ValueError('partitions_must_be_listed_oldest_first')
    for a, b in zip(ordered, ordered[1:]):
        if b['start'] <= a['end']:
            raise ValueError(f"partitions_overlap:{a['name']}:{b['name']}")
    return ordered


def assert_dates_in_partition(dates, partition, label):
    ds = [str(d)[:10] for d in dates]
    if not ds:
        raise ValueError(f'{label}_requires_dates')
    outside = [d for d in ds if not partition['start'] <= d <= partition['end']]
    if outside:
        raise PermissionError(f"{label}_dates_outside_{partition['name']}:{outside[0]}")
    return True


def compute_mde_from_real_d_t(real_d_t, window_days, k_eff=1.0, alpha=tc.DEFAULT_ALPHA, target_power=POWER_FLOOR, seed=AP['base_seed']):
    """MDE from the autocorrelation-aware (conservative HAC / block-bootstrap) sigma of real d_t."""
    xc = cross_check_sigma(real_d_t, seed=seed)
    sigma = xc['sigma_used']
    n = max(1, int(window_days))
    crit = tc.compute_effective_critical_value(k_eff=k_eff, alpha=alpha)['effective_critical_value']
    z_power = float(NormalDist(0.0, 1.0).inv_cdf(float(target_power)))
    mde = max(0.0, (crit + z_power) * sigma / math.sqrt(n))
    return {
        'sigma_d_t': sigma,
        'sigma_d_t_bps': sigma * 10_000.0,
        'sigma_cross_check': xc,
        'window_days': n,
        'k_eff': float(k_eff),
        'effective_critical_value': crit,
        'target_power': float(target_power),
        'mde_daily': mde,
        'mde_bps': mde * 10_000.0,
    }


def estimate_power_at_injected_edge(
    real_d_t,
    smallest_actionable_edge,
    window_days,
    k_eff=1.0,
    alpha=tc.DEFAULT_ALPHA,
    n_sims=AP['calibration_n_sims'],
    seed=AP['base_seed'],
):
    """Power at the injected edge: analytic (HAC sigma) and empirical (circular block bootstrap of centred real d_t,
    HAC t-stat per draw). Measured power is the minimum of the two."""
    edge = float(smallest_actionable_edge)
    if edge <= 0.0:
        raise ValueError('smallest_actionable_edge_must_be_positive')
    mde_info = compute_mde_from_real_d_t(real_d_t, window_days=window_days, k_eff=k_eff, alpha=alpha, seed=seed)
    sigma = mde_info['sigma_d_t']
    crit = mde_info['effective_critical_value']
    n = mde_info['window_days']
    nd = NormalDist(0.0, 1.0)
    analytical_power = 1.0 if sigma <= 1e-15 else float(1.0 - nd.cdf(crit - edge * math.sqrt(n) / sigma))

    d = np.asarray(real_d_t, dtype=float)
    centered = d - np.mean(d)
    rng = np.random.default_rng(int(seed))
    sims = max(AP['calibration_min_sims'], int(n_sims))
    idx = _circular_block_indices(len(centered), AP['bootstrap_block_size'], sims, rng)
    if idx.shape[1] < n:  # window longer than history: tile blocks to the window length
        reps = int(math.ceil(n / float(idx.shape[1])))
        idx = np.concatenate([_circular_block_indices(len(centered), AP['bootstrap_block_size'], sims, rng) for _ in range(reps)], axis=1)
    idx = idx[:, :n]
    detections = sum(1 for row in idx if rc.newey_west_t_hac(centered[row] + edge)['t_hac'] >= crit)
    empirical_power = detections / float(sims)
    return {
        **mde_info,
        'injected_edge': edge,
        'injected_edge_bps': edge * 10_000.0,
        'analytical_power': analytical_power,
        'empirical_power': empirical_power,
        'empirical_method': 'circular_block_bootstrap_hac_t',
        'measured_power': min(analytical_power, empirical_power),
    }


def tune_thresholds_on_partition(partitions, d_t_dates, candidate_thresholds, real_d_t):
    """Threshold tuning ONLY on the oldest partition, enforced by the dates of the d_t used."""
    ordered = validate_partitions(partitions)
    oldest = ordered[0]
    assert_dates_in_partition(d_t_dates, oldest, 'threshold_tuning')
    d = np.asarray(real_d_t, dtype=float)
    if len(d) < 2 or len(d) != len(d_t_dates):
        raise ValueError('threshold_tuning_requires_dated_d_t_with_at_least_2_observations')
    sigma = hac_sigma_with_sensitivity(d)['sigma_hac']
    best = None
    for thresh in candidate_thresholds:
        val = float(thresh)
        if best is None or abs(val - 0.5 * sigma) < abs(best - 0.5 * sigma):
            best = val
    return {'partition_used': oldest['name'], 'oldest_partition_verified': True, 'tuned_threshold': best, 'sigma_d_t': sigma}


def run_calibration_go_no_go(
    db,
    namespace,
    strategy,
    real_d_t,
    smallest_actionable_edge,
    d_t_dates,
    partitions,
    window_days=None,
    k_eff=1.0,
    power_floor=POWER_FLOOR,
    lengthen_on_low_power=False,
    calibration_id=None,
    correlation_id=None,
    seed=AP['base_seed'],
):
    """Calibration go/no-go on the date-enforced oldest partition. GO needs power >= floor AND HAC/bootstrap agreement.

    The economic hurdle (smallest_actionable_edge) is an input locked before calibration; it is never retuned here.
    """
    s = int(strategy)
    ordered = validate_partitions(partitions)
    oldest = ordered[0]
    if len(d_t_dates) != len(real_d_t):
        raise ValueError('d_t_dates_must_match_real_d_t')
    assert_dates_in_partition(d_t_dates, oldest, 'calibration')

    w_days = int(window_days or ae.default_target_window_days(s))
    pwr = estimate_power_at_injected_edge(real_d_t=real_d_t, smallest_actionable_edge=smallest_actionable_edge, window_days=w_days, k_eff=k_eff, seed=seed)
    floor = float(power_floor)
    sigma_ok = bool(pwr['sigma_cross_check']['agree'])
    passes_floor = pwr['measured_power'] >= floor - 1e-12

    if not sigma_ok:
        go_no_go, pipeline_mode, recommended_window_days = 'NO_GO_MONITOR_ONLY', 'MONITOR_ONLY', w_days
        reason = 'hac_bootstrap_sigma_disagreement_fail_closed'
    elif passes_floor:
        go_no_go, pipeline_mode, recommended_window_days = 'GO', 'OBSERVATION_ONLY_PENDING_COMPLETION', w_days
        reason = 'power_floor_met'
    elif lengthen_on_low_power:
        go_no_go, pipeline_mode = 'NO_GO_LENGTHEN_WINDOW', 'MONITOR_ONLY'
        z_req = pwr['effective_critical_value'] + float(NormalDist(0.0, 1.0).inv_cdf(floor))
        req_n = int(math.ceil(((z_req * pwr['sigma_d_t']) / float(smallest_actionable_edge)) ** 2))
        recommended_window_days = max(w_days + ae.EXTENSION_DAYS, req_n)
        reason = 'power_below_floor_lengthen_window'
    else:
        go_no_go, pipeline_mode, recommended_window_days = 'NO_GO_MONITOR_ONLY', 'MONITOR_ONLY', w_days
        reason = 'power_below_floor'

    cid = str(calibration_id or f'cal_{s}_{uuid.uuid4().hex[:12]}')
    prior = db.query('SELECT holdout_touched,vault_spent FROM adaptive_calibration_runs WHERE namespace=? AND calibration_id=?', (namespace, cid))
    if prior and (int(prior[0]['holdout_touched']) or int(prior[0]['vault_spent'])):
        raise PermissionError(f'calibration_already_touched_holdout:{cid}')
    corr = str(correlation_id or cid)
    stamp = _utc_now()
    details = {
        **pwr,
        'power_floor': floor,
        'passes_power_floor': passes_floor,
        'sigma_cross_check_agree': sigma_ok,
        'go_no_go': go_no_go,
        'go_no_go_reason': reason,
        'pipeline_mode': pipeline_mode,
        'automatic_promotion_enabled': False,
        'recommended_window_days': recommended_window_days,
        'partition_name': oldest['name'],
        'partitions': ordered,
        'd_t_first_date': str(min(d_t_dates))[:10],
        'd_t_last_date': str(max(d_t_dates))[:10],
        'economic_hurdle_locked_before_calibration': float(smallest_actionable_edge),
    }
    dhash = alog.compute_hash(details)
    db.batch([(
        'INSERT OR REPLACE INTO adaptive_calibration_runs('
        'namespace,calibration_id,correlation_id,strategy,partition_name,sigma_d_t,mde,injected_edge,'
        'measured_power,power_floor,go_no_go,pipeline_mode,holdout_touched,vault_spent,details_json,data_hash,recorded_at'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (namespace, cid, corr, s, oldest['name'], pwr['sigma_d_t'], pwr['mde_daily'], pwr['injected_edge'],
         pwr['measured_power'], floor, go_no_go, pipeline_mode, 0, 0, alog.stable_json(details), dhash, stamp),
    )])
    alog.append_log(
        db, namespace, correlation_id=corr, track='SYSTEM', strategy=s, category='CALIBRATION',
        decision_type='CALIBRATION_GO' if go_no_go == 'GO' else 'CALIBRATION_NO_GO',
        named_cause=reason, rule_fired='calibration_power_floor_and_hac_bootstrap_cross_check',
        conditions={'sigma_d_t': pwr['sigma_d_t'], 'mde_daily': pwr['mde_daily'], 'injected_edge': pwr['injected_edge'],
                    'measured_power': pwr['measured_power'], 'power_floor': floor, 'partition_name': oldest['name'],
                    'sigma_cross_check_agree': sigma_ok},
        execution_details=details, input_data=details, source_at=stamp,
    )
    return {'calibration_id': cid, 'correlation_id': corr, 'strategy': s, **details}


def _fresh_holdout_min_sessions(strategy):
    return AP['fresh_holdout_min_sessions_fast_swing'] if ae.strategy_tier(strategy) == 'FAST_SWING' else AP['fresh_holdout_min_sessions_trend']


def _vaults_for_strategy(db, namespace, strategy):
    rows = db.query(
        'SELECT v.* FROM adaptive_holdout_vault v JOIN adaptive_calibration_runs c ON c.namespace=v.namespace AND c.calibration_id=v.calibration_id '
        'WHERE v.namespace=? AND c.strategy=? ORDER BY v.first_touched_at',
        (namespace, int(strategy)),
    )
    for r in rows:
        r['details'] = json.loads(r['details_json'] or '{}')
    return rows


def touch_holdout_vault_once(db, namespace, vault_id, calibration_id, holdout_passed, holdout_dates, partitions=None):
    """Single-touch holdout vault (C-09).

    - The holdout dates must lie in the newest partition of the calibration (date-enforced), and the calibration must
      be GO; a vault is never touched to rescue a NO-GO.
    - A failed touch spends the vault: the strategy is monitor-only (NOT eligible for promotion). Observation-period
      data is NOT a substitute holdout. Eligibility returns only after a FRESH holdout of fresh_holdout_min_sessions
      sessions, all dated after the spent vault's last holdout date, is touched once and passes.
    """
    vid = str(vault_id)
    existing = db.query('SELECT * FROM adaptive_holdout_vault WHERE namespace=? AND vault_id=?', (namespace, vid))
    if existing and (int(existing[0]['touched_count']) >= 1 or int(existing[0]['vault_spent']) == 1):
        raise PermissionError(f'holdout_vault_already_touched_or_spent:{vid}')
    cal_rows = db.query('SELECT * FROM adaptive_calibration_runs WHERE namespace=? AND calibration_id=?', (namespace, str(calibration_id)))
    if not cal_rows:
        raise KeyError(f'calibration_not_found:{calibration_id}')
    cal_row = cal_rows[0]
    if cal_row['go_no_go'] != 'GO':
        raise PermissionError(f"holdout_touch_requires_calibration_go:{cal_row['go_no_go']}")
    if int(cal_row['holdout_touched']):
        raise PermissionError(f'calibration_already_touched_holdout:{calibration_id}')
    s = int(cal_row['strategy'])
    ds = sorted(str(d)[:10] for d in holdout_dates)
    if not ds:
        raise ValueError('holdout_dates_required')
    cal_details = json.loads(cal_row['details_json'] or '{}')
    ordered = validate_partitions(partitions or cal_details.get('partitions') or [])
    prior = _vaults_for_strategy(db, namespace, s)
    spent = [v for v in prior if int(v['vault_spent']) == 1]
    fresh_required = None
    if spent:
        last_spent_date = max(str(v['details'].get('holdout_last_date') or '') for v in spent)
        fresh_required = _fresh_holdout_min_sessions(s)
        if ds[0] <= last_spent_date:
            raise PermissionError(f'fresh_holdout_must_start_after_spent_vault:{last_spent_date}')
        if len(set(ds)) < fresh_required:
            raise PermissionError(f'fresh_holdout_insufficient_sessions:{len(set(ds))}<{fresh_required}')
    else:
        assert_dates_in_partition(ds, ordered[-1], 'holdout')

    passed = bool(holdout_passed)
    vault_spent = 0 if passed else 1
    status = 'PASSED_SINGLE_TOUCH' if passed else 'SPENT_MONITOR_ONLY_UNTIL_FRESH_HOLDOUT'
    stamp = _utc_now()
    details = {
        'vault_id': vid, 'calibration_id': str(calibration_id), 'strategy': s, 'holdout_passed': passed,
        'vault_spent': bool(vault_spent), 'holdout_first_date': ds[0], 'holdout_last_date': ds[-1],
        'holdout_sessions': len(set(ds)), 'fresh_holdout_required_sessions': fresh_required,
        'promotion_eligible': passed, 'observation_period_is_not_a_holdout': True,
    }
    db.batch([
        ('INSERT INTO adaptive_holdout_vault(namespace,vault_id,touched_count,vault_spent,first_touched_at,calibration_id,status,details_json,updated_at) '
         'VALUES(?,?,?,?,?,?,?,?,?)', (namespace, vid, 1, vault_spent, stamp, str(calibration_id), status, alog.stable_json(details), stamp)),
        ('UPDATE adaptive_calibration_runs SET holdout_touched=1,vault_spent=? WHERE namespace=? AND calibration_id=?',
         (vault_spent, namespace, str(calibration_id))),
    ])
    alog.append_log(
        db, namespace, correlation_id=str(calibration_id), track='SYSTEM', strategy=s, category='CALIBRATION',
        decision_type='CALIBRATION_GO' if passed else 'CALIBRATION_NO_GO',
        named_cause='holdout_vault_passed' if passed else 'holdout_vault_spent_monitor_only',
        rule_fired='holdout_vault_single_touch_lock',
        conditions={'vault_id': vid, 'touched_count': 1, 'holdout_passed': passed}, execution_details=details, source_at=stamp,
    )
    return {'vault_id': vid, 'touched_count': 1, 'vault_spent': bool(vault_spent), 'status': status, 'details': details}


def holdout_eligibility(db, namespace, strategy):
    """Promotion eligibility from the vault history: the most recent touch must be a pass. Spent => monitor-only."""
    vaults = _vaults_for_strategy(db, namespace, strategy)
    if not vaults:
        return {'eligible': False, 'reason': 'holdout_vault_not_touched'}
    last = vaults[-1]
    if int(last['vault_spent']) == 1:
        return {'eligible': False, 'reason': 'holdout_vault_spent_monitor_only_until_fresh_holdout', 'vault_id': last['vault_id']}
    return {'eligible': True, 'reason': 'holdout_vault_passed', 'vault_id': last['vault_id'], 'calibration_id': last['calibration_id']}


def evaluate_meta_kill_switch(db, namespace, strategy, champion_r_series, track_a_r_series, min_days=AP['meta_kill_min_days'], max_negative_t=AP['meta_kill_max_t'], spawn_id=None):
    """Trigger the meta-kill switch if Track C champions fail to beat Track A baseline over time."""
    s = int(strategy)
    d_t = rc.compute_paired_active_returns(champion_r_series, track_a_r_series)
    n = len(d_t)
    hac = rc.newey_west_t_hac(d_t)
    cum_active = float(np.sum(d_t))
    triggered = bool(n >= int(min_days) and cum_active < 0.0 and hac['t_hac'] <= float(max_negative_t))
    stamp = _utc_now()

    if triggered:
        db.batch([
            (
                "UPDATE adaptive_tracks SET status='killed',updated_at=? WHERE namespace=? AND track='C' AND strategy=?",
                (stamp, namespace, s),
            ),
            (
                "UPDATE adaptive_probation SET meta_kill_triggered=1,status='META_KILLED',updated_at=? WHERE namespace=? AND strategy=?",
                (stamp, namespace, s),
            ),
            (
                "INSERT INTO paper_meta(namespace,key,value) VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
                (namespace, f'adaptive_meta_kill_strategy_{s}', '1'),
            ),
        ])
        alog.append_log(
            db, namespace, correlation_id=str(spawn_id or f'meta_kill_s{s}'),
            spawn_id=spawn_id, track='C', strategy=s,
            category='KILL_SWITCH', decision_type='META_KILL',
            named_cause='champion_failed_to_beat_track_a_over_time',
            rule_fired='meta_kill_switch_on_negative_champion_vs_track_a',
            conditions={'days': n, 'min_days': int(min_days), 'cumulative_active_return': cum_active, 't_hac': hac['t_hac']},
            execution_details={'meta_kill_triggered': True, 'track_c_demoted_to_track_a': True},
            source_at=stamp,
        )

    return {
        'strategy': s,
        'days_evaluated': n,
        'cumulative_active_return': cum_active,
        'mean_active_return': hac['mean'],
        't_hac': hac['t_hac'],
        'meta_kill_triggered': triggered,
    }


def run_lookahead_truncation_test(stock_df, index_df, corp_actions, cut_dates, strategies=None, params_by_strategy=None, signal_fn=None):
    """Automated CI look-ahead truncation test: cuts BOTH price data AND corporate-action data at t.

    For every cut date t and strategy, the prefilter, the corporate-action back-adjusted closes and the entry/exit
    signals at t must be identical between the full dataset and an INDEPENDENT raw cut (rebuilt from raw records, not
    a view of the full frame). Corporate actions are cut by the date they became knowable (announcement date for a
    late announcement), so a late-announced action must not leak backwards. `signal_fn` defaults to the production
    evaluator; the mutation test passes a planted look-ahead evaluator and requires a mismatch.
    """
    params_by_strategy = params_by_strategy or {}
    strategies = list(strategies) if strategies is not None else sorted(ae.BASELINE_PARAMS)
    signal_fn = signal_fn or ae.evaluate_parameterized_strategy
    mismatches = []
    checks_run = 0
    raw_stock = stock_df.to_dict('records')
    raw_index = index_df.to_dict('records')
    symbols = sorted({str(r['symbol']) for r in raw_stock})

    for cut_t in [str(d) for d in cut_dates]:
        trunc_stock = pd.DataFrame([dict(r) for r in raw_stock if str(r['trade_date']) <= cut_t])
        trunc_index = pd.DataFrame([dict(r) for r in raw_index if str(r['trade_date']) <= cut_t])
        trunc_corps = [dict(c) for c in (corp_actions or []) if ae.corp_action_known_date(c) <= cut_t]
        if trunc_stock.empty:
            continue

        for sym in symbols:
            full_sym = stock_df[stock_df['symbol'] == sym]
            trunc_sym = trunc_stock[trunc_stock['symbol'] == sym].reset_index(drop=True)
            if trunc_sym.empty or str(trunc_sym.iloc[-1]['trade_date']) != cut_t:
                continue

            pf_full = pf.evaluate(full_sym, index_df, cut_t)
            pf_trunc = pf.evaluate(trunc_sym, trunc_index, cut_t)
            checks_run += 1
            if pf_full['status'] != pf_trunc['status'] or pf_full['checks'] != pf_trunc['checks']:
                mismatches.append({'cut_date': cut_t, 'symbol': sym, 'stage': 'prefilter', 'full': pf_full['status'], 'trunc': pf_trunc['status']})

            adj_full_all_rows = ae.back_adjust_ohlc_pit(full_sym, corp_actions, as_of_date=cut_t, truncate_rows=False)
            adj_from_full = adj_full_all_rows[adj_full_all_rows['trade_date'].astype(str) <= cut_t]
            adj_from_trunc = ae.back_adjust_ohlc_pit(trunc_sym, trunc_corps, as_of_date=cut_t)
            closes_f = adj_from_full['close'].astype(float).to_numpy()
            closes_t = adj_from_trunc['close'].astype(float).to_numpy()
            checks_run += 1
            if len(closes_f) != len(closes_t) or not np.allclose(closes_f, closes_t, rtol=0.0, atol=1e-9):
                mismatches.append({'cut_date': cut_t, 'symbol': sym, 'stage': 'back_adjusted_history'})

            for s in strategies:
                p = params_by_strategy.get(int(s), {})
                ind_full = signal_fn(int(s), adj_full_all_rows, p)
                row_full = ind_full[ind_full['trade_date'].astype(str) == cut_t].iloc[-1]
                row_trunc = signal_fn(int(s), adj_from_trunc, p).iloc[-1]
                checks_run += 1
                en_f, en_t = bool(row_full['entry_condition']), bool(row_trunc['entry_condition'])
                ex_f, ex_t = bool(row_full['exit_condition']), bool(row_trunc['exit_condition'])
                cl_f, cl_t = float(row_full['close']), float(row_trunc['close'])
                if en_f != en_t or ex_f != ex_t or abs(cl_f - cl_t) > 1e-9:
                    mismatches.append({
                        'cut_date': cut_t, 'symbol': sym, 'strategy': int(s), 'stage': 'signal_or_back_adjust',
                        'full': {'entry': en_f, 'exit': ex_f, 'close': cl_f},
                        'trunc': {'entry': en_t, 'exit': ex_t, 'close': cl_t},
                    })

    return {'passed': len(mismatches) == 0 and checks_run > 0, 'checks_run': checks_run,
            'cut_dates_tested': [str(d) for d in cut_dates], 'mismatches': mismatches}
