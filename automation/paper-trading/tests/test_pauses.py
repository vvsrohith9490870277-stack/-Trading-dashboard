import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capital as cap
import pauses as pz

class PauseTests(unittest.TestCase):
    def test_drawdown_ucb_and_reinstatement_rules(self):
        self.assertIsNone(pz.breaches([1000, 900], 0.1))
        self.assertEqual(pz.breaches([1000, 899], 0.1), 1000)
        self.assertIsNone(pz.breaches([1000, 920], 0.08))
        self.assertEqual(pz.breaches([1000, 919], 0.08), 1000)
        self.assertTrue(pz.recovered(970, 1000, 0.03))
        self.assertFalse(pz.recovered(969, 1000, 0.03))
        z = cap.POLICY['ucb_one_sided_z']
        self.assertLess(pz.upper_bound([-100] * 30, z), 0)
        self.assertGreater(pz.upper_bound([100] * 30, z), 0)
        self.assertIsNone(cap.POLICY['hysteresis_shadow_trades'])
        closes = [
            {'book': 'shadow', 'strategy': 1, 'day': '2026-11-05', 'equity_paise': 50000000},
            {'book': 'shadow', 'strategy': 1, 'day': '2026-11-06', 'equity_paise': 44000000},
            {'book': 'shadow', 'strategy': 6, 'day': '2026-11-05', 'equity_paise': 50000000},
            {'book': 'shadow', 'strategy': 6, 'day': '2026-11-06', 'equity_paise': 45000000},
            {'book': 'shadow', 'strategy': 3, 'day': '2026-11-05', 'equity_paise': 50000000},
            {'book': 'shadow', 'strategy': 7, 'day': '2026-11-05', 'equity_paise': 50000000},
        ]
        losses = [{'book': 'shadow', 'strategy': 3, 'net_pnl_paise': -100, 'closed_at': f'2026-10-{i:02d}'} for i in range(1, 31)]
        losses += [{'book': 'shadow', 'strategy': 7, 'net_pnl_paise': -100, 'closed_at': f'2026-10-{i:02d}'} for i in range(1, 31)]
        losses += [{'book': 'shadow', 'strategy': 2, 'net_pnl_paise': -100, 'closed_at': f'2026-10-{i:02d}'} for i in range(1, 30)]
        writes, _ = pz.plan(closes, losses, [], [], '2026-11-09', '2026-11-09T10:00:00+05:30')
        by = {(w['scope'], w['strategy']): w for w in writes}
        self.assertEqual(by[('strategy', 1)]['cause'], 'sleeve_drawdown_exceeds_10pct')
        self.assertEqual(by[('strategy', 7)]['cause'], 'ucb_net_average_trade_below_zero')
        self.assertNotIn(('strategy', 6), by)
        self.assertNotIn(('strategy', 3), by)
        self.assertNotIn(('strategy', 2), by)
        state = {'scope': 'strategy', 'strategy': 1, 'paused': 1, 'cause': 'sleeve_drawdown_exceeds_10pct', 'baseline_day': '', 'paused_at': '2026-11-09T10:00:00', 'pre_pause_peak_paise': 50000000}
        recovered = closes + [{'book': 'shadow', 'strategy': 1, 'day': '2026-11-11', 'equity_paise': 50000000}]
        stayed, _ = pz.plan(recovered, [], [state], [], '2026-11-11', '2026-11-11T10:00:00')
        self.assertFalse(any(w['scope'] == 'strategy' and w['strategy'] == 1 for w in stayed))
        override = [{'scope': 'strategy', 'strategy': 1, 'action': 'reinstate', 'recorded_at': '2026-11-11T09:00:00'}]
        cleared, notes = pz.plan(recovered, [], [state], override, '2026-11-11', '2026-11-11T10:00:00')
        self.assertEqual(cleared[0]['paused'], 0)
        self.assertEqual(cleared[0]['baseline_day'], '2026-11-11')
        self.assertEqual(notes[0]['rule'], 'manual_override_reinstates_strategy')
        system = [
            {'book': 'capped', 'strategy': 0, 'day': '2026-11-05', 'equity_paise': 100000},
            {'book': 'capped', 'strategy': 0, 'day': '2026-11-06', 'equity_paise': 90000},
        ]
        paused, _ = pz.plan(system, [], [], [], '2026-11-09', 'now')
        self.assertEqual(paused[0]['cause'], 'system_drawdown_exceeds_8pct')
        held = {'scope': 'system', 'strategy': 0, 'paused': 1, 'paused_at': 'now', 'pre_pause_peak_paise': paused[0]['pre_pause_peak_paise'], 'baseline_day': ''}
        later = system + [{'book': 'capped', 'strategy': 0, 'day': '2026-11-12', 'equity_paise': 97000}]
        back, back_notes = pz.plan(later, [], [held], [], '2026-11-12', 'later')
        self.assertEqual(back[0]['paused'], 0)
        self.assertEqual(back[0]['peak_paise'], 97000)
        self.assertEqual(back[0]['baseline_day'], '2026-11-12')
        self.assertEqual(back_notes[0]['rule'], 'system_recovered_within_3pct_of_pre_pause_peak')


    def test_good_session_hysteresis_reinstates_after_configured_streak(self):
        self.assertIsNone(cap.POLICY['hysteresis_shadow_trades'])
        n = cap.POLICY['hysteresis_good_sessions']
        self.assertGreaterEqual(n, 1)
        state = {'scope': 'strategy', 'strategy': 1, 'paused': 1, 'cause': 'sleeve_drawdown_exceeds_10pct', 'baseline_day': '', 'paused_at': '2026-11-09T10:00:00', 'pre_pause_peak_paise': 50000000}
        pause_close = {'book': 'shadow', 'strategy': 1, 'day': '2026-11-09', 'equity_paise': 44000000}

        def shadow_days(start, count, start_equity, step):
            rows = [pause_close]
            equity = start_equity
            # dates after the pause, skipping the 10 Nov holiday by using explicit ISO days
            days = ['2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19']
            for i in range(count):
                equity = equity + step
                rows.append({'book': 'shadow', 'strategy': 1, 'day': days[i], 'equity_paise': equity})
            return rows, days[count - 1]

        short, day = shadow_days(0, n - 1, 44000000, 100000)
        stayed, _ = pz.plan(short, [], [state], [], day, day + 'T10:00:00')
        self.assertFalse(any(w['scope'] == 'strategy' and w['strategy'] == 1 and w['paused'] == 0 for w in stayed))

        done, notes = pz.plan(shadow_days(0, n, 44000000, 100000)[0], [], [state], [], shadow_days(0, n, 44000000, 100000)[1], 'later')
        self.assertEqual(done[0]['paused'], 0)
        self.assertEqual(done[0]['baseline_day'], shadow_days(0, n, 44000000, 100000)[1])
        self.assertEqual(notes[0]['rule'], f'shadow_equity_non_negative_for_{n}_sessions')

        # A down day resets the streak: n-1 up, one down, then n-1 up is still paused; n up after the down reinstates.
        days = ['2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20', '2026-11-23', '2026-11-24', '2026-11-25']
        equity = 44000000
        rows = [pause_close]
        # n-1 good, one bad
        for i in range(n - 1):
            equity += 100000
            rows.append({'book': 'shadow', 'strategy': 1, 'day': days[i], 'equity_paise': equity})
        equity -= 200000
        rows.append({'book': 'shadow', 'strategy': 1, 'day': days[n - 1], 'equity_paise': equity})
        reset, _ = pz.plan(rows, [], [state], [], days[n - 1], 'reset')
        self.assertFalse(any(w['scope'] == 'strategy' and w['strategy'] == 1 and w['paused'] == 0 for w in reset))
        for i in range(n):
            equity += 100000
            rows.append({'book': 'shadow', 'strategy': 1, 'day': days[n + i], 'equity_paise': equity})
        back, back_notes = pz.plan(rows, [], [state], [], days[n + n - 1], 'back')
        self.assertEqual(back[0]['paused'], 0)
        self.assertEqual(back_notes[0]['rule'], f'shadow_equity_non_negative_for_{n}_sessions')

if __name__ == '__main__':
    unittest.main(verbosity=2)
