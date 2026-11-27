"""Supervisor + web server for Render (Task 6, brief render-deploy-01).

One process: a thread serves GET /health from an in-memory snapshot, a
lease-renewal thread holds the runner lease, and the main loop manages the
engine worker subprocess and the IST schedule. SIGTERM stops the worker,
releases the lease and exits 0 within 25 s.

Test hooks: --check-namespace-only validates the resolved namespace and
exits 0 (ok) or 3 (refused) without starting anything; PAPER_FAKE_WORKER=1
runs a sleeping fake worker instead of the engine (with PAPER_TEST_DATABASE
for a local SQLite store).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import render_health as rh
import render_jobs as rj
import render_lease as rl
import render_schedule as rsch
import render_state as rs
import render_trial as rt
import render_fyers_auth as rfa
import render_full_schedule as rfs
import runtime_mode as rm
import support_monitor as sm
import support_agent_render as sar

IST = ZoneInfo('Asia/Kolkata')
WORKER_BACKOFF_SECONDS = (5, 10, 20, 40, 60)
WORKER_STOP_TIMEOUT_SECONDS = 20
# Main-loop tick (read-usage guard): the supervisor re-checks the worker
# and refreshes the /health store fields at most once per tick. Was 5 s.
SUPERVISOR_TICK_SECONDS = 60


def repo_config_namespace():
    """The namespace from config.json (next to engine.py; never edited)."""
    cfg_path = Path(__file__).resolve().parent / 'config.json'
    return json.loads(cfg_path.read_text())['namespace']


def repo_config():
    """The whole config.json (read-only; the file is never edited)."""
    cfg_path = Path(__file__).resolve().parent / 'config.json'
    return json.loads(cfg_path.read_text())


def session_window(cfg, day):
    """Session window for `day` from config.json alone.

    Mirrors engine.session without importing the engine (so /health
    stays fast): None when the calendar does not verify the day,
    () when the market is closed, else (open, close) times.
    """
    key = day.isoformat()
    if not (cfg.get('calendar_valid_from', '')
            <= key <= cfg.get('calendar_valid_through', '')):
        return None
    if key in (cfg.get('special_sessions') or {}):
        return tuple(dt_time.fromisoformat(t)
                     for t in cfg['special_sessions'][key])
    if day.weekday() >= 5 or key in (cfg.get('holidays') or []):
        return ()
    return tuple(dt_time.fromisoformat(t)
                 for t in cfg['regular_session'])


def check_namespace():
    """Resolve the namespace for this process. Returns (ok, namespace).

    ok is False when the namespace is refused (exit 3); the refusal is
    printed, never the config value that was refused.
    """
    try:
        ns = rm.resolve_namespace({'namespace': repo_config_namespace()})
    except Exception as ex:
        print('namespace refused: %s' % type(ex).__name__, flush=True)
        return False, None
    # Full-schedule startup guard (brief 23 Nov 2026): paper_v1 is the
    # archived namespace and must never be written to. resolve() already
    # refuses it in both modes; this is defense in depth so no future
    # resolver path can start the supervisor under it.
    if ns == 'paper_v1':
        print('namespace refused: paper_v1', flush=True)
        return False, None
    return True, ns


def job_rss_cap_mb():
    """Per-job RSS cap in MB (env RENDER_JOB_RSS_CAP_MB, default 350)."""
    try:
        return max(1, int(os.environ.get('RENDER_JOB_RSS_CAP_MB', '350')))
    except ValueError:
        return 350


def run_job_streaming(cmd, env, timeout=1800, rss_cap_mb=None):
    """Run a job child streaming stdout/stderr (render-deploy-03 Task 1).

    The child's output is read as it arrives in fixed 64 KiB chunks
    (never readline: a single very long line must not be buffered) and
    only the last 2000 characters of each stream are kept, so a chatty
    job cannot grow the supervisor's memory — the base version used
    capture_output=True and buffered the whole output. Returns
    {'exit_code', 'stdout_tail', 'stderr_tail'}. Raises
    subprocess.TimeoutExpired on timeout, like subprocess.run.

    Memory guard (Task 2): the child's whole process tree is watched
    every 0.5 s; over RENDER_JOB_RSS_CAP_MB (default 350) the process
    group is killed and the result carries killed_reason 'memory_cap'
    plus the cap in force, so the job is recorded failed/memory_cap.
    The watcher also records 'peak_rss_mb' (Task 3): the largest
    sampled tree RSS. It is a sampled lower bound, not a continuous
    measurement — a job that allocates and exits between two polls is
    never observed using that memory.
    """
    import threading
    if rss_cap_mb is None:
        rss_cap_mb = job_rss_cap_mb()
    tails = {'out': '', 'err': ''}
    killed = {'reason': None}
    peak = {'mb': 0}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)

    def _drain(stream, key):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                tails[key] = (tails[key] + chunk)[-2000:]
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _watch():
        while proc.poll() is None:
            try:
                rss = rh.tree_rss_mb(proc.pid)
                if rss > peak['mb']:
                    peak['mb'] = rss
                if rss > rss_cap_mb:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        break
                    killed['reason'] = 'memory_cap'
                    break
            except Exception:
                pass
            time.sleep(0.5)

    threads = [threading.Thread(target=_drain, args=(proc.stdout, 'out'),
                                daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, 'err'),
                                daemon=True),
               threading.Thread(target=_watch, daemon=True)]
    for th in threads:
        th.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        raise subprocess.TimeoutExpired(cmd, timeout)
    for th in threads:
        th.join(timeout=10)
    out = {'exit_code': rc, 'stdout_tail': tails['out'],
           'stderr_tail': tails['err'], 'peak_rss_mb': peak['mb']}
    if killed['reason']:
        out['killed_reason'] = killed['reason']
        out['rss_cap_mb'] = rss_cap_mb
    return out


class Worker:
    """The engine worker subprocess, with crash backoff and a memory guard."""

    def __init__(self, cmd, env, restarts=0, restart_times=None):
        self.cmd = cmd
        self.env = env
        self.proc = None
        self.restarts = restarts
        self.restart_times = list(restart_times or [])
        self.backoff_idx = 0
        self.next_start = 0.0
        self.state = 'stopped'

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, env=self.env)  # inherit stdout/stderr: Render captures them
        self.state = 'running'
        self.next_start = 0.0

    def stop(self, timeout=WORKER_STOP_TIMEOUT_SECONDS):
        if self.proc is None:
            self.state = 'stopped'
            return
        p, self.proc = self.proc, None
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self.state = 'stopped'

    def poll(self):
        """Update state from the child. Returns 'running', 'stopped' or
        'crashed' (exited; backoff applies before the next start)."""
        if self.proc is None:
            return self.state
        rc = self.proc.poll()
        if rc is None:
            return 'running'
        self.proc = None
        self.restarts += 1
        self.restart_times.append(time.time())
        backoff = WORKER_BACKOFF_SECONDS[
            min(self.backoff_idx, len(WORKER_BACKOFF_SECONDS) - 1)]
        self.backoff_idx += 1
        self.next_start = time.time() + backoff
        self.state = 'crashed'
        return 'crashed'

    def rss_mb(self):
        if self.proc is None:
            return 0
        try:
            with open('/proc/%d/status' % self.proc.pid) as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) // 1024
        except Exception:
            return 0
        return 0

    def describe(self):
        now = time.time()
        self.restart_times = [t for t in self.restart_times
                              if now - t <= 600]
        return {'state': self.poll(),
                'pid': self.proc.pid if self.proc is not None else None,
                'rss_mb': self.rss_mb(), 'restarts': self.restarts,
                'restarts_10m': len(self.restart_times),
                # Needs a store read (paper_monitor_health); left null
                # per brief 02 section 6 — see render_health.py.
                'last_heartbeat_age_s': None}


class Supervisor:
    def __init__(self, store, namespace, mode, owner_id):
        self.store = store
        self.namespace = namespace
        self.mode = mode
        self.owner_id = owner_id
        self.config = repo_config()
        self.lease = rl.Lease(store, namespace, owner_id)
        self.lease_held = False
        self.worker = None
        self.started_at = time.time()
        self.stopping = False
        self.store_error = None
        self._job_thread = None
        # One-shot boot actions (Tasks 1-2) run once per supervisor
        # boot, on the first tick where the lease is held.
        self._boot_actions_done = False
        self._lock = threading.Lock()
        self.last_flag = None
        self.last_snapshot = None
        self.last_tick_at = None
        self.last_lease_renew_at = None
        # Open crash-loop breakers {job: info}, refreshed from
        # paper_job_runs (authoritative; survives a restart).
        self.breakers = {}
        # /health store-read fields (Task 3): cached by the background
        # refresher (see _refresh_health_store); snapshot() never reads
        # the store. Replaced wholesale on each refresh (never
        # mutated), so the HTTP thread can read it without a lock.
        self._health_cache = rh.new_health_cache()
        # Read-only support monitor (brief 23 Nov 2026): the monitor
        # runner replaces this dict wholesale after each cycle (see
        # _monitor_state below); snapshot() serves it from memory.
        # getattr fallbacks in snapshot() keep Supervisors constructed
        # without __init__ (tests) working.
        self.support_monitor_state = sm.default_state(
            enabled=sm.monitor_enabled())
        # Full Support Agent (brief Nov 2026): the agent runner
        # replaces this dict wholesale after each cycle (see
        # _agent_state below); snapshot() serves it from memory.
        self.support_agent_state = sar.default_state(
            enabled=sar.agent_enabled())
        # Fyers token auto-refresh (brief 23 Nov 2026): one manager
        # per supervisor process. It keeps the token in memory only
        # and never writes it anywhere; see render_fyers_auth.
        self._fyers_auth = rfa.FyersTokenManager(
            env=dict(os.environ), mode=self.mode,
            stored_refresh_token=self._stored_refresh_token)

    # -- worker ---------------------------------------------------------
    def _worker_cmd(self):
        if os.environ.get('PAPER_FAKE_WORKER') == '1':
            return [sys.executable, '-c', 'import time; time.sleep(3600)']
        return [sys.executable, 'engine.py', 'worker']

    def _stored_refresh_token(self):
        """Refresh token from the encrypted token store, or None.

        Read-only and fails closed: any store problem returns None,
        and the manager then falls back to the full TOTP login. The
        manager never writes to the store."""
        try:
            import token_store as ts
            tok = ts.TokenStore(self.store).get_token()
            return (tok or {}).get('refresh_token')
        except Exception:
            return None

    def _child_env(self):
        env = dict(os.environ)
        env['PAPER_RUNTIME'] = 'render'
        env['RENDER_MODE'] = self.mode
        env['RENDER_OWNER_ID'] = self.owner_id
        # Every child (worker, job, probe) gets the token through the
        # manager: the cached token, the env token, or a fresh one
        # from the refresh-token / TOTP login. When the manager has
        # no token, the env is left as-is and the child falls back to
        # the token store, exactly as today.
        mgr = getattr(self, '_fyers_auth', None)
        if mgr is not None:
            try:
                token = mgr.get_access_token()
            except Exception:
                token = None
            if token:
                env['FYERS_ACCESS_TOKEN'] = token
        return env

    def _worker_window_open(self, now=None):
        """True from 2 minutes before the session open to the close.
        Outside it the worker is not started (and is stopped if running);
        engine.worker exits outside the session, so starting it there only
        burns the crash-restart budget."""
        if os.environ.get('PAPER_FAKE_WORKER') == '1':
            return True
        now = now or datetime.now(IST)
        window = session_window(self.config, now.date())
        if not window:
            return False
        moment = now.time()
        start = (datetime.combine(now.date(), window[0])
                 - timedelta(minutes=2)).time()
        return start <= moment < window[1]

    def _manage_worker(self):
        if not self._worker_window_open():
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
            return
        if self.worker is None:
            self.worker = Worker(self._worker_cmd(), self._child_env())
            self.worker.start()
            return
        state = self.worker.poll()
        if state == 'running':
            if rh.memory_over_limit(self.worker.rss_mb()):
                # memory guard: restart a worker over 450 MB
                restarts = self.worker.restarts
                times = self.worker.restart_times
                self.worker.stop()
                self.worker = Worker(self._worker_cmd(), self._child_env(),
                                     restarts=restarts, restart_times=times)
                self.worker.start()
            return
        if state == 'crashed' and time.time() >= self.worker.next_start:
            restarts = self.worker.restarts
            times = self.worker.restart_times
            self.worker = Worker(self._worker_cmd(), self._child_env(),
                                 restarts=restarts, restart_times=times)
            self.worker.start()

    # -- jobs -----------------------------------------------------------
    def _job_runner(self, job):
        """The runner run_job uses for a job: engine.py <command> as a
        child with the memory guard (run_job_streaming). Shared by the
        scheduler and the one-shot run (Task 2). A job may carry its own
        per-job limits in the schedule (timeout_s / rss_cap_mb); they
        apply to that job only -- jobs without them keep the defaults
        (timeout 1800 s, cap from RENDER_JOB_RSS_CAP_MB, default 350 MB).
        The adaptive_eod job sets both (measured 244 s, daily tree peak
        ~401 MB; cap 407 MB = 512 MB container less the ~105 MB
        supervisor, so the container cannot OOM from this job)."""
        def runner(name):
            cmd = [sys.executable, 'engine.py', job['command']]
            timeout = job.get('timeout_s') or 1800
            rss_cap = job.get('rss_cap_mb')
            res = run_job_streaming(cmd, self._child_env(),
                                    timeout=timeout, rss_cap_mb=rss_cap)
            # A BLOCKED adaptive simulation rides in the child's JSON
            # report (its stdout is one JSON line; the last non-empty
            # line is parsed, as probe_result_from_child does). Surface
            # its status so run_job records 'blocked' instead of 'ok'
            # (Grok review of PR #25). Any other job's output, and any
            # unparseable tail, is untouched.
            try:
                lines = [ln for ln in str(res.get('stdout_tail') or '')
                         .splitlines() if ln.strip()]
                parsed = json.loads(lines[-1]) if lines else None
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                sim = parsed.get('simulation_status')
                if isinstance(sim, str) and sim.startswith('BLOCKED'):
                    res = dict(res)
                    res['simulation_status'] = sim
            return res
        return runner

    def _run_job_sync(self, job, now=None):
        now = now or datetime.now(IST)
        day = now.date().isoformat()
        # Record the run in render_job_runs (the scheduler's record);
        # a job that already completed today is never started (the
        # guard lives in record_job_start as well as in the due
        # check). The record is best-effort: run_job's paper_job_runs
        # row stays authoritative, so a record failure never blocks
        # the job itself.
        try:
            started = rfs.record_job_start(
                self.store, self.namespace, job['name'], day)
        except Exception:
            started = True
        if not started:
            return
        try:
            result = rj.run_job(self.store, self.namespace, job['name'],
                                day, self.owner_id, self._job_runner(job))
            status, error = 'ok', None
            if isinstance(result, dict):
                # A BLOCKED adaptive simulation is not a success: the
                # day was not simulated, so record 'blocked' (with the
                # reason) in render_job_runs too, matching the
                # paper_job_runs row run_job just wrote.
                sim = result.get('simulation_status')
                if isinstance(sim, str) and sim.startswith('BLOCKED'):
                    status, error = 'blocked', sim
        except Exception as ex:
            # Recorded in paper_job_runs; the next tick retries
            # in-window. A job exception never kills the scheduler
            # or the worker.
            status, error = 'error', type(ex).__name__
        try:
            rfs.record_job_finish(
                self.store, self.namespace, job['name'], day,
                status, error)
        except Exception:
            pass

    def _refresh_health_store(self, force=False):
        """Refresh the cached /health store fields (Task 3), at most
        once per RENDER_HEALTH_REFRESH_S (default 300 s) unless forced.
        One refresh is a handful of SELECTs; /health itself never
        touches the store. A failed refresh keeps the last good values
        (stale, with store_age_s growing) and marks store_ok false;
        it never raises into the tick or /health. Also updates
        self.breakers from the same job rows."""
        cache = getattr(self, '_health_cache', None)
        if cache is None:
            cache = rh.new_health_cache()
            self._health_cache = cache
        if not force and cache.get('refreshed_at') is not None and \
                time.time() - cache['refreshed_at'] < \
                rh.health_refresh_seconds():
            return cache
        err = None
        result = None
        try:
            # The loaded schedule and config feed /health.schedule
            # (full daily schedule, brief 23 Nov 2026). A schedule
            # load failure leaves that field unknown, never the whole
            # refresh failed.
            try:
                sched = rsch.load_schedule(mode=self.mode)
            except Exception:
                sched = None
            result = rh.refresh_store_fields(
                self.store, self.namespace, self.mode,
                schedule=sched, config=self.config)
            if not result.get('ok'):
                # The store answered nothing: every read group failed.
                err = 'store_unreachable'
        except Exception as ex:
            err = type(ex).__name__
        if err is not None:
            reasons = dict(cache.get('reasons') or {})
            reasons['store_refresh'] = err
            if cache.get('refreshed_at') is None:
                # No good data yet: every field is unknown because the
                # store could not be read, not because it is empty.
                reasons = {k: 'store_unreachable'
                           for k in (cache.get('fields') or {})}
                reasons['store_refresh'] = err
            self._health_cache = {
                'ok': False, 'refreshed_at': cache.get('refreshed_at'),
                'error': err, 'fields': cache.get('fields') or {},
                'reasons': reasons}
            return self._health_cache
        self._health_cache = {
            'ok': True, 'refreshed_at': time.time(),
            'error': None, 'fields': result['fields'],
            'reasons': result['reasons']}
        self.breakers = result['fields'].get('breakers') or {}
        return self._health_cache

    def _refresh_breakers(self):
        """Re-read the open crash-loop breakers from the store into
        self.breakers. The store is authoritative; on a read failure
        the last known breakers stand (a breaker is never silently
        cleared by a transient store error)."""
        try:
            day = datetime.now(IST).date().isoformat()
            self.breakers = rj.open_breakers(self.store, self.namespace,
                                             day)
        except Exception:
            pass
        return self.breakers

    def _manage_jobs(self, now=None):
        if self._job_thread is not None and self._job_thread.is_alive():
            return
        now = now or datetime.now(IST)
        try:
            sched = rsch.load_schedule(mode=self.mode)
            last = rj.last_run_times(self.store, self.namespace)
        except Exception:
            return
        # Startup catch-up first (full schedule, brief 23 Nov 2026):
        # a missed scan (before 15:00) or a missed EOD (after 16:10)
        # after a restart is a trading job and outranks housekeeping.
        # The normal 30-minute window is too short for a restart
        # mid-morning, so catch-up covers it, each exactly once per
        # trade_date. Completed jobs come from both job tables, so a
        # job that finished through the older path is not re-run
        # after a restart. On a normal tick the catch-up is empty
        # (the slot has not passed yet, or the job already ran).
        due = []
        try:
            completed = rfs.completed_jobs_today(
                self.store, self.namespace, now.date().isoformat())
            due = rfs.catch_up_due_jobs(
                sched, now, completed, self.config)
        except Exception:
            pass
        if not due:
            try:
                due = rsch.due_jobs(sched, now=now, last_run=last)
            except Exception:
                return
            if not due:
                return
        # A job whose breaker is open is not started again today.
        self._refresh_breakers()
        due = [j for j in due if j['name'] not in self.breakers]
        # Adaptive EOD depends on today's history sync (Grok review of
        # PR #25): the adaptive job needs sync_history's data, so it
        # must not run before sync_history has finished ok today. The
        # sync is listed before adaptive in render_schedule.json, so
        # when both are due the sync runs first; this filter covers the
        # case where the sync's window has passed without an ok run
        # (then adaptive waits for tomorrow instead of running on
        # stale data). A store read failure leaves the dependency
        # unsatisfied: the job is skipped this tick, never run blind.
        if any(j['name'] == 'adaptive_eod' for j in due):
            try:
                completed_today = rfs.completed_jobs_today(
                    self.store, self.namespace, now.date().isoformat())
            except Exception:
                completed_today = set()
            if 'sync_history' not in completed_today:
                due = [j for j in due if j['name'] != 'adaptive_eod']
        if not due:
            return
        job = due[0]
        # Single writer (full schedule): a write job only runs when
        # this supervisor holds the lease. The tick gates on it, so
        # this is defense in depth for a lease lost mid-tick; the
        # skip is recorded so /health.schedule shows it.
        if not self.lease_held:
            try:
                rfs.record_job_skipped(
                    self.store, self.namespace, job['name'],
                    now.date().isoformat(), 'lease_held_by_other')
            except Exception:
                pass
            return
        self._job_thread = threading.Thread(
            target=self._run_job_sync, args=(job, now), daemon=True)
        self._job_thread.start()

    # -- boot-time one-shot actions (Tasks 1-2) ----------------------
    def _run_probe_child(self):
        """Spawn the quote-probe child and return its run_job_streaming
        result (or raise subprocess.TimeoutExpired). Tests replace this
        with a canned result; the default runs the real child, which
        imports the engine in its own process and exits with it, so
        the supervisor never pays the numpy/pandas import."""
        cmd = [sys.executable, 'render_quote_probe.py']
        return run_job_streaming(cmd, self._child_env(), timeout=120)

    def _maybe_run_quote_probe(self):
        """Run the one-shot quote probe (Task 1) when enabled.

        Returns the recorded result, a refusal record, or None when
        the env is unset. The result is written to paper_runtime_kv
        (key 'quote_probe') and to the audit event
        'render_quote_probe'; /health serves it from the refresher's
        cache. A refusal never writes to the store. Never raises into
        the tick."""
        import render_boot as rb
        if not rb.probe_enabled():
            return None
        ok, reason = rb.guard_ok(self.mode, self.namespace)
        if not ok:
            print('quote probe refused: %s' % reason, flush=True)
            return {'status': 'refused', 'reason': reason,
                    'symbols': list(rb.PROBE_SYMBOLS),
                    'at': datetime.now(IST).isoformat()}
        try:
            res = self._run_probe_child()
        except subprocess.TimeoutExpired:
            result = rb.probe_failed_result(
                'probe_timeout', 'quote probe child timed out')
        except Exception as ex:
            result = rb.probe_failed_result(
                'probe_child_failed', type(ex).__name__)
        else:
            result = rb.probe_result_from_child(res)
        try:
            result['audit_ok'] = rb.emit_probe_audit(
                self.store, self.namespace, result)
        except Exception:
            result['audit_ok'] = False
        try:
            rb.kv_put(self.store, self.namespace, rb.QUOTE_PROBE_KEY,
                      result)
        except Exception as ex:
            print('quote probe kv write failed: %s' % type(ex).__name__,
                  flush=True)
        return result

    def _maybe_run_once_job(self):
        """Run the one-shot trial job (Task 2) when requested.

        Returns a status record, or None when RENDER_RUN_ONCE is
        unset. The job goes through the normal job runner (paper_job_
        runs, RSS cap, crash-loop breaker) even though it is in
        RENDER_DISABLED_JOBS, and a done-marker keyed by date+job is
        written after the run settles, so a restart never repeats it.
        A refusal or an open breaker writes no marker and starts
        nothing. Never raises into the tick."""
        import render_boot as rb
        job_name = rb.run_once_requested()
        if job_name is None:
            return None
        ok, reason = rb.guard_ok(self.mode, self.namespace)
        if not ok:
            print('run-once refused: %s' % reason, flush=True)
            return {'status': 'refused', 'reason': reason, 'job': job_name}
        day = datetime.now(IST).date().isoformat()
        if rb.run_once_done(self.store, self.namespace, job_name, day) \
                is not None:
            return {'status': 'already_done', 'job': job_name, 'day': day}
        job = rb.find_run_once_job(job_name)
        if job is None:
            print('run-once refused: no enabled trial job %r' % job_name,
                  flush=True)
            return {'status': 'refused', 'reason': 'job_not_found',
                    'job': job_name}
        if rj.breaker_open(self.store, self.namespace, job_name, day):
            print('run-once skipped: breaker open for %s' % job_name,
                  flush=True)
            return {'status': 'skipped', 'reason': 'breaker_open',
                    'job': job_name, 'day': day}
        if self._job_thread is not None and self._job_thread.is_alive():
            return {'status': 'skipped', 'reason': 'job_already_running',
                    'job': job_name, 'day': day}

        def _run():
            try:
                rj.run_job(self.store, self.namespace, job['name'], day,
                           self.owner_id, self._job_runner(job))
                outcome = {'status': 'ok'}
            except Exception as ex:
                outcome = {'status': 'error',
                           'error': type(ex).__name__}
            try:
                rb.mark_run_once(self.store, self.namespace, job_name,
                                 day, outcome)
            except Exception:
                pass

        self._job_thread = threading.Thread(target=_run, daemon=True)
        self._job_thread.start()
        return {'status': 'started', 'job': job_name, 'day': day}

    def _run_boot_actions(self):
        """One-shot boot actions (Tasks 1-2), once per supervisor boot.

        Runs on the first tick where the lease is held (so this
        supervisor is the active runner), before the worker starts.
        Each action never raises into the tick; a failure is printed
        (type only, never a secret) and the other action still runs.
        """
        if getattr(self, '_boot_actions_done', False):
            return
        self._boot_actions_done = True
        # Fyers token first, so the probe child (spawned below) gets
        # the fresh token through _child_env. Never raises into the
        # tick; a failure leaves the legacy token path untouched.
        try:
            mgr = getattr(self, '_fyers_auth', None)
            if mgr is not None:
                mgr.refresh(kind='scheduled')
        except Exception as ex:
            print('fyers token refresh error: %s' % type(ex).__name__,
                  flush=True)
        try:
            self._maybe_run_quote_probe()
        except Exception as ex:
            print('quote probe error: %s' % type(ex).__name__, flush=True)
        try:
            self._maybe_run_once_job()
        except Exception as ex:
            print('run-once error: %s' % type(ex).__name__, flush=True)

    def _maybe_refresh_token(self, now=None):
        """Daily 08:30 IST token refresh (retry window until 09:05).

        Only the active runner (lease held) refreshes, only on
        trading days, only inside 08:30-09:05 IST, and only when no
        token was obtained today yet. The supervisor tick runs every
        60 s, so a failed attempt is retried on the next tick until
        09:05, bounded by the manager (3 attempts per hour, 30-minute
        circuit). `now` is injectable for tests. Never raises into
        the tick."""
        mgr = getattr(self, '_fyers_auth', None)
        if mgr is None or not self.lease_held:
            return
        try:
            now = now or datetime.now(IST)
            # Cheap time check first: is_trading_day imports the
            # engine (slow), so it must only run inside the window,
            # never on every tick before the worker starts.
            if not (dt_time(8, 30) <= now.time() < dt_time(9, 5)):
                return
            if not rsch.is_trading_day(now.date()):
                return
            if mgr.last_success_date() == now.date():
                return
            mgr.refresh(kind='scheduled')
        except Exception:
            pass

    # -- main tick ------------------------------------------------------
    def tick(self, now=None):
        now = now or datetime.now(IST)
        self.last_tick_at = time.time()
        # The background refresher (due-checked, at most once per
        # RENDER_HEALTH_REFRESH_S): /health serves its cache.
        self._refresh_health_store()
        try:
            flag = rl.get_active_runner(self.store, self.namespace)
            self.store_error = None
        except Exception as ex:
            self.store_error = type(ex).__name__
            self.last_snapshot = self.snapshot(None)
            return self.last_snapshot
        self.last_flag = flag
        lease_was_held = self.lease_held
        if flag != 'render':
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
            if self.lease_held:
                self.lease.release()
                self.lease_held = False
            if self.lease_held != lease_was_held:
                # The lease just changed hands: refresh so /health
                # shows the new lease state, not the pre-change cache.
                self._refresh_health_store(force=True)
            self.last_snapshot = self.snapshot(flag)
            return self.last_snapshot
        if not self.lease_held:
            self.lease_held = self.lease.acquire()
            if self.lease_held:
                self.last_lease_renew_at = time.time()
            if self.lease_held != lease_was_held:
                # The lease was just acquired in this tick, after the
                # start-of-tick refresh read no lease row: refresh
                # again so the snapshot shows the held lease.
                self._refresh_health_store(force=True)
            if not self.lease_held:
                self.last_snapshot = self.snapshot(flag)
                return self.last_snapshot
        # One-shot boot actions (probe + run-once) on the first tick
        # where this supervisor holds the lease.
        self._run_boot_actions()
        # worker first (the primary process), then due jobs
        self._manage_worker()
        self._manage_jobs(now)
        # Daily 08:30 token refresh (retry window until 09:05). Runs
        # after the worker starts, so a slow calendar read here can
        # never delay the worker or serve a healthy snapshot with
        # the worker still stopped.
        self._maybe_refresh_token(now)
        self.last_snapshot = self.snapshot(flag)
        return self.last_snapshot

    def renew_lease(self):
        """Called by the renewal thread every 20 s while the lease is held."""
        if self.lease_held:
            self.lease_held = self.lease.renew()
            if self.lease_held:
                self.last_lease_renew_at = time.time()

    def snapshot(self, flag):
        """Build the /health payload from memory only (Task 3).

        Never touches the store: the lease, jobs and all store-read
        fields come from the background refresher's cache
        (_refresh_health_store, driven by tick). Before the first
        refresh the fields are null with 'not_refreshed_yet' reasons.
        `getattr` fallbacks keep Supervisors constructed without
        __init__ (tests) working.
        """
        if flag is None:
            flag = self.last_flag
        cache = getattr(self, '_health_cache', None)
        if cache is None:
            cache = rh.EMPTY_HEALTH_CACHE
        fields = cache.get('fields') or {}
        reasons = cache.get('reasons') or {}
        worker = (self.worker.describe() if self.worker is not None
                  else {'state': 'stopped', 'pid': None, 'rss_mb': 0,
                        'restarts': 0, 'restarts_10m': 0,
                        'last_heartbeat_age_s': None})
        if self.store_error is not None:
            worker = dict(worker, state='crashed')
        # The heartbeat age comes from the store refresher (the worker
        # process itself does not report it); the rest is in-memory.
        # Fix 2: the worker's own status, consecutive failures and
        # last successful quote also come from the refresher (they are
        # read from the heartbeat row / the audit events, not from the
        # process), so /health shows a backoff instead of hiding it.
        worker = dict(worker, last_heartbeat_age_s=fields.get(
            'worker_last_heartbeat_age_s'),
            status=fields.get('worker_status'),
            consecutive_failures=fields.get(
                'worker_consecutive_failures'),
            last_quote_ok_at=fields.get('worker_last_quote_ok_at'))
        breakers = getattr(self, 'breakers', None) or {}
        if self.stopping:
            state = 'stopping'
        elif self.store_error is not None or worker['state'] == 'crashed':
            state = 'degraded'
        elif breakers:
            # A breaker is protective, not a failure: the supervisor is
            # up and the worker runs, it just will not start that job
            # again today. State degraded, /health stays 200 (see
            # build_snapshot's ok rule).
            state = 'degraded'
        elif flag != 'render':
            state = 'idle_not_active_runner'
        elif not self.lease_held:
            state = 'standby'
        elif worker['state'] == 'running':
            state = 'active'
        else:
            state = 'starting'
        if self.lease_held and flag == 'render':
            role = 'active'
        elif flag == 'render':
            role = 'standby'
        else:
            role = 'idle'
        now = datetime.now(IST)
        today = now.date()
        session_open = False
        window = session_window(self.config, today)
        if window:
            moment = now.timetz().replace(tzinfo=None)
            session_open = window[0] <= moment < window[1]
        valid_through = self.config.get('calendar_valid_through')
        try:
            days_left = (date.fromisoformat(valid_through) - today).days \
                if valid_through else None
        except Exception:
            days_left = None
        caps = rt.trial_caps()
        refreshed_at = cache.get('refreshed_at')
        mgr = getattr(self, '_fyers_auth', None)
        return rh.build_snapshot(
            mode=self.mode, namespace=self.namespace, active_runner=flag,
            lease_held=self.lease_held, lease=fields.get('lease'),
            worker=worker, jobs=fields.get('jobs') or {},
            uptime_seconds=time.time() - self.started_at,
            memory_mb=rh.memory_rss_mb(),
            state=state, role=role, runner_id=self.owner_id,
            session_open=session_open,
            calendar_valid_through=valid_through,
            calendar_days_left=days_left,
            last_tick_ist=(datetime.fromtimestamp(
                self.last_tick_at, IST).isoformat()
                if self.last_tick_at is not None else None),
            last_lease_renew_age_s=(
                round(time.time() - self.last_lease_renew_at, 1)
                if self.last_lease_renew_at is not None else None),
            jobs_today=fields.get('jobs_today') or {},
            last_job=fields.get('last_job'),
            children_rss_mb=rh.children_rss_mb(),
            breakers=breakers,
            store_ok=cache.get('ok', False),
            store_age_s=(int(time.time() - refreshed_at)
                         if refreshed_at is not None else None),
            reasons=reasons,
            peak_job_rss_mb=fields.get('peak_job_rss_mb'),
            last_job_peak_rss_mb=fields.get('last_job_peak_rss_mb'),
            job_rss_cap_mb=job_rss_cap_mb(),
            quote_probe=fields.get('quote_probe'),
            store_reads_total=fields.get('store_reads_total'),
            support_monitor=getattr(
                self, 'support_monitor_state', None),
            support_agent=getattr(
                self, 'support_agent_state', None),
            fyers_auth=(mgr.status() if mgr is not None else None),
            schedule=fields.get('schedule'),
            fyers_budget={
                'day_used': fields.get('fyers_budget_day_used'),
                'per_day_limit': int(
                    (self.config.get('market_data') or {}).get(
                        'budget_requests_per_day', 4000))},
            auth={'circuit_open': None},
            trial_budget=({'day_used': fields.get(
                               'trial_budget_day_used'),
                           'day_cap': caps['day'],
                           'minute_cap': caps['minute']}
                          if self.mode == 'trial' else None),
            box_conflict_check=(
                fields.get('box_conflict_check')
                if self.mode == 'trial' else None))

    def shutdown(self):
        self.stopping = True
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        if self.lease_held:
            try:
                self.lease.release()
            except Exception:
                pass
            self.lease_held = False


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return
        code, payload = rh.health_response(
            self.server.supervisor.last_snapshot
            or self.server.supervisor.snapshot(None))
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_POST(self):
        # The health endpoint is GET-only; anything else is not a route.
        self.send_response(405)
        self.send_header('Content-Length', '0')
        self.end_headers()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render supervisor + health')
    parser.add_argument('--check-namespace-only', action='store_true')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('PORT', '10000')))
    args = parser.parse_args(argv)
    if args.check_namespace_only:
        ok, ns = check_namespace()
        if ok:
            print('namespace ok: %s' % ns, flush=True)
            return 0
        return 3
    store = rs.open_store_from_env()
    rs.migrate(store)
    ok, namespace = check_namespace()
    if not ok:
        print('namespace refused; supervisor not starting', flush=True)
        return 3
    mode = rm.render_mode()
    owner_id = rl.make_owner_id('render')
    sup = Supervisor(store, namespace, mode, owner_id)

    httpd = ThreadingHTTPServer(('0.0.0.0', args.port), _Handler)
    httpd.supervisor = sup
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print('render supervisor up: mode=%s namespace=%s port=%d' % (
        mode, namespace, args.port), flush=True)

    def _stop(signum, frame):
        sup.stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def _renew_loop():
        while not sup.stopping:
            time.sleep(rl.LEASE_RENEW_SECONDS)
            if not sup.stopping:
                try:
                    sup.renew_lease()
                except Exception:
                    pass

    threading.Thread(target=_renew_loop, daemon=True).start()

    # Read-only support monitor (brief 23 Nov 2026): its own daemon
    # thread next to the worker. It reads the paper book from Turso
    # (SELECT only) and the in-process /health snapshot, and records
    # findings in support_monitor_findings. A monitor crash never
    # stops the worker: each cycle is try/except and it backs off
    # after errors (see support_monitor.Monitor). Disabled entirely
    # when SUPPORT_MONITOR_ENABLED=0 (the /health field then reports
    # enabled false). The thread is not started when disabled.
    def _monitor_state(state):
        sup.support_monitor_state = state

    sm.start_monitor_thread(
        store, namespace, sup.config,
        lambda: sup.last_snapshot,
        _monitor_state, lambda: sup.stopping)

    # Full Support Agent (brief Nov 2026): its own daemon thread
    # next to the worker and the monitor. It runs the box Support
    # Agent's v3 detector suite on Turso (SELECT only) and records
    # hash-chained findings in support_findings. An agent crash never
    # stops the worker: each cycle is try/except and it backs off
    # after errors (see support_agent_render.Agent). Disabled
    # entirely when SUPPORT_AGENT_ENABLED=0 (the /health field then
    # reports enabled false). The thread is not started when disabled.
    def _agent_state(state):
        sup.support_agent_state = state

    sar.start_agent_thread(
        store, namespace, sup.config,
        _agent_state, lambda: sup.stopping)

    while not sup.stopping:
        try:
            sup.tick()
        except Exception:
            pass
        # Sleep the 60 s tick in 1 s slices so SIGTERM (which sets
        # sup.stopping) is honoured within ~1 s, not up to 60 s:
        # time.sleep() retries after a signal handler (PEP 475), so a
        # single 60 s sleep would delay shutdown past the 25 s exit
        # guarantee the SIGTERM test pins.
        for _ in range(SUPERVISOR_TICK_SECONDS):
            if sup.stopping:
                break
            time.sleep(1)
    sup.shutdown()
    httpd.shutdown()
    print('render supervisor stopped', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
