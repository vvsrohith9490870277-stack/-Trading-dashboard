"""Paired Daily Active Return (d_t), Newey-West HAC t-stat, Required Residualized d_t Check,
Gate 4 Benchmark Alpha Binding Rule, and Block-Bootstrap Capital-Day Expectancy.

Implements Section 4 (Gate 2 & Gate 4), A5, A8.5, and B3 of the Definitive Coding-Ready Plan:
- d_t = R_var,t - R_base,t (unlevered total portfolio MTM including cash)
- Clears cost + slippage hurdle AND t_HAC >= effective_critical_value(K_eff)
- Required residualized check: regress d_t on index returns R_idx,t; residual mean (alpha)
  must be positive (or Gate 4 becomes binding when beta estimate is stable)
- Secondary: capital-day expectancy with circular/moving block-bootstrap CI
"""
import math
import numpy as np

import adaptive_policy as apol
import trial_counting as tc

AP = apol.POLICY


def compute_paired_active_returns(r_var, r_base):
    """Compute paired daily active return d_t = R_var,t - R_base,t (unlevered total portfolio MTM including cash)."""
    v = np.asarray(r_var, dtype=float)
    b = np.asarray(r_base, dtype=float)
    if v.shape != b.shape or v.ndim != 1 or len(v) == 0:
        raise ValueError('paired_returns_must_have_matching_nonempty_1d_shape')
    return v - b


def newey_west_auto_lag(n, bandwidth_multiplier=1.0):
    """Newey-West (1994) automatic lag floor(c * (n / n_ref)^(a/b)), constants from adaptive_policy.json
    (standard_formula), optionally scaled by a bandwidth multiplier for sensitivity analysis (C-09)."""
    expo = AP['hac_auto_lag_exponent_num'] / float(AP['hac_auto_lag_exponent_den'])
    base = AP['hac_auto_lag_coefficient'] * ((int(n) / AP['hac_auto_lag_reference_n']) ** expo)
    return max(1, min(int(n) - 1, int(math.floor(base * float(bandwidth_multiplier)))))


def newey_west_t_hac(series, max_lag=None, bandwidth_multiplier=1.0):
    """Compute Newey-West HAC standard error and t-statistic for the mean of a 1D return series."""
    x = np.asarray(series, dtype=float)
    n = len(x)
    if n < 2:
        return {'mean': float(x[0]) if n == 1 else 0.0, 'se_hac': 0.0, 't_hac': 0.0, 'max_lag': 0}
    mean = float(np.mean(x))
    centered = x - mean
    if max_lag is None:
        max_lag = newey_west_auto_lag(n, bandwidth_multiplier)
    max_lag = min(int(max_lag), n - 1)

    gamma_0 = float(np.dot(centered, centered)) / n
    lrv = gamma_0
    for lag in range(1, max_lag + 1):
        weight = 1.0 - lag / float(max_lag + 1)
        gamma_l = float(np.dot(centered[lag:], centered[:-lag])) / n
        lrv += 2.0 * weight * gamma_l

    # Fallback to sample variance if Bartlett sum is numerically non-positive
    if lrv <= 0.0:
        lrv = float(np.var(x, ddof=1))
    se_hac = math.sqrt(max(lrv, 0.0) / n)
    if se_hac > 1e-15:
        t_hac = mean / se_hac
    elif abs(mean) > 1e-15:
        t_hac = math.copysign(1e6, mean)
    else:
        t_hac = 0.0
    return {
        'mean': mean,
        'long_run_sigma': math.sqrt(max(lrv, 0.0)),
        'se_hac': se_hac,
        't_hac': float(t_hac),
        'max_lag': max_lag,
        'n': n,
    }


def run_residualized_check(d_t, index_returns, beta_se_stability_threshold=AP['gate4_beta_se_stability_threshold'], max_subsample_beta_diff=AP['gate4_max_subsample_beta_diff']):
    """Regress d_t on index returns: d_t = alpha + beta * (R_idx,t - mean(R_idx)) + u_t, and also standard OLS intercept.

    In standard OLS d_t = alpha + beta * R_idx,t + eps_t:
    - alpha = mean(d_t) - beta * mean(R_idx,t) is the benchmark-residualized mean active return
      (the part of d_t NOT explained by passive beta exposure to the index).
    - The residualized series adjusted for index exposure is r_res,t = d_t - beta * R_idx,t,
      whose sample mean is exactly alpha = mean(d_t) - beta * mean(R_idx,t).
    - Also checks beta stability across halves / standard error to determine if Gate 4 is binding.
    """
    d = np.asarray(d_t, dtype=float)
    idx = np.asarray(index_returns, dtype=float)
    if d.shape != idx.shape or d.ndim != 1 or len(d) < 3:
        raise ValueError('d_t_and_index_returns_must_match_with_at_least_3_observations')

    n = len(d)
    var_idx = float(np.var(idx, ddof=1))
    if var_idx <= 1e-16:
        beta = 0.0
        alpha = float(np.mean(d))
        beta_se = float('inf')
        t_alpha = newey_west_t_hac(d)['t_hac']
        residual_series = d.copy()
    else:
        cov_d_idx = float(np.cov(d, idx, ddof=1)[0, 1])
        beta = cov_d_idx / var_idx
        alpha = float(np.mean(d) - beta * np.mean(idx))
        # Exposure-stripped active return series: d_t - beta * R_idx,t (mean == alpha)
        residual_series = d - beta * idx
        ols_resid = residual_series - alpha
        s2 = float(np.dot(ols_resid, ols_resid)) / max(1, n - 2)
        ssx = float(np.sum((idx - np.mean(idx)) ** 2))
        beta_se = math.sqrt(max(0.0, s2 / ssx)) if ssx > 1e-16 else float('inf')
        t_alpha = newey_west_t_hac(residual_series)['t_hac']

    # Check subsample beta stability (first half vs second half)
    mid = n // 2
    if mid >= 3 and float(np.var(idx[:mid], ddof=1)) > 1e-16 and float(np.var(idx[mid:], ddof=1)) > 1e-16:
        b1 = float(np.cov(d[:mid], idx[:mid], ddof=1)[0, 1] / np.var(idx[:mid], ddof=1))
        b2 = float(np.cov(d[mid:], idx[mid:], ddof=1)[0, 1] / np.var(idx[mid:], ddof=1))
        sub_diff = abs(b1 - b2)
    else:
        sub_diff = 0.0

    beta_stable = math.isfinite(beta_se) and (beta_se <= float(beta_se_stability_threshold)) and (sub_diff <= float(max_subsample_beta_diff))
    residual_mean_positive = float(alpha) > 0.0
    gate4_binding = bool(beta_stable or not residual_mean_positive)
    gate4_passed = bool(residual_mean_positive and t_alpha > 0.0)
    passed = bool(residual_mean_positive and (not gate4_binding or gate4_passed))

    return {
        'residual_mean': float(alpha),
        'alpha': float(alpha),
        'beta': float(beta),
        'beta_se': float(beta_se) if math.isfinite(beta_se) else None,
        'subsample_beta_diff': float(sub_diff),
        'beta_stable': beta_stable,
        't_alpha_hac': float(t_alpha),
        'residual_mean_positive': residual_mean_positive,
        'gate4_binding': gate4_binding,
        'gate4_passed': gate4_passed,
        'passed': passed,
    }


def compute_capital_day_expectancy_bootstrap(d_t, block_size=AP['bootstrap_block_size'], n_boot=AP['bootstrap_n_boot'], ci_level=AP['bootstrap_ci_level'], seed=AP['base_seed']):
    """Compute secondary metric: capital-day expectancy with circular block-bootstrap confidence interval."""
    d = np.asarray(d_t, dtype=float)
    n = len(d)
    if n == 0:
        return {'expectancy': 0.0, 'ci_lower': 0.0, 'ci_upper': 0.0, 'positive_ci': False}
    expectancy = float(np.mean(d))
    if n < 2:
        return {'expectancy': expectancy, 'ci_lower': expectancy, 'ci_upper': expectancy, 'positive_ci': expectancy > 0}

    rng = np.random.default_rng(int(seed))
    b_len = max(1, min(int(block_size), n))
    n_blocks = int(math.ceil(n / float(b_len)))
    starts = rng.integers(0, n, size=(int(n_boot), n_blocks))
    offsets = np.arange(b_len)
    indices = ((starts[:, :, None] + offsets[None, None, :]) % n).reshape(int(n_boot), -1)[:, :n]
    boot_means = np.mean(d[indices], axis=1)

    tail = (1.0 - float(ci_level)) / 2.0
    ci_low = float(np.quantile(boot_means, tail))
    ci_high = float(np.quantile(boot_means, 1.0 - tail))
    return {
        'expectancy': expectancy,
        'expectancy_bps': expectancy * 10_000.0,
        'ci_level': float(ci_level),
        'ci_lower': ci_low,
        'ci_upper': ci_high,
        'block_size': b_len,
        'n_boot': int(n_boot),
        'positive_expectancy': expectancy > 0.0,
        'positive_lower_ci': ci_low > 0.0,
    }


def evaluate_gate2_and_gate4(
    r_var,
    r_base,
    index_returns,
    k_cand=1,
    rho_bar=0.0,
    cost_slippage_hurdle=AP['gate2_cost_slippage_hurdle_daily'],
    alpha=tc.DEFAULT_ALPHA,
    bootstrap_seed=AP['base_seed'],
):
    """Evaluate Gate 2 (paired daily active return d_t, t_HAC >= effective critical value, required residualized check)
    and Gate 4 (benchmark alpha, binding when residualized check or beta stability requires it).
    """
    d_t = compute_paired_active_returns(r_var, r_base)
    hac = newey_west_t_hac(d_t)
    crit_link = tc.evaluate_gate2_critical_link(
        t_hac=hac['t_hac'],
        k_cand=k_cand,
        rho_bar=rho_bar,
        alpha=alpha,
    )
    res_check = run_residualized_check(d_t, index_returns)
    cap_day = compute_capital_day_expectancy_bootstrap(d_t, seed=bootstrap_seed)

    clears_hurdle = hac['mean'] > float(cost_slippage_hurdle)
    gate2_passed = bool(
        clears_hurdle
        and crit_link['passes_effective_critical_value']
        and res_check['passed']
    )
    gate4_passed = bool(res_check['gate4_passed']) if res_check['gate4_binding'] else True

    return {
        'd_t_mean': hac['mean'],
        'd_t_mean_bps': hac['mean'] * 10_000.0,
        'se_hac': hac['se_hac'],
        't_hac': hac['t_hac'],
        'cost_slippage_hurdle': float(cost_slippage_hurdle),
        'clears_cost_slippage_hurdle': clears_hurdle,
        'k_cand': crit_link['k_cand'],
        'rho_bar': crit_link['rho_bar_clamped'],
        'k_eff': crit_link['k_eff'],
        'effective_critical_value': crit_link['effective_critical_value'],
        'passes_effective_critical_value': crit_link['passes_effective_critical_value'],
        'residualized_check': res_check,
        'capital_day_expectancy': cap_day,
        'gate2_passed': gate2_passed,
        'gate4_binding': res_check['gate4_binding'],
        'gate4_passed': gate4_passed,
    }
