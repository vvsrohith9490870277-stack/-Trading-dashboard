"""P1: NSE UDiFF CM Bhavcopy -> local archive (or a scratch dir). Paper-only, local only.

Guards (all fail closed unless --probe/--no-ingest/--offline-dir):
  Track A EOD for backfill.not_before_session reconciled, adaptive_policy backfill window,
  stop file before every request, rate cap backfill_max_requests_per_minute,
  stop after max_consecutive_failures. Only the UDiFF era (>= udiff_first_session) is supported.

Fetcher: curl --http2 with a browser User-Agent and Referer (urllib HTTP/1.1 is often HTTP 403
from this host). 403/429 are retried with backoff; 404 is a no-session day (not a consecutive
failure). The TradDt inside the zip must match the requested session (a holiday URL can return
the previous session's file).

Usage:
  bhavcopy_backfill.py --from YYYY-MM-DD --to YYYY-MM-DD
  bhavcopy_backfill.py --probe --dates 2026-11-06,2026-11-09 --scratch-dir DIR --no-ingest
  bhavcopy_backfill.py --from YYYY-MM-DD --to YYYY-MM-DD --offline-dir DIR --no-ingest
"""
import argparse, csv, io, json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB)); sys.path.insert(0, str(HERE))
import adaptive_guard as ag, adaptive_policy as apol, pit_archive as pit   # noqa: E402
import adaptive_daily as ad                                                # noqa: E402
import circuit_bands as cb                                                 # noqa: E402

IST = ZoneInfo('Asia/Kolkata')
CFG = json.loads((HERE / 'adaptive_live_config.json').read_text()); BF = CFG['backfill']; AP = apol.POLICY
ENGINE_CFG = apol.read_json('config.json')
UA = BF.get('user_agent') or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
REFERER = BF.get('referer') or 'https://www.nseindia.com/'
CURL = shutil.which('curl')


def session_days(frm, to):
    """Weekdays minus config holidays minus indicator-excluded sessions (Muhurat).

    Holidays outside calendar_valid_from..through may be missing from config; those dates
    are still requested and then classified as no_session on HTTP 404 or a TradDt mismatch.
    """
    holidays = set(ENGINE_CFG.get('holidays') or [])
    excluded = set(ad.EXCLUDED)
    d0, d1 = date.fromisoformat(frm), date.fromisoformat(to)
    out, d = [], d0
    while d <= d1:
        iso = d.isoformat()
        if d.weekday() < 5 and iso not in holidays and iso not in excluded:
            out.append(iso)
        d += timedelta(days=1)
    return out


def parse_udiff(raw_zip_bytes, trade_date):
    with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        if not names:
            raise ValueError('udiff_zip_has_no_csv')
        rows = list(csv.DictReader(io.TextIOWrapper(z.open(names[0]), encoding='utf8')))
    keep = BF['series_keep_in_priority']; best = {}
    for r in rows:
        if str(r.get('FinInstrmTp', '')).strip() != BF['instrument_type']:
            continue
        ser = str(r.get('SctySrs', '')).strip().upper()
        if ser not in keep:
            continue
        trad = str(r.get('TradDt', '')).strip()
        if trad != trade_date:
            raise ValueError(f'udiff_trade_date_mismatch:{trad}!={trade_date}')
        sym = pit.canonical_symbol(r.get('TckrSymb'))
        if sym not in best or keep.index(ser) < keep.index(best[sym]['SctySrs'].strip().upper()):
            best[sym] = r
    return list(best.values()), len(rows)


def gate_open(day_list):
    eods = sorted(p.name for p in (HERE.parent / 'state').glob('track_a_eod-*.json'))
    ok = any(n[len('track_a_eod-'):-5] >= BF['not_before_session'] and json.loads((HERE.parent / 'state' / n).read_text()).get('ledger_reconciled') for n in eods)
    if not ok:
        raise RuntimeError('backfill_blocked_until_track_a_eod_reconciled:' + BF['not_before_session'])
    if not ag.can_run_backfill():
        raise RuntimeError('backfill_outside_policy_window')
    if day_list and min(day_list) < BF['udiff_first_session']:
        raise RuntimeError('pre_udiff_era_not_supported')


def _curl_get(url, dest, timeout, extra=None):
    if not CURL:
        raise RuntimeError('curl_not_found')
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.partial')
    cmd = [CURL, '-sS', '-L', '--http2', '-A', UA, '-e', REFERER,
           '--max-time', str(int(timeout)), '-o', str(tmp), '-w', '%{http_code}', url]
    if extra:
        cmd[1:1] = extra  # after curl
    r = subprocess.run(cmd, capture_output=True, text=True)
    code = (r.stdout or '').strip()
    try:
        status = int(code[-3:]) if code else 0
    except ValueError:
        status = 0
    if r.returncode != 0 and status == 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f'curl_failed:{r.returncode}:{(r.stderr or "")[:200]}')
    if status >= 400 or status == 0:
        body_len = tmp.stat().st_size if tmp.exists() else 0
        if tmp.exists():
            tmp.unlink()
        err = urllib.error.HTTPError(url, status or 599, f'http_{status}', hdrs=None, fp=None)
        err.body_len = body_len
        raise err
    tmp.replace(dest)
    return dest


def _urllib_get(url, dest, timeout):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*', 'Referer': REFERER, 'Accept-Language': 'en-US,en;q=0.9'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        dest.write_bytes(r.read())
    return dest


def fetch_udiff(url, dest, timeout, retries=3, gap=3.0):
    """Download one UDiFF zip. Prefer curl --http2 (verified against nsearchives). Retry 403/429."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            if CURL:
                return _curl_get(url, dest, timeout)
            return _urllib_get(url, dest, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            if e.code in (403, 429, 500, 502, 503) and attempt < retries:
                time.sleep(gap * attempt)
                continue
            raise
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(gap * attempt)
                continue
            raise
    raise last


# Official NSE CM archive forms (nsearchives.nseindia.com only). Never invent a session.
# 1) UDiFF zip (from 2024-07-08)
# 2) Legacy equity cm{DD}{MON}{YYYY}bhav.csv.zip
# 3) sec_bhavdata_full_{DDMMYYYY}.csv
NSE_MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
SOURCE_BY_FORM = {
    'udiff': 'NSE_BHAVCOPY_UDIFF',
    'legacy_cm': 'NSE_BHAVCOPY_LEGACY_CM',
    'sec_bhavdata_full': 'NSE_BHAVCOPY_SEC_BHAVDATA_FULL',
}


def official_archive_candidates(trade_date):
    """Return official NSE archive (form, url, filename) tuples for a YYYY-MM-DD session."""
    y, m, d = trade_date.split('-')
    mon = NSE_MONTHS[int(m) - 1]
    yyyymmdd = y + m + d
    ddmmyyyy = d + m + y
    udiff_url = BF['udiff_url_template'].format(yyyymmdd=yyyymmdd)
    return [
        ('udiff', udiff_url, f'udiff-{trade_date}.csv.zip'),
        (
            'legacy_cm',
            f'https://nsearchives.nseindia.com/content/historical/EQUITIES/{y}/{mon}/cm{d}{mon}{y}bhav.csv.zip',
            f'cm{d}{mon}{y}bhav.csv.zip',
        ),
        (
            'sec_bhavdata_full',
            f'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv',
            f'sec_bhavdata_full_{ddmmyyyy}.csv',
        ),
    ]


def _looks_like_html(raw):
    head = raw[:800].lstrip().lower()
    return head.startswith(b'<!') or head.startswith(b'<html') or b'access denied' in head or b'<title>access' in head


class NseBrowserSession:
    """Fresh cookie jar warmed against www.nseindia.com, reused for nsearchives fetches."""

    def __init__(self, timeout=30):
        self.timeout = int(timeout or 30)
        self._dir = Path(tempfile.mkdtemp(prefix='nse-session-'))
        self.jar = self._dir / 'cookies.txt'
        self.warmed = False

    def _browser_headers(self):
        return [
            '-A', UA,
            '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            '-H', 'Accept-Language: en-US,en;q=0.9',
            '-H', 'Accept-Encoding: gzip, deflate, br',
            '--compressed',
            '-b', str(self.jar),
            '-c', str(self.jar),
        ]

    def warmup(self):
        dest = self._dir / 'warmup.html'
        for url, referer in (
            ('https://www.nseindia.com/', None),
            ('https://www.nseindia.com/all-reports', 'https://www.nseindia.com/'),
            ('https://www.nseindia.com/market-data/archives', 'https://www.nseindia.com/'),
        ):
            extra = list(self._browser_headers())
            if referer:
                extra.extend(['-H', f'Referer: {referer}', '-e', referer])
            try:
                _curl_get(url, dest, self.timeout, extra=extra)
            except Exception:
                # Homepage may still have written cookies; continue warming.
                pass
        self.warmed = True
        return self

    def fetch(self, url, dest, timeout=None, referer='https://www.nseindia.com/'):
        if not self.warmed:
            self.warmup()
        extra = list(self._browser_headers())
        extra.extend([
            '-H', 'Accept: text/csv,application/zip,application/octet-stream,*/*;q=0.8',
            '-H', f'Referer: {referer}',
            '-e', referer,
        ])
        return _curl_get(url, dest, timeout or self.timeout, extra=extra)


def fetch_official(url, dest, timeout, retries=3, gap=1.0, session=None):
    last = None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for i in range(max(1, int(retries))):
        try:
            if session is not None:
                return session.fetch(url, dest, timeout=timeout)
            return fetch_udiff(url, dest, timeout, retries=1, gap=gap)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                if dest.exists():
                    dest.unlink()
                raise
            if dest.exists():
                dest.unlink()
            if i + 1 < retries and e.code in (403, 429, 500, 502, 503):
                time.sleep(gap * (i + 2))
                if session is not None and e.code in (403, 429):
                    session.warmed = False
                    session.warmup()
                continue
            raise
        except Exception as e:
            last = e
            if dest.exists():
                dest.unlink()
            if i + 1 < retries:
                time.sleep(gap * (i + 1))
                continue
            raise
    if last:
        raise last


def parse_tabular_bhav(raw, trade_date):
    """Parse UDiFF CSV, legacy cm bhavcopy, or sec_bhavdata_full. TradDt/TIMESTAMP/DATE1 must match."""
    if isinstance(raw, bytes):
        if raw[:2] == b'PK':
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
                if not names:
                    raise ValueError('archive_zip_has_no_csv')
                raw = zf.read(names[0])
        if _looks_like_html(raw):
            raise ValueError('archive_file_is_html_or_block_page')
        text = raw.decode('utf-8-sig')
    else:
        text = raw
    rows_in = list(csv.DictReader(io.StringIO(text)))
    keep = BF['series_keep_in_priority']
    best, raw_n, seen_dates = {}, 0, set()
    for r in rows_in:
        raw_n += 1
        raw_dt = r.get('TradDt') or r.get('TIMESTAMP') or r.get('DATE1') or r.get('DATE') or r.get('trade_date')
        dt = pit.canonical_trade_date(raw_dt)
        if dt:
            seen_dates.add(dt)
        if 'FinInstrmTp' in r and str(r.get('FinInstrmTp') or '').strip() != BF['instrument_type']:
            continue
        ser = (r.get('SctySrs') or r.get('SERIES') or r.get('series') or 'EQ').strip().upper()
        if ser not in keep:
            continue
        if dt and dt != trade_date:
            continue
        rec = pit.normalize_bhavcopy_row(r, default_trade_date=trade_date)
        base = rec['symbol'].rsplit('-', 1)[0]
        prev = best.get(base)
        if prev is None or keep.index(rec['series']) < keep.index(prev['series']):
            best[base] = rec
    if seen_dates and trade_date not in seen_dates:
        raise ValueError(f'trade_date_mismatch:{sorted(seen_dates)}!={trade_date}')
    return list(best.values()), raw_n


def detect_archive_form(path, raw=None):
    path = Path(path)
    raw = raw if raw is not None else path.read_bytes()
    name = path.name.lower()
    if name.startswith('udiff-') or 'bhavcopy_nse_cm' in name:
        return 'udiff'
    if name.startswith('cm') and name.endswith('bhav.csv.zip'):
        return 'legacy_cm'
    if 'sec_bhavdata_full' in name:
        return 'sec_bhavdata_full'
    # Peek headers.
    peek = raw
    if peek[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(peek)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
                peek = zf.read(names[0]) if names else b''
        except zipfile.BadZipFile:
            return 'udiff'
    try:
        header = peek.decode('utf-8-sig', errors='ignore').splitlines()[0].upper()
    except Exception:
        return 'udiff'
    if 'TCKRSYMB' in header or 'TRADDT' in header:
        return 'udiff'
    if 'DATE1' in header or 'OPEN_PRICE' in header or 'TTL_TRD_QNTY' in header:
        return 'sec_bhavdata_full'
    if 'TIMESTAMP' in header or ( 'SYMBOL' in header and 'TOTTRDQTY' in header):
        return 'legacy_cm'
    return 'udiff'


def parse_official_file(path, trade_date):
    path = Path(path)
    raw = path.read_bytes()
    if _looks_like_html(raw):
        raise ValueError('archive_file_is_html_or_block_page')
    form = detect_archive_form(path, raw)
    rows, raw_n = parse_tabular_bhav(raw, trade_date)
    source = SOURCE_BY_FORM[form]
    if form == 'udiff':
        source = BF.get('source_label') or source
    return rows, raw_n, form, source


def local_candidates(raw_dir, trade_date):
    raw_dir = Path(raw_dir)
    found = []
    for form, _url, name in official_archive_candidates(trade_date):
        p = raw_dir / name
        if p.exists() and p.stat().st_size > 0:
            found.append((form, p))
    return found


def obtain_session_file(trade_date, raw_dir, session=None, offline=False, timeout=30, retries=3, gap=1.0):
    """Locate or fetch an official NSE archive for trade_date. Never synthesizes a session."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    # Prefer an existing local file of any official form.
    for form, path in local_candidates(raw_dir, trade_date):
        try:
            rows, raw_n, form2, source = parse_official_file(path, trade_date)
            return {'status': 'ok', 'form': form2, 'source': source, 'path': str(path),
                    'rows': rows, 'rows_kept': len(rows), 'raw_rows': raw_n, 'from_cache': True}
        except ValueError as e:
            if 'mismatch' in str(e):
                return {'status': 'no_session', 'error': str(e),
                        'note': 'file date did not match requested session (holiday/weekend placeholder)',
                        'path': str(path), 'form': form}
            errors.append(f'local:{form}:{e}')
    if offline:
        return {'status': 'missing_offline', 'error': ';'.join(errors) or 'no local official file'}

    last_http = None
    any_403 = False
    any_404 = False
    for form, url, name in official_archive_candidates(trade_date):
        dest = raw_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            # already tried in local_candidates (and failed parse)
            continue
        try:
            fetch_official(url, dest, timeout, retries=retries, gap=gap, session=session)
        except urllib.error.HTTPError as e:
            last_http = e.code
            if e.code == 404:
                any_404 = True
            else:
                any_403 = any_403 or e.code in (403, 429)
            errors.append(f'{form}:HTTPError:{e.code}')
            time.sleep(gap)
            continue
        except Exception as e:
            errors.append(f'{form}:{type(e).__name__}:{str(getattr(e, "code", "") or e)[:160]}')
            time.sleep(gap)
            continue
        try:
            rows, raw_n, form2, source = parse_official_file(dest, trade_date)
        except ValueError as e:
            if dest.exists():
                # Keep a genuine mismatch file for inspection; drop HTML/block pages.
                if 'mismatch' not in str(e):
                    dest.unlink()
            if 'mismatch' in str(e):
                return {'status': 'no_session', 'error': str(e),
                        'note': 'file date did not match requested session (holiday/weekend placeholder)',
                        'path': str(dest), 'form': form, 'attempts': errors + [str(e)]}
            errors.append(f'{form}:parse:{e}')
            time.sleep(gap)
            continue
        time.sleep(gap)
        return {'status': 'ok', 'form': form2, 'source': source, 'path': str(dest),
                'rows': rows, 'rows_kept': len(rows), 'raw_rows': raw_n, 'from_cache': False, 'url': url}
    if any_404 and not any_403 and last_http == 404:
        return {'status': 'no_session', 'error': ';'.join(errors), 'note': 'official archives returned 404'}
    return {'status': 'fetch_failed', 'error': ';'.join(errors) or f'HTTPError:{last_http}'}


def compare_to_ohlcv(rows, trade_date, ohlcv_by_symbol):
    """Compare a few EQ rows against a trade_date -> {symbol: {open,high,low,close,volume}} map."""
    want = ohlcv_by_symbol or {}
    checked, open_match, close_match, missing_in_ohlcv, missing_in_bhav, mismatches = 0, 0, 0, 0, 0, []
    bhav = {}
    for r in rows:
        # Match daily_ohlcv keys: BAJAJ-AUTO-EQ, HFCL-BE (series from the row, hyphenated base kept).
        sym = pit.market_symbol(
            r.get('TckrSymb') or r.get('symbol') or r.get('SYMBOL'),
            r.get('SctySrs') or r.get('series') or r.get('SERIES'),
        )
        if not sym:
            continue
        bhav[sym] = r
    # symbols present in either side (cap the mismatch list)
    keys = sorted(set(bhav) | set(want))
    for sym in keys:
        if sym not in bhav:
            missing_in_bhav += 1
            continue
        if sym not in want:
            missing_in_ohlcv += 1
            continue
        checked += 1
        r, o = bhav[sym], want[sym]
        def f(row, *ks):
            for k in ks:
                if k in row and row[k] not in (None, ''):
                    try:
                        return round(float(row[k]), 2)
                    except (TypeError, ValueError):
                        return None
            return None
        bo, bc = f(r, 'OpnPric', 'open'), f(r, 'ClsPric', 'close')
        oo, oc = f(o, 'open'), f(o, 'close')
        if bo == oo:
            open_match += 1
        if bc == oc:
            close_match += 1
        if bo != oo or bc != oc:
            if len(mismatches) < 12:
                mismatches.append({'symbol': sym, 'bhav_open': bo, 'ohlcv_open': oo, 'bhav_close': bc, 'ohlcv_close': oc})
    return {
        'trade_date': trade_date,
        'bhav_eq_rows': len(bhav),
        'ohlcv_rows': len(want),
        'compared': checked,
        'open_match': open_match,
        'close_match': close_match,
        'missing_in_ohlcv': missing_in_ohlcv,
        'missing_in_bhav': missing_in_bhav,
        'mismatches': mismatches,
    }


def load_local_ohlcv(trade_date):
    import sqlite3
    dbp = HERE.parent / 'state' / 'market.sqlite'
    if not dbp.exists():
        return {}
    c = sqlite3.connect(dbp)
    rows = c.execute('SELECT symbol,open,high,low,close,volume FROM daily_ohlcv WHERE trade_date=?', (trade_date,)).fetchall()
    c.close()
    return {r[0]: {'open': r[1], 'high': r[2], 'low': r[3], 'close': r[4], 'volume': r[5]} for r in rows}


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument('--from', dest='frm', default=None)
    a.add_argument('--to', default=None)
    a.add_argument('--dates', default=None, help='comma-separated YYYY-MM-DD (probe of a few sessions)')
    a.add_argument('--offline-dir', default=None)
    a.add_argument('--scratch-dir', default=None)
    a.add_argument('--no-ingest', action='store_true')
    a.add_argument('--probe', action='store_true', help='scratch-only; skip EOD/window gates; never ingest')
    a = a.parse_args(argv)

    if a.dates:
        days = [d.strip() for d in a.dates.split(',') if d.strip()]
    elif a.frm and a.to:
        days = session_days(a.frm, a.to)
    else:
        raise SystemExit('need --from and --to, or --dates')

    probe = bool(a.probe or a.no_ingest or a.scratch_dir)
    if a.probe:
        a.no_ingest = True
    if not a.offline_dir and not probe:
        gate_open(days)

    raw_dir = Path(a.scratch_dir) if a.scratch_dir else (Path(a.offline_dir) if a.offline_dir else HERE.parent / BF['raw_dir'])
    raw_dir.mkdir(parents=True, exist_ok=True)
    stop = HERE.parent / BF['stop_file']
    gap = 60.0 / max(1, int(AP['backfill_max_requests_per_minute']))
    retries = int(BF.get('fetch_retries') or 3)
    fails = 0
    out = []
    db = None if a.no_ingest else ad.open_adaptive_db()
    session = None if a.offline_dir else NseBrowserSession(timeout=BF['request_timeout_seconds'])
    if session is not None:
        session.warmup()
        time.sleep(gap)

    for d in days:
        if stop.exists() and not a.offline_dir:
            out.append({'date': d, 'status': 'stopped_by_stop_file'}); break
        got = obtain_session_file(
            d, raw_dir, session=session, offline=bool(a.offline_dir),
            timeout=BF['request_timeout_seconds'], retries=retries, gap=gap,
        )
        if got.get('status') not in ('ok',):
            rec = {'date': d, **{k: v for k, v in got.items() if k != 'rows'}}
            out.append(rec)
            if got.get('status') == 'no_session':
                time.sleep(gap); continue
            fails += 1
            time.sleep(gap)
            if fails >= BF['max_consecutive_failures']:
                out.append({'status': 'aborted_consecutive_failures'}); break
            continue
        fails = 0
        rec = {'date': d, 'status': 'parsed', 'rows': got['rows_kept'], 'raw_rows': got['raw_rows'],
               'path': got['path'], 'form': got['form'], 'source': got['source']}
        local = load_local_ohlcv(d)
        if local:
            rec['vs_local_ohlcv'] = compare_to_ohlcv(got['rows'], d, local)
        if not a.no_ingest and db is not None:
            res = pit.ingest_bhavcopy_series(db, got['rows'], trade_date=d, source=got['source'])
            rec['status'] = 'ingested'
            rec['ingested'] = res['ingested']
        # Official price-band file for the same session. Missing/dynamic stays unknown; never invent.
        cir_dir = Path(a.scratch_dir) if a.scratch_dir else (
            Path(a.offline_dir) if a.offline_dir else HERE.parent / (json.loads((HERE / 'adaptive_live_config.json').read_text()).get('circuit_bands') or {}).get('raw_dir', 'state/circuit_bands')
        )
        rec['circuit'] = cb.process_session_file(
            d, cir_dir, db=None if a.no_ingest else db,
            fetch=not bool(a.offline_dir),
            timeout=BF['request_timeout_seconds'], retries=retries, gap=gap,
        )
        out.append(rec)

    ingested = [x for x in out if x.get('status') in ('ingested', 'parsed')]
    result = {
        'days_requested': len(days),
        'ingested_days': len([x for x in out if x.get('status') == 'ingested']),
        'parsed_days': len(ingested),
        'coverage': len(ingested) / len(days) if days else 0,
        'required': AP['archive_min_coverage'],
        'probe': probe,
        'scratch_dir': str(raw_dir),
        'detail': out,
        'failures': [x for x in out if x.get('status') not in ('ingested', 'parsed', 'no_session')],
        'no_session': [x for x in out if x.get('status') == 'no_session'],
    }
    print(json.dumps(result, indent=1, default=str))
    return result


if __name__ == '__main__':
    main()
