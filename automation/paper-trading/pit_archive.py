"""Point-in-time (PIT) multi-year daily_prefilter_universe and Bhavcopy SERIES archive.

Enforces strict point-in-time boundaries (`trade_date <= as_of_date`) so warm-start,
candidate screening, and gate evaluations never read future prefilter memberships or
future Bhavcopy series transitions.
"""
import csv, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import adaptive_policy as apol
import prefilter as pf

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_prefilter_universe(namespace TEXT NOT NULL,trade_date TEXT NOT NULL,symbol TEXT NOT NULL,policy_version TEXT NOT NULL,status TEXT NOT NULL,eligible INTEGER NOT NULL,pit_series TEXT,metrics_json TEXT NOT NULL,data_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,trade_date,symbol,policy_version));
CREATE INDEX IF NOT EXISTS daily_prefilter_universe_date_idx ON daily_prefilter_universe(namespace,trade_date,eligible,symbol);
CREATE TABLE IF NOT EXISTS bhavcopy_series_archive(trade_date TEXT NOT NULL,symbol TEXT NOT NULL,series TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume INTEGER,turnover REAL,isin TEXT,source TEXT NOT NULL,data_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(trade_date,symbol));
CREATE INDEX IF NOT EXISTS bhavcopy_series_date_idx ON bhavcopy_series_archive(trade_date,series,symbol);
"""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _stable_json(val):
    return json.dumps(val, sort_keys=True, separators=(',', ':'), default=str)


def _sha256(val):
    raw = val if isinstance(val, str) else _stable_json(val)
    return hashlib.sha256(raw.encode('utf8')).hexdigest()


def init(db):
    db.batch([(s.strip(), ()) for s in SCHEMA.split(';') if s.strip()])


def assert_pit(trade_date, as_of_date=None):
    """Raise ValueError if trade_date is after as_of_date."""
    if as_of_date is not None and str(trade_date) > str(as_of_date):
        raise ValueError(f'pit_lookahead_violation:{trade_date}>{as_of_date}')


def canonical_symbol(raw_symbol):
    """Normalize symbol to <BASE>-EQ canonical key used across paper tables."""
    if raw_symbol is None:
        return None
    sym = str(raw_symbol).strip().upper()
    if not sym:
        return None
    base = sym.split('-')[0]
    return f'{base}-EQ'


def _float_or_none(val):
    if val is None or str(val).strip() == '':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int_or_none(val):
    if val is None or str(val).strip() == '':
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def normalize_bhavcopy_row(row, default_trade_date=None):
    """Extract canonical symbol, PIT series, OHLCV, and ISIN from classic or UDiFF Bhavcopy row."""
    raw_sym = row.get('symbol') or row.get('SYMBOL') or row.get('TckrSymb')
    sym = canonical_symbol(raw_sym)
    if not sym:
        return None
    raw_series = row.get('series') or row.get('SERIES') or row.get('SctySrs')
    if not raw_series and raw_sym and '-' in str(raw_sym):
        raw_series = str(raw_sym).strip().upper().split('-', 1)[1]
    series = str(raw_series or 'EQ').strip().upper()
    tdate = str(
        row.get('trade_date')
        or row.get('TIMESTAMP')
        or row.get('TradDt')
        or default_trade_date
        or ''
    ).strip()
    if not tdate:
        raise ValueError('bhavcopy_trade_date_required')
    open_p = _float_or_none(row.get('open') if 'open' in row else (row.get('OPEN') if 'OPEN' in row else row.get('OpnPric')))
    high_p = _float_or_none(row.get('high') if 'high' in row else (row.get('HIGH') if 'HIGH' in row else row.get('HghPric')))
    low_p = _float_or_none(row.get('low') if 'low' in row else (row.get('LOW') if 'LOW' in row else row.get('LwPric')))
    close_p = _float_or_none(row.get('close') if 'close' in row else (row.get('CLOSE') if 'CLOSE' in row else row.get('ClsPric')))
    vol = _int_or_none(row.get('volume') if 'volume' in row else (row.get('TOTTRDQTY') if 'TOTTRDQTY' in row else row.get('TtlTradgVol')))
    turn = _float_or_none(row.get('turnover') if 'turnover' in row else (row.get('TOTTRDVAL') if 'TOTTRDVAL' in row else row.get('TtlTrfVal')))
    isin = str(row.get('isin') or row.get('ISIN') or row.get('ISINCode') or '').strip() or None
    payload = {
        'trade_date': tdate,
        'symbol': sym,
        'series': series,
        'open': open_p,
        'high': high_p,
        'low': low_p,
        'close': close_p,
        'volume': vol,
        'turnover': turn,
        'isin': isin,
    }
    payload['data_hash'] = _sha256(payload)
    return payload


def ingest_bhavcopy_series(db, rows, trade_date=None, source='NSE_BHAVCOPY', recorded_at=None):
    """Persist daily Bhavcopy SERIES and OHLCV into bhavcopy_series_archive."""
    stamp = recorded_at or _utc_now()
    statements = []
    ingested = 0
    for r in rows:
        norm = normalize_bhavcopy_row(r, default_trade_date=trade_date)
        if norm is None:
            continue
        statements.append((
            'INSERT INTO bhavcopy_series_archive(trade_date,symbol,series,open,high,low,close,volume,turnover,isin,source,data_hash,recorded_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(trade_date,symbol) DO UPDATE SET series=excluded.series,open=excluded.open,high=excluded.high,low=excluded.low,'
            'close=excluded.close,volume=excluded.volume,turnover=excluded.turnover,isin=excluded.isin,source=excluded.source,'
            'data_hash=excluded.data_hash,recorded_at=excluded.recorded_at',
            (
                norm['trade_date'],
                norm['symbol'],
                norm['series'],
                norm['open'],
                norm['high'],
                norm['low'],
                norm['close'],
                norm['volume'],
                norm['turnover'],
                norm['isin'],
                str(source),
                norm['data_hash'],
                stamp,
            ),
        ))
        ingested += 1
        if len(statements) >= apol.POLICY['db_batch_chunk_rows']:
            db.batch(statements)
            statements = []
    if statements:
        db.batch(statements)
    return {'ingested': ingested, 'source': source}


def ingest_bhavcopy_csv(db, csv_path, trade_date=None, source='NSE_BHAVCOPY_CSV', recorded_at=None):
    """Load a daily Bhavcopy CSV file into bhavcopy_series_archive."""
    path = Path(csv_path)
    with path.open(newline='', encoding='utf8') as handle:
        rows = list(csv.DictReader(handle))
    return ingest_bhavcopy_series(db, rows, trade_date=trade_date, source=source, recorded_at=recorded_at)


def get_pit_series(db, symbol, trade_date, as_of_date=None):
    """Return the point-in-time Bhavcopy SERIES for symbol on trade_date (never reading past as_of_date)."""
    assert_pit(trade_date, as_of_date)
    sym = canonical_symbol(symbol)
    tdate = str(trade_date)
    rows = db.query(
        'SELECT series FROM bhavcopy_series_archive WHERE trade_date=? AND symbol=?',
        (tdate, sym),
    )
    if rows and rows[0].get('series'):
        return str(rows[0]['series']).upper()
    # Fallback to most recent Bhavcopy record on or before trade_date if exact date not in archive
    prior = db.query(
        'SELECT series FROM bhavcopy_series_archive WHERE symbol=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1',
        (sym, tdate),
    )
    if prior and prior[0].get('series'):
        return str(prior[0]['series']).upper()
    try:
        bands = db.query(
            'SELECT series FROM paper_price_bands WHERE symbol=? AND trade_date=?',
            (sym, tdate),
        )
        if bands and bands[0].get('series'):
            return str(bands[0]['series']).upper()
    except Exception:
        pass
    return None


def get_pit_series_map(db, trade_date, symbols=None, as_of_date=None):
    """Return {symbol: series} map for trade_date strictly respecting as_of_date."""
    assert_pit(trade_date, as_of_date)
    tdate = str(trade_date)
    if symbols:
        norm_syms = [canonical_symbol(s) for s in symbols]
        out = {}
        for s in norm_syms:
            ser = get_pit_series(db, s, tdate, as_of_date=as_of_date)
            if ser is not None:
                out[s] = ser
        return out
    rows = db.query('SELECT symbol,series FROM bhavcopy_series_archive WHERE trade_date=? ORDER BY symbol', (tdate,))
    return {str(r['symbol']): str(r['series']).upper() for r in rows}


def archive_daily_prefilter(db, namespace, trade_date, stock_df, index_df, symbols=None, policy_version=None, as_of_date=None, recorded_at=None):
    """Compute and store point-in-time daily_prefilter_universe for a single historical trade_date."""
    tdate = str(trade_date)
    assert_pit(tdate, as_of_date)
    ver = policy_version or pf.POLICY['version']
    stamp = recorded_at or _utc_now()

    if isinstance(stock_df, pd.DataFrame) and not stock_df.empty:
        pit_stock = stock_df[stock_df['trade_date'].astype(str) <= tdate].copy()
    else:
        pit_stock = pd.DataFrame()

    if isinstance(index_df, pd.DataFrame) and not index_df.empty:
        pit_index = index_df[index_df['trade_date'].astype(str) <= tdate].copy()
    else:
        pit_index = pd.DataFrame()

    if symbols is None:
        if not pit_stock.empty and 'symbol' in pit_stock.columns:
            all_syms = sorted(pit_stock['symbol'].astype(str).unique().tolist())
        else:
            all_syms = []
    else:
        all_syms = sorted({str(s) for s in symbols})

    grouped = {sym: g for sym, g in pit_stock.groupby('symbol')} if not pit_stock.empty else {}
    statements = []
    counts = {'PASS': 0, 'FAIL': 0, 'UNAVAILABLE': 0}

    for sym in all_syms:
        sym_df = grouped.get(sym, pd.DataFrame())
        res = pf.evaluate(sym_df, pit_index, tdate)
        pit_ser = get_pit_series(db, sym, tdate, as_of_date=as_of_date or tdate)
        # No silent EQ default: without a PIT Bhavcopy SERIES row the series is UNKNOWN (fails require_eq_series).
        res['pit_series'] = pit_ser or 'UNKNOWN'
        status = res['status']
        counts[status] = counts.get(status, 0) + 1
        eligible = 1 if status == 'PASS' else 0
        metrics_str = _stable_json(res)
        dhash = _sha256({'namespace': namespace, 'trade_date': tdate, 'symbol': sym, 'policy_version': ver, 'metrics': res})
        statements.append((
            'INSERT INTO daily_prefilter_universe(namespace,trade_date,symbol,policy_version,status,eligible,pit_series,metrics_json,data_hash,recorded_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(namespace,trade_date,symbol,policy_version) DO UPDATE SET status=excluded.status,eligible=excluded.eligible,'
            'pit_series=excluded.pit_series,metrics_json=excluded.metrics_json,data_hash=excluded.data_hash,recorded_at=excluded.recorded_at',
            (namespace, tdate, sym, ver, status, eligible, res['pit_series'], metrics_str, dhash, stamp),
        ))
        if len(statements) >= apol.POLICY['db_batch_chunk_rows']:
            db.batch(statements)
            statements = []
    if statements:
        db.batch(statements)
    return {
        'namespace': namespace,
        'trade_date': tdate,
        'policy_version': ver,
        'symbols_evaluated': len(all_syms),
        'counts': counts,
    }


def build_multiyear_prefilter_archive(db, namespace, trade_dates, stock_df, index_df, symbols=None, policy_version=None, as_of_date=None, recorded_at=None):
    """Populate daily_prefilter_universe chronologically across a multi-year list of trade_dates."""
    sorted_dates = sorted({str(d) for d in trade_dates})
    for d in sorted_dates:
        assert_pit(d, as_of_date)
    summaries = []
    for d in sorted_dates:
        summaries.append(
            archive_daily_prefilter(
                db,
                namespace=namespace,
                trade_date=d,
                stock_df=stock_df,
                index_df=index_df,
                symbols=symbols,
                policy_version=policy_version,
                as_of_date=d,
                recorded_at=recorded_at,
            )
        )
    return {
        'namespace': namespace,
        'dates_archived': len(summaries),
        'first_date': sorted_dates[0] if sorted_dates else None,
        'last_date': sorted_dates[-1] if sorted_dates else None,
        'summaries': summaries,
    }


def get_pit_prefilter_universe(db, namespace, trade_date, as_of_date=None, policy_version=None, require_eq_series=False):
    """Query daily_prefilter_universe for trade_date with strict point-in-time enforcement."""
    tdate = str(trade_date)
    assert_pit(tdate, as_of_date)
    ver = policy_version or pf.POLICY['version']
    rows = db.query(
        'SELECT symbol,status,eligible,pit_series,metrics_json,data_hash FROM daily_prefilter_universe '
        'WHERE namespace=? AND trade_date=? AND policy_version=? ORDER BY symbol',
        (namespace, tdate, ver),
    )
    eligible_symbols = []
    by_symbol = {}
    for r in rows:
        sym = str(r['symbol'])
        ser = get_pit_series(db, sym, tdate, as_of_date=as_of_date or tdate) or str(r.get('pit_series') or 'UNKNOWN').upper()
        is_pass = int(r['eligible']) == 1 and str(r['status']) == 'PASS'
        if require_eq_series and ser != 'EQ':
            is_pass = False
        entry = {
            'symbol': sym,
            'status': str(r['status']),
            'eligible': bool(is_pass),
            'pit_series': ser,
            'metrics': json.loads(r['metrics_json']),
            'data_hash': str(r['data_hash']),
        }
        by_symbol[sym] = entry
        if is_pass:
            eligible_symbols.append(sym)
    snapshot_hash = _sha256([{'symbol': s, 'hash': by_symbol[s]['data_hash'], 'series': by_symbol[s]['pit_series']} for s in sorted(by_symbol)])
    return {
        'namespace': namespace,
        'trade_date': tdate,
        'policy_version': ver,
        'eligible_symbols': eligible_symbols,
        'by_symbol': by_symbol,
        'snapshot_hash': snapshot_hash,
    }
