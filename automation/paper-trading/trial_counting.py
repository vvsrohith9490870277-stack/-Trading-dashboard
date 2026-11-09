"""Trial Counting (K_cand, K_eff) and Explicit Link to Gate 2 Effective Critical Value.

Implements Section 3 / A4 / B3 of the Definitive Coding-Ready Plan:
- K_cand counts only candidate evaluations eligible for selection (CANDIDATE and TRACK_C_COMPARISON).
- Placebos, plateau neighbors, and shadows are strictly excluded from K_cand.
- Linear formula: K_eff = 1 + (K_cand - 1) * (1 - rho_bar), with rho_bar clamped <= 0.9.
- Effective critical value: max(1.40, multiple-testing adjusted threshold Phi^{-1}(1 - alpha / K_eff)).
- Explicit Gate 2 link: observed t_HAC must clear the effective critical value.
"""
import math
from statistics import NormalDist
import numpy as np

import adaptive_logging as alog
import adaptive_policy as apol

ELIGIBLE_CANDIDATE_KINDS = frozenset({'CANDIDATE', 'TRACK_C_COMPARISON'})
EXCLUDED_CANDIDATE_KINDS = frozenset({'PLACEBO', 'PLATEAU_NEIGHBOR', 'SHADOW', 'BASELINE'})

MAX_RHO_BAR = apol.POLICY['max_rho_bar']
MIN_CRITICAL_VALUE_FLOOR = apol.POLICY['critical_value_floor']
DEFAULT_ALPHA = apol.POLICY['alpha']


def count_k_cand(records):
    """Count K_cand from a list of candidate evaluation records, excluding placebos, plateau neighbors, and shadows."""
    eligible = []
    excluded = []
    for r in records:
        kind = str(r.get('candidate_kind') or 'CANDIDATE').upper()
        flag = r.get('eligible_for_selection')
        is_eligible = (kind in ELIGIBLE_CANDIDATE_KINDS) and (flag is None or bool(int(flag)))
        if kind in EXCLUDED_CANDIDATE_KINDS:
            is_eligible = False
        if is_eligible:
            eligible.append(r)
        else:
            excluded.append(r)
    return {
        'k_cand': max(1, len(eligible)),
        'raw_eligible_count': len(eligible),
        'excluded_count': len(excluded),
        'eligible_ids': [r.get('spawn_id') or r.get('id') for r in eligible],
        'excluded_ids': [r.get('spawn_id') or r.get('id') for r in excluded],
    }


def estimate_rho_bar(candidate_return_matrix=None, explicit_rho_bar=None):
    """Estimate average pairwise correlation rho_bar across screened candidates, clamped <= 0.90."""
    if explicit_rho_bar is not None:
        val = float(explicit_rho_bar)
        if not math.isfinite(val):
            val = 0.0
        return min(MAX_RHO_BAR, max(0.0, val))

    if candidate_return_matrix is None:
        return 0.0

    arr = np.asarray(candidate_return_matrix, dtype=float)
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 3:
        return 0.0

    n_cand = arr.shape[0]
    corr = np.corrcoef(arr)
    off_diag = []
    for i in range(n_cand):
        for j in range(i + 1, n_cand):
            c = float(corr[i, j])
            if math.isfinite(c):
                off_diag.append(c)
    if not off_diag:
        return 0.0
    mean_rho = float(np.mean(off_diag))
    return min(MAX_RHO_BAR, max(0.0, mean_rho))


def compute_k_eff(k_cand, rho_bar=0.0):
    """Compute linear K_eff = 1 + (K_cand - 1) * (1 - rho_bar) with rho_bar clamped <= 0.90."""
    kc = max(1, int(k_cand))
    clamped_rho = min(MAX_RHO_BAR, max(0.0, float(rho_bar)))
    k_eff = 1.0 + (kc - 1) * (1.0 - clamped_rho)
    return {
        'k_cand': kc,
        'rho_bar_raw': float(rho_bar),
        'rho_bar_clamped': clamped_rho,
        'k_eff': float(k_eff),
    }


def compute_effective_critical_value(k_eff, alpha=DEFAULT_ALPHA, floor=MIN_CRITICAL_VALUE_FLOOR):
    """Compute effective critical value = max(1.40, Phi^{-1}(1 - alpha / K_eff)).

    Also reports the expected maximum under K_eff standard normal null draws so both
    multiple-testing threshold representations are available and audited.
    """
    ke = max(1.0, float(k_eff))
    a = float(alpha)
    if not (0.0 < a < 0.5):
        raise ValueError(f'invalid_alpha:{alpha}')

    nd = NormalDist(0.0, 1.0)
    tail_prob = max(1e-12, min(0.5, a / ke))
    t_star_sidak = float(nd.inv_cdf(1.0 - tail_prob))
    # Expected max of K_eff Gaussian draws via Euler-Mascheroni extreme-value quantiles
    euler_gamma = 0.5772156649015329
    if ke <= 1.0:
        expected_max_null = 0.0
    else:
        q1 = nd.inv_cdf(1.0 - 1.0 / ke)
        q2 = nd.inv_cdf(1.0 - 1.0 / (ke * math.e))
        expected_max_null = float((1.0 - euler_gamma) * q1 + euler_gamma * q2)

    multiple_testing_threshold = max(t_star_sidak, expected_max_null)
    effective_crit = max(float(floor), multiple_testing_threshold)
    return {
        'k_eff': ke,
        'alpha': a,
        'floor': float(floor),
        't_star_quantile': t_star_sidak,
        'expected_max_null_t': expected_max_null,
        'multiple_testing_threshold': multiple_testing_threshold,
        'effective_critical_value': effective_crit,
    }


def evaluate_gate2_critical_link(t_hac, k_cand, rho_bar=0.0, alpha=DEFAULT_ALPHA, floor=MIN_CRITICAL_VALUE_FLOOR):
    """Explicit link between K_eff and Gate 2: observed t_HAC must exceed/equal effective critical value."""
    keff_info = compute_k_eff(k_cand=k_cand, rho_bar=rho_bar)
    crit_info = compute_effective_critical_value(keff_info['k_eff'], alpha=alpha, floor=floor)
    observed = float(t_hac)
    eff_crit = crit_info['effective_critical_value']
    passed = math.isfinite(observed) and (observed >= eff_crit)
    return {
        **keff_info,
        **crit_info,
        't_hac': observed,
        'passes_effective_critical_value': passed,
        'margin_over_critical': observed - eff_crit if math.isfinite(observed) else None,
    }


def cumulative_k_cand(db, namespace, strategy):
    """C-11: K_cand persisted cumulatively across ALL spawn rounds for a strategy (every eligible CANDIDATE and
    TRACK_C_COMPARISON ever spawned, whatever its status), read from adaptive_spawns."""
    rows = db.query(
        'SELECT spawn_id,candidate_kind,eligible_for_selection FROM adaptive_spawns WHERE namespace=? AND strategy=?',
        (namespace, int(strategy)),
    )
    return count_k_cand(rows)


def record_trial_count(db, namespace, strategy, candidate_records=None, candidate_return_matrix=None, explicit_rho_bar=None, t_hac=0.0, spawn_id=None, correlation_id=None, alpha=DEFAULT_ALPHA):
    """Compute K_cand, rho_bar, K_eff, and effective critical value from DB or provided records and persist to spawn/logs."""
    if candidate_records is None:
        candidate_records = db.query(
            'SELECT spawn_id,candidate_kind,eligible_for_selection FROM adaptive_spawns WHERE namespace=? AND strategy=?',
            (namespace, int(strategy)),
        )
    counts = count_k_cand(candidate_records)
    rho = estimate_rho_bar(candidate_return_matrix=candidate_return_matrix, explicit_rho_bar=explicit_rho_bar)
    gate2_link = evaluate_gate2_critical_link(t_hac=t_hac, k_cand=counts['k_cand'], rho_bar=rho, alpha=alpha)

    if spawn_id is not None:
        db.batch([(
            'UPDATE adaptive_spawns SET k_cand=?,rho_bar=?,k_eff=?,effective_critical_value=? WHERE namespace=? AND spawn_id=?',
            (
                gate2_link['k_cand'],
                gate2_link['rho_bar_clamped'],
                gate2_link['k_eff'],
                gate2_link['effective_critical_value'],
                namespace,
                str(spawn_id),
            ),
        )])
        alog.append_log(
            db,
            namespace=namespace,
            correlation_id=str(correlation_id or spawn_id),
            spawn_id=str(spawn_id),
            track='B',
            strategy=int(strategy),
            category='TRIAL_COUNT',
            decision_type='GATE_PASS' if gate2_link['passes_effective_critical_value'] else 'HOLD',
            named_cause='k_eff_linked_to_gate2',
            rule_fired='linear_k_eff_with_rho_clamped_0_90',
            conditions={
                'k_cand': gate2_link['k_cand'],
                'excluded_count': counts['excluded_count'],
                'rho_bar_clamped': gate2_link['rho_bar_clamped'],
                'k_eff': gate2_link['k_eff'],
                'effective_critical_value': gate2_link['effective_critical_value'],
                't_hac': gate2_link['t_hac'],
            },
            execution_details={
                'eligible_ids': counts['eligible_ids'],
                'excluded_ids': counts['excluded_ids'],
                't_star_quantile': gate2_link['t_star_quantile'],
                'expected_max_null_t': gate2_link['expected_max_null_t'],
            },
        )
    return {
        **counts,
        **gate2_link,
    }
