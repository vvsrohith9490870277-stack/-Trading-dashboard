"""Findings-only independent verification stage for the Support Agent.

Each detector finding is re-checked by at least two deterministic verifiers that
re-derive the condition from raw sources with different code paths:

  events_sql  — SQL over the local snapshot (paper_events / paper_fills / paper_signals)
  log_files   — a separate walk of engine.jsonl and audit-YYYY-MM-DD.jsonl

Neither path calls run_v3_production_detectors or the emit/record_episode helpers.
This module is read-only against Track A: it never writes Turso, paper-trading files,
patches, or installs. It may record verifier rows in the local findings database and
write paste-ready review prompts under the report directory.

Verdicts per verifier: CONFIRM, REJECT, NOT_VERIFIABLE.
Aggregate: confirmed (>=2 CONFIRM, 0 REJECT), disputed (CONFIRM and REJECT),
rejected (>=2 REJECT, 0 CONFIRM), unconfirmed (otherwise).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICY = json.loads((ROOT / 'support_policy.json').read_text(encoding='utf8'))
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
CAP = json.loads((ROOT / 'capital_policy.json').read_text(encoding='utf8'))

IST = timezone(timedelta(minutes=int(POLICY['utc_offset_minutes'])), name=POLICY['timezone'])
VERIFIER_IDS = ('events_sql', 'log_files')

SCHEMA = """
CREATE TABLE IF NOT EXISTS support_finding_verifications(
  namespace TEXT NOT NULL,
  finding_id TEXT NOT NULL,
  verifier_id TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN('CONFIRM','REJECT','NOT_VERIFIABLE')),
  evidence_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(namespace, finding_id, verifier_id)
)
"""


def init(db):
    """db is a SupportStore or any object with .batch / .query; also accept a sqlite path/connection."""
    if hasattr(db, 'batch'):
        db.batch([(SCHEMA, ())])
        return db
    if isinstance(db, (str, Path)):
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        c.execute(SCHEMA)
        c.commit()
        return c
    db.execute(SCHEMA)
    db.commit()
    return db


def _ist(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # 'YYYY-MM-DD HH:MM:SS' or 'HH:MM:SS' is not used as a session key here
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _hhmm(spec):
    h, m = [int(x) for x in str(spec).split(':')[:2]]
    return h, m


def _session_dt(session_day, hhmm, extra_minutes=0, extra_seconds=0):
    h, m = _hhmm(hhmm)
    return datetime(
        int(session_day[:4]), int(session_day[5:7]), int(session_day[8:10]),
        h, m, tzinfo=IST,
    ) + timedelta(minutes=int(extra_minutes), seconds=int(extra_seconds))


def _load_json(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _details(finding):
    d = finding.get('details') or finding.get('details_json') or {}
    return _load_json(d)


def _connect_snapshot(snap):
    if hasattr(snap, 'query'):
        return snap
    c = sqlite3.connect(f'file:{Path(snap).resolve()}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    class Q:
        def query(self, sql, args=()):
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        def close(self):
            c.close()
    return Q()


def _tables(snap):
    try:
        rows = snap.query("SELECT name FROM sqlite_master WHERE type='table'")
        return {r['name'] for r in rows}
    except Exception:
        return set()


def query_events(snap, namespace, session_day, types=None):
    tables = _tables(snap)
    if 'paper_events' not in tables:
        return []
    sql = 'SELECT event_id, event_type, source_at, recorded_at, data_json, signal_id FROM paper_events WHERE namespace=? AND session_day=?'
    args = [namespace, session_day]
    if types:
        sql += f" AND event_type IN ({','.join('?' * len(types))})"
        args.extend(types)
    sql += ' ORDER BY source_at, recorded_at'
    return snap.query(sql, tuple(args))


def query_fills(snap, namespace, session_day=None):
    tables = _tables(snap)
    if 'paper_fills' not in tables:
        return []
    # filled_at may be ISO; filter in Python for session_day to avoid dialect issues
    rows = snap.query('SELECT * FROM paper_fills WHERE namespace=?', (namespace,))
    if not session_day:
        return rows
    out = []
    for r in rows:
        at = r.get('filled_at') or r.get('source_at') or ''
        if str(at)[:10] == session_day or str(r.get('session_day') or '') == session_day:
            out.append(r)
    return out


def query_signals(snap, namespace):
    tables = _tables(snap)
    if 'paper_signals' not in tables:
        return []
    return snap.query('SELECT * FROM paper_signals WHERE namespace=?', (namespace,))


def iter_jsonl(path):
    path = Path(path) if path else None
    if not path or not path.exists():
        return
    with path.open(encoding='utf8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            # Drop credential-like keys; this is only for verification evidence.
            yield {k: v for k, v in ev.items()
                   if not any(s in k.lower() for s in ('token', 'secret', 'password', 'authorization', 'cookie', 'api_key'))}


def log_day(ev, session_day):
    at = ev.get('at_IST') or ev.get('source_at') or ev.get('at') or ev.get('recorded_at') or ev.get('filled_at')
    if not at:
        return False
    try:
        return _ist(at).strftime('%Y-%m-%d') == session_day
    except Exception:
        return str(at)[:10] == session_day


def load_log_events(engine_path, audit_path, session_day, namespace=None):
    """Independent parser (not support_runner.engine_log_events)."""
    out = []
    for path, source in ((engine_path, 'engine.jsonl'), (audit_path, 'audit.jsonl')):
        for ev in iter_jsonl(path) or []:
            if not log_day(ev, session_day):
                continue
            ns = ev.get('namespace')
            if namespace and ns and ns != namespace and not str(ns).startswith('paper_exit_test'):
                continue
            ev = dict(ev)
            ev['_source_file'] = source
            ev['_kind'] = ev.get('kind') or ev.get('event_type') or ev.get('action') or ev.get('command') or ''
            ev['_at'] = ev.get('at_IST') or ev.get('source_at') or ev.get('at')
            out.append(ev)
    return out


def _feed_403_from_payload(payload):
    """Different walk from the detector helper that lists 403 hits: walk dicts/lists for http_403 / HTTP 403."""
    hits = []

    def walk(obj, feed=None):
        if isinstance(obj, dict):
            name = obj.get('feed') or obj.get('name') or obj.get('source') or feed
            err = obj.get('error') or obj.get('status') or obj.get('http_status') or obj.get('code')
            text = json.dumps(obj, default=str)
            is_403 = False
            if err is not None and '403' in str(err).lower().replace(' ', '_'):
                is_403 = True
            if obj.get('http_status') == 403 or obj.get('status_code') == 403 or obj.get('code') == 403:
                is_403 = True
            if is_403:
                hits.append({'feed': name or 'nse', 'error': err})
            # sources map used by reference_feeds engine logs
            sources = obj.get('sources')
            if isinstance(sources, dict):
                for k, v in sources.items():
                    walk(v, feed=k)
            else:
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        walk(v, feed=name)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, feed=feed)
        elif isinstance(obj, str) and '403' in obj:
            hits.append({'feed': feed or 'nse', 'error': obj})

    walk(payload)
    # dedup by feed
    seen, uniq = set(), []
    for h in hits:
        k = str(h.get('feed'))
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


def _kind(ev):
    return str(ev.get('event_type') or ev.get('_kind') or ev.get('kind') or ev.get('command') or '').lower()


def _is_scan_completed(ev):
    k = _kind(ev)
    if k in ('scan_completed',):
        return True
    if k in ('command_completed', 'engine_command_completed') and str(ev.get('command') or ev.get('action') or '').lower() == 'scan':
        outcome = str(ev.get('outcome') or ev.get('status') or 'ok').lower()
        return outcome in ('ok', 'success', 'completed', '')
    if k == 'scan' and str(ev.get('outcome') or ev.get('status') or '').lower() in ('ok', 'success', 'completed'):
        return True
    return False


def _is_scan_failed(ev):
    k = _kind(ev)
    if k in ('scan_failed',):
        return True
    if k in ('command_completed', 'command_failed') and str(ev.get('command') or '').lower() == 'scan':
        outcome = str(ev.get('outcome') or ev.get('status') or '').lower()
        return outcome in ('error', 'failed', 'fail')
    return False


def _is_monitor(ev):
    k = _kind(ev)
    if k in ('monitor_completed', 'monitor_idle', 'monitor_check'):
        return True
    # engine.jsonl may log command=monitor / worker_heartbeat
    if k in ('command_completed',) and str(ev.get('command') or ev.get('action') or '').lower() in ('monitor', 'monitor_once', 'worker_check'):
        return True
    if k in ('worker_heartbeat', 'worker_check', 'heartbeat'):
        return True
    return False


def _is_eod_done(ev):
    k = _kind(ev)
    if k in ('eod_completed', 'eod_reconciled'):
        return True
    if 'eod' in k and any(s in k for s in ('complete', 'reconcil', 'done')):
        return True
    if k in ('command_completed',) and str(ev.get('command') or '').lower() in ('eod', 'eod_reconciliation'):
        outcome = str(ev.get('outcome') or ev.get('status') or 'ok').lower()
        return outcome in ('ok', 'success', 'completed', '')
    return False


def _at(ev):
    return _ist(ev.get('source_at') or ev.get('_at') or ev.get('at_IST') or ev.get('at') or ev.get('recorded_at') or ev.get('filled_at'))


def _verdict(confirm, reject_reason=None, not_verifiable=None, evidence=None):
    if not_verifiable:
        return {'verdict': 'NOT_VERIFIABLE', 'evidence': evidence or {'reason': not_verifiable}}
    if confirm:
        return {'verdict': 'CONFIRM', 'evidence': evidence or {}}
    return {'verdict': 'REJECT', 'evidence': evidence or ({'reason': reject_reason} if reject_reason else {})}


# --------------- per-detector re-derivation ---------------

def _scan_missed_from_times(completed_before, failed_before, now_ist, session_day, started_and_finished=None):
    exp = POLICY['expectations']['premarket_scan']
    deadline = _session_dt(session_day, exp['expected_at'], extra_minutes=exp['grace_minutes'])
    ev = {
        'deadline_at': deadline.isoformat(),
        'completed_on_time': len(completed_before),
        'failed_on_time': len(failed_before),
        'started_and_finished_before_open': len(started_and_finished or []),
    }
    if now_ist < deadline:
        return _verdict(False, reject_reason='deadline_not_reached', evidence=ev)
    scan_ok = bool(completed_before) or bool(started_and_finished)
    warranted = (not scan_ok) or (bool(failed_before) and not scan_ok)
    return _verdict(warranted, reject_reason='scan_completed_on_time' if completed_before else 'scan_started_on_time_and_finished', evidence=ev)


def check_scan_missed_events(snap, finding, namespace, session_day, now_ist):
    rows = query_events(snap, namespace, session_day, types=('scan_completed', 'scan_failed', 'scan', 'scan_started'))
    exp = POLICY['expectations']['premarket_scan']
    deadline = _session_dt(session_day, exp['expected_at'], extra_minutes=exp['grace_minutes'])
    session_open = _session_dt(session_day, exp['expected_at'], extra_minutes=int(exp['grace_minutes']) + int(exp.get('completion_overrun_minutes') or 0))
    done, failed, started, finished = [], [], [], []
    for ev in rows:
        at = _at(ev)
        if at is None:
            continue
        if ev['event_type'] == 'scan_started' and at <= deadline:
            started.append(ev['source_at'])
        if ev['event_type'] == 'scan_completed' and at <= deadline:
            done.append(ev['source_at'])
        if ev['event_type'] == 'scan_failed' and at <= deadline:
            failed.append(ev['source_at'])
    if started:
        start_at = min(_at(ev) for ev in rows if ev['event_type'] == 'scan_started' and _at(ev) and _at(ev) <= deadline)
        finished = [ev['source_at'] for ev in rows if ev['event_type'] == 'scan_completed' and _at(ev) and start_at <= _at(ev) <= session_open]
    return _scan_missed_from_times(done, failed, now_ist, session_day, started_and_finished=finished)


def check_scan_missed_logs(logs, finding, session_day, now_ist):
    exp = POLICY['expectations']['premarket_scan']
    deadline = _session_dt(session_day, exp['expected_at'], extra_minutes=exp['grace_minutes'])
    scan_like = [ev for ev in logs if _is_scan_completed(ev) or _is_scan_failed(ev) or 'scan' in _kind(ev)]
    if not scan_like:
        return _verdict(False, not_verifiable='no_scan_events_in_logs')
    done, failed, started, finished = [], [], [], []
    session_open = _session_dt(session_day, exp['expected_at'], extra_minutes=int(exp['grace_minutes']) + int(exp.get('completion_overrun_minutes') or 0))
    for ev in scan_like:
        at = _at(ev)
        if at is None:
            continue
        kind = _kind(ev)
        if kind == 'scan_started' and at <= deadline:
            started.append(ev.get('_at') or ev.get('source_at') or ev.get('at_IST'))
        if at > deadline:
            continue
        if _is_scan_failed(ev):
            failed.append(ev.get('_at') or ev.get('source_at'))
        elif _is_scan_completed(ev):
            done.append(ev.get('_at') or ev.get('source_at'))
    if started:
        start_at = min(_at(ev) for ev in scan_like if _kind(ev) == 'scan_started' and _at(ev) and _at(ev) <= deadline)
        finished = [ev.get('_at') or ev.get('source_at') for ev in scan_like
                    if _is_scan_completed(ev) and _at(ev) and start_at <= _at(ev) <= session_open]
    return _scan_missed_from_times(done, failed, now_ist, session_day, started_and_finished=finished)


def _monitor_gaps(times, session_day, now_ist, stale_s, start, end, grace_s, live_times=None):
    times = sorted(t for t in times if t is not None)
    live = sorted(t for t in (live_times if live_times is not None else times) if t is not None)
    in_session = [t for t in times if start <= t <= end]
    ev = {
        'monitor_count': len(times),
        'in_session': len(in_session),
        'grace_seconds': grace_s,
        'stale_seconds': stale_s,
        'liveness_count': len(live),
    }
    if not live and not times:
        ev['initial_gap_seconds'] = None
        ev['warranted_worker_down'] = now_ist >= start + timedelta(seconds=grace_s)
        ev['warranted_staleness'] = ev['warranted_worker_down']
        ev['gaps'] = []
        return ev
    first = (live or times)[0]
    initial = (first - start).total_seconds()
    gaps = []
    seq = in_session if in_session else times
    for a, b in zip(seq, seq[1:]):
        gap = (b - a).total_seconds()
        if gap > stale_s:
            gaps.append({'from_ist': a.strftime('%H:%M:%S'), 'to_ist': b.strftime('%H:%M:%S'), 'gap_seconds': gap})
    last = times[-1]
    late_last = now_ist >= end and (end - last).total_seconds() > stale_s
    ev.update({
        'first_check': first.strftime('%H:%M:%S'),
        'initial_gap_seconds': initial,
        'max_gap_seconds': max([g['gap_seconds'] for g in gaps] or [0.0]),
        'gaps': gaps,
        'late_last': late_last,
        'warranted_worker_down': now_ist >= start + timedelta(seconds=grace_s) and (not live or initial > grace_s),
        'warranted_staleness': now_ist >= start + timedelta(seconds=grace_s) and (
            initial > grace_s or bool(gaps) or late_last
        ),
    })
    return ev


def check_worker_or_staleness_events(snap, finding, namespace, session_day, now_ist, kind):
    w = POLICY['expectations']['worker_active']
    start = _session_dt(session_day, w['start_at'])
    end = _session_dt(session_day, w['end_at'])
    stale_s = float(CFG['worker_stale_seconds'])
    rows = query_events(snap, namespace, session_day, types=('monitor_completed', 'monitor_idle', 'monitor_check'))
    live_rows = query_events(snap, namespace, session_day, types=('monitor_started', 'monitor_completed', 'monitor_idle', 'monitor_check', 'worker_check'))
    times = [_at(ev) for ev in rows]
    ev = _monitor_gaps(times, session_day, now_ist, stale_s, start, end, float(w['grace_seconds']), live_times=[_at(ev) for ev in live_rows])
    if kind == 'worker_down':
        return _verdict(ev['warranted_worker_down'], reject_reason='worker_started_within_grace', evidence=ev)
    return _verdict(ev['warranted_staleness'], reject_reason='no_stale_monitor_gap', evidence=ev)


def check_worker_or_staleness_logs(logs, finding, session_day, now_ist, kind):
    w = POLICY['expectations']['worker_active']
    start = _session_dt(session_day, w['start_at'])
    end = _session_dt(session_day, w['end_at'])
    stale_s = float(CFG['worker_stale_seconds'])
    mon = [ev for ev in logs if _is_monitor(ev)]
    live = mon + [ev for ev in logs if _kind(ev) in ('monitor_started',)]
    if not live:
        # engine.jsonl may simply not record every monitor tick — do not invent a down finding
        return _verdict(False, not_verifiable='no_monitor_or_heartbeat_events_in_logs')
    times = [_at(ev) for ev in mon]
    ev = _monitor_gaps(times, session_day, now_ist, stale_s, start, end, float(w['grace_seconds']), live_times=[_at(ev) for ev in live])
    ev['log_monitor_kinds'] = sorted({_kind(x) for x in mon})
    if kind == 'worker_down':
        return _verdict(ev['warranted_worker_down'], reject_reason='worker_started_within_grace', evidence=ev)
    return _verdict(ev['warranted_staleness'], reject_reason='no_stale_monitor_gap', evidence=ev)


def check_eod_events(snap, finding, namespace, session_day, now_ist):
    exp = POLICY['expectations']['eod_reconciliation']
    deadline = _session_dt(session_day, exp['expected_at'], extra_minutes=exp['grace_minutes'])
    rows = query_events(snap, namespace, session_day, types=('eod_completed', 'eod_reconciled'))
    ev = {'deadline_at': deadline.isoformat(), 'eod_events': [r['source_at'] for r in rows]}
    if now_ist < deadline:
        return _verdict(False, reject_reason='deadline_not_reached', evidence=ev)
    return _verdict(not rows, reject_reason='eod_event_present', evidence=ev)


def check_eod_logs(logs, finding, session_day, now_ist):
    exp = POLICY['expectations']['eod_reconciliation']
    deadline = _session_dt(session_day, exp['expected_at'], extra_minutes=exp['grace_minutes'])
    eod_like = [ev for ev in logs if _is_eod_done(ev) or 'eod' in _kind(ev)]
    if not eod_like and not any('eod' in json.dumps(ev, default=str).lower() for ev in logs):
        return _verdict(False, not_verifiable='no_eod_events_in_logs')
    done = [ev for ev in eod_like if _is_eod_done(ev)]
    ev = {'deadline_at': deadline.isoformat(), 'eod_done_count': len(done)}
    if now_ist < deadline:
        return _verdict(False, reject_reason='deadline_not_reached', evidence=ev)
    return _verdict(not done, reject_reason='eod_event_present_in_logs', evidence=ev)


def check_403_events(snap, finding, namespace, session_day, now_ist):
    rows = query_events(snap, namespace, session_day)
    wanted = str((_details(finding) or {}).get('feed') or '')
    hits = []
    for ev in rows:
        payload = _load_json(ev.get('data_json'))
        raw = ev.get('data_json') or ''
        if ev.get('event_type') in ('reference_feed_error', 'reference_feeds', 'reference_feed') or '403' in str(raw):
            for h in _feed_403_from_payload(payload) or ([{'feed': 'nse', 'error': '403'}] if '403' in str(raw) else []):
                hits.append(h)
    feeds = sorted({str(h.get('feed')) for h in hits})
    ev = {'feeds_with_403': feeds, 'hit_count': len(hits)}
    if wanted:
        ok = wanted in feeds or any(wanted in f for f in feeds)
        ev['wanted_feed'] = wanted
        if not hits:
            return _verdict(False, reject_reason='no_403_in_events', evidence=ev)
        return _verdict(ok, reject_reason='403_present_but_different_feed', evidence=ev)
    return _verdict(bool(hits), reject_reason='no_403_in_events', evidence=ev)


def check_403_logs(logs, finding, session_day, now_ist):
    wanted = str((_details(finding) or {}).get('feed') or '')
    hits = []
    for ev in logs:
        payload = ev
        hits.extend(_feed_403_from_payload(payload))
    feeds = sorted({str(h.get('feed')) for h in hits})
    ev = {'feeds_with_403': feeds, 'hit_count': len(hits)}
    if not logs:
        return _verdict(False, not_verifiable='log_files_missing_or_empty')
    if wanted:
        ok = wanted in feeds or any(wanted in f for f in feeds)
        ev['wanted_feed'] = wanted
        if not hits:
            return _verdict(False, reject_reason='no_403_in_logs', evidence=ev)
        return _verdict(ok, reject_reason='403_present_but_different_feed', evidence=ev)
    return _verdict(bool(hits), reject_reason='no_403_in_logs', evidence=ev)


def _expected_qty(price):
    target = int(CAP['target_notional_inr'])
    try:
        import capital
        return int(capital.integer_shares(price, target))
    except Exception:
        if not price:
            return 0
        return int(__import__('math').floor(target / float(price)))


def check_sizing_events(snap, finding, namespace, session_day, now_ist):
    fills = query_fills(snap, namespace, session_day)
    anomalies = []
    for f in fills:
        if str(f.get('side') or 'BUY').upper() != 'BUY':
            continue
        price = float(f.get('price') or 0.0)
        qty = int(f.get('qty') or f.get('quantity') or 0)
        if price <= 0:
            continue
        exp = _expected_qty(price)
        if qty != exp:
            anomalies.append({'symbol': f.get('symbol'), 'filled_qty': qty, 'expected_qty': exp, 'price': price})
    ev = {'buy_fills': sum(1 for f in fills if str(f.get('side') or 'BUY').upper() == 'BUY'), 'anomaly_count': len(anomalies), 'samples': anomalies[:12]}
    return _verdict(bool(anomalies), reject_reason='all_buy_qtys_match_target_notional', evidence=ev)


def check_sizing_logs(logs, finding, session_day, now_ist):
    fills = []
    for ev in logs:
        k = _kind(ev)
        if k in ('fill', 'paper_fill', 'order_filled') or ev.get('side') or ev.get('qty') or ev.get('quantity'):
            payload = ev.get('payload') if isinstance(ev.get('payload'), dict) else ev
            if str(payload.get('side') or ev.get('side') or 'BUY').upper() == 'SELL':
                continue
            price = float(payload.get('price') or payload.get('fill_price') or 0.0)
            qty = payload.get('qty') if payload.get('qty') is not None else payload.get('quantity')
            if price > 0 and qty is not None:
                fills.append({'symbol': payload.get('symbol'), 'price': price, 'qty': int(qty)})
    if not fills:
        return _verdict(False, not_verifiable='no_fill_records_in_logs')
    anomalies = []
    for f in fills:
        exp = _expected_qty(f['price'])
        if int(f['qty']) != exp:
            anomalies.append({**f, 'expected_qty': exp})
    ev = {'fill_like': len(fills), 'anomaly_count': len(anomalies), 'samples': anomalies[:12]}
    return _verdict(bool(anomalies), reject_reason='all_log_buy_qtys_match_target_notional', evidence=ev)


def check_exit_events(snap, finding, namespace, session_day, now_ist):
    w = POLICY['expectations']['worker_active']
    end = _session_dt(session_day, w['end_at'])
    if now_ist < end:
        return _verdict(False, reject_reason='session_not_closed_yet', evidence={'session_end': end.isoformat()})
    fills = query_fills(snap, namespace, session_day)
    sells = [f for f in fills if str(f.get('side') or '').upper() == 'SELL']
    stops = query_events(snap, namespace, session_day, types=('stop_hit', 'target_hit', 'trail_stop_filled'))
    selftest_rows = []
    try:
        selftest_rows.extend(snap.query(
            "SELECT * FROM paper_events WHERE namespace LIKE ? AND session_day=?",
            ('paper_exit_test%', session_day),
        ) or [])
    except Exception:
        pass
    try:
        for f in (snap.query("SELECT * FROM paper_fills WHERE namespace LIKE ?", ('paper_exit_test%',)) or []):
            if f.get('filled_at'):
                selftest_rows.append({**f, 'event_type': 'paper_fill', 'source_at': f['filled_at']})
    except Exception:
        pass
    from support_exit_evidence import exit_selftest_evidence
    selftest = exit_selftest_evidence(selftest_rows, session_day, now_ist)
    ev = {'sell_fills': len(sells), 'stop_or_target_hits': len(stops), 'exit_selftest': selftest}
    warranted = len(sells) == 0 and len(stops) == 0 and not selftest['exercised']
    return _verdict(warranted, reject_reason='sell_or_stop_present' if (sells or stops) else 'exit_selftest_exercised', evidence=ev)


def check_exit_logs(logs, finding, session_day, now_ist):
    w = POLICY['expectations']['worker_active']
    end = _session_dt(session_day, w['end_at'])
    if now_ist < end:
        return _verdict(False, reject_reason='session_not_closed_yet', evidence={'session_end': end.isoformat()})
    sells, stops = [], []
    for ev in logs:
        ns = ev.get('namespace')
        if ns and ns != finding.get('namespace') and not str(ns).startswith('paper_exit_test'):
            continue
        if str(ns or '').startswith('paper_exit_test'):
            continue  # counted via exit_selftest_evidence below
        k = _kind(ev)
        payload = ev.get('payload') if isinstance(ev.get('payload'), dict) else ev
        side = str(payload.get('side') or ev.get('side') or '').upper()
        if side == 'SELL' or k in ('sell_fill', 'exit_filled'):
            sells.append(k)
        if k in ('stop_hit', 'target_hit', 'trail_stop_filled'):
            stops.append(k)
    if not logs:
        return _verdict(False, not_verifiable='log_files_missing_or_empty')
    from support_exit_evidence import exit_selftest_evidence
    selftest = exit_selftest_evidence(logs, session_day, now_ist)
    ev = {'sell_like': len(sells), 'stop_like': len(stops), 'exit_selftest': selftest}
    warranted = len(sells) == 0 and len(stops) == 0 and not selftest['exercised']
    return _verdict(warranted, reject_reason='sell_or_stop_present_in_logs' if (sells or stops) else 'exit_selftest_exercised', evidence=ev)


def check_latency_events(snap, finding, namespace, session_day, now_ist):
    poll = float(CFG['poll_seconds'])
    mult = float(POLICY['latency']['signal_to_fill_poll_multiple'])
    thresh = poll * mult
    fills = query_fills(snap, namespace, session_day)
    sigs = {str(s.get('id') or s.get('signal_id')): s for s in query_signals(snap, namespace)}
    high = []
    for f in fills:
        sid = str(f.get('signal_id') or '')
        sig = sigs.get(sid)
        if not sig or not sig.get('created_at') or not f.get('filled_at'):
            continue
        a, b = _ist(sig['created_at']), _ist(f['filled_at'])
        if not a or not b or a.date() != b.date():
            continue
        start = _session_dt(session_day, POLICY['expectations']['worker_active']['start_at'])
        clock = a if a >= start else start
        lat = (b - clock).total_seconds()
        if lat > thresh + 1:
            high.append({'signal_id': sid, 'latency_seconds': lat, 'latency_start': clock.isoformat(), 'threshold_seconds': thresh})
    ev = {'threshold_seconds': thresh, 'high_latency_count': len(high), 'samples': high[:8]}
    return _verdict(bool(high), reject_reason='no_fill_exceeded_latency_threshold', evidence=ev)


def check_latency_logs(logs, finding, session_day, now_ist):
    poll = float(CFG['poll_seconds'])
    thresh = poll * float(POLICY['latency']['signal_to_fill_poll_multiple'])
    # logs rarely have joinable signal/fill pairs; if the finding cites samples, re-check those timestamps only
    samples = (_details(finding) or {}).get('samples') or []
    if not samples:
        return _verdict(False, not_verifiable='no_joinable_signal_fill_pairs_in_logs')
    high = []
    for s in samples:
        if s.get('created_at') and s.get('filled_at'):
            a, b = _ist(s['created_at']), _ist(s['filled_at'])
            if a and b:
                start = _session_dt(session_day, POLICY['expectations']['worker_active']['start_at'])
                clock = a if a >= start else start
                lat = (b - clock).total_seconds()
                if lat > thresh + 1:
                    high.append({**s, 'recomputed_latency_seconds': lat, 'threshold_seconds': thresh})
    # Independent path: also accept explicit latency events in logs
    for ev in logs:
        if _kind(ev) == 'signal_to_fill_latency' or ev.get('latency_seconds') is not None:
            lat = float(ev.get('latency_seconds') or (ev.get('payload') or {}).get('latency_seconds') or 0)
            if lat > thresh + 1:
                high.append({'latency_seconds': lat, 'threshold_seconds': thresh, 'from': 'log'})
    ev = {'threshold_seconds': thresh, 'high_latency_count': len(high)}
    if not high and not samples:
        return _verdict(False, not_verifiable='no_joinable_signal_fill_pairs_in_logs')
    return _verdict(bool(high), reject_reason='cited_samples_within_threshold', evidence=ev)


def check_quote_batch_events(snap, finding, namespace, session_day, now_ist):
    rows = query_events(snap, namespace, session_day)
    hits = [r for r in rows if 'quote_batch' in str(r.get('event_type') or '') or 'quote_batch' in str(r.get('data_json') or '').lower()]
    failed = []
    for r in hits:
        blob = (r.get('event_type') or '') + ' ' + str(r.get('data_json') or '')
        if any(s in blob.lower() for s in ('fail', 'error', 'timeout', '403', '429')):
            failed.append(r['event_id'])
    ev = {'quote_batch_events': len(hits), 'failed_like': len(failed)}
    if not hits:
        return _verdict(False, not_verifiable='no_quote_batch_events')
    return _verdict(bool(failed), reject_reason='quote_batch_events_have_no_failure_marker', evidence=ev)


def check_quote_batch_logs(logs, finding, session_day, now_ist):
    hits = [ev for ev in logs if 'quote_batch' in json.dumps(ev, default=str).lower()]
    if not hits:
        return _verdict(False, not_verifiable='no_quote_batch_events_in_logs')
    failed = [ev for ev in hits if any(s in json.dumps(ev, default=str).lower() for s in ('fail', 'error', 'timeout', 'http_403', '429'))]
    ev = {'quote_batch_like': len(hits), 'failed_like': len(failed)}
    return _verdict(bool(failed), reject_reason='quote_batch_logs_have_no_failure_marker', evidence=ev)


def check_generic_events(snap, finding, namespace, session_day, now_ist):
    det = finding.get('detector') or ''
    rows = query_events(snap, namespace, session_day)
    token = det.replace('_', '')
    hits = []
    for r in rows:
        blob = (r.get('event_type') or '') + ' ' + str(r.get('data_json') or '')
        if det in blob or token and token in blob.replace('_', ''):
            hits.append(r['event_type'])
    ev = {'matching_events': len(hits), 'types': sorted(set(hits))[:12]}
    if not hits:
        return _verdict(False, not_verifiable=f'no_corroborating_events_for_{det}')
    return _verdict(True, evidence=ev)


def check_generic_logs(logs, finding, session_day, now_ist):
    det = finding.get('detector') or ''
    if not logs:
        return _verdict(False, not_verifiable='log_files_missing_or_empty')
    hits = [ev for ev in logs if det in json.dumps(ev, default=str)]
    ev = {'matching_logs': len(hits)}
    if not hits:
        return _verdict(False, not_verifiable=f'no_corroborating_logs_for_{det}')
    return _verdict(True, evidence=ev)


def check_mark_stale_events(snap, finding, namespace, session_day, now_ist):
    rows = query_events(snap, namespace, session_day, types=('position_marks_stale',))
    ev = {'events': len(rows), 'source_at': [r.get('source_at') for r in rows[:10]]}
    return _verdict(bool(rows), reject_reason='no_position_marks_stale_event', evidence=ev)


def check_mark_stale_logs(logs, finding, session_day, now_ist):
    if not logs:
        return _verdict(False, not_verifiable='log_files_missing_or_empty')
    hits = [ev for ev in logs if _kind(ev) == 'position_marks_stale']
    ev = {'matching_logs': len(hits)}
    return _verdict(bool(hits), reject_reason='no_position_marks_stale_log', evidence=ev)


EVENTS_CHECKERS = {
    'scan_missed': check_scan_missed_events,
    'worker_down': lambda *a, **k: check_worker_or_staleness_events(*a, kind='worker_down', **k),
    'monitor_staleness': lambda *a, **k: check_worker_or_staleness_events(*a, kind='monitor_staleness', **k),
    'eod_missed': check_eod_events,
    'feed_http_403': check_403_events,
    'fill_sizing_anomaly': check_sizing_events,
    'exit_path_unexercised': check_exit_events,
    'signal_to_fill_latency': check_latency_events,
    'quote_batch_failed': check_quote_batch_events,
    'position_marks_stale': check_mark_stale_events,
}

LOG_CHECKERS = {
    'scan_missed': check_scan_missed_logs,
    'worker_down': lambda logs, finding, session_day, now_ist: check_worker_or_staleness_logs(logs, finding, session_day, now_ist, 'worker_down'),
    'monitor_staleness': lambda logs, finding, session_day, now_ist: check_worker_or_staleness_logs(logs, finding, session_day, now_ist, 'monitor_staleness'),
    'eod_missed': check_eod_logs,
    'feed_http_403': check_403_logs,
    'fill_sizing_anomaly': check_sizing_logs,
    'exit_path_unexercised': check_exit_logs,
    'signal_to_fill_latency': check_latency_logs,
    'quote_batch_failed': check_quote_batch_logs,
    'position_marks_stale': check_mark_stale_logs,
}


def aggregate_status(verdicts):
    confirms = sum(1 for v in verdicts if v == 'CONFIRM')
    rejects = sum(1 for v in verdicts if v == 'REJECT')
    if confirms >= 2 and rejects == 0:
        return 'confirmed'
    if rejects >= 1 and confirms >= 1:
        return 'disputed'
    if rejects >= 2 and confirms == 0:
        return 'rejected'
    return 'unconfirmed'


def run_verifier(verifier_id, finding, *, snap, logs, namespace, session_day, now_ist):
    det = finding.get('detector') or ''
    if verifier_id == 'events_sql':
        fn = EVENTS_CHECKERS.get(det, check_generic_events)
        if det in EVENTS_CHECKERS:
            return fn(snap, finding, namespace, session_day, now_ist)
        return fn(snap, finding, namespace, session_day, now_ist)
    if verifier_id == 'log_files':
        fn = LOG_CHECKERS.get(det, check_generic_logs)
        if det in ('scan_missed', 'eod_missed', 'feed_http_403', 'fill_sizing_anomaly', 'exit_path_unexercised', 'signal_to_fill_latency', 'quote_batch_failed'):
            return fn(logs, finding, session_day, now_ist)
        if det in ('worker_down', 'monitor_staleness'):
            return fn(logs, finding, session_day, now_ist)
        return fn(logs, finding, session_day, now_ist)
    return _verdict(False, not_verifiable=f'unknown_verifier:{verifier_id}')


def review_prompt(finding, record):
    det = finding.get('detector')
    fid = finding.get('finding_id')
    lines = [
        f'# External review prompt — {fid}',
        '',
        'You are an independent reviewer of a paper-trading Support Agent finding.',
        'You cannot change production systems. Do not invent events, prices, fills, or timestamps.',
        'Answer with exactly one of: CONFIRMED, DISPUTED, NOT_VERIFIABLE, then a short reason.',
        'If you cannot open the raw evidence files (paper_events snapshot, engine.jsonl, audit jsonl),',
        'you MUST answer NOT_VERIFIABLE. Never answer CONFIRMED or PASS from this prompt text alone.',
        '',
        '## Finding',
        f"- finding_id: {fid}",
        f"- detector: {det}",
        f"- severity: {finding.get('severity')}",
        f"- status: {finding.get('status')}",
        f"- session_day: {finding.get('session_day')}",
        f"- first_event_at: {finding.get('first_event_at')}",
        f"- details: {json.dumps(_details(finding), default=str)[:4000]}",
        '',
        '## Independent deterministic verifiers',
    ]
    for v in record.get('verifiers') or []:
        lines += [
            f"### {v['verifier_id']}: {v['verdict']}",
            f"```json",
            json.dumps(v.get('evidence') or {}, indent=2, default=str)[:3000],
            '```',
            '',
        ]
    lines += [
        f"## Aggregate: **{record.get('status')}**",
        '',
        '## What to check',
        '- Re-derive the detector condition from raw paper_events and from engine.jsonl/audit files separately.',
        '- Confirm timestamps are in Asia/Kolkata and fall on the claimed session day.',
        '- If the two sources disagree, answer DISPUTED and say which source you trust and why.',
        '',
        'Your one-word verdict:',
    ]
    return '\n'.join(lines) + '\n'


def verify_findings(findings, *, snapshot_path, engine_path=None, audit_path=None,
                    namespace=None, session_day=None, now_ist=None, findings_db=None,
                    recorded_at=None):
    """Run both verifiers on each finding. Returns a JSON-serializable report."""
    now_ist = _ist(now_ist) or datetime.now(IST)
    stamp = recorded_at or now_ist.isoformat()
    snap = _connect_snapshot(snapshot_path)
    logs = load_log_events(engine_path, audit_path, session_day, namespace=namespace)
    log_meta = {
        'engine_path': str(engine_path) if engine_path else None,
        'engine_exists': bool(engine_path and Path(engine_path).exists()),
        'audit_path': str(audit_path) if audit_path else None,
        'audit_exists': bool(audit_path and Path(audit_path).exists()),
        'log_events_loaded': len(logs),
    }

    store = None
    if findings_db is not None:
        store = init(findings_db)

    latest = {}
    for f in findings or []:
        latest[f['finding_id']] = f
    rows = []
    counts = defaultdict(int)
    for fid, finding in latest.items():
        finding = dict(finding)
        finding.setdefault('session_day', session_day)
        verifiers = []
        for vid in VERIFIER_IDS:
            result = run_verifier(vid, finding, snap=snap, logs=logs, namespace=namespace,
                                  session_day=session_day, now_ist=now_ist)
            rec = {
                'verifier_id': vid,
                'verdict': result['verdict'],
                'evidence': result.get('evidence') or {},
            }
            verifiers.append(rec)
            if store is not None:
                _record(store, namespace, fid, vid, rec['verdict'], rec['evidence'], stamp)
        status = aggregate_status([v['verdict'] for v in verifiers])
        counts[status] += 1
        record = {
            'finding_id': fid,
            'detector': finding.get('detector'),
            'severity': finding.get('severity'),
            'status': status,
            'verifiers': verifiers,
        }
        record['review_prompt'] = review_prompt(finding, record)
        rows.append(record)

    if hasattr(snap, 'close'):
        try:
            snap.close()
        except Exception:
            pass

    return {
        'session_day': session_day,
        'namespace': namespace,
        'mode': 'read_only_findings_verification',
        'verified_at_ist': stamp,
        'findings_total': len(rows),
        'by_status': dict(counts),
        'log_meta': log_meta,
        'findings': rows,
        'actions_taken': 'none (no patch, no install, no notification, no orders, no finding-status mutation)',
    }


def _record(store, namespace, finding_id, verifier_id, verdict, evidence, stamp):
    args = (namespace, finding_id, verifier_id, verdict, json.dumps(evidence, default=str), stamp)
    sql = ('INSERT OR REPLACE INTO support_finding_verifications('
           'namespace,finding_id,verifier_id,verdict,evidence_json,recorded_at) VALUES(?,?,?,?,?,?)')
    if hasattr(store, 'batch'):
        store.batch([(sql, args)])
        return
    if hasattr(store, 'execute'):
        store.execute(sql, args)
        store.commit()
        return
    raise TypeError('findings_db does not support writes')


def write_report(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir = out_dir / 'review-prompts'
    prompt_dir.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in report.items() if k != 'findings'}
    slim['findings'] = [
        {k: f[k] for k in f if k != 'review_prompt'} for f in report['findings']
    ]
    (out_dir / 'verification.json').write_text(json.dumps(slim, indent=2, default=str))
    parts = [
        f"# Independent finding verification — {report.get('session_day')}",
        '',
        f"Findings: **{report.get('findings_total')}** — "
        + ', '.join(f"{k}: {v}" for k, v in sorted((report.get('by_status') or {}).items())),
        '',
        'Each finding was re-derived by `events_sql` (snapshot paper_events/fills) and `log_files` '
        '(engine.jsonl + audit jsonl). Status is confirmed only when both CONFIRM and neither REJECTS.',
        'This stage is read-only / findings-only: no patches, installs, or finding-status changes.',
        '',
        '| Finding | Detector | Status | events_sql | log_files |',
        '|---|---|---|---|---|',
    ]
    all_prompts = [f"# Paste-ready review prompts — {report.get('session_day')}", '']
    for f in report['findings']:
        vmap = {v['verifier_id']: v['verdict'] for v in f['verifiers']}
        parts.append(f"| `{f['finding_id']}` | {f.get('detector')} | **{f['status']}** | {vmap.get('events_sql')} | {vmap.get('log_files')} |")
        name = ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in f['finding_id'])
        (prompt_dir / f'{name}.md').write_text(f['review_prompt'])
        all_prompts += [f['review_prompt'], '', '---', '']
    (out_dir / 'VERIFICATION.md').write_text('\n'.join(parts) + '\n')
    (out_dir / 'REVIEW_PROMPTS.md').write_text('\n'.join(all_prompts))
    return {
        'verification_json': str(out_dir / 'verification.json'),
        'verification_md': str(out_dir / 'VERIFICATION.md'),
        'review_prompts': str(out_dir / 'REVIEW_PROMPTS.md'),
        'prompt_dir': str(prompt_dir),
    }
