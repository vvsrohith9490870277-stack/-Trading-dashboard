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
"""
import os
from datetime import datetime
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
                   breakers=None):
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
                  'paper_v2_heartbeat_age_s': None})
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
