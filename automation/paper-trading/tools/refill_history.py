"""One-shot Fyers history refill for the Turso move (Task 2).

Fetches the full 364-calendar-day window for every universe symbol plus the
benchmark and writes the bars DIRECTLY into daily_ohlcv / index_ohlcv
with INSERT OR IGNORE.

Why this exists instead of reusing engine.sync / engine.sync_history:
  (1) engine.sync skips symbols with no existing history (engine.py:493) —
      on a fresh DB that is all 692 symbols, so zero calls happen;
  (2) engine.sync rejects candles outside the verified calendar
      (2026-11-01..2026-12-31, engine.py:515-517) — every bar before
      2026-11-01 would be rejected.
This script deliberately bypasses both guards. It does NOT change engine.py.

Benchmark: only NIFTY50-INDEX (POLICY['benchmark_db_symbol'] in
prefilter_policy.json). When index_ohlcv is empty, prefilter.benchmark
starts at target - 465 days (prefilter.py:48) and chunks in 364-day
windows (prefilter.py:50-56), so the benchmark needs TWO calls
(364 + 101 days). NIFTY500 is NOT fetched: nothing writes NIFTY500 rows
to index_ohlcv (the only writer is prefilter.benchmark, which writes
NIFTY50-INDEX); prefilter.py:59 only reads NIFTY500 rows as a
session-date reference, and its completeness check falls back to the
session() calendar, so it works without NIFTY500 rows.

Budget (brief): Fyers allows 4/sec, 40/min, 4,000/day, but the session
monitor reserves 1,500 calls/day, so history may use at most 2,500/day.
Calls are paced >= 1.3 s apart. Retries count toward the budget.
Full universe: 692 stocks + 2 benchmark calls = 694 calls, one day.

The api and db handles are injected by the caller (Grok runs this on the
box with the real Fyers/Turso handles). No credentials live in this file
and nothing here prints a secret.
"""
import argparse
import math
import sqlite3
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

LOOKBACK_DAYS = 364
BENCHMARK_LOOKBACK_DAYS = 465  # prefilter.py:48, empty-benchmark start
MAX_CALLS_PER_RUN = 2500  # 4,000/day minus the 1,500-call monitor reserve
MIN_INTERVAL_SECONDS = 1.3
MAX_ATTEMPTS = 3
BENCHMARK_SYMBOL = "NIFTY50-INDEX"
STOCK_SOURCE = "FYERS_PAPER_SYNC"
BENCHMARK_SOURCE = "FYERS_PREFILTER_SYNC"
BATCH_SIZE = 150

STOCK_SQL = (
    "INSERT OR IGNORE INTO daily_ohlcv"
    "(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?)"
)
BENCHMARK_SQL = (
    "INSERT OR IGNORE INTO index_ohlcv"
    "(trade_date,symbol,open,high,low,close,volume,source,loaded_at)"
    " VALUES(?,?,?,?,?,?,?,?,?)"
)


class _SQLiteAdapter:
    """Wraps a sqlite3 connection so it looks like the engine DB handle."""

    def __init__(self, conn):
        self.conn = conn

    def query(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def batch(self, statements):
        with self.conn:
            for sql, args in statements:
                self.conn.execute(sql, args)


def _as_db(db):
    if hasattr(db, "query") and hasattr(db, "batch"):
        return db
    if isinstance(db, sqlite3.Connection):
        return _SQLiteAdapter(db)
    raise TypeError("db must have .query/.batch or be a sqlite3 connection")


def valid_candle(candle):
    """Same OHLC validity filter as engine.sync, plus a finite volume.

    Returns (trade_date, reason) — reason is None when the candle is valid.
    NOTE: deliberately NO verified-calendar / session check here: the whole
    point of the refill is to write bars from before 2026-11-01 too.
    """
    try:
        ts, o, h, l, cl, v = candle[:6]
        day = datetime.fromtimestamp(float(ts), IST).date()
        values = [float(x) for x in (o, h, l, cl)]
    except (TypeError, ValueError, OverflowError, OSError):
        return None, "invalid_OHLC"
    if not all(math.isfinite(x) and x > 0 for x in values):
        return None, "invalid_OHLC"
    if not (h >= max(o, l, cl) and l <= min(o, h, cl)):
        return None, "invalid_OHLC"
    try:
        volume = float(v)
    except (TypeError, ValueError):
        return None, "invalid_OHLC"
    if not math.isfinite(volume) or volume < 0:
        return None, "invalid_OHLC"
    return day, None


def _chunk_windows(start, end, chunk_days=LOOKBACK_DAYS):
    """Split [start, end] into <=chunk_days windows, as prefilter does."""
    windows = []
    cur = start
    while cur <= end:
        windows.append((cur, min(end, cur + timedelta(days=chunk_days))))
        cur = windows[-1][1] + timedelta(days=1)
    return windows


def refill(db, api, target, symbols, benchmark_symbol=BENCHMARK_SYMBOL,
           benchmark_lookback_days=BENCHMARK_LOOKBACK_DAYS,
           max_calls=MAX_CALLS_PER_RUN, max_attempts=MAX_ATTEMPTS,
           min_interval_seconds=MIN_INTERVAL_SECONDS,
           sleep=time.sleep, now=None, batch_size=BATCH_SIZE):
    """Refill one 364-day window for `symbols` + the benchmark.

    api: object with .history(symbol, start_date, end_date) -> Fyers candles
         [epoch_ts, open, high, low, close, volume], same shape as
         engine.MarketData.history.
    db: engine-style .query/.batch handle or a sqlite3 connection.
    Benchmark: `benchmark_symbol` fetched over `benchmark_lookback_days`
    (default 465), chunked in <=364-day windows (default 2 calls).
    Returns a report dict. Never raises for a per-symbol failure: the
    symbol lands in symbols_failed and the run continues.
    """
    db = _as_db(db)
    now = now or (lambda: datetime.now(IST))
    start = target - timedelta(days=LOOKBACK_DAYS)
    bench_start = target - timedelta(days=benchmark_lookback_days)
    loaded_at = now().isoformat()
    # One job per API call: stocks get one 364-day call each; the benchmark
    # gets one call per <=364-day chunk of its 465-day window.
    jobs = [(s, STOCK_SQL, STOCK_SOURCE, False, start, target) for s in symbols]
    jobs += [
        (benchmark_symbol, BENCHMARK_SQL, BENCHMARK_SOURCE, True, w0, w1)
        for w0, w1 in _chunk_windows(bench_start, target)
    ]

    report = {
        "target": str(target),
        "window_start": str(start),
        "window_end": str(target),
        "benchmark_window_start": str(bench_start),
        "benchmark_symbol": benchmark_symbol,
        "calls_made": 0,
        "stock_calls": 0,
        "benchmark_calls": 0,
        "candles_written": 0,
        "benchmark_candles_written": 0,
        "symbols_done": [],
        "symbols_deferred": [],
        "symbols_failed": [],
        "invalid": [],  # {symbol, bar_date, reason} — counts/reasons only
        "stopped": None,
        "max_calls": max_calls,
    }
    pending = []
    last_call_at = None

    def flush():
        if pending:
            db.batch(pending)
            pending.clear()

    def pace():
        nonlocal last_call_at
        if last_call_at is None:
            return
        wait = min_interval_seconds - (time.monotonic() - last_call_at)
        if wait > 0:
            sleep(wait)

    for symbol, sql, source, is_benchmark, job_start, job_end in jobs:
        if report["stopped"] is not None:
            report["symbols_deferred"].append(symbol)
            continue
        candles = None
        attempts = 0
        while attempts < max_attempts:
            if report["calls_made"] >= max_calls:
                report["stopped"] = "budget_exhausted"
                report["symbols_deferred"].append(symbol)
                break
            pace()
            try:
                candles = api.history(symbol, job_start, job_end)
            except Exception:
                attempts += 1
                report["calls_made"] += 1
                if is_benchmark:
                    report["benchmark_calls"] += 1
                else:
                    report["stock_calls"] += 1
                last_call_at = time.monotonic()
                if attempts >= max_attempts:
                    if symbol not in report["symbols_failed"]:
                        report["symbols_failed"].append(symbol)
                continue
            attempts += 1
            report["calls_made"] += 1
            if is_benchmark:
                report["benchmark_calls"] += 1
            else:
                report["stock_calls"] += 1
            last_call_at = time.monotonic()
            break
        if candles is None:
            continue
        for c in candles:
            day, reason = valid_candle(c)
            if reason is None and day > target:
                reason = "future_candle"
            if reason is not None:
                bar_date = None
                try:
                    bar_date = str(datetime.fromtimestamp(float(c[0]), IST).date())
                except Exception:
                    pass
                report["invalid"].append(
                    {"symbol": symbol, "bar_date": bar_date, "reason": reason}
                )
                continue
            _, o, h, l, cl, v = c[:6]
            if is_benchmark:
                pending.append(
                    (sql, (str(day), symbol, o, h, l, cl, v, source, loaded_at))
                )
                report["benchmark_candles_written"] += 1
            else:
                pending.append(
                    (
                        sql,
                        (
                            str(day), symbol, o, h, l, cl, v,
                            float(cl) * float(v), source, loaded_at,
                        ),
                    )
                )
                report["candles_written"] += 1
            if len(pending) >= batch_size:
                flush()
        if symbol not in report["symbols_done"]:
            report["symbols_done"].append(symbol)
    flush()
    return report


def load_universe(path):
    import csv

    with open(path, newline="") as f:
        return sorted({row["db_symbol"] for row in csv.DictReader(f) if row.get("db_symbol")})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", required=True, help="refill date YYYY-MM-DD")
    p.add_argument("--universe", default="universe.csv", help="universe csv path")
    p.add_argument("--benchmark-symbol", default=BENCHMARK_SYMBOL,
                   help="benchmark db symbol (default NIFTY50-INDEX)")
    p.add_argument("--benchmark-lookback-days", type=int,
                   default=BENCHMARK_LOOKBACK_DAYS)
    p.add_argument("--max-calls", type=int, default=MAX_CALLS_PER_RUN)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sqlite", help="local sqlite file to write into")
    group.add_argument("--live", action="store_true",
                       help="use engine.MarketData + engine.DB (box only)")
    args = p.parse_args(argv)
    target = date.fromisoformat(args.target)
    symbols = load_universe(args.universe)
    if args.sqlite:
        conn = sqlite3.connect(args.sqlite)
        db = conn
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import engine as e

        db = e.DB()
        api = e.MarketData()
        report = refill(db, api, target, symbols,
                        benchmark_symbol=args.benchmark_symbol,
                        benchmark_lookback_days=args.benchmark_lookback_days,
                        max_calls=args.max_calls)
        print(report_summary(report))
        return 0
    # --sqlite path still needs a live Fyers api; refuse to invent one.
    print("ERROR: --sqlite needs an api handle injected by the caller; "
          "use --live on the box, or call refill() from Python.", file=sys.stderr)
    return 2


def report_summary(report):
    return (
        "refill target={target} window={window_start}..{window_end} "
        "calls={calls_made}/{max_calls} stocks={stock_calls} "
        "benchmark={benchmark_calls} candles={candles_written} "
        "bench_candles={benchmark_candles_written} "
        "done={done} deferred={deferred} failed={failed} "
        "invalid={invalid} stopped={stopped}".format(
            done=len(report["symbols_done"]),
            deferred=len(report["symbols_deferred"]),
            failed=len(report["symbols_failed"]),
            invalid=len(report["invalid"]),
            **report,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
