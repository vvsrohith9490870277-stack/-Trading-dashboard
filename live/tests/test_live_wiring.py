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


if __name__ == '__main__':
    unittest.main()
