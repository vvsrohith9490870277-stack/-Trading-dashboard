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
import runtime_mode as rm

IST = ZoneInfo('Asia/Kolkata')
WORKER_BACKOFF_SECONDS = (5, 10, 20, 40, 60)
WORKER_STOP_TIMEOUT_SECONDS = 20


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
        return True, ns
    except Exception as ex:
        print('namespace refused: %s' % type(ex).__name__, flush=True)
        return False, None


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

    # -- worker ---------------------------------------------------------
    def _worker_cmd(self):
        if os.environ.get('PAPER_FAKE_WORKER') == '1':
            return [sys.executable, '-c', 'import time; time.sleep(3600)']
        return [sys.executable, 'engine.py', 'worker']

    def _child_env(self):
        env = dict(os.environ)
        env['PAPER_RUNTIME'] = 'render'
        env['RENDER_MODE'] = self.mode
        env['RENDER_OWNER_ID'] = self.owner_id
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
    def _run_job_sync(self, job):
        def runner(name):
            cmd = [sys.executable, 'engine.py', job['command']]
            return run_job_streaming(cmd, self._child_env(), timeout=1800)
        day = datetime.now(IST).date().isoformat()
        try:
            rj.run_job(self.store, self.namespace, job['name'], day,
                       self.owner_id, runner)
        except Exception:
            pass  # recorded in paper_job_runs; the next tick retries in-window

    def _refresh_health_store(self, force=False):
        """Refresh the cached /health store fields (Task 3), at most
        once per RENDER_HEALTH_REFRESH_S (default 60 s) unless forced.
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
            result = rh.refresh_store_fields(self.store, self.namespace,
                                             self.mode)
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

    def _manage_jobs(self):
        if self._job_thread is not None and self._job_thread.is_alive():
            return
        try:
            sched = rsch.load_schedule(mode=self.mode)
            last = rj.last_run_times(self.store, self.namespace)
            due = rsch.due_jobs(sched, last_run=last)
        except Exception:
            return
        if not due:
            return
        # A job whose breaker is open is not started again today.
        self._refresh_breakers()
        due = [j for j in due if j['name'] not in self.breakers]
        if not due:
            return
        job = due[0]
        self._job_thread = threading.Thread(
            target=self._run_job_sync, args=(job,), daemon=True)
        self._job_thread.start()

    # -- main tick ------------------------------------------------------
    def tick(self):
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
        # worker first (the primary process), then due jobs
        self._manage_worker()
        self._manage_jobs()
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
        worker = dict(worker, last_heartbeat_age_s=fields.get(
            'worker_last_heartbeat_age_s'))
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
    while not sup.stopping:
        try:
            sup.tick()
        except Exception:
            pass
        time.sleep(5)
    sup.shutdown()
    httpd.shutdown()
    print('render supervisor stopped', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
