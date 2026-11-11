# adaptive-live — paper-only adaptive + Support Agent sidecar (Wed 11 – Fri 13 Nov 2026)

Code: git worktree of origin/main `e1593cd` at /workspace/adaptive-live (detached). Sidecar files: `live/`, `run_adaptive.sh`, `run_support.sh`.
State (local only): `state/adaptive.sqlite` (namespace `adaptive_v1`), `state/market.sqlite` (read-only copy of Track A inputs),
`state/support/findings.sqlite`, `state/snapshots/`. Reports: `/workspace/reports/adaptive/<day>/`, `/workspace/reports/support-agent/<day>/`.
Turso access is SELECT-only inside a libsql read transaction (`live/turso_ro.mjs`). Nothing writes to `/workspace/paper-trading`.

| When (IST) | Command | What |
|---|---|---|
| 08:20 | `run_adaptive.sh preflight` | kill switch, frozen-file hashes (worktree + installed), forbidden imports, harness hash, Turso RO reachability |
| 09:15–15:30 | nothing | H4: no adaptive work in market hours |
| 16:45 | `run_support.sh --session <day>` | read-only findings for the day (after the 16:10 EOD) |
| 16:50 | `run_adaptive.sh eod --session <day>` | waits for reconciled Track A EOD (deadline 19:00), snapshot, spawns, PIT gate, sims, row-count guard |
| 18:05 (Wed–Fri) | `run_adaptive.sh backfill --session <day> --from 2026-06-02 --to <day>` | P1 minimal UDiFF Bhavcopy -> local archive; idempotent (skips files already fetched); refused before Wed EOD |
| 18:30 (Wed–Fri) | `run_adaptive.sh eod --session <day> --no-wait` | re-run so simulations run with the archive (deterministic, idempotent) |

Kill switch: `touch automation/paper-trading/out/adaptive/KILL_SWITCH`. Backfill stop: `touch state/BACKFILL_STOP`.
Reset adaptive state: delete `state/adaptive.sqlite` (local only).
Tests: `cd live && python -m unittest tests.test_live_wiring`.

## Tonight's routines (Wed 11 Nov 2026) — argument check

These command lines parse and resolve paths. Do not run `eod` or `backfill` before 15:35 IST (hard quiet window 09:00–15:35, plus the policy heavy-work window). `run_support.sh` is read-only and is safe at 16:45.

```
./run_support.sh --session 2026-11-11
./run_adaptive.sh eod --session 2026-11-11
./run_adaptive.sh backfill --session 2026-11-11 --from 2026-06-02 --to 2026-11-11
./run_adaptive.sh eod --session 2026-11-11 --no-wait
```

Backfill now: uses `curl --http2` (urllib HTTP/1.1 often gets HTTP 403 from this host), a browser User-Agent, calendar session days (skips weekends, config holidays, Muhurat 2026-11-08), treats HTTP 404 and TradDt mismatches as `no_session`, retries 403/429, and still refuses a production ingest until Track A EOD for `2026-11-11` is reconciled. A sidecar lockfile `state/adaptive.lock` plus niceness/ionice wrap `eod`/`backfill`.

Support Agent 16:45 also reads `logs/engine.jsonl` (read-only) so `reference_feeds` HTTP 403s are visible, and `monitor_staleness` lists each check gap above `worker_stale_seconds`.


## 11 Nov 2026 — sidecar readiness (16:45 / 16:50 / 18:05 / 18:30 IST)

Verified command lines (argument parser + paths; the eod/backfill jobs themselves are not started from here):

- 16:45 `run_support.sh --session 2026-11-11`
- 16:50 `run_adaptive.sh eod --session 2026-11-11`
- 18:05 `run_adaptive.sh backfill --session 2026-11-11 --from 2026-06-02 --to 2026-11-11`
- 18:30 `run_adaptive.sh eod --session 2026-11-11 --no-wait`

`eod` and `backfill` refuse to start between 09:00 and 15:35 IST, take a sidecar lockfile (`state/adaptive.lock`), and apply nice/ionice plus the existing memory cap. Backfill day lists are calendar-based (weekdays minus holidays minus Muhurat); 404 or a TradDt mismatch is `no_session`, not a consecutive failure. Downloads use `curl --http2` with a browser User-Agent (plain urllib from this host often gets HTTP 403).

Support Agent reads Turso paper_events and also `/workspace/paper-trading/logs/engine.jsonl` (read-only) so `reference_feeds` HTTP 403s are detected. `monitor_staleness` lists each check gap above `worker_stale_seconds`. Dry-run against 11 Nov data (scratch findings DB, `--now 15:40`) produced `scan_missed`, `monitor_staleness` gaps at 09:18–09:22, 09:33–09:41 and 11:08–11:15, and `feed_http_403` for corporate_actions/ASM/GSM. Tonight’s 16:45 run should use the default tick set (ends 16:35) so it can also see the 16:10 EOD.

Official NSE `sec_list` (CM price-band percent file) is ingested into local `nse_price_band_archive`, point-in-time per session, and used by the shadow simulator for UPPER/LOWER locked entries/exits. Missing files, missing previous close, and dynamic/F&O (no static band) stay UNKNOWN — a lock or a band level is never invented. Adaptive does not call Fyers. Backfill will fetch/ingest `sec_list` alongside UDiFF when a file is not already on disk; `adaptive_daily` also ingests any files already under `state/circuit_bands`.

After detectors run, a findings-only verification stage re-derives each finding with two independent deterministic verifiers (`events_sql` from the snapshot, `log_files` from engine.jsonl / audit jsonl). Findings are marked confirmed / disputed / rejected / unconfirmed with evidence; paste-ready review prompts are written to `REVIEW_PROMPTS.md` and `review-prompts/`. Finding status in the store is not mutated. No patches, installs, or notifications.

Findings are deduped by detector+correlation across reruns (a 15:40 dry-run must not double the official 16:45 403s). Signal-to-fill latency is measured from the 09:15 official open, not pre-open scan time. A later successful reference-feed fetch (e.g. 15:41, corporate actions ok) or recovery after a rate-limit stall resolves those findings so they are not left OPEN after the fix is installed.

Official UDiFF bhavcopy for 2026-11-06 parsed (3715 raw / 2980 EQ+BE+BZ). All 824 overlapping opens matched Turso `daily_ohlcv`; some closes differ where the Turso source is a Fyers live 1D bar. 2026-11-09 returned HTTP 403 from this host after earlier probes; 2026-11-10 (holiday) correctly returned 404. The 18:05 job retries 403s with backoff. A missing session is retried with a fresh browser-like cookie session against official nsearchives forms only (UDiFF zip, legacy `cm{DD}{MON}{YYYY}bhav.csv.zip`, and `sec_bhavdata_full_{DDMMYYYY}.csv`); TradDt/TIMESTAMP/DATE1 must match the requested date, and a session is never synthesized. The 99% PIT series-archive gate is not lowered when a date stays unavailable.

Circuit data: official `sec_list_DDMMYYYY.csv` (CM Price Band complete list) downloaded for 2026-11-06 (3608 rows, 3051 with a static 2/5/10/20/40% band). Adaptive does not call Fyers. Turso `paper_price_bands` currently holds only ASM/GSM flags, not upper/lower prices. F&O names often have a blank Band (dynamic flex) and stay `UNKNOWN`.
