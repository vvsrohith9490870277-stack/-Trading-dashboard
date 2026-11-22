"""B3: candidate_shadow runner tests (live side).

Runs the shadow challengers offline on a synthetic market (make_market) with
an in-memory sidecar DB. No Turso, no network, no orders.
"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))

import candidate_shadow as cs
# adaptive_fixtures lives in automation/paper-trading/tests, but that
# directory must NOT go on sys.path: it also holds test_namespace_cutover,
# and live/tests holds a module of the same name, so inserting it breaks
# unittest discovery of live/tests (ImportError: module incorrectly
# imported from the automation copy). Load the fixtures by file path.
import importlib.util as _ilu
_FIXTURES_PATH = HERE.parent / 'automation' / 'paper-trading' / 'tests' / 'adaptive_fixtures.py'
_spec = _ilu.spec_from_file_location('adaptive_fixtures', _FIXTURES_PATH)
adaptive_fixtures = _ilu.module_from_spec(_spec)
sys.modules['adaptive_fixtures'] = adaptive_fixtures
_spec.loader.exec_module(adaptive_fixtures)
from adaptive_fixtures import fresh_db, make_market, seed_pit

# Snapshot sys.path right after this module's own imports: the regression
# test below asserts this module did not add the automation tests dir.
_SYS_PATH_AFTER_IMPORT = list(sys.path)


class SpyDB:
    def __init__(self, db):
        self.db = db
        self.sql = []

    def batch(self, stm):
        for sql, args in stm:
            self.sql.append((sql, args))
        self.db.batch(stm)

    def query(self, sql, args=()):
        return self.db.query(sql, args)


class CandidateShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        days, df, idx = make_market(n_days=280, seed=7, volume=300_000)
        cls.days, cls.df, cls.idx = days, df, idx
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db, 'shadow', days, sorted(df.symbol.unique()), series='EQ')
        cls.spy = SpyDB(db)
        cls.out = cs.run_shadow(cls.spy, 'adaptive_v1', df, idx, days[:260], days[260:])
        cls.db = db

    def test_import_does_not_pollute_sys_path(self):
        # Regression test for the G3 discovery failure: this module used to
        # insert automation/paper-trading/tests into sys.path at import time,
        # so unittest discovery of live/tests then resolved the duplicate
        # module name test_namespace_cutover to the automation copy and died
        # with ImportError ('incorrectly imported from ...'). The snapshot
        # is taken at this module's import time, so later imports by other
        # test modules cannot affect it.
        import sys
        tests_dir = str(HERE.parent / 'automation' / 'paper-trading' / 'tests')
        self.assertNotIn(tests_dir, _SYS_PATH_AFTER_IMPORT)

    def test_shadow_runs_all_six_offline(self):
        self.assertEqual(set(self.out), {101, 102, 103, 104, 105, 106})
        for s, r in self.out.items():
            self.assertEqual(r['status'], 'SHADOW_SIMULATED')
            self.assertEqual(r['mode'], 'shadow')
            self.assertTrue(r['simulated'])
            self.assertGreater(r['eval_days'], 0)

    def test_shadow_tables_only(self):
        # Every write targets adaptive_candidate_* (or the simulator's own PIT
        # prefilter cache). Nothing mentions a trading namespace.
        allowed = ('adaptive_candidate_daily', 'adaptive_candidate_trades',
                   'daily_prefilter_universe')
        for sql, args in self.spy.sql:
            tokens = [t.strip().lower() for t in sql.replace('(', ' ').split()]
            hits = [t for t in tokens if t in allowed]
            if sql.strip().upper().startswith(('INSERT', 'REPLACE', 'CREATE')):
                self.assertTrue(hits, sql)
            blob = json.dumps(args, default=str)
            for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1'):
                self.assertNotIn(ns, blob)

    def test_shadow_refuses_protected_namespace(self):
        for ns in ('paper_v1', 'paper_demo_v1', 'paper_v2'):
            with self.assertRaises(ValueError):
                cs.run_shadow(self.spy, ns, self.df, self.idx, self.days[:260], self.days[260:])

    def test_shadow_results_are_marked_shadow_and_simulated(self):
        daily = self.db.query('SELECT * FROM adaptive_candidate_daily')
        self.assertGreater(len(daily), 0)
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in daily))
        trades = self.db.query('SELECT * FROM adaptive_candidate_trades')
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in trades))

    def test_shadow_uses_same_frozen_costs(self):
        # A shadow run starts from the same sleeve capital as Track A.
        import adaptive_engine as ae
        days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db, 'shadow', days, sorted(df.symbol.unique()), series='EQ')
        import adaptive_candidates as ac
        res = ae.simulate_track_window(db, 'shadow', 102, dict(ac.CANDIDATE_STRATEGIES[102]['params']),
                                       df, idx, days[:200], days[200:],
                                       signal_evaluator=lambda s, d: ac.candidate_signals(s, d))
        self.assertEqual(res['starting_equity']['starting_equity_gross_paise'], 50_000_000)


if __name__ == '__main__':
    unittest.main()
