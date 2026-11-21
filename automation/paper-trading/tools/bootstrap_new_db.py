"""Bootstrap an EMPTY database with every table/index the app needs.

Turso move, Task 1. Grok runs this against the NEW free Turso DB; the
coordinator runs it against tmp sqlite files in tests.

What it creates (73 tables, 15 indexes):
  Group A (54 tables, 15 indexes): automation/paper-trading/schema.sql,
    read and executed at runtime (never copied here) so the bootstrap stays
    in sync with schema.sql. One exception: the support_findings statement
    in schema.sql is the NARROW version (severity INFO/WARN/ERROR/CRITICAL
    only) and would reject live support-agent writes, so it is replaced in
    place by the WIDE version from support_agent.py:188 (SEV1-SEV4 plus
    INFO/WARN/ERROR/CRITICAL; status includes VERIFIED).
  Group B (14 tables): inline schemas that live nowhere else — embedded
    verbatim below, each cited with its source file:line.
  Group C (3 tables): Render-only tables from render_schema.sql on
    origin/feat/render-deploy (render_state.migrate() already applies these
    on Render start, so duplicating them here is harmless).
  Group D (2 tables): daily_ohlcv / index_ohlcv have NO CREATE in prod
    code; defined here from inferred columns (see notes below).

Target selection (in priority order):
  --sqlite PATH      local sqlite file (testing / offline use)
  --db sqlite:///PATH   same, URL form
  (no flag)          Turso, from TURSO_DATABASE_URL + TURSO_AUTH_TOKEN
There is deliberately NO fallback to the box .env file: that file points
at the OLD database, and this script must never write to it. As a second
guard, any Turso URL containing the old DB name is refused outright.

Turso execution goes through the app's existing db.mjs (the same
per-call path engine.py uses: `node db.mjs` with a JSON batch request on
stdin). db.mjs reads its credentials from a .env FILE (PAPER_ENV_FILE),
so the script writes the env-var credentials to a temp .env file (0600,
deleted afterwards) and points PAPER_ENV_FILE at it for the child only.
No secret is ever printed: stdout/stderr on failure are redacted first.

Idempotent: every statement is CREATE TABLE IF NOT EXISTS /
CREATE INDEX IF NOT EXISTS, so re-running on a populated DB changes
nothing and never deletes rows.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent  # automation/paper-trading
DEFAULT_SCHEMA_SQL = ROOT / 'schema.sql'
DB_MJS = ROOT / 'db.mjs'

OLD_DB_NAME = 'tradinghistory-rohithvvs'  # the old Turso DB: never touch it
NODE_TIMEOUT_SECONDS = 300


class RefusedTarget(ValueError):
    """Usage-level refusal (no target, wrong scheme, old DB): exit code 2."""


# ---------------------------------------------------------------- Group B
# Inline schemas, embedded verbatim. Each block cites its source; do not
# edit the SQL here without also changing the source module.

# support_agent.py:188-190 (SCHEMA). Wide support_findings + its index +
# support_checkpoints (identical to schema.sql's; IF NOT EXISTS makes the
# duplicate creation harmless). This REPLACES schema.sql's narrow
# support_findings statement (see load_schema_statements).
SUPPORT_AGENT_SCHEMA = """CREATE TABLE IF NOT EXISTS support_findings(namespace TEXT NOT NULL,finding_id TEXT NOT NULL,status_seq INTEGER NOT NULL,detector TEXT NOT NULL,correlation_id TEXT NOT NULL,first_event_at TEXT NOT NULL,session_day TEXT NOT NULL,severity TEXT NOT NULL CHECK(severity IN('SEV1','SEV2','SEV3','SEV4','INFO','WARN','ERROR','CRITICAL')),status TEXT NOT NULL CHECK(status IN('OPEN','ACKNOWLEDGED','RESOLVED','WONT_FIX','VERIFIED')),observation_only INTEGER NOT NULL DEFAULT 1,notified INTEGER NOT NULL DEFAULT 0,details_json TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,finding_id,status_seq));
CREATE INDEX IF NOT EXISTS support_findings_session_idx ON support_findings(namespace,session_day,finding_id,status_seq);
CREATE TABLE IF NOT EXISTS support_checkpoints(namespace TEXT PRIMARY KEY,last_recorded_at TEXT NOT NULL,last_event_id TEXT NOT NULL,last_decision_seq INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);"""

# support_agent.py:199-203 (EXTENDED_SUPPORT_SCHEMA).
EXTENDED_SUPPORT_SCHEMA = """CREATE TABLE IF NOT EXISTS support_episodes(namespace TEXT NOT NULL,episode_id TEXT NOT NULL,session_day TEXT NOT NULL,expectation TEXT NOT NULL,detector TEXT NOT NULL,finding_id TEXT NOT NULL,base_severity TEXT NOT NULL,current_severity TEXT NOT NULL,occurrence_count INTEGER NOT NULL DEFAULT 1,cumulative_blind_seconds REAL NOT NULL DEFAULT 0,escalated INTEGER NOT NULL DEFAULT 0,informed_escalation INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL CHECK(status IN('OPEN','CLOSED')),opened_at TEXT NOT NULL,updated_at TEXT NOT NULL,closed_at TEXT,details_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(namespace,episode_id));
CREATE TABLE IF NOT EXISTS support_detector_runs(run_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,session_day TEXT NOT NULL,ran_at TEXT NOT NULL,findings_evaluated INTEGER NOT NULL,findings_created INTEGER NOT NULL,details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS support_exports(export_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,session_day TEXT NOT NULL,exported_at TEXT NOT NULL,json_path TEXT NOT NULL,markdown_path TEXT,sha256_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS support_heartbeats(heartbeat_id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,session_day TEXT NOT NULL,heartbeat_at TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'systemd_timer',status TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS support_approvals(batch_id TEXT PRIMARY KEY,session_day TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN('APPROVE','DECLINE','HOLD')),approved_changes_json TEXT NOT NULL,commit_shas_json TEXT NOT NULL,subset_tree_sha TEXT,actor TEXT NOT NULL,chat_turn_verified INTEGER NOT NULL DEFAULT 1,recorded_at TEXT NOT NULL,voided INTEGER NOT NULL DEFAULT 0,void_reason TEXT);"""

# support_finding_verify.py:35-44 (SCHEMA).
SUPPORT_FINDING_VERIFICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_finding_verifications(
  namespace TEXT NOT NULL,
  finding_id TEXT NOT NULL,
  verifier_id TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN('CONFIRM','REJECT','NOT_VERIFIABLE')),
  evidence_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(namespace, finding_id, verifier_id)
)
"""

# adaptive_logging.py:70-71 (adaptive_eod_markers, adaptive_parity — the only
# two tables in that module's SCHEMA that are not already in schema.sql).
ADAPTIVE_EOD_MARKERS = ("CREATE TABLE IF NOT EXISTS adaptive_eod_markers(namespace TEXT NOT NULL,trade_date TEXT NOT NULL,reconciled INTEGER NOT NULL CHECK(reconciled IN(0,1)),source_artifact TEXT NOT NULL,source_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,trade_date))")
ADAPTIVE_PARITY = ("CREATE TABLE IF NOT EXISTS adaptive_parity(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,trade_date TEXT NOT NULL,fills_match INTEGER NOT NULL,skips_match INTEGER NOT NULL,qty_match INTEGER NOT NULL,equity_diff_paise INTEGER NOT NULL,parity_ok INTEGER NOT NULL CHECK(parity_ok IN(0,1)),details_json TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,strategy,trade_date))")

# adaptive_sim_trades.py:9-25 (TABLE_SQL, ensure_table).
ADAPTIVE_SIM_TRADES = ("CREATE TABLE IF NOT EXISTS adaptive_sim_trades(namespace TEXT NOT NULL,spawn_id TEXT NOT NULL,strategy INTEGER NOT NULL,session_day TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT,entry_date TEXT NOT NULL,entry_price REAL NOT NULL,exit_date TEXT NOT NULL,exit_price REAL NOT NULL,exit_reason TEXT,qty INTEGER NOT NULL,pnl_paise INTEGER NOT NULL,track TEXT,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace, spawn_id, symbol, entry_date, exit_date))")

# live/candidate_shadow.py:46-56 (init).
ADAPTIVE_CANDIDATE_DAILY = ("CREATE TABLE IF NOT EXISTS adaptive_candidate_daily(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, trade_date TEXT NOT NULL, equity_paise INTEGER NOT NULL, mode TEXT NOT NULL, simulated INTEGER NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(namespace, strategy, trade_date))")
ADAPTIVE_CANDIDATE_TRADES = ("CREATE TABLE IF NOT EXISTS adaptive_candidate_trades(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, symbol TEXT NOT NULL, entry_date TEXT NOT NULL, exit_date TEXT, qty INTEGER NOT NULL, entry_price REAL NOT NULL, exit_price REAL, net_pnl_paise INTEGER, mode TEXT NOT NULL, simulated INTEGER NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(namespace, strategy, symbol, entry_date))")

# live/adaptive_daily.py:61-62 (inline in open_adaptive_db).
ADAPTIVE_LIVE_TRACK_A_EQUITY = ("CREATE TABLE IF NOT EXISTS adaptive_live_track_a_equity(namespace TEXT NOT NULL,trade_date TEXT NOT NULL,strategy INTEGER NOT NULL,equity_paise INTEGER NOT NULL,cash_paise INTEGER NOT NULL,market_value_paise INTEGER NOT NULL,open_positions INTEGER NOT NULL,basis TEXT NOT NULL,source_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(namespace,trade_date,strategy))")

# live/circuit_bands.py:30-44 (SCHEMA, init).
NSE_PRICE_BAND_ARCHIVE_SCHEMA = """
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

# server/demo-schema.sql:1-14.
PAPER_DEMO_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_demo_trades (
 id TEXT PRIMARY KEY,
 namespace TEXT NOT NULL CHECK(namespace='paper_demo_v1'),
 is_test INTEGER NOT NULL CHECK(is_test=1),
 strategy_label TEXT NOT NULL CHECK(strategy_label='Dummy Test'),
 symbol TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side='BUY'),
 qty INTEGER NOT NULL CHECK(qty>0),
 entry_price REAL NOT NULL CHECK(entry_price>0),
 status TEXT NOT NULL CHECK(status='OPEN'),
 entry_at TEXT NOT NULL,
 price_source TEXT NOT NULL CHECK(price_source='MANUAL_TEST_NOT_LIVE'),
 note TEXT NOT NULL
)
"""

# ---------------------------------------------------------------- Group C
# Render-only tables, verbatim from automation/paper-trading/render_schema.sql
# on branch origin/feat/render-deploy. render_state.migrate() already
# applies these idempotently on Render start; duplicating them here is
# harmless and lets one bootstrap cover every consumer.
RENDER_SCHEMA = """CREATE TABLE IF NOT EXISTS paper_runner_lease(
  namespace TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  acquired_unix INTEGER NOT NULL,
  expires_unix INTEGER NOT NULL,
  heartbeat_at TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_job_runs(
  namespace TEXT NOT NULL,
  job TEXT NOT NULL,
  session_day TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  claimed_unix INTEGER NOT NULL,
  lease_until_unix INTEGER NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  result_json TEXT,
  PRIMARY KEY(namespace, job, session_day)
);
CREATE TABLE IF NOT EXISTS paper_runtime_kv(
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope, key)
);"""

# ---------------------------------------------------------------- Group D
# OHLCV: NO CREATE in prod code (live/market_snapshot.py copies the DDL from
# the live DB at runtime). Columns inferred from the read side:
#   daily_ohlcv: the SELECT in live/market_snapshot.py + the full test
#     version in tests/test_history_repair.py:9 (there volume is REAL; the
#     brief specifies INTEGER here — volume is a share count).
#   index_ohlcv: same shape minus turnover (9 cols). Its PRIMARY KEY is
#     inferred by analogy with daily_ohlcv, NOT proven — the live DB is
#     unreadable, so Grok should confirm it against the new DB.
DAILY_OHLCV = ("CREATE TABLE IF NOT EXISTS daily_ohlcv(trade_date TEXT NOT NULL,symbol TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume INTEGER,turnover REAL,source TEXT,loaded_at TEXT,PRIMARY KEY(trade_date,symbol))")
INDEX_OHLCV = ("CREATE TABLE IF NOT EXISTS index_ohlcv(trade_date TEXT NOT NULL,symbol TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume INTEGER,source TEXT,loaded_at TEXT,PRIMARY KEY(trade_date,symbol))")


def split_statements(sql_text):
    """Split DDL on ';' exactly like engine.py init() does."""
    return [s.strip() for s in sql_text.split(';') if s.strip()]


def load_schema_statements(schema_path=None):
    """Group A: schema.sql, with the narrow support_findings statement
    replaced in place by the wide support_agent.py:188 version."""
    path = Path(schema_path) if schema_path else DEFAULT_SCHEMA_SQL
    try:
        text = path.read_text()
    except OSError as ex:
        raise RefusedTarget('cannot read schema file %s: %s' % (path, ex))
    out = []
    replaced = False
    for stmt in split_statements(text):
        if 'CREATE TABLE IF NOT EXISTS SUPPORT_FINDINGS(' in stmt.upper():
            out.extend(split_statements(SUPPORT_AGENT_SCHEMA))
            replaced = True
        else:
            out.append(stmt)
    if not replaced:
        raise RuntimeError(
            'schema.sql no longer contains support_findings; the wide-version'
            ' replacement in load_schema_statements needs updating')
    return out


def all_statements(schema_path=None):
    """Every DDL statement the bootstrap applies, in order."""
    stmts = load_schema_statements(schema_path)
    stmts.extend(split_statements(EXTENDED_SUPPORT_SCHEMA))
    stmts.extend(split_statements(SUPPORT_FINDING_VERIFICATIONS_SCHEMA))
    stmts.append(ADAPTIVE_EOD_MARKERS)
    stmts.append(ADAPTIVE_PARITY)
    stmts.append(ADAPTIVE_SIM_TRADES)
    stmts.append(ADAPTIVE_CANDIDATE_DAILY)
    stmts.append(ADAPTIVE_CANDIDATE_TRADES)
    stmts.append(ADAPTIVE_LIVE_TRACK_A_EQUITY)
    stmts.extend(split_statements(NSE_PRICE_BAND_ARCHIVE_SCHEMA))
    stmts.extend(split_statements(PAPER_DEMO_TRADES_SCHEMA))
    stmts.extend(split_statements(RENDER_SCHEMA))
    stmts.append(DAILY_OHLCV)
    stmts.append(INDEX_OHLCV)
    return stmts


def _summarize_sqlite(conn, statements):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()}
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()}
    return {'statements': len(statements), 'tables': len(tables),
            'indexes': len(indexes)}


def bootstrap_sqlite(path, schema_path=None):
    """Apply all DDL to a local sqlite file. Returns a summary dict."""
    stmts = all_statements(schema_path)
    conn = sqlite3.connect(os.fspath(path))
    try:
        with conn:
            for stmt in stmts:
                conn.execute(stmt)
        return _summarize_sqlite(conn, stmts)
    finally:
        conn.close()


def _redact(text, secrets):
    out = text or ''
    for s in secrets:
        if s:
            out = out.replace(s, '[redacted]')
    return out


def bootstrap_turso(url, token, schema_path=None):
    """Apply all DDL to a Turso DB via node db.mjs (batch action).

    db.mjs reads credentials from a .env FILE, so the credentials from the
    env vars are written to a temp .env (0600) that only the child sees,
    then deleted. Never prints the URL or token.
    """
    if not url or not token:
        raise RefusedTarget(
            'Turso target needs both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN')
    if url.startswith('file:'):
        raise RefusedTarget('TURSO_DATABASE_URL looks like a local file URL;'
                            ' use --sqlite for local files')
    if OLD_DB_NAME in url.lower():
        raise RefusedTarget(
            'refusing to touch the old database (%s); set TURSO_DATABASE_URL'
            ' to the NEW database' % OLD_DB_NAME)
    stmts = all_statements(schema_path)
    payload = {'action': 'batch',
               'statements': [[s, []] for s in stmts]}
    tmpdir = tempfile.mkdtemp(prefix='bootstrap-new-db-')
    env_path = Path(tmpdir) / '.env'
    try:
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, ('TURSO_DATABASE_URL=%s\nTURSO_AUTH_TOKEN=%s\n'
                          % (url, token)).encode())
        finally:
            os.close(fd)
        child_env = dict(os.environ)
        child_env['PAPER_ENV_FILE'] = str(env_path)
        try:
            result = subprocess.run(
                ['node', str(DB_MJS)], input=json.dumps(payload), text=True,
                capture_output=True, timeout=NODE_TIMEOUT_SECONDS,
                cwd=str(ROOT), env=child_env)
        except subprocess.TimeoutExpired:
            raise RuntimeError('db.mjs batch timed out after %ds'
                               % NODE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            detail = _redact((result.stderr or result.stdout or '').strip(),
                             [url, token])
            raise RuntimeError('db.mjs batch failed (exit %d): %s'
                               % (result.returncode, detail or 'no output'))
        host = urlparse(url).hostname or 'turso'
        return {'statements': len(stmts), 'tables': None, 'indexes': None,
                'host': host}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _parse_sqlite_url(db_url):
    if not db_url.startswith('sqlite://'):
        raise RefusedTarget(
            "--db expects a sqlite:///PATH URL (Turso targets come from"
            " TURSO_DATABASE_URL / TURSO_AUTH_TOKEN instead)")
    path = db_url[len('sqlite://'):]
    if not path:
        raise RefusedTarget('--db sqlite URL has no path')
    if path.startswith('//'):  # sqlite:////tmp/x.db -> /tmp/x.db
        path = path[1:]
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Create every table/index the app needs on an EMPTY'
                    ' database (idempotent).')
    parser.add_argument('--sqlite', metavar='PATH',
                        help='local sqlite file to bootstrap')
    parser.add_argument('--db', metavar='URL',
                        help='sqlite:///PATH URL for a local sqlite file')
    parser.add_argument('--schema', metavar='PATH',
                        help='schema.sql to read (default: %(default)s)',
                        default=None)
    args = parser.parse_args(argv)
    try:
        if args.sqlite:
            summary = bootstrap_sqlite(args.sqlite, args.schema)
            print('bootstrap complete: target=sqlite path=%s tables=%d'
                  ' indexes=%d statements=%d'
                  % (args.sqlite, summary['tables'], summary['indexes'],
                     summary['statements']))
        elif args.db:
            path = _parse_sqlite_url(args.db)
            summary = bootstrap_sqlite(path, args.schema)
            print('bootstrap complete: target=sqlite path=%s tables=%d'
                  ' indexes=%d statements=%d'
                  % (path, summary['tables'], summary['indexes'],
                     summary['statements']))
        else:
            url = os.environ.get('TURSO_DATABASE_URL')
            token = os.environ.get('TURSO_AUTH_TOKEN')
            if not url or not token:
                raise RefusedTarget(
                    'no target database: pass --sqlite PATH (or --db'
                    ' sqlite:///PATH), or set TURSO_DATABASE_URL and'
                    ' TURSO_AUTH_TOKEN for the NEW Turso database')
            summary = bootstrap_turso(url, token, args.schema)
            print('bootstrap complete: target=Turso host=%s statements=%d'
                  % (summary['host'], summary['statements']))
        return 0
    except RefusedTarget as ex:
        print('bootstrap refused: %s' % ex, file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, RuntimeError) as ex:
        print('bootstrap failed: %s' % _redact(
            str(ex), [os.environ.get('TURSO_DATABASE_URL'),
                      os.environ.get('TURSO_AUTH_TOKEN')]), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
