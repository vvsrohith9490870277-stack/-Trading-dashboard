"""Health snapshot for the Render supervisor (Task 6, brief 01).

/health is 200 normally and 503 only when something is actually wrong:
- idle_not_active_runner (the handover flag names the box, or is unset):
  the supervisor is correctly idle, so 200.
- ok: the flag names render, the lease is held, the worker is running: 200.
- lease_lost / degraded: the flag names render but the lease is not held,
  the store is unreachable, or the worker keeps crashing: 503.

The payload carries every field brief 01 Task 6.3 lists, with exactly
those names. The keyword a monitor matches is the literal substring
`"ok": true` (standard json.dumps output): UptimeRobot's keyword check
is `"ok": true`, and `ok` is true exactly when the HTTP status is 200.

Fields that cannot be computed cheaply from memory are present with a
null value rather than a fresh DB read (brief 02 section 6): the
snapshot is built on the supervisor tick from memory, and /health
itself never touches the store. Those fields are fyers_budget.day_used,
trial_budget.day_used, auth.circuit_open,
box_conflict_check.paper_v2_heartbeat_*,
worker.last_heartbeat_age_s, jobs_today and last_job. The senior
decides whether the tick may read the store for them.

Render-deploy-03 Task 3 is that decision: a background refresher
(`refresh_store_fields`, driven from the supervisor tick) reads the
store at most once every RENDER_HEALTH_REFRESH_S (default 60 s) and
caches the result; /health serves the cache and never touches the
store. A store error sets store_ok false and never raises into
/health. Values that cannot be computed stay null with a `reasons`
map, never a made-up zero.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
MEMORY_LIMIT_MB = 450
# /health payload version; bump when the /health shape changes.
VERSION = '1.0.0'


def memory_rss_mb():
    """This process's RSS in MB (Linux /proc; 0 when unreadable)."""
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    except Exception:
        return 0


def children_rss_mb():
    """RSS in MB of this process's direct children (worker + job
    children), read from /proc. 0 when unreadable."""
    total = 0
    try:
        me = os.getpid()
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            try:
                with open('/proc/%s/stat' % name) as f:
                    # fields after the (comm) field: state, ppid, ...
                    parts = f.read().rsplit(')', 1)[1].split()
                if int(parts[1]) != me:
                    continue
                with open('/proc/%s/status' % name) as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            total += int(line.split()[1]) // 1024
                            break
            except Exception:
                continue
    except Exception:
        return 0
    return total


def tree_rss_mb(pid):
    """RSS in MB of a process and all its descendants (Linux /proc).
    0 when unreadable. Used by the per-job memory guard."""
    try:
        ppid = {}
        rss = {}
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            try:
                with open('/proc/%s/stat' % name) as f:
                    parts = f.read().rsplit(')', 1)[1].split()
                ppid[int(name)] = int(parts[1])
                with open('/proc/%s/status' % name) as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            rss[int(name)] = int(line.split()[1]) // 1024
                            break
            except Exception:
                continue
        children = {}
        for p, parent in ppid.items():
            children.setdefault(parent, []).append(p)
        total = rss.get(pid, 0)
        frontier = [pid]
        seen = {pid}
        while frontier:
            nxt = []
            for p in frontier:
                for c in children.get(p, []):
                    if c not in seen:
                        seen.add(c)
                        total += rss.get(c, 0)
                        nxt.append(c)
            frontier = nxt
        return total
    except Exception:
        return 0


def memory_over_limit(rss_mb, limit=MEMORY_LIMIT_MB):
    return rss_mb > limit


def health_refresh_seconds():
    """Background refresher interval in seconds (env
    RENDER_HEALTH_REFRESH_S, default 60)."""
    try:
        return max(1, int(os.environ.get('RENDER_HEALTH_REFRESH_S', '60')))
    except ValueError:
        return 60


def _parse_ts(value):
    """Parse an ISO timestamp to an aware IST datetime, or None.

    Naive values are UTC (the render store's convention for lease and
    job timestamps); aware values are converted from their own offset
    (the engine writes paper_monitor_health heartbeats IST-aware).
    Returns None when the value is missing or unparseable.
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


def _read_reason(ex):
    """The reasons-map code for a failed store read: 'table_missing'
    when the table does not exist (a schema gap, not an outage), else
    'store_unreachable'. Only the code is recorded, never the message
    (a message could repeat a connection string)."""
    if 'no such table' in str(ex).lower():
        return 'table_missing'
    return 'store_unreachable'


def _job_reason(row):
    """The short reason a job failed, from its stored result_json only.
    Only the classified fields are read (killed_reason, the error
    message); nothing else from result_json is echoed into /health.
    Truncated to one short line."""
    try:
        res = json.loads(row.get('result_json') or '{}')
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    if res.get('killed_reason'):
        return str(res['killed_reason'])[:140]
    err = res.get('error')
    if isinstance(err, dict):
        return str(err.get('message') or err.get('type') or 'error')[:140]
    if isinstance(err, str):
        return err[:140]
    return None


def _budget_day_used(raw, today):
    """day_used for one budget counter (the raw paper_runtime_kv value
    for _global/fyers_budget or _global/fyers_budget_trial), plus a
    reason when it cannot be computed. Returns (value, reason). No
    stored row, or a row from an earlier day, means 0: the engine
    resets the counter to 0 on a new day, so 0 is the counter's real
    value, not a made-up zero. A row that is not valid JSON, or not a
    dict with a usable requests count, is null with
    'budget_unparseable'."""
    if raw is None:
        return 0, None
    try:
        state = json.loads(raw)
    except Exception:
        return None, 'budget_unparseable'
    if not isinstance(state, dict):
        return None, 'budget_unparseable'
    if state.get('day') != today:
        return 0, None
    try:
        return int(state.get('requests') or 0), None
    except (TypeError, ValueError):
        return None, 'budget_unparseable'


# Store-derived /health fields (the refresher's `fields` dict).
_HEALTH_FIELDS = (
    'lease', 'jobs', 'worker_last_heartbeat_age_s', 'jobs_today',
    'last_job', 'fyers_budget_day_used', 'trial_budget_day_used',
    'box_conflict_check', 'peak_job_rss_mb', 'last_job_peak_rss_mb',
    'breakers')


def new_health_cache():
    """A fresh /health store cache: no successful refresh yet, every
    store-derived field null with a 'not_refreshed_yet' reason. The
    refresher replaces the whole dict on each refresh (never mutates
    it), so the HTTP thread can read it without a lock."""
    return {
        'ok': False, 'refreshed_at': None, 'error': None,
        'fields': {
            'lease': None, 'jobs': {},
            'worker_last_heartbeat_age_s': None, 'jobs_today': {},
            'last_job': None, 'fyers_budget_day_used': None,
            'trial_budget_day_used': None, 'box_conflict_check': None,
            'peak_job_rss_mb': None, 'last_job_peak_rss_mb': None,
            'breakers': {},
        },
        'reasons': {k: 'not_refreshed_yet' for k in _HEALTH_FIELDS},
    }


# Read-only fallback for snapshots built before any refresh (and for
# Supervisors constructed without __init__ in tests). Never mutated.
EMPTY_HEALTH_CACHE = new_health_cache()


def refresh_store_fields(store, namespace, mode, now=None):
    """Read the store-derived /health fields (Task 3, render-deploy-03).

    Pure except for the store reads: every read is a SELECT, no writes,
    no Fyers call, no new table. Returns {'ok', 'fields', 'reasons'}:
    - fields: lease, jobs, worker_last_heartbeat_age_s, jobs_today,
      last_job, fyers_budget_day_used, trial_budget_day_used (trial
      only), box_conflict_check (trial only), peak_job_rss_mb,
      last_job_peak_rss_mb, breakers.
    - reasons: {field: reason} for every field that could not be
      computed (never a made-up zero).
    - ok: True when at least one read group succeeded; False only when
      the store answered nothing (every group failed) — that is the
      store_ok signal. A single group's failure (e.g.
      paper_monitor_health missing on a fresh render store — it is a
      box table, not in render_schema.sql) leaves only that group's
      fields null, with a reason, and does not fail the rest.
    `now` is an aware IST datetime (default: now). The budget counters
    are the same rows the engine budget uses (paper_runtime_kv
    _global/fyers_budget and _global/fyers_budget_trial — the keys
    TokenStore.get_budget reads), read here with a plain SELECT so
    /health does not depend on the cryptography module; never a new
    counter. A job-tree peak is the max sampled RSS the guard recorded
    in the job's result_json (jobs run before the guard existed have no
    peak); the peak is a sampled lower bound, not a continuous
    measurement.
    """
    import render_jobs as rj
    import render_lease as rl
    if now is None:
        now = datetime.now(IST)
    today = now.date().isoformat()
    fields = new_health_cache()['fields']
    reasons = {}
    ok_groups = 0

    # --- jobs (paper_job_runs, today): jobs_today, last_job, peaks,
    # breakers. One SELECT, ordered so the last row is the most
    # recently started job today (started_at ties break by rowid,
    # i.e. insertion order).
    try:
        rows = store.execute(
            'SELECT rowid AS rid, job, status, started_at, finished_at,'
            ' exit_code, result_json FROM paper_job_runs'
            ' WHERE namespace=? AND session_day=?'
            ' ORDER BY started_at, rid',
            (namespace, today))
        counts = {}
        last = None
        peak = None
        for r in rows:
            counts[r['status']] = counts.get(r['status'], 0) + 1
            last = r
            try:
                res = json.loads(r.get('result_json') or '{}')
            except Exception:
                res = {}
            p = res.get('peak_rss_mb') if isinstance(res, dict) else None
            if isinstance(p, (int, float)):
                peak = p if peak is None else max(peak, p)
        fields['jobs_today'] = counts
        if rows:
            if peak is None:
                reasons['peak_job_rss_mb'] = 'no_peak_recorded'
            else:
                fields['peak_job_rss_mb'] = peak
            started = _parse_ts(last.get('started_at'))
            finished = _parse_ts(last.get('finished_at'))
            duration = None
            if started is not None and finished is not None:
                duration = int((finished - started).total_seconds())
            fields['last_job'] = {
                'name': last['job'], 'status': last['status'],
                'started_at_ist': (started.isoformat()
                                   if started is not None else None),
                'duration_s': duration,
                'reason': _job_reason(last)}
            try:
                res = json.loads(last.get('result_json') or '{}')
            except Exception:
                res = {}
            p = res.get('peak_rss_mb') if isinstance(res, dict) else None
            if isinstance(p, (int, float)):
                fields['last_job_peak_rss_mb'] = p
            else:
                reasons['last_job_peak_rss_mb'] = 'no_peak_recorded'
            # Breakers, from the same rows (job_starts reads the stored
            # starts list; no second query).
            breakers = {}
            for r in rows:
                try:
                    n = rj.job_starts(r)
                except Exception:
                    continue
                if n >= rj.job_max_starts():
                    breakers[r['job']] = {
                        'starts': n,
                        'window_min': rj.job_window_min(),
                        'max_starts': rj.job_max_starts()}
            fields['breakers'] = breakers
        else:
            reasons['last_job'] = 'no_jobs_today'
            reasons['peak_job_rss_mb'] = 'no_jobs_today'
            reasons['last_job_peak_rss_mb'] = 'no_jobs_today'
        ok_groups += 1
    except Exception as ex:
        reason = _read_reason(ex)
        for k in ('jobs', 'jobs_today', 'last_job', 'peak_job_rss_mb',
                  'last_job_peak_rss_mb', 'breakers'):
            reasons[k] = reason
    # last_run_times (all days) is a separate GROUP BY query; it feeds
    # the 'jobs' field. A failure here leaves only 'jobs' empty.
    try:
        last_runs = rj.last_run_times(store, namespace)
        fields['jobs'] = {k: v.isoformat() for k, v in last_runs.items()}
        ok_groups += 1
    except Exception as ex:
        reasons['jobs'] = _read_reason(ex)

    # --- worker heartbeat (paper_monitor_health, one row per
    # namespace, upserted by the engine worker). A missing table is a
    # schema gap (the table is a box table, not in render_schema.sql),
    # not an outage: only this field goes null.
    try:
        rows = store.execute(
            'SELECT heartbeat_at FROM paper_monitor_health'
            ' WHERE namespace=?', (namespace,))
        if rows:
            hb = _parse_ts(rows[0].get('heartbeat_at'))
            if hb is not None:
                fields['worker_last_heartbeat_age_s'] = int(
                    (now - hb).total_seconds())
            else:
                reasons['worker_last_heartbeat_age_s'] = \
                    'heartbeat_unparseable'
        else:
            reasons['worker_last_heartbeat_age_s'] = 'no_heartbeat_row'
        ok_groups += 1
    except Exception as ex:
        reasons['worker_last_heartbeat_age_s'] = _read_reason(ex)

    # --- budgets (paper_runtime_kv): the same counter the engine
    # budget uses (TokenStore.get_budget reads these exact keys), read
    # here with a plain SELECT so /health does not depend on the
    # cryptography module. Never a new counter, no Fyers call. The
    # trial key is read only in trial mode.
    try:
        keys = ['fyers_budget']
        if mode == 'trial':
            keys.append('fyers_budget_trial')
        raw = {}
        for key in keys:
            rows = store.execute(
                'SELECT value FROM paper_runtime_kv'
                ' WHERE scope=? AND key=?', ('_global', key))
            raw[key] = rows[0]['value'] if rows else None
        value, reason = _budget_day_used(raw.get('fyers_budget'), today)
        fields['fyers_budget_day_used'] = value
        if reason:
            reasons['fyers_budget_day_used'] = reason
        if mode == 'trial':
            tvalue, treason = _budget_day_used(
                raw.get('fyers_budget_trial'), today)
            fields['trial_budget_day_used'] = tvalue
            if treason:
                reasons['trial_budget_day_used'] = treason
        ok_groups += 1
    except Exception as ex:
        reason = _read_reason(ex)
        reasons['fyers_budget_day_used'] = reason
        if mode == 'trial':
            reasons['trial_budget_day_used'] = reason

    # --- this namespace's lease row (feeds the 'lease' field).
    try:
        fields['lease'] = rl.get_lease(store, namespace)
        if fields['lease'] is None:
            reasons['lease'] = 'no_lease_row'
        ok_groups += 1
    except Exception as ex:
        reasons['lease'] = _read_reason(ex)

    # --- box conflict (trial only, read-only): the box lease and the
    # active_runner flag for paper_v2. No write, no threshold: the
    # heartbeat age is reported and the operator decides what is a
    # conflict.
    if mode == 'trial':
        try:
            lease_v2 = rl.get_lease(store, 'paper_v2')
            flag_v2 = rl.get_active_runner(store, 'paper_v2')
            hb = _parse_ts((lease_v2 or {}).get('heartbeat_at'))
            fields['box_conflict_check'] = {
                'paper_v2_heartbeat_present': bool(
                    lease_v2 and lease_v2.get('heartbeat_at')),
                'paper_v2_heartbeat_age_s': (
                    int((now - hb).total_seconds())
                    if hb is not None else None),
                'paper_v2_active_runner': flag_v2}
            if lease_v2 is None:
                reasons['box_conflict_check'] = 'no_paper_v2_lease'
            elif hb is None:
                reasons['box_conflict_check'] = 'heartbeat_unparseable'
            ok_groups += 1
        except Exception as ex:
            reasons['box_conflict_check'] = _read_reason(ex)

    return {'ok': ok_groups > 0, 'fields': fields, 'reasons': reasons}


def build_snapshot(mode, namespace, active_runner, lease_held, lease, worker,
                   jobs, uptime_seconds, memory_mb,
                   state=None, role=None, runner_id=None, epoch=None,
                   now_ist=None, session_open=None,
                   calendar_valid_through=None, calendar_days_left=None,
                   last_tick_ist=None, last_lease_renew_age_s=None,
                   jobs_today=None, last_job=None, rss_mb=None,
                   children_rss_mb=None, fyers_budget=None, auth=None,
                   git_sha=None, version=None, trial_budget=None,
                   box_conflict_check=None, login_allowed=None,
                   breakers=None, store_ok=None, store_age_s=None,
                   reasons=None, peak_job_rss_mb=None,
                   last_job_peak_rss_mb=None, job_rss_cap_mb=None):
    """Build the /health payload (brief 01 Task 6.3).

    The first nine arguments are the original snapshot inputs and keep
    their meaning; every new field is an optional keyword so existing
    callers keep working. Values the caller does not supply are derived
    from the inputs where possible (state/role/epoch/ok) and left null
    where they would need a store read (see the module docstring).
    `trial_budget` and `box_conflict_check` appear only in trial mode.
    `breakers` is {job: info} for open crash-loop breakers (Task 2):
    an open breaker makes the state 'degraded' but leaves ok true
    (HTTP 200) — the supervisor is up and the worker runs, it just
    will not start that job again today.
    Task 3 fields come from the background refresher
    (`refresh_store_fields`): `store_ok` (the last refresh read the
    store), `store_age_s` (seconds since the last successful refresh,
    null before the first), `reasons` (per-field reasons for null
    values), `peak_job_rss_mb` / `last_job_peak_rss_mb` (largest
    job-tree peak today / the last job's peak, from the guard's sampled
    RSS in the job rows), `job_rss_cap_mb` (the cap in force).
    """
    if active_runner != 'render':
        status = 'idle_not_active_runner'
    elif not lease_held:
        status = 'lease_lost'
    elif worker.get('state') == 'crashed':
        status = 'degraded'
    elif breakers:
        status = 'degraded'
    else:
        status = 'ok'
    ok = status in ('ok', 'idle_not_active_runner')
    if status == 'degraded' and breakers and lease_held and \
            worker.get('state') != 'crashed':
        ok = True
    if state is None:
        if active_runner != 'render':
            state = 'idle_not_active_runner'
        elif worker.get('state') == 'crashed':
            state = 'degraded'
        elif breakers:
            state = 'degraded'
        elif not lease_held:
            state = 'standby'
        elif worker.get('state') == 'running':
            state = 'active'
        else:
            state = 'starting'
    if role is None:
        if lease_held and active_runner == 'render':
            role = 'active'
        elif active_runner == 'render':
            role = 'standby'
        else:
            role = 'idle'
    worker = {
        'state': worker.get('state', 'stopped'),
        'pid': worker.get('pid'),
        'rss_mb': worker.get('rss_mb', 0),
        'restarts': worker.get('restarts', 0),
        'restarts_10m': worker.get('restarts_10m'),
        'last_heartbeat_age_s': worker.get('last_heartbeat_age_s'),
    }
    if login_allowed is None:
        import render_trial as rt
        login_allowed = rt.login_allowed(mode, os.environ)
    snap = {
        'ok': ok,
        'status': status,
        'mode': mode,
        'state': state,
        'namespace': namespace,
        'role': role,
        'runner_id': runner_id,
        'epoch': epoch if epoch is not None
                 else (lease or {}).get('epoch'),
        'uptime_s': round(uptime_seconds, 1),
        'uptime_seconds': round(uptime_seconds, 1),
        'now_ist': now_ist if now_ist is not None
                   else datetime.now(IST).isoformat(),
        'session_open': session_open,
        'calendar_valid_through': calendar_valid_through,
        'calendar_days_left': calendar_days_left,
        'active_runner': active_runner,
        'lease': {'held': bool(lease_held),
                  'owner_id': (lease or {}).get('owner_id'),
                  'epoch': (lease or {}).get('epoch')} if lease else
                 {'held': bool(lease_held), 'owner_id': None, 'epoch': None},
        'worker': worker,
        'last_tick_ist': last_tick_ist,
        'last_lease_renew_age_s': last_lease_renew_age_s,
        'jobs': jobs,
        'jobs_today': jobs_today if jobs_today is not None else {},
        'last_job': last_job,
        'breakers': breakers if breakers is not None else {},
        'store_ok': store_ok,
        'store_age_s': store_age_s,
        'reasons': reasons if reasons is not None else {},
        'peak_job_rss_mb': peak_job_rss_mb,
        'last_job_peak_rss_mb': last_job_peak_rss_mb,
        'job_rss_cap_mb': job_rss_cap_mb,
        'rss_mb': rss_mb if rss_mb is not None else memory_mb,
        'memory_mb': memory_mb,
        'children_rss_mb': (children_rss_mb if children_rss_mb is not None
                            else worker['rss_mb']),
        'fyers_budget': fyers_budget if fyers_budget is not None
                        else {'day_used': None, 'per_day_limit': None},
        'auth': auth if auth is not None else {'circuit_open': None},
        'login_allowed': login_allowed,
        'git_sha': git_sha if git_sha is not None
                   else os.environ.get('RENDER_GIT_COMMIT'),
        'version': version if version is not None else VERSION,
    }
    if mode == 'trial':
        if trial_budget is None:
            import render_trial as rt
            caps = rt.trial_caps()
            trial_budget = {'day_used': None, 'day_cap': caps['day'],
                            'minute_cap': caps['minute']}
        snap['trial_budget'] = trial_budget
        snap['box_conflict_check'] = (
            box_conflict_check if box_conflict_check is not None
            else {'paper_v2_heartbeat_present': None,
                  'paper_v2_heartbeat_age_s': None,
                  'paper_v2_active_runner': None})
    return snap


def health_response(snapshot):
    """(http_code, payload): 200 for ok/idle, 503 for lease_lost/degraded.

    The code follows the snapshot's `ok` flag (true exactly when the
    HTTP status is 200); snapshots without `ok` fall back to `status`.
    """
    ok = snapshot.get('ok')
    if ok is None:
        ok = snapshot.get('status') in ('ok', 'idle_not_active_runner')
    return (200, snapshot) if ok else (503, snapshot)
