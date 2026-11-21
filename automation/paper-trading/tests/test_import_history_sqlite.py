"""Tests for tools/import_history_sqlite.py — copy daily_ohlcv / index_ohlcv
from a local sqlite file (e.g. /workspace/adaptive-live/state/market.sqlite)
into the target DB. Hermetic: fixture sqlite files in tmp dirs, no network.
"""
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import import_history_sqlite as ih

IST = timezone(timedelta(hours=5, minutes=30))
T = datetime(2026, 11, 21, 12, 0, tzinfo=IST)

DAILY_COLS = (
    "trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at"
)
INDEX_COLS = "trade_date,symbol,open,high,low,close,volume,source,loaded_at"


def make_source(path, daily_rows=(), index_rows=(), extra_table=True, extra_col=False):
    c = sqlite3.connect(str(path))
    daily_def = (
        "trade_date TEXT,symbol TEXT,open REAL,high REAL,low REAL,close REAL,"
        "volume REAL,turnover REAL,source TEXT,loaded_at TEXT,extra TEXT"
        if extra_col
        else "trade_date TEXT,symbol TEXT,open REAL,high REAL,low REAL,close REAL,"
        "volume REAL,turnover REAL,source TEXT,loaded_at TEXT"
    )
    c.execute("CREATE TABLE daily_ohlcv(%s)" % daily_def)
    c.execute(
        "CREATE TABLE index_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
        "high REAL,low REAL,close REAL,volume REAL,source TEXT,loaded_at TEXT)"
    )
    if extra_table:
        c.execute("CREATE TABLE paper_runs(id INTEGER,note TEXT)")
        c.execute("INSERT INTO paper_runs VALUES(1,'not history')")
    for r in daily_rows:
        c.execute(
            "INSERT INTO daily_ohlcv VALUES(%s)" % ",".join("?" * len(r)), r
        )
    for r in index_rows:
        c.execute("INSERT INTO index_ohlcv VALUES(?,?,?,?,?,?,?,?,?)", r)
    c.commit()
    c.close()


def daily_row(d="2026-11-20", sym="AAA-EQ", close=100):
    return (d, sym, 99, 101, 98, close, 1000, close * 1000, "SRC", T.isoformat())


def index_row(d="2026-11-20", sym="NIFTY50-INDEX", close=25000):
    return (d, sym, 24900, 25100, 24800, close, 0, "SRC", T.isoformat())


class TargetDB:
    """Empty target with the real table shapes (PK = idempotency key)."""

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


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "market.sqlite"
        self.db = TargetDB()

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_rows_exactly_and_ignores_extra_tables_and_columns(self):
        d1 = daily_row()
        d2 = daily_row("2026-11-19", "BBB-EQ", 50)
        i1 = index_row()
        # extra column present in source daily_ohlcv; extra table paper_runs too
        make_source(
            self.src,
            daily_rows=[d1 + ("junk",), d2 + ("junk",)],
            index_rows=[i1],
            extra_col=True,
        )
        r = ih.import_history(str(self.src), self.db.c)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["copied"], {"daily_ohlcv": 2, "index_ohlcv": 1})
        self.assertEqual(r["skipped"], {"daily_ohlcv": 0, "index_ohlcv": 0})
        got = [tuple(x) for x in self.db.c.execute(
            "SELECT %s FROM daily_ohlcv ORDER BY symbol" % DAILY_COLS
        )]
        # sqlite returns REAL columns as floats; normalise the fixture ints
        def as_floats(row):
            return tuple(
                float(v) if isinstance(v, (int, float)) else v for v in row
            )
        self.assertEqual([as_floats(r) for r in got],
                         [as_floats(d1), as_floats(d2)])
        got_i = [tuple(x) for x in self.db.c.execute(
            "SELECT %s FROM index_ohlcv" % INDEX_COLS
        )]
        self.assertEqual(got_i, [i1])
        # extra table was NOT copied anywhere
        tables = [x[0] for x in self.db.c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertNotIn("paper_runs", tables)

    def test_reimport_is_idempotent(self):
        make_source(self.src, daily_rows=[daily_row()], index_rows=[index_row()])
        r1 = ih.import_history(str(self.src), self.db.c)
        r2 = ih.import_history(str(self.src), self.db.c)
        self.assertEqual(r1["copied"], {"daily_ohlcv": 1, "index_ohlcv": 1})
        self.assertEqual(r2["copied"], {"daily_ohlcv": 0, "index_ohlcv": 0})
        self.assertEqual(r2["skipped"], {"daily_ohlcv": 1, "index_ohlcv": 1})
        self.assertEqual(r2["errors"], [])
        n = self.db.c.execute("SELECT COUNT(*) FROM daily_ohlcv").fetchone()[0]
        self.assertEqual(n, 1)

    def test_missing_source_table_gives_clear_error_not_crash(self):
        c = sqlite3.connect(str(self.src))
        c.execute(
            "CREATE TABLE daily_ohlcv(trade_date TEXT,symbol TEXT,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,turnover REAL,"
            "source TEXT,loaded_at TEXT)"
        )
        c.execute("INSERT INTO daily_ohlcv VALUES(?,?,?,?,?,?,?,?,?,?)", daily_row())
        c.commit()
        c.close()
        r = ih.import_history(str(self.src), self.db.c)
        self.assertEqual(r["copied"]["daily_ohlcv"], 1)
        self.assertEqual(r["copied"]["index_ohlcv"], 0)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("index_ohlcv", r["errors"][0])
        self.assertIn("missing", r["errors"][0].lower())

    def test_missing_source_file_gives_clear_error_not_crash(self):
        r = ih.import_history(str(Path(self.tmp.name) / "nope.sqlite"), self.db.c)
        self.assertEqual(r["copied"], {"daily_ohlcv": 0, "index_ohlcv": 0})
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("not found", r["errors"][0].lower())

    def test_missing_target_table_gives_clear_error(self):
        make_source(self.src, daily_rows=[daily_row()], index_rows=[index_row()])
        self.db.c.execute("DROP TABLE index_ohlcv")
        r = ih.import_history(str(self.src), self.db.c)
        self.assertEqual(r["copied"]["daily_ohlcv"], 1)
        self.assertEqual(r["copied"]["index_ohlcv"], 0)
        self.assertTrue(any("index_ohlcv" in e for e in r["errors"]))

    def test_partial_overlap_only_new_rows_copied(self):
        make_source(
            self.src,
            daily_rows=[daily_row("2026-11-20"), daily_row("2026-11-19")],
            index_rows=[index_row()],
        )
        self.db.c.execute(
            "INSERT INTO daily_ohlcv VALUES(?,?,?,?,?,?,?,?,?,?)",
            daily_row("2026-11-20"),
        )
        r = ih.import_history(str(self.src), self.db.c)
        self.assertEqual(r["copied"]["daily_ohlcv"], 1)
        self.assertEqual(r["skipped"]["daily_ohlcv"], 1)
        self.assertEqual(r["copied"]["index_ohlcv"], 1)

    def test_query_batch_db_handle_accepted(self):
        """Target may be an engine-style .query/.batch handle, not sqlite3."""
        class Handle:
            def __init__(self, target):
                self.t = target

            def query(self, s, args=()):
                cur = self.t.c.execute(s, args)
                cols = [d[0] for d in cur.description] if cur.description else []
                return [dict(zip(cols, row)) for row in cur.fetchall()]

            def batch(self, stm):
                with self.t.c:
                    for s, args in stm:
                        self.t.c.execute(s, args)

        make_source(self.src, daily_rows=[daily_row()], index_rows=[index_row()])
        r = ih.import_history(str(self.src), Handle(self.db))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["copied"], {"daily_ohlcv": 1, "index_ohlcv": 1})


if __name__ == "__main__":
    unittest.main()
