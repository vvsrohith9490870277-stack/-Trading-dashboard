"""One-shot Fyers quote probe (muse-tonight-20-nov Task 1).

Runs as a short-lived child of the Render supervisor (spawned by
render_app via run_job_streaming), NOT in the supervisor process: the
supervisor never imports numpy/pandas for a one-shot diagnostic (that
would add ~70 MB to the supervisor for its whole life and push
supervisor+worker+scan toward the 480 MB budget).

What it does, exactly once per run:
1. token-check (no network): read the token store's metadata only
   (present, has_refresh, access_len, issued_at). No token means no
   Fyers call at all — the result is 'token_missing' with 0 requests.
2. one real quote batch for NSE:RELIANCE-EQ and NSE:TCS-EQ via
   engine.MarketData().quotes() — exactly the code path, token source
   and network route the worker uses. No fills, no position changes,
   no exit logic (quotes() only reads market data).
3. print the result JSON to stdout (one line) and exit 0. The
   supervisor records it in paper_runtime_kv + the audit event.

The result carries the real error (classified error_code + redacted
error_detail), the HTTP status, the Fyers code, the LTP count, the
duration and the request count — never a bare 'RuntimeError' and never
a secret (error text goes through audit.redact_error_detail).

Request bound: quotes() is called exactly once. The engine chunks 2
symbols into 1 request per attempt and retries at most max_attempts=3
(config) on 401/403, so at most 3 quote requests per boot; the actual
count is measured from the budget snapshot delta and recorded.
"""
from __future__ import annotations
import json
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
PROBE_SYMBOLS = ('RELIANCE', 'TCS')
_QUOTES_URL_PART = '/data/quotes'
_FYERS_CODE_RE = re.compile(r'market_data_error_(-?\d+)')


def _build_api():
    """The worker's MarketData (lazy import: this module is the child,
    so importing the engine here is isolated and exits with it)."""
    import engine as e
    return e.MarketData()


def _read_token_meta():
    """Token metadata only (never the token value), like render_ctl
    token-check without --validate. Fails closed: any store problem
    is reported as 'not present' with the store error type."""
    try:
        import token_store as ts
        tok = ts.open_token_store().get_token()
    except Exception as ex:
        return {'present': False, 'has_refresh': False, 'access_len': None,
                'issued_at': None, 'store_error': type(ex).__name__}
    if not tok:
        return {'present': False, 'has_refresh': False, 'access_len': None,
                'issued_at': None, 'store_error': None}
    return {'present': True,
            'has_refresh': bool(tok.get('refresh_token')),
            'access_len': len(tok.get('access_token') or ''),
            'issued_at': tok.get('issued_at'),
            'store_error': None}


def _recording_get(real_get, recorder):
    """Wrap requests.get to record the last HTTP status seen for the
    quotes route (and only that route). Delegates 1:1 — the request
    itself is unchanged, so the code path stays the worker's. A
    transport failure leaves the recorder untouched (no response)."""
    def wrapped(url, **kw):
        resp = real_get(url, **kw)
        try:
            if _QUOTES_URL_PART in str(url):
                recorder['status'] = int(resp.status_code)
        except Exception:
            pass
        return resp
    return wrapped


def _budget_route_count(api, route):
    """The api's budget count for one route (0 when unavailable)."""
    try:
        snap = api.budget_snapshot()
    except Exception:
        return 0
    try:
        by_route = (snap or {}).get('by_route') or {}
        return int(by_route.get(route) or 0)
    except Exception:
        return 0


def _flush_budget(api):
    """Persist the probe's requests to the shared budget counter
    (best-effort: the child exits without a SIGTERM flush)."""
    try:
        budget = getattr(api, 'budget', None)
        if budget is not None:
            budget.flush()
    except Exception:
        pass


def _extract_fyers_code(message):
    m = _FYERS_CODE_RE.search(str(message or ''))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def run_probe(api=None, token_meta=None, status_recorder=None):
    """Run the probe once and return the result dict.

    `api` defaults to the worker's MarketData; `token_meta` defaults
    to the real token-store metadata; `status_recorder` is a dict the
    caller (main) updates with the HTTP status via _recording_get.
    Tests inject fakes for all three, so no test ever touches Fyers.
    """
    import audit as al
    started = time.monotonic()
    meta = dict(token_meta) if token_meta is not None \
        else _read_token_meta()
    result = {
        'status': 'failed', 'symbols': list(PROBE_SYMBOLS),
        'ltp_count': 0, 'http_status': None, 'fyers_code': None,
        'error_code': None, 'error_detail': None, 'duration_ms': 0,
        'requests': 0, 'depth_requests': 0,
        'token_present': bool(meta.get('present')),
        'token_has_refresh': bool(meta.get('has_refresh')),
        'token_access_len': meta.get('access_len'),
        'token_issued_at': meta.get('issued_at'),
        'at': datetime.now(IST).isoformat(),
    }
    if not meta.get('present'):
        detail = 'no FYERS_ACCESS_TOKEN in env or token store'
        if meta.get('store_error'):
            detail = 'token store unreadable: %s' % meta['store_error']
        result.update({'error_code': 'token_missing',
                       'error_detail': al.redact_error_detail(detail),
                       'duration_ms': 0})
        return result
    if api is None:
        api = _build_api()
    before_quotes = _budget_route_count(api, 'quotes')
    before_depth = _budget_route_count(api, 'depth')
    try:
        out = api.quotes(list(PROBE_SYMBOLS))
    except Exception as ex:
        _flush_budget(api)
        result.update({
            'error_code': al.classified_error(ex),
            'error_detail': al.error_detail(ex),
            'fyers_code': _extract_fyers_code(ex),
            'http_status': (status_recorder or {}).get('status'),
            'duration_ms': round((time.monotonic() - started) * 1000),
            'requests': max(0, _budget_route_count(api, 'quotes')
                            - before_quotes),
            'depth_requests': max(0, _budget_route_count(api, 'depth')
                                  - before_depth),
            'at': datetime.now(IST).isoformat(),
        })
        return result
    _flush_budget(api)
    priced = 0
    for q in (out or {}).values():
        price = q.get('price') if isinstance(q, dict) else None
        if isinstance(price, (int, float)) and price > 0:
            priced += 1
    result.update({
        'status': 'ok', 'ltp_count': priced,
        'http_status': (status_recorder or {}).get('status'),
        'duration_ms': round((time.monotonic() - started) * 1000),
        'requests': max(0, _budget_route_count(api, 'quotes')
                        - before_quotes),
        'depth_requests': max(0, _budget_route_count(api, 'depth')
                              - before_depth),
        'at': datetime.now(IST).isoformat(),
    })
    return result


def main(argv=None):
    """Child entry point: run the probe, print the result JSON (one
    line) to stdout, exit 0. The exit code is always 0 — the result
    carries the status; a crash would print a traceback to stderr and
    the supervisor records 'probe_child_failed' instead."""
    import engine as e
    recorder = {'status': None}
    orig_get = e.requests.get
    e.requests.get = _recording_get(orig_get, recorder)
    try:
        result = run_probe(status_recorder=recorder)
    finally:
        e.requests.get = orig_get
    print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
