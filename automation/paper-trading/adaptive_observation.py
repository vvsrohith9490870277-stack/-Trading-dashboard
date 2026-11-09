"""Observation-session truth (E8, G-03) and Track A parity streak (E4, D3).

A session counts toward observation only when ALL of these hold for that IST date:
- the shadow runner logged `shadow_session_completed` for the strategy in the hash-chained decision log;
- an official NSE Bhavcopy for that date is in bhavcopy_series_archive (source starts with NSE_BHAVCOPY);
- the Track A EOD for that date is reconciled (adaptive_eod_markers, written from the read-only EOD artifact);
- the date is a regular weekday session: not in config.json holidays and not an indicator-excluded special session
  (Muhurat Sun 8 Nov 2026 never counts).
The count is always derived from these records; no caller can pass a session count in.
"""
import calendar
from datetime import date, datetime, timezone

import adaptive_logging as alog
import adaptive_policy as apol

AP = apol.POLICY
CFG = apol.read_json('config.json')
HOLIDAYS = frozenset(str(d) for d in CFG.get('holidays') or [])
EXCLUDED_SPECIAL = frozenset(str(d) for d in (CFG.get('indicator_excluded_sessions') or {}))
OFFICIAL_BHAVCOPY_PREFIX = 'NSE_BHAVCOPY'
SESSION_DONE_CAUSE = 'shadow_session_completed'



def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def init(db):
    """Tables adaptive_eod_markers / adaptive_parity live in adaptive_logging.SCHEMA."""
    alog.init(db)


def _iso(d):
    s = str(d)[:10]
    date.fromisoformat(s)
    return s


def is_regular_session_date(d):
    s = _iso(d)
    return date.fromisoformat(s).weekday() < calendar.SATURDAY and s not in HOLIDAYS and s not in EXCLUDED_SPECIAL


def record_eod_marker(db, namespace, trade_date, reconciled, source_artifact, source_hash, recorded_at=None):
    """Idempotent: a duplicate marker for the same date is ignored; a reconciled marker is never downgraded."""
    d = _iso(trade_date)
    if not source_hash:
        raise ValueError('eod_marker_requires_source_hash')
    prior = db.query('SELECT reconciled,source_hash FROM adaptive_eod_markers WHERE namespace=? AND trade_date=?', (namespace, d))
    if prior:
        if int(prior[0]['reconciled']) == 1:
            return {'trade_date': d, 'reconciled': True, 'duplicate': True}
    db.batch([(
        'INSERT INTO adaptive_eod_markers(namespace,trade_date,reconciled,source_artifact,source_hash,recorded_at) VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(namespace,trade_date) DO UPDATE SET reconciled=excluded.reconciled,source_artifact=excluded.source_artifact,'
        'source_hash=excluded.source_hash,recorded_at=excluded.recorded_at',
        (namespace, d, 1 if reconciled else 0, str(source_artifact), str(source_hash), recorded_at or _utc_now()),
    )])
    return {'trade_date': d, 'reconciled': bool(reconciled), 'duplicate': bool(prior)}


def log_shadow_session_completed(db, namespace, strategy, trade_date, details=None):
    d = _iso(trade_date)
    return alog.append_log(
        db, namespace, correlation_id=f'shadow_s{int(strategy)}_{d}', track='SYSTEM', strategy=int(strategy),
        category='SYSTEM', decision_type='SYSTEM_OP', named_cause=SESSION_DONE_CAUSE,
        rule_fired='shadow_runner_session_after_reconciled_eod',
        conditions={'trade_date': d}, execution_details=details or {}, source_at=d,
    )


def record_parity(db, namespace, strategy, trade_date, fills_match, skips_match, qty_match, equity_diff_paise, details=None, recorded_at=None):
    """D3: exact fill/skip/qty match and |equity diff| <= parity_equity_tolerance_paise."""
    diff = int(equity_diff_paise)
    ok = bool(fills_match and skips_match and qty_match and abs(diff) <= AP['parity_equity_tolerance_paise'])
    db.batch([(
        'INSERT OR REPLACE INTO adaptive_parity(namespace,strategy,trade_date,fills_match,skips_match,qty_match,equity_diff_paise,parity_ok,details_json,recorded_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        (namespace, int(strategy), _iso(trade_date), int(bool(fills_match)), int(bool(skips_match)), int(bool(qty_match)), diff,
         int(ok), alog.stable_json(details or {}), recorded_at or _utc_now()),
    )])
    return ok


def count_observation_sessions(db, namespace, strategy):
    """Derive the observation session count from logs + official Bhavcopy + reconciled EOD (never from an argument)."""
    s = int(strategy)
    rows = db.query(
        'SELECT DISTINCT substr(source_at,1,10) AS d FROM adaptive_decision_logs WHERE namespace=? AND strategy=? AND named_cause=? ORDER BY d',
        (namespace, s, SESSION_DONE_CAUSE),
    )
    counted, excluded = [], []
    for r in rows:
        d = r['d']
        if not is_regular_session_date(d):
            excluded.append({'trade_date': d, 'reason': 'not_a_regular_weekday_session'})
            continue
        bhav = db.query(
            "SELECT COUNT(*) AS n FROM bhavcopy_series_archive WHERE trade_date=? AND source LIKE ?",
            (d, OFFICIAL_BHAVCOPY_PREFIX + '%'),
        )[0]['n']
        if not int(bhav):
            excluded.append({'trade_date': d, 'reason': 'no_official_nse_bhavcopy'})
            continue
        eod = db.query('SELECT reconciled FROM adaptive_eod_markers WHERE namespace=? AND trade_date=?', (namespace, d))
        if not eod or int(eod[0]['reconciled']) != 1:
            excluded.append({'trade_date': d, 'reason': 'track_a_eod_not_reconciled'})
            continue
        counted.append(d)
    return {'strategy': s, 'sessions': len(counted), 'session_dates': counted, 'excluded': excluded}


def parity_streak(db, namespace, strategy, session_dates):
    """Consecutive most-recent counted sessions with parity_ok; a missing parity row breaks the streak."""
    by_date = {r['trade_date']: int(r['parity_ok']) for r in db.query(
        'SELECT trade_date,parity_ok FROM adaptive_parity WHERE namespace=? AND strategy=?', (namespace, int(strategy)))}
    streak = 0
    for d in reversed(list(session_dates)):
        if by_date.get(d) != 1:
            break
        streak += 1
    failures = sorted(d for d, ok in by_date.items() if ok != 1)
    return {'streak': streak, 'parity_failures': failures}


def observation_status(db, namespace, strategy):
    """E8: complete only when N sessions AND the parity streak are both met."""
    cnt = count_observation_sessions(db, namespace, strategy)
    par = parity_streak(db, namespace, strategy, cnt['session_dates'])
    need_n = AP['observation_min_sessions']
    need_streak = AP['parity_min_consecutive_sessions']
    return {
        **cnt,
        'parity_streak': par['streak'],
        'parity_failures': par['parity_failures'],
        'min_sessions': need_n,
        'min_parity_streak': need_streak,
        'completed': cnt['sessions'] >= need_n and par['streak'] >= need_streak,
    }
