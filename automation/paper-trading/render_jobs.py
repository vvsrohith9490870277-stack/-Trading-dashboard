"""Job runner: run a scheduled job once, record it in paper_job_runs (Task 5).

The runner is injected: the supervisor passes the real engine runner, tests
pass a fake. Every attempt is recorded with its attempt number for the
(namespace, job, session_day) triple, so a re-run after a crash is visible
and never double-counted as a first attempt.
"""
import json
import time
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _next_attempt(store, namespace, job, session_day):
    rows = store.execute(
        'SELECT COALESCE(MAX(attempt), 0) AS m FROM paper_job_runs'
        ' WHERE namespace=? AND job=? AND session_day=?',
        (namespace, job, session_day))
    return int(rows[0]['m']) + 1 if rows else 1


def run_job(store, namespace, job, session_day, owner_id, runner):
    """Run `job` once via `runner(job)` and record the attempt.

    Returns the runner's result. On error the attempt is recorded with
    status 'error' and the exception is re-raised.
    """
    attempt = _next_attempt(store, namespace, job, session_day)
    claimed = int(time.time())
    # One row per (namespace, job, session_day): a re-run after a crash
    # updates the same row with attempt+1, never a second row.
    store.execute(
        'INSERT INTO paper_job_runs(namespace, job, session_day, attempt,'
        ' status, owner_id, claimed_unix, lease_until_unix, started_at)'
        ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)'
        ' ON CONFLICT(namespace, job, session_day) DO UPDATE SET'
        ' attempt=excluded.attempt, status=excluded.status,'
        ' owner_id=excluded.owner_id, claimed_unix=excluded.claimed_unix,'
        ' lease_until_unix=excluded.lease_until_unix,'
        ' started_at=excluded.started_at, finished_at=NULL,'
        ' exit_code=NULL, result_json=NULL',
        (namespace, job, session_day, attempt, 'running', owner_id,
         claimed, claimed + 90, _now_iso()))
    try:
        result = runner(job)
    except Exception as ex:
        store.execute(
            'UPDATE paper_job_runs SET status=?, finished_at=?,'
            ' exit_code=?, result_json=?'
            ' WHERE namespace=? AND job=? AND session_day=?',
            ('error', _now_iso(), 1,
             json.dumps({'error': type(ex).__name__, 'message': str(ex)}),
             namespace, job, session_day))
        raise
    store.execute(
        'UPDATE paper_job_runs SET status=?, finished_at=?, exit_code=?,'
        ' result_json=?'
        ' WHERE namespace=? AND job=? AND session_day=?',
        ('ok', _now_iso(), 0,
         json.dumps(result) if isinstance(result, dict) else
         json.dumps({'result': result}),
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
