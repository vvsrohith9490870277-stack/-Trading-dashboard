import fcntl, json, os, subprocess, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_deps as rd
import engine as e

FORBIDDEN_ROLLBACK = (
    'rsync --delete', 'git clean', 'rm -rf .venv', 'rm -rf node_modules',
    'rm -rf /workspace/sahil-fyers-turso', 'rm -rf /workspace/paper-trading\n',
    'rm -rf "$ROOT"', 'rm -rf /workspace/paper-trading/',
)

class RuntimeDepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'out').mkdir()
        (self.tmp / 'requirements.txt').write_text('numpy==2.5.4\n')
        (self.tmp / 'package-lock.json').write_text('{}')
        (self.tmp / 'package.json').write_text('{"private":true}')
        cfg = {
            'runtime_deps': {
                'venv_path': str(self.tmp / 'venv'),
                'requirements_path': 'requirements.txt',
                'package_lock_path': 'package-lock.json',
                'required_python_imports': ['numpy'],
                'required_node_packages': ['@libsql/client'],
            }
        }
        (self.tmp / 'config.json').write_text(json.dumps(cfg))
        self.cfg = rd.load_cfg(self.tmp)
        self.calls = []
        self.prev_stamp = rd.STAMP
        rd.STAMP = self.tmp / 'out' / 'runtime_deps_last.json'
    def tearDown(self):
        rd.STAMP = self.prev_stamp
    def fake_run(self, cmd, cwd=None, capture_output=True, text=True):
        self.calls.append((list(cmd), cwd))
        cmd0 = list(cmd)
        if cmd0[:2] == [sys.executable, '-m'] and cmd0[2] == 'venv':
            venv = Path(cmd0[3]); (venv / 'bin').mkdir(parents=True); (venv / 'bin' / 'python').write_text('#!/bin/sh\n'); (venv / 'bin' / 'python').chmod(0o755); (venv / 'bin' / 'pip').write_text('#!/bin/sh\n'); (venv / 'bin' / 'pip').chmod(0o755)
            return subprocess.CompletedProcess(cmd0, 0, '', '')
        if 'pip' in str(cmd0[0]) and 'install' in cmd0:
            return subprocess.CompletedProcess(cmd0, 0, '', '')
        if cmd0[:2] == ['npm', 'ci']:
            root = Path(cwd) if cwd else self.tmp
            pkg = root / 'node_modules' / '@libsql' / 'client'; pkg.mkdir(parents=True); (pkg / 'package.json').write_text('{"name":"@libsql/client"}')
            return subprocess.CompletedProcess(cmd0, 0, '', '')
        return subprocess.CompletedProcess(cmd0, 0, '', '')
    def test_check_reports_missing(self):
        r = rd.check(self.cfg)
        self.assertFalse(r['ok']); self.assertFalse(r['python_ok']); self.assertFalse(r['node_ok'])
    def test_ensure_restores_from_lockfiles_and_is_idempotent(self):
        # After fake venv create, python -c import will still fail (stub python).
        # Patch _python_ok after restore_python so the second check passes.
        original_ok = rd._python_ok
        state = {'restored': False}
        def py_ok(cfg):
            return (True, 'ok') if state['restored'] else original_ok(cfg)
        def run(cmd, **kw):
            out = self.fake_run(cmd, **kw)
            if cmd[:2] == [sys.executable, '-m']:
                state['restored'] = True
            return out
        prev = rd._python_ok
        rd._python_ok = py_ok
        try:
            r = rd.ensure(self.cfg, run=run)
        finally:
            rd._python_ok = prev
        self.assertTrue(r['ok']); self.assertEqual(set(r['restored']), {'venv', 'node_modules'}); self.assertTrue(r['healed'])
        # second ensure does not reinstall
        self.calls.clear(); state['restored'] = True
        rd._python_ok = lambda cfg: (True, 'ok')
        try:
            r2 = rd.ensure(self.cfg, run=self.fake_run)
        finally:
            rd._python_ok = prev
        self.assertTrue(r2['ok']); self.assertEqual(r2['restored'], []); self.assertFalse(r2['healed'])
        self.assertEqual(self.calls, [])
    def test_audit_labels(self):
        self.assertEqual(rd.report_for_audit({'healed': True, 'ok': True, 'restored': ['venv'], 'python_ok': True, 'node_ok': True})['event'], 'runtime_dependency_restored')
        self.assertEqual(rd.report_for_audit({'healed': False, 'ok': True, 'python_ok': True, 'node_ok': True})['event'], 'runtime_dependency_ok')
        self.assertEqual(rd.report_for_audit({'healed': False, 'ok': False, 'python_ok': False, 'node_ok': True})['event'], 'runtime_dependency_missing')
    def test_rollback_scripts_never_touch_runtime_dirs(self):
        found = 0
        for path in Path('/workspace').glob('paper-trading-backup-*/ROLLBACK.sh'):
            found += 1
            body='\n'.join(line for line in path.read_text().splitlines() if not line.lstrip().startswith('#'))
            for bad in FORBIDDEN_ROLLBACK:
                self.assertNotIn(bad, body, msg=f'{path} contains {bad!r}')
            self.assertNotIn('rsync', body)
            self.assertNotIn('git clean', body)
            self.assertIn('SAFETY', path.read_text())
            self.assertIn('cp -a', body)
        self.assertGreaterEqual(found, 6)


    def test_extra_node_project_restored_from_lockfile_and_skipped_when_locked(self):
        extra = self.tmp / 'adaptive-live'
        extra.mkdir(); (extra / 'state').mkdir(); (extra / 'package-lock.json').write_text('{}')
        (extra / 'package.json').write_text('{"private":true}')
        lock = extra / 'state' / 'adaptive.lock'; lock.write_text('dead 0')
        self.cfg['extra_node_projects'] = [{
            'path': str(extra),
            'package_lock_path': 'package-lock.json',
            'required_node_packages': ['@libsql/client'],
            'lock_path': 'state/adaptive.lock',
            'lock_held_reason': 'adaptive_lock_held',
        }]
        original_ok = rd._python_ok
        rd._python_ok = lambda cfg: (True, 'ok')
        # hold the flock
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.calls.clear()
            r = rd.ensure(self.cfg, run=self.fake_run)
            self.assertTrue(r['ok'])  # paper engine still ok
            extras = r['extra_projects']
            self.assertEqual(len(extras), 1)
            self.assertEqual(extras[0]['skipped'], 'adaptive_lock_held')
            self.assertFalse(extras[0]['ok'])
            self.assertFalse(any(cmd[0][:2]==['npm','ci'] and cmd[1]==str(extra) for cmd in self.calls))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
        # lock free: restore
        r2 = rd.ensure(self.cfg, run=self.fake_run)
        self.assertTrue(r2['ok'])
        self.assertTrue(r2['extra_projects'][0]['ok'])
        self.assertTrue(r2['extra_projects'][0]['restored'])
        self.assertIn(str(extra), r2['restored'])
        rd._python_ok = original_ok

    def test_health_reports_runtime_deps(self):
        db=e.DB(str(e.ROOT/'tests'/'test_runtime_health_')+os.urandom(4).hex()+'.sqlite');e.init(db)
        h=e.au.health(e,db)
        self.assertIn('runtime_deps',h)
        self.assertIn('ok',h['runtime_deps'])
        self.assertTrue(h['runtime_deps']['ok'])

if __name__=='__main__':unittest.main()
