"""Same-session evidence that the live exit path ran in paper_exit_test_* (read-only).

Shared by the Support Agent detector and the independent verifiers. Does not import
production detectors.

Exit-selftest often cleans Turso paper_exit_test_* rows afterwards. A sanitized
durable JSON (reports/ or state/) plus engine.jsonl rows must still count, even
when those log rows were merged into the snapshot under paper_v1 and the real
namespace/command live only inside data_json.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
EXIT_SELFTEST_NS_PREFIX = 'paper_exit_test'
DURABLE_ENV = 'SUPPORT_EXIT_SELFTEST_EVIDENCE'


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


def _payload(ev):
    """Return (payload, result) from a snapshot row or a raw engine.jsonl object."""
    try:
        data = ev.get('data_json')
        payload = json.loads(data) if isinstance(data, str) else (data or {})
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    result = ev.get('result') if isinstance(ev.get('result'), dict) else None
    if result is None and isinstance(payload.get('result'), dict):
        result = payload['result']
    if result is None:
        result = {}
    return payload, result


def _event_namespace(ev):
    """Use the payload namespace when a merger remapped paper_exit_test_* onto paper_v1."""
    payload, result = _payload(ev)
    inner = payload.get('namespace') or result.get('namespace')
    if is_exit_selftest_namespace(inner):
        return inner
    return ev.get('namespace') or inner


def _selftest_event_is_sell(ev):
    side = str(ev.get('side') or '').upper()
    if side == 'SELL':
        return True
    payload, result = _payload(ev)
    if str(result.get('side') or payload.get('side') or '').upper() != 'SELL':
        return False
    if result.get('filled') in (True, 'true', 'True', 1):
        return True
    kind = ev.get('event_type') or ev.get('kind') or payload.get('kind')
    return kind in ('paper_fill_attempt', 'paper_order_executed', 'paper_fill')


def _selftest_event_is_stop(ev):
    kind = str(ev.get('event_type') or ev.get('kind') or ev.get('rule') or '').lower()
    if kind in ('stop_hit', 'target_hit', 'trail_stop_filled', 'trail_touch'):
        return True
    payload, result = _payload(ev)
    rule = str(result.get('rule') or payload.get('rule') or ev.get('rule') or '').lower()
    return rule in ('trail_touch', 'stop_hit', 'target_hit', 'trail_stop')


def empty_selftest_evidence():
    return {
        'exercised': False,
        'sells': 0,
        'stop_or_target_hits': 0,
        'exit_selftest_commands': 0,
        'namespace_prefix': EXIT_SELFTEST_NS_PREFIX,
        'samples': [],
    }


def exit_selftest_evidence(rows, session_day, now_ist):
    """Same-session proof that the live monitor exit path ran in paper_exit_test_*.

    Never treats paper_v1 activity as selftest evidence. A session is never invented.
    Namespace and command may live on the row or inside data_json / result (engine logs).
    """
    sells, stops, commands = [], [], []
    now_ist = _ist(now_ist)
    for ev in rows or []:
        ns = _event_namespace(ev)
        if not is_exit_selftest_namespace(ns):
            continue
        payload, result = _payload(ev)
        at = (ev.get('source_at') or ev.get('at_IST') or ev.get('at')
              or ev.get('filled_at') or ev.get('recorded_at') or payload.get('at_IST'))
        if not at:
            continue
        when = _ist(at)
        if when is None or _session_day(at) != session_day or (now_ist and when > now_ist):
            continue
        kind = str(ev.get('event_type') or ev.get('kind') or payload.get('kind') or ev.get('command') or '')
        cmd = str(ev.get('command') or payload.get('command') or result.get('command') or '').lower()
        if cmd == 'exit-selftest' or kind.lower() in ('exit-selftest', 'exit_selftest'):
            raw_outcome = ev.get('outcome') or ev.get('status') or payload.get('status') or payload.get('outcome')
            if isinstance(ev.get('result'), str):
                raw_outcome = raw_outcome or ev.get('result')
            outcome = str(raw_outcome or 'ok').lower()
            if outcome in ('ok', 'success', 'completed', 'passed', ''):
                commands.append({'at': at, 'namespace': ns, 'command': 'exit-selftest'})
        if _selftest_event_is_sell(ev) or str(ev.get('side') or '').upper() == 'SELL':
            sells.append({'at': at, 'namespace': ns, 'symbol': ev.get('symbol') or payload.get('symbol') or result.get('symbol')})
        if _selftest_event_is_stop(ev):
            stops.append({'at': at, 'namespace': ns, 'kind': kind or result.get('rule')})
    return {
        'exercised': bool(sells or stops or commands),
        'sells': len(sells),
        'stop_or_target_hits': len(stops),
        'exit_selftest_commands': len(commands),
        'namespace_prefix': EXIT_SELFTEST_NS_PREFIX,
        'samples': (sells + stops + commands)[:8],
    }


def combine_selftest_evidence(*parts):
    """Union of independent evidence sources; counts are the max so a source is not double-counted."""
    parts = [p for p in parts if p]
    if not parts:
        return empty_selftest_evidence()
    samples = []
    seen = set()
    for p in parts:
        for s in p.get('samples') or []:
            key = json.dumps(s, default=str, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            samples.append(s)
    out = empty_selftest_evidence()
    out['exercised'] = any(bool(p.get('exercised')) for p in parts)
    out['sells'] = max(int(p.get('sells') or 0) for p in parts)
    out['stop_or_target_hits'] = max(int(p.get('stop_or_target_hits') or 0) for p in parts)
    out['exit_selftest_commands'] = max(int(p.get('exit_selftest_commands') or 0) for p in parts)
    out['samples'] = samples[:8]
    if any(p.get('durable') for p in parts):
        out['durable'] = True
    return out


def durable_evidence_paths(session_day, extra=None):
    paths = []
    if extra:
        paths.append(Path(extra))
    env = os.environ.get(DURABLE_ENV)
    if env:
        paths.append(Path(env))
    report_root = Path(os.environ.get('SUPPORT_REPORT_DIR', '/workspace/reports/support-agent'))
    paths.append(report_root / session_day / 'exit-selftest-evidence.json')
    state_root = Path(os.environ.get('ADAPTIVE_LIVE_STATE', '/workspace/adaptive-live/state'))
    paths.append(state_root / 'support' / f'exit-selftest-evidence-{session_day}.json')
    # Preserve order, drop duplicates.
    out, seen = [], set()
    for p in paths:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


def load_durable_evidence(session_day, path=None):
    """Read a sanitized durable record for this session, or None if absent/mismatched."""
    for p in durable_evidence_paths(session_day, extra=path):
        if not p.exists() or not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding='utf8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get('session_day') and data.get('session_day') != session_day:
            continue
        ev = empty_selftest_evidence()
        ev['exercised'] = bool(data.get('exercised'))
        ev['sells'] = int(data.get('sells') or 0)
        ev['stop_or_target_hits'] = int(data.get('stop_or_target_hits') or 0)
        ev['exit_selftest_commands'] = int(data.get('exit_selftest_commands') or 0)
        ev['samples'] = list(data.get('samples') or [])[:8]
        ev['durable'] = True
        ev['source'] = str(p)
        return ev
    return None


def write_durable_evidence(path, evidence, session_day=None):
    """Persist a sanitized record (no secrets, no paper_v1 rows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    for s in evidence.get('samples') or []:
        if not isinstance(s, dict):
            continue
        samples.append({k: s[k] for k in ('at', 'namespace', 'command', 'symbol', 'kind') if k in s})
    payload = {
        'session_day': session_day or evidence.get('session_day'),
        'namespace_prefix': EXIT_SELFTEST_NS_PREFIX,
        'exercised': bool(evidence.get('exercised')),
        'sells': int(evidence.get('sells') or 0),
        'stop_or_target_hits': int(evidence.get('stop_or_target_hits') or 0),
        'exit_selftest_commands': int(evidence.get('exit_selftest_commands') or 0),
        'samples': samples[:8],
        'source': evidence.get('source') or 'support_agent_exit_selftest',
        'durable': True,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + '\n', encoding='utf8')
    return payload
