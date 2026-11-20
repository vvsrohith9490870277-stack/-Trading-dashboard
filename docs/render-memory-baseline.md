# Render scan memory baseline (render-deploy-03, Task 0)

Measured 2026-11-20 on this machine (Python 3.12.3, Node v24.22.1).
Worktree `~/workspace/rd3` at `dc85db8` (rebased from `3ad36b4`; the two
senior commits `b4e3fe4`/`dc85db8` do not touch the scan path).

## Fixture
`tests/render_mem_fixture.py` (seeded, generated, never committed):
692 symbols from `universe.csv` x 1501 sessions ending 2026-11-19
(weekdays minus the policy closed_dates, plus the 2026-11-08 Muhurat
special session), seed 20261120. Factor model: the benchmark
(NIFTY50-INDEX, `index_ohlcv`, 1501 rows) follows a shared market factor
exactly; each stock's return is beta_i * market + idiosyncratic noise,
beta_i ~ U(0.6, 1.8), ~25% of symbols with +0.25%/day drift. Result:
1,038,692 `daily_ohlcv` rows (168 MB sqlite), prefilter 43 PASS / 649
FAIL / 0 UNAVAILABLE on the baseline run — a realistic pass rate.
An earlier independent-returns fixture (all betas ~0, 0 PASS) measured
almost identically (tree 1057 MB, python 875 MB, node 212 MB, 180 s),
so the peak is not driven by passing symbols.

## Method
`/usr/bin/time` does NOT exist on this machine, so peak RSS is measured
by `tests/render_mem_fixture.py::measure_scan`: the real command
`python engine.py scan` (real clock; Fri 20 Nov before 16:00 IST, so the
completed day is 2026-11-19) run as a subprocess with
PAPER_RUNTIME=render, RENDER_MODE=trial,
PAPER_NAMESPACE_OVERRIDE=paper_render_trial_v1,
PAPER_TEST_DATABASE=<fixture>, FYERS_AUTO_LOGIN=0; the process tree's
VmRSS is sampled from /proc every 20 ms, and
resource.getrusage(RUSAGE_CHILDREN) gives the largest single child.
The optional live Turso read was skipped: `/workspace/sahil-fyers-turso/.env`
does not exist on this machine (no /workspace directory here).

## Baseline numbers (factor-model fixture, `dc85db8` code)
| metric | value |
|---|---|
| scan process-tree peak RSS | **1059 MiB** |
| python peak RSS (ru_maxrss confirms) | **867 MiB** |
| node peak RSS (largest single db.mjs child) | **213 MiB** |
| wall time | 208 s |
| exit code | 0 |
| prefilter | 43 PASS / 649 FAIL / 0 UNAVAILABLE |
| signals queued | 0 (fresh watermarks: bootstrap path) |
| latest qualifiers | 51 |

For scale: the Render free limit is 512 MiB for the whole instance
(supervisor ~90 MiB idle + worker ~95 MiB + scan). The scan alone peaks
at more than twice the instance limit — this is the OOM crash loop.

## Suspects, ranked by measured contribution
1. **`read_history` dict list (engine.py:687-691) — dominant, ~700-850 MiB.**
   Measured: 822 bytes per row-dict (1000-row sample, sys.getsizeof
   incl. keys/values) x 1,038,692 rows = ~854 MiB. The full frame built
   from it is ~165 MiB (5.7 MiB per 36,024 rows, measured). Python's
   867 MiB peak is essentially the dict list plus the frame.
2. **`filter_history` / `raw[valid]` copies (engine.py:709-713, 732, 737-748)
   — second.** Each is another full-frame copy (~165 MiB transient),
   coexisting with the frame; `raw.groupby('symbol')` runs twice.
3. **One giant `db.batch(stm)` (engine.py:787) — dominant on the node side.**
   On a 30-symbol check DB the scan's single batch is 11,949 statements /
   7.6 MiB JSON (dominated there by 662 skipped symbols x 7 strategies);
   the full scan's batch is ~7,000 statements / ~15-20 MiB (estimate).
   Node parses that JSON into a large object graph: node peak 213 MiB
   vs a 78-79 MiB baseline for a trivial query.
4. **Per-call `node db.mjs` children (engine.py:81) — the floor, and it
   scales with result size.** Single-query node peak by rows returned:
   1 symbol (1,501 rows) 87 MiB; 5 symbols (7,505 rows) 107 MiB;
   10 symbols (15,010 rows) 133 MiB; 25 symbols (37,525 rows) 200 MiB.
   A 25-symbol read chunk already peaks its node child at 200 MiB.
   The scan never enables the persistent helper (only the worker does,
   automation.py:597), so every query/batch is a fresh node process,
   sequential, never overlapping.
5. **`render_app.py:241-243` capture_output — small but unbounded.**
   The scan prints its whole report dict to stdout; the supervisor
   buffers all of it (only the last 2000 chars are kept). ~50-200 KiB
   in practice, but unbounded in principle.

Python import floor: `import engine` peaks at 75 MiB (pandas+numpy 65 MiB).

## Other full-universe loaders (git grep)
`git grep -n "read_history|raw.groupby|DataFrame("` over `*.py`:
the scan path loads the full universe only in `read_history`
(engine.py:687) plus the 1501-row benchmark frame (prefilter.py:65).
`calibration.py:450-451`, `pit_archive.py:310-330` and
`adaptive_engine.py:192` build frames but are not called by the scan.

## Tracemalloc note
Full-scan tracemalloc was abandoned: it slows this workload 10-30x
(a 12 s scan took >6 min; a 208 s scan did not finish in 11 min).
The ranking above is by direct component measurement instead
(dict-list bytes, frame bytes, batch JSON bytes, node scaling curve).

## Design consequence for Task 1
- Stream per symbol chunk AND flush `stm`/audit in bounded batches.
- The 25-symbol chunk size the brief suggests peaks its node child at
  200 MiB, so the chunk size must be smaller; it will be an env var
  (`RENDER_SCAN_CHUNK_SYMBOLS`) defaulted from measurement (candidates
  5 / 10 symbols: node 107 / 133 MiB). Chunk size changes no results.
- Two-pass chunked design (pass A: prefilter/gates; pass B: strategy
  loop) preserves the `stm` and audit-event order exactly.
