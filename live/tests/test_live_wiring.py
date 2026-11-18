"""Tests for the adaptive-live sidecar wiring (offline: no Turso, no NSE, no Track A writes)."""
import io, json, os, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_daily as ad, adaptive_engine as ae, adaptive_guard as ag, adaptive_policy as apol
import bhavcopy_backfill as bb

def node_ro(sql):
    js = f"import('{HERE / 'turso_ro.mjs'}').then(m=>{{try{{m.assertReadOnly({json.dumps(sql)});console.log('ALLOWED')}}catch(e){{console.log(e.message)}}}})"
    return subprocess.run(['node', '-e', js], capture_output=True, text=True, cwd=HERE).stdout.strip()

def libsql_available():
    """True only if turso_ro.mjs can resolve @libsql/client from this machine."""
    js = f"import('{HERE / 'turso_ro.mjs'}').then(()=>console.log('OK')).catch(()=>console.log('MISSING'))"
    r = subprocess.run(['node', '-e', js], capture_output=True, text=True, cwd=HERE)
    return r.stdout.strip() == 'OK'

class ReadOnlyClient(unittest.TestCase):
    def test_select_allowed(self):
        if not libsql_available():
            self.skipTest('@libsql/client not resolvable on this machine (run npm ci in the repo root or automation/paper-trading)')
        self.assertEqual(node_ro('SELECT COUNT(*) FROM paper_fills WHERE namespace=?'), 'ALLOWED')
        self.assertEqual(node_ro("WITH x AS (SELECT 1) SELECT * FROM x"), 'ALLOWED')
    def test_writes_refused(self):
        if not libsql_available():
            self.skipTest('@libsql/client not resolvable on this machine (run npm ci in the repo root or automation/paper-trading)')
        for sql in ('DELETE FROM paper_fills', 'INSERT INTO paper_runs VALUES(1)', 'UPDATE paper_positions SET qty=0',
                    'SELECT 1; DROP TABLE paper_fills', 'PRAGMA writable_schema=1', 'ATTACH DATABASE x AS y',
                    'WITH a AS (SELECT 1) DELETE FROM paper_fills', 'REPLACE INTO paper_meta VALUES(1)'):
            self.assertNotEqual(node_ro(sql), 'ALLOWED', sql)
    def test_python_wrapper_refuses_batch(self):
        from ro_client import TursoRO
        with self.assertRaises(PermissionError): TursoRO().batch([('DELETE FROM paper_fills', ())])
    def _run(self, outcomes):
        calls = []
        def run(*a, **k):
            calls.append(1); o = outcomes[len(calls) - 1]
            if o == 'timeout': raise subprocess.TimeoutExpired('node', 1)
            return subprocess.CompletedProcess(a, 0 if o == 'ok' else 2, stdout='[[{"d":1}]]' if o == 'ok' else '', stderr=json.dumps({'code': o}) if o != 'ok' else '')
        return run, calls
    def test_transient_read_failures_are_retried(self):
        from ro_client import TursoRO
        run, calls = self._run(['turso_read_failed', 'timeout', 'ok']); slept = []
        self.assertEqual(TursoRO(run=run, sleep=slept.append).query('SELECT 1'), [{'d': 1}])
        self.assertEqual(len(calls), 3); self.assertEqual(len(slept), 2)
        self.assertEqual(slept, [3.0, 6.0])
    def test_retries_are_bounded_and_guard_refusals_not_retried(self):
        from ro_client import TursoRO
        run, calls = self._run(['turso_read_failed'] * 3)
        with self.assertRaises(RuntimeError): TursoRO(run=run, sleep=lambda s: None).query('SELECT 1')
        self.assertEqual(len(calls), 3)
        run, calls = self._run(['ro_write_refused', 'ok'])
        with self.assertRaises(RuntimeError): TursoRO(run=run, sleep=lambda s: None).query('SELECT 1')
        self.assertEqual(len(calls), 1)
    def test_batch_still_raises_permission_error(self):
        from ro_client import TursoRO
        with self.assertRaises(PermissionError): TursoRO().batch([('DELETE FROM paper_fills', ())])
    def test_broken_pipe_on_final_print_is_not_a_failure(self):
        import adaptive_runner as ar
        class BrokenPipeStdout:
            def write(self, s): raise BrokenPipeError('broken pipe')
            def flush(self): raise BrokenPipeError('broken pipe')
        with tempfile.TemporaryDirectory() as t:
            old_env = os.environ.get('ADAPTIVE_REPORT_DIR')
            os.environ['ADAPTIVE_REPORT_DIR'] = t
            old_out = sys.stdout
            sys.stdout = BrokenPipeStdout()
            try:
                with self.assertRaises(SystemExit) as ctx:
                    ar.main(['status'])
            finally:
                sys.stdout = old_out
                if old_env is None: os.environ.pop('ADAPTIVE_REPORT_DIR', None)
                else: os.environ['ADAPTIVE_REPORT_DIR'] = old_env
            self.assertEqual(ctx.exception.code, 0)
            today = __import__('datetime').date.today().isoformat()
            self.assertTrue((Path(t) / today / 'runner-status.json').exists())

class Candidates(unittest.TestCase):
    def test_parameter_only_and_capped(self):
        for s in ae.BASELINE_PARAMS:
            cands = ad.candidate_params(s)
            self.assertTrue(1 <= len(cands) <= apol.POLICY['max_track_b_challengers'])
            for p in cands:
                self.assertEqual(set(p), set(ae.BASELINE_PARAMS[s]))
                self.assertEqual(sum(p[k] != ae.BASELINE_PARAMS[s][k] for k in p), 1)
                self.assertNotEqual(p, ae.BASELINE_PARAMS[s])

class Isolation(unittest.TestCase):
    def test_namespace_not_protected(self):
        self.assertNotIn(ad.NS, ad.CFG['protected_namespaces'])
    def test_protected_namespace_refused(self):
        old = ad.NS
        try:
            ad.NS = 'paper_v1'
            with self.assertRaises(RuntimeError): ad.open_adaptive_db()
        finally:
            ad.NS = old
    def test_adaptive_db_has_no_track_a_tables(self):
        with tempfile.TemporaryDirectory() as t:
            old = ad.CFG['adaptive_db']; ad.CFG['adaptive_db'] = str(Path(t) / 'a.sqlite')
            try:
                db = ad.open_adaptive_db()
                names = {r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                ad.CFG['adaptive_db'] = old
        self.assertFalse([n for n in names if n.startswith('paper_') or n.startswith('support_')])
        self.assertIn('adaptive_spawns', names)
    def test_no_forbidden_imports_in_library(self):
        self.assertEqual(ag.forbidden_import_violations()['violations'], [])
    def test_daily_step_imports_no_db_client_or_broker(self):
        src = (HERE / 'adaptive_daily.py').read_text()
        for bad in ('ro_client', 'libsql', 'fyers', 'requests', 'import engine', 'subprocess'):
            self.assertNotIn(bad, src)
    def test_scrubbed_env_has_no_credentials(self):
        env = ag.scrubbed_env({'PATH': '/bin', 'TURSO_AUTH_TOKEN': 'x', 'FYERS_ACCESS_TOKEN': 'y', 'HOME': '/h'})
        self.assertEqual(ag.credential_like_keys(env), []); self.assertEqual(env['TZ'], 'Asia/Kolkata')
    def test_live_runtime_paths_never_written(self):
        for f in ('adaptive_daily.py', 'adaptive_runner.py', 'support_runner.py', 'bhavcopy_backfill.py', 'market_snapshot.py', 'snapshot.py'):
            src = (HERE / f).read_text()
            self.assertNotIn("'/workspace/paper-trading/", src.replace("'/workspace/paper-trading'", ''), f)

class Backfill(unittest.TestCase):
    def _zip(self, rows):
        hdr = ['TradDt', 'FinInstrmTp', 'TckrSymb', 'SctySrs', 'ISIN', 'OpnPric', 'HghPric', 'LwPric', 'ClsPric', 'TtlTradgVol', 'TtlTrfVal']
        s = io.StringIO(); s.write(','.join(hdr) + '\n')
        for r in rows: s.write(','.join(r) + '\n')
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z: z.writestr('BhavCopy.csv', s.getvalue())
        return b.getvalue()
    def test_parse_keeps_stock_series_with_priority(self):
        z = self._zip([['2026-11-11', 'STK', 'AAA', 'EQ', 'IN1', '1', '2', '1', '2', '10', '20'],
                       ['2026-11-11', 'STK', 'AAA', 'BL', 'IN1', '1', '2', '1', '2', '10', '20'],
                       ['2026-11-11', 'STK', 'BBB', 'BE', 'IN2', '1', '2', '1', '2', '10', '20'],
                       ['2026-11-11', 'IDX', 'NIFTY', 'EQ', '', '1', '2', '1', '2', '10', '20']])
        rows, raw = bb.parse_udiff(z, '2026-11-11')
        self.assertEqual(raw, 4)
        self.assertEqual(sorted((r['TckrSymb'], r['SctySrs']) for r in rows), [('AAA', 'EQ'), ('BBB', 'BE')])
    def test_date_mismatch_fails_closed(self):
        z = self._zip([['2026-11-12', 'STK', 'AAA', 'EQ', 'IN1', '1', '2', '1', '2', '10', '20']])
        with self.assertRaises(ValueError): bb.parse_udiff(z, '2026-11-11')
    def test_gate_closed_before_wed_eod(self):
        with tempfile.TemporaryDirectory() as t:
            # no reconciled EOD artifact >= not_before_session in a clean state dir -> refused
            old = bb.HERE; bb.HERE = Path(t) / 'live'; (Path(t) / 'state').mkdir()
            try:
                with self.assertRaises(RuntimeError): bb.gate_open(['2026-11-11'])
            finally:
                bb.HERE = old

    def test_compare_hyphenated_and_be_keys_match_ohlcv(self):
        # The six 2026-11-06 Turso daily_ohlcv symbols that previously missed the bhavcopy map.
        rows = [
            {'TckrSymb': 'BAJAJ-AUTO', 'SctySrs': 'EQ', 'OpnPric': '1', 'ClsPric': '1'},
            {'TckrSymb': 'NAM-INDIA', 'SctySrs': 'EQ', 'OpnPric': '2', 'ClsPric': '2'},
            {'TckrSymb': 'HFCL', 'SctySrs': 'BE', 'OpnPric': '3', 'ClsPric': '3'},
            {'TckrSymb': 'RELINFRA', 'SctySrs': 'BE', 'OpnPric': '4', 'ClsPric': '4'},
            {'TckrSymb': 'STLTECH', 'SctySrs': 'BE', 'OpnPric': '5', 'ClsPric': '5'},
            {'TckrSymb': 'TVSHLTD', 'SctySrs': 'BE', 'OpnPric': '6', 'ClsPric': '6'},
        ]
        ohlcv = {
            'BAJAJ-AUTO-EQ': {'open': 1, 'close': 1},
            'NAM-INDIA-EQ': {'open': 2, 'close': 2},
            'HFCL-BE': {'open': 3, 'close': 3},
            'RELINFRA-BE': {'open': 4, 'close': 4},
            'STLTECH-BE': {'open': 5, 'close': 5},
            'TVSHLTD-BE': {'open': 6, 'close': 6},
        }
        cmp = bb.compare_to_ohlcv(rows, '2026-11-06', ohlcv)
        self.assertEqual(cmp['compared'], 6)
        self.assertEqual(cmp['open_match'], 6)
        self.assertEqual(cmp['close_match'], 6)
        self.assertEqual(cmp['missing_in_bhav'], 0)
        self.assertEqual(cmp['missing_in_ohlcv'], 0)
        self.assertEqual(cmp['mismatches'], [])


class PortableSidecar(unittest.TestCase):
    """Task 1: no /workspace assumptions. Import must not crash when the Track A root is missing;
    TRACK_A_ROOT / ADAPTIVE_REPORT_DIR env overrides must be honoured; defaults unchanged."""
    def _run_py(self, code, env_extra=None, cwd=None):
        env = dict(os.environ)
        env.pop('TRACK_A_ROOT', None); env.pop('ADAPTIVE_REPORT_DIR', None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                              cwd=cwd or HERE, env=env)

    def test_import_with_fake_track_a_root(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, 'config.json').write_text(json.dumps({'namespace': 'paper_v1'}))
            r = self._run_py("import adaptive_runner as ar; print(ar.get_track_a_namespace())",
                             env_extra={'TRACK_A_ROOT': t})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), 'paper_v1')

    def test_import_with_missing_track_a_root_and_preflight_error(self):
        missing = '/nonexistent-track-a-root-xyz'
        r = self._run_py("import adaptive_runner as ar; print('imported', ar.TRACK_A)",
                         env_extra={'TRACK_A_ROOT': missing})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('imported', r.stdout)
        r = self._run_py("import adaptive_runner as ar; ar.preflight()",
                         env_extra={'TRACK_A_ROOT': missing})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('track_a_root_not_found', r.stderr)

    def test_report_dir_env_override_and_default(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'reports'
            r = self._run_py(
                "import adaptive_runner as ar; print(ar.report_dir())",
                env_extra={'ADAPTIVE_REPORT_DIR': str(out)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), str(out))
            r = self._run_py("import adaptive_runner as ar; print(ar.report_dir())")
            self.assertEqual(r.stdout.strip(), '/workspace/reports/adaptive')
            # A real `status` run honours the override (writes runner-status.json under it).
            r = self._run_py("import adaptive_runner as ar; ar.main(['status'])",
                             env_extra={'ADAPTIVE_REPORT_DIR': str(out)})
            self.assertEqual(r.returncode, 0, r.stderr)
            today = __import__('datetime').date.today().isoformat()
            self.assertTrue((out / today / 'runner-status.json').exists())

    def test_adaptive_daily_report_dir_default_uses_env(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'reports'
            r = self._run_py(
                "import adaptive_daily as ad; print(ad.report_dir_default())",
                env_extra={'ADAPTIVE_REPORT_DIR': str(out)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), str(out))


class StructuredFailureReports(unittest.TestCase):
    """Task 3: any phase failure writes runner-<phase>-FAILED.json with a short
    error_code and a non-secret detail; a clean run writes no FAILED file."""
    def _track_a_root(self, t, alter=False):
        root = Path(t) / 'track-a'
        root.mkdir()
        lib = HERE.parent / 'automation' / 'paper-trading'
        (root / 'config.json').write_text((lib / 'config.json').read_text())
        for name in ('capital_policy.json', 'charge_policy.json', 'charge_rates.json'):
            (root / name).write_text((lib / name).read_text())
        if alter:
            p = root / 'capital_policy.json'
            data = json.loads(p.read_text()); data['master_capital_inr'] = int(data['master_capital_inr']) + 1
            p.write_text(json.dumps(data))
        return root

    def _run_main(self, args, env_extra):
        env = dict(os.environ)
        env.pop('TRACK_A_ROOT', None); env.pop('ADAPTIVE_REPORT_DIR', None)
        env.update(env_extra)
        return subprocess.run([sys.executable, str(HERE / 'adaptive_runner.py')] + args,
                              capture_output=True, text=True, cwd=HERE, env=env)

    def test_preflight_frozen_mismatch_writes_failed_report(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._track_a_root(t, alter=True)
            reports = Path(t) / 'reports'
            secret = 's3cr3t-marker-xyz-123'
            r = self._run_main(['preflight'], {'TRACK_A_ROOT': str(root),
                                               'ADAPTIVE_REPORT_DIR': str(reports),
                                               'ADAPTIVE_TEST_SECRET': secret})
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            today = __import__('datetime').date.today().isoformat()
            failed = reports / today / 'runner-preflight-FAILED.json'
            self.assertTrue(failed.exists(), r.stdout + r.stderr)
            rep = json.loads(failed.read_text())
            self.assertEqual(rep['phase'], 'preflight')
            self.assertEqual(rep['status'], 'FAILED')
            self.assertEqual(rep['error_code'], 'frozen_file_hash_mismatch')
            self.assertFalse(rep['ok'])
            self.assertIn('capital_policy.json', rep['detail'])
            self.assertIn('installed', rep['detail'])
            self.assertIn('expected=', rep['detail'])
            self.assertIn('actual=', rep['detail'])
            self.assertNotIn(secret, failed.read_text())
            self.assertNotIn('traceback', failed.read_text().lower())

    def test_clean_status_run_writes_normal_report_and_no_failed(self):
        with tempfile.TemporaryDirectory() as t:
            reports = Path(t) / 'reports'
            r = self._run_main(['status'], {'ADAPTIVE_REPORT_DIR': str(reports)})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            today = __import__('datetime').date.today().isoformat()
            self.assertTrue((reports / today / 'runner-status.json').exists())
            self.assertFalse((reports / today / 'runner-status-FAILED.json').exists())


class MarketHoursAndLockRefusal(unittest.TestCase):
    """R5: a market-hours refusal and a held sidecar lock must write
    runner-eod-FAILED.json and exit 1 — never raise uncaught with no report."""
    def _run_eod_main(self, reports):
        import adaptive_runner as ar
        old_env = os.environ.get('ADAPTIVE_REPORT_DIR')
        os.environ['ADAPTIVE_REPORT_DIR'] = str(reports)
        try:
            with self.assertRaises(SystemExit) as ctx:
                ar.main(['eod', '--session', '2026-11-11', '--no-wait'])
        finally:
            if old_env is None:
                os.environ.pop('ADAPTIVE_REPORT_DIR', None)
            else:
                os.environ['ADAPTIVE_REPORT_DIR'] = old_env
        return ctx.exception.code

    def test_market_hours_refusal_writes_failed_report(self):
        import adaptive_runner as ar
        with tempfile.TemporaryDirectory() as t:
            reports = Path(t) / 'reports'
            old = ar.in_market_quiet_window
            ar.in_market_quiet_window = lambda now=None: True
            try:
                code = self._run_eod_main(reports)
            finally:
                ar.in_market_quiet_window = old
            self.assertEqual(code, 1)
            failed = reports / '2026-11-11' / 'runner-eod-FAILED.json'
            self.assertTrue(failed.exists())
            rep = json.loads(failed.read_text())
            self.assertEqual(rep['status'], 'FAILED')
            self.assertEqual(rep['error_code'], 'adaptive_eod_refused_in_market_hours_0900_1535_IST')
            self.assertFalse(rep['ok'])

    def test_lock_held_writes_failed_report(self):
        import adaptive_runner as ar
        import fcntl
        with tempfile.TemporaryDirectory() as t:
            reports = Path(t) / 'reports'
            old = ar.in_market_quiet_window
            ar.in_market_quiet_window = lambda now=None: False
            lock_existed = ar.LOCK_PATH.exists()
            try:
                ar.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(ar.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    code = self._run_eod_main(reports)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
            finally:
                ar.in_market_quiet_window = old
                if not lock_existed:
                    ar.LOCK_PATH.unlink(missing_ok=True)
            self.assertEqual(code, 1)
            failed = reports / '2026-11-11' / 'runner-eod-FAILED.json'
            self.assertTrue(failed.exists())
            rep = json.loads(failed.read_text())
            self.assertEqual(rep['status'], 'FAILED')
            self.assertEqual(rep['error_code'], 'adaptive_sidecar_already_running')
            self.assertFalse(rep['ok'])


class EodReportDirPassthrough(unittest.TestCase):
    """R6: eod() must launch adaptive_daily.py with --report-dir <override>,
    so the child writes to ADAPTIVE_REPORT_DIR, not to /workspace."""
    def test_daily_child_gets_report_dir_override(self):
        import adaptive_runner as ar
        import types
        from unittest import mock
        day = '2026-11-11'
        with tempfile.TemporaryDirectory() as t:
            reports = Path(t) / 'reports'
            state = Path(t) / 'state'
            old_state, old_env = ar.STATE, os.environ.get('ADAPTIVE_REPORT_DIR')
            os.environ['ADAPTIVE_REPORT_DIR'] = str(reports)
            ar.STATE = state
            stub = types.ModuleType('market_snapshot')
            stub.refresh = lambda ro: {'stubbed': True}
            old_ms = sys.modules.get('market_snapshot')
            sys.modules['market_snapshot'] = stub
            old_runnable = ar.ag.assert_runnable
            ar.ag.assert_runnable = lambda **kw: True
            old_counts = ar.protected_counts
            ar.protected_counts = lambda ro: {}
            old_ns = ar.get_track_a_namespace
            ar.get_track_a_namespace = lambda: 'paper_v1'
            calls = []
            def fake_run(argv, **kw):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout='{"session_day": "%s"}\n' % day, stderr='')
            class FakeRO:
                def query(self, sql, args=()):
                    if 'paper_eod' in sql:
                        return [{'report_json': json.dumps({'ledger_reconciled': True})}]
                    return []
                def many(self, qs):
                    return [[{'n': 0}] for _ in qs]
            try:
                with mock.patch('subprocess.run', side_effect=fake_run):
                    out = ar.eod(day, no_wait=True, ro=FakeRO())
            finally:
                ar.STATE = old_state
                ar.ag.assert_runnable = old_runnable
                ar.protected_counts = old_counts
                ar.get_track_a_namespace = old_ns
                if old_ms is None:
                    sys.modules.pop('market_snapshot', None)
                else:
                    sys.modules['market_snapshot'] = old_ms
                if old_env is None:
                    os.environ.pop('ADAPTIVE_REPORT_DIR', None)
                else:
                    os.environ['ADAPTIVE_REPORT_DIR'] = old_env
            self.assertEqual(out['status'], 'OK')
            self.assertEqual(len(calls), 1)
            argv = calls[0]
            self.assertIn('--report-dir', argv)
            self.assertEqual(argv[argv.index('--report-dir') + 1], str(reports))
            self.assertFalse((Path('/workspace/reports/adaptive') / day).exists(),
                             'nothing may be written under /workspace')


class BackfillRetryPassthrough(unittest.TestCase):
    """R9: run_adaptive.sh backfill can pass --retry-missing through the runner."""
    def test_parser_accepts_retry_missing(self):
        import adaptive_runner as ar
        a = ar.build_parser().parse_args(['backfill', '--from', '2026-07-23', '--to', '2026-07-24',
                                          '--retry-missing'])
        self.assertTrue(a.retry_missing)

    def test_backfill_passes_retry_missing_to_child(self):
        import adaptive_runner as ar
        from unittest import mock
        calls = []
        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout='{"days_requested": 0}\n', stderr='')
        with mock.patch('subprocess.run', side_effect=fake_run):
            out = ar.backfill('2026-07-23', '2026-07-24', retry_missing=True)
        self.assertEqual(len(calls), 1)
        self.assertIn('--retry-missing', calls[0])
        self.assertEqual(out['status'], 'OK')
        with mock.patch('subprocess.run', side_effect=fake_run):
            calls.clear()
            ar.backfill('2026-07-23', '2026-07-24')
        self.assertNotIn('--retry-missing', calls[0])


if __name__ == '__main__':
    unittest.main()
