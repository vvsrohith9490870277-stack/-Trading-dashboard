# Follow-ups from brief-02 Tasks 1–5 — proposal (PROPOSE ONLY, not implemented)

Status: **PROPOSAL ONLY — no code changed.** Each item names the branch and file it belongs to; the senior decides the order.

## 1. Official NSE price-band ingestion for Track A (from Task 1)

**Problem, with evidence.** Circuit detection (`automation/paper-trading/engine.py`, `market_guard`) now records how a lock was decided (`circuit_source`: `explicit`, `depth_band`, `price_bands`, `book_heuristic`, `book_heuristic_unconfirmed`), in this order: explicit circuit flag → depth band (`upper_band`/`lower_band` on the quote) → `paper_price_bands` row → book heuristic. The `price_bands` source is only as good as the rows in `paper_price_bands`, and those rows are not populated from the exchange: the only live band feed is the Fyers depth quote (`ohlcv_flag=1`), confirmed on demand by `_confirm_circuit_band` in `_monitor`. There is no ingestion of the official NSE daily price bands, so a symbol whose depth quote carries no band falls through to the book heuristic and locks unconfirmed.

**Options.**
- (a) Ingest the official bands from the NSE bhavcopy already downloaded for closes, if it carries the band columns — one ingestion path, no new source. *Recommended, if the columns are there.*
- (b) Ingest from a separate NSE price-band file/circular — a new source to fetch, store and verify.
- (c) Keep depth-only bands — the `book_heuristic_unconfirmed` lock stays as the fallback, and false locks are still avoided (the Task 1 fix), but genuinely locked symbols without a depth band are never confirmed from exchange data.

**Acceptance if done:** `paper_price_bands` rows exist for the traded universe on each session; the `price_bands` circuit_source fires from exchange data in a test; the depth-band confirm step stays as a fallback only. Do this on `fix/paper-circuit-detection` (or a new branch from it). Do not touch adaptive files.

## 2. Adaptive mirror of the exit-slippage reason (from Task 2)

**Problem, with evidence.** Task 2 Part A (branch `fix/paper-exit-slippage`) changed only `automation/paper-trading/db.mjs`: the SELL branch now names the reason when slippage cannot be applied (`slippage_reason`: `adv_below_floor` / `lagged_adv_history_short`, in the decide payload and the returned fill JSON). The adaptive engine mirrors the same model in `automation/paper-trading/adaptive_engine.py:398` (`slippage_fill_paise()`, used at lines 467 and 1006) and has no equivalent reason field, so a live fill and its replica can disagree silently about why slippage was skipped. The Part B fallback (options in `docs/proposals/exit-slippage-fallback.md`, awaiting the senior's choice) must also be applied to both sides once chosen, or live and replica will diverge on the same order.

**Recommendation.** Mirror the reason naming (and, once chosen, the Part B option) in `adaptive_engine.py` on the adaptive branch, after the merge OK for brief 01 — not on a brief-02 branch. No adaptive file was touched in brief 02.

## 3. Test logs, locks and the skipped test (from Task 4)

**Problem, with evidence.** The clean inventory (origin/main, 521 tests) shows tests also write `logs/tests.jsonl`, `logs/engine.jsonl`, `logs/db-helper.log`, `logs/monitor-worker.log`, `logs/audit-spool/<hash>/replay.lock` and lock files `engine.lock`, `exit_selftest.lock`, `guardian.lock`, `monitor.lock`, `reference_feeds.lock` into the runtime tree. Brief 02 Task 4 covered only `out/` and `tests/*.sqlite`; logs and locks were listed, not moved. The 1 skipped test is `test_adaptive_phase2.py:234` (adaptive-ci.yml is not on main-derived branches) — expected until the adaptive branch merges.

**Options.**
- (a) Leave logs/locks as they are and document them as expected test output. *Recommended for locks* — a lock file is the proof the lock behaviour was exercised; deleting it in tearDown would hide a real leak.
- (b) Redirect test-namespace logs under `logs/test-exports/<namespace>/`, mirroring `audit.py:116` `out_dir()` — the same pattern Task 4 used for `out/`.
- (c) Clean logs/locks in tearDown — smallest visible change, but it destroys the evidence if a test ever leaks a lock.

## 4. Gate blockers that need a senior-approved one-line fix (from Task 4)

**Problem, with evidence.** Two pre-existing failures block the §4b verification gate (G5) on every branch, and neither is in any brief-02 task:
- `npm run lint` fails on origin/main: `automation/paper-trading/db_helper.mjs 44:39 error Empty block statement no-empty` — the empty `catch{}` in `finish(){try{db.close()}catch{}process.exit(0)}`.
- `npm run check:secrets` crashes on any checkout containing a Python venv: `Error: EISDIR: illegal operation on a directory, read at scripts/check-secrets.mjs:5:52` — the walk treats symlinks as files and `.venv-*/lib64` is a symlink to a directory. On a venv-free tree it passes cleanly. No brief-02 file is a secret.

**Recommendation.** The senior approves one one-line fix each (a named empty catch; skip directory symlinks in the walk), or the gate records them as known blockers with the exact output. Do not weaken a test or fake the check to get green.

## 5. Dashboard follow-ups (from Task 5)

- **360 px visual check.** The new Execution quality panel and Outages list wrap by CSS (`.ad-outages` rows stack at ≤640 px; `.ad-tiles` stays two-column), and the rules are pinned by `tests/performance-phase2.test.jsx`. A visual check at 360 px in a real browser was not done in this round (no live browser in the worker); do it before calling the UI done.
- **Verify against the real paper_v1 rows.** The API now surfaces `revaluation`, `mark_basis` and the trimmed `performance` for stored EODs, and the UI shows the tag on the three revalued days when given the fixture (16 Nov −₹1,777.09, 17 Nov +₹6,037.85, 18 Nov −₹4,684.49). Once deployed, confirm the live dashboard shows the tag on those three days from the real `paper_eod` rows.
- **Outages are display-only.** The Outages list reads `paper_outages` (kinds: `no_monitoring`, `monitor_coverage_low`, `scan_missing`, `eod_missing`). Alerting on them is the always-on session checker proposed in `docs/proposals/always-on-session-checker.md` (Task 6b) — the two belong together.
- **Schema note.** `tests/paper-schema.sql` gained the `paper_outages` table (same DDL as `automation/paper-trading/schema.sql:76`); the Python Track A tests already use `schema.sql`, so no change was needed there.

## 6. Anything else noticed

- **`automation-api.test.jsx` cross-check spawns a hard-coded path** (`/workspace/sahil-fyers-turso/.venv/bin/python`, ENOENT on any machine without `/workspace`) — a pre-existing vitest failure. Consider making it skip cleanly when the path is absent, as the T1 fix did for the adaptive suite in brief 01.
- **`history.test.jsx` times out at 5000 ms on this VM** (tests take ~53 s here vs ~8 s on the senior's box) — a slow-VM timeout, not a product bug. Consider raising the timeout or splitting the test.
- **`/tmp` is a 512 MB tmpfs and filled to 100%** during the Task 4 inventory run. The §4b gate's clean checkout plus venv plus `node_modules` must fit in it, or the gate should use a path under `/home/hatch` and say so plainly in the report.
- **Clearing-charge GST wording (Task 3).** If the senior accepts the GST-base-only fix (clearing belongs in the GST base per the Fyers charges page, zero numeric effect while clearing is 0), it goes through the frozen-file process: new `charge_policy.json` / `charge_rates.json` versions, new digests, and a session-boundary change — see `docs/proposals/clearing-charge.md`.
