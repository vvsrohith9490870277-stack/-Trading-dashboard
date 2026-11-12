"""Persist the simulation's closed-trade tape into local adaptive SQLite.

Writes only values present on each simulate_track_window closed_trades record.
Incomplete rows are skipped, never filled in. Reruns upsert on
(namespace, spawn_id, symbol, entry_date, exit_date).
"""

TABLE_SQL = (
    'CREATE TABLE IF NOT EXISTS adaptive_sim_trades('
    'namespace TEXT NOT NULL,'
    'spawn_id TEXT NOT NULL,'
    'strategy INTEGER NOT NULL,'
    'session_day TEXT NOT NULL,'
    'symbol TEXT NOT NULL,'
    'side TEXT,'
    'entry_date TEXT NOT NULL,'
    'entry_price REAL NOT NULL,'
    'exit_date TEXT NOT NULL,'
    'exit_price REAL NOT NULL,'
    'exit_reason TEXT,'
    'qty INTEGER NOT NULL,'
    'pnl_paise INTEGER NOT NULL,'
    'track TEXT,'
    'recorded_at TEXT NOT NULL,'
    'PRIMARY KEY(namespace, spawn_id, symbol, entry_date, exit_date))'
)

_REQUIRED = ('symbol', 'entry_date', 'entry_price', 'exit_date', 'exit_price', 'qty', 'net_pnl_paise')

_INSERT = (
    'INSERT OR REPLACE INTO adaptive_sim_trades('
    'namespace,spawn_id,strategy,session_day,symbol,side,'
    'entry_date,entry_price,exit_date,exit_price,exit_reason,qty,pnl_paise,track,recorded_at'
    ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)


def ensure_table(db):
    db.batch([(TABLE_SQL, ())])


def _complete(trade):
    if not isinstance(trade, dict):
        return False
    for key in _REQUIRED:
        val = trade.get(key)
        if val is None or val == '':
            return False
    return True


def persist_sim_trades(db, namespace, spawn_id, strategy, session_day, closed_trades, recorded_at, track=None):
    """Upsert closed trades from one simulation. Returns written/skipped counts."""
    ensure_table(db)
    rows = []
    skipped = 0
    for trade in closed_trades or []:
        if not _complete(trade):
            skipped += 1
            continue
        rows.append((_INSERT, (
            namespace,
            spawn_id,
            int(strategy),
            session_day,
            str(trade['symbol']),
            trade.get('side'),
            str(trade['entry_date']),
            float(trade['entry_price']),
            str(trade['exit_date']),
            float(trade['exit_price']),
            trade.get('reason'),
            int(trade['qty']),
            int(trade['net_pnl_paise']),
            track,
            recorded_at,
        )))
    if rows:
        db.batch(rows)
    return {'written': len(rows), 'skipped_incomplete': skipped}
