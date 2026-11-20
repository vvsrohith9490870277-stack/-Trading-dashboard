"""IST scheduler with catch-up (Task 5, brief render-deploy-01).

All times are IST. A job is due at its slot time, and for
``catch_up_minutes`` after it. A job that already ran for that slot is never
due again. No job is due on a day the market is closed.
"""
import json
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
SCHEDULE_PATH = Path(__file__).resolve().parent / 'render_schedule.json'
DEFAULT_CATCH_UP_MINUTES = 30


def load_schedule(path=SCHEDULE_PATH, mode='live'):
    """Load the schedule. Only enabled jobs are returned. In trial mode the
    jobs under modes.trial replace the live jobs (same day shape, trial
    namespace). The adaptive job is not scheduled on Render at all."""
    data = json.loads(Path(path).read_text())
    jobs = data.get('jobs', [])
    if mode == 'trial':
        trial = data.get('modes', {}).get('trial', {})
        if 'jobs' in trial:
            jobs = trial['jobs']
    # Ops switch: RENDER_DISABLED_JOBS=scan,eod keeps those jobs from being
    # scheduled at all (no job rows are written for them).
    disabled = {n.strip() for n in
                os.environ.get('RENDER_DISABLED_JOBS', '').split(',')
                if n.strip()}
    jobs = [j for j in jobs if j.get('name') not in disabled]
    return {
        'timezone': data.get('timezone', 'Asia/Kolkata'),
        'catch_up_minutes': data.get('catch_up_minutes',
                                     DEFAULT_CATCH_UP_MINUTES),
        'jobs': [j for j in jobs if j.get('enabled', True)],
    }


def is_trading_day(day):
    """True when the market has a session on `day` (not a weekend/holiday).

    Uses the engine's own session calendar; falls back to weekday<5 when the
    engine cannot be imported (bare test environment).
    """
    try:
        import engine as e
        return bool(e.session(day))
    except Exception:
        return day.weekday() < 5


def _parse_time(s):
    h, m = s.split(':')
    return dtime(int(h), int(m))


def due_jobs(schedule, now=None, last_run=None):
    """Jobs due at `now` (any tz; interpreted in IST), in schedule order.

    `last_run` maps job name -> aware datetime of its last completed run.
    A slot is due when its scheduled time is within `catch_up_minutes`
    before `now` and the job has not run since that slot. A slot more than
    `catch_up_minutes` late is skipped, never caught up.
    """
    now = now or datetime.now(IST)
    now = now.astimezone(IST)
    if not is_trading_day(now.date()):
        return []
    due = []
    catch = timedelta(minutes=schedule.get('catch_up_minutes',
                                           DEFAULT_CATCH_UP_MINUTES))
    last_run = last_run or {}
    for job in schedule['jobs']:
        times = job.get('times') or [job['time']]
        for t in times:
            sched = datetime.combine(now.date(), _parse_time(t), tzinfo=IST)
            if sched <= now < sched + catch:
                last = last_run.get(job['name'])
                if last is None or last.astimezone(IST) < sched:
                    due.append(job)
                    break
    return due
