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
import support_finding_verify as sfv  # noqa: E402
import support_render as srender  # noqa: E402  (read-only Render checks, A3)
import support_summary as ss  # noqa: E402  (compact summary, A4)

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
            ev_ns = ev.get('namespace')
            if ev_ns and ev_ns != namespace and not str(ev_ns).startswith('paper_exit_test'):
                continue
            kind = ev.get('kind') or ev.get('event_type') or ev.get('action') or 'engine_log'
            # Drop obviously credential-bearing keys if a future log line ever includes them.
            payload = {k: v for k, v in ev.items()
                       if not any(s in k.lower() for s in ('token', 'secret', 'password', 'authorization', 'cookie', 'api_key'))}
            event_type = 'reference_feeds' if kind == 'reference_feeds' else kind
            out.append({
                'namespace': ev_ns or namespace,
                'event_id': _safe_event_id(kind, at, str(ev.get('trade_day') or ev.get('command') or ev_ns or '')),
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
            cur = c.execute(
                f'INSERT OR IGNORE INTO paper_events({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                tuple(ev[k] for k in cols),
            )
            if cur.rowcount and cur.rowcount > 0:
                added += int(cur.rowcount)
        except Exception:
            continue
    c.commit(); c.close()
    return added


def build_parser():
    """Parser used by the 16:45 routine: run_support.sh --session YYYY-MM-DD [--now ISO]."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', required=True)
    ap.add_argument('--now', default=None, help='virtual clock end (ISO, IST); default = full session tick set ending at policy EOD')
    ap.add_argument('--render-health-url', default=None,
                    help='Render /health URL (default: $SUPPORT_RENDER_HEALTH_URL or https://signal-atlas-paper.onrender.com/health)')
    ap.add_argument('--no-render', action='store_true', help='skip the read-only Render checks (A3)')
    return ap


def support_status_line(compact, path):
    """One-line status for stdout: SUPPORT_STATUS=<status> critical=<n> file=<path>."""
    return f"SUPPORT_STATUS={compact['status']} critical={compact['counts']['critical']} file={path}"


def write_compact_summary(out_dir, findings, session_day, namespace, runner, not_evaluable, now_ist):
    """Build the compact summary (A4) and write summary.compact.json in the report folder.

    Returns (path, compact). Writes only this one file, inside out_dir.
    """
    out_dir = Path(out_dir)
    compact = ss.build_compact(findings, session_day, namespace, runner, not_evaluable, now_ist)
    path = out_dir / 'summary.compact.json'
    path.write_text(json.dumps(compact, indent=2, default=str))
    return path, compact


def run_render_check(namespace, session_day, now_ist, health_url=None, no_render=False):
    """Read-only Render check (A3). Returns {findings, not_evaluable, info, scan_completed, eod_completed}.

    Never raises: any failure to reach Render or Turso becomes a
    not_evaluable entry so the support run itself is never blocked.
    ``health_url`` defaults to $SUPPORT_RENDER_HEALTH_URL, then the
    signal-atlas-paper /health URL. ``now_ist`` is a datetime.
    """
    info = {'expected': None, 'health_ok': None, 'mode': None}
    empty = {'findings': [], 'not_evaluable': [], 'info': info,
             'scan_completed': False, 'eod_completed': False}
    if no_render:
        return {**empty, 'not_evaluable': [{'detector': 'render_check', 'reason': 'render_check_skipped'}]}
    url = health_url or os.environ.get('SUPPORT_RENDER_HEALTH_URL') or srender.DEFAULT_HEALTH_URL
    try:
        _status, health = srender.fetch_health(url)
        try:
            from ro_client import TursoRO
            ro = TursoRO(timeout=30, retries=1)
            state = srender.query_render_state(ro.query, namespace, session_day)
        except Exception:
            state = {'kv': None, 'lease': None, 'jobs': None,
                     'tables_absent': True, 'schema_mismatch': False}
        if state.get('schema_mismatch'):
            result = {**empty, 'not_evaluable': [
                {'detector': 'render_lease', 'reason': 'lease_schema_mismatch'}]}
            if isinstance(health, dict):
                result['info'] = {'expected': None, 'health_ok': health.get('ok'),
                                  'mode': health.get('mode')}
            return result
        expected_session = sa.is_trading_day(session_day) and (
            sa.parse_ist(f'{session_day}T08:45:00+05:30')
            <= now_ist <=
            sa.parse_ist(f'{session_day}T16:35:00+05:30'))
        evaluated = srender.evaluate(health, state['kv'], state['lease'], state['jobs'],
                                     now_ist, namespace, expected_session)
        findings = [f for f in evaluated if f['severity'] != 'NOT_EVALUABLE']
        not_evaluable = [{'detector': f['detector'], 'reason': f['details'].get('reason')}
                         for f in evaluated if f['severity'] == 'NOT_EVALUABLE']
        if isinstance(health, dict):
            info = {'expected': (state['kv'] or {}).get('active_runner'),
                    'health_ok': health.get('ok'), 'mode': health.get('mode')}
        jobs = state['jobs'] or []
        scan_ok = any('scan' in str(j.get('job') or '').lower()
                      and j.get('status') == 'ok' for j in jobs)
        eod_ok = any('eod' in str(j.get('job') or '').lower()
                     and j.get('status') == 'ok' for j in jobs)
        return {'findings': findings, 'not_evaluable': not_evaluable, 'info': info,
                'scan_completed': scan_ok, 'eod_completed': eod_ok}
    except Exception:
        return {**empty, 'not_evaluable': [{'detector': 'render_check', 'reason': 'render_check_failed'}]}


def main(argv=None):
    a = build_parser().parse_args(argv)
    day = sa.ist_session_day(a.session)
    ns = json.loads((LIVE_ROOT / 'config.json').read_text())['namespace']  # Track A namespace, read-only
    out_dir = REPORTS / day; out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(IST)

    snap_path = Path(os.environ.get('SUPPORT_SNAPSHOT_PATH') or (STATE / 'snapshots' / f'support-{day}.sqlite'))
    counts = snapshot.build(snap_path, [
        ('paper_events', 'session_day=? AND (namespace=? OR namespace LIKE ?)', (day, ns, 'paper_exit_test%')),
        ('paper_decisions', 'namespace=?', (ns,)),
        ('paper_fills', 'namespace=? OR namespace LIKE ?', (ns, 'paper_exit_test%')),
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

    # A3: read-only Render check (never blocks the run on failure). Findings
    # land in the store before the replay so they appear in the report; the
    # scan/EOD flags let a successful Render job satisfy those expectations.
    now_dt = sa.parse_ist(a.now) if a.now else datetime.now(IST)
    render = run_render_check(ns, day, now_dt, health_url=a.render_health_url, no_render=a.no_render)
    render_info = render['info']
    render_not_evaluable = render['not_evaluable']
    for f in render['findings']:
        sa.append_finding(store, ns, f['detector'], f"{f['detector']}:{day}",
                          now_dt.isoformat(), day, severity=f['severity'],
                          status='OPEN', details=f['details'],
                          observation_only=True, recorded_at=now_dt.isoformat())

    # Durable sanitized exit-selftest evidence (survives Turso --cleanup-only and log rotation).
    import support_exit_evidence as evd
    ev_rows = list(engine_events)
    try:
        ev_rows.extend(trading.query('SELECT * FROM paper_events WHERE session_day=?', (day,)) or [])
        for f in (trading.query("SELECT * FROM paper_fills WHERE namespace LIKE ?", ('paper_exit_test%',)) or []):
            if f.get('filled_at'):
                ev_rows.append({**f, 'event_type': 'paper_fill', 'source_at': f['filled_at']})
    except Exception:
        pass
    now_for_ev = a.now if a.now else datetime.now(IST)
    evidence = evd.exit_selftest_evidence(ev_rows, day, now_for_ev)
    evidence['session_day'] = day
    evidence['source'] = 'engine.jsonl+snapshot'
    if evidence.get('exercised'):
        evd.write_durable_evidence(out_dir / 'exit-selftest-evidence.json', evidence, session_day=day)
        evd.write_durable_evidence(STATE / 'support' / f'exit-selftest-evidence-{day}.json', evidence, session_day=day)

    ticks = None
    if a.now:
        ticks = [a.now]
    replay = sa.run_virtual_clock_replay(store, trading, namespace=ns, session_day=day, ticks=ticks, observation_only=True,
                                         render_scan_completed=render['scan_completed'],
                                         render_eod_completed=render['eod_completed'])
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
    # A2: audit spool not replayed (only_in_audit > 0 after the session ended).
    # only_in_turso is ignored here (expected, harmless). Appended before the
    # extended findings are read so it appears in the summary and compact JSON.
    if audit.exists():
        session_ended = now_dt >= sa.parse_ist(f'{day}T16:35:00+05:30')
        spool = sa.audit_spool_status(parity.get('only_in_audit', 0),
                                      parity.get('only_in_turso', 0), session_ended)
        if spool is not None:
            sa.append_finding(store, ns, spool['detector'], f"{spool['detector']}:{day}",
                              now_dt.isoformat(), day, severity=spool['severity'],
                              status='OPEN', details=spool['details'],
                              observation_only=True, recorded_at=now_dt.isoformat())
    # Detector findings (for verification, unchanged behaviour) plus the
    # Render / spool findings appended to the store (for the summary).
    detector_findings = replay['findings']
    findings = sa.current_findings(store, ns, day)
    latest = {}
    for f in findings:
        latest[f['finding_id']] = f
    sev = Counter(f['severity'] for f in latest.values())
    det = Counter((f['severity'], f['detector']) for f in latest.values())
    # A4: compact summary next to SUMMARY.md / summary.json, plus the
    # one-line status on stdout.
    compact_not_evaluable = ([{'detector': k, 'reason': v} for k, v in (replay['not_evaluable'] or {}).items()]
                             + list(render_not_evaluable))
    compact_path, compact = write_compact_summary(out_dir, findings, day, ns, render_info,
                                                  compact_not_evaluable, now_dt)
    print(support_status_line(compact, compact_path))
    summary = {
        'session_day': day, 'namespace_read': ns, 'mode': 'read_only_findings', 'observation_only': True,
        'started_at_ist': started.isoformat(), 'finished_at_ist': datetime.now(IST).isoformat(),
        'snapshot_rows': counts, 'findings_total': len(latest), 'by_severity': dict(sorted(sev.items())),
        'by_detector': [{'severity': s, 'detector': d, 'count': n} for (s, d), n in sorted(det.items())],
        'not_evaluable': replay['not_evaluable'], 'chain_failures': replay['chain_failures'],
        'audit_parity': parity, 'disk_free_pct': round(100.0 * du.free / du.total, 1),
        'actions_taken': 'none (no patch, no install, no notification, no orders)',
        'engine_log': str(engine_path), 'engine_log_exists': engine_path.exists(),
        'session_fills': sa.session_ledger_fills(trading, ns, day),
        'compact_summary': {'path': str(compact_path), 'status': compact['status']},
    }
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, default=str))
    md = [f'# Support Agent findings — {day} (read-only)', '',
          f"Findings: **{summary['findings_total']}** — " + ', '.join(f'{k}: {v}' for k, v in summary['by_severity'].items()), '',
          '| Severity | Detector | Count |', '|---|---|---|']
    md += [f"| {x['severity']} | {x['detector']} | {x['count']} |" for x in summary['by_detector']]
    # A2: CRITICAL block at the top of the report: <detector>: <action>.
    critical = [f for f in findings if f['severity'] in ('SEV1', 'CRITICAL')]
    if critical:
        md += ['', '## CRITICAL', '']
        for f in sorted(critical, key=lambda x: x['detector']):
            action = (f.get('details') or {}).get('action') or ''
            md.append(f"- **{f['detector']}**: {action}")
    fills = summary.get('session_fills') or []
    if fills:
        md += ['', '## Session fills (ledger `paper_fills.price`, not the raw quote)', '',
               '| Symbol | Side | Qty | Fill price | Filled at (IST) | Path |', '|---|---|---|---|---|---|']
        for f in fills:
            md.append(
                f"| {f.get('symbol')} | {f.get('side')} | {f.get('qty')} | {f.get('fill_price')} | {f.get('filled_at')} | {f.get('execution_path') or ''} |"
            )
    md += ['', f"Not evaluable: {json.dumps(summary['not_evaluable'], default=str)}",
           f"Audit spool vs Turso paper_events: {json.dumps({k: v for k, v in parity.items() if k != 'top_event_types'})}",
           f"Hash-chain failures: {len(summary['chain_failures'])}",
           f"Engine log events merged: {counts.get('engine_jsonl_events', 0)} (reference_feeds={counts.get('engine_jsonl_reference_feeds', 0)})",
           '', 'Actions taken: none (findings only).']
        # Independent verification stage (read-only). Does not mutate finding status.
    now_for_v = a.now if a.now else datetime.now(IST)
    verification = sfv.verify_findings(
        detector_findings,
        snapshot_path=snap_path,
        engine_path=engine_path,
        audit_path=audit,
        namespace=ns,
        session_day=day,
        now_ist=now_for_v,
        findings_db=store,
    )
    vpaths = sfv.write_report(verification, out_dir)
    summary['verification'] = {
        'findings_total': verification['findings_total'],
        'by_status': verification['by_status'],
        'log_meta': verification['log_meta'],
        'report': vpaths,
    }
    md += ['', '## Independent verification', '',
           'Each finding was re-derived by two deterministic verifiers (`events_sql` from the snapshot, '
           '`log_files` from engine.jsonl/audit). Confirmed only when both CONFIRM and neither REJECTS.',
           'Paste-ready external review prompts: `REVIEW_PROMPTS.md` and `review-prompts/`.',
           '']
    md += [f"- **{k}**: {v}" for k, v in sorted((verification.get('by_status') or {}).items())]
    md += ['', '| Finding | Detector | Status | events_sql | log_files |', '|---|---|---|---|---|']
    for f in verification['findings']:
        vmap = {x['verifier_id']: x['verdict'] for x in f['verifiers']}
        md.append(f"| `{f['finding_id']}` | {f.get('detector')} | **{f['status']}** | {vmap.get('events_sql')} | {vmap.get('log_files')} |")

    (out_dir / 'SUMMARY.md').write_text('\n'.join(md) + '\n')
    print(json.dumps({k: summary[k] for k in ('session_day', 'findings_total', 'by_severity', 'by_detector', 'not_evaluable', 'audit_parity', 'snapshot_rows', 'verification')}, indent=1, default=str))

if __name__ == '__main__':
    main()
