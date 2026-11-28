"""Boot-time one-shot actions for the Render supervisor (muse-tonight-20-nov
Tasks 1-2).

Pure helpers the supervisor wires into its first active tick:

- Task 1 (quote probe): env RENDER_QUOTE_PROBE_ON_BOOT=1 runs one real
  Fyers quote batch for RELIANCE + TCS in a short-lived child
  (render_quote_probe.py) and records the result in paper_runtime_kv
  (key 'quote_probe') and in an audit event 'render_quote_probe'.
- Task 2 (run-once): env RENDER_RUN_ONCE=scan|eod runs that one job
  through the normal job runner even though it is in
  RENDER_DISABLED_JOBS, and stores a done-marker keyed by date+job so
  a restart never repeats it.

Both refuse unless RENDER_MODE=trial AND the resolved namespace is
exactly paper_render_trial_v1 (brief section 3); a refusal never
writes to the store.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')

TRIAL_NAMESPACE = 'paper_render_trial_v1'
PROBE_SYMBOLS = ('RELIANCE', 'TCS')
PROBE_ENV = 'RENDER_QUOTE_PROBE_ON_BOOT'
RUN_ONCE_ENV = 'RENDER_RUN_ONCE'
RUN_ONCE_JOBS = ('scan', 'eod')
QUOTE_PROBE_KEY = 'quote_probe'


def probe_enabled(env=None):
    """True only when RENDER_QUOTE_PROBE_ON_BOOT=1 (off by default)."""
    env = os.environ if env is None else env
    return str(env.get(PROBE_ENV, '')) == '1'


def run_once_requested(env=None):
    """The RENDER_RUN_ONCE job ('scan' or 'eod'), or None.

    Anything else (including an empty value) means no one-shot run:
    the env is an operator switch, not a free-form job name.
    """
    env = os.environ if env is None else env
    value = str(env.get(RUN_ONCE_ENV, '') or '').strip()
    return value if value in RUN_ONCE_JOBS else None


def guard_ok(mode, namespace):
    """(True, None) when the boot actions may run, else (False, reason).

    Both actions refuse unless the mode is trial AND the resolved
    namespace is exactly paper_render_trial_v1. The check is on the
    resolved namespace (the supervisor's), never on config.json alone.
    """
    if mode != 'trial':
        return False, 'not_trial_mode'
    if namespace != TRIAL_NAMESPACE:
        return False, 'wrong_namespace'
    return True, None


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())


def kv_get(store, scope, key):
    """The parsed JSON value of a paper_runtime_kv row, or None.

    A missing row or an unparseable value is None (the caller treats
    both as 'no record yet'); a stored non-dict is also None.
    """
    try:
        rows = store.execute(
            'SELECT value FROM paper_runtime_kv WHERE scope=? AND key=?',
            (scope, key))
    except Exception:
        return None
    if not rows:
        return None
    try:
        value = json.loads(rows[0]['value'])
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def kv_put(store, scope, key, value):
    """Upsert a paper_runtime_kv row (scope, key) with a JSON dict value."""
    now = _now_iso()
    store.execute(
        'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
        ' VALUES(?, ?, ?, ?)'
        ' ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,'
        ' updated_at=excluded.updated_at',
        (scope, key, json.dumps(value), now))
    return value


def run_once_marker_key(job, day):
    """The done-marker key for (job, IST day)."""
    return 'run_once:%s:%s' % (job, day)


def run_once_done(store, namespace, job, day):
    """The done-marker for (job, day), or None when it has not run yet."""
    return kv_get(store, namespace, run_once_marker_key(job, day))


def mark_run_once(store, namespace, job, day, outcome):
    """Write the done-marker for (job, day) and return it.

    `outcome` carries the run's terminal status ('ok'/'error'/...).
    The marker is written only after the run settles: a supervisor
    killed mid-run writes no marker, so the next boot retries.
    """
    marker = {'job': job, 'day': day, 'status': outcome.get('status'),
              'at': datetime.now(IST).isoformat()}
    if outcome.get('error'):
        marker['error'] = str(outcome['error'])[:140]
    return kv_put(store, namespace, run_once_marker_key(job, day), marker)


def find_run_once_job(job_name):
    """The trial-schedule job for a run-once request, or None.

    The lookup ignores RENDER_DISABLED_JOBS (that is the point of the
    one-shot) but respects the schedule's own `enabled` flag, and only
    accepts the jobs RENDER_RUN_ONCE allows ('scan'/'eod').
    """
    import render_schedule as rsch
    if job_name not in RUN_ONCE_JOBS:
        return None
    return rsch.trial_job(name=job_name)


def probe_failed_result(error_code, error_detail):
    """A full-shape failed probe result (used for child failures,
    timeouts and refused runs that still need a record)."""
    return {
        'status': 'failed', 'symbols': list(PROBE_SYMBOLS),
        'ltp_count': 0, 'http_status': None, 'fyers_code': None,
        'error_code': error_code, 'error_detail': error_detail,
        'duration_ms': 0, 'requests': 0, 'depth_requests': 0,
        'token_present': None, 'token_has_refresh': None,
        'token_access_len': None, 'token_issued_at': None,
        'at': datetime.now(IST).isoformat(),
        'child_exit_code': None, 'peak_rss_mb': None,
    }


def probe_result_from_child(res):
    """Build the probe result record from a run_job_streaming result.

    The child's stdout is one JSON line (its result); the last
    non-empty line is parsed, so stray prints before it are ignored.
    A nonzero exit or an unparseable stdout becomes a
    'probe_child_failed' result with the redacted stderr tail as the
    detail (never the raw tail — it could repeat a secret). The
    guard's sampled peak RSS rides along in 'peak_rss_mb'.
    """
    import audit as al
    if res.get('exit_code') != 0:
        detail = (res.get('stderr_tail') or '').strip()
        out = probe_failed_result(
            'probe_child_failed',
            al.redact_error_detail(detail) if detail else
            'quote probe child exited %s' % res.get('exit_code'))
        out['child_exit_code'] = res.get('exit_code')
        out['peak_rss_mb'] = res.get('peak_rss_mb')
        return out
    try:
        lines = [ln for ln in str(res.get('stdout_tail') or '').splitlines()
                 if ln.strip()]
        parsed = json.loads(lines[-1]) if lines else None
    except Exception:
        parsed = None
    if not isinstance(parsed, dict) or 'status' not in parsed:
        out = probe_failed_result(
            'probe_child_failed',
            'quote probe child printed no JSON result')
        out['child_exit_code'] = res.get('exit_code')
        out['peak_rss_mb'] = res.get('peak_rss_mb')
        return out
    parsed = dict(parsed)
    parsed['child_exit_code'] = res.get('exit_code')
    parsed['peak_rss_mb'] = res.get('peak_rss_mb')
    return parsed


class _AuditCtx:
    """Minimal engine-like object for audit.event (no engine import).

    audit.event needs .now(), .NS and (optionally) .pf/.si policy
    versions; the supervisor never imports the engine, so this shim
    supplies them. The event's timestamps come from now() (IST-aware).
    """

    def __init__(self, namespace):
        self.NS = namespace

    def now(self):
        return datetime.now(IST)


def emit_probe_audit(store, namespace, result):
    """Write the 'render_quote_probe' audit event for a probe result.

    Returns True when the row was written, False when it could not be
    (e.g. the paper_events table is missing on a fresh store) — the
    failure never raises: the kv record is the primary store, the
    audit event is the durable log. Only allowlisted fields are
    written (audit.clean drops the rest).
    """
    import audit as al
    try:
        ctx = _AuditCtx(namespace)
        status = result.get('status')
        error_code = result.get('error_code')
        row = al.event(
            ctx, 'render_quote_probe',
            outcome='ok' if status == 'ok' else 'failed',
            severity='INFO' if status == 'ok' else 'ERROR',
            status=status,
            error_code=error_code,
            error_detail=result.get('error_detail'),
            duration_ms=result.get('duration_ms'),
            symbols_count=len(result.get('symbols') or []),
            symbols=list(result.get('symbols') or []),
            reason='ok' if status == 'ok' else (error_code or 'failed'),
            http_status=result.get('http_status'),
            fyers_code=result.get('fyers_code'),
            ltp_count=result.get('ltp_count'),
            requests=result.get('requests'))
        sql, params = al.statements([row])[0]
        store.execute(sql, params)
        return True
    except Exception:
        return False


def emit_scan_abandoned_audit(store, namespace, remaining, reserve):
    """Write the 'scan_abandoned' audit event for a scan retry the
    supervisor refused to start because the remaining Fyers day
    budget is below the reserve kept for EOD and sync_history
    (I23 on Render).

    Severity ERROR, so the I22 email alert (ERROR audit events)
    picks it up. Returns True when the row was written. Returns
    False when an event for this namespace and day is already
    recorded — at most one abandon event per day, so a low budget
    does not spam one event per tick — or when the row could not be
    written (e.g. the paper_events table is missing on a fresh
    store). Never raises. Only allowlisted fields are written
    (audit.clean drops the rest).
    """
    import audit as al
    try:
        day = datetime.now(IST).date().isoformat()
        try:
            rows = store.execute(
                'SELECT 1 AS x FROM paper_events WHERE namespace=?'
                ' AND event_type=? AND session_day=? LIMIT 1',
                (namespace, 'scan_abandoned', day))
            if rows:
                return False
        except Exception:
            pass
        ctx = _AuditCtx(namespace)
        row = al.event(
            ctx, 'scan_abandoned',
            outcome='abandoned', severity='ERROR',
            reason='fyers_budget_below_scan_retry_reserve',
            remaining_budget_requests=remaining,
            scan_retry_reserve_requests=reserve)
        sql, params = al.statements([row])[0]
        store.execute(sql, params)
        return True
    except Exception:
        return False
