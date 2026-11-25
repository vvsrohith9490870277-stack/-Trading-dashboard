"""Python wrapper around automation/paper-trading/db.mjs for Turso WRITES
from the live sidecar (the mirror of ro_client.TursoRO, which is SELECT-only).

- One node db.mjs call per batch/query, same JSON protocol as
  automation/paper-trading/engine.py DB._call_per_process: stdin is
  {'action': 'batch'|'query', 'statements'|'sql', 'args', ...}, stdout is
  JSON. Credentials are NOT handled here: db.mjs reads TURSO_DATABASE_URL /
  TURSO_AUTH_TOKEN from the .env file (PAPER_ENV_FILE or the default
  /workspace/sahil-fyers-turso/.env) itself, exactly as the Track A engine
  does. This module never sees a token.
- Writes are never retried: a write whose outcome is unknown (timeout, or a
  helper that died after sending) raises, like engine.py's per-call path,
  so the caller can record the failure instead of risking a double write.
- test_database: when set, db.mjs uses a local libSQL file instead of
  Turso (db.mjs restricts it to automation/paper-trading/tests/test_*.sqlite).
  Tests use that; production never sets it.
- Only the adaptive shadow mirror uses this client. It writes ONLY the
  adaptive_candidate_* tables, never paper_v1 / paper_demo_v1 / paper_v2
  (the caller enforces the namespace guard; see candidate_shadow.run_shadow).
"""
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'automation' / 'paper-trading'
DB_MJS = LIB / 'db.mjs'


def _safe_code(code):
    code = str(code or 'TURSO_OPERATION_FAILED')
    return code if len(code) < 90 and all(c.isalnum() or c == '_' for c in code) else 'Turso_operation_failed'


class TursoWrite:
    """Write-only Turso client via node db.mjs (one process per call)."""

    def __init__(self, timeout=120, test_database=None, run=subprocess.run):
        self.timeout = timeout
        self.test_database = test_database
        self.run = run

    def _call(self, req):
        if self.test_database:
            req = {**req, 'test_database': str(self.test_database)}
        try:
            r = self.run(['node', str(DB_MJS)], input=json.dumps(req), text=True,
                         capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('turso_write_timeout') from None
        if r.returncode:
            try:
                code = json.loads(r.stderr).get('code', 'Turso_operation_failed')
            except (ValueError, AttributeError):
                code = 'Turso_operation_failed'
            raise RuntimeError(_safe_code(code))
        return json.loads(r.stdout)

    def query(self, sql, args=()):
        return self._call({'action': 'query', 'sql': sql, 'args': list(args)})

    def batch(self, statements):
        """statements: iterable of (sql, args). No-op on an empty list."""
        statements = list(statements)
        if statements:
            self._call({'action': 'batch',
                        'statements': [{'sql': s, 'args': list(a)} for s, a in statements]})
