"""Freeze-version capital policy. Share rounding, the 9-position sleeve, and the 3-strategy ISIN cap are enforced."""
import hashlib, json, math, re
from pathlib import Path

POLICY = json.loads((Path(__file__).resolve().parent / 'capital_policy.json').read_text())
_PRICE = re.compile(r'^\d+(\.\d{1,2})?$')

ROOT = Path(__file__).resolve().parent
LOCK_FILE = ROOT / 'freeze_lock.json'
LOCKED_FILES = ('capital_policy.json', 'charge_policy.json', 'charge_rates.json')
# Cause codes are protocol identifiers named by the freeze plan (B1), not tunable numbers.
CAUSES = {'zero_share_cause': 'UNAFFORDABLE_ZERO_SHARES', 'sleeve_slot_cause': 'SLEEVE_SLOT_FULL', 'isin_cap_cause': 'ISIN_CAP', 'sleeve_cash_cause': 'INSUFFICIENT_CASH', 'adv_filter_cause': 'ADV_FILTER_SKIP', 'participation_cause': 'ADV_PARTICIPATION_CAP', 'strategy_pause_cause': 'KILL_PAUSE'}
REQUIRED_ENFORCED = ('integer_share_rounding', 'sleeve_slot_limit', 'isin_cap', 'sleeve_cash', 'shadow_ledger', 'adv_filter_placeholder', 'adv_participation_cap', 'tiered_slippage', 'execution_path', 'microstructure_guards', 'corporate_actions', 'kill_pause')
POSITIVE_INTS = ('master_capital_inr', 'sleeve_capital_inr', 'strategy_count', 'target_notional_inr', 'max_positions_per_sleeve', 'max_strategies_per_isin', 'adv_floor_inr', 'adv_lookback_sessions', 'adv_lag_sessions', 'ucb_closed_trades', 'slippage_base_bps_through_20cr', 'slippage_base_bps_above_20cr', 'slippage_tier_threshold_inr', 'hysteresis_good_sessions')
UNIT_FRACTIONS = ('participation_rate', 'sleeve_pause_drawdown', 'system_pause_drawdown', 'system_reinstatement_gap')
ADV_REVIEW_STATES = ('pending_funnel_report', 'reviewed')


def canonical(obj):
    """Key-sorted compact JSON; db.mjs computes the same bytes for the same parsed policy."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()


def current_lock():
    """Digests of the policy files as they are on disk now (used by `python3 capital.py --print-lock`)."""
    return {name: digest(json.loads((ROOT / name).read_text())) for name in LOCKED_FILES}


def _fail(reason):
    raise SystemExit('FREEZE_CAPITAL_POLICY_REQUIRED:' + reason)


def assert_freeze(policy=None, lock=None):
    """Fail closed unless the capital policy matches freeze_lock.json and is internally consistent.

    Policy numbers live only in capital_policy.json. This function never repeats them: a change
    is made in the JSON plus a refreshed freeze_lock.json (a deliberate, reviewable step), not in code.
    """
    p = POLICY if policy is None else policy
    lk = json.loads(LOCK_FILE.read_text()) if lock is None else lock
    if digest(p) != lk.get('files', {}).get('capital_policy.json'):
        _fail('lock_mismatch')
    if not isinstance(p.get('version'), str) or not p['version'] or p.get('version') != lk.get('capital_policy_version'):
        _fail('version')
    for key in POSITIVE_INTS:
        v = p.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            _fail(key)
    for key in UNIT_FRACTIONS:
        v = p.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1:
            _fail(key)
    for key in ('slippage_participation_multiplier_bps', 'slippage_stress_multiplier', 'ucb_one_sided_z', 'circuit_lock_haircut_pct_per_extra_day'):
        v = p.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            _fail(key)
    if p['sleeve_capital_inr'] * p['strategy_count'] != p['master_capital_inr']:
        _fail('sleeves_do_not_sum_to_master')
    # Fees and slippage need headroom: a full sleeve of target-notional trades must stay below sleeve capital.
    if p['max_positions_per_sleeve'] * p['target_notional_inr'] >= p['sleeve_capital_inr']:
        _fail('no_fee_headroom')
    if p['adv_lookback_sessions'] < 2:
        _fail('adv_lookback_sessions')
    for key, name in CAUSES.items():
        if p.get(key) != name:
            _fail(key)
    if not isinstance(p.get('slot_order_seed'), str) or not p['slot_order_seed']:
        _fail('slot_order_seed')
    if p.get('share_rounding') != 'floor_notional_over_fill_price':
        _fail('share_rounding')
    if not isinstance(p.get('slippage_stress'), bool):
        _fail('slippage_stress')
    if p.get('adv_floor_review') not in ADV_REVIEW_STATES:
        _fail('adv_floor_review')
    strategies = p.get('drawdown_only_strategies')
    if not isinstance(strategies, list) or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= p['strategy_count'] for x in strategies):
        _fail('drawdown_only_strategies')
    enforced = p.get('enforced', [])
    if any(item not in enforced for item in REQUIRED_ENFORCED) or any(item in p.get('recorded_not_yet_enforced', []) for item in REQUIRED_ENFORCED):
        _fail('enforced')
    # Capability guards: these say what the code implements, not what the numbers are.
    if p['adv_lag_sessions'] != 1:
        _fail('adv_lag_sessions_only_1_implemented')
    if p.get('hysteresis_shadow_trades') is not None:
        _fail('hysteresis_rule_not_specified_by_plan')
    return p

def slot_rank(session_date, symbol):
    """Stable order for competing entries: sha256(session date, symbol, fixed seed)."""
    day = str(session_date)
    if not isinstance(symbol, str) or not symbol:
        raise ValueError('slot_symbol')
    digest = hashlib.sha256(f'{day}|{symbol}|{POLICY["slot_order_seed"]}'.encode()).hexdigest()
    return int(digest[:16], 16)

def price_paise(fill_price):
    """Paise exactly as fees.mjs accepts: a positive price with at most two decimal places."""
    if isinstance(fill_price, bool) or isinstance(fill_price, (bytes, bytearray)):
        raise ValueError('price_requires_positive_paise')
    if isinstance(fill_price, float):
        if not math.isfinite(fill_price):
            raise ValueError('price_requires_positive_paise')
        text = json.dumps(fill_price)
        if text.endswith('.0'):
            text = text[:-2]
    elif isinstance(fill_price, int):
        text = str(fill_price)
    else:
        text = str(fill_price).strip()
    if not _PRICE.fullmatch(text):
        raise ValueError('price_requires_positive_paise')
    whole, _, frac = text.partition('.')
    paise = int(whole) * 100 + int((frac + '00')[:2])
    if paise <= 0:
        raise ValueError('price_requires_positive_paise')
    return paise

def integer_shares(fill_price, target_notional_inr=None):
    """floor(target notional / fill price). Zero means the order cannot buy one share."""
    target = POLICY['target_notional_inr'] if target_notional_inr is None else target_notional_inr
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise ValueError('target_notional')
    return (target * 100) // price_paise(fill_price)

if __name__ == '__main__' and '--print-lock' in __import__('sys').argv:
    print(json.dumps({'files': current_lock(), 'capital_policy_version': POLICY['version']}, indent=2))
else:
    assert_freeze()
