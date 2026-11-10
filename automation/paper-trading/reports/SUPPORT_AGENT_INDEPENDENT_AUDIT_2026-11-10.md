# Support Agent v3 — Independent Audit (10 Nov 2026, IST)

Scope: the Support Agent code on `main` at `325b02b` (support files unchanged since `04c39fe`), tested in an isolated clone against Production Action Plan v3. Nothing was installed, scheduled or wired; the live paper runtime was not touched.

## Method
- Ran the existing suites (13/13 support tests; 287 Python tests in total).
- Replayed the real 9 Nov audit export (8,080 events, loaded read-only into an in-memory DB) with the plan's virtual clock: 08:30–16:45 IST in `worker_stale_seconds` (120 s) steps, then re-ran it to check duplicates.

## What the shipped replay hid
The shipped 9 Nov test uses a hand-built fixture with event types the engine never emits (`lock_blocked_gap`, `worker_error_unclassified`, `quote_batch_recovered`) and only 5 ticks. On the real export at the plan's cadence:
- `lock_blocked_gap` never fired (the engine emits `worker_waiting` / `singleton_busy`).
- `quote_batch_failed` never recovered (the engine's recovery is the next `quote_batch_response`).
- Evidence froze at the first tick that saw a problem: `error_taxonomy_gap` 2 of 6, `fill_sizing_anomaly` 1 of 17, `signal_to_fill_latency` 1 of 17, `worker_down` with no first check and 180 s blind, `monitor_staleness` last check 12:27:44.

## Bugs fixed (each has a test that fails on the old code)
1. Aggregate findings keep the whole session's evidence (append-only OPEN→OPEN evidence row; never rolled back by a replay; zero rows on an unchanged re-run).
2. `lock_blocked_gap` derived from real `worker_waiting`/`singleton_busy` streaks bounded by checks (9 Nov: ≈499 s + ≈975 s, escalated SEV3→SEV2).
3. `quote_batch_failed` closes on the next successful `quote_batch_response`.
4. `worker_down` closes on the first monitor check with blind window and time-to-recover; `monitor_staleness` also covers mid-session gaps and last check vs close.
5. `fill_sizing_anomaly` flags any quantity ≠ floor(target notional / fill price) (via `capital.integer_shares`), not only 1-share fills.
6. Holidays/weekends are fully silent (policy hygiene no longer fires on 10 Nov).
7. `support_findings` cannot be UPDATEd or REPLACEd through the write guard.
8. Setting `incomplete_lifecycle_timeout_minutes` in policy enables the timeout instead of crashing every pass.
9. `run_scheduled_pass` refuses to run on a support policy lock mismatch.
10. Reconciler reads the binding script verdict itself; a forged V2 PASS cannot overrule failing scripts; a missing verdict fails closed.
11. Reconciler requires every verifier verdict to be PASS / PASS_WITH_WARNINGS (a FAIL with no items no longer reaches READY_FOR_HUMAN).
12. `config.json` is a locked file for H3.
13. Engine-class changes: file allowlist (engine/audit/automation/guardian), AST proof required (no before/after source = FAIL), real signal/sizing/price functions protected.
14. H1 AST bans: `subprocess`, `importlib`, `ctypes`, broker SDK imports, `eval`/`exec`/`compile`/`__import__`, `os.system`/`popen`.
15. Naive clock AST catches `datetime.datetime.now()`, `now(tz=None)`, `date.today()`.
16. Quiescence gate: the engine's lock files are empty flock files, so the size check never saw a held lock. The gate now reads `/proc/locks` (read-only; never takes the lock) and fails closed if it cannot. `execute_install` always runs it.
17. `execute_install` refuses a release SHA that is not in the approved batch (or the re-verified subset tree).
18. `select_install_slot`: an approval after 21:30, or on a weekend/holiday, takes the next trading day's 07:50 slot (was ineligible); report time read from policy.

## Result after fixes
Real 9 Nov export, plan cadence: SEV1 `fill_sizing_anomaly` (17), `sleeve_over_limit_new_buy` (S7 12 vs 9); SEV2 `scan_missed`, `worker_down` 09:15–12:27 (resolved at first check), `monitor_staleness` (first 12:27:44, last 15:29:58), `lock_blocked_gap` ×2 escalated; SEV3 `error_taxonomy_gap` (6), `quote_batch_failed` (recovered), `signal_to_fill_latency` (17); SEV4 `exit_path_unexercised`; `asm_gsm_buy` NOT_EVALUABLE; 3 SEV4 `policy_value_missing` at 08:30. Each episode once; re-run adds zero rows; hash chain intact.

## Still open versus v3 (not fixed here)
- The v3 detector suite is not wired: `run_cli` / `run_scheduled_pass` still run only the 7 legacy detectors and write into the trading DB, not `out/support/support.sqlite`.
- No systemd timer unit, sidecar loop, `support.lock`, pass budget, Turso circuit breaker, read-only Turso token path, or `support replay --session` CLI.
- Detectors missing: `scan_slow`, `quote_stale_rate`, `eod`-side `ledger_unreconciled` / `snapshot_missing`, `feed_stale_for_session`, `bhavcopy_unreachable`, `calendar_validity_expiring`, `freeze_lock_mismatch`, `routine_missed`, `usage_block_suspected`, `support_self_stale`, `funnel_anomaly`; `box_health` / `dashboard_health` only work when a caller passes metrics.
- Flap guard (re-open within a policy window) and a policy window key are not implemented; inform/notifier and webhook route do not exist (correct for P1/P2).
- Verifiers 1–3 are deterministic stubs over caller-supplied artifacts; no sub-worker orchestration, Proposer worktree tooling, replay diff runner, or real gitleaks binary.
- Release layout is a `current_release.txt` pointer, not the `/workspace/pt/releases` + `current` symlink migration (G-01).
- Policy differences to review: `eod_reconciliation.expected_at` is 16:15 (plan text says 16:10); IST uses a fixed +05:30 offset rather than `ZoneInfo('Asia/Kolkata')` (equivalent for IST).
