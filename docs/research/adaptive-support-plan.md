# Adaptive Support Tuning — Phase 0 plan

- Brief: `adaptive-support-tuning-01.md` (received 2026-11-20 08:09 IST)
- Base: `origin/main` `33e6b77` (verified still current 2026-11-20 morning, before any commit)
- Branches (both from `33e6b77`, never stacked):
  - A — `feat/support-agent-hardening` — worktree `~/workspace/ast-a`
  - B — `feat/adaptive-tuning` — worktree `~/workspace/ast-b`
- Phase 0 time-box: 2 hours, mandatory before any code. This file is the first
  commit on branch A; branch B's first commit is an identical copy.
- Status: Phase 0 COMPLETE (audit + research done, this plan written).
  Implementation follows the ranked list in §5.

## 0. Starting-state findings (verified, not assumed)

- `open-issues-03.md` was NOT found anywhere under `~/workspace` (checked the
  user-files dir and the repo). The open-issues-03 branches do NOT exist on
  origin (checked 2026-11-20 08:09 IST): no `fix/exit-slippage-base-fallback`,
  no `fix/support-stale-near-close`, no `fix/support-scan-missed-verifier`,
  no `feat/adaptive-parity`. `fix/paper-exit-slippage` exists but is the old
  merged brief-02 branch, not the open-issues-03 one. So B1 is Muse's to
  build and B2 builds the never-built parity design here.
- `feat/adaptive-parity` does not exist. The parity design lives only in the
  approved Review 2 decision (P2+E1, amendments a–d, conditions 1–6); see §4.2.
- `render_schema.sql` (lease / job-runs / runtime-kv tables) exists only on
  `feat/render-deploy`, NOT on main. A3 must mirror its table/column names
  from that branch; see Q1 in §6.
- No "R" definition exists anywhere in the codebase (searched `R-multiple`,
  `risk_unit`, `1R` — no hits). The definition in §4.4 is proposed here.
- `strategy_indicators.py` has no shared indicator helpers (no `rsi()` /
  `atr()` / `sma()` functions; each `strategy_N` is inline). Candidate
  indicators (B3) must therefore be computed inside the new candidate module,
  mirroring the frozen functions' constructions — never by editing the frozen
  file.

## 1. Audit table — Support Agent (branch A)

Paths are repo-root-relative. All line numbers read on `origin/main` `33e6b77`.

| File:line | What it is | What Phase 0 found |
|---|---|---|
| `automation/paper-trading/support_agent.py:26` | `VALID_SEVERITIES` | SEV1–SEV4 only; SQL severity mapping at `:190` (`LEGACY_SEV_TO_SQL`: SEV1→CRITICAL, SEV2→ERROR, SEV3→WARN, SEV4→INFO). A2 maps CRITICAL↔SEV1. |
| `automation/paper-trading/support_agent.py:48` | `ERROR_TAXONOMY_PATTERN` | Regex the taxonomy detector matches against. |
| `automation/paper-trading/support_agent.py:90-98` | `missing_policy_values()` | Emits `policy_value_missing` for NULL policy values; `support_policy.json` NULLs today: `incomplete_lifecycle_timeout_minutes`, `feed_stale_max_hours`, `dashboard_probe_timeout_seconds`. |
| `automation/paper-trading/support_agent.py:834` | `run_detectors()` | v2 detectors incl. `box_health`, `dashboard_health`, `feed_http_403`, `policy_value_missing`. |
| `automation/paper-trading/support_agent.py:1138` | `run_v3_production_detectors()` | v3 detector set; each detector calls `emit()` at `:1174`. |
| `automation/paper-trading/support_agent.py:1174` | `emit()` | Writes via `append_finding()` / `update_finding_evidence()`; `details` dict is the evidence payload. A2 adds a one-line `action` string into `details` here (wiring only — no detector logic changes). |
| `automation/paper-trading/support_agent.py:1213` | `scan_missed` SEV2 | Already exists (brief's "already exist" list confirmed). |
| `automation/paper-trading/support_agent.py:1349` | `monitor_staleness` SEV2 | Already exists. |
| `automation/paper-trading/support_agent.py:1359` | `position_marks_stale` SEV2 | Already exists. |
| `automation/paper-trading/support_agent.py:1368` | `lock_blocked_gap` SEV3 | Already exists. |
| `automation/paper-trading/support_agent.py:1413-1423` | `error_taxonomy_gap` SEV3 | Counts events matching `ERROR_TAXONOMY_PATTERN` or with outcome `unclassified_condition`; finding details carry `unclassified_count`, `events`, `last_at` — but NOT per-type counts (gap for A1). |
| `automation/paper-trading/support_agent.py:1486` | `quote_field_regression` SEV1 | Already exists. |
| `automation/paper-trading/support_agent.py:1555` | `fill_sizing_anomaly` SEV1 | Already exists. |
| `automation/paper-trading/support_agent.py:1642` | `signal_to_fill_latency` SEV3 | Already exists. |
| `automation/paper-trading/support_agent.py:1678` | `exit_path_unexercised` SEV4 | Already exists. |
| `automation/paper-trading/support_agent.py:1700` | `eod_missed` SEV1 | Already exists. |
| `automation/paper-trading/support_agent.py:1717` | `reconstruction_failure` SEV1 | Already exists. |
| `automation/paper-trading/support_agent.py:1843` | `run_virtual_clock_replay()` | Replays 5 ticks (09:05/12:30/14:20/15:40/16:35 IST) through `run_v3_production_detectors`; asserts zero duplicate findings on repeat ticks. A1/A2 tests use this. |
| `automation/paper-trading/support_agent.py:1882` | `session_ledger_fills()` | Ledger fill prices only; never falls back to order/quote prices. |
| `automation/paper-trading/support_agent.py:1925` | `session_report()` | Builds the per-session report. A4's compact JSON is a new module, not an edit here. |
| `automation/paper-trading/audit.py:11` | `REASONS` allow-list | The only reasons `clean()` keeps. |
| `automation/paper-trading/audit.py:33` | `clean()` | Rewrites any `reason`/`reasons`/`error_code` NOT in `REASONS` (or matching `SQLITE|TURSO|HTTP|HELPER_…`, `market_data_error_…`, `auth_circuit_open_…`) to `'unclassified_condition'`. **This is the root cause of the 23 unclassified 19-Nov events.** |
| `automation/paper-trading/automation.py:379` | `session_outage_recorded` | Emits `reason=kind` where kind ∈ {`no_monitoring`, `monitor_coverage_low`, `scan_missing`, `eod_missing`}. None are in `REASONS` → rewritten. |
| `automation/paper-trading/automation.py:495,503` | `host_freeze_detected` | Emits `reason='host_vm_paused'` (fields `started`/`stopped`/`age_seconds`/`frozen_seconds`/`missed_checks`/`count`). Not in `REASONS` → rewritten. |
| `automation/paper-trading/sessiond.py:110` | `sessiond_wake` | Emits `reason='box_resumed_catching_up'` (field `frozen_seconds`). Not in `REASONS` → rewritten. |
| `live/support_runner.py` (250 lines) | Support runner | Reads `config.json` namespace, builds snapshot, merges `engine.jsonl`, writes `findings.sqlite` + report dir, `summary.json` + `SUMMARY.md`, runs `support_finding_verify.py`. Hard-coded `shutil.disk_usage('/workspace')`; no CRITICAL block, no compact JSON, no Render awareness, no action strings. A3/A4 wire new modules here (read-only additions). |
| `live/ro_client.py` (35 lines) | `TursoRO` | SELECT-only client; `batch()` raises `PermissionError('turso_read_only_client')`. A3 reuses this — Support stays strictly read-only. |
| `live/support_finding_verify.py` (978 lines) | Verifiers | `check_scan_missed_events`, `_monitor_gaps`, `check_worker_or_staleness_events`, `query_events`/`query_fills`/`query_signals`, `load_log_events`. |
| `automation/paper-trading/support_policy.json` | Policy | Thresholds: `lock_blocked_gap_seconds` 180, `quote_timeout_seconds` 30, `support_self_stale_seconds` 600, `box_disk_free_min_pct` 10, `box_clock_drift_max_seconds` 5. Expectations: scan 08:45 + 15m grace, worker 09:15–15:30 grace 120s, EOD 16:15 + 20m. Episode escalation: 600 blind seconds / 3 occurrences. **Frozen — never edit.** |
| `automation/paper-trading/config.json` | Runtime config | `quote_max_age_seconds` 60 (the number A2's `fill_on_stale_quote` reuses), `worker_stale_seconds` 90 (the brief's "90 s monitor_staleness value"), `position_mark_stale_seconds` 180. |

### A1 — the 23 unclassified 19-Nov events: classification decided

Six new reasons go into the proposal doc (NOT into `audit.py` — that file
is frozen for this brief; the proposal doc is the only edit point):

| Reason (as emitted) | Event type | New classification | Severity rule |
|---|---|---|---|
| `host_vm_paused` | `host_freeze_detected` | host freeze / VM pause | CRITICAL if `frozen_seconds` ≥ 600 (the episode-escalation blind-seconds value already in policy) else ERROR; `frozen_seconds` missing → `not_evaluable` / `policy_value_missing` path, never invented |
| `box_resumed_catching_up` | `sessiond_wake` | box resume / catch-up | ERROR (resume is recovery, not the outage itself) |
| `scan_missing` | `session_outage_recorded` | missed trading session | CRITICAL (a trading session was missed) |
| `eod_missing` | `session_outage_recorded` | missed trading session | CRITICAL (a trading session was missed) |
| `no_monitoring` | `session_outage_recorded` | monitoring gap | ERROR |
| `monitor_coverage_low` | `session_outage_recorded` | monitoring gap | ERROR |

`support_taxonomy.py` (new) implements: fingerprint = stable key from
(normalised reason, event type) with variable parts (timestamps, ids, counts)
stripped before hashing; per-type counts in finding details (closing the
`error_taxonomy_gap` gap at `support_agent.py:1413-1423`); grouping-version
field so old events are never re-fingerprinted (see §2, Sentry).

## 2. Sources — monitoring (what A1–A4 borrow, and what they deliberately do not)

1. **Prometheus / Alertmanager — severity labels, grouping, inhibition,
   runbook annotations.**
   https://prometheus.io/docs/alerting/latest/alertmanager/ (and the rule
   format: `labels: {severity: page}`, `annotations: {summary, runbook_url}`).
   Borrowed: labels route, annotations tell the human what to do (A2's
   one-line `action` is the annotation); grouping merges alerts sharing
   labels into one notification (A1's per-type counts); inhibition mutes a
   warning while the related page for the same service is active (proposed
   extra E2, §5). Not borrowed: Alertmanager itself — no new service.
2. **Healthchecks.io — dead-man's switch / heartbeat with grace time.**
   https://healthchecks.io/docs/ (checks: period + grace; states New / Up /
   Late / Down; a check goes Down only after period + grace with no ping).
   Borrowed: the framing that `scan_missed` / `eod_missed` / `monitor_staleness`
   are absence-of-success detectors — silence is the signal, and the grace
   values already in `support_policy.json` (scan 15m, worker 120s, EOD 20m)
   are the grace times. No code change; documented in A2/A4. Not borrowed:
   an external ping service — detection stays in-process.
3. **Kubernetes Lease API — leader election, fencing, epoch.**
   https://kubernetes.io/docs/concepts/cluster-administration/coordinated-leader-election/
   (`coordination.k8s.io/v1` Lease: `holderIdentity`, `acquireTime`,
   `renewTime`, `leaseDurationSeconds`, `leaseTransitions`; optimistic
   concurrency on `resourceVersion` — only one contested update wins).
   Borrowed: the lease shape A3 reads — `paper_runner_lease` on Render has
   the same fields (`owner_id`, `owner_kind`, `epoch`, `acquired_unix`,
   `expires_unix`, `heartbeat_at`); the `epoch` is the fencing token: a stale
   owner (old epoch) must never be treated as live. A3 reports
   `lease_owner`, `lease_epoch`, `lease_age_seconds`, `lease_expired`
   read-only; it never acquires, renews, or writes a lease.
4. **Sentry — error grouping / fingerprinting.**
   https://docs.sentry.io/concepts/data-management/event-grouping/ and
   https://docs.sentry.io/concepts/data-management/event-grouping/fingerprint-rules/
   ("All versions consider the `fingerprint` first, the stack trace next,
   then the exception, and then finally the message"; grouping changes ship
   as a new version applied only to new events — old events are never
   re-fingerprinted).
   Borrowed: fingerprint = stable key from the parts that stay put when the
   same failure repeats (reason + event type, variables stripped); message
   text is the weakest signal and is never the key; the grouping algorithm
   is versioned and old events keep their old grouping. This is A1's design.
5. **Google SRE Workbook — alert severity / paging policy (background).**
   https://sre.google/workbook/alerting-on-slos/ — two severities that page
   and the rest that ticket is enough for most teams; everything that pages
   must have an owner and a runbook. Supports A2's rule that every CRITICAL
   carries a one-line action (the runbook in one line), and that SEV1 maps
   1:1 to CRITICAL.

## 3. Audit table — adaptive engine (branch B)

| File:line | What it is | What Phase 0 found |
|---|---|---|
| `automation/paper-trading/adaptive_engine.py:63-71` | `BASELINE_PARAMS` | Strategies 1–7 only. B3 must NEVER add 101–105 here. |
| `automation/paper-trading/adaptive_engine.py:168-174` | `HARNESS_FILES` | Files in the harness hash; `adaptive_engine.py` is in it. Any B-file edit changes the hash — flag the new hash in the §8 report. |
| `automation/paper-trading/adaptive_engine.py:235` | `evaluate_parameterized_strategy()` | Baseline params delegate to `si_base.strategy_{s}` for 2–7; strategy 1 inline — identical to `engine.py:478`. |
| `automation/paper-trading/adaptive_engine.py:398` | `slippage_fill_paise()` | Mirrors `fees.mjs` `executionSlippage()` (see below). |
| `automation/paper-trading/adaptive_engine.py:451` | `estimate_exit_haircut_paise()` | Applies NO slippage when ADV unknown — **B1 mirror point #2**. |
| `automation/paper-trading/adaptive_engine.py:571` | `classify_discordant_trades()` | Discordance classification used by the parity comparison. |
| `automation/paper-trading/adaptive_engine.py:900` | `simulate_track_window()` | Exit path ~1001–1021: `adv = lagged_adv_paise(...)`; if adv, tiered slippage; else the fill stays at the reference price and the closed trade records `exit_slippage_applied: bool(adv)` — **B1 mirror point #1**. |
| `automation/paper-trading/adaptive_engine.py` | `compute_harness_hash()` / `verify_harness_hash()` | Hash over git blob hashes of `HARNESS_FILES`; test at `automation/paper-trading/tests/test_adaptive_phase2.py:385` (`test_harness_hash_is_g05_definition_over_git_blob_hashes`). No stored hash constant to update — the new hash is reported, not patched in. |
| `automation/paper-trading/strategy_indicators.py:5-44` | `strategy_2` (Supertrend) | ATR10, mult 3.0; Wilder-style smoothing `(atr[i-1]*9 + tr[i])/10`. **Frozen.** |
| `automation/paper-trading/strategy_indicators.py:45-70` | `strategy_3` (RSI14) | RSI via Wilder smoothing `(13*ag[i-1] + up[i])/14`, seeded with the mean of bars 1–15; `sma200`, `sma5`; entry `close > sma200 & rsi14 < 40`; exit `rsi14 > 60 | close > sma5`. **Frozen — B3's RSI(2) mirrors this construction with period 2.** |
| `automation/paper-trading/strategy_indicators.py:71-134` | `strategy_4/5/6/7` | S4 EMA20/50 cross + SMA200; S5 Bollinger 20/2 reversion + SMA200; S6 MACD 12/26/9 + SMA50; S7 252-day high within 10%, exit close < SMA50. **Frozen.** |
| `automation/paper-trading/engine.py:478` | Strategy 1 (Donchian) | Entry close > 20-day high, exit close < 10-day low. **Frozen.** |
| `live/adaptive_daily.py:271-300` | Spawn loop | Iterates `ae.BASELINE_PARAMS`; Track A replica spawn has `candidate_kind='BASELINE'`, role `'track_a_replica_for_parity'`. B3 must not add to this loop's baseline path. |
| `automation/paper-trading/adaptive_policy.json` | Policy | `fast_swing_strategies` [3,5], `trend_strategies` [1,2,4,6,7]; warm-start 60 / 100–120 (default 110); target windows 45 / 75; extension 15; `max_track_b_challengers` 3; `parity_equity_tolerance_paise` 100 (old D3 basis — frozen file, B2 must NOT use it for pass/fail). **Frozen.** |
| `live/parity_record.py` (138 lines) | `record_for_session()` | **R4 hold confirmed**: returns `{'status':'not_evaluable','reason':'parity_basis_pending_review'}`, writes no row. `compare_session_parity()` exists with exact-2dp price matching and equity diff vs 100 paise tolerance; R3 strategy filter already present (`int(f['strategy']) != int(strategy)`). B2 replaces the comparison basis and lifts the hold per §4.2. |
| `live/track_a_parity.py` (211 lines) | `compare()` | Compares sleeve equity copy + `trade_parity` via `adaptive_parity` rows; read-only (Turso RO + local sqlite read). |
| `automation/paper-trading/adaptive_observation.py:74` | `record_parity()` | D3 basis: exact match + `|equity diff| ≤ parity_equity_tolerance_paise` → `parity_ok`. B2 must replace this basis per the approved design (price/equity informational, qty reported not matched, matched/live-only/replica-only counts, equity change-based, idempotent replace, only the `adaptive_parity` table). |
| `automation/paper-trading/fees.mjs` | `executionSlippage()` | bps = base + 2000 × (order/ADV); base 8 bps ≤ ₹20 cr else 5; stress ×1.5; half-up paise rounding; reads `capitalPolicy` from `./capital_policy.json`. |
| `automation/paper-trading/db.mjs:~128-131` | SELL branch | `slip={applied:false, rule:'exit_slippage_unavailable'}`; if `laggedAdv()` ok → `slipOf()`; else `slip.reason` = `lagged_adv_history_short` or `adv_below_floor`. |
| `automation/paper-trading/capital_policy.json` | Slippage inputs | `slippage_base_bps_through_20cr` 8, `slippage_base_bps_above_20cr` 5, `slippage_tier_threshold_inr` 200000000, `slippage_participation_multiplier_bps` 10, `participation_rate` 0.005, `slippage_stress_multiplier` 1.5, `adv_floor_inr` 200000000. **Frozen — B1 reads the 8 bps base, never edits it.** |
| `automation/paper-trading/tests/test_adaptive_phase2.py:385` | Harness hash test | See `adaptive_engine.py` row above. |
| `automation/paper-trading/tests/test_adaptive_independent_audit.py:67` | Mirror test | Slippage mirror test — B1 must update and explain any existing test that asserted the old (no-slippage-when-ADV-unknown) behaviour. |
| `live/tests/test_parity_record.py` | Parity tests | Hold tests + pure-comparison tests on the D3 basis (`test_one_fill_price_mismatch_is_not_a_match`, `test_equity_diff_beyond_tolerance_is_parity_ok_zero`, `test_full_match_pure_comparison_agrees`). B2 updates these to the approved basis and explains each change. |
| `docs/proposals/exit-slippage-fallback.md` (6 KB) | B1 proposal | Option (a) DECIDED by the user: base 8 bps fallback when ADV unknown. Worked example: SELL 500 @ ₹110 → ₹109.91 (8 bps = 8.8 paise → 9 paise half-up). |

### 4.1 B1 — exit-slippage fallback, option (a) (decided)

When ADV is unknown at exit, apply the base 8 bps slippage (the
`slippage_base_bps_through_20cr` value read from `capital_policy.json`,
never hard-coded) instead of zero slippage. Mirror points: the live
`db.mjs` SELL branch (rule becomes `exit_slippage_base_fallback`,
`applied:true`) and `adaptive_engine.py:451` + the `simulate_track_window`
exit path (~1001–1021, `exit_slippage_applied` becomes true for fallback
fills). The fallback must be distinguishable in every record
(`rule`/`applied` fields), so a later audit can tell base-fallback fills
from ADV-tiered fills. No change to `fees.mjs` `executionSlippage()` itself
(it already computes base-only correctly when ADV is absent — the gap is
only that callers skip it).

### 4.2 B2 — parity, approved P2+E1 (never built before)

Approved by the user 2026-11-18 21:37 IST (Review 2). Match key includes
strategy — (strategy, symbol, side, session day); qty is REPORTED (live qty,
replica qty, difference), not a hard match condition; besides the OHLC-range
plausibility check, both prices and the bps difference are recorded as
information; E1 has NO tolerance number from Muse — record the change
difference in ₹ and %, leave pass/fail off until the user sets a tolerance;
compare on the fill day (signal D → fill D+1, compare on D+1); report
matched / live-only / replica-only counts; rows only for reconciled +
SIMULATED days, idempotent, only `adaptive_parity` in the sidecar DB, no
policy numbers. Conditions 1–6 from the Review 2 decision apply (see
`adaptive-engine-01-review-2__1.md`, "Parity decision"). Bar-level offline
parity first, then lift the hold in `live/parity_record.py`. If anything
conflicts with the code, stop and ask; never invent a tolerance number.

## 4. Candidate strategies (B3) — exact rule text

All five are **long-only, daily-close, shadow challengers only**
(ids 101–105). None enters `BASELINE_PARAMS`, `adaptive_policy.json`,
`strategy_rules.json`, or the `adaptive_daily.py` spawn loop. Each has its
own warm-start (recorded in the registry, §5 extra E4). Exits are
indicator-based like the frozen seven; there is no fixed stop, which is why
"R" is defined via ATR (§4.4). All prices are closes unless stated.

### 101 — Minervini Trend Template (Minervini 2013)

Primary source: Mark Minervini, *Trade Like a Stock Market Wizard*
(McGraw-Hill, 2013) — the Trend Template, eight criteria, all must hold.
Secondary write-up (criteria quoted verbatim):
https://stonks.run/trend-template.html

- Entry (all computable criteria true at the close):
  1. close > SMA150 AND close > SMA200.
  2. SMA150 > SMA200.
  3. SMA200 trending up: SMA200 today > SMA200 20 sessions ago (at least 1 month).
  4. SMA50 > SMA150 AND SMA50 > SMA200.
  5. close > SMA50.
  6. close ≥ 1.30 × 52-week low (252-session lowest low).
  7. close ≥ 0.75 × 52-week high (within 25% of the 52-week high).
  8. Relative Strength rank ≥ 70 — **NOT computable from OHLCV** (it is a
     rank against every other stock, from Investor's Business Daily). The
     challenger implements criteria 1–7 and records criterion 8 as
     `not_evaluable` with reason `rs_rank_unavailable`; the challenger is
     flagged "partial template (7/8)". See Q2.
- Exit: close < SMA50.
- Warm-start: 252 sessions.

### 102 — Connors RSI(2) mean reversion (Connors & Alvarez 2008)

Primary source: Larry Connors & Cesar Alvarez, *Short Term Trading
Strategies That Work* (2008) — 2-period RSI, tested on US index/ETF data
1995–2007. Rule summary (StockCharts):
https://swingfolio.com/blog/rsi-2-trading-system-strategy and
https://www.luxalgo.com/library/concept/rsi-2/

- Filter: close > SMA200 (long trades only above the 200-day average).
- Entry: RSI(2) closes < 10. RSI built with Wilder smoothing exactly as
  frozen `strategy_3` builds RSI14 (`strategy_indicators.py:45-70`), with
  period 2 (seed = mean of the first 2 bars' gains/losses).
- Exit: close > SMA5.
- Note: the original has NO stop-loss (risk carried by position size and
  the fast SMA5 exit). The stricter variant RSI(2) < 5 is recorded as an
  alternative, not implemented. No stop is added — adding one would not be
  the published rule.
- Warm-start: 200 sessions (SMA200 dominates).

### 103 — Bollinger Squeeze breakout, Method I (Bollinger 2001)

Primary source: John Bollinger, *Bollinger on Bollinger Bands*
(McGraw-Hill, 2001) — %b, BandWidth, the Squeeze, Method I (volatility
breakout). Definitions: https://synapsetrading.com/john-bollinger-bands/
and https://tradingcompendium.com/en/technical-indicators/bollinger-bands

- Bands: middle = SMA20; upper/lower = middle ± 2 standard deviations
  (same 20/2 construction as frozen `strategy_5`; the standard-deviation
  convention is taken from `strategy_5`'s code, not re-decided here).
- BandWidth = (upper − lower) / middle. %b = (close − lower) / (upper − lower).
- Squeeze: BandWidth at its lowest level in 126 sessions (6 months) —
  Bollinger's own definition ("lowest level in 6 months"). No percentile
  threshold; see Q5.
- Entry: squeeze present AND close > upper band (the breakout gives the
  direction; the squeeze itself has none — Bollinger is explicit about this).
- Exit: close < middle band (SMA20).
- Note: the head fake (a short wrong-way move near the end of a squeeze) is
  acknowledged in the book and accepted as part of the rule.
- Warm-start: 126 sessions.

### 104 — Wilder DMI / ADX (Wilder 1978)

Primary source: J. Welles Wilder Jr., *New Concepts in Technical Trading
Systems* (1978) — Directional Movement System (+DI, −DI, ADX), 14-period
default. Construction: https://www.candelacharts.com/glossary/adx-dmi-system
and https://therighttrader.com/learn/indicators/average-directional-index

- Per bar: UpMove = high − prior high; DownMove = prior low − low. Only the
  larger counts, and only if positive: +DM = UpMove (else 0), −DM = DownMove
  (else 0).
- Smooth +DM, −DM and True Range with Wilder's recursive average over 14
  (same `(n−1)/n` construction as `strategy_3`'s RSI smoothing).
- +DI = 100 × smoothed +DM / smoothed TR; −DI = 100 × smoothed −DM /
  smoothed TR. DX = 100 × |+DI − −DI| / (+DI + −DI). ADX = Wilder-smoothed
  DX over 14.
- Entry (long): +DI crosses above −DI AND ADX > 25.
- Exit: −DI crosses above +DI.
- Note: Wilder's extreme-point rule (enter only when price passes the
  crossover bar's high) is NOT applied — the daily-close harness enters at
  the close. Recorded here as a stated simplification, not a silent change.
- Warm-start: 200 sessions (14-period smoothing needs a seed, but the
  +DI/−DI history is only stable after a full trend sample; 200 is
  conservative and matches the SMA-free design — no SMA is used).

### 105 — Jegadeesh–Titman 12–1 momentum, adapted (Jegadeesh & Titman 1993)

Primary source: Narasimhan Jegadeesh & Sheridan Titman, "Returns to Buying
Winners and Selling Losers: Implications for Stock Market Efficiency",
*The Journal of Finance* 48(1), 65–91 (1993). 16 formation/holding
combinations (3/6/9/12 months); best was 12-month formation / 3-month
holding (~1.31%/month winner-minus-loser); the 6-month/6-month version
(~1%/month, 12.01%/year compounded 1965–1989) became the standard.
Summary: https://foxholm.com/q/research/jegadeesh-titman-momentum/ ;
full text: https://floridapsc.com/library/FILINGS/2013/07457-2013/Support/Gulf's%20response%20to%20OPC's%201st%20POD,%20No.%2057/Jegadeesh%20and%20Titman%201993%20(140-167).pdf

- The paper's rule is CROSS-SECTIONAL (rank all NYSE/AMEX stocks, buy the
  top decile, sell the bottom decile). Track A's harness simulates one
  symbol at a time, so a faithful single-name version does not exist. The
  implemented rule is an adaptation — see Q4:
- Entry: at each monthly rebalance, rank the Track A universe by its 12–1
  month return (return over sessions −252..−21, skipping the most recent
  21 sessions to avoid the short-term reversal the paper documents at 1-week
  / 1-month horizons); enter when the symbol's rank is in the TOP DECILE
  of the universe that day.
- Exit: at the 3-month (63-session) hold expiry, OR when the symbol falls
  out of the top 3 deciles at a monthly rebalance — whichever comes first.
- Warm-start: 252 sessions.
- This is the only candidate that needs the full panel (universe ranks), not
  just one symbol's bars; the registry records that dependency.

## 4.4 "R" — definition (proposed, needs senior confirmation — Q3)

No "R" exists in the codebase. Proposed definition for the B4 compare report:

- **1R per trade = qty × 2 × ATR(14) at entry, in paise.** ATR(14) is the
  Wilder-smoothed True Range over 14 sessions at the entry close.
- **R-multiple per trade = net P&L (paise) ÷ 1R.**
- **Portfolio R = Σ net P&L ÷ Σ 1R** over the compared window; expectancy
  in R = mean R-multiple across closed trades.
- Why ATR and not entry−stop: the frozen seven have NO fixed stops (their
  exits are indicator-based), so entry−stop is not computable for them and
  a compare report must use one definition for baseline and challengers
  alike. ATR is already computed in the codebase (Supertrend ATR10,
  `strategy_indicators.py:5-44`) and is the standard per-trade risk proxy.
- Rejected alternative: 1R = qty × (entry − stop) — requires a stop per
  strategy, which is a policy decision the senior would have to make for
  all 12 strategies. Flagged as Q3.

## 5. Ranked list (implementation order)

1. **A1 taxonomy** — root cause known and proven (`audit.py:33`); new module
   `support_taxonomy.py` + proposal-doc additions; per-type counts close the
   `error_taxonomy_gap` gap.
2. **A2 severity + actions** — new module `support_severity.py`; one-line
   `action` per CRITICAL/ERROR finding, wired through `emit()` details;
   reuses existing numbers (`quote_max_age_seconds` 60, worker staleness 90s).
3. **B1 slippage fallback** — user-decided option (a); smallest change,
   highest certainty; mirror points known (§4.1).
4. **A3 Render / namespace awareness** — new module `support_render.py`,
   read-only via `TursoRO`; lease fields + namespace from `config.json`;
   never imports `render_*` modules; never writes.
5. **A4 compact summary JSON** — new module `support_summary.py`; depends on
   A1–A3 outputs (counts by severity, by taxonomy, lease state).
6. **B2 parity** — bar-level offline parity first, then lift the hold in
   `live/parity_record.py` per §4.2; update the D3 tests in
   `live/tests/test_parity_record.py` and explain each change.
7. **B3 candidates 101–105** — new registry module only; frozen-seven golden
   file made on `origin/main` BEFORE editing anything; shadow only.
8. **B4 compare report** — side-by-side baseline vs challengers on the same
   window, in R-multiples (§4.4) plus counts; no new data, no live effect.

## 5b. Proposed extras (not in the brief — for the senior to approve or drop)

- **E1. Fingerprint versioning (A1).** The taxonomy fingerprint algorithm
  carries a version; old events keep their old grouping (Sentry rule, §2
  source 4). Prevents a re-grouping from reshuffling history.
- **E2. Inhibition (A2).** While a CRITICAL host-freeze / session-outage
  finding is OPEN for a session, suppress duplicate downstream WARN/INFO
  findings for the same session and record the suppressed count
  (Alertmanager inhibition, §2 source 1). Reduces noise; the suppressed
  evidence is still stored.
- **E3. Dead-man's-switch framing (A2/A4, docs only).** State in the report
  that `scan_missed` / `eod_missed` / `monitor_staleness` are
  absence-of-success detectors with the policy grace times (Healthchecks
  pattern, §2 source 2). No code change.
- **E4. Per-candidate warm-start in the registry (B3).** 252 (101, 105),
  200 (102, 104), 126 (103) sessions; the harness skips pre-warm-start days
  explicitly and records the skip, instead of silently producing no signals.
- **E5. B4 extra columns.** R-multiples, max drawdown in R, win rate — all
  computed from the same simulated trades; no new data source.
- **E6. A4 provenance fields.** `generated_at_ist`, `namespace`, `branch`,
  `harness_hash` (branch B) in the summary JSON.

## 6. §6 questions (stop-and-ask — none blocks starting; all have a fallback)

- **Q1. A3 lease names.** `paper_runner_lease` / `paper_job_runs` /
  `paper_runtime_kv` and their columns are defined in `render_schema.sql`,
  which lives only on `feat/render-deploy`, not on main. Confirm those are
  the deployed names A3 should read. Fallback: mirror the names from the
  render-deploy branch and assert them in a test; if the deployed schema
  differs, A3 reports `not_evaluable` with reason `lease_schema_mismatch`
  rather than guessing.
- **Q2. Candidate 101, criterion 8 (RS rank ≥ 70).** No RS-rank feed exists.
  Proceed with the 7/8 partial template, criterion 8 `not_evaluable`
  (`rs_rank_unavailable`)? (Proposed: yes.)
- **Q3. "R" definition.** Confirm the proposed ATR(14)-based 1R (§4.4), or
  prefer entry−stop (which needs a stop per strategy — a policy decision)?
  Fallback until answered: report both R-multiples (ATR-based) and raw
  paise P&L; no pass/fail on R.
- **Q4. Candidate 105 adaptation.** Confirm the top-decile-of-universe
  adaptation (§4, 105) is acceptable as the single-name version of a
  cross-sectional paper rule. Fallback: implement it as specified and flag
  it "adapted, not the paper's exact rule" in every report.
- **Q5. Candidate 103 squeeze threshold.** Bollinger's own definition is
  BandWidth at its lowest in 6 months (126 sessions). Confirm no percentile
  threshold (e.g. < 5th percentile) is wanted instead. Fallback: lowest-in-
  126 as specified.
- **Q6. (Standing rule, no answer needed.)** Anything needing a new
  threshold/policy number stays `not_evaluable` with reason
  `policy_value_missing` until the senior sets it. No number is invented
  anywhere in this work.

## 7. Gate plan (§7 — per branch, on a clean clone of the LOCAL branch)

- Record main baselines first (Track A IST/UTC, live, vitest, lint,
  check:secrets, harness hash on `33e6b77`).
- Branch A checks: G0–G7 + G7a–e (support checks). Branch B checks: G0–G7 +
  G7f–i (adaptive checks), including the new harness hash (flag it, do not
  patch it in).
- Tests first for every task; every new test must fail without its change
  (G6 mutation/revert check).
- Frozen files stay byte-identical: `capital_policy.json`,
  `freeze_lock.json`, `charge_policy.json`, `charge_rates.json`,
  `strategy_rules.json`, `strategy_indicators.py`, `support_policy.json`,
  `support_policy_lock.json`, `adaptive_policy.json`. Also frozen for this
  work: `audit.py` and every Track A runtime file (proposal doc only).
- No hard-coded `/workspace` paths in new code/tests (skip cleanly when
  absent). No secrets. No force-push, no merge, no PRs, no deploy.
- Known quirks (do not re-learn): clone under `~/workspace` (NOT /tmp —
  tmpfs is wiped on service restart); venv OUTSIDE the clone
  (`check:secrets` crashes on a venv `lib64` symlink); `npm run build`
  before `check:secrets` on a fresh clone; Track A full suite ~20–25 min
  per TZ — iterate on single files, full G2 once per loop; count gate loops.
- Gate clones: `~/workspace/ast-gate-a` / `~/workspace/ast-gate-b`.
- If anything fails, fix and re-run the whole gate for that branch.

## 8. Decisions already made (do not re-decide)

- B1: option (a), base 8 bps fallback — user-decided.
- B2: P2+E1 with amendments a–d and conditions 1–6 — user-approved
  2026-11-18 21:37 IST; E1 tolerance is NOT a number Muse chooses.
- Support stays strictly read-only; no broker orders, no writes to
  `paper_v1`/`demo`, no merges to main, no force-push, never edit
  `/workspace/paper-trading` (the box path — this work is in
  `~/workspace/github/Trading-dashboard` only).
- Nothing is pushed by the worker — the parent handles push approvals
  with the user. No PRs, no deploy, no touching production Turso/Fyers/
  Render, no touching `feat/render-deploy` or anything live (Fri 20 Nov is
  the Render trial day; Grok is handling it).

## 9. Grok's §6 answers (received 2026-11-20 ~08:58 IST via VVS — these replace the §6 fallbacks above)

- **Q1. A3 lease names — YES.** `paper_runner_lease`, `paper_job_runs` and `paper_runtime_kv` are the real deployed names, already migrated in production Turso. Keep the test and the `lease_schema_mismatch` fallback anyway.
- **Q2. Candidate 101, criterion 8 — do NOT drop it.** Compute RS rank from bars already held: percentile (0–100) of each stock's 252-session return within the 692 eligible universe on the same bar. Label it `derived_rs_rank (in-universe)`, NOT IBD's rating. If a stock's history is missing, criterion 8 = `not_evaluable` (`rs_rank_unavailable`) for that stock only.
- **Q3. "R" definition — ATR-based, with 1R = qty × 2 × ATR(14) at entry (in paise)**, to match the brief's default stop of entry − 2×ATR(14). R-multiple = net P&L ÷ 1R. Also show raw paise P&L. No pass/fail on R. (This supersedes §4.4's 1R = qty × ATR(14).)
- **Q4. Candidate 105 — CONFIRMED.** Top decile of the universe by 12-month return skipping the latest month, rebalanced monthly, flagged "adapted, not the paper's exact rule" everywhere.
- **Q5. Candidate 103 squeeze — lowest BandWidth in 126 sessions** (Bollinger's own rule). No percentile threshold.
- **Standing rule CONFIRMED:** no invented numbers; anything without a policy value stays `not_evaluable`.
