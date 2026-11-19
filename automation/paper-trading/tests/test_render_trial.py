"""Task 10 trial-mode rule tests (resolve table only in the Task 1 commit).

The budget/token/init/compare/purge tests land with the Task 10 wiring.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render_state as rs
import render_trial as t


class ResolveTableTests(unittest.TestCase):
    def test_resolve_table_all_combinations(self):
        # (mode, cfg_ns, override, runtime_is_render) -> expected namespace or 'REFUSE'
        cases = [
            ('live', 'paper_v2', None, False, 'paper_v2'),
            ('live', 'paper_v2', None, True, 'paper_v2'),
            ('live', 'paper_v2', 'paper_render_trial_v1', True, 'REFUSE'),
            ('live', 'paper_v2', 'paper_v2', True, 'REFUSE'),
            ('live', 'paper_render_trial_v1', None, True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_render_trial_v1', True, 'paper_render_trial_v1'),
            ('trial', 'paper_v2', None, True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_v1', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_v2', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_demo_v1', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_exit_test_v1', True, 'REFUSE'),
            ('trial', 'paper_v2', 'adaptive_v1', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_render_trial_', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_render_trial_UPPER', True, 'REFUSE'),
            ('trial', 'paper_v2', 'x', True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_render_trial_' + 'a' * 25, True, 'REFUSE'),
            ('trial', 'paper_v2', 'paper_render_trial_' + 'a' * 24, True, 'paper_render_trial_' + 'a' * 24),
            # Override ignored when the runtime is not render.
            ('trial', 'paper_v2', 'paper_render_trial_v1', False, 'paper_v2'),
            ('live', 'paper_v2', 'paper_render_trial_v1', False, 'paper_v2'),
        ]
        for mode, cfg_ns, override, is_render, expected in cases:
            with self.subTest(mode=mode, cfg_ns=cfg_ns, override=override, is_render=is_render):
                if expected == 'REFUSE':
                    with self.assertRaises(t.NamespaceRefused):
                        t.resolve(mode, cfg_ns, override, is_render)
                else:
                    self.assertEqual(t.resolve(mode, cfg_ns, override, is_render), expected)

    def test_live_refuses_trial_name_and_override(self):
        with self.assertRaises(t.NamespaceRefused):
            t.resolve('live', 'paper_render_trial_v1', None, True)
        with self.assertRaises(t.NamespaceRefused):
            t.resolve('live', 'paper_v2', 'paper_render_trial_v1', True)

    def test_trial_refuses_paper_v1_v2_demo_selftest_adaptive(self):
        for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1', 'paper_exit_test_v1', 'adaptive_v1'):
            with self.subTest(ns=ns):
                with self.assertRaises(t.NamespaceRefused):
                    t.resolve('trial', 'paper_v2', ns, True)

    def test_protected_namespaces(self):
        # The guard's data itself: a scratch edit that drops a name from
        # PROTECTED must fail here (G6 mutation check), even where the
        # trial regex would also refuse that name.
        self.assertEqual(t.PROTECTED, frozenset({
            'paper_v1', 'paper_v2', 'paper_demo_v1',
            'paper_exit_test_v1', 'adaptive_v1'}))

    def test_override_ignored_when_runtime_not_render(self):
        self.assertEqual(
            t.resolve('trial', 'paper_v2', 'paper_render_trial_v1', False), 'paper_v2')

    def test_login_allowed_only_live_flag_and_creds(self):
        base = {'FYERS_AUTO_LOGIN': '1', 'FYERS_CLIENT_ID': 'c', 'FYERS_SECRET_ID': 's',
                'FYERS_PIN': 'p', 'FYERS_TOTP_SECRET': 't'}
        self.assertTrue(t.login_allowed('live', base))
        self.assertFalse(t.login_allowed('trial', base))
        for missing in ('FYERS_CLIENT_ID', 'FYERS_SECRET_ID', 'FYERS_PIN', 'FYERS_TOTP_SECRET'):
            env = dict(base)
            del env[missing]
            self.assertFalse(t.login_allowed('live', env))
        env = dict(base, FYERS_AUTO_LOGIN='0')
        self.assertFalse(t.login_allowed('live', env))
        env = dict(base, FYERS_AUTO_LOGIN='1')
        env.pop('FYERS_AUTO_LOGIN')
        self.assertFalse(t.login_allowed('live', env))

    def test_trial_budget_caps_and_never_raised_by_env(self):
        self.assertEqual(t.trial_caps({}), {'day': 1000, 'minute': 8, 'second': 1, 'reserve': 0})
        self.assertEqual(t.trial_caps({'RENDER_TRIAL_DAY_CAP': '500'})['day'], 500)
        self.assertEqual(t.trial_caps({'RENDER_TRIAL_DAY_CAP': '5000'})['day'], 1000)
        self.assertEqual(t.trial_caps({'RENDER_TRIAL_DAY_CAP': 'abc'})['day'], 1000)


class TrialBudgetKeyTests(unittest.TestCase):
    """The trial budget lives under its own kv key, never the box key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trial.sqlite'
        self.store = rs.SqliteStore(self.path)
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_trial_budget_uses_own_key(self):
        import token_store as ts
        enc = ts.generate_key()
        store = ts.TokenStore(self.store, enc, {})
        box_state = {'day': '2026-11-19', 'requests': 7, 'by_route': {},
                     'times': []}
        store.put_budget(box_state, trial=False)
        trial_state = {'day': '2026-11-19', 'requests': 3, 'by_route': {},
                       'times': []}
        store.put_budget(trial_state, trial=True)
        self.assertEqual(store.get_budget(trial=False)['requests'], 7)
        self.assertEqual(store.get_budget(trial=True)['requests'], 3)
        rows = self.store.execute(
            "SELECT key FROM paper_runtime_kv WHERE scope='_global'")
        keys = {r['key'] for r in rows}
        self.assertIn('fyers_budget_trial', keys)
        self.assertIn('fyers_budget', keys)

    def test_engine_trial_budget_caps(self):
        """In trial mode the engine budget is capped at the trial limits."""
        import engine as e
        old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_TEST_DATABASE',
                  'PAPER_STATE_DIR'):
            old[k] = os.environ.pop(k, None)
        os.environ['PAPER_RUNTIME'] = 'render'
        os.environ['RENDER_MODE'] = 'trial'
        os.environ['PAPER_TEST_DATABASE'] = str(self.path)
        os.environ['PAPER_STATE_DIR'] = self.tmp.name
        try:
            md = e.MarketData()
            limits = md.budget.snapshot()['limits']
            self.assertEqual(limits['per_day'], 1000)
            self.assertEqual(limits['per_minute'], 8)
            self.assertEqual(limits['per_second'], 1)
            self.assertEqual(limits['reserve_for_session_monitor'], 0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TrialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trial.sqlite'
        self.store = rs.SqliteStore(self.path)
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_trial_init(self):
        out = t.init_trial(self.store, 'paper_render_trial_v1')
        self.assertEqual(out['namespace'], 'paper_render_trial_v1')
        self.assertTrue(out['ok'])
        rows = self.store.execute(
            "SELECT value FROM paper_runtime_kv WHERE scope=?"
            " AND key='trial_init'", ('paper_render_trial_v1',))
        self.assertEqual(len(rows), 1)

    def test_trial_init_refuses_non_trial(self):
        with self.assertRaises(t.NamespaceRefused):
            t.init_trial(self.store, 'paper_v2')
        with self.assertRaises(t.NamespaceRefused):
            t.init_trial(self.store, 'paper_render_trial_BAD')

    def test_trial_purge_only_trial_namespace(self):
        import render_lease as rl
        rl.set_active_runner(self.store, 'paper_v2', 'render')
        rl.set_active_runner(self.store, 'paper_render_trial_v1', 'render')
        lease = rl.Lease(self.store, 'paper_render_trial_v1', 'render:h:1:1')
        self.assertTrue(lease.acquire())
        out = t.purge_trial(self.store, 'paper_render_trial_v1')
        self.assertTrue(out['ok'])
        self.assertIsNone(rl.get_active_runner(
            self.store, 'paper_render_trial_v1'))
        self.assertIsNone(rl.get_lease(self.store, 'paper_render_trial_v1'))
        # the live namespace is untouched
        self.assertEqual(rl.get_active_runner(self.store, 'paper_v2'),
                         'render')

    def test_trial_purge_refuses_live(self):
        with self.assertRaises(t.NamespaceRefused):
            t.purge_trial(self.store, 'paper_v2')

    def test_trial_compare(self):
        class FakeDB:
            def query(self, sql, args=()):
                ns = args[0]
                if 'paper_signals' in sql:
                    return [{'n': 5 if ns == 'paper_render_trial_v1' else 4}]
                if 'paper_fills' in sql:
                    return [{'n': 2 if ns == 'paper_render_trial_v1' else 3}]
                if 'paper_positions' in sql:
                    return [{'n': 1 if ns == 'paper_render_trial_v1' else 1}]
                return [{'n': 0}]
        out = t.compare_trial(FakeDB(), 'paper_render_trial_v1', 'paper_v2',
                               '2026-11-20')
        self.assertEqual(out['trial']['signals'], 5)
        self.assertEqual(out['live']['signals'], 4)
        self.assertEqual(out['trial']['fills'], 2)
        self.assertEqual(out['live']['fills'], 3)
        self.assertEqual(out['day'], '2026-11-20')

    def test_trial_isolation_no_live_writes(self):
        """Init + trial budget writes touch only the trial scope."""
        import token_store as ts
        t.init_trial(self.store, 'paper_render_trial_v1')
        enc = ts.generate_key()
        store = ts.TokenStore(self.store, enc, {})
        store.put_budget({'day': '2026-11-19', 'requests': 1, 'by_route': {},
                          'times': []}, trial=True)
        rows = self.store.execute(
            "SELECT scope, key FROM paper_runtime_kv")
        scopes = {(r['scope'], r['key']) for r in rows}
        self.assertNotIn(('paper_v2', 'trial_init'), scopes)
        # no lease or job rows for the live namespace
        self.assertEqual(self.store.execute(
            "SELECT * FROM paper_runner_lease WHERE namespace='paper_v2'"), [])
        self.assertEqual(self.store.execute(
            "SELECT * FROM paper_job_runs WHERE namespace='paper_v2'"), [])


if __name__ == '__main__':
    unittest.main()
