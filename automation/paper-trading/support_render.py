"""Render / namespace awareness for the Support Agent (brief adaptive-support-tuning-01, A3).

Read-only by construction: the only Turso access is SELECT statements issued
through the injected ``query_fn`` (in production, ``live/ro_client.py``
``TursoRO.query``), and the only HTTP access is a GET of the Render
``/health`` endpoint with a 10 s timeout and one retry for free-tier cold
start. Nothing here writes anywhere.

Table / column names mirror ``render_schema.sql`` on ``feat/render-deploy``
(they exist in production Turso, not in ``schema.sql`` on main)::

    paper_runner_lease(namespace, owner_id, owner_kind, epoch, acquired_unix,
                       expires_unix, heartbeat_at, detail_json)
    paper_job_runs(namespace, job, session_day, attempt, status, owner_id,
                   claimed_unix, lease_until_unix, started_at, finished_at,
                   exit_code, result_json)
    paper_runtime_kv(scope, key, value, updated_at)

The active runner flag is ``key='active_runner'`` in the namespace's scope.

A ``null`` health field means ``not_evaluable``, never an alarm and never a
pass. When the deployed schema differs from the mirror above, the query layer
reports ``schema_mismatch`` and the check reports
``not_evaluable: lease_schema_mismatch`` instead of guessing.

``evaluate()`` is a pure function: ``health`` is the parsed ``/health`` JSON
(or None when unreachable / non-200), ``kv`` / ``lease`` / ``jobs`` are the
read-only query results (None when the tables could not be read at all),
``now_ist`` is a datetime, ``namespace`` is the configured namespace
(``paper_v2``), and ``expected_session`` is True when a trading session is
expected / in progress now (IST session window on a trading day). It returns
a list of finding dicts ``{detector, severity, details}``; not-evaluable
items carry severity ``'NOT_EVALUABLE'`` and ``details['reason']``.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import support_severity as sev

ROOT = Path(__file__).resolve().parent
try:
    _CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
    STALE_THRESHOLD_SECONDS = int(_CFG['worker_stale_seconds'])
except Exception:
    # No threshold to reuse: the heartbeat-age check reports not_evaluable.
    STALE_THRESHOLD_SECONDS = None

DEFAULT_HEALTH_URL = 'https://signal-atlas-paper.onrender.com/health'

# Mirror of render_schema.sql on feat/render-deploy (see module docstring).
RENDER_TABLES = {
    'paper_runner_lease': ['namespace', 'owner_id', 'owner_kind', 'epoch',
                           'acquired_unix', 'expires_unix', 'heartbeat_at', 'detail_json'],
    'paper_job_runs': ['namespace', 'job', 'session_day', 'attempt', 'status',
                       'owner_id', 'claimed_unix', 'lease_until_unix', 'started_at',
                       'finished_at', 'exit_code', 'result_json'],
    'paper_runtime_kv': ['scope', 'key', 'value', 'updated_at'],
}


def _finding(detector, severity, details):
    rule = sev.rule_for(detector)
    merged = dict(details or {})
    if rule is not None and 'action' not in merged:
        merged['action'] = rule['action']
    return {'detector': detector, 'severity': severity, 'details': merged}


def _not_evaluable(detector, reason):
    return {'detector': detector, 'severity': 'NOT_EVALUABLE',
            'details': {'reason': reason}}


def _parse_now(now_ist):
    if isinstance(now_ist, datetime):
        return now_ist
    try:
        return datetime.fromisoformat(str(now_ist).replace('Z', '+00:00'))
    except Exception:
        return None


def fetch_health(url, opener=None, timeout=10):
    """GET the Render /health endpoint (10 s timeout, one retry).

    Returns ``(status, payload)`` where payload is the parsed JSON dict, or
    ``(None, None)`` when the endpoint is unreachable after the retry.
    A non-200 status returns ``(status, None)``. ``opener`` is injected in
    tests; it defaults to ``urllib.request.urlopen``.
    """
    open_url = opener or urllib.request.urlopen
    last_status = None
    for _attempt in range(2):
        try:
            with open_url(url, timeout=timeout) as resp:
                status = int(getattr(resp, 'status', 0) or 0)
                last_status = status
                if status != 200:
                    if status >= 500:
                        continue
                    return status, None
                try:
                    payload = json.loads(resp.read().decode('utf8'))
                except Exception:
                    return status, None
                return status, payload if isinstance(payload, dict) else None
        except Exception:
            last_status = None
            continue
    return None, None


def query_render_state(query_fn, namespace, session_day):
    """Read-only reads of the Render state tables for one namespace + day.

    ``query_fn(sql, args)`` returns row dicts. Returns ``{'kv', 'lease',
    'jobs', 'tables_absent', 'schema_mismatch'}``. A missing table sets
    ``tables_absent``; any other read failure (for example a missing column,
    which means the deployed schema differs from the mirror) sets
    ``schema_mismatch``; both leave kv / lease / jobs as None.
    """
    result = {'kv': None, 'lease': None, 'jobs': None,
              'tables_absent': False, 'schema_mismatch': False}
    try:
        kv_rows = query_fn(
            'SELECT key, value FROM paper_runtime_kv WHERE scope = ? AND key = ?',
            (namespace, 'active_runner'))
        result['kv'] = {str(r['key']): r['value'] for r in kv_rows}
        lease_rows = query_fn(
            'SELECT namespace, owner_id, owner_kind, epoch, acquired_unix, '
            'expires_unix, heartbeat_at, detail_json FROM paper_runner_lease '
            'WHERE namespace = ?',
            (namespace,))
        result['lease'] = dict(lease_rows[0]) if lease_rows else None
        job_rows = query_fn(
            'SELECT namespace, job, session_day, attempt, status, owner_id, '
            'claimed_unix, lease_until_unix, started_at, finished_at, exit_code, '
            'result_json FROM paper_job_runs WHERE namespace = ? AND session_day = ?',
            (namespace, session_day))
        result['jobs'] = [dict(r) for r in job_rows]
    except Exception as ex:
        msg = str(ex).lower()
        if 'no such table' in msg or 'does not exist' in msg:
            result['tables_absent'] = True
        else:
            result['schema_mismatch'] = True
        result['kv'] = None
        result['lease'] = None
        result['jobs'] = None
    return result


def _is_scan_or_eod(job):
    name = str(job or '').lower()
    return 'scan' in name or 'eod' in name


def evaluate(health, kv, lease, jobs, now_ist, namespace, expected_session):
    """Pure Render / namespace checks. See the module docstring for inputs."""
    out = []
    now = _parse_now(now_ist)

    # Tables unreadable at all: nothing about the runner can be judged.
    if kv is None and lease is None and jobs is None:
        return [_not_evaluable('render_tables', 'render_tables_absent')]
    if kv is None:
        return [_not_evaluable('render_tables', 'render_tables_absent')]
    if lease is None:
        out.append(_not_evaluable('render_lease', 'lease_row_missing'))
    if jobs is None:
        out.append(_not_evaluable('render_jobs', 'render_tables_absent'))
        jobs = []

    expected_runner = kv.get('active_runner')
    if expected_runner is None:
        out.append(_not_evaluable('render_expected_runner', 'active_runner_missing'))
    render_expected = expected_runner == 'render'

    health_ns = health.get('namespace') if isinstance(health, dict) else None
    if isinstance(health, dict) and health_ns and health_ns != namespace:
        # The health endpoint serves another namespace (for example the trial
        # book paper_render_trial_v1 while this namespace is paper_v2): its
        # worker / lease state says nothing about this namespace.
        out.append(_not_evaluable('render_namespace', 'render_namespace_mismatch'))
        return out

    # --- /health reachability and ok flag ---
    if health is None:
        if expected_session:
            out.append(_finding(
                'render_health_unreachable',
                'SEV1' if render_expected else 'SEV3',
                {'reason': 'health_unreachable_or_non_200',
                 'expected_runner': expected_runner}))
        return out
    ok = health.get('ok')
    if ok is None:
        out.append(_not_evaluable('render_health_ok', 'field_null'))
    elif ok is not True and expected_session:
        out.append(_finding(
            'render_health_not_ok',
            'SEV1' if render_expected else 'SEV3',
            {'ok': ok, 'expected_runner': expected_runner}))

    # --- Worker (only meaningful when Render runs this namespace) ---
    worker = health.get('worker')
    if render_expected:
        if worker is None:
            out.append(_not_evaluable('render_worker', 'field_null'))
        else:
            session_open = health.get('session_open')
            state = worker.get('state')
            if session_open and state != 'running':
                out.append(_finding('render_worker_stopped', 'SEV1', {
                    'worker_state': state, 'session_open': session_open}))
            restarts = worker.get('restarts_10m')
            if restarts is None:
                out.append(_not_evaluable('render_worker_restarts', 'field_null'))
            elif restarts > 0:
                out.append(_finding('render_worker_restarts', 'WARN', {
                    'restarts_10m': restarts}))
            age = worker.get('last_heartbeat_age_s')
            if age is None:
                out.append(_not_evaluable('render_worker_heartbeat', 'field_null'))
            elif STALE_THRESHOLD_SECONDS is None:
                out.append(_not_evaluable('render_worker_heartbeat', 'policy_value_missing'))
            elif age > STALE_THRESHOLD_SECONDS:
                # A2 condition 1: heartbeat gap not explained by a host pause.
                out.append(_finding('monitor_staleness', 'SEV1', {
                    'source': 'render_worker',
                    'last_heartbeat_age_s': age,
                    'threshold_seconds': STALE_THRESHOLD_SECONDS}))

    # --- Lease ---
    if isinstance(lease, dict):
        expires = lease.get('expires_unix')
        if expires is None:
            out.append(_not_evaluable('render_lease', 'field_null'))
        elif now is not None and render_expected and int(expires) < int(now.timestamp()):
            out.append(_finding('lease_conflict', 'SEV1', {
                'reason': 'lease_expired', 'expires_unix': expires,
                'now_unix': int(now.timestamp())}))
        owner_kind = lease.get('owner_kind')
        if expected_runner and owner_kind and owner_kind != expected_runner:
            out.append(_finding('lease_conflict', 'SEV1', {
                'reason': 'lease_kv_mismatch', 'owner_kind': owner_kind,
                'active_runner': expected_runner}))

    # --- Box heartbeat while Render is active ---
    conflict = health.get('box_conflict_check') if isinstance(health, dict) else None
    if isinstance(conflict, dict):
        present = conflict.get('paper_v2_heartbeat_present')
        if present is None:
            out.append(_not_evaluable('render_box_conflict_check', 'field_null'))
        elif present is True and render_expected:
            out.append(_finding('lease_conflict', 'SEV1', {
                'reason': 'box_heartbeat_while_render_active'}))

    # --- Job runs ---
    owners_by_job = {}
    for row in jobs or []:
        key = (row.get('job'), row.get('session_day'))
        owners_by_job.setdefault(key, set()).add(row.get('owner_id'))
        status = row.get('status')
        exit_code = row.get('exit_code')
        if status == 'failed' or (exit_code is not None and exit_code != 0):
            out.append(_finding(
                'render_job_failed',
                'SEV1' if _is_scan_or_eod(row.get('job')) else 'SEV3',
                {'job': row.get('job'), 'session_day': row.get('session_day'),
                 'status': status, 'exit_code': exit_code,
                 'attempt': row.get('attempt')}))
    for (job, day), owners in sorted(owners_by_job.items(), key=lambda kv_: str(kv_[0])):
        if len([o for o in owners if o is not None]) > 1:
            out.append(_finding('lease_conflict', 'SEV1', {
                'reason': 'two_owners_same_job_day', 'job': job,
                'session_day': day, 'owners': sorted(str(o) for o in owners if o is not None)}))

    # --- Fyers budget / auth circuit feeds fyers_auth_or_budget_block ---
    auth = health.get('auth') if isinstance(health, dict) else None
    if isinstance(auth, dict) and auth.get('circuit_open') is True:
        out.append(_finding('fyers_auth_or_budget_block', 'SEV1', {
            'reason': 'auth_circuit_open', 'source': 'render_health'}))
    budget = health.get('fyers_budget') if isinstance(health, dict) else None
    if isinstance(budget, dict):
        used = budget.get('day_used')
        limit = budget.get('per_day_limit')
        if used is None:
            out.append(_not_evaluable('render_fyers_budget', 'field_null'))
        elif limit is not None and used >= limit:
            out.append(_finding('fyers_auth_or_budget_block', 'SEV1', {
                'reason': 'fyers_day_budget_exhausted', 'source': 'render_health',
                'day_used': used, 'per_day_limit': limit}))

    # --- Calendar horizon: no senior-set number, so always not_evaluable ---
    days_left = health.get('calendar_days_left') if isinstance(health, dict) else None
    if days_left is None:
        out.append(_not_evaluable('render_calendar_days_left', 'field_null'))
    else:
        out.append(_not_evaluable('render_calendar_days_left', 'policy_value_missing'))

    return out
