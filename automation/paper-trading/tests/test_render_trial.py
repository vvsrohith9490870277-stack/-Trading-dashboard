"""Task 10 trial-mode rule tests (resolve table only in the Task 1 commit).

The budget/token/init/compare/purge tests land with the Task 10 wiring.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


if __name__ == '__main__':
    unittest.main()
