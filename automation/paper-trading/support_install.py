"""Support Agent Evening Approval Gate, Quiescence Gate, Release Swap & Rollback (Plan v3).

Enforces:
- Human approval only from a genuine user-authored chat turn (never tool outputs or logs).
- Batch ID tied to exact SHA-256 of sorted commit SHAs; any commit mutation voids approval.
- Engine-class changes require explicit per-change ID approval.
- Subset approval requires re-verification of the exact subset tree SHA.
- Quiescence gate: aborts with SEV2 install_aborted_process_active if any engine/scan/worker/guardian process or lock is active.
- Morning abort at policy install.morning_abort_at (08:05 IST) or install.max_seconds (900s) triggers unconditional rollback and SEV2 install_rolled_back.
"""
import hashlib, json, os, re, shutil
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
import support_agent as sa
import support_verifiers as sv

APPROVAL_CMD_RE = re.compile(r'^\s*(APPROVE|DECLINE|HOLD)\s+(B-\d{4}-\d{2}-\d{2}-[0-9a-f]{8})(?:\s+([A-Za-z0-9_,\s\-]+))?\s*$')


def format_evening_report(session_day, findings, ready_changes=None):
    """Build 16:45 IST evening report and install approval ask.

    If nothing is READY_FOR_HUMAN, returns a short status and schedules no reminders.
    """
    day = sa.ist_session_day(session_day)
    ready = [c for c in (ready_changes or []) if c.get('reconciler_outcome') == 'READY_FOR_HUMAN']
    if not ready:
        return {
            'session_day': day,
            'report_at': f"{day}T{sa.SUPPORT_POLICY['schedule']['evening_daily_report']}:00+05:30",
            'has_ready_batch': False,
            'batch_id': None,
            'reminders': [],
            'text': f"Support Evening Report ({day}): {len(findings)} finding(s), 0 changes ready for install. No approval needed tonight.",
        }
    shas = [c['commit_sha'] for c in ready]
    batch_id = sv.compute_batch_id(day, shas)
    reminders = sa.reminder_schedule_for_batch(day)
    change_lines = []
    for c in ready:
        cid = c['change_id']
        cls = c.get('change_class', 'support_only')
        req_id = ' (requires explicit change ID approval)' if cls == 'engine_instrumentation_or_locking' else ''
        change_lines.append(f"- {cid} [{cls}] sha={c['commit_sha'][:8]} detector={c.get('detector')}{req_id}")
    text = '\n'.join([
        f"Support Evening Report ({day}) — Batch {batch_id}",
        f"Findings today: {len(findings)}",
        "Ready changes (verified by V1, V2, V3 + Reconciler):",
        *change_lines,
        f"Reply with: APPROVE {batch_id} (or APPROVE {batch_id} C1 / DECLINE {batch_id} / HOLD {batch_id})",
    ])
    return {
        'session_day': day,
        'report_at': f"{day}T{sa.SUPPORT_POLICY['schedule']['evening_daily_report']}:00+05:30",
        'has_ready_batch': True,
        'batch_id': batch_id,
        'commit_shas': sorted(shas),
        'changes': ready,
        'reminders': reminders,
        'text': text,
    }


def parse_and_record_approval(support_store, message_text, message_source, batch_manifest, actor='human_kgr', recorded_at=None):
    """Parse an approval command strictly from a user-authored chat turn and record it in support.sqlite.

    Rejects any APPROVE inside a tool result, log, or non-user message source.
    """
    if message_source != 'user_chat':
        raise PermissionError(f'approval_source_rejected:{message_source}')
    m = APPROVAL_CMD_RE.match(str(message_text or ''))
    if not m:
        raise ValueError('invalid_approval_command_syntax')
    decision, batch_id, raw_ids = m.group(1), m.group(2), m.group(3)

    day = sa.ist_session_day(batch_manifest['session_day'])
    manifest_changes = {c['change_id']: c for c in batch_manifest.get('changes', [])}
    all_shas = [c['commit_sha'] for c in manifest_changes.values()]
    expected_batch_id = sv.compute_batch_id(day, all_shas)
    if batch_id != expected_batch_id or batch_id != batch_manifest.get('batch_id'):
        raise ValueError('batch_id_commit_hash_mismatch')

    explicit_ids = [x.strip() for x in re.split(r'[,\s]+', raw_ids.strip()) if x.strip()] if raw_ids else []
    if decision == 'APPROVE':
        if explicit_ids:
            unknown = [cid for cid in explicit_ids if cid not in manifest_changes]
            if unknown:
                raise ValueError(f'unknown_change_ids:{unknown}')
            selected_ids = explicit_ids
        else:
            # Engine-class changes require explicit approval by change_id
            engine_changes = [
                cid for cid, c in manifest_changes.items()
                if c.get('change_class') == 'engine_instrumentation_or_locking'
            ]
            if engine_changes and sa.SUPPORT_POLICY['verifier_rules']['engine_class_needs_per_change_approval']:
                raise PermissionError(f'engine_class_requires_explicit_change_id:{engine_changes}')
            selected_ids = sorted(manifest_changes.keys())
    else:
        selected_ids = explicit_ids or sorted(manifest_changes.keys())

    approved_shas = sorted(manifest_changes[cid]['commit_sha'] for cid in selected_ids)
    is_subset = len(selected_ids) < len(manifest_changes)
    stamp = recorded_at or sa.ist_now().isoformat()

    sa._write_support_only(support_store, [(
        'INSERT OR REPLACE INTO support_approvals(batch_id,session_day,decision,approved_changes_json,commit_shas_json,subset_tree_sha,actor,chat_turn_verified,recorded_at,voided,void_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (
            batch_id,
            day,
            decision,
            json.dumps(selected_ids),
            json.dumps(approved_shas),
            None,
            str(actor),
            1,
            stamp,
            0,
            None,
        ),
    )])
    return {
        'batch_id': batch_id,
        'session_day': day,
        'decision': decision,
        'approved_change_ids': selected_ids,
        'approved_commit_shas': approved_shas,
        'is_subset': is_subset,
        'recorded_at': stamp,
    }


def void_approval_on_mutation(support_store, batch_id, current_commit_shas, session_day):
    """Void an existing approval if the commit list changed after the report."""
    expected_id = sv.compute_batch_id(session_day, current_commit_shas)
    rows = support_store.query('SELECT * FROM support_approvals WHERE batch_id=?', (batch_id,))
    if not rows:
        return {'voided': False, 'reason': 'not_found'}
    stored_shas = json.loads(rows[0]['commit_shas_json'])
    if batch_id != expected_id or not set(stored_shas).issubset(set(current_commit_shas)):
        sa._write_support_only(support_store, [(
            'UPDATE support_approvals SET voided=1,void_reason=? WHERE batch_id=?',
            ('post_approval_commit_mutation', batch_id),
        )])
        return {'voided': True, 'reason': 'post_approval_commit_mutation'}
    return {'voided': False}


def select_install_slot(approval_at, cfg=None):
    """Determine install slot (21:45 evening if approved by 21:30, or 07:50 next trading day if approved by 07:45)."""
    c = sa.CFG if cfg is None else cfg
    inst = sa.SUPPORT_POLICY['install']
    t = sa.parse_ist(approval_at)
    d = t.date()
    wall = t.time().replace(tzinfo=None)

    ev_cut = dt_time(*[int(x) for x in inst['evening_cutoff'].split(':')])
    mo_cut = dt_time(*[int(x) for x in inst['morning_cutoff'].split(':')])
    report_at = dt_time(*[int(x) for x in sa.SUPPORT_POLICY['schedule']['evening_daily_report'].split(':')])

    if sa.is_trading_day(d, cfg=c) and report_at <= wall <= ev_cut:
        return {
            'eligible': True,
            'slot': 'evening',
            'scheduled_at': f"{d.isoformat()}T{inst['evening_slot']}:00+05:30",
        }
    if sa.is_trading_day(d, cfg=c) and wall <= mo_cut:
        return {
            'eligible': True,
            'slot': 'morning',
            'scheduled_at': f"{d.isoformat()}T{inst['morning_slot']}:00+05:30",
        }
    # Plan 5.4: an approval after the 21:30 evening cutoff, or on a weekend/holiday, is still before the
    # 07:45 cutoff of the next trading day, so it takes that day's 07:50 morning slot.
    if (sa.is_trading_day(d, cfg=c) and wall > ev_cut) or not sa.is_trading_day(d, cfg=c):
        nxt = sa.next_trading_day(d, cfg=c)
        return {
            'eligible': True,
            'slot': 'morning',
            'scheduled_at': f"{nxt.isoformat()}T{inst['morning_slot']}:00+05:30",
        }
    return {'eligible': False, 'reason': 'missed_cutoff_waits_for_next_evening_report'}


QUIESCENCE_LOCKS = ('engine.lock', 'monitor.lock', 'guardian.lock')


def _held_lock_inodes(proc_locks_path='/proc/locks'):
    """Read-only view of held POSIX/flock locks as {(major, minor, inode)}. None if unreadable."""
    try:
        text = Path(proc_locks_path).read_text(encoding='utf8')
    except OSError:
        return None
    held = set()
    for line in text.splitlines():
        parts = line.split()
        dev = next((x for x in parts if x.count(':') == 2), None)
        if not dev:
            continue
        try:
            maj, mnr, ino = dev.split(':')
            held.add((int(maj, 16), int(mnr, 16), int(ino)))
        except ValueError:
            continue
    return held


def check_quiescence_gate(root_dir=None, active_processes=None, proc_locks_path='/proc/locks'):
    """Quiescence gate: verify no engine, scan, worker, or guardian process or lock is active.

    The engine, worker and guardian hold empty lock files with fcntl.flock, so file size says nothing.
    The gate reads /proc/locks (read-only, never takes the lock itself, so it cannot make a starting
    engine see singleton_busy). If held locks cannot be read, the gate fails closed.
    """
    base = Path(root_dir) if root_dir is not None else sa.ROOT
    active_locks = []
    held = _held_lock_inodes(proc_locks_path)
    for lock_name in QUIESCENCE_LOCKS:
        lp = base / lock_name
        if not lp.exists():
            continue
        if held is None:
            active_locks.append(f'{lock_name}:lock_state_unreadable')
            continue
        st = lp.stat()
        if (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino) in held:
            active_locks.append(lock_name)
    procs = list(active_processes or [])
    busy = active_locks + procs
    return {'quiescent': len(busy) == 0, 'active_locks': active_locks, 'active_processes': procs}


def execute_install(
    support_store,
    namespace,
    batch_id,
    batch_manifest,
    install_started_at,
    release_sha,
    pt_root_dir,
    safety_verdict,
    subset_reverified=False,
    subset_tree_sha=None,
    active_locks_or_procs=None,
    smoke_test_fn=None,
    smoke_finished_at=None,
    runtime_root=None,
):
    """Execute pre-flight, quiescence gate, atomic release switch, smoke test, and morning abort rollback."""
    start_ist = sa.parse_ist(install_started_at)
    session_day = start_ist.date().isoformat()
    inst_cfg = sa.SUPPORT_POLICY['install']

    # 1. Verify local approval record in support.sqlite
    rows = support_store.query('SELECT * FROM support_approvals WHERE batch_id=?', (batch_id,))
    if not rows:
        raise PermissionError('missing_local_approval_record')
    app = rows[0]
    if int(app.get('voided', 0)) == 1 or app.get('decision') != 'APPROVE' or int(app.get('chat_turn_verified', 0)) != 1:
        raise PermissionError('approval_not_valid_for_install')

    # 2. Check subset re-verification if subset of batch was approved
    approved_ids = json.loads(app['approved_changes_json'])
    approved_shas = set(json.loads(app['commit_shas_json']))
    total_changes = len(batch_manifest.get('changes', []))
    is_subset = len(approved_ids) < total_changes
    if is_subset:
        if not subset_reverified or not subset_tree_sha:
            raise PermissionError('subset_approval_requires_reverified_tree_sha')
    # Approval binds to the exact SHAs in the report (plan 5.4): never install anything else.
    allowed_release = {str(subset_tree_sha)} if is_subset else approved_shas
    if str(release_sha) not in allowed_release:
        raise PermissionError('release_sha_not_in_approved_batch')
    if is_subset:
        sa._write_support_only(support_store, [(
            'UPDATE support_approvals SET subset_tree_sha=? WHERE batch_id=?',
            (str(subset_tree_sha), batch_id),
        )])

    # 3. Verify install window and binding safety verdict
    win_check = sv.check_install_window(start_ist)
    if not win_check['passed']:
        raise PermissionError(f"install_outside_allowed_window:{win_check['reason']}")
    if not safety_verdict or safety_verdict.get('overall') != 'PASS' or safety_verdict.get('hard_veto'):
        raise PermissionError('binding_safety_verdict_failed')

    # 4. Quiescence gate (always probes the runtime's own locks; caller-supplied processes are added)
    gate = check_quiescence_gate(runtime_root, active_processes=active_locks_or_procs)
    if not gate['quiescent']:
        active_locks_or_procs = gate['active_locks'] + gate['active_processes']
        sa.append_finding(
            support_store, namespace, 'install_aborted_process_active',
            f'install_abort:{batch_id}', start_ist.isoformat(), session_day,
            severity='SEV2', status='OPEN',
            details={'batch_id': batch_id, 'active': list(active_locks_or_procs)},
            observation_only=False, recorded_at=start_ist.isoformat(),
        )
        return {'status': 'ABORTED', 'reason': 'install_aborted_process_active'}

    # 5. Prepare release layout (/workspace/pt/releases/<sha>/, /workspace/pt/shared/, current pointer)
    pt_root = Path(pt_root_dir)
    releases_dir = pt_root / 'releases'
    shared_dir = pt_root / 'shared'
    current_ptr = pt_root / 'current_release.txt'
    releases_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    prev_sha = current_ptr.read_text(encoding='utf8').strip() if current_ptr.exists() else 'initial_release'

    new_rel_dir = releases_dir / str(release_sha)
    new_rel_dir.mkdir(parents=True, exist_ok=True)
    (new_rel_dir / 'RELEASE_SHA').write_text(str(release_sha), encoding='utf8')
    current_ptr.write_text(str(release_sha), encoding='utf8')

    # 6. Run smoke test & enforce morning abort cutoff (08:05 IST) and max_seconds (900s)
    smoke_ok = True
    if smoke_test_fn is not None:
        try:
            smoke_ok = bool(smoke_test_fn(new_rel_dir))
        except Exception:
            smoke_ok = False

    finish_ist = sa.parse_ist(smoke_finished_at) if smoke_finished_at is not None else start_ist
    elapsed_sec = (finish_ist - start_ist).total_seconds()
    abort_h, abort_m = [int(x) for x in inst_cfg['morning_abort_at'].split(':')]
    morning_abort_dt = datetime(finish_ist.year, finish_ist.month, finish_ist.day, abort_h, abort_m, tzinfo=sa.IST)
    crossed_morning_abort = (start_ist.hour < 12) and (finish_ist >= morning_abort_dt)
    exceeded_max_seconds = elapsed_sec > float(inst_cfg['max_seconds'])

    if not smoke_ok or crossed_morning_abort or exceeded_max_seconds:
        # Automatic unconditional rollback
        current_ptr.write_text(prev_sha, encoding='utf8')
        rollback_reason = (
            'morning_abort_cutoff_exceeded' if crossed_morning_abort
            else ('install_max_seconds_exceeded' if exceeded_max_seconds else 'smoke_test_failed')
        )
        sa.append_finding(
            support_store, namespace, 'install_rolled_back',
            f'install_rollback:{batch_id}', finish_ist.isoformat(), session_day,
            severity='SEV2', status='OPEN',
            details={
                'batch_id': batch_id,
                'failed_release_sha': release_sha,
                'rolled_back_to': prev_sha,
                'reason': rollback_reason,
                'elapsed_seconds': elapsed_sec,
            },
            observation_only=False, recorded_at=finish_ist.isoformat(),
        )
        return {
            'status': 'ROLLED_BACK',
            'reason': rollback_reason,
            'active_release': prev_sha,
        }

    return {
        'status': 'INSTALLED',
        'batch_id': batch_id,
        'active_release': str(release_sha),
        'previous_release': prev_sha,
        'elapsed_seconds': elapsed_sec,
    }


def verify_finding_next_session(support_store, namespace, finding_id_val, next_session_day, recorded_at=None):
    """Step 8 of Daily Loop: if detector stays silent in the next session, transition finding to VERIFIED."""
    day = sa.ist_session_day(next_session_day)
    rows = support_store.query(
        'SELECT * FROM support_findings WHERE namespace=? AND finding_id=? ORDER BY status_seq DESC LIMIT 1',
        (namespace, finding_id_val),
    )
    if not rows:
        raise KeyError('finding_not_found')
    detector = rows[0]['detector']
    new_day_findings = [
        f for f in sa.current_findings(support_store, namespace, day)
        if f['detector'] == detector and f['status'] == 'OPEN'
    ]
    if new_day_findings:
        return {'verified': False, 'reason': 'detector_fired_in_next_session'}
    tr = sa.transition_finding(
        support_store, namespace, finding_id_val, 'VERIFIED',
        note=f'detector_silent_on_{day}',
        recorded_at=recorded_at or sa.ist_now().isoformat(),
        actor='support_verifier_loop',
    )
    return {'verified': True, 'finding_id': finding_id_val, 'status': tr['status'], 'status_seq': tr['status_seq']}
