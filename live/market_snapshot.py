"""Incremental read-only copy of Track A market inputs into local SQLite for the adaptive runner.
Reads Turso (SELECT-only). Writes only /workspace/adaptive-live/state/market.sqlite."""
import json, sqlite3, sys
from datetime import date, timedelta
from pathlib import Path
from ro_client import TursoRO

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
DB_PATH = HERE.parent / 'state' / 'market.sqlite'
TABLES = ('daily_ohlcv', 'index_ohlcv', 'paper_corporate_actions', 'paper_price_bands')

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def refresh(ro=None, log=print):
    ro = ro or TursoRO()
    c = connect()
    ddl = {r['name']: r['sql'] for r in ro.query(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN (%s)" % ','.join('?' * len(TABLES)), list(TABLES))}
    for t in TABLES:
        c.execute(ddl[t].replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1))
    have = c.execute('SELECT MAX(trade_date) FROM daily_ohlcv').fetchone()[0]
    start = (date.fromisoformat(have) - timedelta(days=CFG['snapshot_overlap_calendar_days'])).isoformat() if have else \
        (date.today() - timedelta(days=CFG['snapshot_history_calendar_days'])).isoformat()
    report = {'from': start}
    # daily_ohlcv in monthly slices to keep each response bounded
    cur = date.fromisoformat(start); end = date.today(); total = 0
    while cur <= end:
        nxt = min(end, cur + timedelta(days=CFG['snapshot_slice_calendar_days']))
        rows = ro.query('SELECT trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at FROM daily_ohlcv WHERE trade_date>=? AND trade_date<=?',
                        (cur.isoformat(), nxt.isoformat()))
        c.executemany('INSERT OR REPLACE INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      [(r['trade_date'], r['symbol'], r['open'], r['high'], r['low'], r['close'], r['volume'], r['turnover'], r['source'], r['loaded_at']) for r in rows])
        total += len(rows); cur = nxt + timedelta(days=1)
    report['daily_ohlcv_rows_copied'] = total
    for t, col in (('index_ohlcv', 'trade_date'), ('paper_price_bands', 'trade_date'), ('paper_corporate_actions', 'ex_date')):
        rows = ro.query(f'SELECT * FROM {t} WHERE {col}>=?', (start,))
        if rows:
            cols = list(rows[0]); c.executemany(f'INSERT OR REPLACE INTO {t}({",".join(cols)}) VALUES({",".join("?"*len(cols))})', [tuple(r[k] for k in cols) for r in rows])
        report[f'{t}_rows_copied'] = len(rows)
    c.commit()
    report['local_max_trade_date'] = c.execute('SELECT MAX(trade_date) FROM daily_ohlcv').fetchone()[0]
    report['local_daily_rows'] = c.execute('SELECT COUNT(*) FROM daily_ohlcv').fetchone()[0]
    c.close()
    return report

if __name__ == '__main__':
    print(json.dumps(refresh(), indent=1))
