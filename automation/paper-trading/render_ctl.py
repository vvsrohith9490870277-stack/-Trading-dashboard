"""Render control CLI (Tasks 7 and 10, brief render-deploy-01).

Operator tooling for the Render deployment. It never touches the live box
and never logs a secret. Task 10 ships the trial commands and the combined
budget check; Task 7 adds migrate/status/handoff/keys/token/net-check/
dry-run/footprint.

Usage: python render_ctl.py <command> [args]
  trial-init --namespace paper_render_trial_v1
  trial-compare --namespace paper_render_trial_v1 [--live paper_v2] [--day YYYY-MM-DD]
  trial-purge --namespace paper_render_trial_v1 [--state-only]
  budget-check
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_lease as rl
import render_state as rs
import render_trial as rt
import runtime_mode as rm


def _store():
    return rs.open_store_from_env()


def _trading_db():
    """The paper-trading DB (engine helper: PAPER_TEST_DATABASE only in
    render mode, else the configured Turso DB)."""
    import engine as e
    return e._db()


def _print(obj):
    print(json.dumps(obj), flush=True)


def cmd_trial_init(args):
    out = rt.init_trial(_store(), args.namespace)
    _print(out)
    return 0


def cmd_trial_compare(args):
    day = args.day
    if not day:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    out = rt.compare_trial(_trading_db(), args.namespace,
                           args.live or 'paper_v2', day)
    _print(out)
    return 0


def cmd_trial_purge(args):
    db = None if args.state_only else _trading_db()
    out = rt.purge_trial(_store(), args.namespace, db=db)
    _print(out)
    return 0


def cmd_budget_check(args):
    """Combined budget state: box limits vs trial limits, and the counters
    stored in kv for each (None when a budget has never been used)."""
    import token_store as ts
    store = _store()
    tstore = ts.TokenStore(store)
    box = tstore.get_budget(trial=False)
    trial = tstore.get_budget(trial=True)
    import engine as e
    md = e.MD
    caps = rt.trial_caps()
    out = {
        'box': {'limits': {
            'per_day': int(md.get('budget_requests_per_day', 4000)),
            'per_minute': int(md.get('budget_requests_per_minute', 40)),
            'per_second': int(md.get('budget_requests_per_second', 4)),
            'reserve_for_session_monitor': int(
                md.get('budget_reserve_for_session_monitor', 1500))},
            'state': box},
        'trial': {'limits': {
            'per_day': caps['day'], 'per_minute': caps['minute'],
            'per_second': caps['second'],
            'reserve_for_session_monitor': caps['reserve']},
            'state': trial},
    }
    _print(out)
    return 0


def cmd_status(args):
    """One-line status: mode, namespace, flag, lease, last jobs."""
    store = _store()
    ok, namespace = True, None
    try:
        import render_app as ra
        ok, namespace = ra.check_namespace()
    except Exception:
        namespace = None
    flag = None
    lease = None
    jobs = {}
    if namespace:
        try:
            flag = rl.get_active_runner(store, namespace)
            lease = rl.get_lease(store, namespace)
            import render_jobs as rj
            jobs = {k: v.isoformat() for k, v in
                    rj.last_run_times(store, namespace).items()}
        except Exception:
            pass
    _print({'mode': rm.render_mode(), 'namespace': namespace,
            'namespace_ok': ok, 'active_runner': flag,
            'lease': lease, 'jobs': jobs})
    return 0


def cmd_migrate(args):
    out = rs.migrate(_store())
    _print({'ok': True, **out})
    return 0


def cmd_handoff(args):
    if args.to not in ('box', 'render'):
        print('handoff --to must be box or render', file=sys.stderr)
        return 2
    import render_app as ra
    ok, namespace = ra.check_namespace()
    if not ok:
        print('namespace refused; handoff not applied', file=sys.stderr)
        return 3
    rl.set_active_runner(_store(), namespace, args.to)
    _print({'ok': True, 'namespace': namespace,
            'active_runner': args.to})
    return 0


def cmd_gen_key(args):
    import token_store as ts
    _print({'TOKEN_ENC_KEY': ts.generate_key()})
    return 0


def cmd_seed_budget(args):
    import token_store as ts
    store = _store()
    tstore = ts.TokenStore(store)
    day = args.day
    if not day:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    state = {'day': day, 'requests': args.requests, 'by_route': {},
             'times': []}
    tstore.put_budget(state, trial=args.trial)
    _print({'ok': True, 'trial': args.trial, 'state': state})
    return 0


def cmd_seed_token(args):
    """Store a Fyers token from the environment (never printed).

    Reads FYERS_ACCESS_TOKEN (+ FYERS_REFRESH_TOKEN) from env and stores it
    encrypted under _global/fyers_token. Requires TOKEN_ENC_KEY.
    """
    import token_store as ts
    access = os.environ.get('FYERS_ACCESS_TOKEN')
    if not access:
        print('FYERS_ACCESS_TOKEN is not set', file=sys.stderr)
        return 2
    store = _store()
    tstore = ts.TokenStore(store)
    tstore.put_token({
        'access_token': access,
        'refresh_token': os.environ.get('FYERS_REFRESH_TOKEN', ''),
    })
    _print({'ok': True, 'stored': 'fyers_token'})
    return 0


def cmd_publish_token(args):
    """Publish the box .env token into the Turso token store.

    Reads the access token from the box .env file (default
    runtime_mode.DEFAULT_ENV_FILE, overridable with --env-file) and stores
    it encrypted. The token value is never printed.
    """
    import token_store as ts
    env_file = args.env_file or str(rm.DEFAULT_ENV_FILE)
    try:
        from fyers_auth import load_env as _load_env
        creds = _load_env(Path(env_file))
    except Exception as ex:
        print('cannot read %s: %s' % (env_file, type(ex).__name__),
              file=sys.stderr)
        return 1
    access = creds.get('FYERS_ACCESS_TOKEN')
    if not access:
        print('no FYERS_ACCESS_TOKEN in %s' % env_file, file=sys.stderr)
        return 2
    tstore = ts.TokenStore(_store())
    tstore.put_token({
        'access_token': access,
        'refresh_token': creds.get('FYERS_REFRESH_TOKEN', ''),
    })
    _print({'ok': True, 'stored': 'fyers_token', 'env_file': env_file})
    return 0


def cmd_token_check(args):
    """Report the stored token's metadata only: present, age, access length.

    Never prints the token. With --validate, also reports whether the
    access token still authenticates (one Fyers call, live only).
    """
    import token_store as ts
    store = _store()
    tstore = ts.TokenStore(store)
    tok = tstore.get_token()
    if tok is None:
        _print({'present': False})
        return 0
    out = {'present': True,
           'issued_at': tok.get('issued_at'),
           'access_len': len(tok.get('access_token') or ''),
           'has_refresh': bool(tok.get('refresh_token'))}
    if args.validate:
        import requests
        try:
            r = requests.get(
                'https://api-t1.fyers.in/api/v3/profile',
                headers={'Authorization': 'Bearer ' +
                         tok['access_token']},
                timeout=10)
            out['valid'] = r.status_code == 200
            out['http_status'] = r.status_code
        except Exception as ex:
            out['valid'] = False
            out['error'] = type(ex).__name__
    _print(out)
    return 0


def cmd_net_check(args):
    """Outbound connectivity check with no credentialed calls.

    TCP + TLS handshake only (no HTTP request, no token) to the hosts the
    service needs. --host host:port may repeat; the default list is the
    Turso host (from TURSO_DATABASE_URL) plus the Fyers API hosts.
    """
    import socket
    import ssl
    hosts = list(args.host or [])
    if not hosts:
        url = os.environ.get('TURSO_DATABASE_URL', '')
        if url:
            host = url.split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0]
            if host:
                hosts.append(host + ':443')
        hosts += ['api-t2.fyers.in:443', 'api-t1.fyers.in:443']
    results = []
    for hp in hosts:
        host, _, port = hp.rpartition(':')
        port = int(port) if port else 443
        started = __import__('time').time()
        entry = {'host': hp, 'reachable': False, 'ms': None}
        try:
            sock = socket.create_connection((host, port), timeout=10)
            try:
                ctx = ssl.create_default_context()
                tls = ctx.wrap_socket(sock, server_hostname=host)
                tls.close()
            except Exception:
                sock.close()
            entry['reachable'] = True
        except Exception as ex:
            entry['error'] = type(ex).__name__
        entry['ms'] = round(
            (__import__('time').time() - started) * 1000, 1)
        results.append(entry)
    _print({'hosts': results})
    return 0


def cmd_dry_run(args):
    """Simulate the day's schedule without any Fyers call or DB write.

    Resolves the namespace, loads the schedule for the current mode and
    lists every slot that would run, plus whether a Fyers login is allowed.
    """
    import render_app as ra
    import render_schedule as rsch
    ok, namespace = ra.check_namespace()
    if not ok:
        print('namespace refused; dry-run not possible', file=sys.stderr)
        return 3
    mode = rm.render_mode()
    sched = rsch.load_schedule(mode=mode)
    slots = []
    for job in sched['jobs']:
        slots.append({'name': job['name'],
                      'command': job['command'],
                      'time': job.get('time'),
                      'times': job.get('times')})
    out = {'namespace': namespace, 'mode': mode,
           'timezone': sched['timezone'],
           'fyers_login_allowed': rt.login_allowed(mode, os.environ),
           'slots': slots}
    _print(out)
    return 0


def cmd_footprint(args):
    """Measure the supervisor + worker footprint over --seconds seconds.

    Starts the supervisor with a fake worker against the configured store,
    samples RSS of the supervisor and its worker, and estimates the wall
    time the same CPU work would take at 0.1 CPU (free tier). Reports
    whether the total stays under the 512 MB free limit.
    """
    import signal
    import subprocess
    import time
    seconds = max(1, args.seconds)
    env = dict(os.environ)
    env['PAPER_RUNTIME'] = 'render'
    env['PAPER_FAKE_WORKER'] = '1'
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / 'render_app.py')],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    peak = {'supervisor': 0, 'worker': 0}
    cpu_start = _tree_cpu_seconds(proc.pid)
    cpu_last = cpu_start
    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            for label, pid in (('supervisor', proc.pid),):
                rss = _rss_mb(pid)
                if rss > peak[label]:
                    peak[label] = rss
            # the worker is the supervisor's child
            for pid in _child_pids(proc.pid):
                rss = _rss_mb(pid)
                if rss > peak['worker']:
                    peak['worker'] = rss
            now = _tree_cpu_seconds(proc.pid)
            if now is not None:
                cpu_last = now
            time.sleep(1)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    cpu_seconds = 0.0
    if cpu_start is not None and cpu_last is not None:
        cpu_seconds = max(0.0, cpu_last - cpu_start)
    total = peak['supervisor'] + peak['worker']
    _print({
        'seconds_observed': seconds,
        'supervisor_peak_rss_mb': peak['supervisor'],
        'worker_peak_rss_mb': peak['worker'],
        'total_peak_rss_mb': total,
        'cpu_seconds': round(cpu_seconds, 2),
        'est_wall_at_0_1_cpu_seconds': round(cpu_seconds / 0.1, 1),
        'free_tier_ok': total < 512,
    })
    return 0


def _rss_mb(pid):
    try:
        with open('/proc/%d/status' % pid) as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        return 0
    return 0


def _child_pids(pid):
    try:
        out = subprocess.run(
            ['pgrep', '-P', str(pid)], capture_output=True, text=True,
            timeout=5)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _proc_cpu_seconds(pid):
    try:
        with open('/proc/%d/stat' % pid) as f:
            parts = f.read().rsplit(')', 1)[1].split()
        # fields 14 (utime) and 15 (stime) after the comm field
        utime = int(parts[11])
        stime = int(parts[12])
        import os as _os
        return (utime + stime) / _os.sysconf('SC_CLK_TCK')
    except Exception:
        return None


def _tree_cpu_seconds(pid):
    """CPU seconds for a process plus its children (None if unreadable)."""
    total = _proc_cpu_seconds(pid)
    if total is None:
        return None
    for child in _child_pids(pid):
        c = _proc_cpu_seconds(child)
        if c is not None:
            total += c
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render control CLI')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('trial-init')
    p.add_argument('--namespace', required=True)
    p.set_defaults(fn=cmd_trial_init)
    p = sub.add_parser('trial-compare')
    p.add_argument('--namespace', required=True)
    p.add_argument('--live', default='paper_v2')
    p.add_argument('--day', default=None)
    p.set_defaults(fn=cmd_trial_compare)
    p = sub.add_parser('trial-purge')
    p.add_argument('--namespace', required=True)
    p.add_argument('--state-only', action='store_true')
    p.set_defaults(fn=cmd_trial_purge)
    p = sub.add_parser('budget-check')
    p.set_defaults(fn=cmd_budget_check)
    p = sub.add_parser('status')
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser('migrate')
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser('handoff')
    p.add_argument('--to', required=True)
    p.set_defaults(fn=cmd_handoff)
    p = sub.add_parser('gen-key')
    p.set_defaults(fn=cmd_gen_key)
    p = sub.add_parser('seed-budget')
    p.add_argument('--requests', type=int, required=True)
    p.add_argument('--day', default=None)
    p.add_argument('--trial', action='store_true')
    p.set_defaults(fn=cmd_seed_budget)
    p = sub.add_parser('seed-token')
    p.set_defaults(fn=cmd_seed_token)
    p = sub.add_parser('publish-token')
    p.add_argument('--env-file', default=None)
    p.set_defaults(fn=cmd_publish_token)
    p = sub.add_parser('token-check')
    p.add_argument('--validate', action='store_true')
    p.set_defaults(fn=cmd_token_check)
    p = sub.add_parser('net-check')
    p.add_argument('--host', action='append', default=None)
    p.set_defaults(fn=cmd_net_check)
    p = sub.add_parser('dry-run')
    p.set_defaults(fn=cmd_dry_run)
    p = sub.add_parser('footprint')
    p.add_argument('--seconds', type=int, default=10)
    p.set_defaults(fn=cmd_footprint)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except rt.NamespaceRefused as ex:
        print('refused: %s' % ex, file=sys.stderr, flush=True)
        return 3
    except Exception as ex:
        print('error: %s: %s' % (type(ex).__name__, ex),
              file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
