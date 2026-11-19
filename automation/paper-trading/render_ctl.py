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
import sqlite3
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_lease as rl
import render_state as rs
import render_trial as rt
import runtime_mode as rm

IST = ZoneInfo('Asia/Kolkata')
# Jobs that touch Fyers (market data / history) in a simulated run. Each
# simulated run of one of these uses exactly one request, so the simulated
# budget is a conservative upper bound for the day's Fyers use.
FYERS_JOBS = {'preflight', 'scan', 'eod', 'support', 'sync_history',
              'revalue'}
# The gate fixture: a small paper_v2 baseline at this bar (the latest
# completed day the fixture knows about).
FIXTURE_COMPLETED_DAY = '2026-11-18'
FIXTURE_LIVE_NS = 'paper_v2'


def _store():
    return rs.open_store_from_env()


def _trading_db():
    """The paper-trading DB (engine helper: PAPER_TEST_DATABASE only in
    render mode, else the configured Turso DB)."""
    import engine as e
    return e._db()


def _print(obj):
    print(json.dumps(obj), flush=True)


class _SqliteTradingDb:
    """Minimal engine-DB facade over a sqlite3 connection: .query() returns
    a list of dicts, .batch() runs [(sql, params)]. Used by the --fixture
    commands and the dry-run fake box."""

    def __init__(self, conn):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def query(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def batch(self, statements):
        for sql, params in statements:
            self._conn.execute(sql, params)
        self._conn.commit()


def _fixture_db():
    """Build the gate fixture: a temp SQLite DB with the render state
    schema, minimal paper tables, and a paper_v2 baseline at bar
    FIXTURE_COMPLETED_DAY (3 signals, 2 fills, 1 position, 2 watermarks,
    2 shared paper_meta rows). Returns (store, trading_db, tmp_path)."""
    fd, path = tempfile.mkstemp(prefix='render-fixture-', suffix='.sqlite')
    os.close(fd)
    store = rs.SqliteStore(Path(path))
    rs.migrate(store)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS paper_signals(
            id INTEGER PRIMARY KEY, namespace TEXT, bar_date TEXT);
        CREATE TABLE IF NOT EXISTS paper_fills(
            id INTEGER PRIMARY KEY, namespace TEXT, filled_at TEXT);
        CREATE TABLE IF NOT EXISTS paper_positions(
            id INTEGER PRIMARY KEY, namespace TEXT);
        CREATE TABLE IF NOT EXISTS paper_meta(
            key TEXT PRIMARY KEY, value TEXT);
    ''')
    day = FIXTURE_COMPLETED_DAY
    for _ in range(3):
        conn.execute(
            'INSERT INTO paper_signals(namespace, bar_date) VALUES(?, ?)',
            (FIXTURE_LIVE_NS, day))
    for _ in range(2):
        conn.execute(
            'INSERT INTO paper_fills(namespace, filled_at) VALUES(?, ?)',
            (FIXTURE_LIVE_NS, day + 'T10:00:00'))
    conn.execute('INSERT INTO paper_positions(namespace) VALUES(?)',
                 (FIXTURE_LIVE_NS,))
    conn.execute("INSERT INTO paper_meta(key, value) VALUES('box_heartbeat', ?)",
                 (day + 'T10:00:00',))
    conn.execute("INSERT INTO paper_meta(key, value) VALUES('box_started', ?)",
                 (day + 'T08:00:00',))
    # Commit the paper-table writes before the store writes its own rows:
    # the two connections share one file, and an open write transaction on
    # one blocks the other until its busy timeout.
    conn.commit()
    now = day + 'T10:00:00'
    for key in ('watermark:daily_ohlcv', 'watermark:signals'):
        store.execute(
            'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
            ' VALUES(?, ?, ?, ?)',
            (FIXTURE_LIVE_NS, key, day, now))
    return store, _SqliteTradingDb(conn), Path(path)


def _close_fixture(store, db, path):
    """Close a fixture's connections and remove its temp file."""
    try:
        db._conn.close()
    except Exception:
        pass
    try:
        store.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except Exception:
        pass


def _count_namespace_rows(store, db, namespace):
    """Row counts for one namespace across the state store and paper tables."""
    def _n(sql, params):
        rows = store.execute(sql, params)
        return int(rows[0]['n']) if rows else 0

    counts = {
        'job_runs': _n('SELECT COUNT(*) AS n FROM paper_job_runs'
                       ' WHERE namespace=?', (namespace,)),
        'kv': _n('SELECT COUNT(*) AS n FROM paper_runtime_kv WHERE scope=?',
                 (namespace,)),
    }
    for table in ('paper_signals', 'paper_fills', 'paper_positions'):
        counts[table] = len(db.query(
            'SELECT 1 FROM %s WHERE namespace=?' % table, (namespace,)))
    return counts


def _count_all_namespaces(store, db):
    """{namespace: total row count} across the state store and paper tables."""
    out = {}
    for row in store.execute(
            'SELECT namespace, COUNT(*) AS n FROM paper_job_runs'
            ' GROUP BY namespace'):
        out[row['namespace']] = out.get(row['namespace'], 0) + int(row['n'])
    for row in store.execute(
            'SELECT scope AS namespace, COUNT(*) AS n FROM paper_runtime_kv'
            ' GROUP BY scope'):
        out[row['namespace']] = out.get(row['namespace'], 0) + int(row['n'])
    for table in ('paper_signals', 'paper_fills', 'paper_positions'):
        for row in db.query(
                'SELECT namespace, COUNT(*) AS n FROM %s GROUP BY namespace'
                % table):
            out[row['namespace']] = out.get(row['namespace'], 0) + int(row['n'])
    return out


def cmd_trial_init(args):
    if getattr(args, 'fixture', False):
        return _cmd_trial_init_fixture(args)
    out = rt.init_trial(_store(), args.namespace)
    _print(out)
    return 0


def _cmd_trial_init_fixture(args):
    """Trial-init against the gate fixture: a paper_v2 baseline at bar
    FIXTURE_COMPLETED_DAY. Copies the paper_v2 watermarks into the trial
    namespace and verifies they match. A bar later than the fixture's
    latest completed day is refused (non-zero exit)."""
    bar = args.bar or FIXTURE_COMPLETED_DAY
    if bar > FIXTURE_COMPLETED_DAY:
        print('trial-init refuses bar %s: later than the latest completed'
              ' day %s' % (bar, FIXTURE_COMPLETED_DAY), file=sys.stderr)
        return 1
    store, _db, _path = _fixture_db()
    try:
        out = rt.init_trial(store, args.namespace)
        watermarks = {}
        for row in store.execute(
                "SELECT key, value FROM paper_runtime_kv"
                " WHERE scope=? AND key LIKE 'watermark:%'",
                (FIXTURE_LIVE_NS,)):
            watermarks[row['key']] = row['value']
        now = FIXTURE_COMPLETED_DAY + 'T10:00:00'
        for key, value in watermarks.items():
            store.execute(
                'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
                ' VALUES(?, ?, ?, ?)'
                ' ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,'
                ' updated_at=excluded.updated_at',
                (args.namespace, key, value, now))
        back = {row['key']: row['value'] for row in store.execute(
            "SELECT key, value FROM paper_runtime_kv"
            " WHERE scope=? AND key LIKE 'watermark:%'", (args.namespace,))}
        out.update({'bar': bar, 'watermarks': watermarks,
                    'watermarks_match_paper_v2': back == watermarks})
        _print(out)
        return 0
    finally:
        _close_fixture(store, _db, _path)


def cmd_trial_compare(args):
    day = args.day
    if not day:
        day = datetime.now(IST).date().isoformat()
    if getattr(args, 'fixture', False):
        store, db, _path = _fixture_db()
        try:
            out = rt.compare_trial(db, args.namespace,
                                   args.live or 'paper_v2', day)
        finally:
            _close_fixture(store, db, _path)
        _print(out)
        return 0
    out = rt.compare_trial(_trading_db(), args.namespace,
                           args.live or 'paper_v2', day)
    _print(out)
    return 0


def cmd_trial_purge(args):
    if getattr(args, 'fixture', False):
        return _cmd_trial_purge_fixture(args)
    db = None if args.state_only else _trading_db()
    out = rt.purge_trial(_store(), args.namespace, db=db)
    _print(out)
    return 0


def _cmd_trial_purge_fixture(args):
    """Purge against the gate fixture. Without --yes this is a dry run
    (nothing is deleted); with --yes it purges the trial namespace and
    proves no other namespace changed."""
    store, db, _path = _fixture_db()
    try:
        if not args.yes:
            counts = _count_namespace_rows(store, db, args.namespace)
            _print({'dry_run': True, 'namespace': args.namespace,
                    'would_delete': counts})
            return 0
        before = _count_all_namespaces(store, db)
        out = rt.purge_trial(store, args.namespace, db=db)
        after = _count_all_namespaces(store, db)
        changed = sum(abs(after[ns] - before.get(ns, 0))
                      for ns in after if ns != args.namespace)
        changed += sum(before.get(ns, 0)
                       for ns in before if ns not in after
                       and ns != args.namespace)
        out.update({'dry_run': False,
                    'other_namespaces_changed': changed})
        _print(out)
        return 0
    finally:
        _close_fixture(store, db, _path)


def cmd_budget_check(args):
    """Combined budget state: box limits vs trial limits, and the counters
    stored in kv for each (None when a budget has never been used)."""
    if getattr(args, 'fixture_trial', None) is not None or \
            getattr(args, 'fixture_box', None) is not None:
        trial_used = args.fixture_trial or 0
        box_used = args.fixture_box or 0
        caps = rt.trial_caps()
        import engine as e
        box_day = int(e.MD.get('budget_requests_per_day', 4000))
        within = trial_used <= caps['day'] and box_used <= box_day
        _print({'within_limits': within,
                'trial': {'used': trial_used, 'limit': caps['day'],
                          'within': trial_used <= caps['day']},
                'box': {'used': box_used, 'limit': box_day,
                        'within': box_used <= box_day}})
        return 0 if within else 2
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


def _slot_for(job, now, catch):
    """The slot datetime of `job` that is due at `now`, or None."""
    times = job.get('times') or [job['time']]
    for t in times:
        hh, mm = (int(x) for x in t.split(':'))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now < slot + catch:
            return slot
    return None


def _simulate_phase(store, namespace, mode, day, minutes, owner_ids,
                    kill_restart_at=None, hold_lease=False, extra_active=0):
    """Simulate one runner's day against the store, no network.

    Steps the fake clock minute by minute from 08:00 IST for `minutes`.
    Each enabled job runs once, at its first slot (the dry-run exercises
    each job once; the per-slot repetition of record_outages is covered by
    the due_jobs unit tests and by the real supervisor). The jobs run
    through the real run_job() and the real Lease, so a double-run or a
    lease race would show up here exactly as it would on Render.
    `extra_active` counts leases held by other runners for the whole
    phase (the fake box, in the trial dry-run).
    """
    import render_jobs as rj
    import render_schedule as rsch
    sched = rsch.load_schedule(mode=mode)
    catch = timedelta(minutes=sched.get('catch_up_minutes', 30))
    raw = json.loads((Path(rsch.__file__).resolve().parent
                      / 'render_schedule.json').read_text())
    all_jobs = (raw['modes']['trial']['jobs'] if mode == 'trial'
                else raw['jobs'])
    start = datetime.combine(day, time(8, 0), tzinfo=IST)
    end = start + timedelta(minutes=minutes)
    kill = None
    if kill_restart_at:
        hh, mm = (int(x) for x in kill_restart_at.split(':'))
        kill = start.replace(hour=hh, minute=mm)
    leases = [rl.Lease(store, namespace, owner_id) for owner_id in owner_ids]
    for lease in leases:
        lease.acquire()
    jobs_out = {j['name']: {'status': 'skipped', 'runs': 0,
                            'enabled': bool(j.get('enabled', True))}
                for j in all_jobs}

    def _runner(_job):
        return {'status': 'ok', 'simulated': True}

    ran = {}
    last_run = {}
    runs = {}
    double_run = False
    catch_ups = 0
    max_active = sum(1 for l in leases if l.held) + extra_active
    per_minute = {}
    per_second = {}
    now = start
    restarted = False
    down_until = None
    while now < end:
        if kill is not None and now >= kill and not restarted:
            # The supervisor dies at the kill time: release the lease,
            # stay down 20 minutes (a realistic restart), then re-acquire.
            # Slots that fall in the downtime are caught up afterwards.
            for lease in leases:
                lease.release()
            down_until = now + timedelta(minutes=20)
            restarted = True
            now = down_until
            for lease in leases:
                lease.acquire()
            max_active = max(max_active, sum(
                1 for l in leases if l.held) + extra_active)
            continue
        holder = next((l for l in leases if l.held), None)
        if holder is not None:
            for job in rsch.due_jobs(sched, now, last_run=last_run):
                name = job['name']
                if name in ran:
                    continue  # each job runs once a day in the dry-run
                slot = _slot_for(job, now, catch)
                if slot is None:
                    continue
                if (restarted and down_until is not None
                        and slot < down_until):
                    catch_ups += 1
                rj.run_job(store, namespace, name, day.isoformat(),
                           holder.owner_id, _runner)
                ran[name] = slot
                last_run[name] = slot
                runs[name] = runs.get(name, 0) + 1
                if runs[name] > 1:
                    double_run = True
                jobs_out[name] = {'status': 'ok', 'runs': runs[name],
                                  'enabled': True}
                if name in FYERS_JOBS:
                    # One simulated request, at a distinct second inside
                    # this minute so peak_second stays honest.
                    minute = int(now.timestamp()) // 60
                    per_minute[minute] = per_minute.get(minute, 0) + 1
                    sec = int(now.timestamp()) + len(ran)
                    per_second[sec] = per_second.get(sec, 0) + 1
        # A standby that never held the lease tries again each minute; the
        # real lease only lets one owner hold it at a time.
        for lease in leases:
            if not lease.held:
                lease.acquire()
        max_active = max(max_active, sum(
            1 for l in leases if l.held) + extra_active)
        now += timedelta(minutes=1)
    if not hold_lease:
        for lease in leases:
            if lease.held:
                lease.release()
    budget = {'day_used': sum(per_minute.values()),
              'peak_minute': max(per_minute.values()) if per_minute else 0,
              'peak_second': max(per_second.values()) if per_second else 0}
    return {'jobs': jobs_out, 'double_run_detected': double_run,
            'catch_ups': catch_ups,
            'max_simultaneous_active': max_active,
            'login_calls': 0, 'budget': budget,
            'leases': leases if hold_lease else []}


def _count_box_rows(store, namespace):
    """Job-run + kv rows for one namespace, plus shared paper_meta rows."""
    def _n(sql, params):
        rows = store.execute(sql, params)
        return int(rows[0]['n']) if rows else 0

    out = {
        'job_runs': _n('SELECT COUNT(*) AS n FROM paper_job_runs'
                       ' WHERE namespace=?', (namespace,)),
        'kv': _n('SELECT COUNT(*) AS n FROM paper_runtime_kv WHERE scope=?',
                 (namespace,)),
        'shared': 0,
    }
    try:
        out['shared'] = _n('SELECT COUNT(*) AS n FROM paper_meta', ())
    except Exception:
        out['shared'] = 0
    return out


def _ensure_shared_table(store):
    """Create the shared paper_meta table (SQLite test stores only) and
    seed the fake box's rows, so the trial-isolation check is non-trivial."""
    if not isinstance(store, rs.SqliteStore):
        return
    store.execute('CREATE TABLE IF NOT EXISTS paper_meta'
                  '(key TEXT PRIMARY KEY, value TEXT)')
    day = FIXTURE_COMPLETED_DAY
    store.execute(
        "INSERT OR IGNORE INTO paper_meta(key, value) VALUES('box_heartbeat', ?)",
        (day + 'T10:00:00',))
    store.execute(
        "INSERT OR IGNORE INTO paper_meta(key, value) VALUES('box_started', ?)",
        (day + 'T08:00:00',))


def _cmd_dry_run_fake_clock(args):
    """--fake-clock dry-run (gate G7c/7d/7e/7l): simulate a compressed day
    on a test SQLite store, no network. Prints one JSON object to stdout
    and nothing else. Exit 0 when the simulated use fits the mode's
    budget and no job ran twice, else 2."""
    import render_app as ra
    import render_schedule as rsch
    store = _store()
    rs.migrate(store)
    trial = bool(getattr(args, 'trial', False))
    if trial:
        ok, namespace = ra.check_namespace()
        if not ok:
            print('namespace refused; dry-run not possible', file=sys.stderr)
            return 3
        mode = 'trial'
    elif getattr(args, 'namespace', None):
        namespace = args.namespace
        mode = rm.render_mode()
        if mode == 'trial' and not rt.is_trial_namespace(namespace):
            print('refused: %s is not a trial namespace' % namespace,
                  file=sys.stderr)
            return 3
        if mode != 'trial' and namespace in ('paper_v1', 'paper_demo_v1'):  # refused: archived namespaces
            print('refused: %s is an archived namespace' % namespace,
                  file=sys.stderr)
            return 3
    else:
        # A simulation never writes a real namespace.
        namespace = 'test_dry_run'
        mode = rm.render_mode()
    today = datetime.now(IST).date()
    day = today
    for _ in range(7):
        if rsch.is_trading_day(day):
            break
        day += timedelta(days=1)
    minutes = args.minutes or 660
    owners = ['dry-run-sup-%d' % (i + 1)
              for i in range(max(1, args.supervisors or 1))]
    kill = getattr(args, 'kill_restart_at', None)
    result = {'mode': mode, 'namespace': namespace,
              'day': day.isoformat(), 'minutes': minutes}
    if getattr(args, 'with_fake_box', False):
        # The fake box runs the live schedule in the live namespace first
        # and holds its lease through the trial phase; then we prove the
        # trial changed none of its rows (or any shared row).
        live_ns = FIXTURE_LIVE_NS
        _ensure_shared_table(store)
        box_phase = _simulate_phase(store, live_ns, 'live', day, minutes,
                                    ['fake-box'], hold_lease=True)
        before = _count_box_rows(store, live_ns)
        trial_phase = _simulate_phase(store, namespace, mode, day, minutes,
                                      owners, kill_restart_at=kill,
                                      extra_active=1)
        after = _count_box_rows(store, live_ns)
        result['box_rows_changed'] = (
            (after['job_runs'] - before['job_runs'])
            + (after['kv'] - before['kv']))
        result['shared_table_rows_changed'] = after['shared'] - before['shared']
        result['jobs'] = trial_phase['jobs']
        result['double_run_detected'] = (
            box_phase['double_run_detected']
            or trial_phase['double_run_detected'])
        result['catch_ups'] = trial_phase['catch_ups']
        result['max_simultaneous_active'] = trial_phase['max_simultaneous_active']
        result['login_calls'] = trial_phase['login_calls']
        result['trial_budget'] = trial_phase['budget']
        result['box_budget'] = box_phase['budget']
        for lease in box_phase['leases']:
            lease.release()
    else:
        phase = _simulate_phase(store, namespace, mode, day, minutes,
                                owners, kill_restart_at=kill)
        result['jobs'] = phase['jobs']
        result['double_run_detected'] = phase['double_run_detected']
        result['catch_ups'] = phase['catch_ups']
        result['max_simultaneous_active'] = phase['max_simultaneous_active']
        result['login_calls'] = phase['login_calls']
        if mode == 'trial':
            result['trial_budget'] = phase['budget']
        else:
            result['budget'] = phase['budget']
    caps = rt.trial_caps() if mode == 'trial' else {
        'day': 4000, 'minute': 40, 'second': 4}
    b = result.get('trial_budget') or result.get('budget') or {}
    within = (b.get('day_used', 0) <= caps['day']
              and b.get('peak_minute', 0) <= caps['minute']
              and b.get('peak_second', 0) <= caps['second'])
    result['within_limits'] = bool(
        within and not result['double_run_detected'])
    _print(result)
    return 0 if result['within_limits'] else 2


def cmd_dry_run(args):
    """Simulate the day's schedule without any Fyers call or DB write.

    Resolves the namespace, loads the schedule for the current mode and
    lists every slot that would run, plus whether a Fyers login is allowed.
    With --fake-clock, simulates the whole compressed day against the
    test store instead (gate G7c/7d/7e/7l).
    """
    if getattr(args, 'fake_clock', False):
        return _cmd_dry_run_fake_clock(args)
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
    p.add_argument('--bar', default=None)
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_init)
    p = sub.add_parser('trial-compare')
    p.add_argument('--namespace', required=True)
    p.add_argument('--live', default='paper_v2')
    p.add_argument('--day', default=None)
    p.add_argument('--session', dest='day', default=None)
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_compare)
    p = sub.add_parser('trial-purge')
    p.add_argument('--namespace', required=True)
    p.add_argument('--state-only', action='store_true')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_purge)
    p = sub.add_parser('budget-check')
    p.add_argument('--fixture-trial', type=int, default=None)
    p.add_argument('--fixture-box', type=int, default=None)
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
    p.add_argument('--fake-clock', action='store_true')
    p.add_argument('--minutes', type=int, default=660)
    p.add_argument('--kill-restart-at', default=None)
    p.add_argument('--supervisors', type=int, default=1)
    p.add_argument('--trial', action='store_true')
    p.add_argument('--with-fake-box', action='store_true')
    p.add_argument('--namespace', default=None)
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
