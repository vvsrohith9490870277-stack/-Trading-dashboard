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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import render_health as rh
import render_jobs as rj
import render_lease as rl
import render_schedule as rsch
import render_state as rs
import runtime_mode as rm

IST = ZoneInfo('Asia/Kolkata')
WORKER_BACKOFF_SECONDS = (5, 10, 20, 40, 60)
WORKER_STOP_TIMEOUT_SECONDS = 20


def repo_config_namespace():
    """The namespace from config.json (next to engine.py; never edited)."""
    cfg_path = Path(__file__).resolve().parent / 'config.json'
    return json.loads(cfg_path.read_text())['namespace']


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


class Worker:
    """The engine worker subprocess, with crash backoff and a memory guard."""

    def __init__(self, cmd, env, restarts=0):
        self.cmd = cmd
        self.env = env
        self.proc = None
        self.restarts = restarts
        self.backoff_idx = 0
        self.next_start = 0.0
        self.state = 'stopped'

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        return {'state': self.poll(),
                'pid': self.proc.pid if self.proc is not None else None,
                'rss_mb': self.rss_mb(), 'restarts': self.restarts}


class Supervisor:
    def __init__(self, store, namespace, mode, owner_id):
        self.store = store
        self.namespace = namespace
        self.mode = mode
        self.owner_id = owner_id
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

    def _manage_worker(self):
        if self.worker is None:
            self.worker = Worker(self._worker_cmd(), self._child_env())
            self.worker.start()
            return
        state = self.worker.poll()
        if state == 'running':
            if rh.memory_over_limit(self.worker.rss_mb()):
                # memory guard: restart a worker over 450 MB
                restarts = self.worker.restarts
                self.worker.stop()
                self.worker = Worker(self._worker_cmd(), self._child_env(),
                                     restarts=restarts)
                self.worker.start()
            return
        if state == 'crashed' and time.time() >= self.worker.next_start:
            restarts = self.worker.restarts
            self.worker = Worker(self._worker_cmd(), self._child_env(),
                                 restarts=restarts)
            self.worker.start()

    # -- jobs -----------------------------------------------------------
    def _run_job_sync(self, job):
        def runner(name):
            cmd = [sys.executable, 'engine.py', job['command']]
            p = subprocess.run(cmd, env=self._child_env(),
                               capture_output=True, text=True, timeout=1800)
            return {'exit_code': p.returncode,
                    'stdout_tail': p.stdout[-2000:],
                    'stderr_tail': p.stderr[-2000:]}
        day = datetime.now(IST).date().isoformat()
        try:
            rj.run_job(self.store, self.namespace, job['name'], day,
                       self.owner_id, runner)
        except Exception:
            pass  # recorded in paper_job_runs; the next tick retries in-window

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
        job = due[0]
        self._job_thread = threading.Thread(
            target=self._run_job_sync, args=(job,), daemon=True)
        self._job_thread.start()

    # -- main tick ------------------------------------------------------
    def tick(self):
        try:
            flag = rl.get_active_runner(self.store, self.namespace)
            self.store_error = None
        except Exception as ex:
            self.store_error = type(ex).__name__
            self.last_snapshot = self.snapshot(None)
            return self.last_snapshot
        self.last_flag = flag
        if flag != 'render':
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
            if self.lease_held:
                self.lease.release()
                self.lease_held = False
            self.last_snapshot = self.snapshot(flag)
            return self.last_snapshot
        if not self.lease_held:
            self.lease_held = self.lease.acquire()
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

    def snapshot(self, flag):
        if flag is None:
            flag = self.last_flag
        try:
            lease = rl.get_lease(self.store, self.namespace)
        except Exception:
            lease = None
        try:
            jobs = {k: v.isoformat() for k, v in
                    rj.last_run_times(self.store, self.namespace).items()}
        except Exception:
            jobs = {}
        worker = (self.worker.describe() if self.worker is not None
                  else {'state': 'stopped', 'pid': None, 'rss_mb': 0,
                        'restarts': 0})
        if self.store_error is not None:
            worker = dict(worker, state='crashed')
        return rh.build_snapshot(
            mode=self.mode, namespace=self.namespace, active_runner=flag,
            lease_held=self.lease_held, lease=lease, worker=worker,
            jobs=jobs, uptime_seconds=time.time() - self.started_at,
            memory_mb=rh.memory_rss_mb())

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
