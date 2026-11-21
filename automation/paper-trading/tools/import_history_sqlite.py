"""Import daily_ohlcv / index_ohlcv from a local sqlite file into the target DB.

Source is typically /workspace/adaptive-live/state/market.sqlite (Turso ->
local direction, written by live/market_snapshot.py). This is the first
local -> Turso importer: it copies the two OHLCV tables into the target
handle with INSERT OR IGNORE, preserving every column the target expects.

Rules:
  * only the two OHLCV tables are copied; extra source tables are ignored;
  * only the columns the target expects are copied; extra source columns
    are ignored (a missing `turnover` is derived as close x volume, a
    missing `loaded_at` is stamped now);
  * re-import is idempotent: rows already present (same trade_date+symbol)
    are counted as skipped, never duplicated;
  * a missing source table / file / target table is a clear error in the
    report, not a crash.

The target db is an engine-style .query/.batch handle or a sqlite3
connection. No credentials live here and nothing prints a secret.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

EXPECTED_COLUMNS = {
    "daily_ohlcv": [
        "trade_date", "symbol", "open", "high", "low", "close",
        "volume", "turnover", "source", "loaded_at",
    ],
    "index_ohlcv": [
        "trade_date", "symbol", "open", "high", "low", "close",
        "volume", "source", "loaded_at",
    ],
}
BATCH_SIZE = 500


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


def _table_columns(db, table):
    """Column names of a target table, or None if the table is missing."""
    try:
        rows = db.query("PRAGMA table_info(%s)" % table)
    except Exception:
        return None
    if not rows:
        return None
    return [r["name"] for r in rows]


def import_history(source_path, db, now=None, batch_size=BATCH_SIZE):
    """Copy the two OHLCV tables from `source_path` into `db`.

    Returns {"source": ..., "copied": {table: n}, "skipped": {table: n},
             "errors": [...]}. Errors are reported, never raised, except
    for a badly-typed `db` argument.
    """
    db = _as_db(db)
    now = now or (lambda: datetime.now(IST))
    report = {
        "source": str(source_path),
        "copied": {t: 0 for t in EXPECTED_COLUMNS},
        "skipped": {t: 0 for t in EXPECTED_COLUMNS},
        "errors": [],
    }
    if not Path(source_path).exists():
        report["errors"].append("source file not found: %s" % source_path)
        return report
    try:
        src = sqlite3.connect("file:%s?mode=ro" % source_path, uri=True)
    except sqlite3.Error as ex:
        report["errors"].append("cannot open source sqlite %s: %s" % (source_path, ex))
        return report
    try:
        src_tables = {
            r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, expected in EXPECTED_COLUMNS.items():
            if table not in src_tables:
                report["errors"].append(
                    "source table %s missing in %s" % (table, source_path)
                )
                continue
            target_cols = _table_columns(db, table)
            if target_cols is None:
                report["errors"].append(
                    "target table %s missing" % table
                )
                continue
            src_cols = [
                r[1] for r in src.execute("PRAGMA table_info(%s)" % table)
            ]
            copy_cols = [c for c in expected if c in src_cols and c in target_cols]
            missing = [c for c in expected if c not in copy_cols]
            if missing and not (
                set(missing) <= {"turnover", "loaded_at"}
            ):
                report["errors"].append(
                    "source table %s is missing required columns: %s"
                    % (table, ", ".join(missing))
                )
                continue
            # Rows already in the target (same trade_date+symbol) are skipped.
            existing = {
                (str(r["trade_date"]), str(r["symbol"]))
                for r in db.query(
                    "SELECT trade_date, symbol FROM %s" % table
                )
            }
            loaded_at = now().isoformat()
            placeholders = ",".join("?" * len(expected))
            sql = "INSERT OR IGNORE INTO %s(%s) VALUES(%s)" % (
                table, ",".join(expected), placeholders,
            )
            pending = []
            for row in src.execute(
                "SELECT %s FROM %s" % (",".join(copy_cols), table)
            ):
                values = dict(zip(copy_cols, row))
                if "turnover" in missing and "turnover" in expected:
                    try:
                        values["turnover"] = float(values["close"]) * float(
                            values["volume"]
                        )
                    except (TypeError, ValueError, KeyError):
                        values["turnover"] = None
                if "loaded_at" in missing and "loaded_at" in expected:
                    values["loaded_at"] = loaded_at
                key = (str(values["trade_date"]), str(values["symbol"]))
                if key in existing:
                    report["skipped"][table] += 1
                    continue
                existing.add(key)
                pending.append((sql, tuple(values[c] for c in expected)))
                if len(pending) >= batch_size:
                    db.batch(pending)
                    report["copied"][table] += len(pending)
                    pending.clear()
            if pending:
                db.batch(pending)
                report["copied"][table] += len(pending)
    finally:
        src.close()
    return report


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", help="source sqlite file, e.g. market.sqlite")
    p.add_argument("--sqlite", required=True, help="target sqlite file")
    args = p.parse_args()
    conn = sqlite3.connect(args.sqlite)
    report = import_history(args.source, conn)
    print(report)
    sys.exit(1 if report["errors"] else 0)
