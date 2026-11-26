"""Efficient bars-frame loading for the adaptive sim.

pd.read_sql loads a 403k-row snapshot with a fetchall transient that
retains ~140 MB of allocator arenas (the frame itself is only ~64 MB),
which is a large share of the sim child's peak RSS. load_bars_frame
streams the cursor in chunks and accumulates the numeric columns as
C-double arrays (not Python floats), so the peak transient is one chunk
plus the frame itself -- no fetchall, no dtype change.

The returned frame has EXACTLY the dtypes pd.read_sql produces
(object symbol/trade_date, float64 OHLCV), so every consumer sees the
same frame it saw before: no Categorical (an ordered-Categorical
trade_date breaks snapshot_hash's `>= start` comparison for a start
date with no bars, and changes the snapshot hash). The row order is the
SQL's ORDER BY, unchanged.
"""
import sqlite3
from array import array

import numpy as np
import pandas as pd

_COLUMNS = ('symbol', 'trade_date', 'open', 'high', 'low', 'close', 'volume')
_CHUNK = 20000


def load_bars_frame(conn, sql, params=()):
    """Run sql (a bars SELECT in _COLUMNS order) and return the frame.

    conn is an open sqlite3 connection (or anything with .execute). The
    frame's columns are _COLUMNS in that order; symbol/trade_date are
    object dtype, OHLCV are float64 -- identical to pd.read_sql.
    """
    cur = conn.execute(sql, params)
    syms, dates = [], []
    o, h, l, c, v = array('d'), array('d'), array('d'), array('d'), array('d')
    while True:
        rows = cur.fetchmany(_CHUNK)
        if not rows:
            break
        for sym, d, op, hi, lo, cl, vol in rows:
            syms.append(sym)
            dates.append(d)
            o.append(op)
            h.append(hi)
            l.append(lo)
            c.append(cl)
            v.append(vol)
    return pd.DataFrame({
        'symbol': np.array(syms, dtype=object),
        'trade_date': np.array(dates, dtype=object),
        'open': np.array(o, dtype=np.float64),
        'high': np.array(h, dtype=np.float64),
        'low': np.array(l, dtype=np.float64),
        'close': np.array(c, dtype=np.float64),
        'volume': np.array(v, dtype=np.float64),
    })


def load_stock_frame(conn, as_of):
    """The daily_ohlcv bars frame for market_frames / load_market_sqlite:
    all symbols, trade_date <= as_of, ORDER BY symbol, trade_date."""
    return load_bars_frame(
        conn,
        'SELECT symbol,trade_date,open,high,low,close,volume FROM daily_ohlcv '
        'WHERE trade_date<=? ORDER BY symbol,trade_date',
        (str(as_of)[:10],))
