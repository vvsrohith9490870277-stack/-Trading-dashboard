"""Compact machine-readable Support Agent summary (brief adaptive-support-tuning-01, A4).

``build_compact(findings, session_day, namespace, runner, not_evaluable, now_ist)``
returns one small dict (schema_version 1, stable key order, at most 4 KB)
with no secrets, no credential-bearing URLs and no raw payloads — only
whitelisted fields (detector, severity, action, since_ist, finding_id,
reason) are copied out of the findings.

``status`` is CRITICAL when any critical finding exists, DEGRADED when any
non-critical finding or any not_evaluable entry exists, else OK. When the
finding lists would push the JSON past 4 KB, warnings are cut first, then
critical findings, then not_evaluable entries; the dropped count is reported
as ``truncated`` (critical findings are kept as long as possible).
"""
import json
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(minutes=330))
MAX_BYTES = 4096
SCHEMA_VERSION = 1

_DISPLAY = {
    'SEV1': 'critical', 'CRITICAL': 'critical',
    'SEV2': 'error', 'ERROR': 'error',
    'SEV3': 'warn', 'WARN': 'warn',
    'SEV4': 'info', 'INFO': 'info',
}

_DISPLAY_SEVERITY = {
    'critical': 'CRITICAL', 'error': 'ERROR', 'warn': 'WARN', 'info': 'INFO',
}


def _action_of(finding):
    details = finding.get('details') or {}
    action = details.get('action') or finding.get('action') or ''
    return str(action)[:140]


def _since_of(finding):
    return str(finding.get('first_event_at')
               or (finding.get('details') or {}).get('first_at') or '')


def _finding_id_of(finding):
    return str(finding.get('finding_id') or '')


def build_compact(findings, session_day, namespace, runner, not_evaluable, now_ist):
    """Build the compact summary dict for one session day."""
    if now_ist is None:
        generated = datetime.now(IST).isoformat()
    elif isinstance(now_ist, datetime):
        generated = now_ist.isoformat()
    else:
        generated = str(now_ist)
    runner = runner or {}
    runner_out = {
        'expected': runner.get('expected'),
        'health_ok': runner.get('health_ok'),
        'mode': runner.get('mode'),
    }
    critical = []
    warnings = []
    counts = {'critical': 0, 'error': 0, 'warn': 0, 'info': 0, 'not_evaluable': 0}
    for finding in findings or []:
        bucket = _DISPLAY.get(str(finding.get('severity') or ''), 'warn')
        counts[bucket] += 1
        if bucket == 'critical':
            critical.append({
                'detector': str(finding.get('detector') or ''),
                'severity': 'CRITICAL',
                'action': _action_of(finding),
                'since_ist': _since_of(finding),
                'finding_id': _finding_id_of(finding),
            })
        else:
            warnings.append({
                'detector': str(finding.get('detector') or ''),
                'severity': _DISPLAY_SEVERITY[bucket],
                'action': _action_of(finding),
            })
    ne_list = [{'detector': str(item.get('detector') or ''),
                'reason': str(item.get('reason') or '')}
               for item in (not_evaluable or [])]
    counts['not_evaluable'] = len(ne_list)
    if counts['critical'] > 0:
        status = 'CRITICAL'
    elif warnings or ne_list:
        status = 'DEGRADED'
    else:
        status = 'OK'
    compact = {
        'schema_version': SCHEMA_VERSION,
        'generated_at_ist': generated,
        'session_day': str(session_day),
        'namespace': str(namespace),
        'runner': runner_out,
        'status': status,
        'counts': counts,
        'critical': critical,
        'warnings': warnings,
        'not_evaluable': ne_list,
        'read_only': True,
        'actions_taken': 'none',
    }
    truncated = 0
    # Cut warnings first, then critical findings, then not_evaluable entries.
    while len(json.dumps(compact)) > MAX_BYTES and (warnings or critical or ne_list):
        if warnings:
            warnings.pop()
            compact['warnings'] = warnings
        elif critical:
            critical.pop()
            compact['critical'] = critical
        else:
            ne_list.pop()
            compact['not_evaluable'] = ne_list
        truncated += 1
    if truncated:
        compact['truncated'] = truncated
    return compact
