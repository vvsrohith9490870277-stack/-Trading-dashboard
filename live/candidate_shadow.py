"""Candidate shadow runner (B3): evaluate registry challengers 101-108 and 109 in
shadow / observation mode only.

- Uses adaptive_engine.simulate_track_window with the signal_evaluator hook,
  so candidates get the SAME frozen sizing and costs as Track A (sleeve
  capital and slots, ADV floor, participation, circuit/lock rules, next-open
  fills, charges, slippage including the B1 base-only fallback).
- Writes the sidecar adaptive.sqlite tables adaptive_candidate_daily
  and adaptive_candidate_trades, and (only when a turso_db is passed, or
  --turso on the CLI) mirrors the SAME rows to the Turso tables of the same
  names, so the daily shadow check can read them. Never writes to
  paper_v1 / paper_demo_v1 / paper_v2, never places or queues orders, never
  appears in Track A's decision path. Every row is marked mode='shadow' and
  simulated=1.
- The sidecar namespace must be an adaptive namespace (adaptive_v1); a
  protected trading namespace is refused for BOTH sinks, before any write.
- The Turso mirror goes through live/turso_write.py (node db.mjs, the same
  write path as the Track A engine); the mirror never sees a credential —
  db.mjs reads the .env file itself. A failed mirror raises: the local
  sidecar rows are already written (idempotent INSERT OR REPLACE), so a
  later re-run repairs Turso; a silent partial mirror is never accepted.

Usage (offline):
  python candidate_shadow.py --db state/adaptive.sqlite --session YYYY-MM-DD --market FILE.csv
  python candidate_shadow.py --db state/adaptive.sqlite --session YYYY-MM-DD --market-sqlite state/market.sqlite --turso
  (--market reads a parquet/csv snapshot; --market-sqlite reads daily_ohlcv
  from the local market snapshot the adaptive runner refreshes. --turso
  mirrors the results to Turso as well as the sidecar.)
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
for p in (str(HERE), str(LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import adaptive_candidates as ac
import adaptive_engine as ae

# Sector source for C106 (Grok's decision, 2026-11-22): the Industry column
# of automation/paper-trading/universe.csv, which covers all 692 stocks.
# The NSE official list matched only 545, so it is NOT used.
UNIVERSE_CSV = HERE.parent / 'automation' / 'paper-trading' / 'universe.csv'


def load_universe_sector_map(csv_path=None):
    """Load {db_symbol: Industry} from universe.csv.

    Keyed by db_symbol (e.g. '3MINDIA-EQ') because the price bars and the
    universe frame use db_symbol. Rows with an empty db_symbol or an empty
    Industry are skipped (C106 then has no sector for that symbol and
    enters nothing for it, the safe default). Raises FileNotFoundError if
    the CSV does not exist, ValueError if it yields no usable rows.
    """
    path = Path(csv_path) if csv_path is not None else UNIVERSE_CSV
    if not path.exists():
        raise FileNotFoundError(f'universe_csv_missing:{path}')
    out = {}
    with path.open('r', encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            db_symbol = (row.get('db_symbol') or '').strip()
            industry = (row.get('Industry') or '').strip()
            if db_symbol and industry:
                out[db_symbol] = industry
    if not out:
        raise ValueError(f'universe_csv_no_sectors:{path}')
    return out


def with_sector_column(stock_df, sector_map):
    """Return a copy of the bars frame with a 'sector' column attached,
    mapped from the frame's 'symbol' (db_symbol) through sector_map.
    Symbols with no sector get NaN (C106 skips them safely). The extra
    column does not affect the other candidates: _universe_closes pivots
    only on symbol/trade_date/close, and candidate_signals passes the
    sector map only to C106."""
    frame = stock_df.copy()
    if sector_map:
        frame['sector'] = frame['symbol'].map(sector_map)
    return frame

PROTECTED_NAMESPACES = frozenset({'paper_v1', 'paper_demo_v1', 'paper_v2'})
MODE = 'shadow'


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def init(db):
    """Sidecar tables for shadow challenger results (adaptive_candidate_* only)."""
    db.batch([(
        'CREATE TABLE IF NOT EXISTS adaptive_candidate_daily'
        '(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, trade_date TEXT NOT NULL,'
        ' equity_paise INTEGER NOT NULL, mode TEXT NOT NULL, simulated INTEGER NOT NULL,'
        ' recorded_at TEXT NOT NULL, PRIMARY KEY(namespace, strategy, trade_date))', (),
    ), (
        'CREATE TABLE IF NOT EXISTS adaptive_candidate_trades'
        '(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, symbol TEXT NOT NULL,'
        ' entry_date TEXT NOT NULL, exit_date TEXT, qty INTEGER NOT NULL,'
        ' entry_price REAL NOT NULL, exit_price REAL, net_pnl_paise INTEGER,'
        ' mode TEXT NOT NULL, simulated INTEGER NOT NULL, recorded_at TEXT NOT NULL,'
        ' PRIMARY KEY(namespace, strategy, symbol, entry_date))', (),
    )])


def _check_namespace(namespace):
    if str(namespace) in PROTECTED_NAMESPACES:
        raise ValueError(f'protected_namespace:{namespace}')


def make_evaluator(strategy, stock_df, sector_map=None):
    """signal_evaluator for simulate_track_window: registry signals for one
    candidate. For 101 (derived_rs_rank), 105 (cross-sectional rank) and
    106 (sector-adjusted rank) the full universe panel is captured so ranks
    are computed point-in-time (bars up to each signal date only). 106 also
    needs a symbol->sector map (explicit sector_map, or a 'sector' /
    'industry' column on the panel); without one it enters nothing."""
    s = int(strategy)
    if s in (101, 105, 106):
        # Precompute 101/105/106 signals once on the full panel (each rank at a
        # date uses only closes up to that date, so this is PIT-safe).
        # Corp-action back-adjustment is not applied to the rank panel; the
        # simulator's adj_hist is used only for the fill prices.
        precomputed = {}
        for sym, sdf in stock_df.groupby('symbol'):
            precomputed[str(sym)] = ac.candidate_signals(s, sdf.reset_index(drop=True), universe=stock_df,
                                                          sector_map=sector_map)

        def evaluator(_s, adj_hist):
            sym = str(adj_hist['symbol'].iloc[0])
            frame = precomputed[sym]
            day = str(adj_hist['trade_date'].iloc[-1])
            return frame[frame['trade_date'].astype(str) <= day]
    else:
        def evaluator(_s, adj_hist):
            return ac.candidate_signals(s, adj_hist)
    return evaluator


def _trade_statements(namespace, strategy, stamp, trades):
    """INSERT OR REPLACE statements for adaptive_candidate_trades rows."""
    return [(
        'INSERT OR REPLACE INTO adaptive_candidate_trades'
        '(namespace,strategy,symbol,entry_date,exit_date,qty,entry_price,exit_price,net_pnl_paise,mode,simulated,recorded_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(namespace), int(strategy), str(t['symbol']), str(t['entry_date']), str(t['exit_date']),
         int(t['qty']), float(t['entry_price']), float(t['exit_price']),
         int(t['net_pnl_paise']), MODE, 1, stamp),
    ) for t in trades]


def run_shadow(db, namespace, stock_df, index_df, warm_start_dates, eval_dates,
               strategies=None, session_day=None, sector_map=None, turso_db=None):
    """Evaluate candidates in shadow mode and persist the results.

    Returns {strategy: {'status': 'SHADOW_SIMULATED', 'closed_trades': n,
    'open_positions': n, 'eval_days': n, ...}}. Raises ValueError for a
    protected namespace. Writes adaptive_candidate_* tables to the sidecar
    db, and — only when turso_db is given — mirrors the identical rows to
    the Turso tables of the same names. A failed mirror raises after the
    sidecar write; the sidecar rows are idempotent, so a re-run repairs
    Turso. Never writes to a paper namespace.
    """
    _check_namespace(namespace)
    init(db)
    if turso_db is not None:
        init(turso_db)
    strategies = sorted(strategies) if strategies else sorted(ac.CANDIDATE_STRATEGIES)
    out = {}
    for s in strategies:
        c = ac.CANDIDATE_STRATEGIES[int(s)]
        ws = int(c['warm_start_days'])
        ws_dates = [str(d) for d in warm_start_dates][-ws:]
        ev_dates = [str(d) for d in eval_dates]
        evaluator = make_evaluator(s, stock_df, sector_map=sector_map)
        res = ae.simulate_track_window(db, namespace, s, dict(c['params']), stock_df, index_df,
                                       ws_dates, ev_dates, signal_evaluator=evaluator)
        stamp = _utc_now()
        day = session_day or (ev_dates[-1] if ev_dates else ws_dates[-1])
        daily_stmts = [(
            'INSERT OR REPLACE INTO adaptive_candidate_daily'
            '(namespace,strategy,trade_date,equity_paise,mode,simulated,recorded_at) VALUES(?,?,?,?,?,?,?)',
            (str(namespace), int(s), str(r['trade_date']), int(r['equity_paise']), MODE, 1, stamp),
        ) for r in res['daily_records']]
        trade_stmts = _trade_statements(namespace, s, stamp, res['closed_trades'])
        db.batch(daily_stmts)
        db.batch(trade_stmts)
        if turso_db is not None:
            turso_db.batch(daily_stmts)
            turso_db.batch(trade_stmts)
        out[int(s)] = {'status': 'SHADOW_SIMULATED', 'mode': MODE, 'simulated': True,
                       'closed_trades': len(res['closed_trades']),
                       'open_positions': len(res['open_positions']),
                       'eval_days': len(ev_dates),
                       'warm_start_days': ws, 'session_day': day}
    return out


def load_market_sqlite(path, session_day):
    """Read the market snapshot from a local market.sqlite (the file the
    adaptive runner refreshes), same columns as the --market CSV/parquet
    path. Returns (stock_df, index_df); the index frame is the NIFTY500
    rows, exactly as the --market path selects them. Bars are capped at
    the session day, so no future bar can leak into the warm-start."""
    import sqlite3
    import pandas as pd
    c = sqlite3.connect(str(path))
    try:
        stock_df = pd.read_sql(
            'SELECT symbol,trade_date,open,high,low,close,volume FROM daily_ohlcv '
            'WHERE trade_date<=? ORDER BY symbol,trade_date',
            c, params=(str(session_day)[:10],))
    finally:
        c.close()
    idx = stock_df[stock_df['symbol'] == 'NIFTY500']
    return stock_df, idx


def main(argv=None, turso_writer=None):
    ap = argparse.ArgumentParser(description='Shadow-evaluate candidate strategies 101-108 and 109 (observation only).')
    ap.add_argument('--db', required=True, help='sidecar adaptive.sqlite path')
    ap.add_argument('--session', required=True, help='session day YYYY-MM-DD')
    ap.add_argument('--namespace', default='adaptive_v1')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--market', default=None, help='market snapshot parquet/csv with OHLCV bars')
    group.add_argument('--market-sqlite', default=None, help='local market.sqlite snapshot (as refreshed by the adaptive runner)')
    ap.add_argument('--turso', action='store_true',
                    help='also mirror the shadow results to the Turso adaptive_candidate_* tables')
    a = ap.parse_args(argv)
    _check_namespace(a.namespace)
    import sqlite3
    import pandas as pd
    if a.market_sqlite:
        stock_df, idx = load_market_sqlite(a.market_sqlite, a.session)
    else:
        stock_df = pd.read_csv(a.market) if str(a.market).endswith('.csv') else pd.read_parquet(a.market)
        idx = stock_df[stock_df['symbol'] == 'NIFTY500']
    days = sorted(stock_df['trade_date'].astype(str).unique())
    ws_dates, ev_dates = days[:-1], days[-1:]
    # C106 sector map (Grok, 2026-11-22): universe.csv Industry by
    # db_symbol. Attach it to the universe frame and pass it explicitly
    # to run_shadow, so C106 can rank on the real universe. Without it
    # C106 enters nothing (the safe default, as before).
    sector_map = load_universe_sector_map()
    stock_df = with_sector_column(stock_df, sector_map)
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    class DB:
        def batch(self, stm):
            with c:
                for sql, args in stm:
                    c.execute(sql, args)

        def query(self, sql, args=()):
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    turso_db = None
    if a.turso:
        # turso_writer is the test seam: production constructs the real
        # TursoWrite (node db.mjs, credentials from the .env file it reads
        # itself — this process never sees a token).
        if turso_writer is not None:
            turso_db = turso_writer
        else:
            from turso_write import TursoWrite
            turso_db = TursoWrite()
    out = run_shadow(DB(), a.namespace, stock_df, idx, ws_dates, ev_dates, session_day=a.session,
                     sector_map=sector_map, turso_db=turso_db)
    print(json.dumps({'session_day': a.session, 'namespace': a.namespace, 'mode': MODE,
                      'turso_mirrored': turso_db is not None, 'results': out}, indent=2, default=str))
    return out


if __name__ == '__main__':
    main()
