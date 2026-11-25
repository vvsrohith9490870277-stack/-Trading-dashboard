"""Tests for live/turso_write.py (Turso WRITE client via node db.mjs).

Uses db.mjs test_database (a local libSQL file under
automation/paper-trading/tests/test_*.sqlite) — never the live Turso DB.
Skips when @libsql/client cannot be resolved (same rule as the rest of the
live suite: run npm ci in the repo root or automation/paper-trading).
"""
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(HERE))

from turso_write import TursoWrite

DAILY_DDL = ('CREATE TABLE IF NOT EXISTS adaptive_candidate_daily'
             '(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, trade_date TEXT NOT NULL,'
             ' equity_paise INTEGER NOT NULL, mode TEXT NOT NULL, simulated INTEGER NOT NULL,'
             ' recorded_at TEXT NOT NULL, PRIMARY KEY(namespace, strategy, trade_date))')
TRADES_DDL = ('CREATE TABLE IF NOT EXISTS adaptive_candidate_trades'
              '(namespace TEXT NOT NULL, strategy INTEGER NOT NULL, symbol TEXT NOT NULL,'
              ' entry_date TEXT NOT NULL, exit_date TEXT, qty INTEGER NOT NULL,'
              ' entry_price REAL NOT NULL, exit_price REAL, net_pnl_paise INTEGER,'
              ' mode TEXT NOT NULL, simulated INTEGER NOT NULL, recorded_at TEXT NOT NULL,'
              ' PRIMARY KEY(namespace, strategy, symbol, entry_date))')


def libsql_available():
    """True only if @libsql/client resolves from automation/paper-trading."""
    r = subprocess.run(['node', '-e', "require('@libsql/client'); console.log('OK')"],
                       capture_output=True, text=True, cwd=str(LIB))
    return r.stdout.strip() == 'OK'


class TursoWriteTests(unittest.TestCase):
    def setUp(self):
        if not libsql_available():
            self.skipTest('@libsql/client not resolvable (run npm ci in automation/paper-trading)')
        self.path = LIB / 'tests' / ('test_shadow_turso_' + uuid.uuid4().hex + '.sqlite')
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def test_batch_and_query_roundtrip(self):
        w = TursoWrite(test_database=self.path)
        w.batch([(DAILY_DDL, ()), (TRADES_DDL, ())])
        w.batch([('INSERT OR REPLACE INTO adaptive_candidate_daily'
                  '(namespace,strategy,trade_date,equity_paise,mode,simulated,recorded_at) VALUES(?,?,?,?,?,?,?)',
                  ('adaptive_v1', 102, '2026-11-24', 50000000, 'shadow', 1, '2026-11-24T11:00:00+00:00'))])
        rows = w.query('SELECT namespace,strategy,trade_date,equity_paise,mode,simulated '
                       'FROM adaptive_candidate_daily')
        self.assertEqual(rows, [{'namespace': 'adaptive_v1', 'strategy': 102, 'trade_date': '2026-11-24',
                                 'equity_paise': 50000000, 'mode': 'shadow', 'simulated': 1}])

    def test_empty_batch_is_noop(self):
        calls = []

        def fake_run(*a, **k):
            calls.append(a)
            return subprocess.CompletedProcess(a, 0, stdout='{}', stderr='')

        w = TursoWrite(run=fake_run)
        w.batch([])
        self.assertEqual(calls, [])

    def test_failed_write_raises_safe_code(self):
        # A test_database outside the allowed tests/test_*.sqlite prefix is
        # refused by db.mjs (invalid_test_path); the client surfaces a safe
        # code, never a stack trace or a credential.
        w = TursoWrite(test_database=Path('/tmp/not-allowed.sqlite'))
        with self.assertRaises(RuntimeError) as ctx:
            w.batch([('SELECT 1', ())])
        code = str(ctx.exception)
        self.assertTrue(code.replace('_', '').isalnum())
        self.assertLess(len(code), 90)


if __name__ == '__main__':
    unittest.main()
