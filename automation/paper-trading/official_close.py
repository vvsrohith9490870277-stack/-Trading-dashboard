"""Official NSE CM close overlay for EOD valuation. Never invents prices.

When live marks are stale at EOD, positions are valued from the official NSE
close (UDiFF CM bhavcopy ClsPric) with the source labelled. The bhavcopy is
downloaded with curl --http2 and a browser User-Agent (urllib HTTP/1.1 is often
HTTP 403 from this host). TradDt inside the zip must match the requested
session. If the file is missing, unreadable, dated wrongly, or any open
position symbol is absent, valuation stays unavailable. Live
paper_positions.mark_price is never mutated.
"""
from __future__ import annotations
import csv, io, json, os, shutil, subprocess, time, zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError

def settings(e):
    cfg = dict((getattr(e, 'CFG', None) or {}).get('official_close') or {})
    feeds = (getattr(e, 'CFG', None) or {}).get('reference_feeds') or {}
    cfg.setdefault('enabled', True)
    cfg.setdefault('source_label', 'NSE_BHAVCOPY_UDIFF')
    cfg.setdefault('udiff_url_template', 'https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip')
    cfg.setdefault('instrument_type', 'STK')
    cfg.setdefault('series_keep_in_priority', ['EQ', 'BE', 'BZ'])
    cfg.setdefault('close_field', 'ClsPric')
    cfg.setdefault('timeout_seconds', 30)
    cfg.setdefault('max_attempts', 3)
    cfg.setdefault('backoff_seconds', 2)
    cfg.setdefault('cache_dir', 'out/official-close')
    cfg.setdefault('user_agent', feeds.get('user_agent') or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    cfg.setdefault('referer', (feeds.get('nse_referers') or {}).get('nsearchives.nseindia.com') or feeds.get('nse_default_referer') or 'https://www.nseindia.com/')
    return cfg

def _cache_dir(e, cfg):
    p = Path(cfg['cache_dir'])
    return p if p.is_absolute() else Path(e.ROOT) / p

def _reason(ex):
    s = str(ex)
    if s.startswith('udiff_trade_date_mismatch'):
        return 'udiff_trade_date_mismatch'
    if s and len(s) < 90 and all(c.isalnum() or c in '_:-' for c in s):
        return s
    return type(ex).__name__

def position_symbol(ticker, series):
    ticker = str(ticker or '').strip().upper()
    series = str(series or '').strip().upper()
    if not ticker or not series:
        return None
    return f'{ticker}-{series}'

def parse_udiff(raw_zip_bytes, trade_date, instrument_type='STK', series_keep=None, close_field='ClsPric'):
    """Parse one UDiFF CM zip. Raises on a missing CSV or a TradDt mismatch."""
    trade_date = str(trade_date)
    keep = list(series_keep or ['EQ', 'BE', 'BZ'])
    with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        if not names:
            raise ValueError('udiff_zip_has_no_csv')
        rows = list(csv.DictReader(io.TextIOWrapper(z.open(names[0]), encoding='utf8')))
    best = {}
    for r in rows:
        if str(r.get('FinInstrmTp', '')).strip() != instrument_type:
            continue
        ser = str(r.get('SctySrs', '')).strip().upper()
        if ser not in keep:
            continue
        trad = str(r.get('TradDt', '')).strip()
        if trad != trade_date:
            raise ValueError(f'udiff_trade_date_mismatch:{trad}!={trade_date}')
        sym = position_symbol(r.get('TckrSymb'), ser)
        if not sym:
            continue
        raw_px = str(r.get(close_field, '')).strip()
        try:
            px = Decimal(raw_px)
        except (InvalidOperation, ValueError):
            continue
        if px <= 0:
            continue
        if sym not in best or keep.index(ser) < keep.index(best[sym][1]):
            best[sym] = (px, ser)
    return {sym: px for sym, (px, _ser) in best.items()}, len(rows)

def _curl_get(url, dest, timeout, user_agent, referer, cookie=None):
    curl = shutil.which('curl')
    if not curl:
        raise RuntimeError('curl_not_found')
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.partial')
    cmd = [curl, '-sS', '-L', '--http2', '-A', user_agent, '-e', referer,
           '--max-time', str(int(timeout)), '-o', str(tmp), '-w', '%{http_code}', url]
    if cookie:
        cmd[1:1] = ['-c', str(cookie), '-b', str(cookie)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    code = (r.stdout or '').strip()
    try:
        status = int(code[-3:]) if code else 0
    except ValueError:
        status = 0
    if r.returncode != 0 and status == 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f'curl_failed:{r.returncode}')
    if status >= 400 or status == 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f'official_close_http_{status}')
    os.replace(tmp, dest)
    return dest

def _warmup(cfg, cookie, timeout):
    """Touch nseindia.com so nsearchives can see a browser cookie jar. Homepage 403 is ok."""
    home = cfg.get('warmup_url') or 'https://www.nseindia.com/'
    tmp = Path(str(cookie) + '.warmup')
    try:
        _curl_get(home, tmp, timeout, cfg['user_agent'], cfg['referer'], cookie)
    except Exception:
        pass
    finally:
        tmp.unlink(missing_ok=True)

def fetch_zip(e, session, cfg=None):
    """Download (or reuse a session-named cache of) the UDiFF zip. Never invents a file."""
    cfg = cfg or settings(e)
    session = str(session)
    date.fromisoformat(session)
    yyyymmdd = session.replace('-', '')
    cache = _cache_dir(e, cfg) / f'BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip'
    if cache.is_file() and cache.stat().st_size > 0:
        return cache.read_bytes()
    url = cfg['url'] if 'url' in cfg else cfg['udiff_url_template'].format(yyyymmdd=yyyymmdd)
    last = None
    attempts = int(cfg.get('max_attempts') or 3)
    gap = float(cfg.get('backoff_seconds') or 2)
    cookie = _cache_dir(e, cfg) / 'nse-cookies.txt'
    _warmup(cfg, cookie, min(20, int(cfg.get('timeout_seconds') or 30)))
    for i in range(attempts):
        try:
            _curl_get(url, cache, cfg.get('timeout_seconds') or 30, cfg['user_agent'], cfg['referer'], cookie)
            data = cache.read_bytes()
            if not data:
                cache.unlink(missing_ok=True)
                raise RuntimeError('official_close_empty_file')
            return data
        except Exception as ex:
            last = ex
            if cache.exists() and not cache.stat().st_size:
                cache.unlink()
            code = str(ex)
            if 'official_close_http_404' in code:
                break
            if i + 1 < attempts:
                time.sleep(min(15, gap * (2 ** i)))
    raise last or RuntimeError('official_close_unavailable')

def closes_for(e, session, symbols, fetch=None):
    """Return official closes for symbols, or status=unavailable. Never invents prices."""
    session = str(session)
    symbols = [str(s) for s in dict.fromkeys(symbols)]
    src = settings(e)
    label = src['source_label']
    if not src.get('enabled', True):
        return {'status': 'unavailable', 'reason': 'official_close_disabled', 'closes': {}, 'source': None}
    if not symbols:
        return {'status': 'ok', 'reason': 'no_open_positions', 'closes': {}, 'source': label}
    try:
        raw = (fetch or fetch_zip)(e, session, src)
    except Exception as ex:
        return {'status': 'unavailable', 'reason': _reason(ex), 'closes': {}, 'source': None}
    try:
        closes, raw_n = parse_udiff(raw, session, src['instrument_type'], src['series_keep_in_priority'], src['close_field'])
    except Exception as ex:
        return {'status': 'unavailable', 'reason': _reason(ex), 'closes': {}, 'source': None}
    missing = [s for s in symbols if s not in closes]
    if missing:
        return {'status': 'unavailable', 'reason': 'official_close_incomplete', 'closes': {s: closes[s] for s in symbols if s in closes}, 'missing': missing, 'source': label, 'raw_rows': raw_n}
    wanted = {s: closes[s] for s in symbols}
    return {'status': 'ok', 'reason': 'official_close_available', 'closes': wanted, 'source': label, 'raw_rows': raw_n}
