"""Event taxonomy for the Support Agent (brief adaptive-support-tuning-01, A1).

Root cause it works around (without touching the frozen Track A runtime):
``audit.py`` ``clean()`` rewrites any ``reason`` not in its allow-list to
``'unclassified_condition'``, so the real reasons behind the 23 unclassified
19-Nov-2026 events were lost before the Support Agent saw them. This module
classifies events from their (event_type, outcome) pair — which ``clean()``
does not rewrite — plus whatever payload fields survive.

Table (keyed on ``(event_type, outcome)``):

- ``host_freeze_detected`` / ``failed`` -> ``box_host_pause`` (WARN, never
  CRITICAL by itself). Payload fields ``started`` / ``stopped`` /
  ``frozen_seconds`` give the pause window; a monitor-staleness gap that
  overlaps a pause window is explained by the pause, not a worker fault.
- ``sessiond_wake`` / ``ok`` -> ``box_host_resume`` (INFO). Pairs with a
  pause; the resume is recovery, not the outage itself.
- ``session_outage_recorded`` / ``failed`` -> ``session_outage``. Severity
  comes from the payload fields that survive ``clean()`` (see
  ``_outage_severity``): a missed trading session (``kind`` in
  ``scan_missing`` / ``eod_missing``, or a ``missed_session`` field) or a
  coverage failure (``checks == 0`` or a ``coverage`` value below full) is
  CRITICAL, otherwise ERROR. When no kind/coverage information survives —
  the shape of the single real 19-Nov outage event — the severity is ERROR,
  matching the severity recorded for that event.

Anything not in the table stays ``unclassified`` and is still reported by
``error_taxonomy_gap``, now with counts per ``(event_type, outcome)`` so the
finding is never opaque again.
"""
import json
from datetime import datetime

# (event_type, outcome) -> (class, severity, explains)
# severity None means "derived from the payload" (see _outage_severity).
_TABLE = {
    ('host_freeze_detected', 'failed'): ('box_host_pause', 'WARN', ['monitor_staleness']),
    ('sessiond_wake', 'ok'): ('box_host_resume', 'INFO', ['box_host_pause']),
    ('session_outage_recorded', 'failed'): ('session_outage', None, ['scan_missed', 'eod_missed']),
}

_MISSED_SESSION_KINDS = ('scan_missing', 'eod_missing')


def _payload(event):
    """Return the event payload dict, from data_json / data / reason fields."""
    raw = event.get('data_json', event.get('data'))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if event.get('reason') is not None:
        return {'reason': event.get('reason')}
    return {}


def _outage_severity(payload):
    """Severity for a session_outage event, from the payload fields that survive audit clean()."""
    kind = payload.get('kind') or payload.get('reason')
    if kind in _MISSED_SESSION_KINDS:
        return 'CRITICAL'
    if payload.get('missed_session'):
        return 'CRITICAL'
    checks = payload.get('checks')
    if checks is not None and checks == 0:
        return 'CRITICAL'
    coverage = payload.get('coverage')
    if isinstance(coverage, (int, float)) and coverage < 1.0:
        return 'CRITICAL'
    return 'ERROR'


def classify_event(event):
    """Classify one event dict -> {class, severity, reason_source, explains}.

    ``event`` may be a paper_events row (``event_type``, ``outcome``,
    ``data_json`` as a JSON string or dict) or a plain dict. Unknown pairs
    return class ``'unclassified'`` with reason_source ``'unknown'``.
    """
    event = event or {}
    event_type = str(event.get('event_type') or '')
    outcome = str(event.get('outcome') or '')
    hit = _TABLE.get((event_type, outcome))
    if hit is None:
        return {
            'class': 'unclassified',
            'severity': str(event.get('severity') or 'WARN'),
            'reason_source': 'unknown',
            'explains': [],
        }
    cls, severity, explains = hit
    if cls == 'session_outage':
        return {
            'class': cls,
            'severity': _outage_severity(_payload(event)),
            'reason_source': 'payload',
            'explains': list(explains),
        }
    return {
        'class': cls,
        'severity': severity,
        'reason_source': 'event_type',
        'explains': list(explains),
    }


def _parse_ts(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def pause_windows(events):
    """Pause windows from host_freeze_detected events: [{started, stopped, frozen_seconds}].

    ``started`` / ``stopped`` come from the payload when present; otherwise the
    window ends at the event's ``source_at`` and starts ``frozen_seconds`` earlier.
    """
    windows = []
    for event in events or []:
        if str(event.get('event_type') or '') != 'host_freeze_detected':
            continue
        payload = _payload(event)
        frozen = payload.get('frozen_seconds', payload.get('age_seconds'))
        try:
            frozen = float(frozen) if frozen is not None else None
        except Exception:
            frozen = None
        stopped = _parse_ts(payload.get('stopped')) or _parse_ts(event.get('source_at'))
        started = _parse_ts(payload.get('started'))
        if started is None and stopped is not None and frozen is not None:
            from datetime import timedelta
            started = stopped - timedelta(seconds=frozen)
        if started is None or stopped is None:
            continue
        windows.append({
            'started': started.isoformat(),
            'stopped': stopped.isoformat(),
            'frozen_seconds': frozen,
            'event_id': event.get('event_id'),
        })
    return windows


def total_paused_seconds(events):
    """Total paused seconds across host_freeze_detected events in the session."""
    total = 0.0
    for window in pause_windows(events):
        frozen = window.get('frozen_seconds')
        if frozen is not None:
            total += float(frozen)
            continue
        start = _parse_ts(window.get('started'))
        stop = _parse_ts(window.get('stopped'))
        if start is not None and stop is not None:
            total += (stop - start).total_seconds()
    return total


def gap_explained_by_host_pause(gap_from, gap_to, windows):
    """True when the [gap_from, gap_to] interval overlaps any pause window."""
    start = _parse_ts(gap_from)
    end = _parse_ts(gap_to)
    if start is None or end is None:
        return False
    for window in windows or []:
        w_start = _parse_ts(window.get('started'))
        w_stop = _parse_ts(window.get('stopped'))
        if w_start is None or w_stop is None:
            continue
        if start <= w_stop and end >= w_start:
            return True
    return False
