"""Tests for tools/bootstrap_new_db.py (Turso move brief, Task 1).

Hermetic: tmp sqlite files only, no network, no Turso, no secrets.
The Turso path is exercised with a mocked subprocess runner.
"""
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent          # automation/paper-trading/tests
PAPER_TRADING = HERE.parent                      # automation/paper-trading
TOOLS = PAPER_TRADING / 'tools'
REPO_ROOT = PAPER_TRADING.parent.parent          # repo root

# Canonical 73-table inventory (turso-move brief, Task 1):
#   Group A (54): schema.sql — read at runtime by the bootstrap, listed here
#     only so the test can assert completeness.
#   Group B (14): inline schemas (adaptive_logging, adaptive_sim_trades,
#     candidate_shadow, adaptive_daily, circuit_bands, support_agent,
#     support_finding_verify, demo-schema).
#   Group C (3): Render-only (render_schema.sql on origin/feat/render-deploy).
#   Group D (2): OHLCV (defined from inferred columns; no CREATE in prod).
EXPECTED_TABLES = frozenset("""
adaptive_calibration_runs adaptive_candidate_daily adaptive_candidate_trades
adaptive_daily_returns adaptive_decision_logs adaptive_eod_markers
adaptive_gate_evaluations adaptive_holdout_vault adaptive_live_track_a_equity
adaptive_parity adaptive_probation adaptive_sim_trades adaptive_spawns
adaptive_tracks bhavcopy_series_archive daily_ohlcv daily_prefilter_universe
index_ohlcv nse_price_band_archive paper_cash_credits paper_charge_policies
paper_circuit_locks paper_closed_trades paper_corporate_actions
paper_data_audit paper_decisions paper_demo_trades paper_dp_debits
paper_entry_cohorts paper_entry_gate_proofs paper_entry_input_proofs paper_eod
paper_equity_closes paper_events paper_fill_costs paper_fills
paper_guardian_health paper_history_gap_checks paper_indicator_state
paper_input_evidence paper_input_policies paper_job_runs paper_manual_reviews
paper_meta paper_monitor_health paper_outages paper_pause_overrides
paper_pause_state paper_pnl paper_positions paper_prefilter_policies
paper_prefilter_states paper_price_bands paper_qualifier_states
paper_quote_audit paper_runner_lease paper_runs paper_runtime_kv
paper_scan_batches paper_shadow_costs paper_shadow_positions paper_signals
paper_state_scales paper_upper_circuit_blocks paper_watermarks
support_approvals support_checkpoints support_detector_runs support_episodes
support_exports support_finding_verifications support_findings
support_heartbeats
""".split())

# All 15 indexes live in schema.sql (the paper_events line carries two).
EXPECTED_INDEXES = frozenset("""
adaptive_logs_corr_idx adaptive_logs_query_idx adaptive_logs_spawn_idx
adaptive_spawns_strategy_idx bhavcopy_series_date_idx
daily_prefilter_universe_date_idx paper_cost_day_idx
paper_decisions_correlation paper_events_run paper_events_session
paper_fills_day_idx paper_positions_open_idx paper_qualified_bar_idx
paper_signals_pending_idx support_findings_session_idx
""".split())

# Module / config names the SQL-context grep below picks up from English
# "from <module>" in comments/docstrings. They are NOT tables.
NON_TABLE_TOKENS = frozenset({
    'adaptive_live_config',   # live/adaptive_live_config.json
    'adaptive_policy',        # automation/paper-trading/adaptive_policy.py
    'support_agent',          # automation/paper-trading/support_agent.py
    'support_exit_evidence',  # automation/paper-trading/support_exit_evidence.py
})


def _load_bootstrap():
    path = TOOLS / 'bootstrap_new_db.py'
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location('bootstrap_new_db', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOOT = _load_bootstrap()


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()}


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()}


class BootstrapSqliteTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(BOOT, 'tools/bootstrap_new_db.py not implemented yet')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'new.db')

    def _connect(self):
        return sqlite3.connect(self.db)

    def test_two_runs_create_all_tables_and_indexes_empty(self):
        self.assertEqual(len(EXPECTED_TABLES), 73)
        self.assertEqual(len(EXPECTED_INDEXES), 15)
        BOOT.bootstrap_sqlite(self.db)
        BOOT.bootstrap_sqlite(self.db)
        conn = self._connect()
        try:
            self.assertEqual(_tables(conn), set(EXPECTED_TABLES))
            self.assertEqual(_indexes(conn), set(EXPECTED_INDEXES))
            for t in sorted(_tables(conn)):
                n = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                self.assertEqual(n, 0, '%s not empty after bootstrap' % t)
        finally:
            conn.close()

    def test_marker_row_survives_second_run(self):
        BOOT.bootstrap_sqlite(self.db)
        conn = self._connect()
        conn.execute("INSERT INTO paper_meta(namespace,key,value)"
                     " VALUES('paper_v2','mode','PAPER_ONLY')")
        conn.commit()
        conn.close()
        BOOT.bootstrap_sqlite(self.db)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM paper_meta WHERE namespace='paper_v2'"
                " AND key='mode'").fetchone()
            self.assertEqual(row, ('PAPER_ONLY',))
        finally:
            conn.close()

    def test_support_findings_wide_version(self):
        # schema.sql:57 has the NARROW version (INFO/WARN/ERROR/CRITICAL
        # only); the live support agent writes SEV1..SEV4 + VERIFIED
        # (support_agent.py:188), so the bootstrap must use the wide one.
        BOOT.bootstrap_sqlite(self.db)
        conn = self._connect()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name='support_findings'").fetchone()[0]
            self.assertIn('SEV1', sql)
            self.assertIn('VERIFIED', sql)
            conn.execute(
                "INSERT INTO support_findings(namespace,finding_id,status_seq,"
                "detector,correlation_id,first_event_at,session_day,severity,"
                "status,details_json,recorded_at) VALUES("
                "'paper_v2','f1',1,'d','c','2026-11-21T00:00:00Z','2026-11-21',"
                "'SEV1','VERIFIED','{}','2026-11-21T00:00:00Z')")
            conn.commit()
        finally:
            conn.close()

    def test_ohlcv_shapes(self):
        BOOT.bootstrap_sqlite(self.db)
        conn = self._connect()
        try:
            daily = {r[1]: r for r in conn.execute("PRAGMA table_info(daily_ohlcv)")}
            self.assertEqual(set(daily), {
                'trade_date', 'symbol', 'open', 'high', 'low', 'close',
                'volume', 'turnover', 'source', 'loaded_at'})
            self.assertEqual(
                [name for name, r in sorted(daily.items(), key=lambda kv: kv[1][5])
                 if r[5] > 0], ['trade_date', 'symbol'])
            idx = {r[1]: r for r in conn.execute("PRAGMA table_info(index_ohlcv)")}
            self.assertEqual(set(idx), {
                'trade_date', 'symbol', 'open', 'high', 'low', 'close',
                'volume', 'source', 'loaded_at'})
            self.assertNotIn('turnover', idx)
            self.assertEqual(
                [name for name, r in sorted(idx.items(), key=lambda kv: kv[1][5])
                 if r[5] > 0], ['trade_date', 'symbol'])
        finally:
            conn.close()

    def test_statement_table_set_is_exactly_the_canonical_73(self):
        # paper_outages exists ONLY in schema.sql (no inline copy): if the
        # bootstrap did not read schema.sql at runtime it would be missing.
        names = set()
        for s in BOOT.all_statements():
            m = re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_0-9]+)",
                          s, re.I)
            if m:
                names.add(m.group(1))
        self.assertIn('paper_outages', names)
        self.assertEqual(names, set(EXPECTED_TABLES))


class BootstrapCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(BOOT, 'tools/bootstrap_new_db.py not implemented yet')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'new.db')

    def _no_turso_env(self):
        return {k: v for k, v in os.environ.items()
                if not k.startswith('TURSO_')}

    def test_no_target_refuses_cleanly(self):
        err = io.StringIO()
        with patch.dict(os.environ, self._no_turso_env(), clear=True), \
                redirect_stderr(err):
            rc = BOOT.main([])
        self.assertEqual(rc, 2)
        self.assertIn('TURSO_DATABASE_URL', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_cli_sqlite_flag(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = BOOT.main(['--sqlite', self.db])
        self.assertEqual(rc, 0)
        self.assertIn('73', out.getvalue())
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(_tables(conn), set(EXPECTED_TABLES))
        finally:
            conn.close()

    def test_cli_db_sqlite_url(self):
        rc = BOOT.main(['--db', 'sqlite:///%s' % self.db])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(self.db)
        try:
            self.assertIn('paper_signals', _tables(conn))
        finally:
            conn.close()

    def test_db_flag_rejects_non_sqlite_scheme(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = BOOT.main(['--db', 'libsql://foo.turso.io'])
        self.assertEqual(rc, 2)
        self.assertNotIn('Traceback', err.getvalue())

    def test_never_touches_untargeted_db(self):
        sentinel = str(Path(self.tmp.name) / 'sentinel.db')
        conn = sqlite3.connect(sentinel)
        conn.execute("CREATE TABLE marker(x TEXT)")
        conn.execute("INSERT INTO marker VALUES('do-not-touch')")
        conn.commit()
        before = _tables(conn)
        conn.close()
        BOOT.main(['--sqlite', self.db])
        conn = sqlite3.connect(sentinel)
        try:
            self.assertEqual(_tables(conn), before)
            self.assertEqual(conn.execute("SELECT x FROM marker").fetchone(),
                             ('do-not-touch',))
        finally:
            conn.close()


class BootstrapCodeGrepTests(unittest.TestCase):
    def test_every_table_the_code_references_is_bootstrapped(self):
        # Fails if prod code queries a table the bootstrap does not create.
        marker = REPO_ROOT / 'automation' / 'paper-trading' / 'schema.sql'
        if not marker.exists():
            self.skipTest('repo root not found from test location')
        sql_ctx = re.compile(r"(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)", re.I)
        ddl_ctx = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", re.I)
        list_ctx = re.compile(r"\b\w*TABLES?\s*=\s*[\(\[]([^)\]]*)", re.S)
        quoted = re.compile(r"""['"]([a-z_][a-z0-9_]*)['"]""")
        prefixes = ('paper_', 'adaptive_', 'support_', 'daily_', 'bhavcopy_',
                    'nse_', 'index_')
        candidates = set()
        for p in REPO_ROOT.rglob('*'):
            if p.suffix not in ('.py', '.mjs', '.js', '.sql'):
                continue
            if '.git' in p.parts or 'node_modules' in p.parts:
                continue
            if 'tests' in p.parts or p.name.startswith('test_'):
                continue
            try:
                txt = p.read_text(errors='ignore')
            except OSError:
                continue
            for m in sql_ctx.finditer(txt):
                candidates.add(m.group(1).lower())
            for m in ddl_ctx.finditer(txt):
                candidates.add(m.group(1).lower())
            for m in list_ctx.finditer(txt):
                for s in quoted.findall(m.group(1)):
                    candidates.add(s.lower())
        candidates = {c for c in candidates if c.startswith(prefixes)}
        unknown = candidates - set(EXPECTED_TABLES) - NON_TABLE_TOKENS
        self.assertEqual(unknown, set(),
                         'code references tables the bootstrap does not create')


class BootstrapTursoTests(unittest.TestCase):
    URL = 'libsql://signal-atlas-new-xyz.turso.io'
    TOKEN = 'tok-test-secret-123'

    def setUp(self):
        self.assertIsNotNone(BOOT, 'tools/bootstrap_new_db.py not implemented yet')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _env(self, url=None, token=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('TURSO_')}
        env['TURSO_DATABASE_URL'] = url or self.URL
        env['TURSO_AUTH_TOKEN'] = token or self.TOKEN
        return env

    def test_turso_batch_via_db_mjs_and_temp_env_cleanup(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            env_file = Path(kw['env']['PAPER_ENV_FILE'])
            self.assertTrue(env_file.exists(), 'temp .env must exist during run')
            content = env_file.read_text()
            self.assertIn('TURSO_DATABASE_URL=' + self.URL, content)
            self.assertIn('TURSO_AUTH_TOKEN=' + self.TOKEN, content)
            return subprocess.CompletedProcess(cmd, 0, stdout='{}\n', stderr='')

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self._env(), clear=True), \
                patch.object(BOOT.subprocess, 'run', fake_run), \
                redirect_stdout(out), redirect_stderr(err):
            rc = BOOT.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        cmd, kw = calls[0]
        self.assertEqual(cmd[0], 'node')
        self.assertTrue(str(cmd[1]).endswith('db.mjs'))
        payload = json.loads(kw['input'])
        self.assertEqual(payload['action'], 'batch')
        names = set()
        for s, _args in payload['statements']:
            m = re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_0-9]+)",
                          s, re.I)
            if m:
                names.add(m.group(1))
        self.assertEqual(names, set(EXPECTED_TABLES))
        env_file = Path(kw['env']['PAPER_ENV_FILE'])
        self.assertFalse(env_file.exists(), 'temp .env must be removed after run')
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(self.TOKEN, combined)
        self.assertNotIn(self.URL, combined)

    def test_turso_failure_reports_without_secrets_and_cleans_up(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen['env_file'] = kw['env']['PAPER_ENV_FILE']
            return subprocess.CompletedProcess(cmd, 1, stdout='',
                                             stderr='boom: connection refused')

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self._env(), clear=True), \
                patch.object(BOOT.subprocess, 'run', fake_run), \
                redirect_stdout(out), redirect_stderr(err):
            rc = BOOT.main([])
        self.assertNotEqual(rc, 0)
        self.assertFalse(Path(seen['env_file']).exists())
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(self.TOKEN, combined)
        self.assertNotIn(self.URL, combined)
        self.assertNotIn('Traceback', combined)

    def test_refuses_old_database(self):
        # The old DB must never be written to, even if its URL is supplied.
        with patch.dict(os.environ,
                        self._env(url='libsql://tradinghistory-rohithvvs-abc.turso.io'),
                        clear=True), \
                patch.object(BOOT.subprocess, 'run',
                             side_effect=AssertionError('node must not run')):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = BOOT.main([])
        self.assertEqual(rc, 2)
        self.assertIn('old database', err.getvalue())


if __name__ == '__main__':
    unittest.main()
