"""Read-only Support Agent for paper trading (Production Plan v3).

The Support Agent is strictly read-only on trading state (paper_signals, paper_positions,
paper_fills, paper_fill_costs, paper_decisions, etc.) and only writes to support tables
(or out/support/support.sqlite).
Findings are append-only and SHA-256 hash-chained; status transitions (OPEN, ACKNOWLEDGED,
RESOLVED, WONT_FIX, VERIFIED) insert new rows with incrementing status_seq and actor attribution,
and never update or delete prior rows.
"""
import csv, hashlib, json, math, re, sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
import reconciliation as rec
import capital

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
CAP_POLICY = json.loads((ROOT / 'capital_policy.json').read_text(encoding='utf8'))
POLICY_FILE = ROOT / 'support_policy.json'
POLICY_LOCK_FILE = ROOT / 'support_policy_lock.json'
SUPPORT_POLICY = json.loads(POLICY_FILE.read_text(encoding='utf8'))

IST = timezone(timedelta(minutes=int(SUPPORT_POLICY['utc_offset_minutes'])), name=SUPPORT_POLICY['timezone'])

VALID_STATUSES = ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'WONT_FIX', 'VERIFIED')
VALID_SEVERITIES = ('SEV1', 'SEV2', 'SEV3', 'SEV4', 'INFO', 'WARN', 'ERROR', 'CRITICAL')
INCOMPLETE_LIFECYCLE_MINUTES = SUPPORT_POLICY['thresholds']['incomplete_lifecycle_timeout_minutes']

EXPECTED_SKIP_CAUSES = rec.NAMED_CAUSES | frozenset({
    'not_pending',
    'position_guard',
    'missing_or_failed_current_prefilter_proof',
    'position_identity_guard',
    'same_bar_reentry_guard',
    'not_yet_eligible',
    'expired_entry_signal',
    'nonpositive_price',
    'stale_or_future_quote',
    'market_closed_or_unverified',
    'provider_quote_error',
    'nonpositive_or_missing_price',
    'missing_exchange_timestamp',
    'quote_outside_verified_session',
})

# Note (v3): unclassified_condition is NOT placeholder text; it is classified by error_taxonomy_gap.
PLACEHOLDER_PATTERN = re.compile(r'(\bTODO\b|\bFIXME\b|\bTBD\b|\bPLACEHOLDER_TEXT\b|\[redacted\])', re.IGNORECASE)
ERROR_TAXONOMY_PATTERN = re.compile(r'(\bunclassified_condition\b|\bunclassified_error\b|\bunknown_worker_error\b)', re.IGNORECASE)
GENESIS = '0' * 64


def _stable_json(v):
    if isinstance(v, list):
        return '[' + ','.join(_stable_json(x) for x in v) + ']'
    if isinstance(v, dict):
        return '{' + ','.join(json.dumps(k) + ':' + _stable_json(v[k]) for k in sorted(v.keys())) + '}'
    return json.dumps(None if v is None else v)


def canonical_policy_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def policy_digest(obj):
    return hashlib.sha256(canonical_policy_json(obj).encode('utf-8')).hexdigest()


def assert_support_policy_lock(policy=None, lock=None):
    """Verify support_policy.json matches support_policy_lock.json and satisfies the v3 Null rule."""
    p = SUPPORT_POLICY if policy is None else policy
    lk = json.loads(POLICY_LOCK_FILE.read_text(encoding='utf8')) if lock is None else lock
    expected_hash = lk.get('files', {}).get('support_policy.json')
    if policy_digest(p) != expected_hash:
        raise ValueError('support_policy_lock_mismatch')
    if p.get('version') != lk.get('support_policy_version'):
        raise ValueError('support_policy_version_mismatch')
    # Null rule (v3): No null SEV1/SEV2 threshold before P2.
    thresh = p.get('thresholds', {})
    sevs = p.get('threshold_severities', {})
    for k, sev in sevs.items():
        if sev in ('SEV1', 'SEV2') and thresh.get(k) is None:
            raise ValueError(f'null_sev1_or_sev2_threshold:{k}')
    # Delivery rule (H5): Grok Bot chat only; no email, Slack, or external webhooks.
    deliv = p.get('delivery', {})
    if deliv.get('channel') != 'grok_bot_chat_only' or deliv.get('allow_email') or deliv.get('allow_slack') or deliv.get('allow_external_webhooks'):
        raise ValueError('external_delivery_channel_forbidden_by_H5')
    return p


def missing_policy_values(policy=None):
    """Return undecided (null) SEV3/SEV4 threshold keys to emit policy_value_missing at 08:30."""
    p = SUPPORT_POLICY if policy is None else policy
    thresh = p.get('thresholds', {})
    sevs = p.get('threshold_severities', {})
    missing = []
    for k in sorted(thresh.keys()):
        if thresh[k] is None:
            missing.append({'key': f'thresholds.{k}', 'target_severity': sevs.get(k, 'SEV4')})
    return missing


def derived_counts(root=None):
    """Compute universe size and strategy cells dynamically from repository files."""
    base = Path(root) if root is not None else ROOT
    src = SUPPORT_POLICY['derived_counts_sources']
    with (base / src['universe_csv']).open('r', encoding='utf8', newline='') as f:
        reader = csv.DictReader(f)
        universe_syms = [r['db_symbol'].strip() for r in reader if r.get('db_symbol')]
    exclusions = json.loads((base / src['exclusions_json']).read_text(encoding='utf8'))
    rules = json.loads((base / src['strategy_rules_json']).read_text(encoding='utf8'))
    u_count = len(universe_syms)
    e_count = len(exclusions)
    expected_symbols = u_count + e_count
    strategy_count = len(rules)
    return {
        'universe_symbols': u_count,
        'excluded_symbols': e_count,
        'expected_symbols': expected_symbols,
        'strategy_count': strategy_count,
        'expected_strategy_cells': expected_symbols * strategy_count,
    }


def ist_now():
    """Return current timezone-aware timestamp in Asia/Kolkata."""
    return datetime.now(IST)


def _utc_now():
    return ist_now().isoformat()


def parse_ist(value):
    """Parse an ISO timestamp or datetime into Asia/Kolkata timezone-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)
    s = str(value).strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def ist_session_day(value):
    """Return YYYY-MM-DD session day in Asia/Kolkata."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    s = str(value).strip()
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s
    return parse_ist(s).date().isoformat()


def is_trading_day(day_val, cfg=None):
    """True if day_val is a weekday not in NSE holidays or a special session day."""
    c = CFG if cfg is None else cfg
    d = date.fromisoformat(str(day_val)[:10]) if not isinstance(day_val, date) else (day_val.date() if isinstance(day_val, datetime) else day_val)
    ds = d.isoformat()
    if ds in c.get('special_sessions', {}):
        return True
    if d.weekday() >= 5:
        return False
    if ds in set(c.get('holidays', [])):
        return False
    return True


def next_trading_day(day_val, cfg=None):
    """Return next NSE trading day strictly after day_val."""
    d = date.fromisoformat(str(day_val)[:10]) if not isinstance(day_val, date) else (day_val.date() if isinstance(day_val, datetime) else day_val)
    cur = d + timedelta(days=1)
    while not is_trading_day(cur, cfg=cfg):
        cur += timedelta(days=1)
    return cur


SCHEMA = """CREATE TABLE IF NOT EXISTS support_findings(namespace TEXT NOT NULL,finding_id TEXT NOT NULL,status_seq INTEGER NOT NULL,detector TEXT NOT NULL,correlation_id TEXT NOT NULL,first_event_at TEXT NOT NULL,session_day TEXT NOT NULL,severity TEXT NOT NULL CHECK(severity IN('SEV1','SEV2','SEV3','SEV4','INFO','WARN','ERROR','CRITICAL')),status TEXT NOT NULL CHECK(status IN('OPEN','ACKNOWLEDGED','RESOLVED','WONT_FIX','VERIFIED')),observation_only INTEGER NOT NULL DEFAULT 1,notified INTEGER NOT NULL DEFAULT 0,details_json TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,finding_id,status_seq));
CREATE INDEX IF NOT EXISTS support_findings_session_idx ON support_findings(namespace,session_day,finding_id,status_seq);
CREATE TABLE IF NOT EXISTS support_checkpoints(namespace TEXT PRIMARY KEY,last_recorded_at TEXT NOT NULL,last_event_id TEXT NOT NULL,last_decision_seq INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);"""

LEGACY_SEV_TO_SQL = {
    'SEV1': 'CRITICAL',
    'SEV2': 'ERROR',
    'SEV3': 'WARN',
    'SEV4': 'INFO',
}

EXTENDED_SUPPORT_SCHEMA = """CREATE TABLE IF NOT EXISTS support_episodes(namespace TEXT NOT NULL,episode_id TEXT NOT NULL,session_day TEXT NOT NULL,expectation TEXT NOT NULL,detector TEXT NOT NULL,finding_id TEXT NOT NULL,base_severity TEXT NOT NULL,current_severity TEXT NOT NULL,occurrence_count INTEGER NOT NULL DEFAULT 1,cumulative_blind_seconds REAL NOT NULL DEFAULT 0,escalated INTEGER NOT NULL DEFAULT 0,informed_escalation INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL CHECK(status IN('OPEN','CLOSED')),opened_at TEXT NOT NULL,updated_at TEXT NOT NULL,closed_at TEXT,details_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(namespace,episode_id));
CREATE TABLE IF NOT EXISTS support_detector_runs(run_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,session_day TEXT NOT NULL,ran_at TEXT NOT NULL,findings_evaluated INTEGER NOT NULL,findings_created INTEGER NOT NULL,details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS support_exports(export_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,session_day TEXT NOT NULL,exported_at TEXT NOT NULL,json_path TEXT NOT NULL,markdown_path TEXT,sha256_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS support_heartbeats(heartbeat_id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,session_day TEXT NOT NULL,heartbeat_at TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'systemd_timer',status TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS support_approvals(batch_id TEXT PRIMARY KEY,session_day TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN('APPROVE','DECLINE','HOLD')),approved_changes_json TEXT NOT NULL,commit_shas_json TEXT NOT NULL,subset_tree_sha TEXT,actor TEXT NOT NULL,chat_turn_verified INTEGER NOT NULL DEFAULT 1,recorded_at TEXT NOT NULL,voided INTEGER NOT NULL DEFAULT 0,void_reason TEXT);"""


ALLOWED_WRITE_TABLES = (
    'support_findings',
    'support_checkpoints',
    'support_episodes',
    'support_detector_runs',
    'support_exports',
    'support_heartbeats',
    'support_approvals',
)


APPEND_ONLY_TABLES = ('support_findings',)


def _write_support_only(db, statements):
    """Enforce that the Support Agent never mutates trading tables (H2)."""
    for sql, _ in statements:
        norm = ' '.join(str(sql).strip().split()).upper()
        for t in APPEND_ONLY_TABLES:
            if norm.startswith(f'UPDATE {t.upper()} ') or norm.startswith(f'INSERT OR REPLACE INTO {t.upper()}'):
                raise PermissionError(f'support_findings_append_only_violation:{sql}')
        if not any(
            norm.startswith(f'INSERT INTO {t.upper()}')
            or norm.startswith(f'INSERT OR REPLACE INTO {t.upper()}')
            or norm.startswith(f'UPDATE {t.upper()} ')
            or norm.startswith(f'CREATE TABLE IF NOT EXISTS {t.upper()}')
            or (norm.startswith('CREATE INDEX IF NOT EXISTS ') and f' ON {t.upper()}(' in norm)
            for t in ALLOWED_WRITE_TABLES
        ):
            raise PermissionError(f'support_agent_write_violation:{sql}')
    return db.batch(statements)


def init(db, include_extended=False):
    stmts = [(s.strip(), ()) for s in SCHEMA.split(';') if s.strip()]
    if include_extended:
        stmts.extend((s.strip(), ()) for s in EXTENDED_SUPPORT_SCHEMA.split(';') if s.strip())
    _write_support_only(db, stmts)


class ReadOnlyTradingDB:
    """Opens a local SQLite trading database with mode=ro and blocks any write batch."""
    def __init__(self, sqlite_path):
        self.path = Path(sqlite_path).resolve()
        uri = f'file:{self.path.as_posix()}?mode=ro'
        self.c = sqlite3.connect(uri, uri=True)
        self.c.row_factory = sqlite3.Row

    def query(self, sql, args=()):
        norm = ' '.join(str(sql).strip().split()).upper()
        if not norm.startswith('SELECT ') and not norm.startswith('PRAGMA '):
            raise PermissionError(f'readonly_trading_db_query_violation:{sql}')
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def batch(self, stm):
        raise PermissionError('readonly_trading_db_write_forbidden')

    def close(self):
        self.c.close()


class SupportStore:
    """Local append-only, hash-chained out/support/support.sqlite store."""
    def __init__(self, sqlite_path=None):
        if sqlite_path is None:
            sqlite_path = ROOT / 'out' / 'support' / 'support.sqlite'
        self.path = None if str(sqlite_path) == ':memory:' else Path(sqlite_path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.c = sqlite3.connect(str(self.path))
        else:
            self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        init(self, include_extended=True)

    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)

    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def close(self):
        self.c.close()


def read_audit_spool(spool_dir=None):
    """Read logs/audit-spool/ as files without modifying or unlinking any spool file."""
    target = Path(spool_dir) if spool_dir is not None else (ROOT / 'logs' / 'audit-spool')
    if not target.exists():
        return []
    events = []
    for p in sorted(target.iterdir()):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding='utf8').strip()
            if not text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        except Exception:
            continue
    return events


def finding_id(detector, correlation_id, first_event_at):
    """Stable SHA-256 hash of detector + correlation_id + first_event_at."""
    if not detector or not correlation_id or not first_event_at:
        raise ValueError('finding_identity_required')
    raw = f'{detector}|{correlation_id}|{first_event_at}'
    return hashlib.sha256(raw.encode('utf8')).hexdigest()


def _supports_v3_severity(db):
    try:
        rows = db.query("SELECT sql FROM sqlite_master WHERE type='table' AND name='support_findings'")
        if rows and 'SEV1' in str(rows[0].get('sql') or ''):
            return True
    except Exception:
        pass
    return False


def _finding_hashes(db, namespace, fid, seq, detector, correlation_id, first_event_at, session_day, severity, status, obs_flag, notif_flag, user_details, stamp, actor):
    clean_payload = json.dumps(user_details or {}, sort_keys=True, separators=(',', ':'), default=str)
    evidence_hash = hashlib.sha256(clean_payload.encode('utf8')).hexdigest()
    prev_rows = db.query(
        'SELECT details_json FROM support_findings WHERE namespace=? ORDER BY rowid DESC LIMIT 1',
        (namespace,),
    )
    prev_hash = GENESIS
    if prev_rows:
        try:
            prev_chain = json.loads(prev_rows[0]['details_json']).get('_chain', {})
            prev_hash = str(prev_chain.get('entry_hash') or GENESIS)
        except Exception:
            prev_hash = GENESIS
    body = {
        'namespace': str(namespace),
        'finding_id': str(fid),
        'status_seq': int(seq),
        'detector': str(detector),
        'correlation_id': str(correlation_id),
        'first_event_at': str(first_event_at),
        'session_day': str(session_day),
        'severity': str(severity),
        'status': str(status),
        'observation_only': int(obs_flag),
        'notified': int(notif_flag),
        'evidence_hash': evidence_hash,
        'recorded_at': str(stamp),
        'actor': str(actor),
    }
    entry_hash = hashlib.sha256((prev_hash + '\n' + _stable_json(body)).encode('utf8')).hexdigest()
    enriched = {
        **(user_details or {}),
        '_chain': {
            'actor': str(actor),
            'evidence_hash': evidence_hash,
            'prev_hash': prev_hash,
            'entry_hash': entry_hash,
            'severity': str(severity),
            'status': str(status),
        },
    }
    payload = json.dumps(enriched, sort_keys=True, separators=(',', ':'), default=str)
    return evidence_hash, prev_hash, entry_hash, payload


def verify_findings_chain(db, namespace):
    """Verify hash-chain integrity and evidence_hash of support_findings rows."""
    rows = db.query('SELECT * FROM support_findings WHERE namespace=? ORDER BY rowid', (namespace,))
    prev = GENESIS
    failures = []
    for r in rows:
        raw_det = json.loads(r.get('details_json') or '{}')
        chain = raw_det.get('_chain', {})
        user_det = {k: v for k, v in raw_det.items() if k != '_chain'}
        clean_payload = json.dumps(user_det, sort_keys=True, separators=(',', ':'), default=str)
        ev_hash = hashlib.sha256(clean_payload.encode('utf8')).hexdigest()
        sev = str(chain.get('severity') or r['severity'])
        st = str(chain.get('status') or r['status'])
        actor = str(chain.get('actor') or 'support_agent')
        body = {
            'namespace': str(r['namespace']),
            'finding_id': str(r['finding_id']),
            'status_seq': int(r['status_seq']),
            'detector': str(r['detector']),
            'correlation_id': str(r['correlation_id']),
            'first_event_at': str(r['first_event_at']),
            'session_day': str(r['session_day']),
            'severity': sev,
            'status': st,
            'observation_only': int(r['observation_only']),
            'notified': int(r['notified']),
            'evidence_hash': ev_hash,
            'recorded_at': str(r['recorded_at']),
            'actor': actor,
        }
        expected = hashlib.sha256((prev + '\n' + _stable_json(body)).encode('utf8')).hexdigest()
        if chain.get('evidence_hash') != ev_hash or chain.get('prev_hash') != prev or chain.get('entry_hash') != expected:
            failures.append({
                'finding_id': r['finding_id'],
                'status_seq': int(r['status_seq']),
                'reason': 'support_finding_hash_chain_mismatch',
            })
        prev = str(chain.get('entry_hash') or '')
    return failures


def append_finding(db, namespace, detector, correlation_id, first_event_at, session_day, severity='WARN', status='OPEN', details=None, observation_only=True, notified=False, recorded_at=None, actor='support_agent'):
    """Append an initial OPEN finding idempotently without mutating existing rows."""
    if status not in VALID_STATUSES:
        raise ValueError('invalid_finding_status')
    if severity not in VALID_SEVERITIES:
        raise ValueError('invalid_finding_severity')
    session_day = ist_session_day(session_day)
    # Reruns and repeated events must not mint a second finding_id just because first_event_at differs
    # (finding_id hashes detector + correlation_id + first_event_at).
    prior = db.query(
        'SELECT finding_id, first_event_at FROM support_findings '
        'WHERE namespace=? AND session_day=? AND detector=? AND correlation_id=? '
        'ORDER BY first_event_at, status_seq LIMIT 1',
        (namespace, session_day, detector, correlation_id),
    )
    if prior:
        first_event_at = prior[0]['first_event_at']
    fid = finding_id(detector, correlation_id, first_event_at)
    existing = db.query(
        'SELECT status_seq,status,severity,details_json FROM support_findings WHERE namespace=? AND finding_id=? ORDER BY status_seq DESC LIMIT 1',
        (namespace, fid),
    )
    if existing and status == 'OPEN':
        ex_det = json.loads(existing[0]['details_json'] or '{}')
        ex_sev = ex_det.get('_chain', {}).get('severity', existing[0]['severity'])
        return {'created': False, 'finding_id': fid, 'status_seq': int(existing[0]['status_seq']), 'status': existing[0]['status'], 'severity': ex_sev}
    seq = (int(existing[0]['status_seq']) + 1) if existing else 1
    stamp = recorded_at or _utc_now()
    obs_flag = 1 if observation_only else 0
    notif_flag = 0 if observation_only else (1 if notified else 0)
    ev_hash, prev_hash, entry_hash, payload = _finding_hashes(
        db, namespace, fid, seq, detector, correlation_id, first_event_at, session_day,
        severity, status, obs_flag, notif_flag, details or {}, stamp, actor,
    )
    sql_sev = severity if _supports_v3_severity(db) else LEGACY_SEV_TO_SQL.get(severity, severity)
    sql_status = status if (status != 'VERIFIED' or _supports_v3_severity(db)) else 'RESOLVED'
    _write_support_only(db, [(
        'INSERT INTO support_findings(namespace,finding_id,status_seq,detector,correlation_id,first_event_at,session_day,severity,status,observation_only,notified,details_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (namespace, fid, seq, str(detector), str(correlation_id), str(first_event_at), str(session_day), sql_sev, sql_status, obs_flag, notif_flag, payload, stamp),
    )])
    return {'created': True, 'finding_id': fid, 'status_seq': seq, 'status': status, 'severity': severity, 'entry_hash': entry_hash, 'evidence_hash': ev_hash, 'prev_hash': prev_hash}


def transition_finding(db, namespace, fid, new_status, note=None, recorded_at=None, actor='support_agent', new_severity=None, extra_details=None):
    """Record an immutable status (or severity escalation) transition as a new append-only row."""
    if new_status not in VALID_STATUSES:
        raise ValueError('invalid_finding_status')
    if new_severity is not None and new_severity not in VALID_SEVERITIES:
        raise ValueError('invalid_finding_severity')
    rows = db.query(
        'SELECT * FROM support_findings WHERE namespace=? AND finding_id=? ORDER BY status_seq DESC LIMIT 1',
        (namespace, fid),
    )
    if not rows:
        raise KeyError('finding_not_found')
    latest = rows[0]
    seq = int(latest['status_seq']) + 1
    raw_latest = json.loads(latest['details_json'])
    latest_chain = raw_latest.get('_chain', {})
    prev_status = latest_chain.get('status', latest['status'])
    prev_sev = latest_chain.get('severity', latest['severity'])
    user_details = {k: v for k, v in raw_latest.items() if k != '_chain'}
    user_details = {**user_details, 'previous_status': prev_status, 'status_actor': str(actor)}
    if note is not None:
        user_details['status_note'] = str(note)
    if extra_details:
        user_details.update(extra_details)
    sev = new_severity or prev_sev
    stamp = recorded_at or _utc_now()
    obs_flag = int(latest['observation_only'])
    notif_flag = 0
    ev_hash, prev_hash, entry_hash, payload = _finding_hashes(
        db, namespace, fid, seq, latest['detector'], latest['correlation_id'],
        latest['first_event_at'], latest['session_day'], sev, new_status,
        obs_flag, notif_flag, user_details, stamp, actor,
    )
    sql_sev = sev if _supports_v3_severity(db) else LEGACY_SEV_TO_SQL.get(sev, sev)
    sql_status = new_status if (new_status != 'VERIFIED' or _supports_v3_severity(db)) else 'RESOLVED'
    _write_support_only(db, [(
        'INSERT INTO support_findings(namespace,finding_id,status_seq,detector,correlation_id,first_event_at,session_day,severity,status,observation_only,notified,details_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            namespace,
            fid,
            seq,
            latest['detector'],
            latest['correlation_id'],
            latest['first_event_at'],
            latest['session_day'],
            sql_sev,
            sql_status,
            obs_flag,
            notif_flag,
            payload,
            stamp,
        ),
    )])
    return {'finding_id': fid, 'status_seq': seq, 'status': new_status, 'severity': sev, 'actor': actor, 'entry_hash': entry_hash}


_EVIDENCE_BOOKKEEPING_KEYS = frozenset({'previous_status', 'status_actor', 'status_note'})


def update_finding_evidence(db, namespace, fid, details, recorded_at=None, actor='support_agent', as_of=None):
    """Append an OPEN->OPEN evidence row when an aggregate finding's evidence grew (append-only, idempotent).

    Aggregate per-session findings (counts, samples, last_check) are first written at the first tick that
    sees the problem. Later ticks must not lose the rest of the session's evidence, but an unchanged
    re-run must not add any row (zero duplicates).
    """
    rows = db.query(
        'SELECT details_json FROM support_findings WHERE namespace=? AND finding_id=? ORDER BY status_seq DESC LIMIT 1',
        (namespace, fid),
    )
    if not rows:
        return None
    raw = json.loads(rows[0]['details_json'] or '{}')
    chain = raw.get('_chain', {})
    if chain.get('status', 'OPEN') != 'OPEN':
        return None
    cur = {k: v for k, v in raw.items() if k != '_chain'}
    if as_of is not None and cur.get('evidence_as_of') and parse_ist(cur['evidence_as_of']) >= parse_ist(as_of):
        # A replay or late re-run of an earlier tick must never roll evidence back.
        return None
    new = json.loads(json.dumps(details or {}, sort_keys=True, default=str))
    if all(k in cur and _stable_json(cur[k]) == _stable_json(v) for k, v in new.items()):
        return None
    if as_of is not None:
        new['evidence_as_of'] = parse_ist(as_of).isoformat()
    return transition_finding(db, namespace, fid, 'OPEN', note='evidence_updated', recorded_at=recorded_at, actor=actor, extra_details=new)


def record_episode(db, namespace, session_day, expectation, detector, event_at, blind_seconds=0.0, base_severity='SEV3', details=None, observation_only=True, recorded_at=None, event_id=None):
    """Deduplicate absence/degradation findings into an open episode keyed on session_day + expectation.

    Escalation (v3): episodes count occurrence_count and cumulative_blind_seconds; crossing policy
    limits (episode.escalate_blind_seconds or episode.escalate_occurrences) lifts SEV3 to SEV2 and informs once.
    """
    session_day = ist_session_day(session_day)
    ep_key = f'{session_day}:{expectation}'
    cid = f'episode:{ep_key}'
    ev_token = str(event_id or f'{ep_key}:{event_at}')
    stamp = recorded_at or _utc_now()
    ep_cfg = SUPPORT_POLICY['episode']
    max_blind = float(ep_cfg['escalate_blind_seconds'])
    max_occ = int(ep_cfg['escalate_occurrences'])

    has_ep_table = bool(db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='support_episodes'"))
    all_ep_findings = [
        f for f in current_findings(db, namespace, session_day)
        if f['detector'] == detector and f['correlation_id'] == cid
    ]
    for f in all_ep_findings:
        if ev_token in set(f['details'].get('seen_event_ids', [])):
            return {
                'created': False,
                'finding_id': f['finding_id'],
                'status_seq': f['status_seq'],
                'status': f['status'],
                'severity': f['severity'],
                'episode_id': ep_key,
                'occurrence_count': f['details'].get('occurrence_count', 1),
                'cumulative_blind_seconds': f['details'].get('cumulative_blind_seconds', 0.0),
                'escalated': f['details'].get('escalated', False),
                'inform_immediately': False,
            }

    existing_findings = [f for f in all_ep_findings if f['status'] == 'OPEN']
    if not existing_findings:
        occ = 1
        cum_blind = float(blind_seconds or 0.0)
        escalated = (base_severity == 'SEV3') and (cum_blind >= max_blind or occ >= max_occ)
        cur_sev = 'SEV2' if escalated else base_severity
        ep_details = {
            **(details or {}),
            'expectation': expectation,
            'occurrence_count': occ,
            'cumulative_blind_seconds': cum_blind,
            'escalated': escalated,
            'episode_status': 'OPEN',
            'seen_event_ids': [ev_token],
        }
        res = append_finding(
            db, namespace, detector, cid, str(event_at), session_day,
            severity=cur_sev, status='OPEN', details=ep_details,
            observation_only=observation_only, notified=False, recorded_at=stamp,
        )
        if has_ep_table:
            _write_support_only(db, [(
                'INSERT OR REPLACE INTO support_episodes(namespace,episode_id,session_day,expectation,detector,finding_id,base_severity,current_severity,occurrence_count,cumulative_blind_seconds,escalated,informed_escalation,status,opened_at,updated_at,closed_at,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (namespace, ep_key, session_day, expectation, detector, res['finding_id'], base_severity, cur_sev, occ, cum_blind, int(escalated), int(escalated and not observation_only), 'OPEN', str(event_at), stamp, None, json.dumps(ep_details, sort_keys=True)),
            )])
        return {**res, 'episode_id': ep_key, 'occurrence_count': occ, 'cumulative_blind_seconds': cum_blind, 'escalated': escalated, 'inform_immediately': cur_sev in ('SEV1', 'SEV2')}

    latest = existing_findings[0]
    fid = latest['finding_id']
    prev_det = latest['details']
    seen_ids = list(prev_det.get('seen_event_ids', []))
    if ev_token not in seen_ids:
        seen_ids.append(ev_token)
    occ = int(prev_det.get('occurrence_count', 1)) + 1
    cum_blind = float(prev_det.get('cumulative_blind_seconds', 0.0)) + float(blind_seconds or 0.0)
    was_escalated = bool(prev_det.get('escalated', False))
    should_escalate = (base_severity == 'SEV3') and (cum_blind >= max_blind or occ >= max_occ)
    escalated = was_escalated or should_escalate
    cur_sev = 'SEV2' if escalated else latest['severity']
    newly_escalated = escalated and not was_escalated
    merged_details = {
        **prev_det,
        **(details or {}),
        'expectation': expectation,
        'occurrence_count': occ,
        'cumulative_blind_seconds': cum_blind,
        'escalated': escalated,
        'last_event_at': str(event_at),
        'episode_status': 'OPEN',
        'seen_event_ids': seen_ids,
    }
    tr = transition_finding(
        db, namespace, fid, 'OPEN',
        note='escalated_to_SEV2' if newly_escalated else 'episode_repeat',
        recorded_at=stamp,
        new_severity=cur_sev,
        extra_details=merged_details,
    )
    if has_ep_table:
        _write_support_only(db, [(
            'INSERT OR REPLACE INTO support_episodes(namespace,episode_id,session_day,expectation,detector,finding_id,base_severity,current_severity,occurrence_count,cumulative_blind_seconds,escalated,informed_escalation,status,opened_at,updated_at,closed_at,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (namespace, ep_key, session_day, expectation, detector, fid, base_severity, cur_sev, occ, cum_blind, int(escalated), int(newly_escalated and not observation_only), 'OPEN', latest['first_event_at'], stamp, None, json.dumps(merged_details, sort_keys=True)),
        )])
    return {'created': False, 'finding_id': fid, 'status_seq': tr['status_seq'], 'status': 'OPEN', 'severity': cur_sev, 'episode_id': ep_key, 'occurrence_count': occ, 'cumulative_blind_seconds': cum_blind, 'escalated': escalated, 'newly_escalated': newly_escalated, 'inform_immediately': newly_escalated}


def close_episode(db, namespace, session_day, expectation, detector, recovered_at, recovery_details=None, recorded_at=None):
    """Close an open episode on recovery without deleting the finding history."""
    session_day = ist_session_day(session_day)
    ep_key = f'{session_day}:{expectation}'
    cid = f'episode:{ep_key}'
    stamp = recorded_at or _utc_now()
    existing_findings = [
        f for f in current_findings(db, namespace, session_day)
        if f['detector'] == detector and f['correlation_id'] == cid and f['status'] == 'OPEN'
    ]
    if not existing_findings:
        return None
    latest = existing_findings[0]
    fid = latest['finding_id']
    extra = {
        **(recovery_details or {}),
        'recovered': True,
        'recovered_at': str(recovered_at),
        'episode_status': 'CLOSED',
    }
    tr = transition_finding(
        db, namespace, fid, 'RESOLVED',
        note='episode_recovered',
        recorded_at=stamp,
        extra_details=extra,
    )
    has_ep_table = bool(db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='support_episodes'"))
    if has_ep_table:
        _write_support_only(db, [(
            'UPDATE support_episodes SET status=?,closed_at=?,updated_at=? WHERE namespace=? AND episode_id=?',
            ('CLOSED', str(recovered_at), stamp, namespace, ep_key),
        )])
    return tr


def current_findings(db, namespace, session_day=None):
    """Return latest status per finding_id and full append-only history count."""
    if session_day:
        rows = db.query(
            'SELECT * FROM support_findings WHERE namespace=? AND session_day=? ORDER BY finding_id,status_seq',
            (namespace, ist_session_day(session_day)),
        )
    else:
        rows = db.query(
            'SELECT * FROM support_findings WHERE namespace=? ORDER BY finding_id,status_seq',
            (namespace,),
        )
    grouped = {}
    for r in rows:
        grouped.setdefault(r['finding_id'], []).append(r)
    out = []
    for fid, hist in grouped.items():
        latest = dict(hist[-1])
        latest['history_count'] = len(hist)
        raw_det = json.loads(latest['details_json'] or '{}')
        chain = raw_det.get('_chain', {})
        latest['details'] = {k: v for k, v in raw_det.items() if k != '_chain'}
        if chain:
            latest['severity'] = chain.get('severity', latest['severity'])
            latest['status'] = chain.get('status', latest['status'])
            latest['actor'] = chain.get('actor', 'support_agent')
            latest['evidence_hash'] = chain.get('evidence_hash', '')
            latest['prev_hash'] = chain.get('prev_hash', '')
            latest['entry_hash'] = chain.get('entry_hash', '')
        statuses = []
        for h in hist:
            hc = json.loads(h.get('details_json') or '{}').get('_chain', {})
            statuses.append(hc.get('status', h['status']))
        latest['statuses'] = statuses
        out.append(latest)
    return out


def get_checkpoint(db, namespace):
    rows = db.query('SELECT * FROM support_checkpoints WHERE namespace=?', (namespace,))
    if not rows:
        return {'namespace': namespace, 'last_recorded_at': '', 'last_event_id': '', 'last_decision_seq': 0}
    r = rows[0]
    return {
        'namespace': namespace,
        'last_recorded_at': str(r['last_recorded_at']),
        'last_event_id': str(r['last_event_id']),
        'last_decision_seq': int(r['last_decision_seq']),
    }


def event_correlation_id(event_row):
    try:
        payload = json.loads(event_row.get('data_json') or '{}')
    except Exception:
        payload = {}
    return str(payload.get('correlation_id') or event_row.get('signal_id') or event_row.get('run_id') or event_row.get('event_id'))


def consume_checkpoint(db, namespace, batch_limit=500, recorded_at=None, trading_db=None):
    """Consume new paper_events and paper_decisions after the stored checkpoint (read-only on trading tables)."""
    reader = trading_db if trading_db is not None else db
    cp = get_checkpoint(db, namespace)
    events = reader.query(
        'SELECT * FROM paper_events WHERE namespace=? AND (recorded_at>? OR (recorded_at=? AND event_id>?)) ORDER BY recorded_at,event_id LIMIT ?',
        (namespace, cp['last_recorded_at'], cp['last_recorded_at'], cp['last_event_id'], int(batch_limit)),
    )
    try:
        decisions = reader.query(
            'SELECT * FROM paper_decisions WHERE namespace=? AND seq>? ORDER BY seq LIMIT ?',
            (namespace, cp['last_decision_seq'], int(batch_limit)),
        )
    except Exception:
        decisions = []
    next_rec = cp['last_recorded_at']
    next_eid = cp['last_event_id']
    if events:
        next_rec = str(events[-1]['recorded_at'])
        next_eid = str(events[-1]['event_id'])
    next_seq = int(decisions[-1]['seq']) if decisions else cp['last_decision_seq']
    stamp = recorded_at or _utc_now()
    if events or decisions:
        _write_support_only(db, [(
            'INSERT INTO support_checkpoints(namespace,last_recorded_at,last_event_id,last_decision_seq,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET last_recorded_at=excluded.last_recorded_at,last_event_id=excluded.last_event_id,last_decision_seq=excluded.last_decision_seq,updated_at=excluded.updated_at',
            (namespace, next_rec, next_eid, next_seq, stamp),
        )])
    by_correlation = {}
    for ev in events:
        cid = event_correlation_id(ev)
        slot = by_correlation.setdefault(cid, {'events': [], 'decisions': []})
        slot['events'].append(ev)
    for dec in decisions:
        cid = str(dec['correlation_id'])
        slot = by_correlation.setdefault(cid, {'events': [], 'decisions': []})
        slot['decisions'].append(dec)
    return {
        'namespace': namespace,
        'events_consumed': len(events),
        'decisions_consumed': len(decisions),
        'events': events,
        'decisions': decisions,
        'by_correlation': by_correlation,
        'checkpoint': {
            'last_recorded_at': next_rec,
            'last_event_id': next_eid,
            'last_decision_seq': next_seq,
        },
    }


def verify_decision_reconstruction(decisions):
    """Verify hash-chain integrity and reconstructability of paper_decisions rows."""
    prev = GENESIS
    failures = []
    for row in sorted(decisions, key=lambda r: int(r['seq'])):
        seq = int(row['seq'])
        try:
            payload = json.loads(row['payload_json'])
        except Exception:
            failures.append({'seq': seq, 'correlation_id': row.get('correlation_id'), 'source_at': row.get('source_at'), 'reason': 'invalid_payload_json'})
            prev = str(row.get('entry_hash') or '')
            continue
        body = {
            'namespace': row['namespace'],
            'seq': seq,
            'correlation_id': row['correlation_id'],
            'decision_type': row['decision_type'],
            'strategy': row.get('strategy'),
            'symbol': row.get('symbol'),
            'signal_id': row.get('signal_id'),
            'cause': row.get('cause'),
            'rule': row.get('rule'),
            'execution_path': row.get('execution_path'),
            'source_at': row['source_at'],
            'recorded_at': row['recorded_at'],
            'payload': payload,
        }
        expected = hashlib.sha256((prev + '\n' + _stable_json(body)).encode('utf8')).hexdigest()
        if row.get('prev_hash') != prev or row.get('entry_hash') != expected:
            failures.append({'seq': seq, 'correlation_id': row.get('correlation_id'), 'source_at': row.get('source_at'), 'reason': 'hash_chain_mismatch'})
        missing = [k for k in ('correlation_id', 'decision_type', 'cause', 'rule', 'execution_path', 'source_at') if not row.get(k)]
        if row.get('decision_type') in ('ENTRY', 'EXIT') and ('price' not in payload or 'quantity' not in payload):
            missing.append('entry_exit_price_or_quantity')
        if missing:
            failures.append({'seq': seq, 'correlation_id': row.get('correlation_id'), 'source_at': row.get('source_at'), 'reason': 'missing_reconstruction_fields', 'fields': missing})
        prev = str(row.get('entry_hash') or '')
    return failures


def run_detectors(db, namespace, session_day, observation_only=True, stale_threshold_seconds=None, recorded_at=None, trading_db=None):
    """Run the deterministic support detectors (read-only on trading tables)."""
    # The timeout lives in support_policy.json (null until the user decides). Setting it must enable the
    # timeout, not crash every support pass.
    lifecycle_minutes = INCOMPLETE_LIFECYCLE_MINUTES
    if lifecycle_minutes is not None and (isinstance(lifecycle_minutes, bool) or not isinstance(lifecycle_minutes, (int, float)) or lifecycle_minutes <= 0):
        raise ValueError('incomplete_lifecycle_timeout_minutes_invalid')
    lifecycle_now = parse_ist(recorded_at) if recorded_at else ist_now()
    reader = trading_db if trading_db is not None else db
    session_day = ist_session_day(session_day)
    if stale_threshold_seconds is None:
        stale_threshold_seconds = int(CFG['worker_stale_seconds'])
    counts = derived_counts()
    expected_symbols = counts['expected_symbols']
    expected_cells = counts['expected_strategy_cells']

    events = reader.query('SELECT * FROM paper_events WHERE namespace=? AND session_day=? ORDER BY recorded_at,event_id', (namespace, session_day))
    all_decisions = reader.query('SELECT * FROM paper_decisions WHERE namespace=? ORDER BY seq', (namespace,))
    # IST session_day matching without all-days fallback
    day_decisions = [
        d for d in all_decisions
        if (d.get('source_at') and ist_session_day(d['source_at']) == session_day)
        or (d.get('recorded_at') and ist_session_day(d['recorded_at']) == session_day)
    ]
    created = []

    def record(detector, cid, first_at, severity, details):
        res = append_finding(
            db, namespace, detector, cid, first_at, session_day,
            severity=severity, status='OPEN', details=details,
            observation_only=observation_only, notified=False, recorded_at=recorded_at,
        )
        created.append({**res, 'detector': detector, 'correlation_id': cid})

    # 1. coverage_gap (derived counts from files, replacing hardcoded 751 and 5257)
    scan_runs = {}
    for ev in events:
        rid = ev['run_id']
        sr = scan_runs.setdefault(rid, {'first_at': ev['source_at'], 'prefilter': set(), 'strategy_cells': 0, 'completed': False})
        if ev['event_type'] == 'prefilter_decision' and ev.get('symbol'):
            sr['prefilter'].add(ev['symbol'])
        elif ev['event_type'] == 'strategy_decisions':
            try:
                sr['strategy_cells'] += len(json.loads(ev.get('data_json') or '{}').get('decisions', []))
            except Exception:
                pass
        elif ev['event_type'] == 'scan_completed':
            sr['completed'] = True
    for rid, sr in scan_runs.items():
        if sr['completed'] and (len(sr['prefilter']) < expected_symbols or sr['strategy_cells'] < expected_cells):
            record('coverage_gap', rid, sr['first_at'], 'WARN', {
                'run_id': rid,
                'prefilter_symbols': len(sr['prefilter']),
                'expected_symbols': expected_symbols,
                'strategy_cells': sr['strategy_cells'],
                'expected_strategy_cells': expected_cells,
            })

    # 2. incomplete_lifecycle (no numeric minute timeout invented)
    attempts = {}
    resolved_orders = set()
    runs_started = {}
    runs_finished = set()
    for ev in events:
        cid = event_correlation_id(ev)
        et = ev['event_type']
        if et == 'paper_order_attempt':
            attempts.setdefault(cid, ev['source_at'])
        elif et in ('paper_order_result', 'paper_order_executed', 'paper_order_blocked'):
            resolved_orders.add(cid)
        elif et == 'run_started':
            runs_started.setdefault(ev['run_id'], ev['source_at'])
        elif et in ('run_completed', 'run_failed'):
            runs_finished.add(ev['run_id'])
    for dec in day_decisions:
        resolved_orders.add(str(dec['correlation_id']))
    for cid, first_at in attempts.items():
        if lifecycle_minutes is not None and (lifecycle_now - parse_ist(first_at)).total_seconds() < float(lifecycle_minutes) * 60:
            continue
        if cid not in resolved_orders:
            record('incomplete_lifecycle', cid, first_at, 'ERROR', {
                'kind': 'order_attempt_without_decision_or_result',
                'incomplete_lifecycle_timeout_minutes': lifecycle_minutes,
                'timeout_source': 'support_policy.json' if lifecycle_minutes is not None else 'not_numeric_in_plan_not_invented',
            })
    for rid, first_at in runs_started.items():
        if lifecycle_minutes is not None and (lifecycle_now - parse_ist(first_at)).total_seconds() < float(lifecycle_minutes) * 60:
            continue
        if rid not in runs_finished:
            record('incomplete_lifecycle', rid, first_at, 'WARN', {
                'kind': 'run_started_without_completion',
                'incomplete_lifecycle_timeout_minutes': lifecycle_minutes,
                'timeout_source': 'support_policy.json' if lifecycle_minutes is not None else 'not_numeric_in_plan_not_invented',
            })

    # 3. unexpected_skip
    for dec in day_decisions:
        if dec.get('decision_type') == 'SKIP' and dec.get('cause') not in EXPECTED_SKIP_CAUSES:
            record('unexpected_skip', str(dec['correlation_id']), str(dec['source_at']), 'WARN', {
                'cause': dec.get('cause'),
                'rule': dec.get('rule'),
                'symbol': dec.get('symbol'),
                'strategy': dec.get('strategy'),
            })
    for ev in events:
        if ev['event_type'] == 'paper_order_blocked' and ev.get('outcome') not in EXPECTED_SKIP_CAUSES:
            record('unexpected_skip', event_correlation_id(ev), str(ev['source_at']), 'WARN', {
                'outcome': ev.get('outcome'),
                'event_id': ev['event_id'],
            })

    # 4. placeholder_text (unclassified_condition moved to error_taxonomy_gap in v3)
    for dec in day_decisions:
        raw = str(dec.get('payload_json') or '')
        m = PLACEHOLDER_PATTERN.search(raw)
        if m:
            record('placeholder_text', str(dec['correlation_id']), str(dec['source_at']), 'WARN', {
                'source': 'paper_decisions',
                'matched': m.group(0),
                'seq': int(dec['seq']),
            })
    for ev in events:
        raw = str(ev.get('data_json') or '')
        m = PLACEHOLDER_PATTERN.search(raw)
        if m:
            record('placeholder_text', event_correlation_id(ev), str(ev['source_at']), 'WARN', {
                'source': 'paper_events',
                'matched': m.group(0),
                'event_id': ev['event_id'],
            })

    # 5. reconstruction_failure
    for fail in verify_decision_reconstruction(all_decisions):
        cid = str(fail.get('correlation_id') or f"seq:{fail['seq']}")
        first_at = str(fail.get('source_at') or f'{session_day}T00:00:00+05:30')
        record('reconstruction_failure', cid, first_at, 'CRITICAL', fail)

    # 6. monitor_staleness
    check_times = []
    for ev in events:
        if ev['event_type'] == 'monitor_completed':
            try:
                check_times.append((parse_ist(ev['source_at']), ev))
            except Exception:
                pass
        elif ev['event_type'] == 'guardian_check':
            try:
                lag = json.loads(ev.get('data_json') or '{}').get('heartbeat_lag_seconds')
                if isinstance(lag, (int, float)) and lag > stale_threshold_seconds:
                    record('monitor_staleness', ev['run_id'], str(ev['source_at']), 'WARN', {
                        'heartbeat_lag_seconds': lag,
                        'threshold_seconds': stale_threshold_seconds,
                    })
            except Exception:
                pass
    check_times.sort(key=lambda x: x[0])
    for (t1, _), (t2, ev2) in zip(check_times, check_times[1:]):
        gap = (t2 - t1).total_seconds()
        if gap > stale_threshold_seconds:
            record('monitor_staleness', ev2['run_id'], str(ev2['source_at']), 'WARN', {
                'gap_seconds': gap,
                'threshold_seconds': stale_threshold_seconds,
            })

    # 7. named_cause_dump
    cause_counts = {}
    unmapped_causes = []
    for dec in day_decisions:
        c = str(dec.get('cause') or 'NONE')
        cause_counts[c] = cause_counts.get(c, 0) + 1
        if c != 'filled' and c != 'reinstated' and c not in EXPECTED_SKIP_CAUSES:
            unmapped_causes.append(c)
    if day_decisions or events:
        first_at = (day_decisions[0]['source_at'] if day_decisions else events[0]['source_at'])
        record('named_cause_dump', f'named_cause_dump:{session_day}', str(first_at), 'INFO', {
            'session_day': session_day,
            'cause_counts': cause_counts,
            'unmapped_causes': sorted(set(unmapped_causes)),
            'total_decisions': len(day_decisions),
        })

    return {
        'namespace': namespace,
        'session_day': session_day,
        'findings_evaluated': len(created),
        'findings_created': sum(1 for x in created if x['created']),
        'results': created,
    }



def reference_feed_403_hits(event):
    """Extract HTTP 403 feed failures from reference_feed_error or reference_feeds payloads.

    The live engine often logs kind=reference_feeds on the engine.jsonl spool (sources.<feed>.error
    like 'http_403') rather than a paper_events row typed reference_feed_error. Both shapes count.
    """
    et = str(event.get('event_type') or '')
    try:
        data = json.loads(event.get('data_json') or '{}') if not isinstance(event.get('data_json'), dict) else event['data_json']
    except Exception:
        data = event.get('data') or {}
    if not isinstance(data, dict):
        data = {}
    hits = []

    def is_403(info):
        if not isinstance(info, dict):
            return '403' in str(info)
        err = str(info.get('error') or '')
        status = info.get('http_status')
        return status == 403 or '403' in err or str(info.get('status') or '') in ('http_403',)

    if et == 'reference_feed_error' or (data.get('http_status') == 403 or '403' in str(data.get('error', '')) and et.startswith('reference_feed')):
        if is_403(data) or data.get('http_status') == 403 or '403' in str(data.get('error', '')):
            hits.append({
                'feed': data.get('feed') or data.get('name') or 'nse',
                'error': data.get('error'),
                'http_status': data.get('http_status') or 403,
            })
    sources = data.get('sources') if isinstance(data.get('sources'), dict) else {}
    if et in ('reference_feeds', 'reference_feed') or sources:
        for feed, info in sources.items():
            if is_403(info):
                err = info.get('error') if isinstance(info, dict) else info
                hits.append({'feed': feed, 'error': err, 'http_status': 403, 'status': (info or {}).get('status') if isinstance(info, dict) else None})
    # de-dupe by feed
    out, seen = [], set()
    for h in hits:
        k = str(h.get('feed') or 'nse')
        if k not in seen:
            seen.add(k); out.append(h)
    return out


def latency_clock_start(created_dt, session_start):
    """Official-open fills wait until 09:15. Measure signal-to-fill from max(created_at, session open)."""
    if created_dt is None:
        return session_start
    return created_dt if created_dt >= session_start else session_start


def reference_feed_ok(info):
    """True when a sources.<feed> entry is a successful fetch (not 403 / unavailable)."""
    if not isinstance(info, dict):
        return str(info).lower() in ('ok', 'success', 'available')
    err = str(info.get('error') or '')
    if info.get('http_status') == 403 or '403' in err:
        return False
    st = str(info.get('status') or 'ok').lower()
    return st in ('ok', 'success', 'available', '') and '403' not in st


def reference_feed_latest_status(events):
    """Return {feed: {at, ok, payload}} for the latest reference_feeds / reference_feed_error event per feed."""
    latest = {}
    for ev in events:
        et = ev.get('event_type')
        try:
            data = json.loads(ev.get('data_json') or '{}') if not isinstance(ev.get('data_json'), dict) else ev['data_json']
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        at = ev.get('source_at')
        sources = data.get('sources') if isinstance(data.get('sources'), dict) else {}
        if sources:
            for feed, info in sources.items():
                prev = latest.get(feed)
                if prev is None or str(at) >= str(prev['at']):
                    latest[feed] = {'at': at, 'ok': reference_feed_ok(info), 'info': info if isinstance(info, dict) else {'status': info}}
        elif et in ('reference_feed_error', 'reference_feed') or '403' in str(ev.get('data_json') or ''):
            feed = data.get('feed') or data.get('name') or 'nse'
            prev = latest.get(feed)
            if prev is None or str(at) >= str(prev['at']):
                latest[feed] = {'at': at, 'ok': reference_feed_ok(data), 'info': data}
    return latest


def collapse_duplicate_findings(db, namespace, session_day, recorded_at=None):
    """If earlier reruns minted more than one finding_id for the same correlation, keep the earliest and resolve the rest."""
    latest = current_findings(db, namespace, session_day)
    groups = {}
    for f in latest:
        groups.setdefault((f.get('detector'), f.get('correlation_id')), []).append(f)
    closed = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: (str(x.get('first_event_at') or ''), str(x.get('finding_id') or '')))
        keeper = items[0]
        for dup in items[1:]:
            if dup.get('status') in ('RESOLVED', 'WONT_FIX'):
                continue
            transition_finding(
                db, namespace, dup['finding_id'], 'RESOLVED',
                note='duplicate_correlation_id',
                recorded_at=recorded_at,
                extra_details={'deduplicated': True, 'kept_finding_id': keeper['finding_id']},
            )
            closed += 1
    return closed


def run_v3_production_detectors(db, namespace, session_day, now_at=None, trading_db=None, observation_only=True, check_policy_hygiene=False, box_metrics=None, dashboard_probe=None, recorded_at=None):
    """Run the full Production Action Plan v3 detector suite (SEV1-SEV4 + expectation ledger + episodes)."""
    assert_support_policy_lock()
    reader = trading_db if trading_db is not None else db
    session_day = ist_session_day(session_day)
    now_ist = parse_ist(now_at) if now_at is not None else parse_ist(f'{session_day}T16:35:00+05:30')
    stamp = recorded_at or now_ist.isoformat()
    collapse_duplicate_findings(db, namespace, session_day, recorded_at=stamp)

    raw_events = reader.query('SELECT * FROM paper_events WHERE namespace=? AND session_day=? ORDER BY source_at,event_id', (namespace, session_day))
    events = sorted(
        (ev for ev in raw_events if parse_ist(ev['source_at']) <= now_ist),
        key=lambda ev: (parse_ist(ev['source_at']), str(ev.get('event_id'))),
    )
    all_decisions = reader.query('SELECT * FROM paper_decisions WHERE namespace=? ORDER BY seq', (namespace,))
    day_decisions = [
        d for d in all_decisions
        if ((d.get('source_at') and ist_session_day(d['source_at']) == session_day)
            or (d.get('recorded_at') and ist_session_day(d['recorded_at']) == session_day))
        and parse_ist(d.get('source_at') or d.get('recorded_at')) <= now_ist
    ]
    fills = reader.query('SELECT * FROM paper_fills WHERE namespace=? ORDER BY filled_at,id', (namespace,))
    day_fills = [
        f for f in fills
        if f.get('filled_at') and ist_session_day(f['filled_at']) == session_day and parse_ist(f['filled_at']) <= now_ist
    ]
    signals = reader.query('SELECT * FROM paper_signals WHERE namespace=?', (namespace,))
    sig_by_id = {str(s['id']): s for s in signals}
    try:
        bands = reader.query('SELECT * FROM paper_price_bands WHERE trade_date=?', (session_day,))
    except Exception:
        bands = []

    results = []
    not_evaluable = {}

    def emit(detector, cid, first_at, severity, details):
        res = append_finding(
            db, namespace, detector, cid, str(first_at), session_day,
            severity=severity, status='OPEN', details=details,
            observation_only=observation_only, notified=False, recorded_at=stamp,
        )
        if not res['created']:
            # Aggregate findings keep the whole session's evidence (append-only evidence row, no-op if unchanged).
            update_finding_evidence(db, namespace, res['finding_id'], details, recorded_at=stamp, as_of=now_ist)
        results.append({**res, 'detector': detector, 'correlation_id': cid})
        return res

    if not is_trading_day(session_day):
        # Holidays and weekends are silent (plan section 4 / H8), including policy hygiene.
        return {
            'namespace': namespace,
            'session_day': session_day,
            'trading_day': False,
            'results': results,
            'not_evaluable': not_evaluable,
        }

    # Policy hygiene at 08:30 (once per day and key for null SEV3/SEV4 values)
    ph_h, ph_m = [int(x) for x in SUPPORT_POLICY['schedule']['pre_open_readiness'].split(':')]
    if check_policy_hygiene or (now_ist.hour == ph_h and now_ist.minute == ph_m):
        for item in missing_policy_values():
            key = item['key']
            emit('policy_value_missing', f'policy_missing:{session_day}:{key}', f"{session_day}T{SUPPORT_POLICY['schedule']['pre_open_readiness']}:00+05:30", 'SEV4', {
                'policy_key': key,
                'target_severity': item['target_severity'],
            })

    exp = SUPPORT_POLICY['expectations']
    thresh = SUPPORT_POLICY['thresholds']
    target_notional = float(CAP_POLICY['target_notional_inr'])
    max_sleeve_slots = int(CAP_POLICY['max_positions_per_sleeve'])
    poll_sec = float(CFG['poll_seconds'])
    latency_mult = float(SUPPORT_POLICY['latency']['signal_to_fill_poll_multiple'])

    # 1. Pre-market scan expectation (08:45 + 15m grace -> 09:00 IST) -> scan_missed (SEV2)
    scan_exp = exp['premarket_scan']
    scan_h, scan_m = [int(x) for x in scan_exp['expected_at'].split(':')]
    scan_deadline = datetime(
        int(session_day[:4]), int(session_day[5:7]), int(session_day[8:10]),
        scan_h, scan_m, tzinfo=IST,
    ) + timedelta(minutes=int(scan_exp['grace_minutes']))
    premarket_scans = [
        ev for ev in events
        if ev['event_type'] == 'scan_completed' and parse_ist(ev['source_at']) <= scan_deadline
    ]
    premarket_failed = [
        ev for ev in events
        if ev['event_type'] == 'scan_failed' and parse_ist(ev['source_at']) <= scan_deadline
    ]
    if now_ist >= scan_deadline and (not premarket_scans or premarket_failed):
        first_at = premarket_failed[0]['source_at'] if premarket_failed else f"{session_day}T{scan_exp['expected_at']}:00+05:30"
        ep = record_episode(
            db, namespace, session_day, 'premarket_scan_0845', 'scan_missed',
            first_at, blind_seconds=float(scan_exp['grace_minutes']) * 60,
            base_severity='SEV2', details={'expected_at': scan_exp['expected_at'], 'deadline_at': scan_deadline.isoformat(), 'failed_events': len(premarket_failed)},
            observation_only=observation_only, recorded_at=stamp, event_id=f'scan_missed:{session_day}',
        )
        results.append({**ep, 'detector': 'scan_missed'})

    # 2. Worker down & Monitor coverage staleness (09:15 - 15:30 IST)
    w_exp = exp['worker_active']
    ws_h, ws_m = [int(x) for x in w_exp['start_at'].split(':')]
    we_h, we_m = [int(x) for x in w_exp['end_at'].split(':')]
    session_start = datetime(int(session_day[:4]), int(session_day[5:7]), int(session_day[8:10]), ws_h, ws_m, tzinfo=IST)
    session_end = datetime(int(session_day[:4]), int(session_day[5:7]), int(session_day[8:10]), we_h, we_m, tzinfo=IST)
    monitor_events = [
        (parse_ist(ev['source_at']), ev)
        for ev in events
        if ev['event_type'] in ('monitor_completed', 'monitor_idle', 'monitor_check')
    ]
    monitor_events.sort(key=lambda x: x[0])

    if now_ist >= session_start + timedelta(seconds=int(w_exp['grace_seconds'])):
        if not monitor_events:
            blind = max(0.0, (min(now_ist, session_end) - session_start).total_seconds())
            ep = record_episode(
                db, namespace, session_day, 'worker_active_0915', 'worker_down',
                session_start.isoformat(), blind_seconds=blind,
                base_severity='SEV2', details={'window_start': w_exp['start_at'], 'first_check': None, 'blind_seconds': blind},
                observation_only=observation_only, recorded_at=stamp, event_id=f'worker_down:{session_day}:0915',
            )
            if now_ist >= session_end:
                # Whole session blind: record the final blind time once after the close (not one row per pass).
                update_finding_evidence(db, namespace, ep['finding_id'], {'blind_seconds': blind, 'cumulative_blind_seconds': blind}, recorded_at=stamp, as_of=now_ist)
            results.append({**ep, 'detector': 'worker_down'})
        else:
            first_check_dt, first_ev = monitor_events[0]
            last_check_dt, _ = monitor_events[-1]
            initial_gap = (first_check_dt - session_start).total_seconds()
            in_session = [dt for dt, _ in monitor_events if session_start <= dt <= session_end]
            max_gap = max([(b - a).total_seconds() for a, b in zip(in_session, in_session[1:])] or [0.0])
            stale_s = float(CFG['worker_stale_seconds'])
            late_last = now_ist >= session_end and (session_end - last_check_dt).total_seconds() > stale_s
            if initial_gap > float(w_exp['grace_seconds']):
                recovery = {
                    'window_start': w_exp['start_at'],
                    'first_check': first_check_dt.strftime('%H:%M:%S'),
                    'blind_window': f"{w_exp['start_at']}-{first_check_dt.strftime('%H:%M')}",
                    'blind_seconds': initial_gap,
                    'cumulative_blind_seconds': initial_gap,
                    'time_to_recover_seconds': initial_gap,
                }
                ep = record_episode(
                    db, namespace, session_day, 'worker_active_0915', 'worker_down',
                    session_start.isoformat(), blind_seconds=initial_gap,
                    base_severity='SEV2', details=recovery,
                    observation_only=observation_only, recorded_at=stamp, event_id=f'worker_down:{session_day}:0915',
                )
                # Plan 3.4: the episode closes on the recovery event (first monitor check) and records time-to-recover.
                close_episode(
                    db, namespace, session_day, 'worker_active_0915', 'worker_down',
                    first_ev['source_at'], recovery_details=recovery, recorded_at=stamp,
                )
                results.append({**ep, 'detector': 'worker_down'})
            gap_samples = []
            for a, b in zip(in_session, in_session[1:]):
                gap_s = (b - a).total_seconds()
                if gap_s > stale_s:
                    gap_samples.append({
                        'from_ist': a.strftime('%H:%M:%S'),
                        'to_ist': b.strftime('%H:%M:%S'),
                        'gap_seconds': gap_s,
                    })
            if initial_gap > float(w_exp['grace_seconds']) or max_gap > stale_s or late_last:
                ms_details = {
                    'first_check': first_check_dt.strftime('%H:%M:%S'),
                    'initial_gap_seconds': initial_gap,
                    'max_check_gap_seconds': max_gap,
                    'threshold_seconds': stale_s,
                    'gap_count': len(gap_samples),
                    'gaps': gap_samples,
                }
                if now_ist >= session_end:
                    # last check vs close is only final after the close (avoids one evidence row per pass).
                    ms_details['last_check'] = last_check_dt.strftime('%H:%M:%S')
                    ms_details['last_check_gap_to_close_seconds'] = (session_end - last_check_dt).total_seconds()
                emit('monitor_staleness', f'monitor_staleness:{session_day}', first_ev['source_at'], 'SEV2', ms_details)

    # 3. Scan lock blocked gaps -> lock_blocked_gap (SEV3, escalates to SEV2 via episode rules)
    lock_gap_min = float(thresh['lock_blocked_gap_seconds'])
    # The engine emits worker_waiting(reason=singleton_busy) every poll while a scan holds engine.lock.
    # A streak of those, bounded by the last check before and the first check after, is one check gap.
    check_types = ('monitor_completed', 'monitor_idle', 'worker_check')
    last_check_at = None
    streak = None
    for ev in events:
        et = ev['event_type']
        if et == 'worker_waiting':
            try:
                reason = json.loads(ev.get('data_json') or '{}').get('reason')
            except Exception:
                reason = None
            if reason == 'singleton_busy' and streak is None:
                streak = {'first': ev, 'gap_start': last_check_at or parse_ist(ev['source_at']), 'waits': 0}
            if reason == 'singleton_busy':
                streak['waits'] += 1
        elif et in check_types:
            at = parse_ist(ev['source_at'])
            if streak is not None:
                gap_s = (at - streak['gap_start']).total_seconds()
                if gap_s >= lock_gap_min:
                    ep = record_episode(
                        db, namespace, session_day, 'scan_lock_contention', 'lock_blocked_gap',
                        streak['first']['source_at'], blind_seconds=gap_s, base_severity='SEV3',
                        details={'gap_seconds': gap_s, 'event_id': streak['first']['event_id'], 'worker_waiting_events': streak['waits'], 'gap_end_at': ev['source_at']},
                        observation_only=observation_only, recorded_at=stamp, event_id=streak['first']['event_id'],
                    )
                    results.append({**ep, 'detector': 'lock_blocked_gap'})
                streak = None
            last_check_at = at
    for ev in events:
        if ev['event_type'] in ('lock_blocked_gap', 'worker_lock_blocked'):
            data = json.loads(ev.get('data_json') or '{}')
            gap_s = float(data.get('gap_seconds') or data.get('blocked_seconds') or 0.0)
            if gap_s >= lock_gap_min:
                ep = record_episode(
                    db, namespace, session_day, 'scan_lock_contention', 'lock_blocked_gap',
                    ev['source_at'], blind_seconds=gap_s, base_severity='SEV3',
                    details={'gap_seconds': gap_s, 'event_id': ev['event_id']},
                    observation_only=observation_only, recorded_at=stamp, event_id=ev['event_id'],
                )
                results.append({**ep, 'detector': 'lock_blocked_gap'})

    # 4. Worker errors -> error_taxonomy_gap (SEV3)
    unclassified_events = []
    for ev in events:
        raw = str(ev.get('data_json') or '')
        if ev['event_type'] == 'worker_error_unclassified' or ERROR_TAXONOMY_PATTERN.search(raw) or ev.get('outcome') == 'unclassified_condition':
            unclassified_events.append(ev)
    for dec in day_decisions:
        raw = str(dec.get('payload_json') or '')
        if ERROR_TAXONOMY_PATTERN.search(raw) or dec.get('cause') == 'unclassified_condition':
            unclassified_events.append(dec)
    if unclassified_events:
        first_src = unclassified_events[0].get('source_at') or f'{session_day}T09:15:00+05:30'
        last_src = unclassified_events[-1].get('source_at') or first_src
        emit('error_taxonomy_gap', f'error_taxonomy_gap:{session_day}', first_src, 'SEV3', {
            'unclassified_count': len(unclassified_events),
            'events': [str(x.get('event_id') or x.get('correlation_id')) for x in unclassified_events],
            'last_at': last_src,
        })
        recovered_at = None
        for ev in events:
            try:
                if parse_ist(ev.get('source_at') or last_src) <= parse_ist(last_src):
                    continue
            except Exception:
                if str(ev.get('source_at') or '') <= str(last_src):
                    continue
            if ev['event_type'] in ('eod_completed', 'eod_reconciled', 'quote_batch_recovered'):
                recovered_at = ev['source_at']
                break
            if ev['event_type'] in ('reference_feeds', 'reference_feed'):
                latest = reference_feed_latest_status([ev])
                if latest and all(v['ok'] for v in latest.values()):
                    recovered_at = ev['source_at']
                    break
        if recovered_at:
            existing = [f for f in current_findings(db, namespace, session_day)
                        if f['detector'] == 'error_taxonomy_gap' and f['status'] == 'OPEN']
            for f in existing:
                transition_finding(
                    db, namespace, f['finding_id'], 'RESOLVED',
                    note='condition_recovered',
                    recorded_at=stamp,
                    extra_details={'recovered': True, 'recovered_at': recovered_at, 'last_unclassified_at': last_src},
                )


    # 5. Quotes -> quote_batch_failed + recovery (SEV3) & quote_field_regression (SEV1)
    quote_failed_open = False
    for ev in events:
        data = json.loads(ev.get('data_json') or '{}')
        if ev['event_type'] == 'quote_batch_response' and quote_failed_open and ev.get('outcome') == 'ok':
            close_episode(
                db, namespace, session_day, 'quote_batch', 'quote_batch_failed',
                ev['source_at'], recovery_details={'recovered_at': ev['source_at'], 'recovered_by': 'quote_batch_response'}, recorded_at=stamp,
            )
            quote_failed_open = False
        if ev['event_type'] == 'quote_batch_failed':
            quote_failed_open = True
            ep = record_episode(
                db, namespace, session_day, 'quote_batch', 'quote_batch_failed',
                ev['source_at'], blind_seconds=float(data.get('timeout_seconds') or thresh['quote_timeout_seconds']),
                base_severity='SEV3', details={'timeout_seconds': data.get('timeout_seconds', thresh['quote_timeout_seconds']), 'at': ev['source_at']},
                observation_only=observation_only, recorded_at=stamp, event_id=ev['event_id'],
            )
            results.append({**ep, 'detector': 'quote_batch_failed'})
        elif ev['event_type'] == 'quote_batch_recovered':
            quote_failed_open = False
            close_episode(
                db, namespace, session_day, 'quote_batch', 'quote_batch_failed',
                ev['source_at'], recovery_details={'recovered_at': ev['source_at']}, recorded_at=stamp,
            )
        elif ev['event_type'] == 'quote_field_check' or 'quote_timestamp_field' in data:
            if data.get('quote_timestamp_field') == 'tt' and not data.get('used_depth_ltt', False):
                emit('quote_field_regression', f"quote_field_regression:{ev['event_id']}", ev['source_at'], 'SEV1', {
                    'field_used': 'tt',
                    'expected_field': 'ltt',
                })

    # Close a still-open quote episode when the stall/rate-limit class recovered even if later
    # fail-fast errors (short RuntimeError) were logged after the fix was installed.
    timeout_ms = float(thresh['quote_timeout_seconds']) * 1000.0
    last_stall_at = None
    for ev in events:
        if ev['event_type'] != 'quote_batch_failed':
            continue
        data = json.loads(ev.get('data_json') or '{}')
        dur = float(data.get('duration_ms') or 0.0)
        err = str(data.get('error_code') or data.get('error') or ev.get('outcome') or '')
        if dur >= timeout_ms or '429' in err.lower() or 'timeout' in err.lower() or data.get('timeout_seconds'):
            last_stall_at = ev['source_at']
    if last_stall_at:
        recover_at, recover_by = None, None
        for ev in events:
            try:
                if parse_ist(ev['source_at']) <= parse_ist(last_stall_at):
                    continue
            except Exception:
                if ev['source_at'] <= last_stall_at:
                    continue
            et = ev['event_type']
            if et == 'quote_batch_recovered' or (et == 'quote_batch_response' and ev.get('outcome') == 'ok'):
                recover_at, recover_by = ev['source_at'], et
                break
            if et in ('eod_completed', 'eod_reconciled'):
                recover_at, recover_by = ev['source_at'], et
                break
            if et in ('reference_feeds', 'reference_feed'):
                latest = reference_feed_latest_status([ev])
                if latest and all(v['ok'] for v in latest.values()):
                    recover_at, recover_by = ev['source_at'], et
                    break
        if recover_at:
            close_episode(
                db, namespace, session_day, 'quote_batch', 'quote_batch_failed',
                recover_at, recovery_details={'recovered_at': recover_at, 'recovered_by': recover_by,
                                              'last_timeout_or_429_at': last_stall_at},
                recorded_at=stamp,
            )

    # 6. Fill sizing -> fill_sizing_anomaly (SEV1, new v3)
    one_share_anomalies = []
    for f in day_fills:
        if str(f.get('side', 'BUY')).upper() != 'BUY':
            continue
        price = float(f.get('price') or 0.0)
        qty = int(f.get('qty') or f.get('quantity') or 0)
        if price > 0:
            try:
                expected_qty = int(capital.integer_shares(price, int(CAP_POLICY['target_notional_inr'])))
            except ValueError:
                expected_qty = int(math.floor(target_notional / price))
            if qty != expected_qty:
                one_share_anomalies.append({
                    'fill_id': f.get('id'),
                    'symbol': f.get('symbol'),
                    'strategy': f.get('strategy'),
                    'price': price,
                    'filled_qty': qty,
                    'expected_qty': expected_qty,
                    'filled_at': f.get('filled_at'),
                })
    if one_share_anomalies:
        emit('fill_sizing_anomaly', f'fill_sizing_anomaly:{session_day}', one_share_anomalies[0]['filled_at'], 'SEV1', {
            'anomaly_count': len(one_share_anomalies),
            'target_notional_inr': target_notional,
            'samples': one_share_anomalies,
        })

    # 7. Order gates -> sleeve_over_limit_new_buy (SEV1), duplicate_fill (SEV1), asm_gsm_buy (SEV1 or not_evaluable)
    buys_by_strategy = {}
    seen_strat_symbol = set()
    dup_fills = []
    for f in day_fills:
        if str(f.get('side', 'BUY')).upper() == 'BUY':
            st = int(f['strategy'])
            buys_by_strategy.setdefault(st, []).append(f)
            key = (st, f['symbol'], f.get('signal_id'))
            if key in seen_strat_symbol:
                dup_fills.append(f)
            seen_strat_symbol.add(key)
    for st, st_fills in sorted(buys_by_strategy.items()):
        if len(st_fills) > max_sleeve_slots:
            emit('sleeve_over_limit_new_buy', f'sleeve_over_limit:{session_day}:S{st}', st_fills[max_sleeve_slots]['filled_at'], 'SEV1', {
                'strategy': st,
                'buy_count': len(st_fills),
                'max_positions_per_sleeve': max_sleeve_slots,
            })
    if dup_fills:
        emit('duplicate_fill', f'duplicate_fill:{session_day}', dup_fills[0]['filled_at'], 'SEV1', {
            'duplicate_count': len(dup_fills),
            'fills': [d.get('id') for d in dup_fills],
        })

    if not bands:
        not_evaluable['asm_gsm_buy'] = 'reference_feed_not_loaded_for_session'
    else:
        restricted = {b['symbol'] for b in bands if b.get('asm_stage') or b.get('gsm_stage') or b.get('surveillance')}
        asm_buys = [f for f in day_fills if str(f.get('side', 'BUY')).upper() == 'BUY' and f.get('symbol') in restricted]
        if asm_buys:
            emit('asm_gsm_buy', f'asm_gsm_buy:{session_day}', asm_buys[0]['filled_at'], 'SEV1', {
                'symbols': [a['symbol'] for a in asm_buys],
            })

    # 8. Execution latency -> signal_to_fill_latency (SEV3, new v3)
    max_latency_sec = poll_sec * latency_mult
    high_latency_fills = []
    for f in day_fills:
        sid = str(f.get('signal_id') or '')
        sig = sig_by_id.get(sid)
        if sig and sig.get('created_at') and f.get('filled_at'):
            created_dt = parse_ist(sig['created_at'])
            filled_dt = parse_ist(f['filled_at'])
            if created_dt.date() == filled_dt.date():
                start_dt = latency_clock_start(created_dt, session_start)
                lat_sec = (filled_dt - start_dt).total_seconds()
                if lat_sec > max_latency_sec + 1:
                    high_latency_fills.append({
                        'signal_id': sid,
                        'symbol': f.get('symbol'),
                        'created_at': sig['created_at'],
                        'filled_at': f['filled_at'],
                        'latency_start': start_dt.isoformat(),
                        'latency_seconds': lat_sec,
                        'threshold_seconds': max_latency_sec,
                        'execution_path': f.get('execution_path'),
                    })
    for ev in events:
        if ev['event_type'] == 'signal_to_fill_latency':
            data = json.loads(ev.get('data_json') or '{}')
            created_raw = data.get('scan_at') or data.get('created_at')
            filled_raw = data.get('fill_at') or ev['source_at']
            created_dt = parse_ist(created_raw) if created_raw else None
            filled_dt = parse_ist(filled_raw) if filled_raw else None
            if created_dt and filled_dt:
                start_dt = latency_clock_start(created_dt, session_start)
                lat_sec = (filled_dt - start_dt).total_seconds()
            else:
                lat_sec = float(data.get('latency_seconds') or 0.0)
                start_dt = None
            if lat_sec > max_latency_sec + 1:
                high_latency_fills.append({
                    'signal_id': ev.get('signal_id'),
                    'created_at': created_raw,
                    'filled_at': filled_raw,
                    'latency_start': start_dt.isoformat() if start_dt else None,
                    'latency_seconds': lat_sec,
                    'threshold_seconds': max_latency_sec,
                })
    if high_latency_fills:
        emit('signal_to_fill_latency', f'signal_to_fill_latency:{session_day}', high_latency_fills[0]['filled_at'], 'SEV3', {
            'count': len(high_latency_fills),
            'max_latency_seconds': max(x['latency_seconds'] for x in high_latency_fills),
            'threshold_seconds': max_latency_sec,
            'samples': high_latency_fills,
        })

    # 9. Exits -> exit_path_unexercised (SEV4) after session close
    if now_ist >= session_end:
        sell_fills = [f for f in day_fills if str(f.get('side', '')).upper() == 'SELL']
        stop_events = [ev for ev in events if ev['event_type'] in ('stop_hit', 'target_hit', 'trail_stop_filled')]
        if not sell_fills and not stop_events:
            emit('exit_path_unexercised', f'exit_path_unexercised:{session_day}', session_end.isoformat(), 'SEV4', {
                'sell_fills': 0,
                'stop_or_target_hits': 0,
            })

    # 10. EOD expectation (16:15 + 20m grace = 16:35 IST) -> eod_missed (SEV1)
    eod_exp = exp['eod_reconciliation']
    eh, em = [int(x) for x in eod_exp['expected_at'].split(':')]
    eod_deadline = datetime(
        int(session_day[:4]), int(session_day[5:7]), int(session_day[8:10]),
        eh, em, tzinfo=IST,
    ) + timedelta(minutes=int(eod_exp['grace_minutes']))
    eod_events = [ev for ev in events if ev['event_type'] in ('eod_completed', 'eod_reconciled')]
    if now_ist >= eod_deadline and not eod_events:
        ep = record_episode(
            db, namespace, session_day, 'eod_1615', 'eod_missed',
            eod_deadline.isoformat(), blind_seconds=float(eod_exp['grace_minutes']) * 60,
            base_severity='SEV1', details={'expected_at': eod_exp['expected_at'], 'deadline_at': eod_deadline.isoformat()},
            observation_only=observation_only, recorded_at=stamp,
        )
        results.append({**ep, 'detector': 'eod_missed'})

    # 11. Decision log reconstruction -> reconstruction_failure (SEV1)
    for fail in verify_decision_reconstruction(all_decisions):
        cid = str(fail.get('correlation_id') or f"seq:{fail['seq']}")
        first_at = str(fail.get('source_at') or f'{session_day}T15:30:00+05:30')
        emit('reconstruction_failure', cid, first_at, 'SEV1', fail)

    # 12. Reference feeds HTTP 403 -> feed_http_403 (SEV3), one finding per feed per session.
    # A later successful fetch (for example after the 15:40 NSE session fix) marks the finding recovered.
    feed_hits = {}
    for ev in events:
        if ev['event_type'] in ('reference_feed_error', 'reference_feeds', 'reference_feed') or '403' in str(ev.get('data_json') or ''):
            for hit in reference_feed_403_hits(ev):
                feed = hit.get('feed') or 'nse'
                rec = feed_hits.setdefault(feed, {'first_at': ev['source_at'], 'last_at': ev['source_at'], 'count': 0, 'hit': hit})
                rec['count'] += 1
                rec['last_at'] = ev['source_at']
                rec['hit'] = hit
    latest_feeds = reference_feed_latest_status(events)
    for feed, rec in sorted(feed_hits.items()):
        cid = f'feed_http_403:{session_day}:{feed}'
        details = {
            **rec['hit'],
            'feed': feed,
            'occurrence_count': rec['count'],
            'first_at': rec['first_at'],
            'last_at': rec['last_at'],
        }
        emit('feed_http_403', cid, rec['first_at'], 'SEV3', details)
        later = latest_feeds.get(feed)
        if later and later['ok'] and parse_ist(later['at']) >= parse_ist(rec['last_at']):
            existing = [f for f in current_findings(db, namespace, session_day)
                        if f['detector'] == 'feed_http_403' and f['correlation_id'] == cid]
            if existing and existing[0]['status'] == 'OPEN':
                info = later.get('info') or {}
                transition_finding(
                    db, namespace, existing[0]['finding_id'], 'RESOLVED',
                    note='feed_recovered',
                    recorded_at=stamp,
                    extra_details={
                        'recovered': True,
                        'recovered_at': later['at'],
                        'recovered_status': info.get('status'),
                        'rows_written': info.get('rows_written'),
                        'kinds': info.get('kinds'),
                    },
                )


    # 13. Install pipeline events -> install_aborted_process_active, install_rolled_back (SEV2)
    for ev in events:
        if ev['event_type'] in ('install_aborted_process_active', 'install_rolled_back'):
            emit(ev['event_type'], f"{ev['event_type']}:{ev['event_id']}", ev['source_at'], 'SEV2', json.loads(ev.get('data_json') or '{}'))

    # 14. Box health & Dashboard probe if supplied
    if box_metrics:
        if box_metrics.get('disk_free_pct') is not None and float(box_metrics['disk_free_pct']) < float(thresh['box_disk_free_min_pct']):
            emit('box_health', f'box_health:disk:{session_day}', stamp, 'SEV2', {'kind': 'disk', 'disk_free_pct': box_metrics['disk_free_pct']})
        if box_metrics.get('clock_drift_seconds') is not None and abs(float(box_metrics['clock_drift_seconds'])) > float(thresh['box_clock_drift_max_seconds']):
            emit('box_health', f'box_health:clock:{session_day}', stamp, 'SEV2', {'kind': 'clock', 'clock_drift_seconds': box_metrics['clock_drift_seconds']})
        if box_metrics.get('stale_lock'):
            emit('box_health', f'box_health:stale_lock:{session_day}', stamp, 'SEV2', {'kind': 'stale_lock', 'lock': box_metrics['stale_lock']})
        if box_metrics.get('venv_broken'):
            emit('box_health', f'box_health:venv:{session_day}', stamp, 'SEV2', {'kind': 'venv'})
    if dashboard_probe and not dashboard_probe.get('ok', True):
        emit('dashboard_health', f'dashboard_health:{session_day}', stamp, 'SEV3', dashboard_probe)

    collapse_duplicate_findings(db, namespace, session_day, recorded_at=stamp)

    return {
        'namespace': namespace,
        'session_day': session_day,
        'trading_day': True,
        'results': results,
        'not_evaluable': not_evaluable,
    }


def build_nov9_replay_fixture(db, namespace='paper_v1'):
    """Populate a deterministic 2026-11-09 session fixture matching the 9 Nov evidence table in Plan v3."""
    day = '2026-11-09'
    stmts = [
        # 1. Monitor coverage: first check at 12:27:44, last check at 15:29:58 (no checks 09:15-12:27 -> worker_down + monitor_staleness)
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-mon-first', 'mon-1227', day, '2026-11-09T12:27:44+05:30', '2026-11-09T12:27:44+05:30', 'monitor_completed', 'INFO', 'ok', '{"check":"first"}')),
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-mon-last', 'mon-1529', day, '2026-11-09T15:29:58+05:30', '2026-11-09T15:29:58+05:30', 'monitor_completed', 'INFO', 'ok', '{"check":"last"}')),
        # 2. Scan lock blocked gaps of ~8 min (480s) and ~16 min (960s) -> cumulative 1440s >= 600s -> escalates SEV3 to SEV2
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-lock-1', 'lock-1', day, '2026-11-09T12:50:00+05:30', '2026-11-09T12:50:00+05:30', 'lock_blocked_gap', 'WARN', 'blocked', '{"gap_seconds":480}')),
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-lock-2', 'lock-2', day, '2026-11-09T13:30:00+05:30', '2026-11-09T13:30:00+05:30', 'lock_blocked_gap', 'WARN', 'blocked', '{"gap_seconds":960}')),
        # 3. Quotes: 30s timeout at 14:09 + recovery at 14:10 -> quote_batch_failed (recovered, SEV3)
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-quote-fail', 'q-1409', day, '2026-11-09T14:09:00+05:30', '2026-11-09T14:09:00+05:30', 'quote_batch_failed', 'WARN', 'timeout', '{"timeout_seconds":30}')),
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-quote-rec', 'q-1410', day, '2026-11-09T14:10:00+05:30', '2026-11-09T14:10:00+05:30', 'quote_batch_recovered', 'INFO', 'ok', '{}')),
        # 4. EOD completed at 16:17 IST (within 16:15 + 20m grace)
        ('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
         (namespace, 'nov9-eod-ok', 'eod-1617', day, '2026-11-09T16:17:00+05:30', '2026-11-09T16:17:00+05:30', 'eod_completed', 'INFO', 'ok', '{}')),
    ]
    # 5. 6 unclassified worker failures -> error_taxonomy_gap x6 (SEV3)
    for i in range(1, 7):
        stmts.append((
            'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (namespace, f'nov9-unclass-{i}', f'w-err-{i}', day, f'2026-11-09T13:0{i}:00+05:30', f'2026-11-09T13:0{i}:00+05:30', 'worker_error_unclassified', 'WARN', 'unclassified_condition', '{"reason":"unclassified_condition"}'),
        ))
    # 6. 17 fills at 1 share (12 in Strategy 7 -> sleeve_over_limit_new_buy; MAHABANK at 86.49 implies 578 shares -> fill_sizing_anomaly; first signal created at 12:57:47 and filled at 13:17:58 -> signal_to_fill_latency)
    for idx in range(1, 18):
        strat = 7 if idx <= 12 else 1
        sym = 'MAHABANK-EQ' if idx == 1 else f'SYM{idx:02d}-EQ'
        price = 86.49 if idx == 1 else 250.0
        sig_id = f'nov9-sig-{idx:02d}'
        created_at = '2026-11-09T12:57:47+05:30'
        filled_at = '2026-11-09T13:17:58+05:30' if idx == 1 else '2026-11-09T12:58:10+05:30'
        stmts.append((
            'INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (namespace, sig_id, strat, sym, '2026-11-08', 'BUY', 'filled', 'fixture', created_at, day),
        ))
        stmts.append((
            'INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow,execution_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (namespace, sig_id, strat, sym, 'BUY', 1, price, filled_at, filled_at, 'fixture_fill', 0.0, -price, 'LTP_LEGACY'),
        ))
    db.batch(stmts)


def run_virtual_clock_replay(support_db, trading_db, namespace='paper_v1', session_day='2026-11-09', ticks=None, observation_only=True):
    """Run virtual-clock replay across a session day and verify zero duplicate findings on repeat ticks."""
    session_day = ist_session_day(session_day)
    if ticks is None:
        ticks = [
            f'{session_day}T09:05:00+05:30',
            f'{session_day}T12:30:00+05:30',
            f'{session_day}T14:20:00+05:30',
            f'{session_day}T15:40:00+05:30',
            f'{session_day}T16:35:00+05:30',
        ]
    last_out = None
    for tick in ticks:
        last_out = run_v3_production_detectors(
            support_db, namespace, session_day,
            now_at=tick, trading_db=trading_db,
            observation_only=observation_only,
            check_policy_hygiene=False,
            recorded_at=tick,
        )
    findings = current_findings(support_db, namespace, session_day)
    by_sev = {'SEV1': [], 'SEV2': [], 'SEV3': [], 'SEV4': []}
    for f in findings:
        if f['severity'] in by_sev:
            by_sev[f['severity']].append(f['detector'])
    for k in by_sev:
        by_sev[k].sort()
    return {
        'namespace': namespace,
        'session_day': session_day,
        'findings': findings,
        'by_severity': by_sev,
        'not_evaluable': last_out['not_evaluable'] if last_out else {},
        'chain_failures': verify_findings_chain(support_db, namespace),
    }


def session_report(db, namespace, session_day, out_dir=None, write_markdown=False):
    """Build a deterministic support session report from append-only findings."""
    session_day = ist_session_day(session_day)
    findings = current_findings(db, namespace, session_day)
    by_detector = {}
    by_status = {s: 0 for s in VALID_STATUSES}
    by_severity = {s: 0 for s in VALID_SEVERITIES}
    named_causes = {}
    for f in findings:
        det = f['detector']
        by_detector[det] = by_detector.get(det, 0) + 1
        by_status[f['status']] = by_status.get(f['status'], 0) + 1
        by_severity[f['severity']] = by_severity.get(f['severity'], 0) + 1
        if det == 'named_cause_dump':
            named_causes = f.get('details', {}).get('cause_counts', {})
    report = {
        'namespace': namespace,
        'session_day': session_day,
        'total_findings': len(findings),
        'open_findings': by_status['OPEN'],
        'by_detector': by_detector,
        'by_status': by_status,
        'by_severity': by_severity,
        'named_causes': named_causes,
        'decision_chain_intact': by_detector.get('reconstruction_failure', 0) == 0,
        'support_findings_chain_intact': len(verify_findings_chain(db, namespace)) == 0,
        'incomplete_lifecycle_timeout_minutes': INCOMPLETE_LIFECYCLE_MINUTES,
        'checkpoint': get_checkpoint(db, namespace),
        'findings': findings,
    }
    if out_dir is not None:
        target = Path(out_dir) / f'support-{session_day}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(report, indent=2, sort_keys=True, default=str)
        target.write_text(json_text, encoding='utf8')
        report['report_path'] = str(target)
        report['report_sha256'] = hashlib.sha256(json_text.encode('utf8')).hexdigest()
        if write_markdown:
            md_target = Path(out_dir) / f'support-{session_day}.md'
            lines = [
                f'# Support Session Report — {session_day}',
                '',
                f"- **Namespace:** `{namespace}`",
                f"- **Total Findings:** {report['total_findings']} (Open: {report['open_findings']})",
                f"- **Decision Chain Intact:** `{report['decision_chain_intact']}`",
                f"- **Support Chain Intact:** `{report['support_findings_chain_intact']}`",
                '',
                '## Findings by Detector',
            ]
            for det, cnt in sorted(by_detector.items()):
                lines.append(f'- `{det}`: {cnt}')
            md_target.write_text('\n'.join(lines) + '\n', encoding='utf8')
            report['markdown_path'] = str(md_target)
    return report


SCHEDULE_SPEC = {
    'intraday_interval_seconds': int(SUPPORT_POLICY['schedule']['intraday_interval_seconds']),
    'post_close_offset_minutes': int(SUPPORT_POLICY['schedule']['post_close_offset_minutes']),
    'locks_engine': False,
    'mutates_trading_state': False,
}


def schedule_window(t, session_bounds):
    """Determine whether a scheduled support pass is due and which phase applies."""
    if not session_bounds:
        return {'due': False, 'phase': 'closed', 'schedule': SCHEDULE_SPEC}
    start_t, end_t = session_bounds
    wall = t.time().replace(tzinfo=None)
    sched_cfg = SUPPORT_POLICY['schedule']
    post_end = (datetime.combine(t.date(), end_t, tzinfo=IST).timestamp() + int(sched_cfg['post_close_window_seconds']))
    now_ts = datetime.combine(t.date(), wall, tzinfo=IST).timestamp()
    start_ts = datetime.combine(t.date(), start_t, tzinfo=IST).timestamp() - int(sched_cfg['pre_open_lead_seconds'])
    end_ts = datetime.combine(t.date(), end_t, tzinfo=IST).timestamp()
    if start_ts <= now_ts <= end_ts:
        return {'due': True, 'phase': 'intraday', 'schedule': SCHEDULE_SPEC}
    if end_ts < now_ts <= post_end:
        return {'due': True, 'phase': 'post_eod', 'schedule': SCHEDULE_SPEC}
    return {'due': False, 'phase': 'off_hours', 'schedule': SCHEDULE_SPEC}


def reminder_schedule_for_batch(batch_session_day, cfg=None):
    """Compute the at-most-3 reminder timestamps (19:30, 21:30 same day, and 07:30 on next trading day).

    Never schedules on weekends or NSE holidays (Monday after a Friday batch, or day after a holiday).
    """
    c = CFG if cfg is None else cfg
    d = date.fromisoformat(ist_session_day(batch_session_day))
    if not is_trading_day(d, cfg=c):
        return []
    next_td = next_trading_day(d, cfg=c)
    rem_times = SUPPORT_POLICY['schedule']['reminders']
    return [
        f'{d.isoformat()}T{rem_times[0]}:00+05:30',
        f'{d.isoformat()}T{rem_times[1]}:00+05:30',
        f'{next_td.isoformat()}T{rem_times[2]}:00+05:30',
    ][:int(SUPPORT_POLICY['schedule']['max_reminders'])]


def write_heartbeat(store_or_dir, namespace='paper_v1', now_at=None, source='systemd_timer'):
    """Write local sidecar heartbeat so reports can detect blind windows even when routines pause."""
    t = parse_ist(now_at) if now_at is not None else ist_now()
    day = t.date().isoformat()
    stamp = t.isoformat()
    if hasattr(store_or_dir, 'query') and hasattr(store_or_dir, 'batch'):
        has_hb = bool(store_or_dir.query("SELECT name FROM sqlite_master WHERE type='table' AND name='support_heartbeats'"))
        if has_hb:
            _write_support_only(store_or_dir, [(
                'INSERT INTO support_heartbeats(namespace,session_day,heartbeat_at,source,status) VALUES(?,?,?,?,?)',
                (namespace, day, stamp, source, 'ok'),
            )])
        return {'namespace': namespace, 'session_day': day, 'heartbeat_at': stamp, 'source': source}
    target = Path(store_or_dir) / 'heartbeat.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {'namespace': namespace, 'session_day': day, 'heartbeat_at': stamp, 'source': source, 'status': 'ok'}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf8')
    return payload


def run_scheduled_pass(db, namespace, session_day, session_bounds=None, now_at=None, observation_only=True, out_dir=None, notify_fn=None):
    """Execute one non-blocking support pass (checkpoint -> detectors -> report) without engine.lock."""
    session_day = ist_session_day(session_day)
    # Plan 3.1: the agent refuses to run when support_policy.json does not match its lock.
    assert_support_policy_lock()
    obs = bool(observation_only)
    window = schedule_window(now_at, session_bounds) if now_at is not None else {'due': True, 'phase': 'manual', 'schedule': SCHEDULE_SPEC}
    cp_res = consume_checkpoint(db, namespace)
    det_res = run_detectors(db, namespace, session_day=session_day, observation_only=obs)
    rep = session_report(db, namespace, session_day=session_day, out_dir=out_dir)
    notifications_sent = 0
    if not obs and notify_fn is not None and det_res['findings_created'] > 0:
        notify_fn(rep)
        notifications_sent = det_res['findings_created']
    return {
        'status': 'ok',
        'namespace': namespace,
        'session_day': session_day,
        'window': window,
        'observation_only': obs,
        'notifications_sent': notifications_sent,
        'checkpoint': cp_res,
        'detectors': det_res,
        'report': rep,
    }


def run_cli(e, db, session_day=None):
    """CLI entrypoint invoked from engine.py supervision branch without taking engine.lock."""
    t = e.now()
    day = str(session_day or t.date())
    obs = bool(e.CFG.get('support_observation_only', True))
    return run_scheduled_pass(
        db, e.NS, day,
        session_bounds=e.session(t.date()),
        now_at=t,
        observation_only=obs,
        out_dir=e.ROOT / 'out',
    )
