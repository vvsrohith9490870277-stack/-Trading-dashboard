"""Health snapshot for the Render supervisor (Task 6).

/health is 200 normally and 503 only when something is actually wrong:
- idle_not_active_runner (the handover flag names the box, or is unset):
  the supervisor is correctly idle, so 200.
- ok: the flag names render, the lease is held, the worker is running: 200.
- lease_lost / degraded: the flag names render but the lease is not held,
  the store is unreachable, or the worker keeps crashing: 503.
"""
import os

MEMORY_LIMIT_MB = 450


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


def memory_over_limit(rss_mb, limit=MEMORY_LIMIT_MB):
    return rss_mb > limit


def build_snapshot(mode, namespace, active_runner, lease_held, lease, worker,
                   jobs, uptime_seconds, memory_mb):
    if active_runner != 'render':
        status = 'idle_not_active_runner'
    elif not lease_held:
        status = 'lease_lost'
    elif worker.get('state') == 'crashed':
        status = 'degraded'
    else:
        status = 'ok'
    return {
        'status': status,
        'mode': mode,
        'namespace': namespace,
        'active_runner': active_runner,
        'lease': {'held': bool(lease_held),
                  'owner_id': (lease or {}).get('owner_id'),
                  'epoch': (lease or {}).get('epoch')} if lease else
                 {'held': bool(lease_held), 'owner_id': None, 'epoch': None},
        'worker': worker,
        'jobs': jobs,
        'uptime_seconds': round(uptime_seconds, 1),
        'memory_mb': memory_mb,
    }


def health_response(snapshot):
    """(http_code, payload): 200 for ok/idle, 503 for lease_lost/degraded."""
    if snapshot['status'] in ('ok', 'idle_not_active_runner'):
        return 200, snapshot
    return 503, snapshot
