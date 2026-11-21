"""paper_v2 empty check (Turso move, brief Tasks 3 + 5).

Runs the real CLI (tools/check_paper_v2_empty.py) against synthetic fixture
logs copied into tmp dirs. No box, no Turso, no network, no repo-tree writes
(the only file the script may write is an explicit --report path inside tmp).

Fixture layout (tests/fixtures/):
  paper_v2_empty/     — mirrors the real story: 17 BUYs blocked on 20 Nov
                        (missing_or_failed_current_prefilter_proof), no fills,
                        plus one paper_v1 fill that must be ignored.
  paper_v2_nonempty/  — one paper_v2 BUY fill (fill_id 7, RELIANCE, strategy 2)
                        seen in engine.jsonl AND audit-2026-11-20.jsonl, plus
                        an open position.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # automation/paper-trading/
SCRIPT = ROOT / 'tools' / 'check_paper_v2_empty.py'
FIXTURES = HERE / 'fixtures'
PY = sys.executable

# The script is stdlib-only and importable without the repo on sys.path,
# so a direct import here is safe even on a bare checkout.
_spec = importlib.util.spec_from_file_location('check_paper_v2_empty', SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _load_report(path):
    return json.loads(Path(path).read_text())


class CheckPaperV2EmptyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _stage(self, fixture):
        """Copy a shipped fixture into tmp logs/ + out/ dirs; return (logs, out)."""
        src = FIXTURES / fixture
        logs = self.base / 'logs'
        out = self.base / 'out'
        logs.mkdir()
        out.mkdir()
        shutil.copy(src / 'engine.jsonl', logs / 'engine.jsonl')
        for audit in sorted(src.glob('audit-*.jsonl')):
            shutil.copy(audit, out / audit.name)
        return logs, out

    def _run(self, *args):
        r = subprocess.run([PY, str(SCRIPT), *args], text=True,
                           capture_output=True, timeout=60)
        return r

    def _run_json(self, logs, out, *extra):
        report = self.base / 'report.json'
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out),
                      '--report', str(report), *extra)
        data = _load_report(report) if report.exists() else None
        return r, data

    # ---- empty fixtures: exit 0 -------------------------------------------
    def test_empty_fixtures_are_empty(self):
        logs, out = self._stage('paper_v2_empty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('EMPTY', r.stdout)
        self.assertEqual(data['verdict'], 'EMPTY')
        self.assertEqual(data['fills'], [])
        self.assertEqual(data['positions'], [])
        self.assertEqual(data['realized_pnl'], [])

    def test_empty_fixtures_count_the_17_blocked_buys(self):
        logs, out = self._stage('paper_v2_empty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['blocked_signals'], 17)
        self.assertEqual(
            data['blocked_by_reason'],
            {'missing_or_failed_current_prefilter_proof': 17})
        self.assertIn('17', r.stdout)

    def test_empty_fixtures_ignore_other_namespaces(self):
        # The empty fixture contains a paper_v1 fill; it must not flip the
        # verdict, but it must be counted as ignored, not silently dropped.
        logs, out = self._stage('paper_v2_empty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['scanned']['ignored_other_namespace'], 1)

    def test_empty_fixtures_log_only_without_db(self):
        logs, out = self._stage('paper_v2_empty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['checked']['db'], 'none (log-only check)')
        self.assertIn('log-only', r.stdout)

    # ---- non-empty fixtures: exit 2 ---------------------------------------
    def test_nonempty_fixtures_are_not_empty(self):
        logs, out = self._stage('paper_v2_nonempty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('NOT EMPTY', r.stdout)
        self.assertEqual(data['verdict'], 'NOT EMPTY')

    def test_nonempty_fill_listed_once_with_fill_id_and_sources(self):
        # The same fill appears in engine.jsonl (attempt) and in
        # audit-2026-11-20.jsonl twice (executed + result). It is one fill.
        logs, out = self._stage('paper_v2_nonempty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(len(data['fills']), 1)
        fill = data['fills'][0]
        self.assertEqual(fill['fill_id'], 7)
        self.assertEqual(fill['symbol'], 'RELIANCE')
        self.assertEqual(fill['strategy'], 2)
        self.assertEqual(fill['side'], 'BUY')
        self.assertEqual(fill['day'], '2026-11-20')
        self.assertIn('engine.jsonl', fill['sources'])
        self.assertIn('audit-2026-11-20.jsonl', fill['sources'])
        self.assertIn('7', r.stdout)
        self.assertIn('RELIANCE', r.stdout)

    def test_nonempty_position_is_reported(self):
        logs, out = self._stage('paper_v2_nonempty')
        r, data = self._run_json(logs, out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(any(p['symbol'] == 'RELIANCE' and p['strategy'] == 2
                            for p in data['positions']))
        # The EOD total is a second, unattributed position entry — labelled so.
        self.assertTrue(any(p.get('detail') and 'EOD' in p['detail']
                            for p in data['positions']))
        self.assertIn('RELIANCE', r.stdout)

    def test_days_filter_limits_the_check(self):
        # The fill is only on 20 Nov: checking 19 Nov alone must be EMPTY.
        logs, out = self._stage('paper_v2_nonempty')
        r, data = self._run_json(logs, out, '--days', '2026-11-19')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['verdict'], 'EMPTY')
        self.assertEqual(data['days'], ['2026-11-19'])

    # ---- missing / unreadable logs: exit 1 --------------------------------
    def test_missing_logs_dir_is_exit_1_and_names_the_dir(self):
        missing = self.base / 'no-such-logs'
        out = self.base / 'out'
        out.mkdir()
        r = self._run('--logs-dir', str(missing), '--out-dir', str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(str(missing), r.stdout + r.stderr)

    def test_missing_engine_jsonl_is_exit_1_and_names_the_file(self):
        logs, out = self._stage('paper_v2_empty')
        (logs / 'engine.jsonl').unlink()
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('engine.jsonl', r.stdout + r.stderr)

    def test_missing_audit_file_for_a_requested_day_is_exit_1(self):
        logs, out = self._stage('paper_v2_empty')
        (out / 'audit-2026-11-20.jsonl').unlink()
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('audit-2026-11-20.jsonl', r.stdout + r.stderr)

    def test_malformed_json_line_is_exit_1_and_names_file_and_line(self):
        logs, out = self._stage('paper_v2_empty')
        with (logs / 'engine.jsonl').open('a') as f:
            f.write('{"at_IST": "2026-11-20T11:00:00+05:30", broken\n')
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('engine.jsonl', r.stdout + r.stderr)

    def test_bad_day_is_exit_1_not_exit_2(self):
        # Arg errors must never masquerade as NOT EMPTY (exit 2).
        logs, out = self._stage('paper_v2_empty')
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out),
                      '--days', '20-11-2026')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    # ---- --db (local sqlite): the Turso-side check -------------------------
    def _make_db(self, positions=(), fills=()):
        path = self.base / 'state.sqlite'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE paper_positions(namespace TEXT, strategy INTEGER,'
                    ' symbol TEXT, qty INTEGER, entry_price REAL, entry_at TEXT)')
        con.execute('CREATE TABLE paper_fills(id INTEGER PRIMARY KEY, namespace TEXT,'
                    ' signal_id TEXT, strategy INTEGER, symbol TEXT, side TEXT,'
                    ' qty INTEGER, price REAL, quote_at TEXT, filled_at TEXT,'
                    ' reason TEXT, realized_pnl REAL, cashflow REAL,'
                    ' execution_path TEXT)')
        con.executemany('INSERT INTO paper_positions VALUES(?,?,?,?,?,?)', positions)
        con.executemany('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', fills)
        con.commit()
        con.close()
        return path

    def test_db_empty_is_still_empty(self):
        logs, out = self._stage('paper_v2_empty')
        db = self._make_db()
        r, data = self._run_json(logs, out, '--db', str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['verdict'], 'EMPTY')
        self.assertEqual(data['checked']['db_positions'], 0)
        self.assertEqual(data['checked']['db_fills'], 0)

    def test_db_position_and_fill_make_it_not_empty(self):
        logs, out = self._stage('paper_v2_empty')
        db = self._make_db(
            positions=[('paper_v2', 2, 'RELIANCE', 32, 1523.4, '2026-11-20T04:01:00+00:00')],
            fills=[(7, 'paper_v2', 'cc01', 2, 'RELIANCE', 'BUY', 32, 1523.4,
                    '2026-11-20T04:01:00+00:00', '2026-11-20T04:01:00+00:00',
                    'ok', 0.0, -48748.8, 'LTP_LEGACY')])
        r, data = self._run_json(logs, out, '--db', str(db))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(data['checked']['db_positions'], 1)
        self.assertEqual(data['checked']['db_fills'], 1)
        self.assertTrue(any(p['symbol'] == 'RELIANCE' for p in data['positions']))
        self.assertTrue(any(f.get('fill_id') == 7 for f in data['fills']))

    def test_db_ignores_other_namespaces_and_zero_qty_positions(self):
        logs, out = self._stage('paper_v2_empty')
        db = self._make_db(
            positions=[('paper_v1', 1, 'TCS', 10, 4100.0, '2026-11-19T04:00:00+00:00'),
                       ('paper_v2', 2, 'RELIANCE', 0, 1523.4, '2026-11-20T04:01:00+00:00')],
            fills=[(9, 'paper_v1', 'ff01', 1, 'TCS', 'BUY', 10, 4100.0,
                    '2026-11-20T04:00:00+00:00', '2026-11-20T04:00:00+00:00',
                    'ok', 0.0, -41000.0, 'LTP_LEGACY')])
        r, data = self._run_json(logs, out, '--db', str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(data['verdict'], 'EMPTY')

    def test_db_sell_fill_with_realized_pnl_is_reported(self):
        logs, out = self._stage('paper_v2_empty')
        db = self._make_db(
            fills=[(11, 'paper_v2', 'dd01', 2, 'RELIANCE', 'SELL', 32, 1600.0,
                    '2026-11-20T09:00:00+00:00', '2026-11-20T09:00:00+00:00',
                    'ok', 2451.5, 51200.0, 'LTP_LEGACY')])
        r, data = self._run_json(logs, out, '--db', str(db))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(data['checked']['db_realized_pnl'], 2451.5)
        self.assertEqual(len(data['realized_pnl']), 1)

    def test_missing_db_file_is_exit_1(self):
        logs, out = self._stage('paper_v2_empty')
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out),
                      '--db', str(self.base / 'no-such.sqlite'))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    # ---- --turso via a stub db.mjs -----------------------------------------
    STUB_DB_MJS = textwrap.dedent('''\
        // Test stub: answers the check script's Turso queries from canned rows.
        import fs from 'node:fs';
        const req = JSON.parse(fs.readFileSync(0, 'utf8'));
        const canned = JSON.parse(process.env.STUB_ROWS || '[]');
        if (req.action !== 'query') { process.stdout.write('{}'); process.exit(0); }
        process.stdout.write(JSON.stringify(canned));
        ''')

    def _run_turso(self, logs, out, stub_rows):
        stub = self.base / 'stub_db.mjs'
        stub.write_text(self.STUB_DB_MJS)
        env = {**os.environ, 'STUB_ROWS': json.dumps(stub_rows)}
        r = subprocess.run([PY, str(SCRIPT), '--logs-dir', str(logs),
                            '--out-dir', str(out), '--turso',
                            '--db-mjs', str(stub)],
                           text=True, capture_output=True, timeout=60, env=env)
        return r

    @unittest.skipIf(shutil.which('node') is None, 'node is required for --turso')
    def test_turso_empty_via_stub_db_mjs(self):
        logs, out = self._stage('paper_v2_empty')
        r = self._run_turso(logs, out, [])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('turso', r.stdout.lower())

    @unittest.skipIf(shutil.which('node') is None, 'node is required for --turso')
    def test_turso_fill_via_stub_db_mjs(self):
        logs, out = self._stage('paper_v2_empty')
        # The stub answers every query with the same rows; the script asks
        # positions first, then fills, so use one fill row: it is returned
        # for the fills query and (harmlessly) for the positions query too.
        # Keep the stub honest by returning the fill row only when the SQL
        # mentions paper_fills — the stub below is replaced per test instead.
        stub = self.base / 'stub_db.mjs'
        stub.write_text(textwrap.dedent('''\
            import fs from 'node:fs';
            const req = JSON.parse(fs.readFileSync(0, 'utf8'));
            let rows = [];
            if (/paper_fills/.test(req.sql)) rows = [{id: 7, strategy: 2, symbol: 'RELIANCE', side: 'BUY', qty: 32, price: 1523.4, realized_pnl: 0, filled_at: '2026-11-20T04:01:00+00:00'}];
            process.stdout.write(JSON.stringify(rows));
            '''))
        r = subprocess.run([PY, str(SCRIPT), '--logs-dir', str(logs),
                            '--out-dir', str(out), '--turso',
                            '--db-mjs', str(stub)],
                           text=True, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('RELIANCE', r.stdout)
        self.assertIn('7', r.stdout)

    # ---- pure helpers -------------------------------------------------------
    def test_parse_days_accepts_csv_and_repeat_flags(self):
        self.assertEqual(check.parse_days(['2026-11-19,2026-11-20']),
                         ['2026-11-19', '2026-11-20'])
        self.assertEqual(check.parse_days(['2026-11-20', '2026-11-19']),
                         ['2026-11-19', '2026-11-20'])

    def test_parse_days_rejects_bad_dates(self):
        with self.assertRaises(ValueError):
            check.parse_days(['20-11-2026'])
        with self.assertRaises(ValueError):
            check.parse_days(['2026-13-01'])

    def test_check_is_read_only(self):
        # Running the check must not modify the logs, the audit files or the DB.
        logs, out = self._stage('paper_v2_nonempty')
        db = self._make_db(
            positions=[('paper_v2', 2, 'RELIANCE', 32, 1523.4, '2026-11-20T04:01:00+00:00')])
        before = {str(p): p.read_bytes()
                  for p in list(logs.iterdir()) + list(out.iterdir()) + [db]}
        r = self._run('--logs-dir', str(logs), '--out-dir', str(out),
                      '--db', str(db))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        after = {str(p): p.read_bytes()
                 for p in list(logs.iterdir()) + list(out.iterdir()) + [db]}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
