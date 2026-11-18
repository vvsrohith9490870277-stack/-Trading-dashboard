"""Local record of NSE no_session outcomes (R9/R10). Paper-only, local only.

A date lands here when NSE answered HTTP 404 for every archive form — a
genuine no-session day (holiday the config did not list). The record lives
under state/ and is never written to Turso. It is used in two places:

- bhavcopy_backfill --retry-missing skips recorded dates instead of
  re-hitting NSE for them on every run;
- adaptive_daily's freshness gate counts a date as no_session only if it is
  a config holiday / excluded session (already removed from the expected
  calendar) or it is in this record — a weekday with no bar and no archive
  row and no 404 record is a missed session, not a holiday.
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
HERE = Path(__file__).resolve().parent
NO_SESSION_RECORD = HERE.parent / 'state' / 'bhavcopy_no_session.json'


def load_no_session_dates(record_path=None):
    """Dates recorded as no_session. Missing/corrupt record => no dates."""
    p = Path(record_path) if record_path is not None else NO_SESSION_RECORD
    try:
        data = json.loads(p.read_text())
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    return {str(k) for k, v in data.items() if isinstance(v, dict) and v.get('status') == 'no_session'}


def record_no_session_dates(dates, record_path=None):
    """Persist no_session outcomes (one entry per date) in the local record."""
    p = Path(record_path) if record_path is not None else NO_SESSION_RECORD
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    stamp = datetime.now(IST).isoformat()
    for d in dates:
        data[str(d)[:10]] = {'status': 'no_session', 'recorded_at': stamp}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, sort_keys=True))
    return sorted(data)
