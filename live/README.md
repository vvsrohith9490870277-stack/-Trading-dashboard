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
