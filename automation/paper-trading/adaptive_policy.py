"""Loader for adaptive_policy.json: the single home of every adaptive (Track B/C) number (H6, C-05).

Track A frozen policy files (capital_policy.json, charge_rates.json, config.json, strategy_rules.json)
are only read here, never written. A missing, malformed or unlabelled key fails closed at import time:
every top-level value key must sit in exactly one provenance group.
"""
import hashlib, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / 'adaptive_policy.json'
META_KEYS = frozenset({'version', 'note', 'provenance', 'provenance_sources'})
PROVENANCE_GROUPS = (
    'plan', 'plan_example', 'placeholder_pending_review', 'new_pending_review',
    'implementer_pending_review', 'standard_formula', 'operational_config',
)
PENDING_REVIEW_GROUPS = ('plan_example', 'placeholder_pending_review', 'new_pending_review', 'implementer_pending_review')

_INTS = (
    'max_track_b_challengers', 'max_track_c_champions', 'fast_swing_warm_start_days', 'trend_warm_start_min_days',
    'trend_warm_start_max_days', 'trend_warm_start_default_days', 'fast_swing_target_window_days', 'trend_target_window_days',
    'extension_days', 'min_new_discordant_trades', 'min_distinct_entry_weeks', 'gate1_min_total_trades', 'gate5_min_observations',
    'placebo_runs', 'probation_days_trend', 'probation_days_fast_swing', 'bootstrap_block_size', 'bootstrap_n_boot',
    'calibration_n_sims', 'calibration_min_sims', 'meta_kill_min_days', 'observation_min_sessions', 'base_seed', 'placebo_seed_stride',
    'backfill_max_requests_per_minute', 'archive_depth_months', 'placebo_jitter_sessions', 'gate8_null_count',
    'placebo_cache_equivalence_samples', 'parity_equity_tolerance_paise', 'parity_min_consecutive_sessions',
    'runner_memory_max_mb', 'runner_cpu_quota_pct', 'fresh_holdout_min_sessions_fast_swing', 'fresh_holdout_min_sessions_trend',
    'ci_api_timeout_seconds', 'ci_local_truncation_timeout_seconds', 'db_batch_chunk_rows', 'hac_auto_lag_exponent_num', 'hac_auto_lag_exponent_den',
)
_FLOATS = (
    'max_rho_bar', 'critical_value_floor', 'alpha', 'gate2_cost_slippage_hurdle_daily', 'gate3_min_dd_baseline_floor',
    'max_dd_multiplier', 'max_single_trade_share', 'gate4_beta_se_stability_threshold', 'gate4_max_subsample_beta_diff',
    'gate5_max_abs_lag1_autocorr', 'gate5_min_half_mean_vs_overall', 'plateau_retention_min', 'placebo_percentile',
    'gate9_min_effect_size_daily', 'track_c_superiority_bps_per_day', 'probation_sigma_multiplier', 'bootstrap_ci_level',
    'power_floor', 'meta_kill_max_t', 'backfill_stop_trackA_error_multiplier', 'archive_min_coverage', 'gate8_null_point_max',
    'gate8_null_ucb_max', 'gate8_ucb_confidence', 'hac_bootstrap_max_disagreement', 'hac_auto_lag_coefficient',
    'hac_auto_lag_reference_n',
)
_PROBABILITIES = (
    'alpha', 'power_floor', 'bootstrap_ci_level', 'archive_min_coverage', 'gate8_null_point_max', 'gate8_null_ucb_max',
    'gate8_ucb_confidence', 'hac_bootstrap_max_disagreement',
)
_IST_TIMES = ('backfill_window_start_ist', 'backfill_window_end_ist', 'eod_marker_deadline_ist', 'heavy_work_window_start_ist')
_STRINGS = ('kill_switch_relative_path', 'ci_github_repo', 'harness_dashboard_read_module')
_HHMM = re.compile(r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


def _fail(reason):
    raise RuntimeError(f'adaptive_policy_invalid:{reason}')


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _frozen_strategy_ids():
    rules = json.loads((ROOT / 'strategy_rules.json').read_text(encoding='utf8'))
    return {int(k) for k in rules}


def load(path=POLICY_PATH):
    p = json.loads(Path(path).read_text(encoding='utf8'))
    for k in _INTS:
        v = p.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            _fail(k)
    for k in _FLOATS:
        if not _num(p.get(k)):
            _fail(k)
    for k in _PROBABILITIES:
        if not 0 < p[k] < 1:
            _fail(f'probability_out_of_range:{k}')
    for k in _IST_TIMES:
        if not isinstance(p.get(k), str) or not _HHMM.match(p[k]):
            _fail(k)
    for k in _STRINGS:
        if not isinstance(p.get(k), str) or not p[k]:
            _fail(k)
    names = p.get('ci_required_check_names')
    if not isinstance(names, list) or not names or not all(isinstance(x, str) and x for x in names):
        _fail('ci_required_check_names')
    for k in ('fast_swing_strategies', 'trend_strategies'):
        if not isinstance(p.get(k), list) or not p[k] or not all(isinstance(x, int) and not isinstance(x, bool) for x in p[k]):
            _fail(k)
    if set(p['fast_swing_strategies']) & set(p['trend_strategies']) or set(p['fast_swing_strategies']) | set(p['trend_strategies']) != _frozen_strategy_ids():
        _fail('strategy_tiers_must_partition_frozen_strategy_ids')
    if not p['trend_warm_start_min_days'] <= p['trend_warm_start_default_days'] <= p['trend_warm_start_max_days']:
        _fail('trend_warm_start_default_out_of_range')
    if not p['alpha'] < 0.5:
        _fail('probability_out_of_range:alpha')
    if not 0 <= p['max_rho_bar'] < 1:
        _fail('probability_out_of_range:max_rho_bar')
    if not 0 < p['placebo_percentile'] < 100:
        _fail('placebo_percentile')
    if p['gate8_null_point_max'] > p['gate8_null_ucb_max']:
        _fail('gate8_null_point_max_above_ucb_max')
    grid = p.get('hac_bandwidth_grid')
    if not isinstance(grid, list) or not grid or not all(_num(x) and x > 0 for x in grid):
        _fail('hac_bandwidth_grid')
    steps = p.get('plateau_step_sizes')
    if not isinstance(steps, dict) or {int(k) for k in steps} != _frozen_strategy_ids():
        _fail('plateau_step_sizes_must_cover_every_strategy')
    for sk, params in steps.items():
        if not isinstance(params, dict) or not params or not all(_num(v) and v > 0 for v in params.values()):
            _fail(f'plateau_step_sizes:{sk}')
    agg = p.get('plateau_aggregation')
    if not isinstance(agg, dict) or agg.get('statistic') not in ('median', 'mean') or not isinstance(agg.get('neighbour_radius_steps'), int) \
            or agg['neighbour_radius_steps'] < 1 or not _num(agg.get('min_neighbour_improvement')):
        _fail('plateau_aggregation')

    prov = p.get('provenance') or {}
    if set(prov) - set(PROVENANCE_GROUPS):
        _fail('unknown_provenance_group:' + ','.join(sorted(set(prov) - set(PROVENANCE_GROUPS))))
    seen = {}
    for group, keys in prov.items():
        for k in keys:
            if k in seen:
                _fail(f'key_in_two_provenance_groups:{k}:{seen[k]}:{group}')
            seen[k] = group
    value_keys = set(p) - META_KEYS
    missing = value_keys - set(seen)
    if missing:
        _fail('unlabelled_provenance:' + ','.join(sorted(missing)))
    phantom = set(seen) - value_keys
    if phantom:
        _fail('provenance_names_missing_key:' + ','.join(sorted(phantom)))
    return p


POLICY = load()


def provenance_of(key, policy=None):
    for group, keys in ((policy or POLICY)['provenance']).items():
        if key in keys:
            return group
    raise KeyError(key)


def pending_review_keys(policy=None):
    prov = (policy or POLICY)['provenance']
    return {g: list(prov.get(g, [])) for g in PENDING_REVIEW_GROUPS}


def policy_hash():
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def read_json(name):
    """Read a Track A policy file (read-only)."""
    return json.loads((ROOT / name).read_text(encoding='utf8'))
