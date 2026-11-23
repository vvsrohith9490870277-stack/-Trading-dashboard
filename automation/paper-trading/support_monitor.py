"""Read-only support monitor for the Render service (brief 23 Nov 2026).

Runs inside the Render supervisor next to the worker. Every 5 minutes
during market hours (09:10-15:40 IST on trading days) it reads the paper
book from Turso (SELECT only) and the service's own /health in-process,
and records findings in a log and in the append-only
support_monitor_findings table.

Read-only on trading state: the only write is INSERT into
support_monitor_findings (plus its CREATE TABLE IF NOT EXISTS). Never
INSERT, UPDATE or DELETE any trading table (positions, fills, signals,
decisions, ledger, ohlcv), never place an order, never call a Fyers
order endpoint. No box files are read (no /workspace/... paths); only
Turso and the in-process health snapshot. No pandas or new heavy
dependency (stdlib only). Bounded queries only (LIMIT, current-day
namespace); never reads full history.

A monitor crash never stops the worker: the runner is its own thread,
each cycle is wrapped in try/except, and it backs off after errors.

Env:
  SUPPORT_MONITOR_ENABLED   '1' (default) to run, '0' to disable.
  SUPPORT_MONITOR_INTERVAL_S  seconds between cycles (default 300).
"""
import json
import os
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')

MONITOR_ENABLED_ENV = 'SUPPORT_MONITOR_ENABLED'
MONITOR_INTERVAL_ENV = 'SUPPORT_MONITOR_INTERVAL_S'
DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 3600
MAX_BACKOFF_MULTIPLIER = 6  # sleep at most interval * 6 after errors

# Thresholds (brief 23 Nov 2026).
HEARTBEAT_STALE_S = 180       # worker heartbeat stale > 3 min
STALE_MARK_S = 600            # open position with no mark > 10 min
DEDUP_WINDOW_S = 1800         # same code+symbol within 30 min
SESSION_OPEN_MARGIN_MIN = 5   # monitor starts 5 min before the open
SESSION_CLOSE_MARGIN_MIN = 10  # monitor runs 10 min after the close
# S7 trailing stop (strategy_rules.json S7 trailing_initialization /
# trailing_update): active stop = 0.90 * high-water.
S7_STOP_FACTOR = 0.90

FINDINGS_TABLE = 'support_monitor_findings'
MAX_ROWS_PER_QUERY = 200
MAX_QUERIES_PER_CYCLE = 10

SEVERITIES = ('CRITICAL', 'ERROR', 'WARN', 'INFO')


def monitor_enabled(env=None):
    """True unless SUPPORT_MONITOR_ENABLED is exactly '0' (default on)."""
    env = os.environ if env is None else env
    return str(env.get(MONITOR_ENABLED_ENV, '1')).strip() != '0'


def monitor_interval_seconds(env=None):
    """Seconds between cycles (default 300), clamped to [60, 3600]."""
    env = os.environ if env is None else env
    try:
        value = int(env.get(MONITOR_INTERVAL_ENV, str(DEFAULT_INTERVAL_S)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, value))


def default_state(enabled=True):
    """The /health support_monitor dict when the monitor never ran."""
    return {'enabled': bool(enabled), 'last_run_at': None,
            'last_findings_count': 0, 'reads_total': 0,
            'last_error': None}


# -- session ------------------------------------------------------------
def session_window_for(config, day):
    """Session window for `day` from config.json alone.

    Mirrors render_app.session_window (which mirrors engine.session
    without importing the engine, so the monitor stays light): None
    when the calendar does not verify the day, () when the market is
    closed, else (open, close) times. A cross-check test pins the two
    to agree.
    """
    key = day.isoformat()
    if not (config.get('calendar_valid_from', '')
            <= key <= config.get('calendar_valid_through', '')):
        return None
    if key in (config.get('special_sessions') or {}):
        return tuple(dt_time.fromisoformat(t)
                     for t in config['special_sessions'][key])
    if day.weekday() >= 5 or key in (config.get('holidays') or []):
        return ()
    return tuple(dt_time.fromisoformat(t)
                 for t in config['regular_session'])


def in_monitor_window(config, now):
    """True inside the monitor's active window on a trading day.

    The window is the calendar session widened by the brief's margin:
    5 minutes before the open to 10 minutes after the close, so a
    regular 09:15-15:30 session gives 09:10-15:40 IST. Outside it (or
    on a non-trading day, or outside the verified calendar) the monitor
    does not act.
    """
    window = session_window_for(config, now.date())
    if not window:
        return False
    start = (datetime.combine(now.date(), window[0])
             - timedelta(minutes=SESSION_OPEN_MARGIN_MIN))
    end = (datetime.combine(now.date(), window[1])
           + timedelta(minutes=SESSION_CLOSE_MARGIN_MIN))
    aware_start = start.replace(tzinfo=IST)
    aware_end = end.replace(tzinfo=IST)
    return aware_start <= now < aware_end


# -- findings -----------------------------------------------------------
def _finding(severity, code, symbol, detail):
    return {'severity': severity, 'code': code, 'symbol': symbol,
            'detail': detail}


def _parse_ts(value):
    """Parse an ISO timestamp to an aware IST datetime, or None.

    Naive values are UTC (the store's convention); aware values are
    converted from their own offset. Returns None when missing or
    unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def check(snapshot):
    """Pure check: snapshot dict -> list of findings.

    The snapshot carries everything the checks need (fetched by
    fetch_snapshot); this function does no I/O, so it is unit-testable
    with fake snapshots. Findings are dicts with severity, code,
    symbol (or None) and a JSON-serialisable detail (no sensitive values).
    """
    findings = []
    now = snapshot['now_ist']
    in_session = snapshot.get('in_session', False)

    # (f) namespace != configured one. Always checked: reading the
    # wrong book is critical whenever it is detected.
    if snapshot.get('namespace_configured') != snapshot.get(
            'namespace_effective'):
        findings.append(_finding(
            'CRITICAL', 'namespace_mismatch', None,
            {'configured': snapshot.get('namespace_configured'),
             'effective': snapshot.get('namespace_effective')}))

    # (g) Turso errors in the last cycle. Always checked. All failed
    # queries collapse into one finding (so the 30-minute dedup on
    # code+symbol does not drop a second query's failure); the detail
    # names every failed query.
    errors = snapshot.get('fetch_errors') or []
    if errors:
        findings.append(_finding(
            'ERROR', 'turso_error', None,
            {'queries': [{'query': e.get('query'),
                          'error': e.get('error')} for e in errors]}))

    # (b) /health ok=false or quote_probe errors. Always checked.
    if snapshot.get('health_ok') is False:
        findings.append(_finding(
            'ERROR', 'health_not_ok', None,
            {'health_ok': False}))
    probe = snapshot.get('quote_probe')
    if isinstance(probe, dict) and (
            probe.get('status') == 'failed' or probe.get('error_code')):
        findings.append(_finding(
            'ERROR', 'quote_probe_error', None,
            {'status': probe.get('status'),
             'error_code': probe.get('error_code')}))

    if not in_session:
        return findings

    # (a) worker heartbeat stale > 3 min during the session. No
    # heartbeat row yet means the worker never reported (not a stale
    # heartbeat); only a present-but-old heartbeat flags.
    hb_age = snapshot.get('heartbeat_age_s')
    if hb_age is not None and hb_age > HEARTBEAT_STALE_S:
        findings.append(_finding(
            'ERROR', 'worker_heartbeat_stale', None,
            {'age_s': hb_age, 'threshold_s': HEARTBEAT_STALE_S}))

    # S7 EXIT decisions recorded today, for check (d).
    s7_exits = {}
    for dec in snapshot.get('s7_exit_decisions') or []:
        s7_exits.setdefault(dec.get('symbol'), []).append(
            _parse_ts(dec.get('recorded_at')))

    for pos in snapshot.get('positions') or []:
        symbol = pos.get('symbol')
        qty = pos.get('qty')
        # (e) negative quantity. The DB CHECK(qty>=0) should prevent
        # it; this is the invariant check.
        if isinstance(qty, (int, float)) and qty < 0:
            findings.append(_finding(
                'CRITICAL', 'negative_quantity', symbol,
                {'strategy': pos.get('strategy'), 'qty': qty}))
        if not isinstance(qty, (int, float)) or qty <= 0:
            continue  # only open positions for (c) and (d)
        # (c) open position with no mark updated > 10 min. The
        # effective mark time is mark_at, falling back to updated_at
        # (always set) for a position never marked.
        mark_at = _parse_ts(pos.get('mark_at')) or _parse_ts(
            pos.get('updated_at'))
        if mark_at is None:
            findings.append(_finding(
                'WARN', 'stale_mark', symbol,
                {'strategy': pos.get('strategy'),
                 'reason': 'no_mark_timestamp'}))
        elif (now - mark_at).total_seconds() > STALE_MARK_S:
            findings.append(_finding(
                'WARN', 'stale_mark', symbol,
                {'strategy': pos.get('strategy'),
                 'mark_age_s': int((now - mark_at).total_seconds()),
                 'threshold_s': STALE_MARK_S}))
        # (d) S7 position whose last mark is at/below its active stop
        # but has no exit decision. Active stop = 0.90 * high_water
        # (strategy_rules.json S7). An EXIT decision recorded at or
        # after the mark time means the breach is being handled.
        if pos.get('strategy') == 7:
            mark = pos.get('mark_price')
            high_water = pos.get('high_water')
            if mark is not None and high_water:
                stop = S7_STOP_FACTOR * high_water
                if mark <= stop:
                    exits = s7_exits.get(symbol) or []
                    handled = any(
                        t is not None and mark_at is not None
                        and t >= mark_at for t in exits)
                    if not handled:
                        findings.append(_finding(
                            'ERROR', 's7_stop_breached_no_exit', symbol,
                            {'mark_price': mark, 'stop': stop,
                             'high_water': high_water}))

    # (e) negative cash/equity (today) and fills without a decision.
    for row in snapshot.get('negative_equity') or []:
        findings.append(_finding(
            'CRITICAL', 'negative_cash_equity', None,
            {'strategy': row.get('strategy'), 'day': row.get('day'),
             'equity_paise': row.get('equity_paise')}))
    for fill in snapshot.get('fills_without_decision') or []:
        findings.append(_finding(
            'ERROR', 'fill_without_decision', fill.get('symbol'),
            {'signal_id': fill.get('signal_id'),
             'strategy': fill.get('strategy'),
             'filled_at': fill.get('filled_at')}))
    return findings


# -- fetching -----------------------------------------------------------
def fetch_snapshot(store, namespace_configured, namespace_effective,
                   config, health_snapshot, now):
    """Fetch one cycle's snapshot from the store (SELECT only).

    Bounded queries only (LIMIT, current-day namespace); at most 5
    SELECTs per cycle. Returns (snapshot, reads) where reads is the
    number of SELECTs issued. A failed query is recorded in
    snapshot['fetch_errors'] (type only, never the message, which
    could repeat a connection string) and the cycle continues; the
    function never raises for a store error.
    """
    today = now.date().isoformat()
    snapshot = {
        'now_ist': now, 'in_session': in_monitor_window(config, now),
        'namespace_configured': namespace_configured,
        'namespace_effective': namespace_effective,
        'heartbeat_age_s': None, 'health_ok': None, 'quote_probe': None,
        'positions': [], 'negative_equity': [],
        'fills_without_decision': [], 's7_exit_decisions': [],
        'fetch_errors': [],
    }
    reads = 0

    # In-process health (no store read): ok flag and quote probe.
    if isinstance(health_snapshot, dict):
        snapshot['health_ok'] = health_snapshot.get('ok')
        snapshot['quote_probe'] = health_snapshot.get('quote_probe')

    def _select(label, sql, args):
        nonlocal reads
        try:
            reads += 1
            return store.execute(sql, args)
        except Exception as ex:
            snapshot['fetch_errors'].append(
                {'query': label, 'error': type(ex).__name__})
            return None

    # (a) worker heartbeat (paper_monitor_health, one row/namespace).
    rows = _select(
        'heartbeat',
        'SELECT heartbeat_at, status FROM paper_monitor_health'
        ' WHERE namespace=? LIMIT 1',
        (namespace_effective,))
    if rows:
        hb = _parse_ts(rows[0].get('heartbeat_at'))
        if hb is not None:
            snapshot['heartbeat_age_s'] = int(
                (now - hb).total_seconds())
        else:
            snapshot['fetch_errors'].append(
                {'query': 'heartbeat', 'error': 'heartbeat_unparseable'})

    # (c)(d)(e) open positions (one bounded SELECT; the checks filter
    # in Python). All rows for the namespace; the book holds at most
    # 7 strategies x 9 slots open plus closed qty=0 rows.
    rows = _select(
        'positions',
        'SELECT strategy, symbol, qty, mark_price, mark_at,'
        ' high_water, updated_at FROM paper_positions'
        ' WHERE namespace=? LIMIT %d' % MAX_ROWS_PER_QUERY,
        (namespace_effective,))
    if rows is not None:
        snapshot['positions'] = rows

    # (e) negative cash/equity today (equity_paise < 0).
    rows = _select(
        'negative_equity',
        'SELECT strategy, day, equity_paise FROM paper_equity_closes'
        ' WHERE namespace=? AND day=? AND equity_paise < 0 LIMIT 10',
        (namespace_effective, today))
    if rows is not None:
        snapshot['negative_equity'] = rows

    # (e) fills today with no matching ENTRY decision.
    rows = _select(
        'fills_without_decision',
        'SELECT f.signal_id, f.strategy, f.symbol, f.filled_at'
        ' FROM paper_fills f'
        ' LEFT JOIN paper_decisions d'
        ' ON d.namespace = f.namespace'
        ' AND d.signal_id = f.signal_id'
        " AND d.decision_type = 'ENTRY'"
        ' WHERE f.namespace=? AND substr(f.filled_at, 1, 10)=?'
        ' AND d.signal_id IS NULL LIMIT 10',
        (namespace_effective, today))
    if rows is not None:
        snapshot['fills_without_decision'] = rows

    # (d) S7 EXIT decisions recorded today.
    rows = _select(
        's7_exit_decisions',
        'SELECT strategy, symbol, recorded_at FROM paper_decisions'
        " WHERE namespace=? AND strategy=7 AND decision_type='EXIT'"
        ' AND substr(recorded_at, 1, 10)=? LIMIT 50',
        (namespace_effective, today))
    if rows is not None:
        snapshot['s7_exit_decisions'] = rows

    return snapshot, reads


# -- findings sink ------------------------------------------------------
def ensure_findings_table(store):
    """CREATE TABLE IF NOT EXISTS support_monitor_findings.

    The only DDL the monitor issues, and the only table it ever
    writes. Append-only: (ts, severity, code, detail_json, namespace).
    """
    store.execute(
        'CREATE TABLE IF NOT EXISTS support_monitor_findings('
        ' ts TEXT NOT NULL,'
        ' severity TEXT NOT NULL,'
        ' code TEXT NOT NULL,'
        ' detail_json TEXT NOT NULL,'
        ' namespace TEXT NOT NULL'
        ')', ())
    return True


def sink_findings(store, namespace, findings, now, dedup):
    """Append findings and emit one structured log line per finding.

    De-duplicates the same (code, symbol) within 30 minutes using the
    in-memory `dedup` dict {(code, symbol): last_ts}. Returns the
    number of rows written. detail_json carries only the finding's
    detail (symbols, numbers, codes) and no sensitive values.
    """
    written = 0
    for f in findings:
        key = (f['code'], f.get('symbol'))
        last = dedup.get(key)
        if last is not None and (now - last).total_seconds() < DEDUP_WINDOW_S:
            continue
        detail_json = json.dumps(f['detail'], sort_keys=True)
        store.execute(
            'INSERT INTO support_monitor_findings'
            ' (ts, severity, code, detail_json, namespace)'
            ' VALUES (?, ?, ?, ?, ?)',
            (now.isoformat(), f['severity'], f['code'], detail_json,
             namespace))
        dedup[key] = now
        written += 1
        print(json.dumps({
            'event': 'support_monitor_finding',
            'ts': now.isoformat(), 'severity': f['severity'],
            'code': f['code'], 'symbol': f.get('symbol'),
            'namespace': namespace, 'detail': f['detail'],
        }, sort_keys=True), flush=True)
    return written


# -- runner -------------------------------------------------------------
class Monitor:
    """The monitor runner: one cycle per interval, in its own thread.

    Holds the mutable state (reads_total, dedup, backoff) and the
    /health state dict. A cycle never raises: every failure is caught
    and recorded as last_error, and the next sleep backs off. The
    thread never touches the worker.
    """

    def __init__(self, store, namespace, config, health_snapshot_fn,
                 now_fn=None, state_fn=None):
        self.store = store
        self.namespace = namespace
        # Configured namespace (config.json) for the mismatch check.
        self.namespace_configured = config.get('namespace')
        self.config = config
        self.health_snapshot_fn = health_snapshot_fn
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.state_fn = state_fn
        self.enabled = monitor_enabled()
        self.interval_s = monitor_interval_seconds()
        self.reads_total = 0
        self.last_run_at = None
        self.last_findings_count = 0
        self.last_error = None
        self._dedup = {}
        self._backoff = 0
        self._table_ensured = False

    def state(self):
        """The /health support_monitor dict (replaced wholesale)."""
        return {'enabled': self.enabled,
                'last_run_at': (
                    self.last_run_at.isoformat()
                    if self.last_run_at is not None else None),
                'last_findings_count': self.last_findings_count,
                'reads_total': self.reads_total,
                'last_error': self.last_error}

    def _publish(self):
        if self.state_fn is not None:
            try:
                self.state_fn(self.state())
            except Exception:
                pass

    def cycle(self, now=None):
        """Run one cycle. Returns the state dict. Never raises."""
        if not self.enabled:
            return self.state()
        now = now or self.now_fn()
        try:
            if not in_monitor_window(self.config, now):
                return self.state()
            if not self._table_ensured:
                try:
                    ensure_findings_table(self.store)
                    self._table_ensured = True
                except Exception as ex:
                    self.last_error = type(ex).__name__
                    self._backoff += 1
                    self._publish()
                    return self.state()
            health = None
            try:
                health = self.health_snapshot_fn()
            except Exception:
                health = None
            snapshot, reads = fetch_snapshot(
                self.store, self.namespace_configured, self.namespace,
                self.config, health, now)
            self.reads_total += reads
            findings = check(snapshot)
            try:
                sink_findings(self.store, self.namespace, findings,
                              now, self._dedup)
            except Exception as ex:
                self.last_error = type(ex).__name__
                self._backoff += 1
                self._publish()
                return self.state()
            self.last_run_at = now
            self.last_findings_count = len(findings)
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


def start_monitor_thread(store, namespace, config, health_snapshot_fn,
                         state_fn, stop_fn):
    """Start the monitor in a daemon thread; returns (Monitor, Thread)
    or (None, None) when disabled. The caller owns `stop_fn`."""
    if not monitor_enabled():
        return None, None
    monitor = Monitor(store, namespace, config, health_snapshot_fn,
                      state_fn=state_fn)
    thread = threading.Thread(
        target=monitor.serve_forever, args=(stop_fn,), daemon=True)
    thread.start()
    return monitor, thread
