"""Task 1 render runtime tests: namespace resolver, env creds, portability.

Tests-first: these were written before runtime_mode.py / render_trial.py existed.
"""
import ast
import importlib
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_mode as rm

ROOT = Path(__file__).resolve().parents[1]

# Modules the supervisor/engine path may read the config namespace from.
# support_agent.py and exit_selftest.py are intentionally OUT of this list:
#   - support_agent.py only uses CFG['namespace'] as a default when no namespace
#     argument is given; engine main calls sa.run_cli(e, db, ...) which passes
#     the resolved e.NS explicitly, so those defaults are never hit from the
#     supervisor path. support_agent.py is not in the brief's allowed-edit list.
#   - exit_selftest.py reads e.CFG['namespace'] as the LIVE namespace to compare
#     against (refusal check), not as the active namespace.
SCOPED_MODULES = [
    'engine.py', 'automation.py', 'audit.py', 'sessiond.py', 'guardian.py',
    'runtime_deps.py', 'reconciliation.py', 'runtime_mode.py', 'render_trial.py',
    'render_state.py', 'render_lease.py', 'render_schedule.py', 'render_jobs.py',
    'render_app.py', 'render_health.py', 'render_ctl.py', 'token_store.py',
]


class ResolverTests(unittest.TestCase):
    def test_resolver_returns_config_namespace_when_not_render(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PAPER_RUNTIME', None)
            os.environ.pop('PAPER_NAMESPACE_OVERRIDE', None)
            self.assertEqual(rm.resolve_namespace({'namespace': 'paper_v2'}), 'paper_v2')
            self.assertEqual(rm.resolve_namespace({'namespace': 'paper_v1'}), 'paper_v1')

    def test_override_ignored_and_logged_when_not_render_runtime(self):
        with mock.patch.dict(os.environ, {'PAPER_NAMESPACE_OVERRIDE': 'paper_render_trial_v1'}, clear=False):
            os.environ.pop('PAPER_RUNTIME', None)
            with self.assertLogs('render.runtime_mode', level='WARNING') as cm:
                ns = rm.resolve_namespace({'namespace': 'paper_v2'})
            self.assertEqual(ns, 'paper_v2')
            self.assertTrue(any('PAPER_NAMESPACE_OVERRIDE' in m and 'ignored' in m for m in cm.output))

    def test_every_namespace_read_goes_through_resolver(self):
        """No direct CFG['namespace'] read in the scoped engine modules.

        The single sanctioned read is inside runtime_mode.resolve_namespace
        itself; every other module must go through it.
        """
        bad = []
        pat = re.compile(r"""(?:CFG|cfg|config)\s*\[\s*['"]namespace['"]\s*\]|"""
                         r"""(?:CFG|cfg|config)\.get\(\s*['"]namespace['"]""")
        for name in SCOPED_MODULES:
            p = ROOT / name
            if not p.exists():
                continue
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if name == 'runtime_mode.py':
                    continue  # the resolver itself is the sanctioned read
                if pat.search(line):
                    bad.append(f'{name}:{i}: {line.strip()}')
        self.assertEqual(bad, [])


class EnvCredsTests(unittest.TestCase):
    def test_render_env_creds_used_when_no_env_file(self):
        import engine as e
        env = {
            'PAPER_RUNTIME': 'render',
            'FYERS_APP_ID': 'APPID-100',
            'FYERS_CLIENT_ID': 'CLIENTX',
            'FYERS_ACCESS_TOKEN': 'SEEDTOKEN',
        }
        real_exists = Path.exists

        def fake_exists(self):
            if 'sahil-fyers-turso' in str(self):
                return False
            return real_exists(self)

        with mock.patch.dict(os.environ, env, clear=False):
            # The legacy .env must never be consulted in render mode, even if
            # the path exists on disk.
            with mock.patch.object(Path, 'exists', fake_exists):
                got = e.load_env()
        self.assertEqual(got['FYERS_APP_ID'], 'APPID-100')
        self.assertEqual(got['FYERS_CLIENT_ID'], 'CLIENTX')
        self.assertEqual(got['FYERS_ACCESS_TOKEN'], 'SEEDTOKEN')

    def test_box_behaviour_unchanged_when_env_unset(self):
        """With PAPER_RUNTIME unset, paths and file reads are exactly as before."""
        import engine as e
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PAPER_RUNTIME', None)
            os.environ.pop('PAPER_STATE_DIR', None)
            self.assertFalse(rm.is_render())
            self.assertEqual(rm.state_dir(e.ROOT), e.ROOT)
            self.assertEqual(rm.state_path(e.ROOT, 'logs', 'engine.jsonl'),
                             e.ROOT / 'logs' / 'engine.jsonl')
            self.assertEqual(str(e.LEGACY), '/workspace/sahil-fyers-turso')
            # load_env on the box still goes through the legacy file: prove it
            # by source, without touching the (absent here) legacy path.
            src = Path(e.__file__).read_text()
            self.assertIn('sys.path.insert(0,str(LEGACY))', src)
            self.assertIn("f(LEGACY/'.env')", src)


class NoWorkspacePathsTests(unittest.TestCase):
    def test_no_workspace_paths_in_render_mode(self):
        """Patch Path.exists for /workspace to False and import every module.

        Render mode must not need /workspace to exist at import time.
        """
        real_exists = Path.exists

        def fake_exists(self):
            try:
                if str(self).startswith('/workspace'):
                    return False
            except Exception:
                pass
            return real_exists(self)

        env = {'PAPER_RUNTIME': 'render', 'PAPER_STATE_DIR': tempfile.mkdtemp()}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(Path, 'exists', fake_exists):
                for p in sorted(ROOT.glob('*.py')):
                    mod = p.stem
                    if mod.startswith('test_'):
                        continue
                    try:
                        importlib.import_module(mod)
                    except SystemExit as ex:
                        self.fail(f'importing {mod} in render mode raised SystemExit: {ex}')


class LogRotationTests(unittest.TestCase):
    def test_logs_rotate_at_20mb(self):
        import engine as e
        tmp = Path(tempfile.mkdtemp())
        old_ns, old_now = e.NS, e.now
        try:
            e.NS = 'test_rotate'
            with mock.patch.dict(os.environ, {'PAPER_RUNTIME': 'render', 'PAPER_STATE_DIR': str(tmp)}, clear=False):
                with mock.patch.object(e, 'LOG_ROTATE_BYTES', 200):
                    import io
                    buf = io.StringIO()
                    with mock.patch('sys.stdout', buf):
                        for _ in range(30):
                            e.log('rotate_probe', x='y' * 40)
                    out = buf.getvalue()
            logs = tmp / 'logs'
            rotated = list(logs.glob('tests.jsonl.*'))
            self.assertTrue(rotated, 'no rotated log file created')
            self.assertLess((logs / 'tests.jsonl').stat().st_size, 400)
            self.assertIn('rotate_probe', out)
        finally:
            e.NS, e.now = old_ns, old_now

    def test_spool_under_state_dir(self):
        import audit as al
        import engine as e
        tmp = Path(tempfile.mkdtemp())
        db = e.DB(str(e.ROOT / 'tests' / 'test_render_spool.sqlite'))
        try:
            with mock.patch.dict(os.environ, {'PAPER_RUNTIME': 'render', 'PAPER_STATE_DIR': str(tmp)}, clear=False):
                p = al.spool_dir(e, db)
            self.assertTrue(str(p).startswith(str(tmp)), str(p))
            self.assertIn('audit-spool', str(p))
        finally:
            Path(str(db.test_database)).unlink(missing_ok=True)


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import engine as e
        cls.e = e
        cls.db = e.DB(str(e.ROOT / 'tests' / 'test_render_preflight.sqlite'))
        e.NS = 'test_fixture'
        e.init(cls.db)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close_helper()
        except Exception:
            pass
        p = Path(str(cls.db.test_database))
        p.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(p) + suffix).unlink(missing_ok=True)

    def test_preflight_does_not_start_sessiond_in_render(self):
        import automation as au
        env = {'PAPER_RUNTIME': 'render', 'PAPER_SESSIOND_DISABLE': '1'}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.e, 'surveillance_ready', return_value={'ready': True}):
                r = au.preflight(self.e, self.db)
        self.assertEqual(r['sessiond']['status'], 'disabled')
        self.assertFalse(r['sessiond'].get('started'))
        self.assertEqual(r['status'], 'ok')

    def test_preflight_render_requires_sessiond_disabled(self):
        import automation as au
        env = {'PAPER_RUNTIME': 'render'}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop('PAPER_SESSIOND_DISABLE', None)
            with self.assertRaises(RuntimeError):
                au.preflight(self.e, self.db)


class BoxDaemonDisabledInRenderTests(unittest.TestCase):
    """Task 1.6: the box session daemon / guardian must never start on
    Render (the supervisor replaces them). They return a disabled status
    instead of opening log/lock files under e.ROOT/'logs'."""

    def test_box_daemons_disabled_in_render(self):
        import subprocess
        # db.mjs only accepts a test DB under tests/test_*.sqlite
        db = ROOT / 'tests' / 'test_render_daemon.sqlite'
        import render_state as rs
        store = rs.SqliteStore(db)
        rs.migrate(store)
        # the runner guard fails closed with no flag; set active_runner so
        # the commands reach the render-mode disable
        store.execute(
            'INSERT INTO paper_runtime_kv(scope, key, value, updated_at)'
            ' VALUES(?, ?, ?, ?)'
            ' ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value',
            ('paper_v2', 'active_runner', 'render', '2026-11-19T00:00:00'))
        store.close()
        try:
            for cmd in ('ensure-session', 'guardian', 'sessiond',
                        'ensure-sessiond'):
                with self.subTest(cmd=cmd):
                    with tempfile.TemporaryDirectory() as tmp:
                        env = dict(os.environ)
                        env['PAPER_RUNTIME'] = 'render'
                        env['RENDER_MODE'] = 'live'
                        env['PAPER_TEST_DATABASE'] = str(db)
                        env['PAPER_STATE_DIR'] = tmp
                        env['PAPER_SESSIOND_DISABLE'] = '1'
                        r = subprocess.run(
                            [sys.executable, 'engine.py', cmd],
                            capture_output=True, text=True, env=env,
                            timeout=120, cwd=str(ROOT))
                        self.assertEqual(r.returncode, 0,
                                         r.stdout + r.stderr)
                        out = json.loads(r.stdout.strip().splitlines()[-1])
                        self.assertEqual(out['status'], 'disabled', out)
                        self.assertEqual(out['reason'], 'render_mode', out)
        finally:
            db.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(db) + suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
