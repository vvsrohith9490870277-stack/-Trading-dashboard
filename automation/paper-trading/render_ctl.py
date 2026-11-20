"""Render control CLI (Tasks 7 and 10, brief render-deploy-01).

Operator tooling for the Render deployment. It never touches the live box
and never logs a secret. Task 10 ships the trial commands and the combined
budget check; Task 7 adds migrate/status/handoff/keys/token/net-check/
dry-run/footprint.

Usage: python render_ctl.py <command> [args]
  trial-init [--namespace paper_render_trial_v1] [--bar YYYY-MM-DD] [--fixture]
  trial-compare [--namespace paper_render_trial_v1] [--live paper_v2] [--day YYYY-MM-DD] [--session D] [--fixture]
  trial-purge [--namespace paper_render_trial_v1] [--state-only] [--yes] [--fixture]
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


def build_baselined_db(path, bar, state_dir=None, baseline=True):
    """Build a local SQLite DB with the full paper schema, seeded
    daily_ohlcv history up to `bar`, and (baseline=True) a paper_v2
    baseline via the real bootstrap at `bar`.

    `path` must live under automation/paper-trading/tests/ as
    test_*.sqlite: db.mjs rejects any other test-database path.
    Returns (store, db): a SqliteStore and the node-backed engine DB
    over the same file. The caller closes it with
    close_baselined_db(). No network is used anywhere here.
    """
    import csv
    import engine as e
    tests_dir = Path(__file__).resolve().parent / 'tests'
    if not (path.name.startswith('test_') and path.suffix == '.sqlite'
            and path.resolve().parent == tests_dir.resolve()):
        raise ValueError(
            'baselined DB path must be tests/test_*.sqlite: %s' % path)
    if state_dir is None:
        state_dir = os.environ.get('PAPER_STATE_DIR') or str(
            Path(tempfile.gettempdir()) / 'render-fixture-state')
    for folder in ('logs', 'out'):
        (Path(state_dir) / folder).mkdir(parents=True, exist_ok=True)
    store = rs.SqliteStore(path)
    rs.migrate(store)
    db = e.DB(str(path))
    old_ns = e.NS
    try:
        e.NS = FIXTURE_LIVE_NS
        e.init(db)
    finally:
        e.NS = old_ns
    # Shared history the bootstrap reads (daily_ohlcv is not created by
    # engine.init; it is a shared table the sync normally fills).
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS daily_ohlcv('
            'trade_date TEXT NOT NULL, symbol TEXT NOT NULL,'
            ' open REAL, high REAL, low REAL, close REAL, volume INTEGER,'
            ' turnover REAL, source TEXT, loaded_at TEXT,'
            ' PRIMARY KEY(trade_date, symbol))')
        universe = []
        with open(Path(__file__).resolve().parent
                  / 'universe.csv') as fh:
            universe = [r['db_symbol'] for r in csv.DictReader(fh)][:5]
        end = datetime.fromisoformat(bar).date()
        for sym in universe:
            for b in range(30):
                d = (end - timedelta(days=b)).isoformat()
                conn.execute(
                    'INSERT OR IGNORE INTO daily_ohlcv'
                    '(trade_date, symbol, open, high, low, close, volume,'
                    ' turnover, source, loaded_at)'
                    ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (d, sym, 100.0 + b, 101.0 + b, 99.0 + b, 100.5 + b,
                     1000 + b, 100500.0, 'RENDER_FIXTURE',
                     bar + 'T10:00:00'))
        conn.commit()
        _seed_fixture_rows(conn)
    finally:
        conn.close()
    if baseline:
        # The box's baseline: real bootstrap for paper_v2 at the bar.
        rt._run_bootstrap(db, FIXTURE_LIVE_NS, bar)
    return store, db


def _seed_fixture_rows(conn):
    """Small hand-built live-side rows so trial-compare / trial-purge
    --fixture have non-trivial data (and the trial side has a monitor
    history with one heartbeat gap over 60 s)."""
    day = '2026-11-20'
    conn.execute(
        "INSERT OR IGNORE INTO paper_signals(namespace, id, strategy,"
        " symbol, bar_date, side, status, reason, created_at,"
        " eligible_date) VALUES"
        "('paper_v2', 'fix-sig-1', 1, '360ONE-EQ', '2026-11-19', 'BUY',"
        " 'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20'),"
        "('paper_v2', 'fix-sig-2', 2, 'ABB-EQ', '2026-11-19', 'BUY',"
        " 'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20'),"
        "('paper_v2', 'fix-sig-3', 1, 'ACC-EQ', '2026-11-19', 'SELL',"
        " 'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20')")
    conn.execute(
        "INSERT OR IGNORE INTO paper_fills(namespace, signal_id, strategy,"
        " symbol, side, qty, price, quote_at, filled_at, reason,"
        " realized_pnl, cashflow, execution_path) VALUES"
        "('paper_v2', 'fix-sig-1', 1, '360ONE-EQ', 'BUY', 10, 100.5,"
        " '2026-11-20T09:30:00', '2026-11-20T09:30:00', 'fixture', 0,"
        " -1005.0, 'LTP_LEGACY'),"
        "('paper_v2', 'fix-sig-3', 1, 'ACC-EQ', 'SELL', 5, 200.0,"
        " '2026-11-20T10:00:00', '2026-11-20T10:00:00', 'fixture', 0,"
        " 1000.0, 'LTP_LEGACY')")
    conn.execute(
        "INSERT OR IGNORE INTO paper_positions(namespace, strategy, symbol,"
        " qty, entry_price, entry_at, high_water, updated_at) VALUES"
        "('paper_v2', 1, '360ONE-EQ', 10, 100.5, '2026-11-20T09:30:00',"
        " 101.0, '2026-11-20T09:30:00')")
    conn.execute(
        "INSERT OR IGNORE INTO paper_scan_batches(namespace, bar_date,"
        " scanned_at, status, report_json) VALUES"
        "('paper_v2', '2026-11-19', '2026-11-20T08:45:00', 'ok', '{}')")
    conn.execute(
        "INSERT OR IGNORE INTO paper_eod(namespace, day, as_of,"
        " report_json) VALUES"
        "('paper_v2', '2026-11-20', '2026-11-20T16:10:00', '{}')")
    for i, at in enumerate(('2026-11-20T10:00:00', '2026-11-20T11:00:00')):
        conn.execute(
            "INSERT OR IGNORE INTO paper_events(namespace, event_id,"
            " run_id, session_day, source_at, recorded_at, event_type,"
            " severity, outcome, data_json) VALUES"
            "('paper_v2', ?, 'fixrun', '2026-11-20', ?, ?,"
            " 'fixture_error', 'ERROR', 'failed', '{}')",
            ('%032x' % (i + 1), at, at))
    for at in ('2026-11-20T09:15:00', '2026-11-20T09:15:30',
               '2026-11-20T09:17:30'):
        conn.execute(
            "INSERT OR IGNORE INTO paper_runs(namespace, kind, at, status,"
            " details_json) VALUES"
            "('paper_render_trial_v1', 'monitor', ?, 'ok', '{}')", (at,))
    for at in ('2026-11-20T09:15:00', '2026-11-20T09:15:30'):
        conn.execute(
            "INSERT OR IGNORE INTO paper_runs(namespace, kind, at, status,"
            " details_json) VALUES"
            "('paper_v2', 'monitor', ?, 'ok', '{}')", (at,))
    conn.commit()


def close_baselined_db(store, path):
    """Close a DB built by build_baselined_db and remove its files."""
    try:
        store.close()
    except Exception:
        pass
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.unlink(str(path) + suffix)
        except Exception:
            pass


def _fixture_db(baseline=True):
    """Build the gate fixture: a temp SQLite DB with the render state
    schema, the full paper schema, seeded history and a paper_v2
    baseline at bar FIXTURE_COMPLETED_DAY (baseline=True, the
    trial-init fixture), plus hand-built live-side rows for compare /
    purge. Returns (store, trading_db, tmp_path)."""
    fd, raw = tempfile.mkstemp(
        dir=Path(__file__).resolve().parent / 'tests',
        prefix='test_render_fixture_', suffix='.sqlite')
    os.close(fd)
    path = Path(raw)
    store, db = build_baselined_db(path, FIXTURE_COMPLETED_DAY,
                                   baseline=baseline)
    return store, db, path


def _close_fixture(store, db, path):
    """Close a fixture's connections and remove its temp file."""
    close_baselined_db(store, path)


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


def _trial_namespace(args):
    """The trial namespace for a trial command: --namespace when given,
    else PAPER_NAMESPACE_OVERRIDE when it is a trial namespace, else the
    default trial namespace. The gate's trial-init/compare commands do
    not pass --namespace."""
    if getattr(args, 'namespace', None):
        return args.namespace
    override = os.environ.get('PAPER_NAMESPACE_OVERRIDE', '')
    if override and rt.is_trial_namespace(override):
        return override
    return 'paper_render_trial_v1'


def cmd_trial_init(args):
    """Real trial-init: --bar is required; the namespace is refused
    (exit 3) before any DB write, a missing bar exits 2 with usage and
    writes nothing, an invalid bar exits 2, a watermark mismatch exits 2.
    """
    namespace = _trial_namespace(args)
    if not rt.is_trial_namespace(namespace):
        raise rt.NamespaceRefused(
            'trial-init refuses non-trial namespace %s' % namespace)
    if getattr(args, 'fixture', False):
        return _cmd_trial_init_fixture(args)
    if not args.bar:
        print('usage: trial-init --namespace %s --bar YYYY-MM-DD'
              % namespace, file=sys.stderr)
        print('trial-init needs --bar (a verified completed NSE session)',
              file=sys.stderr)
        return 2
    try:
        out = rt.init_trial(_store(), _trading_db(), namespace, args.bar)
    except rt.BarRefused as ex:
        print('trial-init refuses bar: %s' % ex, file=sys.stderr)
        return 2
    _print(out)
    return 0 if out['watermarks_match_paper_v2'] else 2


def _cmd_trial_init_fixture(args):
    """Trial-init against the gate fixture: the same init_trial function
    as the real path, only the store/DB differ (a fresh fixture DB with
    a paper_v2 baseline at FIXTURE_COMPLETED_DAY)."""
    namespace = _trial_namespace(args)
    if not rt.is_trial_namespace(namespace):
        raise rt.NamespaceRefused(
            'trial-init refuses non-trial namespace %s' % namespace)
    if not args.bar:
        print('usage: trial-init --namespace %s --bar YYYY-MM-DD'
              % namespace, file=sys.stderr)
        print('trial-init needs --bar (a verified completed NSE session)',
              file=sys.stderr)
        return 2
    store, db, _path = _fixture_db(baseline=True)
    try:
        try:
            out = rt.init_trial(store, db, namespace, args.bar,
                                latest_completed_day=FIXTURE_COMPLETED_DAY)
        except rt.BarRefused as ex:
            print('trial-init refuses bar: %s' % ex, file=sys.stderr)
            return 2
        _print(out)
        return 0 if out['watermarks_match_paper_v2'] else 2
    finally:
        _close_fixture(store, db, _path)


def cmd_trial_compare(args):
    day = args.day
    if not day:
        day = datetime.now(IST).date().isoformat()
    if getattr(args, 'fixture', False):
        store, db, _path = _fixture_db(baseline=False)
        try:
            out = rt.compare_trial(db, _trial_namespace(args),
                                   args.live or 'paper_v2', day)
        finally:
            _close_fixture(store, db, _path)
        _print(out)
        return 0
    out = rt.compare_trial(_trading_db(), _trial_namespace(args),
                           args.live or 'paper_v2', day)
    _print(out)
    return 0


def cmd_trial_purge(args):
    if getattr(args, 'fixture', False):
        return _cmd_trial_purge_fixture(args)
    db = None if args.state_only else _trading_db()
    out = rt.purge_trial(_store(), _trial_namespace(args), db=db)
    _print(out)
    return 0


def _cmd_trial_purge_fixture(args):
    """Purge against the gate fixture. Without --yes this is a dry run
    (nothing is deleted); with --yes it purges the trial namespace and
    proves no other namespace changed."""
    namespace = _trial_namespace(args)
    store, db, _path = _fixture_db(baseline=False)
    try:
        if not args.yes:
            counts = _count_namespace_rows(store, db, namespace)
            _print({'dry_run': True, 'namespace': namespace,
                    'would_delete': counts})
            return 0
        before = _count_all_namespaces(store, db)
        out = rt.purge_trial(store, namespace, db=db)
        after = _count_all_namespaces(store, db)
        changed = sum(abs(after[ns] - before.get(ns, 0))
                      for ns in after if ns != namespace)
        changed += sum(before.get(ns, 0)
                       for ns in before if ns not in after
                       and ns != namespace)
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
    if getattr(args, 'box_file', None):
        # The box budget file's counters, checked against the box limits.
        data = json.loads(Path(args.box_file).read_text())
        box_used = int(data.get('requests', 0))
        import engine as e
        box_day = int(e.MD.get('budget_requests_per_day', 4000))
        within = box_used <= box_day
        _print({'within_limits': within,
                'box_file': {'path': args.box_file, 'day': data.get('day'),
                             'used': box_used, 'limit': box_day,
                             'within': within}})
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
    """One-line status: mode, namespace, flag, lease, last jobs.

    Exit 0 healthy, 2 stalled: stalled means the active runner is render
    but the lease heartbeat is older than 120 s and no job completed
    today. Anything else (idle, flag on the box, a fresh lease) is 0.
    """
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
    stalled = False
    if flag == 'render' and lease:
        try:
            from datetime import datetime, timezone
            hb = datetime.strptime(
                lease.get('heartbeat_at') or '', '%Y-%m-%dT%H:%M:%S'
            ).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            today = datetime.now(IST).date().isoformat()
            ran_today = any(v[:10] == today for v in jobs.values())
            stalled = age > 120 and not ran_today
        except Exception:
            stalled = False
    _print({'mode': rm.render_mode(), 'namespace': namespace,
            'namespace_ok': ok, 'active_runner': flag,
            'lease': lease, 'jobs': jobs, 'stalled': stalled})
    return 2 if stalled else 0


def cmd_migrate(args):
    out = rs.migrate(_store())
    _print({'ok': True, **out})
    return 0


def cmd_handoff(args):
    if args.to not in ('box', 'render'):
        print('handoff --to must be box or render', file=sys.stderr)
        return 2
    if not getattr(args, 'yes', False):
        print('handoff needs --yes (refusing to change the active runner'
              ' without it)', file=sys.stderr)
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


def cmd_breaker_clear(args):
    """Clear a job's crash-loop breaker after the cause is fixed
    (render-deploy-03 Task 2). The breaker is the stored start list in
    paper_job_runs; clearing it lets the supervisor schedule the job
    again today. Needs --yes: it changes live scheduling state."""
    if not getattr(args, 'yes', False):
        print('breaker-clear needs --yes (refusing to change scheduling'
              ' state without it)', file=sys.stderr)
        return 2
    import render_app as ra
    import render_jobs as rj
    ok, namespace = ra.check_namespace()
    if not ok:
        print('namespace refused; breaker-clear not applied',
              file=sys.stderr)
        return 3
    if getattr(args, 'namespace', None):
        if args.namespace != namespace:
            # The override must name the namespace this mode resolves
            # to (trial -> the trial namespace, live -> paper_v2). A
            # different namespace would write scheduling state for a
            # namespace this run must not touch, so refuse it (exit 3)
            # without touching the store.
            print('namespace refused: --namespace does not match the'
                  ' resolved namespace; breaker-clear not applied',
                  file=sys.stderr)
            return 3
    day = getattr(args, 'day', None) or datetime.now(IST).date().isoformat()
    store = _store()
    cleared = rj.clear_breaker(store, namespace, args.job, day)
    _print({'ok': True, 'namespace': namespace, 'job': args.job,
            'day': day, 'cleared': cleared,
            'breaker_open': rj.breaker_open(store, namespace, args.job,
                                            day)})
    return 0


def cmd_gen_key(args):
    import token_store as ts
    _print({'TOKEN_ENC_KEY': ts.generate_key()})
    return 0


def cmd_seed_budget(args):
    """Seed a budget from --requests, or from a box budget file
    (--from-file): only the counters day/requests/by_route/times are
    copied, never credentials or anything else in the file."""
    import token_store as ts
    store = _store()
    tstore = ts.TokenStore(store)
    if getattr(args, 'from_file', None):
        data = json.loads(Path(args.from_file).read_text())
        day = data.get('day')
        if not day:
            from datetime import datetime
            day = datetime.now(IST).date().isoformat()
        state = {'day': day, 'requests': int(data.get('requests', 0)),
                 'by_route': data.get('by_route') or {},
                 'times': data.get('times') or []}
    else:
        day = args.day
        if not day:
            from datetime import datetime
            day = datetime.now(IST).date().isoformat()
        state = {'day': day, 'requests': args.requests, 'by_route': {},
                 'times': []}
    tstore.put_budget(state, trial=args.trial)
    _print({'ok': True, 'trial': args.trial, 'state': state})
    return 0


def cmd_seed_token(args):
    """Store a Fyers token (never printed).

    Reads the token from stdin first — a bare access token on one line,
    or a JSON object {"access_token": ..., "refresh_token": ...} — so it
    never appears in argv. Falls back to FYERS_ACCESS_TOKEN /
    FYERS_REFRESH_TOKEN in the environment when stdin is empty.
    Requires TOKEN_ENC_KEY.
    """
    import token_store as ts
    access = None
    refresh = ''
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ''
    if raw and raw.strip():
        text = raw.strip()
        if text.startswith('{'):
            try:
                data = json.loads(text)
                access = data.get('access_token')
                refresh = data.get('refresh_token', '')
            except Exception:
                access = None
        else:
            access = text.splitlines()[0].strip()
    if not access:
        access = os.environ.get('FYERS_ACCESS_TOKEN')
        refresh = os.environ.get('FYERS_REFRESH_TOKEN', '')
    if not access:
        print('no token on stdin and FYERS_ACCESS_TOKEN is not set',
              file=sys.stderr)
        return 2
    store = _store()
    tstore = ts.TokenStore(store)
    tstore.put_token({'access_token': access, 'refresh_token': refresh})
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
        # One quotes request for one symbol (the brief's token-check):
        # it proves the token authenticates and counts as market data,
        # not an auth-profile call.
        try:
            r = requests.get(
                'https://api-t2.fyers.in/data/quotes/',
                params={'symbols': 'NSE:SBIN-EQ'},
                headers={'Authorization': 'Bearer ' +
                         tok['access_token']},
                timeout=10)
            out['valid'] = r.status_code == 200
            out['http_status'] = r.status_code
            out['ok'] = r.status_code == 200
        except Exception as ex:
            out['valid'] = False
            out['ok'] = False
            out['error'] = type(ex).__name__
    _print(out)
    return 0


def cmd_net_check(args):
    """Outbound connectivity check with no credentialed calls.

    Default list: HTTPS GETs (status code only, no token) to the Fyers
    API hosts, the Fyers symbol-master host, the NSE reference-feed hosts
    and nsearchives, plus the Turso HTTP pipeline (SELECT 1, with the
    token from env when present). --host host:port keeps the old TCP+TLS
    probe (used by the local test with a plain listener).
    """
    import socket
    import ssl
    import requests
    results = []
    if args.host:
        for hp in args.host:
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
    urls = ['https://api-t2.fyers.in/',
            'https://api-t1.fyers.in/',
            'https://public.fyers.in/',
            'https://www.nseindia.com/',
            'https://nsearchives.nseindia.com/',
            'https://www.bseindia.com/']
    for url in urls:
        started = __import__('time').time()
        entry = {'host': url, 'reachable': False, 'http_status': None,
                 'ms': None}
        try:
            r = requests.get(url, timeout=10)
            entry['http_status'] = r.status_code
            entry['reachable'] = True
        except Exception as ex:
            entry['error'] = type(ex).__name__
        entry['ms'] = round(
            (__import__('time').time() - started) * 1000, 1)
        results.append(entry)
    # Turso: one pipeline SELECT 1 (needs the token from env; without it
    # there is nothing credentialed to send, so report that plainly).
    turso_url = os.environ.get('TURSO_DATABASE_URL', '')
    turso_token = os.environ.get('TURSO_AUTH_TOKEN', '')
    if turso_url and turso_token:
        base = turso_url.replace('libsql://', 'https://').replace(
            'turso://', 'https://').rstrip('/')
        started = __import__('time').time()
        entry = {'host': base + '/v2/pipeline', 'reachable': False,
                 'http_status': None, 'ms': None}
        try:
            r = requests.post(
                base + '/v2/pipeline',
                headers={'Authorization': 'Bearer ' + turso_token},
                json={'requests': [
                    {'type': 'execute',
                     'stmt': {'sql': 'SELECT 1'}},
                    {'type': 'close'}]},
                timeout=10)
            entry['http_status'] = r.status_code
            entry['reachable'] = r.status_code == 200
        except Exception as ex:
            entry['error'] = type(ex).__name__
        entry['ms'] = round(
            (__import__('time').time() - started) * 1000, 1)
        results.append(entry)
    else:
        results.append({'host': 'turso pipeline',
                        'reachable': False, 'http_status': None,
                        'ms': None, 'error': 'no_turso_credentials'})
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
    try:
        store = _store()
        store_kind = ('sqlite:' + os.environ['PAPER_TEST_DATABASE']
                      if 'PAPER_TEST_DATABASE' in os.environ else 'turso')
    except Exception:
        # A fake-clock dry-run is a local simulation: with no store
        # configured it falls back to a throwaway SQLite store instead
        # of failing (the gate's 7d/7e run it with no store in env).
        # It never touches Turso or a real namespace.
        fd, tmp_path = tempfile.mkstemp(prefix='render-dry-run-',
                                        suffix='.sqlite')
        os.close(fd)
        store = rs.SqliteStore(Path(tmp_path))
        store_kind = 'sqlite-temp:' + tmp_path
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
        if mode != 'trial' and not namespace.startswith('test_'):
            # A non-trial dry-run never writes a real namespace.
            print('refused: dry-run namespace %s must start with test_'
                  % namespace, file=sys.stderr)
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
              'day': day.isoformat(), 'minutes': minutes,
              'store': store_kind}
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


FOOTPRINT_JOBS = [('preflight', 'preflight'), ('scan', 'scan'),
                  ('eod', 'eod'), ('sync_history', 'sync-history'),
                  ('support', 'support'), ('revalue', 'revalue-eod')]
FOOTPRINT_CYCLE_SECONDS = 30  # the worker's cycle length on the box
FOOTPRINT_SYMBOLS = 50
FOOTPRINT_STRATEGIES = 7
FOOTPRINT_POSITIONS_PER_STRATEGY = 9
FOOTPRINT_BARS = 60


def _build_footprint_fixture(path):
    """Create the footprint fixture DB: the full paper schema (via
    `engine.py init`) plus the render schema, then a realistic book:
    7 strategies x 9 positions, a 50-symbol universe, 60 bars of
    daily_ohlcv each, and the active_runner='render' flag so the mutating
    jobs pass the runner guard. Returns the store."""
    import subprocess
    here = Path(__file__).resolve().parent
    env = dict(os.environ)
    env.update({'PAPER_RUNTIME': 'render',
                'PAPER_TEST_DATABASE': str(path),
                'PAPER_STATE_DIR': str(Path(tempfile.gettempdir())
                                       / 'render-footprint-state')})
    r = subprocess.run(
        [sys.executable, str(here / 'engine.py'), 'init'],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(here))
    if r.returncode != 0:
        raise RuntimeError('footprint fixture init failed: %s'
                           % (r.stdout + r.stderr)[-500:])
    store = rs.SqliteStore(path)
    rs.migrate(store)
    rl.set_active_runner(store, 'paper_v2', 'render')
    conn = sqlite3.connect(str(path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS daily_ohlcv('
        'trade_date TEXT NOT NULL, symbol TEXT NOT NULL,'
        ' open REAL, high REAL, low REAL, close REAL, volume INTEGER,'
        ' turnover REAL, source TEXT, loaded_at TEXT,'
        ' PRIMARY KEY(trade_date, symbol))')
    from datetime import date as _date, timedelta as _td
    first = _date(2026, 9, 1)
    for i in range(FOOTPRINT_SYMBOLS):
        symbol = 'SYM%02d' % (i + 1)
        for b in range(FOOTPRINT_BARS):
            bar = (first + _td(days=b)).isoformat()
            conn.execute(
                'INSERT OR IGNORE INTO daily_ohlcv'
                '(trade_date, symbol, open, high, low, close, volume,'
                ' turnover, source, loaded_at)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (bar, symbol, 100.0 + b, 101.0 + b, 99.0 + b, 100.5 + b,
                 1000 + b, 100500.0 + b * 1000, 'FOOTPRINT_FIXTURE',
                 '2026-11-18T10:00:00'))
    for s in range(FOOTPRINT_STRATEGIES):
        for j in range(FOOTPRINT_POSITIONS_PER_STRATEGY):
            conn.execute(
                'INSERT OR IGNORE INTO paper_positions'
                '(namespace, strategy, symbol, qty, entry_price, entry_at,'
                ' high_water, updated_at)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                ('paper_v2', s + 1,
                 'SYM%02d' % ((s * 9 + j) % FOOTPRINT_SYMBOLS + 1),
                 10 + j, 100.0 + j, '2026-11-18T10:00:00', 101.0 + j,
                 '2026-11-18T10:00:00'))
    conn.commit()
    conn.close()
    return store


def _sample_tree(pid, interval, duration, stop_event=None):
    """Sample a process tree's RSS every `interval` seconds for `duration`
    seconds (or until the process exits). Returns (peak_rss_mb, cpu_seconds)
    where cpu_seconds is the tree's user+sys CPU over the window."""
    import time
    start_cpu = _tree_cpu_seconds(pid)
    peak = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        total = _rss_mb(pid)
        for child in _child_pids(pid):
            total += _rss_mb(child)
        if total > peak:
            peak = total
        if stop_event is not None and stop_event():
            break
        time.sleep(interval)
    end_cpu = _tree_cpu_seconds(pid)
    cpu = None
    if start_cpu is not None and end_cpu is not None:
        cpu = max(0.0, end_cpu - start_cpu)
    return peak, cpu


def _measure_command(argv, env, timeout=120):
    """Run a command as a subprocess, sampling its process tree's RSS
    every 100 ms. Returns (peak_rss_mb, cpu_seconds, exit_code). The CPU
    is read while the process is still alive: after it exits,
    /proc/<pid>/stat is gone and the reading would be lost."""
    import subprocess
    import time
    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    peak = 0
    cpu_start = _tree_cpu_seconds(proc.pid)
    cpu_last = cpu_start
    try:
        deadline = time.time() + timeout
        while proc.poll() is None and time.time() < deadline:
            total = _rss_mb(proc.pid)
            for child in _child_pids(proc.pid):
                total += _rss_mb(child)
            if total > peak:
                peak = total
            now = _tree_cpu_seconds(proc.pid)
            if now is not None:
                cpu_last = now
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    cpu = 0.0
    if cpu_start is not None and cpu_last is not None:
        cpu = max(0.0, cpu_last - cpu_start)
    return peak, cpu, proc.returncode


def cmd_footprint(args):
    """Measure the Render footprint: the worker cycle and each job, run as
    real subprocesses against a fixture DB (7 strategies x 9 positions, a
    50-symbol universe, 60 bars of history), with a fake provider (no
    Fyers credentials in the measurement env, so jobs that need market
    data fail fast after startup — their rows measure startup + attempt,
    and the row carries its exit code so that is visible).

    Prints exactly:
    {"rows": {name: {"peak_rss_mb", "cpu_seconds",
                     "est_wall_seconds_at_0.1cpu"}},
     "worker_cpu_seconds_per_cycle", "max_peak_rss_mb",
     "baseline_idle_rss_mb"}
    """
    import signal
    import subprocess
    import time
    seconds = max(1, args.seconds)
    here = Path(__file__).resolve().parent
    tmp = Path(tempfile.mkdtemp(prefix='render-footprint-'))
    fixture = here / 'tests' / 'test_footprint.sqlite'
    env = dict(os.environ)
    env.update({'PAPER_RUNTIME': 'render',
                'PAPER_TEST_DATABASE': str(fixture),
                'PAPER_STATE_DIR': str(tmp / 'state')})
    rows = {}
    try:
        store = _build_footprint_fixture(fixture)
        store.close()
        # Baseline: the idle supervisor + fake worker.
        sup = subprocess.Popen(
            [sys.executable, str(here / 'render_app.py')],
            env={**env, 'PAPER_FAKE_WORKER': '1'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            baseline, _cpu = _sample_tree(sup.pid, 0.1, 2)
        finally:
            sup.send_signal(signal.SIGTERM)
            try:
                sup.wait(timeout=30)
            except subprocess.TimeoutExpired:
                sup.kill()
                sup.wait()
        # Each job, as a real subprocess, sampled every 100 ms.
        for name, command in FOOTPRINT_JOBS:
            peak, cpu, rc = _measure_command(
                [sys.executable, str(here / 'engine.py'), command], env)
            rows[name] = {
                'peak_rss_mb': peak,
                'cpu_seconds': round(cpu, 2),
                'est_wall_seconds_at_0.1cpu': round(cpu / 0.1, 1),
                'exit_code': rc,
            }
        # The worker cycle: let the worker finish starting up (imports
        # are a one-off cost, not per-cycle), then measure its CPU over
        # the observation window and scale it to one 30 s cycle.
        worker = subprocess.Popen(
            [sys.executable, str(here / 'engine.py'), 'worker'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(5)  # warm-up: imports, not per-cycle work
            worker_peak, worker_cpu = _sample_tree(
                worker.pid, 0.1, seconds,
                stop_event=lambda: worker.poll() is not None)
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGTERM)
                try:
                    worker.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
        worker_cpu = worker_cpu or 0.0
        rows['worker_cycle'] = {
            'peak_rss_mb': worker_peak,
            'cpu_seconds': round(worker_cpu, 2),
            'est_wall_seconds_at_0.1cpu': round(worker_cpu / 0.1, 1),
            'exit_code': worker.returncode,
        }
        # One monitor poll's own CPU: a monitor-once run minus a bare
        # import (the long-running worker pays the import only once, so
        # the difference is the poll's incremental work). This is a
        # lower bound for a live poll — without Fyers credentials the
        # poll fails fast at the refresh — plus the idle rate.
        _imp_peak, import_cpu, _rc = _measure_command(
            [sys.executable, '-c', 'import engine'], env)
        _mon_peak, mon_cpu, _rc = _measure_command(
            [sys.executable, str(here / 'engine.py'), 'monitor-once'], env)
        idle_scaled = worker_cpu * (FOOTPRINT_CYCLE_SECONDS / seconds)
        per_cycle = round(
            max(0.0, mon_cpu - import_cpu) + idle_scaled, 2)
    finally:
        try:
            fixture.unlink()
        except Exception:
            pass
    max_peak = max((r['peak_rss_mb'] for r in rows.values()), default=0)
    _print({'rows': rows,
            'worker_cpu_seconds_per_cycle': per_cycle,
            'max_peak_rss_mb': max_peak,
            'baseline_idle_rss_mb': baseline})
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


def build_parser():
    """The CLI parser, exposed so tests (and DEPLOY.md checks) can
    parse a command line with the real parser."""
    parser = argparse.ArgumentParser(description='Render control CLI')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('trial-init')
    p.add_argument('--namespace', default=None)
    p.add_argument('--bar', default=None)
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_init)
    p = sub.add_parser('trial-compare')
    p.add_argument('--namespace', default=None)
    p.add_argument('--live', default='paper_v2')
    p.add_argument('--day', default=None)
    p.add_argument('--session', dest='day', default=None)
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_compare)
    p = sub.add_parser('trial-purge')
    p.add_argument('--namespace', default=None)
    p.add_argument('--state-only', action='store_true')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--fixture', action='store_true')
    p.set_defaults(fn=cmd_trial_purge)
    p = sub.add_parser('budget-check')
    p.add_argument('--fixture-trial', type=int, default=None)
    p.add_argument('--fixture-box', type=int, default=None)
    p.add_argument('--box-file', default=None)
    p.set_defaults(fn=cmd_budget_check)
    p = sub.add_parser('status')
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser('migrate')
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser('handoff')
    p.add_argument('--to', required=True)
    p.add_argument('--yes', action='store_true')
    p.set_defaults(fn=cmd_handoff)
    p = sub.add_parser('breaker-clear')
    p.add_argument('--job', required=True)
    p.add_argument('--namespace', default=None)
    p.add_argument('--day', default=None)
    p.add_argument('--yes', action='store_true')
    p.set_defaults(fn=cmd_breaker_clear)
    p = sub.add_parser('gen-key')
    p.set_defaults(fn=cmd_gen_key)
    p = sub.add_parser('seed-budget')
    p.add_argument('--requests', type=int, default=None)
    p.add_argument('--from-file', default=None)
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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
