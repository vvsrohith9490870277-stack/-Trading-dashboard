"""Official NSE CM Price Band complete list (sec_list_DDMMYYYY.csv).

Adaptive never calls Fyers. Live paper_price_bands in Turso currently stores only ASM/GSM
surveillance flags — no upper_price/lower_price — so the shadow engine cannot take circuit
status from that table. This module:

- parses the official NSE band-percent file
- ingests it into a point-in-time local archive (one official file per session)
- builds the simulator's bands_by_date_symbol map from that archive + previous close + the
  day's OHLC

F&O names typically have no static band (dynamic 10% flex) and stay UNKNOWN. Missing
previous close also stays UNKNOWN. Unknown/dynamic bands are never invented as a lock
or as a fake NONE-that-we-checked; the simulator only applies UPPER/LOWER locks when
the official static band plus OHLC actually infer them.
"""
import csv, hashlib, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
import pit_archive as pit  # noqa: E402

CFG = json.loads((HERE / 'adaptive_live_config.json').read_text())
CIR = CFG.get('circuit_bands') or {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS nse_price_band_archive(
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  series TEXT NOT NULL,
  name TEXT,
  band_raw TEXT,
  band_pct REAL,
  static_band INTEGER NOT NULL,
  remarks TEXT,
  source TEXT NOT NULL,
  data_hash TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(trade_date, symbol)
)
"""

# Lock semantics used by the shadow simulator (and tests). Unknown never invents a lock.
LOCKABLE = frozenset(('UPPER', 'LOWER', 'BOTH', 'NONE'))


def init(db):
    db.batch([(SCHEMA, ())])


def parse_sec_list(text_or_path):
    """Return rows {symbol, series, name, band_pct, remarks, static_band}.

    Band values like '2', '5', '10', '20' are percents. '-', 'NA', 'No Band', '' mean no static band.
    """
    if isinstance(text_or_path, (str, Path)) and Path(str(text_or_path)).exists() and not str(text_or_path).lstrip().startswith('Symbol'):
        raw = Path(text_or_path).read_text(encoding='utf8', errors='replace')
    else:
        raw = str(text_or_path)
    rows = list(csv.DictReader(raw.splitlines()))
    out = []
    for r in rows:
        # column names vary slightly: Symbol / SYMBOL, Band / Price Band, Series
        sym = (r.get('Symbol') or r.get('SYMBOL') or r.get('TckrSymb') or '').strip().upper()
        if not sym:
            continue
        series = (r.get('Series') or r.get('SERIES') or r.get('SctySrs') or '').strip().upper()
        name = (r.get('Security Name') or r.get('SecurityName') or r.get('NAME') or '').strip()
        band_raw = (r.get('Band') or r.get('Price Band') or r.get('PRICE_BAND') or r.get('Applicable Price Band') or '').strip()
        remarks = (r.get('Remarks') or r.get('REMARKS') or '').strip()
        static, pct = False, None
        try:
            pct = float(band_raw)
            if pct > 0:
                static = True
        except (TypeError, ValueError):
            static = False
        out.append({
            'symbol': sym,
            'series': series or 'EQ',
            'name': name,
            'band_raw': band_raw,
            'band_pct': pct,
            'static_band': static,
            'remarks': remarks,
        })
    return out


def infer_circuit(band_pct, prev_close, high=None, low=None, close=None, tol=0.002):
    """Infer UPPER/LOWER/NONE from a static percent band and the day's range.

    Returns None if there is no static band or no previous close (cannot invent a level).
    """
    if not band_pct or not prev_close:
        return None
    upper = round(float(prev_close) * (1 + float(band_pct) / 100.0), 2)
    lower = round(float(prev_close) * (1 - float(band_pct) / 100.0), 2)
    state = 'NONE'
    def hit(px, level):
        return px is not None and level > 0 and abs(float(px) - float(level)) / float(level) <= tol
    if high is not None and (float(high) >= upper or hit(high, upper) or hit(close, upper)):
        state = 'UPPER'
    if low is not None and (float(low) <= lower or hit(low, lower) or hit(close, lower)):
        state = 'LOWER' if state != 'UPPER' else 'BOTH'
    return {'upper': upper, 'lower': lower, 'circuit': state, 'band_pct': float(band_pct)}


def document():
    return {
        'source': CIR.get('url_template_ddmmyyyy'),
        'status': CIR.get('status'),
        'note': CIR.get('note'),
        'fyers': 'not used (adaptive isolation; live Fyers quotes have upper_ckt/lower_ckt but are not snapshotted into paper_price_bands with prices)',
        'turso_paper_price_bands': 'ASM/GSM flags only as of 11 Nov 2026 (source=NSE_ASM_GSM, upper_price/lower_price null)',
        'pit_rule': 'only the official sec_list for trade_date T is used for session T, and only if that file was ingested with trade_date <= as_of; a later session file is never applied backward; missing or non-static bands stay UNKNOWN and never invent a lock or a level',
    }


def market_key(symbol, series):
    """daily_ohlcv-style key (RELIANCE-EQ, BAJAJ-AUTO-EQ, HFCL-BE)."""
    return pit.market_symbol(symbol, series)


def date_from_sec_list_name(path):
    """Accept sec_list-YYYY-MM-DD.csv or official sec_list_DDMMYYYY.csv."""
    name = Path(path).name
    stem = name
    for suffix in ('.csv', '.CSV'):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    if stem.startswith('sec_list-') and len(stem) >= 19:
        maybe = stem[len('sec_list-'):][:10]
        if len(maybe) == 10 and maybe[4] == '-' and maybe[7] == '-':
            return maybe
    if stem.startswith('sec_list_') and len(stem) >= 17:
        ddmmyyyy = stem[len('sec_list_'):][:8]
        if ddmmyyyy.isdigit() and len(ddmmyyyy) == 8:
            return f'{ddmmyyyy[4:8]}-{ddmmyyyy[2:4]}-{ddmmyyyy[0:2]}'
    return None


def url_for_session(trade_date):
    y, m, d = trade_date.split('-')
    tmpl = CIR.get('url_template_ddmmyyyy') or 'https://nsearchives.nseindia.com/content/equities/sec_list_{ddmmyyyy}.csv'
    return tmpl.format(ddmmyyyy=f'{d}{m}{y}')


def unknown_band(series=None, reason='unknown'):
    return {
        'series': series,
        'circuit': 'UNKNOWN',
        'known': False,
        'reason': reason,
        'upper': None,
        'lower': None,
        'band_pct': None,
    }


def simulator_lock(circuit):
    """How the shadow simulator should treat this circuit. Unknown never invents a lock."""
    c = str(circuit or 'UNKNOWN').upper()
    return {
        'block_buy': c in ('UPPER', 'BOTH'),
        'lock_sell': c in ('LOWER', 'BOTH'),
        'known': c in LOCKABLE,
    }


def ingest_sec_list(db, rows, trade_date, source=None, recorded_at=None):
    """Store one session's official file. Point-in-time: rows are keyed by that trade_date only."""
    init(db)
    source = source or CIR.get('source_label') or 'NSE_SEC_LIST_PRICE_BAND'
    from datetime import datetime
    from zoneinfo import ZoneInfo
    stamp = recorded_at or datetime.now(ZoneInfo('Asia/Kolkata')).isoformat()
    payload = json.dumps([
        {'symbol': r['symbol'], 'series': r.get('series') or 'EQ', 'band_pct': r.get('band_pct'), 'static_band': bool(r.get('static_band'))}
        for r in rows
    ], sort_keys=True, default=str)
    data_hash = hashlib.sha256(payload.encode()).hexdigest()
    stm = []
    for r in rows:
        series = (r.get('series') or 'EQ').upper()
        key = market_key(r['symbol'], series)
        stm.append((
            'INSERT OR REPLACE INTO nse_price_band_archive('
            'trade_date,symbol,series,name,band_raw,band_pct,static_band,remarks,source,data_hash,recorded_at'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (trade_date, key, series, r.get('name'), r.get('band_raw'), r.get('band_pct'),
             1 if r.get('static_band') else 0, r.get('remarks'), source, data_hash, stamp),
        ))
    if stm:
        db.batch(stm)
    return {
        'trade_date': trade_date,
        'ingested': len(stm),
        'static_band': sum(1 for r in rows if r.get('static_band')),
        'unknown_band': sum(1 for r in rows if not r.get('static_band')),
        'source': source,
        'data_hash': data_hash,
    }


def ingest_file(db, path, trade_date=None, source=None, recorded_at=None):
    path = Path(path)
    trade_date = trade_date or date_from_sec_list_name(path)
    if not trade_date:
        raise ValueError(f'sec_list_trade_date_unknown:{path.name}')
    return ingest_sec_list(db, parse_sec_list(path), trade_date, source=source, recorded_at=recorded_at)


def ingest_raw_dir(db, raw_dir, as_of=None, source=None):
    """Ingest every recognizable sec_list in raw_dir whose session date is <= as_of (PIT)."""
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        return {'ingested_files': 0, 'skipped_future': 0, 'detail': []}
    init(db)
    detail, skipped = [], 0
    for path in sorted(raw_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != '.csv':
            continue
        day = date_from_sec_list_name(path)
        if not day:
            continue
        if as_of and day > as_of:
            skipped += 1
            continue
        rec = ingest_file(db, path, trade_date=day, source=source)
        rec['path'] = str(path)
        detail.append(rec)
    return {'ingested_files': len(detail), 'skipped_future': skipped, 'detail': detail}


def get_bands_for_session(db, trade_date, as_of_date=None):
    """Return official rows for this session only. A later file is never applied backward."""
    if as_of_date is not None and trade_date > as_of_date:
        return []
    init(db)
    return db.query('SELECT * FROM nse_price_band_archive WHERE trade_date=?', (trade_date,))


def _prev_closes(stock_df, sessions):
    """Map (trade_date, symbol) -> previous session close from stock_df (PIT: only earlier dates)."""
    if stock_df is None or getattr(stock_df, 'empty', True):
        return {}
    by_sym = {}
    for rec in stock_df.to_dict('records'):
        sym = str(rec['symbol'])
        by_sym.setdefault(sym, []).append((str(rec['trade_date']), rec))
    # Also allow looking up previous session even when `sessions` is a subset.
    prev = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: x[0])
        last_close = None
        last_date = None
        for d, rec in rows:
            if last_close is not None and last_date is not None and last_date < d:
                prev[(d, sym)] = float(last_close)
            try:
                last_close = float(rec['close'])
                last_date = d
            except (TypeError, ValueError):
                pass
    return prev


def build_bands_by_date_symbol(db, stock_df, sessions, as_of):
    """Point-in-time circuit map for the simulator: {(date, symbol): band_dict}.

    - Official sec_list for session D is used only when D <= as_of and that file was ingested.
    - Static band + previous close + day's OHLC -> UPPER / LOWER / BOTH / NONE via infer_circuit.
    - Missing file, missing prev close, or non-static (dynamic) band -> UNKNOWN (no lock invented).
    """
    sessions = [str(d) for d in sessions if (as_of is None or str(d) <= as_of)]
    prev = _prev_closes(stock_df, sessions)
    bars = {}
    if stock_df is not None and not getattr(stock_df, 'empty', True):
        for rec in stock_df.to_dict('records'):
            d = str(rec['trade_date'])
            if as_of is not None and d > as_of:
                continue
            bars[(d, str(rec['symbol']))] = rec

    archive = {}
    for d in sessions:
        for row in get_bands_for_session(db, d, as_of_date=as_of):
            archive[(d, row['symbol'])] = row

    out = {}
    # Cover every bar in the window, plus any archive-only names (no bar => still record UNKNOWN/known band).
    keys = set(bars) | set(archive)
    for key in keys:
        d, sym = key
        if as_of is not None and d > as_of:
            continue
        if sessions and d not in sessions and (d, sym) not in bars:
            continue
        row = archive.get(key)
        bar = bars.get(key) or {}
        if row is None:
            out[key] = unknown_band(reason='no_official_sec_list')
            continue
        series = row.get('series')
        if not row.get('static_band') or row.get('band_pct') in (None, ''):
            out[key] = unknown_band(series=series, reason='no_static_band')
            continue
        prev_close = prev.get(key)
        inferred = infer_circuit(
            row['band_pct'], prev_close,
            high=bar.get('high'), low=bar.get('low'), close=bar.get('close'),
        )
        if inferred is None:
            out[key] = unknown_band(series=series, reason='no_previous_close')
            continue
        out[key] = {
            'series': series,
            'circuit': inferred['circuit'],
            'known': True,
            'reason': 'official_sec_list',
            'upper': inferred['upper'],
            'lower': inferred['lower'],
            'band_pct': inferred['band_pct'],
            'source': row.get('source'),
        }
    return out


def summarize_bands(bands):
    from collections import Counter
    c = Counter(str(v.get('circuit') or 'UNKNOWN') for v in bands.values())
    reasons = Counter(str(v.get('reason') or '') for v in bands.values())
    return {
        'symbols': len(bands),
        'by_circuit': dict(c),
        'by_reason': dict(reasons),
        'known': sum(1 for v in bands.values() if v.get('known')),
        'unknown': sum(1 for v in bands.values() if not v.get('known')),
    }




def process_session_file(trade_date, raw_dir, db=None, fetch=False, timeout=30, retries=3, gap=3.0, source=None):
    """Locate, optionally fetch, parse, and ingest one session's official sec_list. Never invents a file."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    y, m, d = trade_date.split('-')
    candidates = [
        raw_dir / f'sec_list-{trade_date}.csv',
        raw_dir / f'sec_list_{d}{m}{y}.csv',
    ]
    path = next((c for c in candidates if c.exists() and c.stat().st_size > 0), None)
    if path is None and fetch:
        try:
            path = maybe_fetch_sec_list(trade_date, candidates[0], timeout=timeout, retries=retries, gap=gap)
        except Exception as e:
            code = getattr(e, 'code', None)
            status = 'no_session' if code == 404 else 'fetch_failed'
            return {'date': trade_date, 'status': status, 'error': type(e).__name__ + ':' + str(code or e)[:180]}
    if path is None:
        return {'date': trade_date, 'status': 'missing'}
    rows = parse_sec_list(path)
    out = {
        'date': trade_date,
        'status': 'parsed',
        'rows': len(rows),
        'static_band': sum(1 for r in rows if r.get('static_band')),
        'unknown_band': sum(1 for r in rows if not r.get('static_band')),
        'path': str(path),
    }
    if db is not None:
        res = ingest_file(db, path, trade_date=trade_date, source=source)
        out['status'] = 'ingested'
        out['ingested'] = res['ingested']
        out['data_hash'] = res['data_hash']
    return out


def maybe_fetch_sec_list(trade_date, dest, timeout=30, retries=3, gap=3.0):
    """Download the official file with the same curl HTTP/2 helper as UDiFF. Never calls Fyers."""
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    import bhavcopy_backfill as bb
    bb.fetch_udiff(url_for_session(trade_date), dest, timeout, retries=retries, gap=gap)
    return dest


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print(json.dumps(document(), indent=2))
        raise SystemExit(0)
    rows = parse_sec_list(path)
    static = sum(1 for r in rows if r['static_band'])
    print(json.dumps({
        'file': path,
        'trade_date': date_from_sec_list_name(path),
        'rows': len(rows),
        'with_static_band': static,
        'without_static_band': len(rows) - static,
        'document': document(),
    }, indent=2))
