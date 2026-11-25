"""Adaptive sidecar orchestrator. Phases: preflight | eod | backfill | status.

- preflight (pre-open, light): kill switch, frozen Track A files (worktree AND installed runtime) vs freeze_lock.json,
  forbidden-import scan, harness hash, Turso read-only reachability. No heavy work, no writes outside the sidecar.
- eod (post-close, heavy): wait for Track A paper_eod(<day>) with ledger_reconciled (deadline from adaptive_policy),
  refresh the local market snapshot (read-only Turso), take protected-namespace row counts, run adaptive_daily.py in a
  scrubbed env with a memory cap, re-count, and fail loudly if any protected count changed. Then, only when the daily
  step actually simulated, run candidate_shadow.py (registry candidates 101-109) with the Turso mirror, so the daily
  shadow check can read the results.
- backfill (P1, minimal UDiFF era): guarded NSE Bhavcopy fetch into the LOCAL archive; refuses before the 11 Nov EOD.
"""
import argparse, fcntl, json, os, resource, subprocess, sys, time
from contextlib import contextmanager
from datetime import datetime, date, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB)); sys.path.insert(0, str(HERE))
import adaptive_guard as ag      # noqa: E402
import adaptive_engine as ae     # noqa: E402
import adaptive_policy as apol   # noqa: E402
import adaptive_paths as apath   # noqa: E402
import adaptive_observation as aobs  # noqa: E402 — SESSION_DONE_CAUSE (R7)
from ro_client import TursoRO    # noqa: E402

IST = ZoneInfo('Asia/Kolkata')
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
AP = apol.POLICY
TRACK_A = apath.track_a_root(CFG)
PY = sys.executable
STATE = HERE.parent / 'state'


def report_dir():
    """Report directory: ADAPTIVE_REPORT_DIR env override, else /workspace/reports/adaptive."""
    return apath.report_dir()


@contextmanager
def _timed(timings, key):
    """Monotonic-clock phase timing (Task 8): accumulates non-negative seconds
    into timings[key]; never touches any other logic."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        timings[key] = timings.get(key, 0.0) + max(0.0, time.monotonic() - t0)


def get_track_a_namespace():
    """Lazily read the Track A namespace; raises track_a_root_not_found only when actually needed."""
    return apath.track_a_namespace(TRACK_A, CFG)


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
    ap.add_argument('--catch-up', action='store_true', help='eod: process every missed session in order')
    ap.add_argument('--force', action='store_true', help='eod --catch-up: re-run already-marked days')
    ap.add_argument('--from', dest='frm', default=None)
    ap.add_argument('--to', default=None)
    ap.add_argument('--retry-missing', action='store_true',
                    help='backfill: re-attempt only dates without a valid local file or archive rows')
    return ap


def preflight():
    out = {'phase': 'preflight', 'at_ist': datetime.now(IST).isoformat()}
    out['kill_switch'] = ag.kill_switch_engaged()
    # Fail with a clear error (not a bare FileNotFoundError) when the Track A root is absent.
    track_a_ns = get_track_a_namespace()
    ag.assert_frozen_files()                                            # worktree copy
    ag.assert_frozen_files(lock_path=LIB / 'freeze_lock.json', root=TRACK_A)  # installed Track A (read-only)
    out['frozen_files'] = 'match (worktree and installed runtime)'
    v = ag.forbidden_import_violations()
    out['forbidden_import_violations'] = v['violations']
    out['modules_scanned'] = len(v['modules_scanned'])
    out['harness_hash'] = ae.compute_harness_hash()
    ro = TursoRO()
    r = ro.many([("SELECT MAX(trade_date) AS d FROM daily_ohlcv", ()), ("SELECT MAX(day) AS d FROM paper_eod WHERE namespace=?", (track_a_ns,))])
    out['turso_read_only'] = {'latest_daily_bar': r[0][0]['d'], 'latest_track_a_eod': r[1][0]['d']}
    out['ok'] = (not out['kill_switch']) and not out['forbidden_import_violations']
    return out


def wait_for_eod(ro, day, no_wait):
    deadline = datetime.combine(date.fromisoformat(day), datetime.strptime(AP['eod_marker_deadline_ist'], '%H:%M').time(), IST)
    ns = get_track_a_namespace()
    while True:
        rows = ro.query('SELECT report_json FROM paper_eod WHERE namespace=? AND day=?', (ns, day))
        if rows:
            rep = json.loads(rows[0]['report_json'])
            if rep.get('ledger_reconciled'):
                return rep
        if no_wait or datetime.now(IST) >= deadline:
            return None
        time.sleep(CFG['eod_wait_poll_seconds'])


def shadow_child_env():
    """Child env for candidate_shadow.py: the scrubbed env (no credential-like
    keys) plus the PAPER_ENV_FILE path, so node db.mjs reads the same .env
    file the Track A engine already uses. Only a file PATH crosses the
    boundary — no credential value does."""
    env = ag.scrubbed_env()
    env['PYTHONPATH'] = str(LIB)
    if 'PAPER_ENV_FILE' in os.environ:
        env['PAPER_ENV_FILE'] = os.environ['PAPER_ENV_FILE']
    return env


def shadow_step(day, daily_report=None):
    """Run the registry-candidate shadow evaluation (101-109) for one session
    day and mirror the results to Turso.

    Runs only when the daily step actually simulated (simulation_status ==
    'SIMULATED'): on a BLOCKED day the market snapshot is stale, so the
    shadow sim would be stale too. The shadow step runs candidate_shadow.py
    as a subprocess with the same memory cap / niceness as the daily child,
    reading the refreshed state/market.sqlite and writing the sidecar plus
    the Turso mirror (--turso). A shadow failure is recorded in the report
    but never flips the eod status, which follows adaptive_daily's exit
    code only (see eod_run_status).
    """
    daily_report = daily_report or {}
    if daily_report.get('simulation_status') != 'SIMULATED':
        return {'status': 'SKIPPED',
                'reason': 'daily_not_simulated',
                'simulation_status': daily_report.get('simulation_status')}
    def limits():
        mb = int(AP['runner_memory_max_mb']) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb)); os.nice(10); apply_ionice_idle()
    p = subprocess.run([PY, str(HERE / 'candidate_shadow.py'),
                        '--db', str(HERE.parent / CFG['adaptive_db']),
                        '--session', day,
                        '--market-sqlite', str(STATE / 'market.sqlite'),
                        '--turso'],
                       env=shadow_child_env(), capture_output=True, text=True, preexec_fn=limits)
    if p.returncode:
        return {'status': 'FAILED', 'exit_code': p.returncode,
                'stderr_tail': p.stderr.strip().splitlines()[-5:]}
    try:
        summary = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'status': 'FAILED', 'exit_code': p.returncode, 'reason': 'shadow_stdout_not_json'}
    return {'status': 'OK',
            'turso_mirrored': bool(summary.get('turso_mirrored')),
            'strategies': {str(k): v.get('status') for k, v in (summary.get('results') or {}).items()}}


def eod_run_status(returncode, protected_changed=None):
    """Sidecar eod status follows adaptive_daily's exit code only.\n\n    Live Track A paper_v1 event counts can grow during a long shadow run; that is not a sidecar failure.\n    protected_changed is still recorded on the report for audit.\n    """
    return 'OK' if int(returncode or 0) == 0 else 'FAILED'


def eod(day, no_wait=False, ro=None):
    ag.assert_runnable(heavy=True)
    ro = ro or TursoRO()
    out = {'phase': 'eod', 'session_day': day, 'at_ist': datetime.now(IST).isoformat()}
    rep = wait_for_eod(ro, day, no_wait)
    if rep is None:
        out['status'] = 'SKIPPED_TRACK_A_EOD_NOT_RECONCILED_BY_DEADLINE'
        return out
    eod_path = STATE / f'track_a_eod-{day}.json'
    STATE.mkdir(parents=True, exist_ok=True)
    eod_path.write_text(json.dumps(rep))
    # Task 4: live fills for the session, for the replica-parity comparison in
    # adaptive_daily.py. Best effort: if Turso cannot be read here, the daily
    # step reports parity as not_evaluable instead of failing the whole run.
    fills_path = STATE / f'track_a_live_fills-{day}.json'
    try:
        fills = ro.query('SELECT strategy, symbol, side, qty, price, filled_at FROM paper_fills '
                         'WHERE namespace=? AND substr(filled_at,1,10)=? ORDER BY filled_at',
                         (get_track_a_namespace(), day))
        fills_path.write_text(json.dumps(fills, default=str))
        out['live_fills_written'] = len(fills)
    except Exception as e:
        out['live_fills'] = {'status': 'unavailable', 'error_code': str(e).split(':')[0]}
    import market_snapshot
    timings = {}
    with _timed(timings, 'snapshot'):
        out['snapshot'] = market_snapshot.refresh(ro)
    with _timed(timings, 'protected_counts_before'):
        before = protected_counts(ro)
    def limits():
        mb = int(AP['runner_memory_max_mb']) * 1024 * 1024   # address-space cap from policy (C-06)
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb)); os.nice(10); apply_ionice_idle()
    env = ag.scrubbed_env()
    env['PYTHONPATH'] = str(LIB)
    with _timed(timings, 'daily'):
        # R6: pass --report-dir explicitly — the scrubbed child env drops
        # ADAPTIVE_REPORT_DIR, so without this the daily step would write to
        # /workspace/reports/adaptive while the runner writes to the override.
        p = subprocess.run([PY, str(HERE / 'adaptive_daily.py'), '--session', day, '--eod-json', str(eod_path),
                            '--report-dir', str(report_dir())],
                           env=env, capture_output=True, text=True, preexec_fn=limits)
    out['daily_exit_code'] = p.returncode
    if p.returncode:
        out['daily_stderr_tail'] = p.stderr.strip().splitlines()[-5:]
    else:
        out['daily'] = json.loads(p.stdout.strip().splitlines()[-1])
    with _timed(timings, 'shadow'):
        out['shadow'] = shadow_step(day, out.get('daily'))
    with _timed(timings, 'protected_counts_after'):
        after = protected_counts(ro)
    out['timings_seconds'] = timings
    changed = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k) and not k.endswith('|*')}
    changed_total = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k) and k.endswith('|*')}
    out['protected_rows_changed'] = changed
    out['all_table_totals_changed'] = changed_total
    out['status'] = eod_run_status(p.returncode, changed)
    return out


def _read_marked_days():
    """Days whose simulation actually ran (simulation_status == 'SIMULATED').

    R7: a reconciled adaptive_eod_markers row alone is NOT "done" — the EOD
    marker is written before the freshness/PIT gate, so a day whose simulation
    was BLOCKED would otherwise never be caught up. A day counts as done only
    when the shadow runner also logged a SESSION_DONE_CAUSE decision-log row
    for it (written only on the SIMULATED path in adaptive_daily.py)."""
    p = HERE.parent / CFG['adaptive_db']
    if not p.exists():
        return set()
    import sqlite3
    try:
        c = sqlite3.connect(p)
        rows = c.execute('SELECT trade_date FROM adaptive_eod_markers WHERE namespace=? AND reconciled=1',
                         (CFG['namespace'],)).fetchall()
        done = c.execute("SELECT DISTINCT substr(source_at,1,10) FROM adaptive_decision_logs "
                         "WHERE namespace=? AND named_cause=?",
                         (CFG['namespace'], aobs.SESSION_DONE_CAUSE)).fetchall()
        c.close()
        return {r[0] for r in rows} & {r[0] for r in done}
    except Exception:
        return set()


def eod_catch_up(frm=None, to=None, force=False, ro=None, eod_fn=None, marked_days=None, namespace=None, now_fn=None):
    """Catch-up (Task 7): from the day after the earliest SIMULATED day
    (or --from) through --session/today, iterate calendar trading
    sessions in order. A day runs the normal eod flow only if Track A's
    paper_eod row exists and ledger_reconciled is true; otherwise it is
    recorded SKIPPED_NO_RECONCILED_TRACK_A_EOD and nothing is synthesised.
    Already-marked days are skipped unless --force. Stops on the first hard
    failure (the runner writes the FAILED report). Market-hours refusal, the
    lock and the memory cap are enforced by main()/eod() as for a single eod.

    R7: a day counts as done only when its simulation_status is 'SIMULATED'
    (a BLOCKED run never marks the day, so a later catch-up re-runs it).
    Every days[] entry carries simulation_status.
    R7b: start at min(marked)+1, not max(marked)+1 — a BLOCKED day older
    than the newest marked day (e.g. 11 blocked, 12 SIMULATED) must be
    re-run; days already marked are skipped anyway as SKIPPED_ALREADY_MARKED.
    """
    ns = namespace or get_track_a_namespace()
    marked = set(marked_days) if marked_days is not None else _read_marked_days()
    if frm:
        start = str(frm)[:10]
    elif marked:
        from datetime import timedelta
        start = (date.fromisoformat(min(marked)) + timedelta(days=1)).isoformat()
    else:
        raise RuntimeError('catch_up_needs_from_when_no_markers')
    end = str(to)[:10] if to else datetime.now(IST).date().isoformat()
    import adaptive_daily as ad
    days = ad.expected_session_days(start, end)
    ro = ro or TursoRO()
    fn = eod_fn or eod
    out = {'phase': 'eod', 'catch_up': True, 'from': start, 'to': end,
           'at_ist': datetime.now(IST).isoformat(), 'days': []}

    def _fail(day, exc):
        summary = ';'.join(f"{d['day']}:{d['status']}" for d in out['days'])
        raise RuntimeError(f'catch_up_failed_at:{day}:{summary}') from exc

    for day in days:
        # R8: the 09:00-15:35 refusal is checked before EVERY day, not only
        # at start — a multi-hour catch-up can run into market hours. On
        # refusal, stop at once: the runner writes the FAILED report (via
        # main()) whose detail lists the days already done.
        try:
            assert_not_market_hours('eod', now=now_fn() if now_fn else None)
        except RuntimeError as e:
            summary = ';'.join(f"{d['day']}:{d['status']}" for d in out['days'])
            raise RuntimeError(f'catch_up_stopped_in_market_hours:{day}:{e}:{summary}') from e
        if day in marked and not force:
            # Marked days are SIMULATED by construction (see _read_marked_days).
            out['days'].append({'day': day, 'status': 'SKIPPED_ALREADY_MARKED', 'simulation_status': 'SIMULATED'})
            continue
        try:
            rows = ro.query('SELECT report_json FROM paper_eod WHERE namespace=? AND day=?', (ns, day))
        except Exception as e:
            out['days'].append({'day': day, 'status': 'FAILED', 'error': str(e), 'simulation_status': None})
            _fail(day, e)
        reconciled = False
        if rows:
            try:
                reconciled = bool(json.loads(rows[0]['report_json']).get('ledger_reconciled'))
            except Exception:
                reconciled = False
        if not reconciled:
            out['days'].append({'day': day, 'status': 'SKIPPED_NO_RECONCILED_TRACK_A_EOD', 'simulation_status': None})
            continue
        try:
            res = fn(day, no_wait=True)
        except Exception as e:
            out['days'].append({'day': day, 'status': 'FAILED', 'error': str(e), 'simulation_status': None})
            _fail(day, e)
        status = res.get('status') if isinstance(res, dict) else 'OK'
        # The real eod() nests the daily report under res['daily']; fakes may
        # carry simulation_status at the top level.
        sim = None
        if isinstance(res, dict):
            sim = res.get('simulation_status')
            if sim is None and isinstance(res.get('daily'), dict):
                sim = res['daily'].get('simulation_status')
        out['days'].append({'day': day, 'status': status, 'simulation_status': sim})
        if status == 'OK' and sim == 'SIMULATED':
            marked.add(day)
        elif status == 'OK':
            # R7: the eod run "succeeded" but the simulation was blocked — the
            # day is NOT done and a later catch-up must re-run it.
            pass
        else:
            _fail(day, RuntimeError(f'eod_status:{status}'))
    out['status'] = 'OK'
    return out


def backfill(frm, to, retry_missing=False):
    """P1 minimal (UDiFF era). Guards live in bhavcopy_backfill.py; runs in a scrubbed env (no credentials needed)."""
    env = ag.scrubbed_env(); env['PYTHONPATH'] = str(LIB)
    args = [PY, str(HERE / 'bhavcopy_backfill.py'), '--from', frm, '--to', to]
    if retry_missing:
        args.append('--retry-missing')
    p = subprocess.run(args, env=env, capture_output=True, text=True)
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


def _scrub_detail(detail):
    """Remove values of credential-like env vars from a report detail (never leak secrets)."""
    out = str(detail)
    for k in ag.credential_like_keys(dict(os.environ)):
        v = os.environ.get(k)
        if v and len(v) >= 4:
            out = out.replace(v, '[redacted]')
    return out


def _error_code(exc):
    msg = str(exc).strip()
    if msg:
        return msg.split(':')[0].strip() or type(exc).__name__
    return type(exc).__name__


def _error_detail(exc):
    mm = getattr(exc, 'frozen_mismatch', None)
    if mm:
        root = str(mm.get('root') or '')
        label = 'installed' if root == str(TRACK_A) else ('worktree' if root == str(LIB) else root)
        return _scrub_detail(
            f"root={label} ({root}) file={mm.get('name')} expected={mm.get('expected')} actual={mm.get('actual')}")
    return _scrub_detail(str(exc))


def failure_report(phase, session_day, exc):
    return {'phase': phase, 'session_day': session_day, 'at_ist': datetime.now(IST).isoformat(),
            'status': 'FAILED', 'error_code': _error_code(exc), 'detail': _error_detail(exc), 'ok': False}


def main(argv=None):
    t0 = time.monotonic()
    a = build_parser().parse_args(argv)
    lock_fd = None
    out, error = None, None
    try:
        # R5: the market-hours refusal and the lock acquisition run INSIDE the
        # structured-failure handling, so a refusal still writes a
        # runner-<phase>-FAILED.json report and exits 1 — and still runs no
        # heavy work (both checks happen before any phase dispatch).
        if a.phase in ('eod', 'backfill'):
            assert_not_market_hours(a.phase)
            lock_fd = acquire_run_lock()
        if a.phase == 'preflight':
            out = preflight()
        elif a.phase == 'eod':
            if a.catch_up:
                out = eod_catch_up(a.frm, a.session, a.force)
            else:
                out = eod(a.session or datetime.now(IST).date().isoformat(), a.no_wait)
        elif a.phase == 'backfill':
            if not a.frm or not a.to:
                out = {'phase': 'backfill', 'status': 'REFUSED_OR_FAILED', 'error': 'backfill_requires_from_and_to'}
            else:
                out = backfill(a.frm, a.to, a.retry_missing)
        else:
            out = status()
    except Exception as e:  # noqa: BLE001 — every phase failure becomes a structured FAILED report
        error = e
        import traceback
        traceback.print_exc()
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass
    day = a.session or datetime.now(IST).date().isoformat()
    rdir = report_dir() / day; rdir.mkdir(parents=True, exist_ok=True)
    total = max(0.0, time.monotonic() - t0)
    if error is not None:
        failed = failure_report(a.phase, day, error)
        failed['timings_seconds'] = {'total': total}
        (rdir / f'runner-{a.phase}-FAILED.json').write_text(json.dumps(failed, indent=2, default=str))
        try:
            print(json.dumps(failed, indent=1, default=str))
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except Exception:
                pass
        sys.exit(1)
    (rdir / f'runner-{a.phase}-FAILED.json').unlink(missing_ok=True)
    out.setdefault('timings_seconds', {})['total'] = total
    (rdir / f'runner-{a.phase}.json').write_text(json.dumps(out, indent=2, default=str))
    try:
        print(json.dumps(out, indent=1, default=str))
    except BrokenPipeError:
        # The report file above is the durable record; a closed stdout pipe
        # (seen 11 Nov) must not turn a successful run into a failure.
        try:
            sys.stdout.close()
        except Exception:
            pass
    sys.exit(0 if out.get('ok', out.get('status') in (None, 'OK')) else 1)


if __name__ == '__main__':
    main()
