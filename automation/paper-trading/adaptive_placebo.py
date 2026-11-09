"""Gate 6 neighbour generation and Gate 8 cached-placebo machinery (Adaptive Completion Plan v2, Phase 3).

Library only: nothing here is scheduled or wired into the live runtime. All numbers come from adaptive_policy.json.

Gate 6 (D5, E3): neighbours are generated from plateau_step_sizes (one parameter moved +/- neighbour_radius_steps
steps at a time), simulated by the caller-supplied simulator, and the improvement retention is aggregated with
plateau_aggregation (median >= plateau_retention_min, no neighbour below min_neighbour_improvement).
Neighbours are PLATEAU_NEIGHBOR and never count in K_cand.

Gate 8 (D4, E2, G-02): every pool member (the full pre-registered spawn-time grid) is simulated ONCE per window and its
trade log is cached together with the per-symbol daily return path. Each of placebo_runs placebo runs jitters ONLY the
discordant trades (entry shifted by up to placebo_jitter_sessions sessions, same holding length) on the cache, re-runs
the recorded spawn-time screening (select the best pool member by HAC t) and records the selected statistic. The
candidate must beat the placebo_percentile of those maxima. Fewer than min_new_discordant_trades discordant trades
fails closed. full_recompute_member_d_t() is the naive day-by-day recomputation used to prove cache equivalence.
"""
import math
from decimal import Decimal
import numpy as np

import adaptive_engine as ae
import adaptive_policy as apol
import residualized_check as rc

AP = apol.POLICY


# ---- Gate 6 -----------------------------------------------------------------------------------------------------

def generate_plateau_neighbours(strategy, params):
    """One-parameter-at-a-time neighbours at +/- k*step for k = 1..neighbour_radius_steps. Invalid points (<= 0,
    a ratio pushed to >= 1, or a parameter the engine rejects) are dropped and reported, never silently replaced."""
    s = int(strategy)
    centre = ae.validate_parameter_only_mutation(s, params)
    steps = AP['plateau_step_sizes'][str(s)]
    radius = AP['plateau_aggregation']['neighbour_radius_steps']
    neighbours, dropped = [], []
    for name in sorted(steps):
        base_val = centre[name]
        is_ratio = isinstance(ae.BASELINE_PARAMS[s][name], float) and ae.BASELINE_PARAMS[s][name] < 1
        for k in range(1, radius + 1):
            for sign in (-1, 1):
                exact = Decimal(str(base_val)) + sign * k * Decimal(str(steps[name]))  # no binary float drift
                v = int(exact) if isinstance(ae.BASELINE_PARAMS[s][name], int) else float(exact)
                cand = dict(centre)
                cand[name] = v
                if v <= 0 or (is_ratio and v >= 1):
                    dropped.append({'param': name, 'value': v, 'reason': 'out_of_domain'})
                    continue
                try:
                    neighbours.append({'param': name, 'offset_steps': sign * k, 'params': ae.validate_parameter_only_mutation(s, cand),
                                       'candidate_kind': 'PLATEAU_NEIGHBOR'})
                except ValueError as exc:
                    dropped.append({'param': name, 'value': v, 'reason': str(exc)})
    return {'centre': centre, 'neighbours': neighbours, 'dropped': dropped}


def aggregate_plateau(candidate_improvement, neighbour_improvements):
    """D5 retention rule from policy. Returns pass/fail with the aggregate and the worst neighbour."""
    agg = AP['plateau_aggregation']
    cand = float(candidate_improvement)
    vals = [float(x) for x in neighbour_improvements]
    if cand <= 0.0 or not vals:
        return {'passed': False, 'reason': 'no_positive_candidate_improvement_or_no_neighbours', 'candidate_improvement': cand,
                'neighbour_count': len(vals), 'neighbours_excluded_from_k_cand': True}
    stat = float(np.median(vals)) if agg['statistic'] == 'median' else float(np.mean(vals))
    retention = stat / cand
    worst = min(vals)
    passed = retention >= AP['plateau_retention_min'] - 1e-12 and worst >= agg['min_neighbour_improvement']
    return {'passed': bool(passed), 'statistic': agg['statistic'], 'retention_ratio': retention,
            'required_retention': AP['plateau_retention_min'], 'min_neighbour_improvement': worst,
            'required_min_neighbour_improvement': agg['min_neighbour_improvement'], 'candidate_improvement': cand,
            'neighbour_count': len(vals), 'neighbours_excluded_from_k_cand': True}


def evaluate_gate6_with_simulation(strategy, candidate_params, simulate_returns, r_base):
    """Gate 6 end to end: generate neighbours, simulate each once with `simulate_returns(params) -> daily returns`
    (same window, same basis as the candidate), aggregate retention of mean improvement over the baseline arm."""
    gen = generate_plateau_neighbours(strategy, candidate_params)
    base = np.asarray(r_base, dtype=float)
    cand_imp = float(np.mean(np.asarray(simulate_returns(gen['centre']), dtype=float) - base))
    imps = []
    for nb in gen['neighbours']:
        r = np.asarray(simulate_returns(nb['params']), dtype=float)
        if r.shape != base.shape:
            raise ValueError('neighbour_window_mismatch')
        nb['improvement'] = float(np.mean(r - base))
        imps.append(nb['improvement'])
    res = aggregate_plateau(cand_imp, imps)
    return {**res, 'neighbours': gen['neighbours'], 'dropped': gen['dropped']}


# ---- Gate 8: cached placebos -------------------------------------------------------------------------------------

def hac_t_rows(matrix):
    """Newey-West HAC t-stat of the mean for every row of a 2-D array (vectorised rc.newey_west_t_hac)."""
    x = np.atleast_2d(np.asarray(matrix, dtype=float))
    n = x.shape[1]
    lag = rc.newey_west_auto_lag(n)
    mean = x.mean(axis=1)
    c = x - mean[:, None]
    lrv = (c * c).sum(axis=1) / n
    for l in range(1, lag + 1):
        lrv = lrv + 2.0 * (1.0 - l / float(lag + 1)) * (c[:, l:] * c[:, :-l]).sum(axis=1) / n
    fallback = x.var(axis=1, ddof=1)
    lrv = np.where(lrv <= 0.0, fallback, lrv)
    se = np.sqrt(np.maximum(lrv, 0.0) / n)
    with np.errstate(divide='ignore', invalid='ignore'):
        t = np.where(se > 1e-15, mean / se, np.where(np.abs(mean) > 1e-15, np.sign(mean) * 1e6, 0.0))
    return t


def _contribution(trades, sym_returns, n_days, shifts=None):
    """Daily active contribution of a set of trades: weight * symbol return on each held day, entries shifted by
    `shifts` (same holding length), clipped to the window."""
    out = np.zeros(n_days)
    for i, tr in enumerate(trades):
        e = int(tr['entry_idx']) + (int(shifts[i]) if shifts is not None else 0)
        lo, hi = max(0, e), min(n_days, e + int(tr['hold']))
        if hi > lo:
            out[lo:hi] += float(tr['weight']) * sym_returns[tr['symbol']][lo:hi]
    return out


def build_member_cache(variant_trades, baseline_trades, sym_returns, n_days):
    """Cache one grid member: discordant trades (in variant not baseline: +, in baseline not variant: -) and its d_t.
    Concordant trades are identical in both arms and contribute exactly zero to d_t."""
    key = lambda t: (t['symbol'], int(t['entry_idx']), int(t['hold']), float(t['weight']))
    vk, bk = {key(t) for t in variant_trades}, {key(t) for t in baseline_trades}
    plus = [t for t in variant_trades if key(t) not in bk]
    minus = [dict(t, weight=-float(t['weight'])) for t in baseline_trades if key(t) not in vk]
    disc = plus + minus
    d_t = _contribution(disc, sym_returns, n_days)
    return {'discordant': disc, 'new_discordant_count': len(plus) + len(minus), 'd_t': d_t, 'n_days': n_days}


def _jitter_shifts(rng, k, n_runs):
    j = AP['placebo_jitter_sessions']
    return rng.integers(-j, j + 1, size=(n_runs, k))


def cached_placebo_d_t(member, sym_returns, shifts):
    """d_t of one member for one placebo run, from the cache only (vectorised per trade)."""
    n = member['n_days']
    out = np.zeros(n)
    for i, tr in enumerate(member['discordant']):
        e = int(tr['entry_idx']) + int(shifts[i])
        lo, hi = max(0, e), min(n, e + int(tr['hold']))
        if hi > lo:
            out[lo:hi] += float(tr['weight']) * sym_returns[tr['symbol']][lo:hi]
    return out


def cached_placebo_matrix(member, sym_returns, shift_matrix):
    """All placebo runs of one member at once (rows = runs), from the cache only."""
    n = member['n_days']
    shift_matrix = np.atleast_2d(np.asarray(shift_matrix, dtype=int))
    m = shift_matrix.shape[0]
    mat = np.zeros((m, n))
    rows = np.arange(m)
    for i, tr in enumerate(member['discordant']):
        starts = int(tr['entry_idx']) + shift_matrix[:, i]
        ret = sym_returns[tr['symbol']]
        w = float(tr['weight'])
        for d in range(int(tr['hold'])):
            idx = starts + d
            ok = (idx >= 0) & (idx < n)
            mat[rows[ok], idx[ok]] += w * ret[idx[ok]]
    return mat


def full_recompute_member_d_t(member, sym_returns, shifts):
    """Naive recomputation for the equivalence check: rebuild both arms' held weights day by day from the shifted
    discordant trades and take the daily difference. Must equal cached_placebo_d_t exactly."""
    n = member['n_days']
    d = np.zeros(n)
    for day in range(n):
        tot = 0.0
        for i, tr in enumerate(member['discordant']):
            e = int(tr['entry_idx']) + int(shifts[i])
            if e <= day < e + int(tr['hold']):
                tot += float(tr['weight']) * float(sym_returns[tr['symbol']][day])
        d[day] = tot
    return d


def evaluate_gate8_cached(candidate_index, pool, sym_returns, seed=AP['base_seed'], m_runs=None, return_equivalence=False):
    """Gate 8 on the cache. `pool` = list of member caches (the full pre-registered grid, candidate included at
    candidate_index). Each run jitters only discordant trades of EVERY member, re-runs the spawn-time screening
    (select max HAC t across the pool) and records that maximum."""
    m = int(m_runs or AP['placebo_runs'])
    if m != AP['placebo_runs']:
        raise ValueError(f"gate8_requires_exactly_{AP['placebo_runs']}_placebo_runs_got_{m}")
    cand = pool[int(candidate_index)]
    obs_t = float(hac_t_rows(cand['d_t'])[0])
    if cand['new_discordant_count'] < AP['min_new_discordant_trades']:
        return {'passed': False, 'reason': 'too_few_discordant_trades_fail_closed', 'new_discordant_trades': cand['new_discordant_count'],
                'min_new_discordant_trades': AP['min_new_discordant_trades'], 'observed_t_hac': obs_t}
    rng = np.random.default_rng(int(seed))
    shifts = [_jitter_shifts(rng, len(mem['discordant']), m) for mem in pool]
    maxima = np.full(m, -np.inf)
    for mi, mem in enumerate(pool):
        if not mem['discordant']:
            maxima = np.maximum(maxima, hac_t_rows(mem['d_t'])[0])
            continue
        maxima = np.maximum(maxima, hac_t_rows(cached_placebo_matrix(mem, sym_returns, shifts[mi])))
    thr = float(np.percentile(maxima, AP['placebo_percentile']))
    out = {'passed': bool(obs_t > thr), 'reason': 'beats_placebo_percentile' if obs_t > thr else 'does_not_beat_placebo_percentile',
           'observed_t_hac': obs_t, 'placebo_threshold_t': thr, 'percentile_required': AP['placebo_percentile'], 'm_runs': m,
           'pool_size': len(pool), 'new_discordant_trades': cand['new_discordant_count'], 'seed': int(seed)}
    if return_equivalence:
        k = min(AP['placebo_cache_equivalence_samples'], m)
        diffs = []
        for mi, mem in enumerate(pool):
            batch = cached_placebo_matrix(mem, sym_returns, shifts[mi][:k]) if mem['discordant'] else np.zeros((k, mem['n_days']))
            for r in range(k):
                full = full_recompute_member_d_t(mem, sym_returns, shifts[mi][r])
                diffs.append(float(max(np.max(np.abs(batch[r] - full)), np.max(np.abs(cached_placebo_d_t(mem, sym_returns, shifts[mi][r]) - full)))))
        out['cache_equivalence_max_abs_diff'] = max(diffs) if diffs else 0.0
        out['cache_equivalence_samples'] = len(diffs)
    return out


# ---- E2 null acceptance -------------------------------------------------------------------------------------------

def binomial_upper_bound(k, n, confidence=None):
    """One-sided Clopper-Pearson upper bound for a binomial proportion (bisection on the exact binomial CDF)."""
    conf = float(confidence if confidence is not None else AP['gate8_ucb_confidence'])
    if k >= n:
        return 1.0
    tail = 1.0 - conf

    def cdf(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
    lo, hi = k / float(n), 1.0
    for _ in range(int(math.ceil(math.log2(1.0 / 1e-12)))):
        mid = (lo + hi) / 2.0
        if cdf(mid) > tail:
            lo = mid
        else:
            hi = mid
    return hi


def null_acceptance(false_passes, n_nulls):
    """E2: at least gate8_null_count nulls, point false-pass <= gate8_null_point_max, one-sided UCB <= gate8_null_ucb_max."""
    ucb = binomial_upper_bound(int(false_passes), int(n_nulls))
    point = int(false_passes) / float(n_nulls)
    ok = int(n_nulls) >= AP['gate8_null_count'] and point <= AP['gate8_null_point_max'] and ucb <= AP['gate8_null_ucb_max']
    return {'passed': bool(ok), 'n_nulls': int(n_nulls), 'false_passes': int(false_passes), 'point_rate': point, 'ucb': ucb,
            'point_max': AP['gate8_null_point_max'], 'ucb_max': AP['gate8_null_ucb_max'], 'min_nulls': AP['gate8_null_count']}
