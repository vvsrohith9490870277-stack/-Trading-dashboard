# adaptive-live — paper-only adaptive + Support Agent sidecar

Sidecar files: `live/`, `run_adaptive.sh`, `run_support.sh`. This repo is the
working copy; the Track A runtime it reads (never writes) lives at
`TRACK_A_ROOT` (see below).
State (local only, never committed): `state/adaptive.sqlite` (namespace
`adaptive_v1`), `state/market.sqlite` (read-only copy of Track A inputs),
`state/support/findings.sqlite`, `state/snapshots/`.
Reports: `<report dir>/<day>/` — default `/workspace/reports/adaptive/<day>/`,
override with `ADAPTIVE_REPORT_DIR`; Support Agent reports default to
`/workspace/reports/support-agent/<day>/` (`SUPPORT_REPORT_DIR`).

## Schedule (IST)

| When | Command | What |
|---|---|---|
| 08:15 | `run_adaptive.sh preflight` | kill switch, frozen-file hashes (worktree + installed), forbidden imports, harness hash, Turso read-only reachability |
| 09:00–15:35 | nothing | heavy work is refused in market hours |
| 16:45 | `run_support.sh --session <day>` | read-only findings for the day (after the 16:10 EOD) |
| 16:50 | `run_adaptive.sh eod --session <day>` | waits for reconciled Track A EOD (deadline 19:00), snapshot, spawns, freshness + PIT gates, sims, row-count guard; then, only when the daily step simulated, runs the registry-candidate shadow step (`live/candidate_shadow.py --turso`, candidates 101–109) and mirrors the shadow results to Turso |
| 18:05 | `run_adaptive.sh backfill --session <day> --from 2026-06-02 --to <day>` | NSE Bhavcopy -> local archive; idempotent; refused before the Track A EOD for `not_before_session` is reconciled |
| 18:30 | `run_adaptive.sh eod --session <day> --no-wait` | re-run so simulations run with the archive (deterministic, idempotent) |
| any time after a gap | `run_adaptive.sh eod --catch-up [--from <day>] [--session <day>] [--force]` | processes missed sessions in date order; a day runs only if Track A's `paper_eod` exists and `ledger_reconciled` is true, otherwise it is recorded `SKIPPED_NO_RECONCILED_TRACK_A_EOD` and nothing is synthesised; already-marked days are skipped unless `--force` |

Do not run `eod` or `backfill` before 15:35 IST (hard quiet window
09:00–15:35, plus the policy heavy-work window). A sidecar lockfile
`state/adaptive.lock` plus niceness/ionice wrap `eod`/`backfill`.

Kill switch: `touch automation/paper-trading/out/adaptive/KILL_SWITCH`.
Backfill stop: `touch state/BACKFILL_STOP`.
Reset adaptive state: delete `state/adaptive.sqlite` (local only).

```
./run_support.sh --session 2026-11-11
./run_adaptive.sh eod --session 2026-11-11
./run_adaptive.sh backfill --session 2026-11-11 --from 2026-06-02 --to 2026-11-11
./run_adaptive.sh eod --session 2026-11-11 --no-wait
./run_adaptive.sh eod --catch-up --from 2026-11-13 --session 2026-11-18
```

## Environment variables

| Variable | Default | What |
|---|---|---|
| `TRACK_A_ROOT` | `/workspace/paper-trading` (from `adaptive_live_config.json` `track_a_root` if the env var is unset) | Track A runtime root, read-only. The runner no longer crashes at import if it is missing; a phase that needs it fails with `track_a_root_not_found:<path>` |
| `ADAPTIVE_REPORT_DIR` | `/workspace/reports/adaptive` | where `runner-<phase>.json` / `adaptive-daily.json` / failure reports are written |
| `ADAPTIVE_PYTHON` | `/workspace/sahil-fyers-turso/.venv/bin/python`, falling back to `python3` when that python does not exist | python used by `run_adaptive.sh` |
| `SUPPORT_REPORT_DIR` | `/workspace/reports/support-agent` | Support Agent report dir |
| `ADAPTIVE_LIVE_DB` | `state/adaptive.sqlite` | local adaptive SQLite file (tests point it at a throwaway copy) |

The sidecar never creates a `.env` file, and no credentials are ever
printed, logged or committed. No sidecar Python code reads a credential:
Turso access is SELECT-only inside a libsql read transaction
(`live/turso_ro.mjs`) for the daily step; transient read failures are
retried (2 retries, 3s/6s backoff) and local read-only-guard refusals
(`ro_*`) are never retried. The ONE write exception is the shadow mirror:
after a simulated daily step, the runner runs `live/candidate_shadow.py
--turso`, which mirrors the shadow results to the Turso
`adaptive_candidate_daily` / `adaptive_candidate_trades` tables via
`live/turso_write.py` (node `automation/paper-trading/db.mjs`, the same
write path as the Track A engine). The mirror never sees a credential
value — db.mjs reads the `.env` file itself, and the child process is only
passed the `PAPER_ENV_FILE` path. The daily step itself stays
credential-free and local-only.

## Tests (offline)

No Turso credentials, no Fyers, no NSE network are needed. Use the repo's
pinned Python deps where possible (Python 3.13 on CI; a local
`.venv-adaptive` with `pip install -r automation/paper-trading/requirements.txt`
also works) and `npm ci` at the repo root (or in
`automation/paper-trading`) so `@libsql/client` resolves; live tests that
need it skip with an explicit reason when it cannot be resolved.

```bash
# A. sidecar runner tests
(cd live && python -m unittest discover -s tests -p 'test_*.py')
# B. adaptive library tests
(cd automation/paper-trading && python -m unittest discover -s tests -p 'test_adaptive*.py')
# C. full Python suite
(cd automation/paper-trading && python -m unittest discover -s tests -p 'test_*.py')
# D. dashboard tests touching adaptive
npm ci && npx vitest run tests/adaptive-read.test.js
```

CI (`.github/workflows/adaptive-ci.yml`) runs three jobs on `main` and on
pull requests: `adaptive-python` (library tests + live tests),
`adaptive-dashboard` (`npx vitest run`) and `adaptive-truncation`.
Permissions are `contents: read`; there are no secrets, no deploy, and no
network access to NSE/Fyers.

## Failure reports

A successful phase writes `<report dir>/<day>/runner-<phase>.json`. Any
phase failure writes `<report dir>/<day>/runner-<phase>-FAILED.json` with
`phase`, `session_day`, `at_ist`, `status: "FAILED"`, a short `error_code`
(e.g. `frozen_file_hash_mismatch`, `turso_read_failed`,
`track_a_root_not_found`), a non-secret `detail` and `ok: false`, and exits
non-zero. The full traceback still goes to `logs/adaptive-<day>.err` only.
A frozen-file mismatch names the file and which root differed (worktree
vs installed runtime) plus the first 12 hex chars of the expected vs actual
digest. A clean run removes any stale `runner-<phase>-FAILED.json`.

## `simulation_status` in `adaptive-daily.json`

| Status | Meaning |
|---|---|
| `SIMULATED` | fresh inputs, PIT coverage >= 99%, simulation ran; `input_freshness.fresh` is true |
| `BLOCKED_PIT_SERIES_ARCHIVE_P1` | official Bhavcopy coverage of the expected sessions is below 99%, or the session day's own Bhavcopy is missing; `pit_series_coverage.missing_dates` lists the missing dates (capped at 50, with the true `missing_count`) |
| `BLOCKED_STALE_INPUT` | the snapshot's latest completed bar is earlier than the session day (or the session day is not a regular trading session); no new `adaptive_daily_returns` / `adaptive_sim_trades` rows are written and no parity rows are recorded |
| `WARM_START_ONLY_NO_FORWARD_SESSION_YET` | per strategy: the warm-start filled but there is no forward session yet |

`input_freshness` also records `latest_completed_bar_in_snapshot`,
`latest_index_bar`, `bhavcopy_for_session_day`, `latest_bhavcopy_date` and
`reasons`. A calendar date with neither an archive row nor an index bar,
on/before the latest bar, is a `no_session` day (unlisted holiday) and is
not counted missing. The 99% gate is never lowered and a missing series is
never assumed to be EQ.

`parity` in the daily report is `compared` only when a trade-level
comparison actually ran and wrote an `adaptive_parity` row; otherwise it is
`not_evaluable` with a reason (live fills unavailable, EOD not reconciled,
simulation not run). Only evaluation-window trades (entry after the
warm-start end) are compared with the live ledger; warm-start-inherited
trades are not discordance by themselves.

Phase timings are in `timings_seconds` on the runner report (`snapshot`,
`protected_counts_before`, `daily`, `shadow`, `protected_counts_after`, `total`) and
on `adaptive-daily.json` (`spawn`, `coverage_gate`, `simulate_<strategy>`,
`persist`, `hash_chain`, `after`). They are diagnostic only; they do not
change simulation logic, results or the memory cap.

## Isolation guarantees (observation-only)

- Paper-only: nothing places real orders and adaptive never calls Fyers.
- Turso is read-only for adaptive, with ONE exception — the shadow mirror:
  the runner's shadow step (after a simulated daily step) writes ONLY the
  `adaptive_candidate_daily` / `adaptive_candidate_trades` tables, namespace
  `adaptive_v1`, every row `mode='shadow'` and `simulated=1`, via
  `live/turso_write.py`. All other adaptive Turso access is SELECT inside a
  read transaction via `live/turso_ro.mjs`; `TursoRO.batch()` raises
  `PermissionError`.
- Adaptive never writes `paper_v1`, `paper_v2` or `paper_demo_v1` (any table, any form);
  the protected-namespace row-count guard runs before and after every `eod`.
- `adaptive_observation_only` stays `true`: no promotion, no Track C, no
  gate/calibration wiring into the nightly run.
- Frozen Track A rules/policy files must match `freeze_lock.json`; a
  mismatch refuses to run.
- No invented data: missing prices, bars, Bhavcopy rows, fills, parity
  results or sessions are reported as `UNKNOWN` / `not_evaluable` /
  `BLOCKED` with a reason, never synthesised.

## Backfill notes

Backfill uses `curl --http2` (urllib HTTP/1.1 often gets HTTP 403 from this
host), a browser User-Agent, calendar session days (skips weekends, config
holidays, Muhurat 2026-11-08), treats HTTP 404 and TradDt mismatches as
`no_session`, retries 403/429, and still refuses a production ingest until
a reconciled Track A EOD exists. Each date's report entry lists the archive
forms tried in order (UDiFF zip, legacy `cm{DD}{MON}{YYYY}bhav.csv.zip`,
`sec_bhavdata_full_{DDMMYYYY}.csv`), the HTTP status of each, which
succeeded, and the final status (`ingested` / `no_session` /
`fetch_failed`). `--retry-missing` re-attempts only dates without a valid
local file or archive rows; a second run with nothing missing is a no-op.
One date's failure never aborts the others, but
`max_consecutive_failures` still stops runaway hammering of NSE, and the
policy rate cap (`backfill_max_requests_per_minute`) is kept.

Official NSE `sec_list` (CM price-band percent file) is ingested into local
`nse_price_band_archive`, point-in-time per session, and used by the shadow
simulator for UPPER/LOWER locked entries/exits. Missing files, missing
previous close, and dynamic/F&O (no static band) stay UNKNOWN — a lock or
a band level is never invented. Adaptive does not call Fyers.

## Support Agent (16:45)

Read-only findings for the day. It reads Turso `paper_events` and also the
Track A `logs/engine.jsonl` (read-only) so `reference_feeds` HTTP 403s are
detected, and `monitor_staleness` lists each check gap above
`worker_stale_seconds`. After detectors run, a findings-only verification
stage re-derives each finding with two independent deterministic verifiers
(`events_sql` from the snapshot, `log_files` from engine.jsonl / audit
jsonl). Findings are marked confirmed / disputed / rejected / unconfirmed
with evidence; paste-ready review prompts are written to `REVIEW_PROMPTS.md`
and `review-prompts/`. Finding status in the store is not mutated. No
patches, installs, notifications or orders.

## History (Nov 2026, for context — see git log for the current state)

The first backfill ingested 102/116 days (coverage 0.879; 11 dates failed
with HTTP 403 and 3 more answered 404); later retries recovered everything
and by 12 Nov all 117/117 days were ingested (coverage 1.0). The 11 Nov
`eod` run took 1h49m and the 12 Nov run 3h32m — phase timings (Task 8) now
show where that time goes. The 13, 16, 17 and 18 Nov adaptive runs did not
happen (13 Nov paused by the user; 16–17 Nov and the morning of 18 Nov were
a platform outage); `eod --catch-up` now recovers missed sessions.
