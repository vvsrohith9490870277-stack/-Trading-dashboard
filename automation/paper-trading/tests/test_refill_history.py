"""Tests for tools/refill_history.py — the one-shot Fyers history refill.

The refill bypasses the two sync-history guards in engine.py on purpose:
  (1) engine.sync skips symbols with no existing history (fresh DB = all 692
      skipped, zero calls);
  (2) engine.sync rejects candles outside the verified calendar
      (2026-11-01..2026-12-31) — every bar before 2026-11-01 rejected.
The refill writes DIRECT INSERT OR IGNORE rows, so old candles land and a
symbol with no history is still fetched. Tests are hermetic: fake api,
in-memory sqlite, no network, no Turso, no Fyers.
"""
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import refill_history as rh

IST = timezone(timedelta(hours=5, minutes=30))
T = datetime(2026, 11, 21, 12, 0, tzinfo=IST)
TARGET = date(2026, 11, 21)


def ts(d, hh=15, mm=30):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp())


def candle(d, o=100, h=101, l=99, c=100, v=1000):
    return [ts(d), o, h, l, c, v]


class FakeDB:
    """sqlite3-backed stand-in for the engine DB (.query/.batch)."""

    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.execute(
            "CREATE TABLE daily_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,turnover REAL,"
            "source TEXT,loaded_at TEXT,PRIMARY KEY(trade_date,symbol))"
        )
        self.c.execute(
            "CREATE TABLE index_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,source TEXT,"
            "loaded_at TEXT,PRIMARY KEY(trade_date,symbol))"
        )

    def query(self, s, args=()):
        return [dict(r) for r in self.c.execute(s, args).fetchall()]

    def batch(self, stm):
        with self.c:
            for s, args in stm:
                self.c.execute(s, args)


class FakeAPI:
    """Canned candles per symbol (or per (symbol, start, end) window);
    records every history call."""

    def __init__(self, candles_by_symbol, fail_first=None):
        self.candles_by_symbol = candles_by_symbol
        self.calls = []  # (symbol, start, end)
        self.fail_first = fail_first or {}  # symbol -> failures left
        self.attempts = {}

    def history(self, symbol, start, end):
        self.calls.append((symbol, str(start), str(end)))
        self.attempts[symbol] = self.attempts.get(symbol, 0) + 1
        if self.fail_first.get(symbol, 0) >= self.attempts[symbol]:
            raise RuntimeError("market_data_transport_failed")
        key = (symbol, str(start), str(end))
        if key in self.candles_by_symbol:
            return list(self.candles_by_symbol[key])
        return list(self.candles_by_symbol.get(symbol, []))


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class RefillTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.sleep = FakeSleep()

    def refill(self, api, symbols, **kw):
        kw.setdefault("sleep", self.sleep)
        kw.setdefault("now", lambda: T)
        return rh.refill(self.db, api, TARGET, symbols, **kw)

    def test_writes_valid_candles_with_turnover_and_sources(self):
        # Benchmark comes back in two chunks (465-day window); give each
        # chunk a different candle so both land.
        api = FakeAPI(
            {
                "AAA-EQ": [candle(date(2026, 11, 20)), candle(date(2026, 11, 21))],
                ("NIFTY50-INDEX", "2025-08-13", "2026-08-12"): [
                    candle(date(2026, 8, 12), o=25000, h=25100, l=24900, c=25050, v=0)
                ],
                ("NIFTY50-INDEX", "2026-08-13", "2026-11-21"): [
                    candle(date(2026, 11, 21), o=25000, h=25100, l=24900, c=25050, v=0)
                ],
            }
        )
        r = self.refill(api, ["AAA-EQ"])
        rows = self.db.query("SELECT * FROM daily_ohlcv ORDER BY trade_date")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["turnover"], 100 * 1000)  # close x volume
        self.assertEqual(rows[0]["source"], "FYERS_PAPER_SYNC")
        self.assertEqual(rows[0]["trade_date"], "2026-11-20")
        bench = self.db.query("SELECT * FROM index_ohlcv")
        self.assertEqual(len(bench), 2)
        self.assertEqual(bench[0]["source"], "FYERS_PREFILTER_SYNC")
        self.assertNotIn("turnover", bench[0])
        self.assertEqual(r["candles_written"], 2)
        self.assertEqual(r["benchmark_candles_written"], 2)
        self.assertEqual(r["benchmark_calls"], 2)  # two chunks of NIFTY50-INDEX

    def test_invalid_candle_dropped_and_reported(self):
        bad_high_low = candle(date(2026, 11, 20), o=100, h=90, l=99, c=100)  # high < close
        bad_zero = candle(date(2026, 11, 19), o=0, h=1, l=0, c=1)
        future = candle(date(2026, 11, 22))
        api = FakeAPI({"AAA-EQ": [bad_high_low, bad_zero, future, candle(date(2026, 11, 21))]})
        r = self.refill(api, ["AAA-EQ"])
        rows = self.db.query("SELECT * FROM daily_ohlcv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(r["candles_written"], 1)
        self.assertEqual(len(r["invalid"]), 3)
        reasons = {i["reason"] for i in r["invalid"]}
        self.assertEqual(reasons, {"invalid_OHLC", "future_candle"})
        # no candle data leaked into the report
        self.assertNotIn("100", str(r["invalid"]))

    def test_pre_2026_11_01_candle_written_calendar_guard_bypassed(self):
        # engine.sync would reject this (outside the verified calendar);
        # the refill must write it.
        old = candle(date(2026, 6, 15))
        api = FakeAPI({"AAA-EQ": [old]})
        r = self.refill(api, ["AAA-EQ"])
        rows = self.db.query("SELECT * FROM daily_ohlcv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "2026-06-15")
        self.assertEqual(r["candles_written"], 1)

    def test_symbol_with_no_existing_history_is_not_skipped(self):
        # Fresh DB: no rows at all. Guard (1) in engine.sync would skip it.
        api = FakeAPI({"AAA-EQ": []})
        r = self.refill(api, ["AAA-EQ"])
        self.assertEqual(
            [c[0] for c in api.calls],
            ["AAA-EQ", "NIFTY50-INDEX", "NIFTY50-INDEX"],  # benchmark: 2 chunks
        )
        self.assertIn("AAA-EQ", r["symbols_done"])
        self.assertEqual(self.db.query("SELECT COUNT(*) c FROM daily_ohlcv")[0]["c"], 0)

    def test_call_count_equals_symbols_plus_benchmark(self):
        syms = ["AAA-EQ", "BBB-EQ", "CCC-EQ"]
        api = FakeAPI({s: [candle(date(2026, 11, 21))] for s in syms})
        r = self.refill(api, syms)
        self.assertEqual(r["calls_made"], 5)  # 3 stocks + 2 benchmark chunks
        self.assertEqual(r["stock_calls"], 3)
        self.assertEqual(r["benchmark_calls"], 2)
        # stocks get one 364-day call each; the benchmark gets two chunks
        # of its 465-day window (2025-08-13..2026-08-12, 2026-08-13..2026-11-21)
        stock_calls = [c for c in api.calls if c[0] in syms]
        for sym, start, end in stock_calls:
            self.assertEqual(start, "2025-11-22")
            self.assertEqual(end, "2026-11-21")
        bench_calls = [c for c in api.calls if c[0] == "NIFTY50-INDEX"]
        self.assertEqual(
            [(c[1], c[2]) for c in bench_calls],
            [("2025-08-13", "2026-08-12"), ("2026-08-13", "2026-11-21")],
        )

    def test_budget_cap_respected_and_remaining_deferred(self):
        syms = ["A%d-EQ" % i for i in range(5)]
        api = FakeAPI({})
        r = self.refill(api, syms, max_calls=2)
        self.assertEqual(r["calls_made"], 2)
        self.assertEqual(r["stopped"], "budget_exhausted")
        self.assertEqual(r["symbols_done"], ["A0-EQ", "A1-EQ"])  # finished first
        self.assertEqual(len(r["symbols_deferred"]), 5)  # 3 stocks + 2 benchmark
        self.assertEqual(r["symbols_failed"], [])

    def test_idempotent_rerun_no_duplicates(self):
        api = FakeAPI({"AAA-EQ": [candle(date(2026, 11, 20)), candle(date(2026, 11, 21))]})
        self.refill(api, ["AAA-EQ"])
        r2 = self.refill(api, ["AAA-EQ"])
        rows = self.db.query("SELECT * FROM daily_ohlcv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(r2["candles_written"], 2)  # attempted again, ignored by PK
        self.assertEqual(self.db.query("SELECT COUNT(*) c FROM daily_ohlcv")[0]["c"], 2)

    def test_pacing_at_least_1_3s_between_calls(self):
        api = FakeAPI({"AAA-EQ": [], "BBB-EQ": []})
        self.refill(api, ["AAA-EQ", "BBB-EQ"])
        # 4 calls (2 stocks + 2 benchmark) -> 3 gaps
        self.assertEqual(len(self.sleep.calls), 3)
        for gap in self.sleep.calls:
            self.assertGreaterEqual(gap, 1.2)  # 1.3 minus real-clock sliver

    def test_retry_counts_toward_budget_and_persists_then_fails_cleanly(self):
        api = FakeAPI({"AAA-EQ": [candle(date(2026, 11, 21))]}, fail_first={"AAA-EQ": 1})
        r = self.refill(api, ["AAA-EQ"], max_attempts=3)
        self.assertEqual(api.attempts["AAA-EQ"], 2)
        self.assertEqual(r["calls_made"], 4)  # 2 stock attempts + 2 benchmark
        self.assertEqual(r["candles_written"], 1)

        api2 = FakeAPI({}, fail_first={"BBB-EQ": 99})
        r2 = self.refill(api2, ["BBB-EQ"], max_attempts=3)
        self.assertEqual(api2.attempts["BBB-EQ"], 3)
        self.assertIn("BBB-EQ", r2["symbols_failed"])
        self.assertIsNone(r2["stopped"])  # failure is per-symbol, run continues
        self.assertEqual(r2["benchmark_calls"], 2)

    def test_full_universe_call_count_is_694(self):
        syms = ["SYM%04d-EQ" % i for i in range(692)]
        api = FakeAPI({})
        r = self.refill(api, syms)
        self.assertEqual(r["calls_made"], 694)  # 692 stocks + 2 benchmark chunks
        self.assertEqual(r["stock_calls"], 692)
        self.assertEqual(r["benchmark_calls"], 2)
        self.assertEqual(len(r["symbols_done"]), 693)  # 692 stocks + NIFTY50-INDEX

    def test_raw_sqlite3_connection_accepted_as_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE daily_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,turnover REAL,"
            "source TEXT,loaded_at TEXT,PRIMARY KEY(trade_date,symbol))"
        )
        conn.execute(
            "CREATE TABLE index_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,source TEXT,"
            "loaded_at TEXT,PRIMARY KEY(trade_date,symbol))"
        )
        api = FakeAPI({"AAA-EQ": [candle(date(2026, 11, 21))]})
        r = rh.refill(conn, api, TARGET, ["AAA-EQ"], sleep=self.sleep, now=lambda: T)
        self.assertEqual(r["candles_written"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_ohlcv").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
