"""Tests for Task 5: IST scheduler with catch-up (render_schedule / render_jobs)."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_jobs as rj
import render_schedule as rsch
import render_state as rs

IST = ZoneInfo('Asia/Kolkata')
UTC = timezone.utc

# Friday 2026-11-20 (trial day) is a trading day; Sunday 2026-11-22 is not.
FRI = datetime(2026, 11, 20, 8, 15, tzinfo=IST)
SUN = datetime(2026, 11, 22, 8, 15, tzinfo=IST)


class ScheduleTests(unittest.TestCase):
    def test_all_slots_in_schedule(self):
        sched = rsch.load_schedule()
        by_name = {j['name']: j for j in sched['jobs']}
        self.assertEqual(by_name['preflight']['time'], '08:15')
        self.assertEqual(by_name['scan']['time'], '08:45')
        self.assertEqual(by_name['eod']['time'], '16:10')
        self.assertEqual(by_name['support']['time'], '16:45')
        self.assertEqual(by_name['sync_history']['time'], '17:30')
        self.assertEqual(by_name['revalue']['time'], '18:00')
        self.assertEqual(by_name['audit_export']['time'], '18:20')
        times = by_name['record_outages']['times']
        self.assertEqual(times[0], '08:00')
        self.assertEqual(times[-1], '19:00')
        self.assertEqual(len(times), 23)  # every 30 min, 08:00-19:00
        # exit_selftest is disabled by default; adaptive is not scheduled
        names = {j['name'] for j in sched['jobs']}
        self.assertNotIn('exit_selftest', names)
        self.assertNotIn('adaptive', names)

    def test_times_are_ist(self):
        # 08:15 IST is 02:45 UTC. The same instant given in UTC must agree.
        # At 08:15 both preflight (08:15) and record_outages (08:00 slot,
        # 15 min into its catch-up window) are due.
        sched = rsch.load_schedule()
        due_ist = [j['name'] for j in rsch.due_jobs(sched, FRI)]
        due_utc = [j['name'] for j in rsch.due_jobs(
            sched, datetime(2026, 11, 20, 2, 45, tzinfo=UTC))]
        self.assertEqual(due_ist, ['preflight', 'record_outages'])
        self.assertEqual(due_utc, due_ist)

    def test_catch_up_after_restart_20_min(self):
        sched = rsch.load_schedule()
        now = datetime(2026, 11, 20, 9, 5, tzinfo=IST)  # scan due 08:45
        self.assertEqual([j['name'] for j in rsch.due_jobs(sched, now)],
                         ['scan', 'record_outages'])

    def test_catch_up_after_restart_40_min(self):
        sched = rsch.load_schedule()
        now = datetime(2026, 11, 20, 9, 25, tzinfo=IST)  # scan 40 min late
        names = [j['name'] for j in rsch.due_jobs(sched, now)]
        self.assertNotIn('scan', names)  # past the 30 min catch-up window
        self.assertIn('record_outages', names)  # 09:00 slot, 25 min late

    def test_no_missed_slot_runs_twice(self):
        sched = rsch.load_schedule()
        now = datetime(2026, 11, 20, 9, 5, tzinfo=IST)
        last = {'scan': datetime(2026, 11, 20, 8, 50, tzinfo=IST),
                'record_outages': datetime(2026, 11, 20, 9, 0, tzinfo=IST)}
        self.assertEqual(rsch.due_jobs(sched, now, last_run=last), [])

    def test_no_job_when_market_closed(self):
        sched = rsch.load_schedule()
        self.assertEqual(rsch.due_jobs(sched, SUN), [])
        self.assertFalse(rsch.is_trading_day(SUN.date()))
        self.assertTrue(rsch.is_trading_day(FRI.date()))

    def test_trial_schedule(self):
        sched = rsch.load_schedule(mode='trial')
        names = {j['name'] for j in sched['jobs']}
        self.assertIn('scan', names)
        self.assertIn('record_outages', names)
        self.assertNotIn('exit_selftest', names)
        self.assertNotIn('adaptive', names)
        # The trial never calls Fyers: no preflight (reference refresh) and
        # no sync_history (history is seeded by trial-init) in trial mode.
        self.assertNotIn('preflight', names)
        self.assertNotIn('sync_history', names)
        by_name = {j['name']: j for j in sched['jobs']}
        self.assertEqual(by_name['scan']['time'], '08:45')


class JobRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'jobs.sqlite'
        self.store = rs.SqliteStore(self.path)
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_run_job_records_ok(self):
        out = rj.run_job(self.store, 'paper_v2', 'scan', '2026-11-20',
                         owner_id='render:h:1:1',
                         runner=lambda name: {'status': 'ok', 'fills': 0})
        self.assertEqual(out, {'status': 'ok', 'fills': 0})
        rows = self.store.execute(
            "SELECT * FROM paper_job_runs WHERE namespace='paper_v2'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['job'], 'scan')
        self.assertEqual(rows[0]['status'], 'ok')
        self.assertEqual(rows[0]['attempt'], 1)

    def test_run_job_records_error(self):
        def boom(name):
            raise RuntimeError('engine exploded')
        with self.assertRaises(RuntimeError):
            rj.run_job(self.store, 'paper_v2', 'scan', '2026-11-20',
                       owner_id='render:h:1:1', runner=boom)
        rows = self.store.execute(
            "SELECT * FROM paper_job_runs WHERE namespace='paper_v2'")
        self.assertEqual(rows[0]['status'], 'error')
        self.assertIn('engine exploded', rows[0]['result_json'])

    def test_run_job_second_attempt_increments(self):
        kw = dict(owner_id='render:h:1:1',
                  runner=lambda name: {'status': 'ok'})
        rj.run_job(self.store, 'paper_v2', 'scan', '2026-11-20', **kw)
        rj.run_job(self.store, 'paper_v2', 'scan', '2026-11-20', **kw)
        rows = self.store.execute(
            "SELECT attempt, status FROM paper_job_runs"
            " WHERE namespace='paper_v2'")
        # one row per (namespace, job, session_day); the re-run bumps attempt
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempt'], 2)
        self.assertEqual(rows[0]['status'], 'ok')
        last = rj.last_run_times(self.store, 'paper_v2')
        self.assertIn('scan', last)

    def test_last_run_times_makes_job_not_due_in_window(self):
        # Regression: finished_at is stored in UTC; last_run_times used to
        # parse it as IST (5.5 h behind), so due_jobs() reported the job
        # due again and the supervisor re-ran it every tick for 30 min.
        rj.run_job(self.store, 'paper_v2', 'scan', '2026-11-20',
                   'owner', lambda job: {'status': 'ok'})
        # Pretend the run finished at the 08:45 IST slot (03:15 UTC).
        self.store.execute(
            "UPDATE paper_job_runs SET finished_at='2026-11-20T03:15:00'"
            " WHERE namespace='paper_v2' AND job='scan'")
        sched = rsch.load_schedule()
        last = rj.last_run_times(self.store, 'paper_v2')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ist = ZoneInfo('Asia/Kolkata')
        # 5 minutes after the 08:45 scan slot, still inside the 30-min
        # catch-up window: the job must NOT be due again.
        now = datetime(2026, 11, 20, 8, 50, tzinfo=ist)
        self.assertNotIn(
            'scan', [j['name'] for j in rsch.due_jobs(sched, now,
                                                      last_run=last)])


if __name__ == '__main__':
    unittest.main()
