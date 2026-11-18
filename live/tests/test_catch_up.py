"""Task 7 tests: eod --catch-up, offline with fake Turso reads and a fake eod().

No real Turso, no real simulation: the fake ro answers paper_eod queries and
the fake eod function records the days it was asked to run.
"""
import json, sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_runner as ar


class FakeRO:
    def __init__(self, reconciled_days, unreconciled_days=()):
        self.reconciled = set(reconciled_days)
        self.unreconciled = set(unreconciled_days)

    def query(self, sql, args=()):
        day = args[-1] if args else None
        if day in self.reconciled:
            return [{'report_json': json.dumps({'ledger_reconciled': True})}]
        if day in self.unreconciled:
            return [{'report_json': json.dumps({'ledger_reconciled': False})}]
        return []


class CatchUpTests(unittest.TestCase):
    def _run(self, **kw):
        calls = []
        def fake_eod(day, no_wait=False):
            calls.append(day)
            return {'phase': 'eod', 'session_day': day, 'status': 'OK'}
        kw.setdefault('ro', FakeRO({'2026-11-13', '2026-11-17'}, {'2026-11-16'}))
        kw.setdefault('eod_fn', fake_eod)
        kw.setdefault('marked_days', set())
        kw.setdefault('namespace', 'paper_v1')
        out = ar.eod_catch_up(**kw)
        return out, calls

    def test_missed_days_run_in_order_with_middle_day_skipped(self):
        out, calls = self._run(frm='2026-11-13', to='2026-11-17')
        self.assertEqual(calls, ['2026-11-13', '2026-11-17'])
        self.assertEqual([(d['day'], d['status']) for d in out['days']],
                         [('2026-11-13', 'OK'),
                          ('2026-11-16', 'SKIPPED_NO_RECONCILED_TRACK_A_EOD'),
                          ('2026-11-17', 'OK')])

    def test_unreconciled_existing_eod_is_skipped_not_run(self):
        out, calls = self._run(frm='2026-11-16', to='2026-11-16')
        self.assertEqual(calls, [])
        self.assertEqual(out['days'][0]['status'], 'SKIPPED_NO_RECONCILED_TRACK_A_EOD')

    def test_second_invocation_is_idempotent_unless_forced(self):
        out, calls = self._run(frm='2026-11-13', to='2026-11-17',
                               marked_days={'2026-11-13', '2026-11-17'})
        self.assertEqual(calls, [])
        self.assertTrue(all(d['status'] == 'SKIPPED_ALREADY_MARKED' for d in out['days']
                            if d['day'] in ('2026-11-13', '2026-11-17')))
        out, calls = self._run(frm='2026-11-13', to='2026-11-17',
                               marked_days={'2026-11-13', '2026-11-17'}, force=True)
        self.assertEqual(calls, ['2026-11-13', '2026-11-17'])

    def test_start_defaults_to_day_after_last_marker(self):
        out, calls = self._run(to='2026-11-17', marked_days={'2026-11-12'})
        self.assertEqual(calls, ['2026-11-13', '2026-11-17'])
        self.assertEqual(out['days'][0]['day'], '2026-11-13')

    def test_parser_accepts_catch_up_and_force(self):
        a = ar.build_parser().parse_args(['eod', '--catch-up', '--from', '2026-11-13',
                                          '--session', '2026-11-17', '--force'])
        self.assertTrue(a.catch_up); self.assertTrue(a.force)
        self.assertEqual(a.frm, '2026-11-13'); self.assertEqual(a.session, '2026-11-17')


if __name__ == '__main__':
    unittest.main()
