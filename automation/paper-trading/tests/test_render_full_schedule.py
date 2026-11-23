"""Tests for the full daily schedule on Render (brief 23 Nov 2026).

The scheduler runs the whole NSE day in-process: pre-flight, scan,
worker, EOD and history sync, with idempotent jobs, catch-up after a
restart, a lease guard and /health.schedule. Tests use a fake clock
(injected `now`), a fake job runner (no engine child is spawned) and
a local SqliteStore — no Fyers, no Turso, no network.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_app as ra
import render_full_schedule as rfs
import render_health as rh
import render_jobs as rj
import render_schedule as rsch
import render_state as rs

IST = ZoneInfo('Asia/Kolkata')
UTC = timezone.utc

# Friday 2026-11-20 is a trading day; Sunday 2026-11-22 is a weekend;
# Tuesday 2026-11-24 is a holiday (config.json).
FRI = datetime(2026, 11, 20, tzinfo=IST)
SUN = datetime(2026, 11, 22, tzinfo=IST)
HOL = datetime(2026, 11, 24, tzinfo=IST)


def _at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=IST)


def _config():
    return json.loads((HERE.parent / 'config.json').read_text())


class ConfigTradingDayTests(unittest.TestCase):
    def test_trading_day_from_config(self):
        cfg = _config()
        self.assertTrue(rfs.is_trading_day_config(FRI.date(), cfg))
        self.assertFalse(rfs.is_trading_day_config(SUN.date(), cfg))
        self.assertFalse(rfs.is_trading_day_config(HOL.date(), cfg))
        # Special session 2026-11-08 (Sunday) still counts.
        self.assertTrue(rfs.is_trading_day_config(
            datetime(2026, 11, 8).date(), cfg))
        # Outside the calendar range.
        self.assertFalse(rfs.is_trading_day_config(
            datetime(2027, 1, 4).date(), cfg))

    def test_next_slot(self):
        cfg = _config()
        sched = rsch.load_schedule(mode='live')
        scan = [j for j in sched['jobs'] if j['name'] == 'scan'][0]
        self.assertEqual(
            rfs.next_slot(scan, _at(FRI, 8, 0), cfg),
            _at(FRI, 8, 45))
        # After Friday's slot, the next trading day is Monday.
        self.assertEqual(
            rfs.next_slot(scan, _at(FRI, 10, 0), cfg),
            datetime(2026, 11, 23, 8, 45, tzinfo=IST))
        # A holiday has no slot: next is the day after.
        self.assertEqual(
            rfs.next_slot(scan, _at(HOL, 8, 0), cfg),
            datetime(2026, 11, 25, 8, 45, tzinfo=IST))


class JobRunsTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(Path(self.tmp.name) / 'jobs.sqlite')
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_migrate_creates_render_job_runs(self):
        names = {r['name'] for r in self.store.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('render_job_runs', names)

    def test_start_finish_and_completed(self):
        ns, day = 'paper_v2', '2026-11-20'
        self.assertFalse(rfs.job_completed_today(
            self.store, ns, 'scan', day))
        self.assertTrue(rfs.record_job_start(
            self.store, ns, 'scan', day))
        self.assertFalse(rfs.job_completed_today(
            self.store, ns, 'scan', day))
        rfs.record_job_finish(self.store, ns, 'scan', day, 'ok')
        self.assertTrue(rfs.job_completed_today(
            self.store, ns, 'scan', day))
        self.assertEqual(rfs.completed_jobs_today(self.store, ns, day),
                         {'scan'})

    def test_start_after_ok_is_refused(self):
        ns, day = 'paper_v2', '2026-11-20'
        rfs.record_job_start(self.store, ns, 'scan', day)
        rfs.record_job_finish(self.store, ns, 'scan', day, 'ok')
        # A second start for the same trade_date is refused.
        self.assertFalse(rfs.record_job_start(
            self.store, ns, 'scan', day))
        row = self.store.execute(
            'SELECT status FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            (ns, 'scan', day))[0]
        self.assertEqual(row['status'], 'ok')

    def test_completed_via_paper_job_runs(self):
        # A job that finished through the older path (paper_job_runs
        # only) still counts as completed, so a restart never re-runs
        # it.
        ns, day = 'paper_v2', '2026-11-20'
        rj.run_job(self.store, ns, 'eod', day, owner_id='render:h:1:1',
                   runner=lambda name: {'exit_code': 0})
        self.assertTrue(rfs.job_completed_today(
            self.store, ns, 'eod', day))
        self.assertEqual(rfs.completed_jobs_today(self.store, ns, day),
                         {'eod'})

    def test_skip_is_idempotent(self):
        ns, day = 'paper_v2', '2026-11-20'
        self.assertTrue(rfs.record_job_skipped(
            self.store, ns, 'scan', day, 'lease_held_by_other'))
        # A second skip does not overwrite the first.
        self.assertFalse(rfs.record_job_skipped(
            self.store, ns, 'scan', day, 'lease_held_by_other'))
        row = self.store.execute(
            'SELECT status, error_code FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            (ns, 'scan', day))[0]
        self.assertEqual((row['status'], row['error_code']),
                         ('skipped', 'lease_held_by_other'))
        # A skip is not a completion.
        self.assertFalse(rfs.job_completed_today(
            self.store, ns, 'scan', day))


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        self.sched = rsch.load_schedule(mode='live')
        self.cfg = _config()

    def test_scan_caught_up_before_1500(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 10, 0), set(), self.cfg)
        self.assertEqual([j['name'] for j in due], ['scan'])

    def test_scan_not_caught_up_after_1500(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 15, 30), set(), self.cfg)
        self.assertEqual(due, [])

    def test_scan_not_caught_up_before_slot(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 8, 0), set(), self.cfg)
        self.assertEqual(due, [])

    def test_eod_caught_up_after_1610(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 17, 0), set(), self.cfg)
        self.assertEqual([j['name'] for j in due], ['eod'])

    def test_eod_not_caught_up_before_1610(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 16, 0), set(), self.cfg)
        self.assertEqual(due, [])

    def test_no_catch_up_when_completed(self):
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 10, 0), {'scan'}, self.cfg)
        self.assertEqual(due, [])

    def test_no_catch_up_on_weekend_or_holiday(self):
        self.assertEqual(rfs.catch_up_due_jobs(
            self.sched, _at(SUN, 10, 0), set(), self.cfg), [])
        self.assertEqual(rfs.catch_up_due_jobs(
            self.sched, _at(HOL, 10, 0), set(), self.cfg), [])

    def test_only_scan_and_eod_are_caught_up(self):
        # At 18:30 the 17:30 sync_history slot has passed, but it is
        # not a catch-up job: only eod is due.
        due = rfs.catch_up_due_jobs(
            self.sched, _at(FRI, 18, 30), set(), self.cfg)
        self.assertEqual([j['name'] for j in due], ['eod'])


class _SupervisorMixin:
    """A bare Supervisor (the established test pattern) with a fake
    job runner: no engine child is spawned, no Fyers, no network."""

    def _store(self):
        store = rs.SqliteStore(Path(self.tmp.name) / 'sched.sqlite')
        rs.migrate(store)
        return store

    def _sup(self, store, namespace='paper_v2', mode='live',
             lease_held=True):
        sup = ra.Supervisor.__new__(ra.Supervisor)
        sup.store = store
        sup.namespace = namespace
        sup.mode = mode
        sup.owner_id = 'render:h:1:1'
        sup.config = _config()
        sup.lease_held = lease_held
        sup.stopping = False
        sup.store_error = None
        sup.started_at = 0
        sup.last_tick_at = None
        sup.last_lease_renew_at = None
        sup.last_flag = 'render'
        sup.breakers = {}
        sup._job_thread = None
        sup._boot_actions_done = True  # skip probe/run-once in tests
        sup._health_cache = rh.new_health_cache()
        sup.worker = None
        self.calls = []
        self.fail_jobs = set()

        def _runner(job):
            def run(name):
                self.calls.append(name)
                if name in self.fail_jobs:
                    raise RuntimeError('fake job failure')
                return {'exit_code': 0, 'stdout_tail': '',
                        'stderr_tail': '', 'peak_rss_mb': 10}
            return run

        sup._job_runner = _runner
        return sup

    def _join(self, sup, timeout=10):
        th = sup._job_thread
        if th is not None:
            th.join(timeout=timeout)
            self.assertFalse(th.is_alive())


class SchedulerTests(_SupervisorMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self._store()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_each_job_fires_once_at_its_time(self):
        sup = self._sup(self.store)
        for at, name in ((_at(FRI, 8, 15), 'preflight'),
                         (_at(FRI, 8, 45), 'scan'),
                         (_at(FRI, 16, 10), 'eod'),
                         (_at(FRI, 17, 30), 'sync_history')):
            sup._manage_jobs(now=at)
            self._join(sup)
            self.assertIn(name, self.calls, 'at %s' % at)
        # Each fired exactly once.
        for name in ('preflight', 'scan', 'eod', 'sync_history'):
            self.assertEqual(self.calls.count(name), 1, name)

    def test_no_job_on_holiday_or_weekend(self):
        sup = self._sup(self.store)
        sup._manage_jobs(now=_at(SUN, 10, 0))
        self._join(sup)
        sup._manage_jobs(now=_at(HOL, 10, 0))
        self._join(sup)
        self.assertEqual(self.calls, [])

    def test_restart_at_1000_runs_missed_scan_once(self):
        sup = self._sup(self.store)
        sup._manage_jobs(now=_at(FRI, 10, 0))
        self._join(sup)
        self.assertEqual(self.calls, ['scan'])
        # A second tick at the same time does not run the scan again
        # (record_outages, a housekeeping job due at 10:00, may run).
        sup._manage_jobs(now=_at(FRI, 10, 0))
        self._join(sup)
        self.assertEqual(self.calls.count('scan'), 1)
        row = self.store.execute(
            'SELECT status FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            ('paper_v2', 'scan', '2026-11-20'))
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]['status'], 'ok')

    def test_no_job_runs_twice_same_day(self):
        sup = self._sup(self.store)
        sup._manage_jobs(now=_at(FRI, 8, 50))
        self._join(sup)
        self.assertIn('scan', self.calls)
        sup._manage_jobs(now=_at(FRI, 8, 55))
        self._join(sup)
        self.assertEqual(self.calls.count('scan'), 1)

    def test_held_lease_blocks_writes(self):
        sup = self._sup(self.store, lease_held=False)
        sup._manage_jobs(now=_at(FRI, 8, 50))
        self._join(sup)
        self.assertEqual(self.calls, [])
        row = self.store.execute(
            'SELECT status, error_code FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            ('paper_v2', 'scan', '2026-11-20'))
        self.assertEqual(len(row), 1)
        self.assertEqual((row[0]['status'], row[0]['error_code']),
                         ('skipped', 'lease_held_by_other'))

    def test_job_exception_recorded_and_scheduler_alive(self):
        sup = self._sup(self.store)
        self.fail_jobs = {'scan'}
        # The failing job is recorded, not raised.
        sup._run_job_sync({'name': 'scan', 'command': 'scan',
                           'time': '08:45'}, now=_at(FRI, 8, 50))
        row = self.store.execute(
            'SELECT status, error_code FROM render_job_runs'
            ' WHERE namespace=? AND job=? AND trade_date=?',
            ('paper_v2', 'scan', '2026-11-20'))
        self.assertEqual((row[0]['status'], row[0]['error_code']),
                         ('error', 'RuntimeError'))
        # The scheduler still works: the next job runs.
        self.fail_jobs = set()
        sup._manage_jobs(now=_at(FRI, 16, 10))
        self._join(sup)
        self.assertIn('eod', self.calls)

    def test_worker_still_starts_after_job_exception(self):
        sup = self._sup(self.store)
        self.fail_jobs = {'scan'}
        sup._run_job_sync({'name': 'scan', 'command': 'scan',
                           'time': '08:45'}, now=_at(FRI, 8, 50))
        old = os.environ.get('PAPER_FAKE_WORKER')
        os.environ['PAPER_FAKE_WORKER'] = '1'
        try:
            sup._manage_worker()
            self.assertIsNotNone(sup.worker)
            self.assertEqual(sup.worker.poll(), 'running')
        finally:
            if old is None:
                os.environ.pop('PAPER_FAKE_WORKER', None)
            else:
                os.environ['PAPER_FAKE_WORKER'] = old
            if sup.worker is not None:
                sup.worker.stop()


class PaperV1GuardTests(unittest.TestCase):
    def test_check_namespace_refuses_paper_v1(self):
        with mock.patch.object(ra.rm, 'resolve_namespace',
                               return_value='paper_v1'):
            ok, ns = ra.check_namespace()
        self.assertFalse(ok)
        self.assertIsNone(ns)

    def test_check_namespace_accepts_paper_v2(self):
        with mock.patch.object(ra.rm, 'resolve_namespace',
                               return_value='paper_v2'):
            ok, ns = ra.check_namespace()
        self.assertTrue(ok)
        self.assertEqual(ns, 'paper_v2')


class SurveillanceTests(unittest.TestCase):
    """The pre-flight surveillance fetch feeds a fail-closed BUY gate:
    when the reference feed has not loaded for the session, buys stay
    blocked. The engine already enforces this; the scheduler's job is
    to run the pre-flight that loads it."""

    class _FakeDb:
        def __init__(self):
            self.meta = {}

        def query(self, sql, args=()):
            if 'paper_meta' in sql:
                key = (args[0], args[1])
                return [{'value': self.meta[key]}] if key in self.meta \
                    else []
            return []

        def batch(self, stmts):
            for sql, args in stmts:
                if 'paper_meta' in sql:
                    self.meta[(args[0], args[1])] = args[2]

    def setUp(self):
        import engine as e
        self.e = e
        self.old_ns = e.NS
        e.NS = 'paper_v2'
        e._SURV_CACHE.clear()

    def tearDown(self):
        self.e.NS = self.old_ns
        self.e._SURV_CACHE.clear()

    def test_missing_reference_feed_blocks_buys(self):
        db = self._FakeDb()
        ready = self.e.surveillance_ready(db, '2026-11-20')
        self.assertFalse(ready['ready'])
        self.assertEqual(ready['reason'],
                         'reference_feed_not_loaded_for_session')

    def test_loaded_reference_feed_allows_buys(self):
        db = self._FakeDb()
        self.e.record_reference_status(db, '2026-11-20', {
            'sources': {'asm': {'status': 'ok'},
                        'gsm': {'status': 'ok'},
                        'series': {'status': 'ok'}}})
        ready = self.e.surveillance_ready(db, '2026-11-20')
        self.assertTrue(ready['ready'])

    def test_one_bad_source_blocks_buys(self):
        db = self._FakeDb()
        self.e.record_reference_status(db, '2026-11-20', {
            'sources': {'asm': {'status': 'ok'},
                        'gsm': {'status': 'failed'},
                        'series': {'status': 'ok'}}})
        ready = self.e.surveillance_ready(db, '2026-11-20')
        self.assertFalse(ready['ready'])
        self.assertEqual(ready['unavailable'], ['gsm'])


class ScheduleHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rs.SqliteStore(Path(self.tmp.name) / 'health.sqlite')
        rs.migrate(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _schedule_field(self, now):
        sched = rsch.load_schedule(mode='live')
        out = rh.refresh_store_fields(
            self.store, 'paper_v2', 'live', now=now,
            schedule=sched, config=_config())
        return out['fields']['schedule']

    def test_schedule_field_shape(self):
        now = _at(FRI, 10, 0)
        sched = self._schedule_field(now)
        self.assertEqual(sched['namespace'], 'paper_v2')
        self.assertIsNone(sched['lease_holder'])
        scan = sched['jobs']['scan']
        self.assertEqual(set(scan), {'last_status', 'last_run_at',
                                    'next_run_at', 'last_error_code'})
        self.assertIsNone(scan['last_status'])
        self.assertIsNone(scan['last_run_at'])
        self.assertIsNone(scan['last_error_code'])
        # Not run yet: the next slot is today 08:45... in the past,
        # so it is the next trading day's slot.
        self.assertEqual(scan['next_run_at'],
                         '2026-11-23T08:45:00+05:30')

    def test_schedule_field_after_scan(self):
        rfs.record_job_start(self.store, 'paper_v2', 'scan', '2026-11-20')
        rfs.record_job_finish(self.store, 'paper_v2', 'scan', '2026-11-20',
                              'ok')
        sched = self._schedule_field(_at(FRI, 10, 0))
        scan = sched['jobs']['scan']
        self.assertEqual(scan['last_status'], 'ok')
        self.assertIsNotNone(scan['last_run_at'])
        self.assertIsNone(scan['last_error_code'])
        self.assertEqual(scan['next_run_at'],
                         '2026-11-23T08:45:00+05:30')

    def test_schedule_field_after_error(self):
        rfs.record_job_start(self.store, 'paper_v2', 'eod', '2026-11-20')
        rfs.record_job_finish(self.store, 'paper_v2', 'eod', '2026-11-20',
                              'error', 'RuntimeError')
        sched = self._schedule_field(_at(FRI, 17, 0))
        eod = sched['jobs']['eod']
        self.assertEqual(eod['last_status'], 'error')
        self.assertEqual(eod['last_error_code'], 'RuntimeError')

    def test_build_snapshot_carries_schedule(self):
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True, lease={'owner_id': 'render:h:1:1', 'epoch': 1},
            worker={'state': 'running', 'pid': 1, 'rss_mb': 100},
            jobs={}, uptime_seconds=60, memory_mb=150,
            schedule={'jobs': {'scan': {'last_status': 'ok',
                                        'last_run_at': None,
                                        'next_run_at': None,
                                        'last_error_code': None}},
                      'lease_holder': 'render:h:1:1',
                      'namespace': 'paper_v2'})
        self.assertEqual(snap['schedule']['lease_holder'], 'render:h:1:1')
        self.assertEqual(snap['schedule']['jobs']['scan']['last_status'],
                         'ok')

    def test_snapshot_reads_no_store(self):
        # /health is cheap: building a snapshot must not touch the
        # store, so a ping adds no Turso reads.
        sched = rsch.load_schedule(mode='live')
        rh.refresh_store_fields(self.store, 'paper_v2', 'live',
                                now=_at(FRI, 10, 0),
                                schedule=sched, config=_config())
        before = self.store.read_count()
        snap = rh.build_snapshot(
            mode='live', namespace='paper_v2', active_runner='render',
            lease_held=True, lease={'owner_id': 'render:h:1:1', 'epoch': 1},
            worker={'state': 'running', 'pid': 1, 'rss_mb': 100},
            jobs={}, uptime_seconds=60, memory_mb=150)
        code, _ = rh.health_response(snap)
        self.assertEqual(code, 200)
        self.assertEqual(self.store.read_count(), before)


class FrozenConstantTests(unittest.TestCase):
    def test_tick_and_refresh_constants(self):
        self.assertEqual(ra.SUPERVISOR_TICK_SECONDS, 60)
        old = os.environ.pop('RENDER_HEALTH_REFRESH_S', None)
        try:
            self.assertEqual(rh.health_refresh_seconds(), 300)
        finally:
            if old is not None:
                os.environ['RENDER_HEALTH_REFRESH_S'] = old


if __name__ == '__main__':
    unittest.main()
