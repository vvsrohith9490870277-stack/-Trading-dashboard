"""Severity rules for Support Agent findings (brief adaptive-support-tuning-01, A2).

One table: detector -> {severity rule, action text}. The ``action`` is a
single imperative line, at most 140 characters, of read-only advice — what
the senior should check or do. It is never a command the Support Agent runs
itself (no shell, no installs, no process control, no file paths).

Severity scale (existing, support_agent.py): SEV1 is reported as CRITICAL,
SEV2 as ERROR, SEV3 as WARN, SEV4 as INFO. ``box_host_pause`` and
``render_worker_restarts`` keep the plain WARN scale the detectors emit.

Conditional severities live in the detector that emits them, not here:
``render_health_unreachable`` / ``render_worker_stopped`` /
``render_job_failed`` are CRITICAL when Render is the expected runner and
WARN otherwise (support_render.evaluate decides); the table records the
CRITICAL side. ``lock_blocked_gap`` starts SEV3 and escalates to SEV2
through the episode rules in support_agent.py.
"""

# detector -> {'severity': str, 'action': str (<= 140 chars, advice only)}
RULES = {
    # --- CRITICAL (SEV1): the seven required conditions + existing SEV1 detectors ---
    'scan_missed': {'severity': 'SEV1',
        'action': 'Check why the 08:45 pre-market scan did not complete; verify the next scan ran before trading.'},
    'eod_missed': {'severity': 'SEV1',
        'action': 'Check why EOD reconciliation did not complete; reconcile the ledger before the next session.'},
    'monitor_staleness': {'severity': 'SEV1',
        'action': 'Check the worker heartbeat and host pause log; a gap with no pause behind it means the worker stalled.'},
    'fill_on_stale_quote': {'severity': 'SEV1',
        'action': 'Check the fill whose quote was older than the 60 s limit; verify the quote feed was live at fill time.'},
    'fyers_auth_or_budget_block': {'severity': 'SEV1',
        'action': 'Check the Fyers auth circuit and day budget; verify the token and remaining budget before the next session.'},
    'audit_spool_unreplayed': {'severity': 'SEV1',
        'action': 'Replay the audit spool into Turso and verify only_in_audit returns to zero before the next session.'},
    'lease_conflict': {'severity': 'SEV1',
        'action': 'Check which runner holds the lease and stop the other one; two active runners must never trade the same book.'},
    'reconstruction_failure': {'severity': 'SEV1',
        'action': 'Check the decision-log hash chain; a broken chain means the session cannot be reconstructed.'},
    'fill_sizing_anomaly': {'severity': 'SEV1',
        'action': 'Check the fills sized away from target notional; verify the sizing rule before the next session.'},
    'duplicate_fill': {'severity': 'SEV1',
        'action': 'Check the duplicated fill and verify the position was counted once in the ledger.'},
    'quote_field_regression': {'severity': 'SEV1',
        'action': 'Check the quote timestamp field in use; fills must use the depth LTT field, not TT.'},
    'sleeve_over_limit_new_buy': {'severity': 'SEV1',
        'action': 'Check the sleeve that bought past its slot limit; verify no extra position was opened.'},
    'asm_gsm_buy': {'severity': 'SEV1',
        'action': 'Check the buy in an ASM/GSM restricted symbol and verify it was allowed before the next session.'},
    'render_health_unreachable': {'severity': 'SEV1',
        'action': 'Check the Render service health endpoint; unreachable during a session means the runner may be down.'},
    'render_health_not_ok': {'severity': 'SEV1',
        'action': 'Check the Render health payload; ok must be true while the session is open.'},
    'render_worker_stopped': {'severity': 'SEV1',
        'action': 'Check the Render worker state; it must be running while the session is open.'},
    'render_job_failed': {'severity': 'SEV1',
        'action': 'Check the failed Render job run and its exit code; a failed scan or EOD blocks the session.'},
    # --- SEV2 ---
    'worker_down': {'severity': 'SEV2',
        'action': 'Check the worker liveness log and first monitor check time; verify the worker recovered.'},
    'lock_blocked_gap': {'severity': 'SEV3',
        'action': 'Check what held the engine lock during the gap; verify scans are not starving the monitor.'},
    'position_marks_stale': {'severity': 'SEV2',
        'action': 'Check the position-mark feed; stale marks mean positions were valued on old prices.'},
    'box_health': {'severity': 'SEV2',
        'action': 'Check box disk, clock or lock health; verify the host has room and the clock is in sync.'},
    'install_aborted_process_active': {'severity': 'SEV2',
        'action': 'Check the aborted setup log and verify no setup step left a process running.'},
    'install_rolled_back': {'severity': 'SEV2',
        'action': 'Check the rollback record and verify the previous version is in place.'},
    # --- SEV3 ---
    'error_taxonomy_gap': {'severity': 'SEV3',
        'action': 'Classify the unlisted event types in the finding and add the new ones to the taxonomy table.'},
    'quote_batch_failed': {'severity': 'SEV3',
        'action': 'Check the quote batch failure and verify the feed recovered before the next cycle.'},
    'signal_to_fill_latency': {'severity': 'SEV3',
        'action': 'Check the slowest signal-to-fill path; verify fills are landing within the latency budget.'},
    'feed_http_403': {'severity': 'SEV3',
        'action': 'Check the reference-feed 403 and verify the feed recovered; a stale feed blocks bars.'},
    'dashboard_health': {'severity': 'SEV3',
        'action': 'Check the dashboard probe result; verify the dashboard is reachable before the next session.'},
    # --- SEV4 / WARN / INFO ---
    'policy_value_missing': {'severity': 'SEV4',
        'action': 'Ask the senior to set the missing policy value; the check stays not evaluable until then.'},
    'exit_path_unexercised': {'severity': 'SEV4',
        'action': 'Verify an exit or self-test exercised the exit path before the next session.'},
    'box_host_pause': {'severity': 'WARN',
        'action': 'Note the host pause window; check the box sleep log if pauses recur every session.'},
    'render_worker_restarts': {'severity': 'WARN',
        'action': 'Check why the Render worker restarted; a single cold start is expected on the free tier.'},
    'coverage_gap': {'severity': 'WARN',
        'action': 'Check the scan coverage counts; verify the prefilter and strategy cells match the universe.'},
    'unexpected_skip': {'severity': 'WARN',
        'action': 'Check the unexpected skip cause and verify it is an allowed skip reason.'},
    'placeholder_text': {'severity': 'WARN',
        'action': 'Replace the placeholder text in the decision or event payload with the real value.'},
    'incomplete_lifecycle': {'severity': 'ERROR',
        'action': 'Check the order attempt or run with no result; verify whether it completed or was dropped.'},
    'named_cause_dump': {'severity': 'INFO',
        'action': 'Review the cause counts for the session; no action unless an unmapped cause appears.'},
}


def rule_for(detector):
    """Return the {severity, action} rule for a detector, or None when unknown."""
    return RULES.get(detector)


def is_critical(detector):
    """True when the table rates this detector CRITICAL (SEV1)."""
    rule = RULES.get(detector)
    return bool(rule) and rule['severity'] in ('SEV1', 'CRITICAL')
