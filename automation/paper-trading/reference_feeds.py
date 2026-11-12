"""Read-only public reference data that feeds the freeze guards (plan A6/A7).

* NSE corporate actions -> paper_corporate_actions (split, bonus, cash dividend, manual-review kinds).
* NSE ASM list           -> paper_price_bands.surveillance = 'ASM'.
* NSE GSM list           -> paper_price_bands.surveillance = 'GSM' (when NSE serves it).
* Fyers symbol master    -> paper_price_bands.series = 'BE' / 'BZ' when an ISIN no longer trades as EQ.

Every source is fail-soft: a failed fetch is reported (never invented) and the engine keeps running.
No classification threshold is invented: dividends described as special/extraordinary are routed to
manual review because the freeze plan never defines "extraordinary".
"""
import csv, io, re, time
from datetime import date, datetime, timedelta
from fractions import Fraction
import requests

SOURCE_CA = 'NSE_CORPORATE_ACTIONS_API'
SOURCE_ASM = 'NSE_ASM_REPORT'
SOURCE_GSM = 'NSE_GSM_REPORT'
SOURCE_MASTER = 'FYERS_SYMBOL_MASTER'
MANUAL_WORDS = (('RIGHTS', r'\brights?\b'), ('DEMERGER', r'\bde-?merger\b'), ('MERGER', r'\bmerger\b|\bamalgamation\b|scheme of arrangement'))
_RS = r'(?:rs\.?|re\.?|inr|₹)\s*([0-9]+(?:\.[0-9]+)?)'


def nse_day(text):
    try:
        return datetime.strptime(str(text).strip(), '%d-%b-%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def classify(subject):
    """Return [(kind, ratio_num, ratio_den, cash_paise)] for one NSE subject line. [] means not a position event."""
    s = str(subject or '').lower()
    out = []
    for kind, pattern in MANUAL_WORDS:
        if re.search(pattern, s):
            out.append((kind, None, None, None))
    if out:
        return out[:1]
    if 'split' in s or 'sub-division' in s or 'sub division' in s:
        m = re.search(r'from\s*' + _RS + r'.*?to\s*' + _RS, s)
        if m and Fraction(m.group(2)) > 0:
            r = Fraction(m.group(1)) / Fraction(m.group(2))
            return [('SPLIT', r.numerator, r.denominator, None)] if r > 1 else [('SPLIT', None, None, None)]
        return [('SPLIT', None, None, None)]
    if 'bonus' in s:
        m = re.search(r'bonus\D*?(\d+)\s*:\s*(\d+)', s)
        if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
            # a:b bonus = a new shares for every b held, so r = (a+b)/b shares after per share before.
            r = Fraction(int(m.group(1)) + int(m.group(2)), int(m.group(2)))
            return [('BONUS', r.numerator, r.denominator, None)]
        return [('BONUS', None, None, None)]
    if 'dividend' in s:
        if 'special' in s or 'extraordinary' in s:
            return [('EXTRAORDINARY_DIVIDEND_REVIEW', None, None, None)]
        amounts = [Fraction(a) for a in re.findall(r'dividend[^/]*?' + _RS + r'\s*per\s*share', s)]
        if not amounts:
            return [('DIVIDEND_UNPARSED_REVIEW', None, None, None)]
        return [('CASH_DIVIDEND', None, None, int(round(sum(amounts) * 100)))]
    return []


def corporate_action_rows(rows, symbols, isins, start, end, now_iso):
    """Map NSE rows to paper_corporate_actions inserts for universe symbols with ex_date in [start, end]."""
    by_nse = {s.rsplit('-', 1)[0]: s for s in symbols}
    merged, skipped = {}, {'not_in_universe': 0, 'not_equity_series': 0, 'isin_differs_symbol_matched': 0, 'outside_window': 0, 'not_a_position_event': 0}
    for r in rows or []:
        sym = by_nse.get(str(r.get('symbol') or '').strip().upper())
        if not sym:
            skipped['not_in_universe'] += 1; continue
        if r.get('series') not in (None, '', '-', 'EQ', 'BE', 'BZ'):
            skipped['not_equity_series'] += 1; continue
        if isins.get(sym) and r.get('isin') and isins[sym] != r['isin']:
            # NSE corporate-action rows can carry a pre-face-value-change ISIN (e.g. CONCOR INE111A01017 vs current
            # INE111A01025). The NSE symbol is the match key; the difference is counted, not used to drop the event.
            skipped['isin_differs_symbol_matched'] += 1
        day = nse_day(r.get('exDate'))
        if not day or not start <= day <= end:
            skipped['outside_window'] += 1; continue
        events = classify(r.get('subject'))
        if not events:
            skipped['not_a_position_event'] += 1; continue
        for kind, num, den, cash in events:
            key = (sym, day, kind)
            if key in merged and kind == 'CASH_DIVIDEND':
                merged[key][5] += cash
            else:
                merged[key] = [sym, day, kind, num, den, cash, SOURCE_CA + ':' + str(r.get('subject'))[:160], now_iso]
    return [tuple(v) for v in merged.values()], skipped


def surveillance_rows(report, symbols, isins, label):
    by_nse = {s.rsplit('-', 1)[0]: s for s in symbols}
    found = {}
    if isinstance(report, dict):
        groups = [g.get('data', []) for g in report.values() if isinstance(g, dict)]
    elif isinstance(report, list):
        groups = [report]
    else:
        groups = []
    for group in groups:
        for r in group or []:
            sym = by_nse.get(str(r.get('symbol') or '').strip().upper())
            if sym:
                found[sym] = label
    return found


def series_from_master(text, symbols, isins):
    """ISIN -> series present in the Fyers NSE_CM master. A universe ISIN with BE/BZ and no EQ row is a transition."""
    series = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 10 or not row[9].startswith('NSE:'):
            continue
        isin, ticker = row[5].strip(), row[9]
        suffix = ticker.rsplit('-', 1)[-1] if '-' in ticker else ''
        series.setdefault(isin, set()).add(suffix)
    out = {}
    for sym in symbols:
        have = series.get(isins.get(sym) or '', set())
        if 'EQ' not in have and have & {'BE', 'BZ'}:
            out[sym] = 'BZ' if 'BZ' in have else 'BE'
    return out


def _get(url, timeout, agent, as_json=True):
    r = requests.get(url, timeout=timeout, headers={'User-Agent': agent, 'Accept': 'application/json,text/csv,*/*'})
    if r.status_code != 200:
        raise RuntimeError('http_' + str(r.status_code))
    return r.json() if as_json else r.text


class NseFetcher:
    """Browser-like, session-based reader for NSE's public pages.

    NSE's CDN refuses non-browser user agents outright and refuses whole sessions at random (HTTP 403 for every
    request in the session, roughly 4 in 10 fresh sessions observed 11 Nov 2026). A refused session is discarded
    and a fresh one tried after a bounded backoff; a session that passes the warm-up is reused for every NSE
    source. HTML error pages are never parsed as data. Non-NSE URLs get the same bounded retries.
    """
    RETRY_STATUS = {401, 403, 429, 500, 502, 503, 504}

    def __init__(self, cfg, session_factory=requests.Session, sleep=time.sleep):
        self.cfg, self.session_factory, self.sleep = cfg, session_factory, sleep
        self.session = None; self.attempts = 0; self.sessions = 0

    def _headers(self, url):
        h = dict(self.cfg['nse_headers']); h['User-Agent'] = self.cfg['user_agent']
        ref = next((r for prefix, r in self.cfg['nse_referers'].items() if prefix in url), self.cfg['nse_default_referer'])
        if ref: h['Referer'] = ref
        return h

    def _new_session(self):
        if self.session is not None:
            try: self.session.close()
            except Exception: pass
        self.session = self.session_factory(); self.sessions += 1
        if self.cfg.get('nse_warmup_url'):
            r = self.session.get(self.cfg['nse_warmup_url'], headers=self._headers(self.cfg['nse_warmup_url']), timeout=self.cfg['timeout_seconds'])
            if r.status_code != 200:
                raise RuntimeError('http_' + str(r.status_code))

    def __call__(self, url, as_json=True):
        last = 'request_failed'
        for attempt in range(self.cfg['max_attempts']):
            if attempt:
                self.sleep(min(self.cfg['backoff_seconds'] * 2 ** (attempt - 1), self.cfg['backoff_max_seconds']))
            self.attempts += 1
            try:
                if self.cfg['nse_session_host'] in url:
                    if self.session is None:
                        self._new_session()
                    r = self.session.get(url, headers=self._headers(url), timeout=self.cfg['timeout_seconds'])
                else:
                    r = requests.get(url, headers=self._headers(url), timeout=self.cfg['timeout_seconds'])
            except RuntimeError as ex:
                last = str(ex); self.session = None; continue
            except requests.RequestException as ex:
                last = 'transport_' + type(ex).__name__; self.session = None; continue
            if r.status_code != 200:
                last = 'http_' + str(r.status_code)
                if r.status_code in self.RETRY_STATUS:
                    self.session = None; continue
                raise RuntimeError(last)
            kind = r.headers.get('content-type', '').lower()
            if 'text/html' in kind:
                last = 'html_instead_of_data'; self.session = None; continue
            if not as_json:
                return r.text
            try:
                return r.json()
            except ValueError:
                last = 'invalid_json'; self.session = None; continue
        raise RuntimeError(last)


def asm_from_archive(texts):
    """NSE archive ASM CSVs (fixed-width padded columns, dashed separator row) -> [{'symbol','isin','stage'}]."""
    out = []
    for text in texts:
        reader = csv.reader(io.StringIO(text))
        header = None
        for row in reader:
            cells = [c.strip() for c in row]
            if not any(cells) or set(''.join(cells)) <= {'-'}:
                continue
            if header is None:
                if cells and cells[0].upper() == 'SYMBOL':
                    header = [c.upper() for c in cells]
                continue
            rec = dict(zip(header, cells))
            if rec.get('SYMBOL'):
                out.append({'symbol': rec['SYMBOL'], 'isin': rec.get('ISIN'), 'stage': next((v for k, v in rec.items() if k.startswith('ASM STAG')), None)})
        if header is None:
            raise RuntimeError('asm_archive_unexpected_format')
    return out


def gsm_from_archive(texts):
    """NSE archive GSM CSV (BOM; quoted header cells may contain newlines) -> [{'symbol','isin','stage'}]."""
    out = []
    for text in texts:
        if text.startswith('\ufeff'):
            text = text.lstrip('\ufeff')
        reader = csv.reader(io.StringIO(text))
        header = None
        for row in reader:
            cells = [c.strip().strip('|').strip() for c in row]
            if not any(cells) or set(''.join(cells)) <= {'-'}:
                continue
            names = [c.upper() for c in cells]
            if header is None:
                if 'SYMBOL' in names:
                    header = names
                continue
            rec = dict(zip(header, cells))
            if rec.get('SYMBOL'):
                out.append({'symbol': rec['SYMBOL'], 'isin': rec.get('ISIN'), 'stage': next((v for k, v in rec.items() if k.startswith('GSM STAG')), None)})
        if header is None:
            raise RuntimeError('gsm_archive_unexpected_format')
    return out


def _payload_rows(report):
    if isinstance(report, dict):
        return sum(len(g.get('data', []) or []) for g in report.values() if isinstance(g, dict))
    return len(report) if isinstance(report, list) else 0


def _ca_url(cfg, day, end):
    template = cfg.get('nse_corporate_actions_range_url')
    if not template:
        return cfg['nse_corporate_actions_url']
    fmt = lambda d: date.fromisoformat(d).strftime('%d-%m-%Y')
    return template.format(from_date=fmt(day), to_date=fmt(end))


def impact(e, db, day, ca_rows, flags):
    """What today's guards would touch: open positions and pending signals in blocked symbols, and ex-date actions on held symbols."""
    held = db.query('SELECT strategy,symbol,qty FROM paper_positions WHERE namespace=? AND qty>0', (e.NS,))
    pending = db.query("SELECT strategy,symbol,side FROM paper_signals WHERE namespace=? AND status='pending'", (e.NS,))
    held_syms = {p['symbol'] for p in held}
    return {
        'entry_blocked_symbols_held': sorted(held_syms & set(flags)),
        'pending_signals_in_blocked_symbols': sorted({(p['strategy'], p['symbol'], p['side']) for p in pending if p['symbol'] in flags}),
        'corporate_actions_on_held_symbols': sorted({(r[0], r[1], r[2]) for r in ca_rows if r[0] in held_syms}),
        'corporate_actions_ex_trade_day_on_held_symbols': sorted({(r[0], r[2]) for r in ca_rows if r[0] in held_syms and r[1] == str(day)}),
        'note': 'Surveillance and BE/BZ series block BUY entries only; exits are never blocked by these guards. Corporate actions on held symbols are applied by the monitor on their ex-date.',
    }


def refresh(e, db, trade_day, fetch=None, dry_run=False):
    """Fetch every source, write what parsed, and report errors per source. Paper data only."""
    cfg = e.CFG['reference_feeds']
    fetcher = None
    if fetch is None:
        fetcher = fetch = NseFetcher(cfg)
    symbols = e.universe(); isins = e.ISINS; now_iso = e.now().isoformat(); day = str(trade_day)
    end = (date.fromisoformat(day) + timedelta(days=cfg['corporate_action_lookahead_days'])).isoformat()
    report = {'trade_day': day, 'dry_run': bool(dry_run), 'sources': {}}
    write = (lambda statements: None) if dry_run else db.batch
    rows = []
    try:
        payload = fetch(_ca_url(cfg, day, end))
        if not isinstance(payload, list):
            raise RuntimeError('corporate_actions_unexpected_payload')
        rows, skipped = corporate_action_rows(payload, symbols, isins, day, end, now_iso)
        write([('INSERT INTO paper_corporate_actions(symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ex_date,kind) DO UPDATE SET ratio_num=excluded.ratio_num,ratio_den=excluded.ratio_den,cash_paise=excluded.cash_paise,source=excluded.source,recorded_at=excluded.recorded_at', r) for r in rows])
        report['sources']['corporate_actions'] = {'status': 'ok', 'rows_received': len(payload), 'rows_written': 0 if dry_run else len(rows), 'rows_matched': len(rows), 'window': [day, end], 'skipped': skipped, 'kinds': sorted({r[2] for r in rows})}
    except Exception as ex:
        rows = []
        report['sources']['corporate_actions'] = {'status': 'unavailable', 'error': e.au.safe_error(ex)}
    flags = {}
    for name, url, label in (('asm', cfg['nse_asm_url'], 'ASM'), ('gsm', cfg['nse_gsm_url'], 'GSM')):
        try:
            via = 'api'
            try:
                payload = fetch(url)
                if not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and not any(isinstance(g, dict) and 'data' in g for g in payload.values())):
                    raise RuntimeError(name + '_unexpected_payload')
            except Exception as api_error:
                archives = cfg.get('nse_' + name + '_archive_urls') or []
                if not archives:
                    raise
                # Every listed archive file must load; a partial list would silently under-block.
                parser = gsm_from_archive if name == 'gsm' else asm_from_archive
                payload = parser([fetch(u, as_json=False) for u in archives]); via = 'archive_csv'
                report.setdefault('fallbacks', {})[name] = {'api_error': e.au.safe_error(api_error), 'archive_files': len(archives)}
            found = surveillance_rows(payload, symbols, isins, label)
            for sym, lab in found.items():
                flags.setdefault(sym, {})['surveillance'] = 'GSM' if 'GSM' in (lab, flags.get(sym, {}).get('surveillance')) else lab
            report['sources'][name] = {'status': 'ok', 'via': via, 'source': {'api': 'NSE_' + label + '_REPORT', 'archive_csv': 'NSE_' + label + '_ARCHIVE'}[via], 'rows_received': _payload_rows(payload), 'universe_symbols_flagged': len(found), 'flagged_symbols': sorted(found)}
        except Exception as ex:
            report['sources'][name] = {'status': 'unavailable', 'error': e.au.safe_error(ex)}
    try:
        moved = series_from_master(fetch(cfg['fyers_symbol_master_url'], as_json=False), symbols, isins)
        for sym, series in moved.items():
            flags.setdefault(sym, {})['series'] = series
        report['sources']['series'] = {'status': 'ok', 'universe_symbols_not_eq': len(moved)}
    except Exception as ex:
        report['sources']['series'] = {'status': 'unavailable', 'error': e.au.safe_error(ex)}
    statements = []
    for sym, f in sorted(flags.items()):
        source = '+'.join(k for k, ok in (('NSE_ASM_GSM', 'surveillance' in f), (SOURCE_MASTER, 'series' in f)) if ok)
        statements.append(('INSERT INTO paper_price_bands(symbol,trade_date,upper_price,lower_price,series,surveillance,fo_eligible,source) VALUES(?,?,NULL,NULL,?,?,0,?) ON CONFLICT(symbol,trade_date) DO UPDATE SET series=COALESCE(excluded.series,paper_price_bands.series),surveillance=COALESCE(excluded.surveillance,paper_price_bands.surveillance),source=excluded.source', (sym, day, f.get('series'), f.get('surveillance'), source)))
    write(statements)
    report['guard_rows_written'] = 0 if dry_run else len(statements)
    report['entry_blocked_symbols'] = sorted(flags)
    report['impact'] = impact(e, db, day, rows, flags)
    if fetcher is not None:
        report['fetch'] = {'requests': fetcher.attempts, 'sessions': fetcher.sessions}
    e.log('reference_feeds_dry_run' if dry_run else 'reference_feeds', **report)
    return report
