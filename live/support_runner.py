"""Read-only Support Agent runner (findings mode). Usage: support_runner.py --session YYYY-MM-DD [--now ISO]

Reads: Turso (SELECT-only, read transaction) -> local snapshot; /workspace/paper-trading/out/audit-<day>.jsonl (read);
       /workspace/paper-trading/logs/engine.jsonl (read) so reference-feed 403s and similar engine-only events are visible.
Writes: findings.sqlite (local, append-only; override with SUPPORT_FINDINGS_DB) and
        /workspace/reports/support-agent/<day>/ (override with SUPPORT_REPORT_DIR).
Never: Turso writes, files inside /workspace/paper-trading, patches, installs, notifications, orders.
"""
import argparse, hashlib, json, os, shutil, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB)); sys.path.insert(0, str(HERE))
import support_agent as sa   # noqa: E402  (library copy, not the installed runtime)
import snapshot              # noqa: E402

IST = ZoneInfo('Asia/Kolkata')
LIVE_ROOT = Path(os.environ.get('TRACK_A_ROOT', '/workspace/paper-trading'))
STATE = HERE.parent / 'state'
REPORTS = Path(os.environ.get('SUPPORT_REPORT_DIR', '/workspace/reports/support-agent'))


def _safe_event_id(kind, at_ist, extra=''):
    raw = f'engine.jsonl|{kind}|{at_ist}|{extra}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def engine_log_events(path, session_day, namespace):
    """Read engine.jsonl (read-only) and return paper_events-shaped rows for the session day.

    This is how reference_feeds HTTP 403s (logged as kind=reference_feeds, not paper_events
    reference_feed_error) reach the Support Agent detectors.
    """
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open(encoding='utf8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            at = ev.get('at_IST') or ev.get('source_at') or ev.get('at')
            if not at:
                continue
            try:
                day = sa.ist_session_day(at)
            except Exception:
                day = str(at)[:10]
            if day != session_day:
                continue
            if ev.get('namespace') and ev.get('namespace') != namespace:
                continue
            kind = ev.get('kind') or ev.get('event_type') or ev.get('action') or 'engine_log'
            # Drop obviously credential-bearing keys if a future log line ever includes them.
            payload = {k: v for k, v in ev.items()
                       if not any(s in k.lower() for s in ('token', 'secret', 'password', 'authorization', 'cookie', 'api_key'))}
            event_type = 'reference_feeds' if kind == 'reference_feeds' else kind
            out.append({
                'namespace': namespace,
                'event_id': _safe_event_id(kind, at, str(ev.get('trade_day') or ev.get('command') or '')),
                'run_id': str(ev.get('run_id') or ev.get('batch_id') or 'engine.jsonl'),
                'session_day': session_day,
                'source_at': at,
                'recorded_at': at,
                'event_type': event_type,
                'severity': 'WARN' if '403' in json.dumps(payload) else 'INFO',
                'outcome': 'ok',
                'data_json': json.dumps(payload, default=str),
            })
    return out


def merge_engine_events(snap_path, events):
    """Insert engine-log rows into the local snapshot only (never Turso)."""
    if not events:
        return 0
    import sqlite3
    c = sqlite3.connect(snap_path)
    # Ensure table exists (it should — paper_events was snapshotted). If not, skip.
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'paper_events' not in names:
        c.close()
        return 0
    cols = ('namespace', 'event_id', 'run_id', 'session_day', 'source_at', 'recorded_at',
            'event_type', 'severity', 'outcome', 'data_json')
    added = 0
    for ev in events:
        try:
            c.execute(
                f'INSERT OR IGNORE INTO paper_events({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                tuple(ev[k] for k in cols),
            )
            added += c.rowcount
        except Exception:
            continue
    c.commit(); c.close()
    return added


def build_parser():
    """Parser used by the 16:45 routine: run_support.sh --session YYYY-MM-DD [--now ISO]."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', required=True)
    ap.add_argument('--now', default=None, help='virtual clock end (ISO, IST); default = full session tick set ending at policy EOD')
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    day = sa.ist_session_day(a.session)
    ns = json.loads((LIVE_ROOT / 'config.json').read_text())['namespace']  # Track A namespace, read-only
    out_dir = REPORTS / day; out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(IST)

    snap_path = Path(os.environ.get('SUPPORT_SNAPSHOT_PATH') or (STATE / 'snapshots' / f'support-{day}.sqlite'))
    counts = snapshot.build(snap_path, [
        ('paper_events', 'namespace=? AND session_day=?', (ns, day)),
        ('paper_decisions', 'namespace=?', (ns,)),
        ('paper_fills', 'namespace=?', (ns,)),
        ('paper_signals', 'namespace=?', (ns,)),
        ('paper_price_bands', 'trade_date=?', (day,)),
    ])
    engine_path = LIVE_ROOT / 'logs' / 'engine.jsonl'
    engine_events = engine_log_events(engine_path, day, ns)
    counts['engine_jsonl_events'] = merge_engine_events(snap_path, engine_events)
    counts['engine_jsonl_reference_feeds'] = sum(1 for e in engine_events if e['event_type'] == 'reference_feeds')

    trading = sa.ReadOnlyTradingDB(snap_path)
    findings_path = Path(os.environ.get('SUPPORT_FINDINGS_DB') or (STATE / 'support' / 'findings.sqlite'))
    store = sa.SupportStore(findings_path)

    ticks = None
    if a.now:
        ticks = [a.now]
    replay = sa.run_virtual_clock_replay(store, trading, namespace=ns, session_day=day, ticks=ticks, observation_only=True)
    rep = sa.session_report(store, ns, day, out_dir=out_dir, write_markdown=True)

    # Audit spool parity (file read only).
    audit = LIVE_ROOT / 'out' / f'audit-{day}.jsonl'
    parity = {'audit_file': str(audit), 'exists': audit.exists()}
    if audit.exists():
        ids, types = set(), Counter()
        with audit.open(encoding='utf8') as f:
            for line in f:
                if line.strip():
                    ev = json.loads(line); ids.add(ev.get('event_id')); types[ev.get('event_type')] += 1
        db_ids = {r['event_id'] for r in trading.query('SELECT event_id FROM paper_events WHERE namespace=? AND session_day=?', (ns, day))}
        parity.update(audit_events=len(ids), turso_events=len(db_ids), only_in_audit=len(ids - db_ids),
                      only_in_turso=len(db_ids - ids), top_event_types=types.most_common(8))
    du = shutil.disk_usage('/workspace')
    findings = replay['findings']
    latest = {}
    for f in findings:
        latest[f['finding_id']] = f
    sev = Counter(f['severity'] for f in latest.values())
    det = Counter((f['severity'], f['detector']) for f in latest.values())
    summary = {
        'session_day': day, 'namespace_read': ns, 'mode': 'read_only_findings', 'observation_only': True,
        'started_at_ist': started.isoformat(), 'finished_at_ist': datetime.now(IST).isoformat(),
        'snapshot_rows': counts, 'findings_total': len(latest), 'by_severity': dict(sorted(sev.items())),
        'by_detector': [{'severity': s, 'detector': d, 'count': n} for (s, d), n in sorted(det.items())],
        'not_evaluable': replay['not_evaluable'], 'chain_failures': replay['chain_failures'],
        'audit_parity': parity, 'disk_free_pct': round(100.0 * du.free / du.total, 1),
        'actions_taken': 'none (no patch, no install, no notification, no orders)',
        'engine_log': str(engine_path), 'engine_log_exists': engine_path.exists(),
    }
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, default=str))
    md = [f'# Support Agent findings — {day} (read-only)', '',
          f"Findings: **{summary['findings_total']}** — " + ', '.join(f'{k}: {v}' for k, v in summary['by_severity'].items()), '',
          '| Severity | Detector | Count |', '|---|---|---|']
    md += [f"| {x['severity']} | {x['detector']} | {x['count']} |" for x in summary['by_detector']]
    md += ['', f"Not evaluable: {json.dumps(summary['not_evaluable'], default=str)}",
           f"Audit spool vs Turso paper_events: {json.dumps({k: v for k, v in parity.items() if k != 'top_event_types'})}",
           f"Hash-chain failures: {len(summary['chain_failures'])}",
           f"Engine log events merged: {counts.get('engine_jsonl_events', 0)} (reference_feeds={counts.get('engine_jsonl_reference_feeds', 0)})",
           '', 'Actions taken: none (findings only).']
    (out_dir / 'SUMMARY.md').write_text('\n'.join(md) + '\n')
    print(json.dumps({k: summary[k] for k in ('session_day', 'findings_total', 'by_severity', 'by_detector', 'not_evaluable', 'audit_parity', 'snapshot_rows')}, indent=1, default=str))

if __name__ == '__main__':
    main()
