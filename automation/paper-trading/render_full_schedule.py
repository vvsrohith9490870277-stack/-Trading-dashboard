"""Full daily schedule for Render (brief 23 Nov 2026).

One Render service runs the whole NSE day on schedule for the resolved
namespace, with no outside trigger. This module is the scheduler's
state and catch-up logic; the Supervisor (render_app) drives it from
its tick. It builds on the existing schedule (render_schedule.json,
render_schedule.due_jobs with its 30-minute catch-up window) and the
existing job runner (render_jobs.run_job, paper_job_runs).

- Idempotency: every job the scheduler runs is recorded in
  render_job_runs (namespace, job, trade_date, status, started_at,
  finished_at, error_code), one row per (namespace, job, trade_date).
  A job that already completed for a trade_date is never run again.
  paper_job_runs is also consulted, so a job that finished today
  through the older path (before this change, or via run-once) is not
  re-run after a restart.
- Catch-up after a restart: the normal 30-minute window is too short
  for a restart mid-morning, so on top of due_jobs the scheduler also
  runs a missed scan (if it is before EOD, 16:20) and a missed EOD (if it
  is after 16:10), each exactly once per trade_date. A scan retry runs
  at most once per 15 minutes (I23 on Render), so a crash loop can
  never fire a burst of attempts back to back. Other jobs are
  not caught up; they run at their next slot.
- Single writer: jobs only run when this supervisor holds the Turso
  lease (the tick gates on it); a job that would run without the
  lease is skipped and recorded, and /health.schedule reports the
  lease holder.
- /health.schedule: per-job last status, last run time, next run
  time and last error code, plus lease_holder and namespace. Times
  are IST. Next run is the next scheduled slot (catch-up runs are
  not reflected in it; a job that already completed today shows
  tomorrow's slot).

No engine import here: the trading-day check reads config.json only,
so this module stays fast and importable in bare test environments.
No orders, no credentials, no Fyers calls.
"""
import json
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')

# Catch-up deadlines (IST) for the jobs the scheduler catches up after
# a restart. scan: run if missed and now is before EOD (config
# sessiond_eod_due, "16:20" — I23 on Render; it used to stop at 15:00,
# so a missed scan quietly gave up mid-afternoon). eod: run if missed
# and now is at/after 16:10 (no upper bound today — EOD is the day's
# close-out and must run whenever the service comes back).
DEFAULT_CATCH_UP_SCAN_BEFORE = dtime(16, 20)
CATCH_UP_EOD_AFTER = dtime(16, 10)

# Minimum gap between two scan attempts (I23 on Render): retries are
# spaced like sessiond's 900 s interval, so a crash loop can never
# fire a burst of attempts back to back — even when the breaker
# reopens, the next attempt still waits out the gap.
SCAN_RETRY_MIN_SPACING = timedelta(minutes=15)

# Jobs the scheduler catches up after a restart. Every other job runs
# at its next slot only.
CATCH_UP_JOBS = frozenset({'scan', 'eod'})

_JOB_RUNS_DDL = (
    'CREATE TABLE IF NOT EXISTS render_job_runs('
    'namespace TEXT NOT NULL, job TEXT NOT NULL,'
    ' trade_date TEXT NOT NULL, status TEXT NOT NULL,'
    ' started_at TEXT, finished_at TEXT, error_code TEXT,'
    ' PRIMARY KEY(namespace, job, trade_date))')


def _now_iso():
    """UTC ISO timestamp (the render store's convention for job rows)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _parse_ts(value):
    """Parse an ISO timestamp to an aware IST datetime, or None.

    Same rule as render_health._parse_ts: naive values are UTC (the
    render store's convention); aware values convert from their own
    offset. Kept local so this module never imports render_health.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def is_trading_day_config(day, config):
    """True when `day` has a session, from config.json alone.

    Mirrors render_app.session_window's trading-day rule without
    importing the engine: inside the calendar range, a special
    session counts, otherwise weekends and holidays are closed.
    """
    key = day.isoformat()
    if not (config.get('calendar_valid_from', '')
            <= key <= config.get('calendar_valid_through', '')):
        return False
    if key in (config.get('special_sessions') or {}):
        return True
    if day.weekday() >= 5 or key in (config.get('holidays') or []):
        return False
    return True


def _slot_times(job):
    """A job's slot times as sorted datetime.time objects (or [])."""
    times = job.get('times') or ([job['time']] if job.get('time') else [])
    out = []
    for t in times:
        try:
            h, m = str(t).split(':')
            out.append(dtime(int(h), int(m)))
        except (ValueError, AttributeError):
            continue
    return sorted(out)


def next_slot(job, start, config):
    """Next scheduled datetime (>= start, IST) for a job, searching up
    to 10 days ahead over trading days only. None when no slot found."""
    slots = _slot_times(job)
    if not slots:
        return None
    start = start.astimezone(IST)
    day = start.date()
    for _ in range(10):
        if is_trading_day_config(day, config):
            for t in slots:
                slot = datetime.combine(day, t, tzinfo=IST)
                if slot >= start:
                    return slot
        day += timedelta(days=1)
    return None


def _slot_today(job, day):
    """The job's first slot today as an aware IST datetime, or None."""
    slots = _slot_times(job)
    if not slots:
        return None
    return datetime.combine(day, slots[0], tzinfo=IST)


# -- render_job_runs ------------------------------------------------------

def ensure_job_runs_table(store):
    """Create render_job_runs if missing. Idempotent; migrate() (which
    reads render_schema.sql) is the normal path — this is for callers
    with a bare store."""
    store.execute(_JOB_RUNS_DDL)
    return True


def _job_run_row(store, namespace, job, trade_date):
    try:
        rows = store.execute(
            'SELECT namespace, job, trade_date, status, started_at,'
            ' finished_at, error_code FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            (namespace, job, trade_date))
    except Exception:
        return None
    return rows[0] if rows else None


def record_job_start(store, namespace, job, trade_date, started_at=None):
    """Mark a job started. Returns False (and writes nothing) when the
    job already completed for this trade_date — the idempotency guard,
    so a restart can never re-run a finished job."""
    row = _job_run_row(store, namespace, job, trade_date)
    if row is not None and row.get('status') == 'ok':
        return False
    started_at = started_at or _now_iso()
    store.execute(
        'INSERT INTO render_job_runs(namespace, job, trade_date, status,'
        ' started_at, finished_at, error_code)'
        ' VALUES(?, ?, ?, ?, ?, NULL, NULL)'
        ' ON CONFLICT(namespace, job, trade_date) DO UPDATE SET'
        ' status=excluded.status, started_at=excluded.started_at,'
        ' finished_at=NULL, error_code=NULL',
        (namespace, job, trade_date, 'running', started_at))
    return True


def record_job_finish(store, namespace, job, trade_date, status,
                      error_code=None, finished_at=None):
    """Mark a job finished (status 'ok', 'error' or 'blocked').
    Upserts so a finish without a recorded start still leaves a row."""
    finished_at = finished_at or _now_iso()
    store.execute(
        'INSERT INTO render_job_runs(namespace, job, trade_date, status,'
        ' started_at, finished_at, error_code)'
        ' VALUES(?, ?, ?, ?, NULL, ?, ?)'
        ' ON CONFLICT(namespace, job, trade_date) DO UPDATE SET'
        ' status=excluded.status, finished_at=excluded.finished_at,'
        ' error_code=excluded.error_code',
        (namespace, job, trade_date, status, finished_at, error_code))
    return True


def record_job_skipped(store, namespace, job, trade_date, error_code,
                       started_at=None):
    """Record a skip (e.g. the lease is held by someone else).
    Idempotent: only inserts when no row exists for this
    (namespace, job, trade_date); never overwrites a running, ok or
    error row."""
    if _job_run_row(store, namespace, job, trade_date) is not None:
        return False
    started_at = started_at or _now_iso()
    store.execute(
        'INSERT INTO render_job_runs(namespace, job, trade_date, status,'
        ' started_at, finished_at, error_code)'
        ' VALUES(?, ?, ?, ?, ?, ?, ?)',
        (namespace, job, trade_date, 'skipped', started_at, started_at,
         error_code))
    return True


def job_completed_today(store, namespace, job, trade_date):
    """True when a job finished ok for this trade_date, in either
    record: render_job_runs (the scheduler's record) or paper_job_runs
    (jobs run before this change, or via run-once). A restart must
    never re-run a job that already finished today."""
    row = _job_run_row(store, namespace, job, trade_date)
    if row is not None and row.get('status') == 'ok':
        return True
    try:
        rows = store.execute(
            'SELECT status FROM paper_job_runs'
            ' WHERE namespace=? AND job=? AND session_day=?',
            (namespace, job, trade_date))
    except Exception:
        return False
    return bool(rows) and rows[0].get('status') == 'ok'


def completed_jobs_today(store, namespace, trade_date):
    """{job names completed ok today}, from both job tables."""
    out = set()
    for table, day_col in (('render_job_runs', 'trade_date'),
                           ('paper_job_runs', 'session_day')):
        try:
            rows = store.execute(
                'SELECT job FROM %s'
                ' WHERE namespace=? AND %s=? AND status=?'
                % (table, day_col),
                (namespace, trade_date, 'ok'))
            out |= {r['job'] for r in rows}
        except Exception:
            continue
    return out


# -- catch-up --------------------------------------------------------------

def _catch_up_scan_before(config):
    """The scan catch-up deadline as an IST time: config
    sessiond_eod_due ("16:20"), else 16:20. A missing or unparsable
    value fails closed to the EOD deadline, never to the old 15:00."""
    raw = (config or {}).get('sessiond_eod_due')
    try:
        h, m = str(raw).split(':')
        return dtime(int(h), int(m))
    except (ValueError, AttributeError, TypeError):
        return DEFAULT_CATCH_UP_SCAN_BEFORE


def catch_up_due_jobs(schedule, now, completed, config, last_attempt=None):
    """Jobs needing a startup catch-up run at `now`.

    `completed` is the set of job names already finished today.
    Only scan and eod are caught up, and only on trading days:
    scan when its 08:45 slot has passed and it is before the EOD
    deadline (config sessiond_eod_due, 16:20);
    eod when its 16:10 slot has passed and it is at/after 16:10.
    `last_attempt` maps job names to the aware datetime of their
    last attempt start: a scan catch-up is due only when the last
    attempt was at least 15 minutes ago, so retries never burst.
    Returns the jobs in schedule order. A job inside the normal
    30-minute due window is also returned here (the caller dedups),
    so a restart at 09:00 and a restart at 10:00 both run the scan.
    """
    now = now.astimezone(IST)
    if not is_trading_day_config(now.date(), config):
        return []
    deadline = _catch_up_scan_before(config)
    last_attempt = last_attempt or {}
    due = []
    for job in schedule.get('jobs', []):
        name = job.get('name')
        if name not in CATCH_UP_JOBS or name in completed:
            continue
        slot = _slot_today(job, now.date())
        if slot is None or slot > now:
            continue
        if name == 'scan':
            if now.time() >= deadline:
                continue
            last = last_attempt.get('scan')
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last.astimezone(IST) < SCAN_RETRY_MIN_SPACING:
                    continue
            due.append(job)
        elif name == 'eod' and now.time() >= CATCH_UP_EOD_AFTER:
            due.append(job)
    return due


# -- /health.schedule -------------------------------------------------------

def _last_job_status(store, namespace, job, trade_date):
    """{status, last_run_at (IST ISO), error_code} for a job today,
    preferring render_job_runs, falling back to paper_job_runs.
    Never raises; unknown jobs report all None."""
    row = _job_run_row(store, namespace, job, trade_date)
    if row is not None:
        at = _parse_ts(row.get('finished_at') or row.get('started_at'))
        return {'status': row.get('status'),
                'last_run_at': at.isoformat() if at is not None else None,
                'error_code': row.get('error_code')}
    try:
        rows = store.execute(
            'SELECT status, started_at, finished_at, exit_code,'
            ' result_json FROM paper_job_runs'
            ' WHERE namespace=? AND job=? AND session_day=?',
            (namespace, job, trade_date))
    except Exception:
        rows = []
    if rows:
        r = rows[0]
        err = None
        try:
            res = json.loads(r.get('result_json') or '{}')
            err = res.get('error') or res.get('killed_reason')
            # A blocked run carries no error: its reason is the
            # simulation_status (e.g. BLOCKED_STALE_INPUT), shown as
            # the error code so /health.schedule names the cause.
            if r.get('status') == 'blocked':
                err = res.get('simulation_status') or err
        except Exception:
            err = None
        if err is None and r.get('exit_code') not in (None, 0):
            err = 'exit_%s' % r.get('exit_code')
        at = _parse_ts(r.get('finished_at') or r.get('started_at'))
        return {'status': r.get('status'),
                'last_run_at': at.isoformat() if at is not None else None,
                'error_code': err}
    return {'status': None, 'last_run_at': None, 'error_code': None}


def _lease_holder(store, namespace):
    """The current lease holder's owner_id, or None.

    The lease row's owner only counts while the lease is live
    (expires_unix in the future by server time); an expired row
    names the last holder, not the current one."""
    try:
        import render_lease as rl
        row = rl.get_lease(store, namespace)
        if not row:
            return None
        try:
            now_unix = store.server_time()
        except Exception:
            now_unix = int(time.time())
        if int(row.get('expires_unix') or 0) <= now_unix:
            return None
        return row.get('owner_id')
    except Exception:
        return None


def schedule_state(store, namespace, trade_date, schedule, config,
                   now=None):
    """The /health.schedule dict for `trade_date`.

    Per job (in schedule order): last_status, last_run_at, next_run_at
    and last_error_code; plus lease_holder and namespace. next_run_at
    is the next scheduled slot: for a job that already completed
    today it is tomorrow's slot, otherwise the next slot from now.
    Never raises: a store failure leaves every job unknown with the
    lease holder None, never a made-up value."""
    now = (now or datetime.now(IST)).astimezone(IST)
    jobs = {}
    for job in schedule.get('jobs', []):
        name = job.get('name')
        if not name:
            continue
        last = _last_job_status(store, namespace, name, trade_date)
        if last.get('status') == 'ok':
            start = datetime.combine(now.date() + timedelta(days=1),
                                     dtime(0, 0), tzinfo=IST)
            nxt = next_slot(job, start, config)
        else:
            nxt = next_slot(job, now, config)
        jobs[name] = {
            'last_status': last.get('status'),
            'last_run_at': last.get('last_run_at'),
            'next_run_at': nxt.isoformat() if nxt is not None else None,
            'last_error_code': last.get('error_code'),
        }
    return {'jobs': jobs, 'lease_holder': _lease_holder(store, namespace),
            'namespace': namespace}
