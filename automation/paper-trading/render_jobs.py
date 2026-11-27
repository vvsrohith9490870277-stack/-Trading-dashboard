"""Job runner: run a scheduled job once, record it in paper_job_runs (Task 5).

The runner is injected: the supervisor passes the real engine runner, tests
pass a fake. Every attempt is recorded with its attempt number for the
(namespace, job, session_day) triple, so a re-run after a crash is visible
and never double-counted as a first attempt.

Crash-loop breaker (render-deploy-03 Task 2): the attempt start times
are kept in the row's result_json 'starts' list (one row per
(namespace, job, session_day), so no new table). A job that starts
RENDER_JOB_MAX_STARTS (default 3) times inside RENDER_JOB_WINDOW_MIN
(default 30) minutes has its breaker open: the supervisor stops
scheduling it for the rest of the IST day. The count comes from the
store, so it survives a supervisor restart. `clear_breaker` drops the
stored starts after the cause is fixed.
"""
import json
import os
import time
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def job_max_starts():
    try:
        return max(1, int(os.environ.get('RENDER_JOB_MAX_STARTS', '3')))
    except ValueError:
        return 3


def job_window_min():
    try:
        return max(1, int(os.environ.get('RENDER_JOB_WINDOW_MIN', '30')))
    except ValueError:
        return 30


def _starts_in_window(result_json, window_min, now_unix):
    """Start timestamps inside the window, from a row's result_json."""
    try:
        data = json.loads(result_json or '{}')
    except Exception:
        data = {}
    cutoff = now_unix - window_min * 60
    return [s for s in (data.get('starts') or [])
            if isinstance(s, (int, float)) and s >= cutoff]


def _job_row(store, namespace, job, session_day):
    rows = store.execute(
        'SELECT * FROM paper_job_runs'
        ' WHERE namespace=? AND job=? AND session_day=?',
        (namespace, job, session_day))
    return rows[0] if rows else None


def job_starts(store, namespace, job, session_day, window_min=None,
               now_unix=None):
    """This job's start timestamps inside the window (default 30 min)."""
    if window_min is None:
        window_min = job_window_min()
    if now_unix is None:
        now_unix = int(time.time())
    row = _job_row(store, namespace, job, session_day)
    if not row:
        return []
    return _starts_in_window(row.get('result_json'), window_min, now_unix)


def breaker_open(store, namespace, job, session_day, max_starts=None,
                 window_min=None, now_unix=None):
    """True when the job started max_starts times inside the window."""
    if max_starts is None:
        max_starts = job_max_starts()
    if window_min is None:
        window_min = job_window_min()
    if now_unix is None:
        now_unix = int(time.time())
    return len(job_starts(store, namespace, job, session_day, window_min,
                          now_unix)) >= max_starts


def open_breakers(store, namespace, session_day, max_starts=None,
                  window_min=None, now_unix=None):
    """{job: {'starts', 'window_min', 'max_starts'}} for every open
    breaker today. One SELECT for the whole namespace+day."""
    if max_starts is None:
        max_starts = job_max_starts()
    if window_min is None:
        window_min = job_window_min()
    if now_unix is None:
        now_unix = int(time.time())
    out = {}
    rows = store.execute(
        'SELECT job, result_json FROM paper_job_runs'
        ' WHERE namespace=? AND session_day=?',
        (namespace, session_day))
    for r in rows:
        starts = _starts_in_window(r.get('result_json'), window_min,
                                   now_unix)
        if len(starts) >= max_starts:
            out[r['job']] = {'starts': len(starts),
                             'window_min': window_min,
                             'max_starts': max_starts}
    return out


def clear_breaker(store, namespace, job, session_day):
    """Clear a job's breaker after the cause is fixed: drops the stored
    start list so the job may be scheduled again. Returns True when a
    row was found and cleared."""
    row = _job_row(store, namespace, job, session_day)
    if not row:
        return False
    try:
        data = json.loads(row.get('result_json') or '{}')
    except Exception:
        data = {}
    data.pop('starts', None)
    store.execute(
        'UPDATE paper_job_runs SET result_json=?'
        ' WHERE namespace=? AND job=? AND session_day=?',
        (json.dumps(data), namespace, job, session_day))
    return True


def _next_attempt(store, namespace, job, session_day):
    rows = store.execute(
        'SELECT COALESCE(MAX(attempt), 0) AS m FROM paper_job_runs'
        ' WHERE namespace=? AND job=? AND session_day=?',
        (namespace, job, session_day))
    return int(rows[0]['m']) + 1 if rows else 1


def run_job(store, namespace, job, session_day, owner_id, runner):
    """Run `job` once via `runner(job)` and record the attempt.

    Returns the runner's result. On error the attempt is recorded with
    status 'error' and the exception is re-raised. When the runner
    reports the child was killed (e.g. the memory guard's
    killed_reason), the attempt is recorded 'failed' with the reason.
    When the runner reports a BLOCKED simulation (a result dict with
    simulation_status BLOCKED_*, from the adaptive EOD: the day's
    snapshot was stale, so nothing was simulated), the attempt is
    recorded 'blocked' — not 'ok' — so /health.schedule shows it and
    the day is not counted as done (a blocked day is re-run by a
    later catch-up). The runner's returned dict is never modified;
    the stored result_json merges in this attempt's start (the
    breaker count).
    """
    attempt = _next_attempt(store, namespace, job, session_day)
    claimed = int(time.time())
    # This attempt's start goes in the row's result_json 'starts' list
    # (today's starts only; the row is per-day, so the list stays
    # tiny). A hard kill mid-run still leaves the start recorded, so
    # the breaker count survives a supervisor restart.
    row = _job_row(store, namespace, job, session_day)
    try:
        prior = json.loads((row or {}).get('result_json') or '{}')
    except Exception:
        prior = {}
    starts = [s for s in (prior.get('starts') or [])
              if isinstance(s, (int, float))]
    starts.append(claimed)
    starts_json = json.dumps({'starts': starts})
    # One row per (namespace, job, session_day): a re-run after a crash
    # updates the same row with attempt+1, never a second row.
    store.execute(
        'INSERT INTO paper_job_runs(namespace, job, session_day, attempt,'
        ' status, owner_id, claimed_unix, lease_until_unix, started_at,'
        ' result_json)'
        ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
        ' ON CONFLICT(namespace, job, session_day) DO UPDATE SET'
        ' attempt=excluded.attempt, status=excluded.status,'
        ' owner_id=excluded.owner_id, claimed_unix=excluded.claimed_unix,'
        ' lease_until_unix=excluded.lease_until_unix,'
        ' started_at=excluded.started_at, finished_at=NULL,'
        ' exit_code=NULL, result_json=excluded.result_json',
        (namespace, job, session_day, attempt, 'running', owner_id,
         claimed, claimed + 90, _now_iso(), starts_json))
    try:
        result = runner(job)
    except Exception as ex:
        store.execute(
            'UPDATE paper_job_runs SET status=?, finished_at=?,'
            ' exit_code=?, result_json=?'
            ' WHERE namespace=? AND job=? AND session_day=?',
            ('error', _now_iso(), 1,
             json.dumps({'error': type(ex).__name__, 'message': str(ex),
                         'starts': starts}),
             namespace, job, session_day))
        raise
    if isinstance(result, dict) and result.get('killed_reason'):
        store.execute(
            'UPDATE paper_job_runs SET status=?, finished_at=?,'
            ' exit_code=?, result_json=?'
            ' WHERE namespace=? AND job=? AND session_day=?',
            ('failed', _now_iso(), 1,
             json.dumps({**result, 'starts': starts}),
             namespace, job, session_day))
        return result
    sim = result.get('simulation_status') if isinstance(result, dict) \
        else None
    status = ('blocked' if isinstance(sim, str)
              and sim.startswith('BLOCKED') else 'ok')
    store.execute(
        'UPDATE paper_job_runs SET status=?, finished_at=?, exit_code=?,'
        ' result_json=?'
        ' WHERE namespace=? AND job=? AND session_day=?',
        (status, _now_iso(), 0,
         json.dumps({**result, 'starts': starts})
         if isinstance(result, dict) else
         json.dumps({'result': result, 'starts': starts}),
         namespace, job, session_day))
    return result


def last_run_times(store, namespace):
    """{job name: aware datetime of its last completed (status ok) run}.

    finished_at is stored in UTC by _now_iso(); parse it as UTC and convert
    to IST. (Parsing it as IST directly puts last_run 5.5 h behind the real
    run, so due_jobs() would report the job due again for the rest of its
    catch-up window and the supervisor would re-run it every tick.)
    """
    from zoneinfo import ZoneInfo
    from datetime import timezone
    rows = store.execute(
        'SELECT job, MAX(finished_at) AS f FROM paper_job_runs'
        ' WHERE namespace=? AND status=? GROUP BY job',
        (namespace, 'ok'))
    out = {}
    for r in rows:
        if r['f']:
            out[r['job']] = datetime.strptime(
                r['f'], '%Y-%m-%dT%H:%M:%S').replace(
                    tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Kolkata'))
    return out
