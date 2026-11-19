"""Stage A and Stage B reconciliation harness for the Frozen Strategies upgrade.

Baseline: frozen signals + legacy LTP fills + freeze sizing (floor(target_notional_inr / price)) and cost rules.
Warmup: 260 point-in-time bars.
Every Stage B divergence between baseline and the upgraded engine must map to a named cause
that exists in code today. Live acceptance is never claimed from unit tests or without
authenticated Turso month history and NSE bhavcopy archives.
"""
import json, os
from pathlib import Path
import bhavcopy_compare as bc
import capital as cap
import runtime_mode as rm

# Freeze plan B1: 260-bar point-in-time warmup. Value lives in config.json reconciliation_warmup_bars.
WARMUP_BARS = int(json.loads((Path(__file__).resolve().parent / 'config.json').read_text())['reconciliation_warmup_bars'])
BASELINE_DEFINITION = 'frozen_signals_plus_ltp_fills_plus_freeze_sizing_and_costs'

NAMED_CAUSES = frozenset({
    'CIRCUIT_LOCKED',
    'SERIES_TRANSITION_BE_BZ',
    'ASM_GSM_RESTRICTION',
    'SLEEVE_SLOT_FULL',
    'ISIN_CAP',
    'INSUFFICIENT_CASH',
    'UNAFFORDABLE_ZERO_SHARES',
    'ADV_PARTICIPATION_CAP',
    'ADV_FILTER_SKIP',
    'KILL_PAUSE',
    'LOCK_UNKNOWN',
    'STATE_SCALING',
    'CORP_ACTION_MANUAL_REVIEW',
    'CASH_CREDITED',
    'OFFICIAL_OPEN_DIFF',
    'FALLBACK_OPEN',
    'OPEN_MATCH',
    'BHAV_OPEN_MISSING',
    'FEED_OPEN_MISSING',
    'FEED_OPEN',
    'AUCTION_CAPTURE',
    'LTP_LEGACY',
    'trail_gap',
    'trail_touch',
})


def check_live_prerequisites(env_path=None, bhavcopy_dir=None):
    """Check whether live Turso credentials and NSE bhavcopy files are present."""
    env_file = Path(env_path or os.environ.get('PAPER_ENV_FILE') or '/workspace/sahil-fyers-turso/.env')  # box default
    blockers = []
    if env_file.exists():
        text = env_file.read_text(encoding='utf8', errors='ignore')
        if 'TURSO_DATABASE_URL=' not in text or 'TURSO_AUTH_TOKEN=' not in text:
            blockers.append('incomplete_turso_credentials_env')
    elif rm.is_render() and os.environ.get('TURSO_DATABASE_URL') and os.environ.get('TURSO_AUTH_TOKEN'):
        pass  # render mode: Turso creds come from process.env, no file needed
    else:
        blockers.append('missing_turso_credentials_env')
    bhav_dir = Path(bhavcopy_dir) if bhavcopy_dir else None
    if not bhav_dir or not bhav_dir.exists() or not list(bhav_dir.glob('*.csv')):
        blockers.append('missing_nse_bhavcopy_month_archive')
    return {
        'ready_for_live_month_acceptance': len(blockers) == 0,
        'external_blockers': blockers,
    }


def _sig_key(row):
    return (str(row['bar_date']), int(row['strategy']), str(row['symbol']), str(row['side']).upper())


def reconcile_stage_a(baseline_signals, candidate_signals, warmup_bars_by_symbol=None, live_verified=False):
    """Compare point-in-time signals after a 260-bar warmup. Never claims live acceptance on fixtures."""
    warmup_bars_by_symbol = warmup_bars_by_symbol or {}
    short_warmup = sorted(sym for sym, n in warmup_bars_by_symbol.items() if int(n) < WARMUP_BARS)
    base_keys = {_sig_key(r) for r in baseline_signals}
    cand_keys = {_sig_key(r) for r in candidate_signals}
    matched = sorted(base_keys & cand_keys)
    missing = sorted(base_keys - cand_keys)
    extra = sorted(cand_keys - base_keys)
    fixture_match = len(missing) == 0 and len(extra) == 0 and len(short_warmup) == 0
    return {
        'stage': 'Stage_A',
        'warmup_required_bars': WARMUP_BARS,
        'short_warmup_symbols': short_warmup,
        'baseline_signal_count': len(base_keys),
        'candidate_signal_count': len(cand_keys),
        'matched_signal_count': len(matched),
        'missing_in_candidate': [
            {'bar_date': b, 'strategy': s, 'symbol': sym, 'side': side}
            for b, s, sym, side in missing
        ],
        'extra_in_candidate': [
            {'bar_date': b, 'strategy': s, 'symbol': sym, 'side': side}
            for b, s, sym, side in extra
        ],
        'local_fixture_signal_match': fixture_match,
        'live_month_acceptance_claimed': bool(live_verified and fixture_match),
    }


def classify_divergence(base_row, cand_row, bhav_by_symbol=None):
    """Map one baseline-vs-candidate fill/decision difference to an existing named cause."""
    bhav_by_symbol = bhav_by_symbol or {}
    if cand_row is None:
        return None
    cause = cand_row.get('cause')
    rule = cand_row.get('rule')
    path = cand_row.get('execution_path')
    payload = cand_row.get('payload') or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if cause in NAMED_CAUSES:
        return cause
    if rule in ('trail_gap', 'trail_touch'):
        return rule
    if payload.get('lock_unknown') is True:
        return 'LOCK_UNKNOWN'
    sym = cand_row.get('symbol') or (base_row or {}).get('symbol')
    if sym and sym in bhav_by_symbol:
        bhav_cause = bhav_by_symbol[sym].get('cause')
        if bhav_cause in NAMED_CAUSES and bhav_cause != 'OPEN_MATCH':
            return bhav_cause
    if path in ('FEED_OPEN', 'AUCTION_CAPTURE', 'FALLBACK_OPEN'):
        if path == 'FEED_OPEN' and sym in bhav_by_symbol and bhav_by_symbol[sym].get('cause') == 'OPEN_MATCH':
            return 'OPEN_MATCH'
        return path
    if path == 'LTP_LEGACY' and base_row and base_row.get('price') == payload.get('price'):
        return 'LTP_LEGACY'
    return None


def reconcile_stage_b(baseline_fills, candidate_decisions, bhavcopy_rows=None, live_verified=False):
    """Reconcile baseline LTP fills against candidate decisions and bhavcopy rows."""
    bhav_by_symbol = {r['symbol']: r for r in (bhavcopy_rows or []) if 'symbol' in r}
    cand_by_corr = {str(r['correlation_id']): r for r in candidate_decisions if r.get('correlation_id')}
    divergences = []
    unmapped = []
    matched_identical = 0
    for base in baseline_fills:
        cid = str(base['correlation_id'])
        cand = cand_by_corr.get(cid)
        payload = (cand or {}).get('payload') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        same_fill = (
            cand is not None
            and cand.get('cause') == 'filled'
            and cand.get('execution_path') == 'LTP_LEGACY'
            and cand.get('rule') not in ('trail_gap', 'trail_touch')
            and not payload.get('lock_unknown')
            and payload.get('quantity') == base.get('quantity')
            and payload.get('price') == base.get('price')
        )
        if same_fill:
            matched_identical += 1
            continue
        named = classify_divergence(base, cand, bhav_by_symbol)
        entry = {
            'correlation_id': cid,
            'symbol': base.get('symbol'),
            'strategy': base.get('strategy'),
            'named_cause': named,
        }
        if named is None:
            unmapped.append(entry)
        else:
            divergences.append(entry)
    for cid, cand in cand_by_corr.items():
        if any(str(b['correlation_id']) == cid for b in baseline_fills):
            continue
        named = classify_divergence(None, cand, bhav_by_symbol)
        entry = {
            'correlation_id': cid,
            'symbol': cand.get('symbol'),
            'strategy': cand.get('strategy'),
            'named_cause': named,
        }
        if named is None:
            unmapped.append(entry)
        else:
            divergences.append(entry)
    by_cause = {}
    for d in divergences:
        by_cause[d['named_cause']] = by_cause.get(d['named_cause'], 0) + 1
    return {
        'stage': 'Stage_B',
        'baseline_definition': BASELINE_DEFINITION,
        'capital_policy_version': cap.POLICY['version'],
        'matched_identical_count': matched_identical,
        'divergence_count': len(divergences),
        'divergences_by_cause': by_cause,
        'divergences': divergences,
        'unmapped_divergences': unmapped,
        'all_divergences_named': len(unmapped) == 0,
        'live_month_acceptance_claimed': bool(live_verified and len(unmapped) == 0),
    }


def run_harness(baseline_signals, candidate_signals, baseline_fills, candidate_decisions, warmup_bars_by_symbol=None, feed_opens=None, bhav_rows=None, trade_date=None, env_path=None, bhavcopy_dir=None):
    """Run Stage A and Stage B reconciliation and report external live-data blockers explicitly."""
    prereq = check_live_prerequisites(env_path=env_path, bhavcopy_dir=bhavcopy_dir)
    live_ok = prereq['ready_for_live_month_acceptance']
    if feed_opens is not None and bhav_rows is not None and not trade_date:
        raise ValueError('trade_date_required_for_bhavcopy_compare')
    bhav_compared = bc.compare_rows(feed_opens or [], bhav_rows or [], trade_date) if (feed_opens is not None and bhav_rows is not None) else []
    stage_a = reconcile_stage_a(baseline_signals, candidate_signals, warmup_bars_by_symbol=warmup_bars_by_symbol, live_verified=live_ok)
    stage_b = reconcile_stage_b(baseline_fills, candidate_decisions, bhavcopy_rows=bhav_compared, live_verified=live_ok)
    return {
        'status': 'LIVE_ACCEPTANCE_READY' if live_ok else 'EXTERNAL_BLOCKER_LIVE_DATA_AND_BHAVCOPY_REQUIRED',
        'acceptance_claimed': False if not live_ok else (stage_a['live_month_acceptance_claimed'] and stage_b['live_month_acceptance_claimed']),
        'prerequisites': prereq,
        'stage_a': stage_a,
        'stage_b': stage_b,
    }
