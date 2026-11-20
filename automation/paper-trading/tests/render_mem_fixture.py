#!/usr/bin/env python3
"""Deterministic full-size scan fixture + peak-memory measurement (render-deploy-03, Task 0).

Generates seeded synthetic OHLCV for the whole 692-symbol universe (about
1,500 sessions) plus the NIFTY50-INDEX benchmark into a local SQLite test
database, and measures the real `python engine.py scan` on it.

Determinism: every series is seeded (SEED); session dates are weekdays
minus the policy closed_dates, plus the 2026-11-08 Muhurat special session,
ending at TARGET (default 2026-11-19). The scan's completed day is frozen
at FROZEN_NOW (2026-11-20 10:00 IST -> completed day 2026-11-19) by the
measurement driver, so the same fixture measures the same scan on any day.

Environment notes (this machine): /usr/bin/time does not exist, so peak
RSS is measured by sampling /proc/<pid>/status VmRSS of the scan process
tree every 50 ms, plus resource.getrusage(RUSAGE_CHILDREN) for the largest
single child. /workspace/sahil-fyers-turso/.env does not exist here, so no
live Turso read is attempted (the brief's optional read is skipped).

Usage:
  python tests/render_mem_fixture.py --build [--db PATH] [--symbols N]
      [--sessions N] [--target DATE] [--seed N]
  python tests/render_mem_fixture.py --scan [--db PATH] [--frozen-now ISO]
      [--real-clock]   # --real-clock runs the literal `engine.py scan`
                       # command (target = today's completed day); default
                       # runs it through the frozen-clock driver so the
                       # fixture target always matches.
The built database is generated, never committed (tests/test_*.sqlite is
gitignored). Nothing over 200 KB is committed by this module.
"""
import argparse
import json
import math
import os
import random
import resource
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

SEED = 20261120
TARGET = '2026-11-19'
FROZEN_NOW = '2026-11-20T10:00:00+05:30'
BENCHMARK_SYMBOL = 'NIFTY50-INDEX'
DEFAULT_DB = str(HERE / 'test_rendermem_full.sqlite')


def session_list(target, n_sessions):
    """Weekday sessions ending at `target`, minus policy closed dates,
    plus the Muhurat special session. Returns a list of ISO date strings
    in ascending order (about n_sessions long)."""
    import engine as e  # noqa: F401  (import for side effects only)
    policy = json.loads((ROOT / 'session_input_policy.json').read_text())
    closed = set(policy['closed_dates'])
    end = date.fromisoformat(target)
    days = []
    d = end
    while len(days) < n_sessions:
        if d.weekday() < 5 and d.isoformat() not in closed:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    days.reverse()
    if '2026-11-08' <= target and '2026-11-08' not in days:
        days.append('2026-11-08')
        days.sort()
    return days


def universe_symbols(limit=None):
    """db_symbol values from universe.csv, in file order."""
    import csv
    with (ROOT / 'universe.csv').open() as f:
        syms = [r['db_symbol'] for r in csv.DictReader(f)]
    return syms[:limit] if limit else syms


def gen_bars(seed, sessions, start_price, drift, vol, volume_base,
             market=None, beta=None, idio=0.012):
    """Seeded synthetic OHLCV: (trade_date, open, high, low, close, volume)
    tuples, OHLC-consistent, all prices > 0.

    Factor model when `market` is given: the day's return is
    beta * market_return + N(drift, idio), so a stock's beta against the
    benchmark (which follows `market` exactly, beta=1, idio=0) is ~beta.
    Without `market`, returns are independent N(drift, vol) (beta ~ 0
    against any benchmark — unrealistic, kept only for the smoke path).
    """
    rng = random.Random(seed)
    price = start_price
    rows = []
    for j, day in enumerate(sessions):
        if market is not None:
            r = beta * market[j] + rng.gauss(drift, idio)
        else:
            r = rng.gauss(drift, vol)
        r = max(-0.20, min(0.20, r))
        o = price * (1 + rng.gauss(0, vol * 0.5))
        c = o * (1 + r)
        o = max(0.01, o)
        c = max(0.01, c)
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        l = max(0.01, l)
        volume = int(volume_base * (1 + rng.gauss(0, 0.3)))
        volume = max(1000, volume)
        rows.append((day, round(o, 2), round(h, 2), round(l, 2),
                     round(c, 2), volume))
        price = c
    return rows


def build_db(path, symbols=None, n_sessions=1500, target=TARGET,
             seed=SEED, namespace='test_rendermem'):
    """Build (or rebuild) the fixture database at `path`.

    Schema via engine.init (the real schema); bulk OHLCV rows via sqlite3
    executemany. Sets paper_runtime_kv active_runner='render' for the
    trial namespace so the CLI scan passes the runner guard under
    PAPER_RUNTIME=render. Returns (path, n_symbol_rows, n_benchmark_rows,
    n_sessions_used).
    """
    import engine as e
    path = str(path)
    p = Path(path)
    if p.exists():
        p.unlink()
    for suffix in ('-wal', '-shm', '-journal'):
        Path(path + suffix).unlink(missing_ok=True)
    sessions = session_list(target, n_sessions)
    syms = symbols or universe_symbols()
    db = e.DB(path)
    old_ns = e.NS
    e.NS = namespace
    try:
        e.init(db)
    finally:
        e.NS = old_ns
    db.close_helper()
    # Render tables (paper_runner_lease, paper_job_runs, paper_runtime_kv)
    # come from render_state.migrate, not engine.init — same as the
    # supervisor does at startup.
    import render_state as rs
    _store = rs.SqliteStore(path)
    try:
        rs.migrate(_store)
    finally:
        _store.close()
    con = sqlite3.connect(path)
    try:
        # daily_ohlcv is a shared table the sync normally fills; it is NOT
        # created by engine.init. DDL copied from render_ctl.py (the
        # production shape). index_ohlcv has no CREATE anywhere in the
        # repo (box-created shared table); same shape as daily_ohlcv.
        con.execute(
            'CREATE TABLE IF NOT EXISTS daily_ohlcv('
            'trade_date TEXT NOT NULL, symbol TEXT NOT NULL,'
            ' open REAL, high REAL, low REAL, close REAL, volume INTEGER,'
            ' turnover REAL, source TEXT, loaded_at TEXT,'
            ' PRIMARY KEY(trade_date, symbol))')
        con.execute(
            'CREATE TABLE IF NOT EXISTS index_ohlcv('
            'trade_date TEXT NOT NULL, symbol TEXT NOT NULL,'
            ' open REAL, high REAL, low REAL, close REAL, volume INTEGER,'
            ' turnover REAL, source TEXT, loaded_at TEXT,'
            ' PRIMARY KEY(trade_date, symbol))')
        # Shared market factor: the benchmark follows it exactly, each
        # stock's return is beta_i * market + idiosyncratic noise, so
        # betas against the benchmark are realistic (~U(0.6, 1.8)) and a
        # realistic fraction of symbols passes the prefilter.
        _mrng = random.Random(seed + 555)
        market = [_mrng.gauss(0.0004, 0.008) for _ in sessions]
        n_rows = 0
        for i, sym in enumerate(syms):
            # ~25% of symbols trend up so a realistic fraction passes the
            # prefilter (beta/momentum checks); the rest are driftless.
            drift = 0.0025 if (i % 4 == 0) else 0.0
            beta_i = random.Random(seed + 777 + i).uniform(0.6, 1.8)
            start = 50 * math.exp(random.Random(seed + i).random()
                                  * math.log(100))
            vol_base = int(50000 * math.exp(
                random.Random(seed + 100000 + i).random() * math.log(100)))
            rows = gen_bars(seed + i, sessions, start, drift, 0.018,
                            vol_base, market=market, beta=beta_i)
            con.executemany(
                'INSERT INTO daily_ohlcv(symbol,trade_date,open,high,low,'
                'close,volume,turnover,source,loaded_at) '
                'VALUES(?,?,?,?,?,?,?,?,\'RENDER_MEM_FIXTURE\','
                '\'2026-11-20T00:00:00+05:30\')',
                [(sym, d, o, h, l, c, v, round(c * v, 2))
                 for (d, o, h, l, c, v) in rows])
            n_rows += len(rows)
        bench = gen_bars(seed + 999999, sessions, 24000.0, 0.0, 0.008,
                         100_000_000, market=market, beta=1.0, idio=0.0)
        con.executemany(
            'INSERT INTO index_ohlcv(trade_date,symbol,open,high,low,close,'
            'volume,source,loaded_at) '
            'VALUES(?,?,?,?,?,?,?,\'RENDER_MEM_FIXTURE\','
            '\'2026-11-20T00:00:00+05:30\')',
            [(d, BENCHMARK_SYMBOL, o, h, l, c, v)
             for (d, o, h, l, c, v) in bench])
        # Runner guard: the CLI scan under PAPER_RUNTIME=render refuses
        # unless the trial namespace's active_runner flag names render.
        con.execute(
            "INSERT INTO paper_runtime_kv(scope,key,value,updated_at) "
            "VALUES('paper_render_trial_v1','active_runner','render',"
            "'2026-11-20T00:00:00')")
        con.commit()
    finally:
        con.close()
    return path, n_rows, len(bench), len(sessions)


def _tree_pids(root_pid):
    """All descendant pids of root_pid (via /proc ppid), including root."""
    try:
        ppid = {}
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            try:
                with open('/proc/%s/stat' % name) as f:
                    parts = f.read().rsplit(')', 1)[1].split()
                ppid[int(name)] = int(parts[1])
            except Exception:
                continue
    except Exception:
        return [root_pid]
    out = [root_pid]
    changed = True
    while changed:
        changed = False
        for pid, par in ppid.items():
            if par in out and pid not in out:
                out.append(pid)
                changed = True
    return out


def _tree_rss_mb(pids):
    """(total VmRSS MB of the pids, {pid: MB}) — 0 for vanished pids."""
    total = 0
    per = {}
    for pid in pids:
        try:
            with open('/proc/%d/status' % pid) as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        mb = int(line.split()[1]) // 1024
                        per[pid] = mb
                        total += mb
                        break
        except Exception:
            continue
    return total, per


def _cmd_name(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            return f.read().split(b'\x00')[0].decode(errors='replace')
    except Exception:
        return ''


def measure_scan(db_path, frozen_now=FROZEN_NOW, real_clock=False,
                 extra_env=None):
    """Run the scan as a subprocess and sample its process-tree RSS.

    Default (frozen clock): runs through `render_mem_fixture.py
    --run-scan`, which patches engine.now to `frozen_now` before
    engine.main(), so the fixture target always matches. With real_clock=True runs the literal
    `python engine.py scan` command. Returns a dict with peak_tree_mb,
    peak_python_mb, peak_node_mb (largest single node child seen),
    python_ru_maxrss_mb, node_ru_maxrss_mb (best effort), wall_s,
    exit_code and stdout_tail.
    """
    env = dict(os.environ)
    env['PAPER_RUNTIME'] = 'render'
    env['RENDER_MODE'] = 'trial'
    env['PAPER_NAMESPACE_OVERRIDE'] = 'paper_render_trial_v1'
    env['PAPER_TEST_DATABASE'] = str(db_path)
    env['FYERS_AUTO_LOGIN'] = '0'
    env['PAPER_STATE_DIR'] = '/tmp/paper-state-rendermem'
    if extra_env:
        env.update(extra_env)
    if real_clock:
        cmd = [sys.executable, 'engine.py', 'scan']
    else:
        env['RENDER_SCAN_FROZEN_NOW'] = frozen_now
        cmd = [sys.executable, str(HERE / 'render_mem_fixture.py'),
               '--run-scan']
    out_path = '/tmp/paper-state-rendermem/scan.out'
    Path('/tmp/paper-state-rendermem').mkdir(parents=True, exist_ok=True)
    peak_tree = 0
    peak_python = 0
    peak_node = 0
    stop = threading.Event()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=open(out_path, 'wb'),
                            stderr=subprocess.STDOUT,
                            start_new_session=True)
    t0 = time.monotonic()
    # Sampler: refresh the descendant set at 4 Hz, sample VmRSS of the
    # known pids every 20 ms. (A full /proc walk every sample is too slow
    # in Python and misses the short peaks that matter.)
    known = {proc.pid: 'python'}
    last_refresh = 0.0

    def sampler():
        nonlocal peak_tree, peak_python, peak_node, last_refresh
        while not stop.is_set():
            now = time.monotonic()
            if now - last_refresh > 0.25:
                last_refresh = now
                for pid in _tree_pids(proc.pid):
                    if pid not in known:
                        cmd0 = os.path.basename(_cmd_name(pid))
                        known[pid] = ('node' if 'node' in cmd0 else
                                      'python' if 'python' in cmd0
                                      else 'other')
            total, per = _tree_rss_mb(list(known))
            if total > peak_tree:
                peak_tree = total
            for pid, mb in per.items():
                if known.get(pid) == 'node':
                    peak_node = max(peak_node, mb)
                elif known.get(pid) == 'python':
                    peak_python = max(peak_python, mb)
            time.sleep(0.02)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        proc.wait()
    finally:
        stop.set()
        th.join(timeout=1.0)
    wall = time.monotonic() - t0
    # RUSAGE_CHILDREN: max RSS of the largest single waited-for child
    # (the python scan process itself, from this driver's view).
    ru = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    ru_mb = ru // 1024 if sys.platform.startswith('linux') else ru // 1024
    tail = Path(out_path).read_text(errors='replace')[-2000:] \
        if Path(out_path).exists() else ''
    return {'peak_tree_mb': peak_tree, 'peak_python_mb': peak_python,
            'peak_node_mb': peak_node, 'python_ru_maxrss_mb': ru_mb,
            'wall_s': round(wall, 1), 'exit_code': proc.returncode,
            'stdout_tail': tail}


def run_scan_frozen():
    """Frozen-clock scan driver (test/measurement only, never production).

    Patches engine.now to RENDER_SCAN_FROZEN_NOW, then runs the real
    engine CLI (engine.main() with the original argv), so a deterministic
    fixture whose bars end at a fixed target always scans the same
    completed day no matter what day the measurement runs on. Nothing
    else is patched: argument parsing, the runner guard, the lock,
    MarketData construction and the whole scan path are the real ones.
    Invoked as: python tests/render_mem_fixture.py --run-scan scan
    (argv after --run-scan is passed to engine.main()).
    """
    import engine as e
    frozen = os.environ.get('RENDER_SCAN_FROZEN_NOW')
    if frozen:
        from datetime import datetime as _dt
        t = _dt.fromisoformat(frozen)
        if t.tzinfo is None:
            t = t.replace(tzinfo=e.IST)
        e.now = lambda: t
    e.main()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build', action='store_true')
    ap.add_argument('--scan', action='store_true')
    ap.add_argument('--run-scan', action='store_true',
                    help='frozen-clock engine scan (test driver); the '
                    'remaining argv is passed to engine.main()')
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--symbols', type=int, default=None)
    ap.add_argument('--sessions', type=int, default=1500)
    ap.add_argument('--target', default=TARGET)
    ap.add_argument('--seed', type=int, default=SEED)
    ap.add_argument('--frozen-now', default=FROZEN_NOW)
    ap.add_argument('--real-clock', action='store_true')
    args, rest = ap.parse_known_args(argv)
    if args.run_scan:
        sys.argv = [sys.argv[0]] + rest
        run_scan_frozen()
        return 0
    if args.build or not args.scan:
        path, n_rows, n_bench, n_sess = build_db(
            args.db, universe_symbols(args.symbols), args.sessions,
            args.target, args.seed)
        print(json.dumps({'db': path, 'symbol_rows': n_rows,
                          'benchmark_rows': n_bench,
                          'sessions': n_sess,
                          'symbols': args.symbols or 692,
                          'target': args.target}))
    if args.scan:
        res = measure_scan(args.db, args.frozen_now, args.real_clock)
        print(json.dumps({k: v for k, v in res.items()
                          if k != 'stdout_tail'}, indent=1))
        print('--- stdout tail ---')
        print(res['stdout_tail'][-1500:])
    return 0


if __name__ == '__main__':
    sys.exit(main())
