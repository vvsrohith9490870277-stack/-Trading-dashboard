"""Task 7 tests: eod --catch-up, offline with fake Turso reads and a fake eod().

No real Turso, no real simulation: the fake ro answers paper_eod queries and
the fake eod function records the days it was asked to run.
"""
import json, sqlite3, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
import adaptive_logging as alog
import adaptive_observation as aobs
import adaptive_runner as ar


def _make_adaptive_db(path, markers, done_days):
    """Temp adaptive DB: reconciled EOD markers for `markers`, and a
    SESSION_DONE_CAUSE decision-log row (i.e. a SIMULATED run) for `done_days`."""
    class DB:
        def __init__(self, p):
            self.c = sqlite3.connect(p); self.c.row_factory = sqlite3.Row
        def batch(self, stm):
            with self.c:
                for sql, args in stm:
                    self.c.execute(sql, args)
        def query(self, sql, args=()):
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]
    db = DB(str(path))
    alog.init(db)
    ns = ar.CFG['namespace']
    for d in markers:
        aobs.record_eod_marker(db, ns, d, True, 'turso:paper_eod/paper_v1/%s' % d, 'hash-%s' % d)
    for d in done_days:
        aobs.log_shadow_session_completed(db, ns, 1, d, {})
    db.c.close()


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
            return {'phase': 'eod', 'session_day': day, 'status': 'OK', 'simulation_status': 'SIMULATED'}
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

    def test_catch_up_stops_when_clock_enters_market_hours(self):
        # R8: fake clock is outside the window for day 1, inside it for day 2.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        IST = ZoneInfo('Asia/Kolkata')
        times = iter([datetime(2026, 11, 13, 18, 0, tzinfo=IST),
                      datetime(2026, 11, 16, 10, 0, tzinfo=IST)])
        calls = []
        def fake_eod(day, no_wait=False):
            calls.append(day)
            return {'phase': 'eod', 'session_day': day, 'status': 'OK', 'simulation_status': 'SIMULATED'}
        ro = FakeRO({'2026-11-13', '2026-11-16'})
        with self.assertRaises(RuntimeError) as ctx:
            ar.eod_catch_up(frm='2026-11-13', to='2026-11-16', ro=ro, eod_fn=fake_eod,
                            marked_days=set(), namespace='paper_v1', now_fn=lambda: next(times))
        self.assertEqual(calls, ['2026-11-13'])  # day 2 is not run
        self.assertIn('2026-11-13:OK', str(ctx.exception))  # days already done are listed
        self.assertIn('adaptive_eod_refused_in_market_hours_0900_1535_IST', str(ctx.exception))


    def test_marked_days_only_counts_simulated_days(self):
        # R7: a day with a reconciled EOD marker but no completed shadow
        # session (its simulation was BLOCKED) is NOT done.
        with tempfile.TemporaryDirectory() as t:
            dbp = Path(t) / 'adaptive.sqlite'
            _make_adaptive_db(dbp, markers={'2026-11-13', '2026-11-17'}, done_days={'2026-11-17'})
            old = ar.CFG['adaptive_db']
            ar.CFG['adaptive_db'] = str(dbp)
            try:
                self.assertEqual(ar._read_marked_days(), {'2026-11-17'})
            finally:
                ar.CFG['adaptive_db'] = old

    def test_blocked_day_is_rerun_and_simulated_day_skipped(self):
        # R7 check: day 1 has a marker but its last status is BLOCKED (no
        # done-log row), so the next catch-up re-runs it; the SIMULATED day
        # is skipped unless --force. Every days[] entry carries simulation_status.
        with tempfile.TemporaryDirectory() as t:
            dbp = Path(t) / 'adaptive.sqlite'
            _make_adaptive_db(dbp, markers={'2026-11-13', '2026-11-17'}, done_days={'2026-11-17'})
            old = ar.CFG['adaptive_db']
            ar.CFG['adaptive_db'] = str(dbp)
            try:
                out, calls = self._run(frm='2026-11-13', to='2026-11-17', marked_days=None)
            finally:
                ar.CFG['adaptive_db'] = old
            self.assertEqual(calls, ['2026-11-13'])
            self.assertEqual([(d['day'], d['status'], d['simulation_status']) for d in out['days']],
                             [('2026-11-13', 'OK', 'SIMULATED'),
                              ('2026-11-16', 'SKIPPED_NO_RECONCILED_TRACK_A_EOD', None),
                              ('2026-11-17', 'SKIPPED_ALREADY_MARKED', 'SIMULATED')])

    def test_blocked_simulation_is_not_marked_done_across_runs(self):
        # R7: eod() returning OK with a BLOCKED simulation_status must not
        # mark the day done — a later catch-up re-runs it.
        with tempfile.TemporaryDirectory() as t:
            dbp = Path(t) / 'adaptive.sqlite'
            _make_adaptive_db(dbp, markers={'2026-11-13'}, done_days=set())
            old = ar.CFG['adaptive_db']
            ar.CFG['adaptive_db'] = str(dbp)
            try:
                calls = []
                def blocked_eod(day, no_wait=False):
                    calls.append(day)
                    return {'phase': 'eod', 'session_day': day, 'status': 'OK',
                            'simulation_status': 'BLOCKED_PIT_SERIES_ARCHIVE_P1'}
                ro = FakeRO({'2026-11-13'})
                out = ar.eod_catch_up(frm='2026-11-13', to='2026-11-13', ro=ro,
                                      eod_fn=blocked_eod, marked_days=None, namespace='paper_v1')
                self.assertEqual(calls, ['2026-11-13'])
                self.assertEqual(out['days'][0]['simulation_status'], 'BLOCKED_PIT_SERIES_ARCHIVE_P1')
                calls.clear()
                out = ar.eod_catch_up(frm='2026-11-13', to='2026-11-13', ro=ro,
                                      eod_fn=blocked_eod, marked_days=None, namespace='paper_v1')
                self.assertEqual(calls, ['2026-11-13'])  # re-run: never counted as done
            finally:
                ar.CFG['adaptive_db'] = old



class CatchUpMarketHoursMainTests(unittest.TestCase):
    def test_main_writes_failed_report_when_clock_enters_market_hours(self):
        # R8 end to end: the refusal inside catch-up becomes a FAILED report
        # (exit 1) listing day 1 as done, via main()'s structured handling.
        import os
        with tempfile.TemporaryDirectory() as t:
            reports = Path(t) / 'reports'
            old_env = os.environ.get('ADAPTIVE_REPORT_DIR')
            os.environ['ADAPTIVE_REPORT_DIR'] = str(reports)
            old_turso, old_eod, old_assert = ar.TursoRO, ar.eod, ar.assert_not_market_hours
            old_ns = ar.get_track_a_namespace
            ar.get_track_a_namespace = lambda: 'paper_v1'
            calls = {'n': 0}
            def counting_assert(phase, now=None):
                calls['n'] += 1
                if calls['n'] >= 3:  # main's check, day 1's check, then day 2 refuses
                    raise RuntimeError('adaptive_eod_refused_in_market_hours_0900_1535_IST')
            eod_calls = []
            def fake_eod(day, no_wait=False):
                eod_calls.append(day)
                return {'phase': 'eod', 'session_day': day, 'status': 'OK', 'simulation_status': 'SIMULATED'}
            lock_existed = ar.LOCK_PATH.exists()
            try:
                ar.TursoRO = lambda: FakeRO({'2026-11-13', '2026-11-16'})
                ar.eod = fake_eod
                ar.assert_not_market_hours = counting_assert
                with self.assertRaises(SystemExit) as ctx:
                    ar.main(['eod', '--catch-up', '--from', '2026-11-13', '--session', '2026-11-16'])
            finally:
                ar.TursoRO, ar.eod, ar.assert_not_market_hours = old_turso, old_eod, old_assert
                ar.get_track_a_namespace = old_ns
                if old_env is None:
                    os.environ.pop('ADAPTIVE_REPORT_DIR', None)
                else:
                    os.environ['ADAPTIVE_REPORT_DIR'] = old_env
                if not lock_existed:
                    ar.LOCK_PATH.unlink(missing_ok=True)
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(eod_calls, ['2026-11-13'])
            failed = reports / '2026-11-16' / 'runner-eod-FAILED.json'
            self.assertTrue(failed.exists())
            rep = json.loads(failed.read_text())
            self.assertEqual(rep['status'], 'FAILED')
            self.assertIn('2026-11-13:OK', rep['detail'])

if __name__ == '__main__':
    unittest.main()
