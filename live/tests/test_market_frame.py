"""Tests for live/market_frame.py (chunked bars loader).

The loader must return a frame identical to pd.read_sql on the same
query (same values, same column order, same dtypes, same row order) --
the sim's consumers depend on object symbol/trade_date and float64
OHLCV -- while avoiding read_sql's fetchall transient (which retains
~140 MB of allocator arenas on a 403k-row snapshot).
"""
import sqlite3, sys, unittest
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LIVE = HERE.parent
LIB = LIVE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIVE))
sys.path.insert(0, str(LIB))

import market_frame as mf  # noqa: E402


class LoadBarsFrameTests(unittest.TestCase):
    def _conn(self):
        c = sqlite3.connect(':memory:')
        c.execute('CREATE TABLE daily_ohlcv (symbol TEXT, trade_date TEXT, '
                  'open REAL, high REAL, low REAL, close REAL, volume REAL)')
        rows = [
            ('AAA-EQ', '2026-11-20', 100.0, 101.0, 99.0, 100.5, 1000.0),
            ('AAA-EQ', '2026-11-21', 100.5, 102.0, 100.0, 101.5, 2000.0),
            ('BBB-EQ', '2026-11-20', 200.0, 201.0, 199.0, 200.5, 3000.0),
            ('AAA-EQ', '2026-11-23', 101.5, 103.0, 101.0, 102.5, 4000.0),
        ]
        c.executemany('INSERT INTO daily_ohlcv VALUES (?,?,?,?,?,?,?)', rows)
        c.commit()
        return c

    def test_matches_read_sql(self):
        c = self._conn()
        try:
            sql = ('SELECT symbol,trade_date,open,high,low,close,volume '
                   'FROM daily_ohlcv WHERE trade_date<=? '
                   'ORDER BY symbol,trade_date')
            expected = pd.read_sql(sql, c, params=('2026-11-23',))
            got = mf.load_bars_frame(c, sql, ('2026-11-23',))
            self.assertTrue(got.equals(expected))
            self.assertEqual(list(got.columns), list(expected.columns))
            self.assertTrue((got.dtypes == expected.dtypes).all())
            self.assertTrue((got.index == expected.index).all())
        finally:
            c.close()

    def test_load_stock_frame_filters_as_of(self):
        c = self._conn()
        try:
            got = mf.load_stock_frame(c, '2026-11-21')
            self.assertEqual(len(got), 3)
            self.assertEqual(sorted(got['trade_date'].unique().tolist()),
                             ['2026-11-20', '2026-11-21'])
            self.assertEqual(list(got.columns), list(mf._COLUMNS))
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main()
