import json, os, shutil, subprocess, sys, tempfile, unittest, uuid
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation as au
import engine as e
T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
# The interpreter running this test (it has the suite's dependencies). Never a hard-coded
# venv path: a clean checkout (e.g. the verification gate) names its venv differently.
PY = sys.executable

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
        # End to end, in a temp copy of the tree: run one real test from EVERY class that
        # creates a file-backed DB (PaperTests.setUpClass and the classes that call it,
        # the per-test DBs, and the health-test DB) and assert no tests/*.sqlite file
        # survives it and nothing lands directly under out/. One test per class is enough:
        # setUpClass creates the DB and tearDownClass must remove it.
        # The return code is NOT asserted: test_health_reports_runtime_deps fails on this
        # VM for an environment reason (runtime_deps ok False) and that failure must not
        # hide a leak — the file invariants below are the assertion. The run is only
        # trusted if all 18 tests actually ran (guards against a vacuous pass from a
        # renamed test or an import error).
        ids = [
            'test_engine.PaperTests.test_buy_duplicate_exit_and_pnl',
            'test_audit_guardian.AtomicAuditTests.test_trade_execution_event_is_atomic_and_idempotent',
            'test_automation.AutomationTests.test_independent_decimal_fee_vectors',
            'test_cycle_roundtrips.CycleRoundTripTests.test_marks_loop_skips_band_read_and_extra_positions_read_when_no_action',
            'test_db_helper.HelperTests.test_query_parity_with_per_call_path',
            'test_official_close.OfficialCloseEodTests.test_stale_marks_use_official_close_and_do_not_mutate_positions',
            'test_phase2.LateEntryFillPrice.test_entry_inside_open_window_uses_official_open',
            'test_prefilter.EntryGuardTests.test_all_seven_entries_fail_closed_without_proofs',
            'test_wake_and_bughunt.WorkerWakeTests.test_failure_stranded_by_freeze_is_retried_at_once_and_post_wake_check_logged',
            'test_wake_and_bughunt.StickySkipTests.test_adv_block_is_evaluated_once_per_session_and_exits_are_never_skipped',
            'test_worker_supervision.WorkerSupervisionTests.test_config_keys_present_and_polling_budget_above_idle',
            'test_history_sync.HistorySyncTests.test_skips_symbols_already_at_target',
            'test_runtime_deps.RuntimeDepsTests.test_health_reports_runtime_deps',
            'test_exit_selftest.ExitSelftestTests.test_uses_own_lock_not_engine_lock',
            'test_exit_slippage_fallback.FallbackNodeTests.test_sell_without_adv_fills_at_base_bps',
            'test_hardcode_audit.SellQuantityTests.test_exit_audits_full_position_quantity_not_one_share',
            'test_namespace_cutover.CutoverTests.test_repo_config_namespace_is_paper_v2',
            'test_reference_feeds.ReferenceFeedTests.test_classify_never_invents_extraordinary_threshold',
        ]
        src = Path(e.ROOT)
        tmp = Path(tempfile.mkdtemp(prefix='hygiene-copy-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        dst = tmp / 'paper-trading'
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            '.git', 'node_modules', '__pycache__', 'out', 'logs', '*.lock',
            'test_*.sqlite', 'test_*.sqlite-wal', 'test_*.sqlite-shm', 'test_*.sqlite-journal'))
        os.symlink(src / 'node_modules', dst / 'node_modules')
        env = dict(os.environ, TZ='Asia/Kolkata')
        r = subprocess.run([PY, '-m', 'unittest'] + ids,
                           cwd=str(dst / 'tests'), env=env, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        self.assertIn('Ran 18 tests', out, out[-2000:])
        left = sorted(p.name for p in (dst / 'tests').glob('test_*.sqlite*'))
        self.assertEqual(left, [], out[-2000:])
        outdir = dst / 'out'
        plain = [str(p) for p in outdir.rglob('*') if p.is_file() and 'test-exports' not in p.parts] if outdir.exists() else []
        self.assertEqual(plain, [], out[-2000:])

if __name__ == '__main__':
    unittest.main()
