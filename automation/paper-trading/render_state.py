"""Render state store (Task 2): one small layer the supervisor, lease,
scheduler and token store share. Two backends with the same contract:

- SqliteStore: stdlib sqlite3, used by tests and dry-run (PAPER_TEST_DATABASE).
- TursoHttpStore: plain requests to the Turso HTTP pipeline API
  (POST https://<host>/v2/pipeline, Bearer token), confirmed against
  https://docs.turso.tech/sdk/http/reference (response results carry
  cols/rows/affected_row_count/last_insert_rowid).

All "now" arithmetic for leases and claims uses server time
(strftime('%s','now') inside the SQL), never the container clock.
Write volume is kept small by the callers (lease renewal every 20 s,
job-run rows only on claim/finish, budget write-behind); /health never
touches the store.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path

import requests

SCHEMA_PATH = Path(__file__).resolve().parent / 'render_schema.sql'
SCHEMA_TABLES = ('paper_runner_lease', 'paper_job_runs', 'paper_runtime_kv')


class TursoHttpError(RuntimeError):
    """Classified Turso failure. Never contains the token."""


def http_base_url(url):
    """libsql:// / turso:// -> https:// (per the Turso HTTP API docs)."""
    if url.startswith('libsql://'):
        return 'https://' + url[len('libsql://'):]
    if url.startswith('turso://'):
        return 'https://' + url[len('turso://'):]
    if url.startswith('https://'):
        return url.rstrip('/')
    raise ValueError('unsupported_turso_url')


def migrate(store):
    """Create the three render tables if missing. Idempotent; never touches
    any existing table."""
    text = SCHEMA_PATH.read_text()
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith('--')]
    stmts = [(s.strip(), ()) for s in '\n'.join(lines).split(';') if s.strip()]
    store.execute_many(stmts)
    return {'tables': sorted(SCHEMA_TABLES)}


class SqliteStore:
    """Local SQLite backend (tests, dry-run)."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=30000')

    def execute(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()] if cur.description is not None else []
        self.conn.commit()
        return rows

    def execute_many(self, statements):
        counts = []
        for sql, args in statements:
            cur = self.conn.execute(sql, args)
            counts.append(cur.rowcount if cur.rowcount is not None else 0)
        self.conn.commit()
        return counts

    def affected_rows(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0

    def server_time(self):
        rows = self.execute("SELECT CAST(strftime('%s','now') AS INTEGER) AS t")
        return int(rows[0]['t'])

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class TursoHttpStore:
    """Turso backend over the HTTP pipeline API (no Node helper needed)."""

    def __init__(self, url, token, session=None, timeout=10):
        self.base = http_base_url(url)
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _pipeline(self, reqs, read_only=False):
        body = {'requests': reqs}
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Content-Type': 'application/json'}
        attempts = 0
        while True:
            try:
                r = self.session.post(self.base + '/v2/pipeline', headers=headers,
                                      json=body, timeout=self.timeout)
            except Exception:
                # Retries only for idempotent reads; a write is never re-sent.
                if read_only and attempts < 2:
                    attempts += 1
                    time.sleep(0.2 * attempts)
                    continue
                raise TursoHttpError('turso_http_transport_failed') from None
            if r.status_code != 200:
                raise TursoHttpError('turso_http_status_%d' % r.status_code)
            try:
                payload = r.json()
            except Exception:
                raise TursoHttpError('turso_http_bad_response') from None
            results = payload.get('results') or []
            for res in results:
                # Error shape is defensive: the docs page shows the ok shape;
                # treat a result with type 'error' or an 'error' key as failure.
                if res.get('type') == 'error' or 'error' in res:
                    err = res.get('error') or {}
                    msg = str(err.get('message') or err.get('code') or 'turso_error')
                    raise TursoHttpError('turso_error:' + msg[:120])
            return results

    @staticmethod
    def _rows(res):
        cols = [(c or {}).get('name') for c in res.get('cols') or []]
        return [dict(zip(cols, row)) for row in res.get('rows') or []]

    def execute(self, sql, args=()):
        read_only = sql.strip().upper().startswith(('SELECT', 'WITH'))
        results = self._pipeline([
            {'type': 'execute', 'stmt': {'sql': sql, 'args': list(args)}},
            {'type': 'close'},
        ], read_only=read_only)
        return self._rows(results[0]) if results else []

    def execute_many(self, statements):
        reqs = [{'type': 'execute', 'stmt': {'sql': s, 'args': list(a)}}
                for s, a in statements]
        reqs.append({'type': 'close'})
        results = self._pipeline(reqs)
        return [int((r or {}).get('affected_row_count') or 0)
                for r in results[:len(statements)]]

    def affected_rows(self, sql, args=()):
        results = self._pipeline([
            {'type': 'execute', 'stmt': {'sql': sql, 'args': list(args)}},
            {'type': 'close'},
        ])
        return int((results[0] or {}).get('affected_row_count') or 0) if results else 0

    def server_time(self):
        rows = self.execute("SELECT CAST(strftime('%s','now') AS INTEGER) AS t")
        return int(rows[0]['t'])

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


def open_store_from_env(env=None):
    """PAPER_TEST_DATABASE -> SqliteStore; else Turso env creds -> TursoHttpStore."""
    env = os.environ if env is None else env
    test_db = env.get('PAPER_TEST_DATABASE')
    if test_db:
        return SqliteStore(test_db)
    url = env.get('TURSO_DATABASE_URL')
    token = env.get('TURSO_AUTH_TOKEN')
    if url and token:
        return TursoHttpStore(url, token)
    raise TursoHttpError('render_store_no_credentials')
