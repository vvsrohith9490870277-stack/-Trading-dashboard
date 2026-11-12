"""Same-session evidence that the live exit path ran in paper_exit_test_* (read-only).

Shared by the Support Agent detector and the independent verifiers. Does not import
production detectors.
"""
import json
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
EXIT_SELFTEST_NS_PREFIX = 'paper_exit_test'


def is_exit_selftest_namespace(ns):
    return str(ns or '').startswith(EXIT_SELFTEST_NS_PREFIX)


def _ist(raw):
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _session_day(raw):
    dt = _ist(raw)
    return dt.date().isoformat() if dt else None


def _selftest_event_is_sell(ev):
    side = str(ev.get('side') or '').upper()
    if side == 'SELL':
        return True
    try:
        data = ev.get('data_json')
        payload = json.loads(data) if isinstance(data, str) else (data or {})
    except Exception:
        payload = ev.get('result') if isinstance(ev.get('result'), dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    result = payload.get('result') if isinstance(payload.get('result'), dict) else payload
    if str(result.get('side') or payload.get('side') or '').upper() == 'SELL':
        if result.get('filled') in (True, 'true', 'True', 1):
            return True
        if ev.get('event_type') in ('paper_fill_attempt', 'paper_order_executed', 'paper_fill', 'paper_fill_attempt') \
                or ev.get('kind') in ('paper_fill_attempt', 'paper_order_executed', 'paper_fill'):
            return True
    return False


def _selftest_event_is_stop(ev):
    kind = str(ev.get('event_type') or ev.get('kind') or ev.get('rule') or '').lower()
    if kind in ('stop_hit', 'target_hit', 'trail_stop_filled', 'trail_touch'):
        return True
    try:
        data = ev.get('data_json')
        payload = json.loads(data) if isinstance(data, str) else (data or ev.get('result') or {})
    except Exception:
        payload = ev or {}
    result = payload.get('result') if isinstance(payload, dict) and isinstance(payload.get('result'), dict) else (payload if isinstance(payload, dict) else {})
    rule = str(result.get('rule') or (payload.get('rule') if isinstance(payload, dict) else '') or ev.get('rule') or '').lower()
    return rule in ('trail_touch', 'stop_hit', 'target_hit', 'trail_stop')


def exit_selftest_evidence(rows, session_day, now_ist):
    """Same-session proof that the live monitor exit path ran in paper_exit_test_*.

    Never treats paper_v1 activity as selftest evidence. A session is never invented.
    """
    sells, stops, commands = [], [], []
    now_ist = _ist(now_ist)
    for ev in rows or []:
        ns = ev.get('namespace')
        if not is_exit_selftest_namespace(ns):
            continue
        at = ev.get('source_at') or ev.get('at_IST') or ev.get('at') or ev.get('filled_at') or ev.get('recorded_at')
        if not at:
            continue
        when = _ist(at)
        if when is None or _session_day(at) != session_day or (now_ist and when > now_ist):
            continue
        kind = str(ev.get('event_type') or ev.get('kind') or ev.get('command') or '')
        cmd = str(ev.get('command') or '').lower()
        if cmd == 'exit-selftest' or kind.lower() in ('exit-selftest', 'exit_selftest'):
            outcome = str(ev.get('outcome') or ev.get('status') or ev.get('result') or 'ok').lower()
            if outcome in ('ok', 'success', 'completed', 'passed', ''):
                commands.append({'at': at, 'namespace': ns, 'command': 'exit-selftest'})
        if _selftest_event_is_sell(ev) or str(ev.get('side') or '').upper() == 'SELL':
            sells.append({'at': at, 'namespace': ns, 'symbol': ev.get('symbol')})
        if _selftest_event_is_stop(ev):
            stops.append({'at': at, 'namespace': ns, 'kind': kind})
    return {
        'exercised': bool(sells or stops or commands),
        'sells': len(sells),
        'stop_or_target_hits': len(stops),
        'exit_selftest_commands': len(commands),
        'namespace_prefix': EXIT_SELFTEST_NS_PREFIX,
        'samples': (sells + stops + commands)[:8],
    }
