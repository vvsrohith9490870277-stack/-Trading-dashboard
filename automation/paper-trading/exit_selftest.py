"""Isolated paper exit self-test. Uses the production monitor/fill path in a private namespace.

Never writes paper_v1. Never takes engine.lock. Never sends broker orders.
"""
import fcntl, hashlib, json, time
from datetime import datetime, timedelta
from pathlib import Path

TEST_NS = 'paper_exit_test_v1'
PROTECTED = frozenset({'paper_v1'})
STOP_SYMBOL = 'RELIANCE-EQ'
SELL_SYMBOL = 'HDFCBANK-EQ'
TIGHT = 0.0015  # 0.15%


def _require_test_ns(ns):
    if ns in PROTECTED or not str(ns).startswith('paper_exit_test'):
        raise ValueError('protected_or_invalid_test_namespace')
    return ns


EXTRA_NAMESPACED_TABLES = (
    'paper_events',
    'adaptive_eod_markers',
    'adaptive_parity',
    'support_episodes',
    'support_detector_runs',
    'support_exports',
    'support_heartbeats',
)

def namespaced_tables(e, db=None):
    rows = []
    for line in (e.ROOT / 'schema.sql').read_text().split(';'):
        line = line.strip()
        if not line.lower().startswith('create table'):
            continue
        if 'namespace' not in line.lower():
            continue
        name = line.split('(')[0].split()[-1]
        if name and name not in rows:
            rows.append(name)
    for name in EXTRA_NAMESPACED_TABLES:
        if name not in rows:
            rows.append(name)
    if db is not None:
        try:
            present = {r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
            rows = [name for name in rows if name in present]
        except Exception:
            pass
    return rows


def snapshot_ns(db, ns):
    def count(sql, args=()):
        return db.query(sql, args)[0]
    out = {
        'namespace': ns,
        'positions': db.query('SELECT strategy,symbol,qty FROM paper_positions WHERE namespace=? AND qty>0 ORDER BY strategy,symbol', (ns,)),
        'fills': count('SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS max_id FROM paper_fills WHERE namespace=?', (ns,)),
        'events': count('SELECT COUNT(*) AS n FROM paper_events WHERE namespace=?', (ns,)),
        'signals_pending': count("SELECT COUNT(*) AS n FROM paper_signals WHERE namespace=? AND status='pending'", (ns,)),
        'runs': count('SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS max_id FROM paper_runs WHERE namespace=?', (ns,)),
    }
    return out


def cleanup(e, db, ns):
    _require_test_ns(ns)
    tables = namespaced_tables(e, db)
    if tables:
        db.batch([('DELETE FROM ' + table + ' WHERE namespace=?', (ns,)) for table in tables])
    suffix = hashlib.sha256((str(getattr(db, 'test_database', None) or 'turso') + '|' + ns).encode()).hexdigest()[:16]
    spool = e.ROOT / 'logs' / 'audit-spool' / suffix
    if spool.is_dir():
        for p in spool.glob('*.jsonl'):
            p.unlink()
    return {'removed': True, 'namespace': ns}


def high_water_for_level(e, level):
    trail = e.TRAIL
    hw = round(float(level) / trail, 2)
    for adj in (0, 0.01, -0.01, 0.02, -0.02, 0.03, -0.03):
        cand = round(hw + adj, 2)
        if e.trail_level(cand) == round(float(level), 2):
            return cand
    return hw


def choose_s7_level(quote, tight=TIGHT):
    """Prefer a trail_touch level (day low <= L < official open). Else a trail_gap level (>= open)."""
    last = e_positive(quote.get('price'))
    official = e_positive(quote.get('open'))
    low = e_positive(quote.get('low'))
    if last is None:
        raise RuntimeError('missing_live_price')
    tight_level = round(last * (1 - tight), 2)
    if official is not None and low is not None and low < official:
        lo, hi = low, official
        if lo <= tight_level < hi:
            return tight_level, 'trail_touch'
        mid = round((lo + hi) / 2, 2)
        if lo <= mid < hi:
            return mid, 'trail_touch'
    # Fall back to a level that the session open has already crossed.
    level = max(x for x in (official, last, tight_level) if x is not None)
    return round(level, 2), 'trail_gap'


def e_positive(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def _budget_ok(snap, max_quotes):
    limits = (snap or {}).get('limits') or {}
    reserve = int(limits.get('reserve_for_session_monitor') or 1500)
    remaining = int((snap or {}).get('remaining_today') or 0)
    if remaining < reserve + int(max_quotes):
        return False, {'remaining_today': remaining, 'reserve': reserve, 'max_quotes': max_quotes}
    return True, {'remaining_today': remaining, 'reserve': reserve, 'max_quotes': max_quotes}


def _seed_s7_stop(e, db, ns, quote, at):
    level, expected_rule = choose_s7_level(quote)
    hw = high_water_for_level(e, level)
    actual_level = e.trail_level(hw)
    px = float(quote['price'])
    db.batch([('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (ns, 7, STOP_SYMBOL, 1, px, at, hw, px, quote.get('quote_at') or at, at))])
    return {'symbol': STOP_SYMBOL, 'strategy': 7, 'entry_price': px, 'high_water': hw, 'level': actual_level, 'expected_rule': expected_rule, 'qty': 1}


def _seed_pending_sell(e, db, ns, quote, at):
    px = float(quote['price'])
    db.batch([('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (ns, 1, SELL_SYMBOL, 1, px, at, px, px, quote.get('quote_at') or at, at))])
    sid = e.stable_id(1, SELL_SYMBOL, at, 'daily_exit_selftest')
    db.batch([('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (ns, sid, 1, SELL_SYMBOL, at[:10], 'SELL', 'pending', 'daily_exit:' + at, at, at[:10]))])
    return {'symbol': SELL_SYMBOL, 'strategy': 1, 'entry_price': px, 'qty': 1, 'signal_id': sid}


def _sell_fill(db, ns, symbol):
    rows = db.query("SELECT * FROM paper_fills WHERE namespace=? AND symbol=? AND side='SELL' ORDER BY id", (ns, symbol))
    return rows[-1] if rows else None


def _exit_decision(db, ns, symbol):
    rows = db.query("SELECT rule,execution_path,payload_json FROM paper_decisions WHERE namespace=? AND symbol=? AND decision_type='EXIT' ORDER BY seq", (ns, symbol))
    return rows[-1] if rows else None


def _case(name, **kw):
    row = {'name': name, 'passed': False}
    row.update(kw)
    return row


def run(e, db, api, max_seconds=1200, max_quotes=40, poll_seconds=20, cleanup_after=True, live=True, quotes_seed=None, stale_first=False, later_quotes=None, keep=False):
    ns = _require_test_ns(TEST_NS)
    lock_path = e.ROOT / 'exit_selftest.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'ok', 'namespace': ns, 'paper_only': True, 'cases': [], 'fyers_requests_used': 0}
    previous_ns = e.NS
    lock_fh = lock_path.open('a+')
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        return {'status': 'another_exit_selftest_running', 'namespace': ns, 'paper_only': True, 'cases': []}
    try:
        snap0 = api.budget_snapshot() if hasattr(api, 'budget_snapshot') else {}
        ok, budget = _budget_ok(snap0, max_quotes)
        report['budget_before'] = snap0
        if not ok:
            report['status'] = 'budget_reserve_would_be_breached'
            report['budget_gate'] = budget
            return report
        v1_before = snapshot_ns(db, 'paper_v1')
        report['paper_v1_before'] = v1_before
        e.NS = ns
        e.init(db)
        t0 = e.now()
        if live:
            quotes = api.quotes([STOP_SYMBOL, SELL_SYMBOL])
        else:
            if not quotes_seed:
                raise RuntimeError('quotes_seed_required')
            quotes = {k: dict(v) for k, v in quotes_seed.items()}
        at = t0.isoformat()
        if not quotes.get(STOP_SYMBOL) or e_positive(quotes[STOP_SYMBOL].get('price')) is None:
            report['status'] = 'missing_live_quote'
            return report
        if not quotes.get(SELL_SYMBOL) or e_positive(quotes[SELL_SYMBOL].get('price')) is None:
            report['status'] = 'missing_live_quote'
            return report
        s7 = _seed_s7_stop(e, db, ns, quotes[STOP_SYMBOL], at)
        sell = _seed_pending_sell(e, db, ns, quotes[SELL_SYMBOL], at)
        report['seeds'] = {'s7_trail_stop': s7, 'pending_sell_exit': sell}

        stale_case = _case('stale_quote_ignored', skipped=not stale_first, passed=not stale_first)
        if not stale_first:
            stale_case['reason'] = 'not_requested'
        if stale_first:
            stale_q = dict(quotes[SELL_SYMBOL])
            # If the seed quote is already stale, use it; otherwise age it.
            qt = stale_q.get('quote_at')
            if qt:
                aged = datetime.fromisoformat(qt)
                if (t0 - aged).total_seconds() <= e.CFG['quote_max_age_seconds']:
                    stale_q['quote_at'] = (t0 - timedelta(seconds=e.CFG['quote_max_age_seconds'] + 30)).isoformat()
            sig = db.query('SELECT * FROM paper_signals WHERE namespace=? AND id=?', (ns, sell['signal_id']))[0]
            r = e.fill(db, sig, stale_q, t0)
            stale_case['skipped'] = (not r.get('filled')) and r.get('reason') == 'stale_or_future_quote'
            stale_case['reason'] = r.get('reason')
            stale_case['passed'] = bool(stale_case['skipped']) and bool(db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0', (ns, SELL_SYMBOL)))
            if later_quotes and hasattr(api, '_quotes'):
                api._quotes = {k: dict(v) for k, v in later_quotes.items()}
        report['cases'].append(stale_case)

        deadline = time.monotonic() + max(0, int(max_seconds))
        polls = 0
        last_monitor = None
        while polls < int(max_quotes) and time.monotonic() <= deadline:
            last_monitor = e.monitor(db, api)
            polls += 1
            if _sell_fill(db, ns, STOP_SYMBOL) and _sell_fill(db, ns, SELL_SYMBOL):
                break
            if poll_seconds:
                time.sleep(float(poll_seconds))
            if not live:
                break  # unit tests use one (or already-updated) snapshot
        report['polls'] = polls
        report['last_monitor'] = {k: last_monitor.get(k) for k in ('status', 'fills', 'fresh_quotes', 'at_IST') if last_monitor}

        report['cases'].append(_evaluate_s7(e, db, ns, s7, quotes[STOP_SYMBOL]))
        report['cases'].append(_evaluate_pending_sell(e, db, ns, sell, quotes.get(SELL_SYMBOL) if live else (later_quotes or quotes).get(SELL_SYMBOL)))

        # Second monitor must not create a duplicate SELL fill.
        sells_before = len(db.query("SELECT id FROM paper_fills WHERE namespace=? AND side='SELL'", (ns,)))
        if polls < int(max_quotes):
            e.monitor(db, api)
            polls += 1
        sells_after = len(db.query("SELECT id FROM paper_fills WHERE namespace=? AND side='SELL'", (ns,)))
        report['polls'] = polls
        report['cases'].append(_case('no_duplicate_exit', passed=(sells_after == sells_before), sells_before=sells_before, sells_after=sells_after))

        snap1 = api.budget_snapshot() if hasattr(api, 'budget_snapshot') else {}
        report['budget_after'] = snap1
        report['fyers_requests_used'] = int(snap1.get('requests') or 0) - int(snap0.get('requests') or 0)
        report['cases_passed'] = sum(1 for c in report['cases'] if c.get('passed'))
        report['cases_total'] = len(report['cases'])
        if report['cases_total'] and report['cases_passed'] == report['cases_total']:
            report['status'] = 'ok'
        else:
            report['status'] = 'failed' if report['status'] == 'ok' else report['status']

        if cleanup_after and not keep:
            cleanup(e, db, ns)
            report['cleanup'] = {'removed': True, 'namespace': ns}
        else:
            report['cleanup'] = {'removed': False, 'namespace': ns}
        e.NS = previous_ns
        report['paper_v1_after'] = snapshot_ns(db, 'paper_v1')
        report['paper_v1_positions_unchanged'] = report['paper_v1_after']['positions'] == v1_before['positions']
        return report
    except Exception as ex:
        report['status'] = 'failed'
        report['error'] = e.au.safe_error(ex)
        try:
            if cleanup_after and not keep:
                cleanup(e, db, ns)
                report['cleanup'] = {'removed': True, 'namespace': ns, 'after_error': True}
        except Exception:
            pass
        return report
    finally:
        e.NS = previous_ns
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def _evaluate_s7(e, db, ns, seed, quote):
    fill = _sell_fill(db, ns, STOP_SYMBOL)
    dec = _exit_decision(db, ns, STOP_SYMBOL)
    payload = {}
    if dec and dec.get('payload_json'):
        try:
            payload = json.loads(dec['payload_json'])
        except Exception:
            payload = {}
    case = _case('s7_trail_stop', symbol=STOP_SYMBOL, strategy=7, entry_price=seed['entry_price'],
                 level=seed['level'], high_water=seed['high_water'], expected_rule=seed['expected_rule'],
                 quote_last=quote.get('price'), quote_open=quote.get('open'), quote_low=quote.get('low'),
                 quote_at=quote.get('quote_at'))
    if not fill or not dec:
        case['reason'] = 'not_triggered'
        return case
    case['exit_price'] = fill['price']
    case['filled_at'] = fill['filled_at']
    case['fill_quote_at'] = fill['quote_at']
    case['execution_path'] = fill['execution_path']
    case['rule'] = dec.get('rule')
    case['reference_price'] = payload.get('reference_price')
    case['qty'] = fill['qty']
    case['passed'] = (
        fill['qty'] == 1
        and dec.get('rule') in ('trail_touch', 'trail_gap')
        and fill['execution_path'] in ('LTP_LEGACY', 'FEED_OPEN')
        and case['reference_price'] is not None
        and not db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND symbol=? AND strategy=7 AND qty>0', (ns, STOP_SYMBOL))
    )
    if dec.get('rule') == 'trail_touch':
        case['passed'] = bool(case['passed'] and fill['execution_path'] == 'LTP_LEGACY' and abs(float(case['reference_price']) - float(seed['level'])) < 0.005)
    if dec.get('rule') == 'trail_gap':
        case['passed'] = bool(case['passed'] and fill['execution_path'] == 'FEED_OPEN')
    return case


def _evaluate_pending_sell(e, db, ns, seed, quote):
    fill = _sell_fill(db, ns, SELL_SYMBOL)
    case = _case('pending_sell_exit', symbol=SELL_SYMBOL, strategy=1, entry_price=seed['entry_price'],
                 quote_last=(quote or {}).get('price'), quote_open=(quote or {}).get('open'),
                 quote_at=(quote or {}).get('quote_at'))
    if not fill:
        case['reason'] = 'not_triggered'
        return case
    case['exit_price'] = fill['price']
    case['filled_at'] = fill['filled_at']
    case['fill_quote_at'] = fill['quote_at']
    case['execution_path'] = fill['execution_path']
    # Production non-stop exit uses official open when present (FEED_OPEN).
    case['reference_price'] = (quote or {}).get('open')
    case['qty'] = fill['qty']
    case['passed'] = (
        fill['qty'] == 1
        and fill['execution_path'] == 'FEED_OPEN'
        and not db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND symbol=? AND strategy=1 AND qty>0', (ns, SELL_SYMBOL))
    )
    return case


def cli(e, db, args):
    if getattr(args, 'cleanup_only', False):
        previous = e.NS
        try:
            e.NS = TEST_NS
            cleanup(e, db, TEST_NS)
        finally:
            e.NS = previous
        v1 = snapshot_ns(db, 'paper_v1')
        return {'status': 'cleaned', 'namespace': TEST_NS, 'paper_only': True, 'paper_v1': v1}
    api = e.MarketData()
    return run(e, db, api,
               max_seconds=getattr(args, 'max_seconds', 1200) or 1200,
               max_quotes=getattr(args, 'max_quotes', 40) or 40,
               poll_seconds=25,
               cleanup_after=not getattr(args, 'keep', False),
               live=True,
               stale_first=True,
               keep=getattr(args, 'keep', False))
