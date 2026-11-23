"""Full Support Agent on Render (brief: support agent on Render, Nov 2026).

Runs the box Support Agent's v3 production detector suite
(support_agent.run_v3_production_detectors) inside the Render
supervisor next to the worker, reading the paper book from Turso
(SELECT only) and writing hash-chained findings to the append-only
support_findings table.

How it differs from the box version:
- No box files: no engine.jsonl, no audit-*.jsonl, no local snapshot
  sqlite, no report files. Inputs are Turso tables only
  (paper_events, paper_decisions, paper_fills, paper_signals,
  paper_price_bands) plus the repo's config/policy files.
- No virtual-clock replay: one live pass per cycle over the current
  session day. The detectors are idempotent across passes (the box
  replay runs the same suite at five ticks a day and asserts zero
  duplicates), so a repeated cycle adds no duplicate findings.
- No box_metrics / dashboard_probe inputs (None): those detectors
  are skipped on Render.
- The support store and the trading store are the same Turso store
  (different tables); the box agent's _write_support_only guard still
  enforces that only support tables are ever written.
- Findings go to support_findings (hash-chained, append-only, with
  status transitions), NOT to support_monitor_findings (the
  monitor's flat table). The monitor stays the fast watchdog (5 min);
  the agent is the deep pass.
- Interval is 15 minutes by default, not the box policy's 5 minutes:
  each agent cycle re-reads the full session day's events over Turso
  HTTP (the monitor's cycles are 5 bounded SELECTs), so the longer
  interval keeps the read load bounded. Override with
  SUPPORT_AGENT_INTERVAL_S (clamped to 300-3600).

Read-only on trading state: the only writes are INSERTs into the
support tables (support_findings, support_episodes,
support_checkpoints, support_detector_runs) plus CREATE TABLE IF NOT
EXISTS for those tables. Never INSERT, UPDATE or DELETE any trading
table (positions, fills, signals, decisions, ledger, ohlcv), never
place an order, never call a Fyers order endpoint. No box files are
read (no /workspace/... paths). No new dependency (stdlib + the
repo's support_agent module).

Env:
  SUPPORT_AGENT_ENABLED    '1' (default) to run, '0' to disable.
  SUPPORT_AGENT_INTERVAL_S seconds between cycles (default 900),
    clamped to [300, 3600].

A cycle never stops the worker: each cycle is try/except, and it
backs off after errors, like the monitor.
"""
import json
import os
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import support_agent as sa
import support_monitor as sm

IST = ZoneInfo('Asia/Kolkata')

AGENT_ENABLED_ENV = 'SUPPORT_AGENT_ENABLED'
AGENT_INTERVAL_ENV = 'SUPPORT_AGENT_INTERVAL_S'
DEFAULT_INTERVAL_S = 900
MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 3600
MAX_BACKOFF_MULTIPLIER = 6  # sleep at most interval * 6 after errors

# The agent window ends at 17:00 IST (not the monitor's close+10min)
# so the 16:35 EOD expectation in the v3 suite is evaluated.
AGENT_WINDOW_END = (17, 0)
SESSION_OPEN_MARGIN_MIN = 5  # agent starts 5 min before the open


def agent_enabled(env=None):
    """True unless SUPPORT_AGENT_ENABLED is exactly '0' (default on)."""
    env = os.environ if env is None else env
    return str(env.get(AGENT_ENABLED_ENV, '1')).strip() != '0'


def agent_interval_seconds(env=None):
    """Seconds between cycles (default 900), clamped to [300, 3600]."""
    env = os.environ if env is None else env
    try:
        value = int(env.get(AGENT_INTERVAL_ENV, str(DEFAULT_INTERVAL_S)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, value))


def default_state(enabled=True):
    """The /health support_agent dict when the agent never ran."""
    return {'enabled': bool(enabled), 'last_run_at': None,
            'last_findings_count': 0, 'findings_created_total': 0,
            'runs_total': 0, 'last_error': None,
            'last_session_day': None}


def in_agent_window(config, now):
    """True inside the agent's active window on a trading day.

    The window is the calendar session widened at the start (5 minutes
    before the open, like the monitor) and extended to 17:00 IST at
    the end so the v3 suite's 16:35 EOD expectation is evaluated.
    Outside it (or on a non-trading day, or outside the verified
    calendar) the agent does not act.
    """
    window = sm.session_window_for(config, now.date())
    if not window:
        return False
    start = (datetime.combine(now.date(), window[0])
             - timedelta(minutes=SESSION_OPEN_MARGIN_MIN))
    end = datetime.combine(
        now.date(),
        dt_time(hour=AGENT_WINDOW_END[0], minute=AGENT_WINDOW_END[1]))
    aware_start = start.replace(tzinfo=IST)
    aware_end = end.replace(tzinfo=IST)
    return aware_start <= now < aware_end


class TursoAdapter:
    """Adapts a Render store to the box Support Agent's db interface.

    The box agent only needs two methods: query(sql, args) returning
    a list of dicts, and batch(statements) applying a list of
    (sql, args) pairs. Both map directly onto the Render store's
    execute / execute_many. The box agent's _write_support_only guard
    runs before every batch, so only support tables can be written
    even though the trading store and the support store are the same
    Turso database here.
    """

    def __init__(self, store):
        self.store = store

    def query(self, sql, args=()):
        return self.store.execute(sql, args)

    def batch(self, statements):
        return self.store.execute_many(statements)


def ensure_support_tables(adapter):
    """CREATE TABLE IF NOT EXISTS for the box agent's support tables.

    Idempotent: the tables already exist in Turso (created by the box
    agent); this only guarantees they exist before the first cycle.
    """
    sa.init(adapter, include_extended=True)
    return True


class Agent:
    """The agent runner: one detector pass per interval, own thread.

    Holds the mutable state (runs_total, findings counts, backoff)
    and the /health state dict. A cycle never raises: every failure
    is caught and recorded as last_error, and the next sleep backs
    off. The thread never touches the worker.
    """

    def __init__(self, store, namespace, config, now_fn=None,
                 state_fn=None):
        self.store = store
        self.adapter = TursoAdapter(store)
        self.namespace = namespace
        self.config = config
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.state_fn = state_fn
        self.enabled = agent_enabled()
        self.interval_s = agent_interval_seconds()
        self.runs_total = 0
        self.last_run_at = None
        self.last_findings_count = 0
        self.findings_created_total = 0
        self.last_error = None
        self.last_session_day = None
        self._backoff = 0
        self._tables_ensured = False

    def state(self):
        """The /health support_agent dict (replaced wholesale)."""
        return {'enabled': self.enabled,
                'last_run_at': (
                    self.last_run_at.isoformat()
                    if self.last_run_at is not None else None),
                'last_findings_count': self.last_findings_count,
                'findings_created_total': self.findings_created_total,
                'runs_total': self.runs_total,
                'last_error': self.last_error,
                'last_session_day': self.last_session_day}

    def _publish(self):
        if self.state_fn is not None:
            try:
                self.state_fn(self.state())
            except Exception:
                pass

    def cycle(self, now=None):
        """Run one detector pass. Returns the state dict. Never raises."""
        if not self.enabled:
            return self.state()
        now = now or self.now_fn()
        try:
            if not in_agent_window(self.config, now):
                return self.state()
            if not self._tables_ensured:
                try:
                    ensure_support_tables(self.adapter)
                    self._tables_ensured = True
                except Exception as ex:
                    self.last_error = type(ex).__name__
                    self._backoff += 1
                    self._publish()
                    return self.state()
            session_day = now.date().isoformat()
            out = sa.run_v3_production_detectors(
                self.adapter, self.namespace, session_day,
                now_at=now.isoformat(), trading_db=self.adapter,
                observation_only=True)
            results = out.get('results') or []
            created = sum(1 for r in results if r.get('created'))
            self.runs_total += 1
            self.last_run_at = now
            self.last_findings_count = len(results)
            self.findings_created_total += created
            self.last_session_day = session_day
            self.last_error = None
            self._backoff = 0
        except Exception as ex:
            self.last_error = type(ex).__name__
            self._backoff += 1
        self._publish()
        return self.state()

    def serve_forever(self, stop_fn, sleep_fn=None):
        """Thread loop: cycle, then sleep the interval in 1 s slices.

        The 1 s slices honour the stop flag promptly (SIGTERM), like
        the supervisor's main loop. After an error the sleep backs
        off (interval x 2**failures, capped at interval x 6).
        """
        if sleep_fn is None:
            sleep_fn = time.sleep
        while not stop_fn():
            self.cycle()
            if stop_fn():
                break
            mult = min(2 ** self._backoff, MAX_BACKOFF_MULTIPLIER)
            total = self.interval_s * mult
            for _ in range(total):
                if stop_fn():
                    break
                sleep_fn(1)


def start_agent_thread(store, namespace, config, state_fn, stop_fn):
    """Start the agent in a daemon thread; returns (Agent, Thread)
    or (None, None) when disabled. The caller owns `stop_fn`."""
    if not agent_enabled():
        return None, None
    agent = Agent(store, namespace, config, state_fn=state_fn)
    thread = threading.Thread(
        target=agent.serve_forever, args=(stop_fn,), daemon=True)
    thread.start()
    return agent, thread
