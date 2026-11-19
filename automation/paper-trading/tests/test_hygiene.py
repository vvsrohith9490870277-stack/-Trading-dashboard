import json, os, shutil, subprocess, sys, tempfile, unittest, uuid
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation as au
import engine as e
T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
VENV_PY = str(Path(__file__).resolve().parents[3] / '.venv-paper' / 'bin' / 'python')

class HygieneTests(unittest.TestCase):
    """Tests must not write into the runtime tree. The EOD prior trail belongs under
    out/test-exports/<namespace>/ for test namespaces (the audit.out_dir pattern), never
    directly under out/; and a test run must not leave test sqlite files behind.
    This class follows the rule it guards: its own DB file is removed in tearDownClass."""
    @classmethod
    def setUpClass(cls):
        cls.db = e.DB(str(e.ROOT / 'tests' / 'test_hygiene_') + uuid.uuid4().hex + '.sqlite')
        e.NS = 'test_fixture'; e.init(cls.db)
        cls.path = Path(cls.db.test_database)
    @classmethod
    def tearDownClass(cls):
        try: cls.db.close_helper()
        except Exception: pass
        cls._unlink_db(cls.path)
    @staticmethod
    def _unlink_db(path):
        path = Path(path)
        path.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)
    def setUp(self):
        e.NS = 'test_' + uuid.uuid4().hex; e.now = lambda: T
        self.ns = e.NS
        out = e.ROOT / 'out'
        self._out_before = {str(p) for p in out.rglob('*')} if out.exists() else set()
        self.addCleanup(self._remove_new_out_files)
    def _remove_new_out_files(self):
        out = e.ROOT / 'out'
        if not out.exists(): return
        for p in sorted((str(x) for x in out.rglob('*') if x.is_file()), reverse=True):
            if p not in self._out_before:
                try: os.unlink(p)
                except FileNotFoundError: pass
        for p in sorted((x for x in out.rglob('*') if x.is_dir()), reverse=True):
            if str(p) not in self._out_before:
                try: p.rmdir()
                except OSError: pass
    def _plain_out_files(self):
        # Only files this test created (not pre-existing ones in the tree).
        out = e.ROOT / 'out'
        if not out.exists(): return []
        return [p for p in out.rglob('*') if p.is_file() and 'test-exports' not in p.parts
                and str(p) not in self._out_before]
    def test_eod_prior_trail_stays_under_test_exports(self):
        # Revaluing a day whose EOD snapshot already exists preserves the prior snapshot
        # via _preserve_prior. For a test namespace that trail must land under
        # out/test-exports/<namespace>/, never directly under out/.
        self.db.batch([('INSERT INTO paper_eod(namespace,day,as_of,report_json) VALUES(?,?,?,?)',
                        (e.NS, '2026-11-09', '2026-11-09T16:10:00+05:30', '{}'))])
        e.now = lambda: datetime(2026, 11, 10, 19, 0, tzinfo=e.IST)
        fetch = lambda e_, d, syms: {'status': 'ok', 'closes': {s: 105.0 for s in syms},
                                      'source': 'fixture', 'reason': 'fixture'}
        out = au.revalue_eod(e, self.db, '2026-11-09', fetch=fetch)
        self.assertEqual(out['status'], 'written')
        trail = Path(out['previous_snapshot_path'])
        self.assertTrue(str(trail).startswith(str(e.ROOT / 'out' / 'test-exports' / self.ns)),
                        'trail %s is not under out/test-exports/%s' % (trail, self.ns))
        self.assertEqual(self._plain_out_files(), [])
    def test_eod_inline_trail_stays_under_test_exports(self):
        # _eod writes the same prior trail inline when an EOD row for today already exists.
        e.now = lambda: datetime(2026, 11, 9, 16, 30, tzinfo=e.IST)
        first = au._eod(e, self.db)
        self.assertNotIn('previous_snapshot_path', first)
        second = au._eod(e, self.db)
        trail = Path(second['previous_snapshot_path'])
        self.assertTrue(str(trail).startswith(str(e.ROOT / 'out' / 'test-exports' / self.ns)),
                        'trail %s is not under out/test-exports/%s' % (trail, self.ns))
        self.assertEqual(self._plain_out_files(), [])
    def test_no_test_sqlite_file_survives_a_test_run(self):
        # End to end, in a temp copy of the tree: run one real test and assert no
        # tests/*.sqlite file survives it (tearDownClass must remove the file-backed DB)
        # and nothing lands directly under out/.
        src = Path(e.ROOT)
        tmp = Path(tempfile.mkdtemp(prefix='hygiene-copy-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        dst = tmp / 'paper-trading'
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            '.git', 'node_modules', '__pycache__', 'out', 'logs', '*.lock',
            'test_*.sqlite', 'test_*.sqlite-wal', 'test_*.sqlite-shm', 'test_*.sqlite-journal'))
        os.symlink(src / 'node_modules', dst / 'node_modules')
        env = dict(os.environ, TZ='Asia/Kolkata')
        r = subprocess.run([VENV_PY, '-m', 'unittest', 'discover', '-s', 'tests',
                            '-p', 'test_engine.py', '-k', 'test_buy_duplicate_exit_and_pnl'],
                           cwd=str(dst), env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:] + r.stdout[-2000:])
        left = sorted(p.name for p in (dst / 'tests').glob('test_*.sqlite*'))
        self.assertEqual(left, [])
        out = dst / 'out'
        plain = [str(p) for p in out.rglob('*') if p.is_file() and 'test-exports' not in p.parts] if out.exists() else []
        self.assertEqual(plain, [])

if __name__ == '__main__':
    unittest.main()
