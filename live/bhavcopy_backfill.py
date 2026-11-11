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
import argparse, csv, io, json, os, shutil, subprocess, sys, time, urllib.error, urllib.request, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIB)); sys.path.insert(0, str(HERE))
import adaptive_guard as ag, adaptive_policy as apol, pit_archive as pit   # noqa: E402
import adaptive_daily as ad                                                # noqa: E402

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

    for d in days:
        if stop.exists() and not a.offline_dir:
            out.append({'date': d, 'status': 'stopped_by_stop_file'}); break
        f = raw_dir / f'udiff-{d}.csv.zip'
        if not f.exists():
            if a.offline_dir:
                out.append({'date': d, 'status': 'missing_offline'}); continue
            url = BF['udiff_url_template'].format(yyyymmdd=d.replace('-', ''))
            try:
                fetch_udiff(url, f, BF['request_timeout_seconds'], retries=retries, gap=gap)
                fails = 0
            except urllib.error.HTTPError as e:
                status = 'no_session' if e.code == 404 else 'fetch_failed'
                rec = {'date': d, 'status': status, 'error': f'HTTPError:{e.code}'}
                out.append(rec)
                if e.code == 404:
                    time.sleep(gap); continue
                fails += 1
                time.sleep(gap)
                if fails >= BF['max_consecutive_failures']:
                    out.append({'status': 'aborted_consecutive_failures'}); break
                continue
            except Exception as e:
                fails += 1
                out.append({'date': d, 'status': 'fetch_failed', 'error': type(e).__name__ + ':' + str(getattr(e, 'code', '') or e)[:180]})
                time.sleep(gap)
                if fails >= BF['max_consecutive_failures']:
                    out.append({'status': 'aborted_consecutive_failures'}); break
                continue
            time.sleep(gap)
        try:
            rows, raw_n = parse_udiff(f.read_bytes(), d)
        except ValueError as e:
            if str(e).startswith('udiff_trade_date_mismatch'):
                out.append({'date': d, 'status': 'no_session', 'error': str(e), 'note': 'file date did not match requested session (holiday/weekend placeholder)'})
                continue
            out.append({'date': d, 'status': 'parse_failed', 'error': str(e)})
            continue
        rec = {'date': d, 'status': 'parsed', 'rows': len(rows), 'raw_rows': raw_n, 'path': str(f)}
        local = load_local_ohlcv(d)
        if local:
            rec['vs_local_ohlcv'] = compare_to_ohlcv(rows, d, local)
        if not a.no_ingest and db is not None:
            res = pit.ingest_bhavcopy_series(db, rows, trade_date=d, source=BF['source_label'])
            rec['status'] = 'ingested'
            rec['ingested'] = res['ingested']
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
