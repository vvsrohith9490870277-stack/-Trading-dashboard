"""Adaptive sidecar orchestrator. Phases: preflight | eod | backfill | status.

- preflight (pre-open, light): kill switch, frozen Track A files (worktree AND installed runtime) vs freeze_lock.json,
  forbidden-import scan, harness hash, Turso read-only reachability. No heavy work, no writes outside the sidecar.
- eod (post-close, heavy): wait for Track A paper_eod(<day>) with ledger_reconciled (deadline from adaptive_policy),
  refresh the local market snapshot (read-only Turso), take protected-namespace row counts, run adaptive_daily.py in a
  scrubbed env with a memory cap, re-count, and fail loudly if any protected count changed.
- backfill (P1, minimal UDiFF era): guarded NSE Bhavcopy fetch into the LOCAL archive; refuses before the 11 Nov EOD.
"""
import argparse, fcntl, json, os, resource, subprocess, sys, time
from datetime import datetime, date, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB)); sys.path.insert(0, str(HERE))
import adaptive_guard as ag      # noqa: E402
import adaptive_engine as ae     # noqa: E402
import adaptive_policy as apol   # noqa: E402
from ro_client import TursoRO    # noqa: E402

IST = ZoneInfo('Asia/Kolkata')
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
AP = apol.POLICY
TRACK_A = Path(CFG['track_a_root'])
TRACK_A_NS = json.loads((TRACK_A / 'config.json').read_text())['namespace']
PY = sys.executable
STATE = HERE.parent / 'state'


def protected_counts(ro):
    tables = ro.query("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    qs, keys = [], []
    for t in tables:
        qs.append((f'SELECT COUNT(*) AS n FROM "{t["name"]}"', ())); keys.append((t['name'], '*'))
        if 'namespace' in (t['sql'] or ''):
            for ns in CFG['protected_namespaces']:
                qs.append((f'SELECT COUNT(*) AS n FROM "{t["name"]}" WHERE namespace=?', (ns,))); keys.append((t['name'], ns))
    return {f'{n}|{ns}': r[0]['n'] for (n, ns), r in zip(keys, ro.many(qs))}



# Hard isolation (P5): never run eod/backfill in 09:00-15:35 IST; sidecar lockfile; niceness in child.
MARKET_QUIET_START = dt_time(9, 0)
MARKET_QUIET_END = dt_time(15, 35)
LOCK_PATH = STATE / 'adaptive.lock'


def in_market_quiet_window(now=None):
    """True during 09:00 inclusive .. 15:35 exclusive IST (covers the cash session plus a pre-open margin)."""
    t = (now or datetime.now(IST)).astimezone(IST).time()
    return MARKET_QUIET_START <= t < MARKET_QUIET_END


def assert_not_market_hours(phase, now=None):
    if in_market_quiet_window(now):
        raise RuntimeError(f'adaptive_{phase}_refused_in_market_hours_0900_1535_IST')


def acquire_run_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        os.close(fd)
        raise RuntimeError('adaptive_sidecar_already_running') from e
    os.write(fd, f'{os.getpid()} {datetime.now(IST).isoformat()}\n'.encode())
    return fd


def apply_ionice_idle():
    """Best-effort IOPRIO_CLASS_IDLE for the current process (Linux)."""
    try:
        import ctypes
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        SYS_ioprio_set, IOPRIO_WHO_PROCESS, IOPRIO_CLASS_IDLE = 251, 1, 3
        libc.syscall(SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE << 13)
    except Exception:
        pass


def build_parser():
    """Argument parser used by the 16:50 / 18:05 / 18:30 routines. Exposed for tests."""
    ap = argparse.ArgumentParser()
    ap.add_argument('phase', choices=['preflight', 'eod', 'backfill', 'status'])
    ap.add_argument('--session', default=None)
    ap.add_argument('--no-wait', action='store_true')
    ap.add_argument('--from', dest='frm', default=None)
    ap.add_argument('--to', default=None)
    return ap


def preflight():
    out = {'phase': 'preflight', 'at_ist': datetime.now(IST).isoformat()}
    out['kill_switch'] = ag.kill_switch_engaged()
    ag.assert_frozen_files()                                            # worktree copy
    ag.assert_frozen_files(lock_path=LIB / 'freeze_lock.json', root=TRACK_A)  # installed Track A (read-only)
    out['frozen_files'] = 'match (worktree and installed runtime)'
    v = ag.forbidden_import_violations()
    out['forbidden_import_violations'] = v['violations']
    out['modules_scanned'] = len(v['modules_scanned'])
    out['harness_hash'] = ae.compute_harness_hash()
    ro = TursoRO()
    r = ro.many([("SELECT MAX(trade_date) AS d FROM daily_ohlcv", ()), ("SELECT MAX(day) AS d FROM paper_eod WHERE namespace=?", (TRACK_A_NS,))])
    out['turso_read_only'] = {'latest_daily_bar': r[0][0]['d'], 'latest_track_a_eod': r[1][0]['d']}
    out['ok'] = (not out['kill_switch']) and not out['forbidden_import_violations']
    return out


def wait_for_eod(ro, day, no_wait):
    deadline = datetime.combine(date.fromisoformat(day), datetime.strptime(AP['eod_marker_deadline_ist'], '%H:%M').time(), IST)
    while True:
        rows = ro.query('SELECT report_json FROM paper_eod WHERE namespace=? AND day=?', (TRACK_A_NS, day))
        if rows:
            rep = json.loads(rows[0]['report_json'])
            if rep.get('ledger_reconciled'):
                return rep
        if no_wait or datetime.now(IST) >= deadline:
            return None
        time.sleep(CFG['eod_wait_poll_seconds'])


def eod(day, no_wait=False):
    ag.assert_runnable(heavy=True)
    ro = TursoRO()
    out = {'phase': 'eod', 'session_day': day, 'at_ist': datetime.now(IST).isoformat()}
    rep = wait_for_eod(ro, day, no_wait)
    if rep is None:
        out['status'] = 'SKIPPED_TRACK_A_EOD_NOT_RECONCILED_BY_DEADLINE'
        return out
    eod_path = STATE / f'track_a_eod-{day}.json'; eod_path.write_text(json.dumps(rep))
    import market_snapshot
    out['snapshot'] = market_snapshot.refresh(ro)
    before = protected_counts(ro)
    def limits():
        mb = int(AP['runner_memory_max_mb']) * 1024 * 1024   # address-space cap from policy (C-06)
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb)); os.nice(10); apply_ionice_idle()
    env = ag.scrubbed_env()
    env['PYTHONPATH'] = str(LIB)
    p = subprocess.run([PY, str(HERE / 'adaptive_daily.py'), '--session', day, '--eod-json', str(eod_path)],
                       env=env, capture_output=True, text=True, preexec_fn=limits)
    out['daily_exit_code'] = p.returncode
    if p.returncode:
        out['daily_stderr_tail'] = p.stderr.strip().splitlines()[-5:]
    else:
        out['daily'] = json.loads(p.stdout.strip().splitlines()[-1])
    after = protected_counts(ro)
    changed = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k) and not k.endswith('|*')}
    changed_total = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k) and k.endswith('|*')}
    out['protected_rows_changed'] = changed
    out['all_table_totals_changed'] = changed_total
    out['status'] = 'OK' if p.returncode == 0 and not changed else 'FAILED'
    return out


def backfill(frm, to):
    """P1 minimal (UDiFF era). Guards live in bhavcopy_backfill.py; runs in a scrubbed env (no credentials needed)."""
    env = ag.scrubbed_env(); env['PYTHONPATH'] = str(LIB)
    p = subprocess.run([PY, str(HERE / 'bhavcopy_backfill.py'), '--from', frm, '--to', to], env=env, capture_output=True, text=True)
    out = {'phase': 'backfill', 'from': frm, 'to': to, 'exit_code': p.returncode}
    if p.returncode:
        out['status'] = 'REFUSED_OR_FAILED'; out['stderr_tail'] = p.stderr.strip().splitlines()[-3:]
    else:
        out['status'] = 'OK'; out['result'] = json.loads(p.stdout)
    return out


def status():
    import sqlite3
    p = HERE.parent / CFG['adaptive_db']
    if not p.exists():
        return {'phase': 'status', 'adaptive_db': 'absent'}
    c = sqlite3.connect(p)
    q = lambda s, a=(): c.execute(s, a).fetchall()
    return {'phase': 'status', 'namespace': CFG['namespace'],
            'tracks': q('SELECT track,strategy,COUNT(*) FROM adaptive_tracks WHERE namespace=? GROUP BY track,strategy', (CFG['namespace'],)),
            'eod_markers': q('SELECT trade_date,reconciled FROM adaptive_eod_markers WHERE namespace=? ORDER BY trade_date', (CFG['namespace'],)),
            'daily_returns_rows': q('SELECT COUNT(*) FROM adaptive_daily_returns WHERE namespace=?', (CFG['namespace'],))[0][0],
            'log_rows': q('SELECT COUNT(*) FROM adaptive_decision_logs WHERE namespace=?', (CFG['namespace'],))[0][0]}


def main(argv=None):
    a = build_parser().parse_args(argv)
    lock_fd = None
    if a.phase in ('eod', 'backfill'):
        assert_not_market_hours(a.phase)
        lock_fd = acquire_run_lock()
    try:
        if a.phase == 'preflight':
            out = preflight()
        elif a.phase == 'eod':
            out = eod(a.session or datetime.now(IST).date().isoformat(), a.no_wait)
        elif a.phase == 'backfill':
            if not a.frm or not a.to:
                out = {'phase': 'backfill', 'status': 'REFUSED_OR_FAILED', 'error': 'backfill_requires_from_and_to'}
            else:
                out = backfill(a.frm, a.to)
        else:
            out = status()
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass
    rdir = Path('/workspace/reports/adaptive') / (a.session or datetime.now(IST).date().isoformat()); rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f'runner-{a.phase}.json').write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=1, default=str))
    sys.exit(0 if out.get('ok', out.get('status') in (None, 'OK')) else 1)


if __name__ == '__main__':
    main()
