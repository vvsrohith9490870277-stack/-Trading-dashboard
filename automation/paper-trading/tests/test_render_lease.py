"""Tests for Task 4: runner lease, handover flag and box guard (render-lease)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import render_lease as rl
import render_state as rs
import render_trial as rt

NS = 'paper_v2'
OWNER_A = 'render:host-a:111:1700000000'
OWNER_B = 'render:host-b:222:1700000000'


def _store(tmp):
    p = Path(tmp) / 'lease.sqlite'
    s = rs.SqliteStore(p)
    rs.migrate(s)
    return s, p


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.path = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_acquire_and_renew_is_same_owner(self):
        lease = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(lease.acquire())
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_A)
        self.assertEqual(row['epoch'], 1)
        self.assertTrue(lease.renew())
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_A)
        self.assertEqual(row['epoch'], 1)  # renewing owner keeps the epoch

    def test_two_owners_one_active(self):
        a = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(a.acquire())
        b = rl.Lease(self.store, NS, OWNER_B)
        self.assertFalse(b.acquire())  # still held, unexpired
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_A)
        self.assertEqual(row['epoch'], 1)

    def test_takeover_after_expiry(self):
        a = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(a.acquire())
        # expire the lease directly in the store
        self.store.execute(
            'UPDATE paper_runner_lease SET expires_unix = expires_unix - 1000 '
            'WHERE namespace=?', (NS,))
        b = rl.Lease(self.store, NS, OWNER_B)
        self.assertTrue(b.acquire())
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_B)
        self.assertEqual(row['epoch'], 2)  # takeover increments the epoch

    def test_takeover_after_release(self):
        a = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(a.acquire())
        self.assertTrue(a.release())
        b = rl.Lease(self.store, NS, OWNER_B)
        self.assertTrue(b.acquire())
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_B)
        self.assertEqual(row['epoch'], 2)

    def test_lost_renewal_60s(self):
        """A lease whose renewal has failed for 60 s is lost to a takeover."""
        a = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(a.acquire())
        # another owner takes over after the lease expires (60 s of no renewal)
        self.store.execute(
            'UPDATE paper_runner_lease SET expires_unix = expires_unix - 1000 '
            'WHERE namespace=?', (NS,))
        b = rl.Lease(self.store, NS, OWNER_B)
        self.assertTrue(b.acquire())
        # the original owner's renewal now fails: it no longer owns the lease
        self.assertFalse(a.renew())
        row = rl.get_lease(self.store, NS)
        self.assertEqual(row['owner_id'], OWNER_B)

    def test_owner_id_format(self):
        oid = rl.make_owner_id('render')
        self.assertTrue(oid.startswith('render:'), oid)
        parts = oid.split(':')
        self.assertEqual(len(parts), 4)
        int(parts[2])  # pid
        int(parts[3])  # start epoch


class HandoffFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.path = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_handoff_flag(self):
        self.assertIsNone(rl.get_active_runner(self.store, NS))
        rl.set_active_runner(self.store, NS, 'render')
        self.assertEqual(rl.get_active_runner(self.store, NS), 'render')
        rl.set_active_runner(self.store, NS, 'box')
        self.assertEqual(rl.get_active_runner(self.store, NS), 'box')

    def test_trial_and_box_leases_coexist(self):
        """A trial namespace has its own lease row; it never blocks the box."""
        trial = rl.Lease(self.store, 'paper_render_trial_v1', OWNER_A)
        self.assertTrue(trial.acquire())
        box = rl.Lease(self.store, NS, OWNER_B)
        self.assertTrue(box.acquire())
        self.assertEqual(
            rl.get_lease(self.store, 'paper_render_trial_v1')['owner_id'], OWNER_A)
        self.assertEqual(rl.get_lease(self.store, NS)['owner_id'], OWNER_B)


class BoxGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.path = _store(self.tmp.name)
        self.old = {}
        for k in ('PAPER_TEST_DATABASE', 'PAPER_RUNTIME', 'RENDER_MODE',
                  'PAPER_RUNNER_ENFORCE', 'RENDER_OWNER_ID',
                  'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN'):
            self.old[k] = os.environ.pop(k, None)
        os.environ['PAPER_TEST_DATABASE'] = str(self.path)
        import engine as e
        self.engine = e
        self._old_ns = e.NS
        e.NS = NS

    def tearDown(self):
        self.engine.NS = self._old_ns
        self.store.close()
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_box_guard_refuses_when_render_active(self):
        rl.set_active_runner(self.store, NS, 'render')
        os.environ['PAPER_RUNNER_ENFORCE'] = '1'
        self.assertEqual(
            self.engine.runner_guard('worker'), {'status': 'runner_not_active'})

    def test_box_guard_passes_when_box_active(self):
        rl.set_active_runner(self.store, NS, 'box')
        os.environ['PAPER_RUNNER_ENFORCE'] = '1'
        self.assertIsNone(self.engine.runner_guard('worker'))

    def test_render_guard_refuses_when_box_active(self):
        rl.set_active_runner(self.store, NS, 'box')
        os.environ['PAPER_RUNTIME'] = 'render'
        os.environ['RENDER_MODE'] = 'live'
        self.assertEqual(
            self.engine.runner_guard('worker'), {'status': 'runner_not_active'})

    def test_render_guard_passes_when_render_active_and_lease_mine(self):
        rl.set_active_runner(self.store, NS, 'render')
        os.environ['PAPER_RUNTIME'] = 'render'
        os.environ['RENDER_MODE'] = 'live'
        lease = rl.Lease(self.store, NS, OWNER_A)
        self.assertTrue(lease.acquire())
        os.environ['RENDER_OWNER_ID'] = OWNER_A
        self.assertIsNone(self.engine.runner_guard('worker'))

    def test_read_only_commands_never_blocked(self):
        rl.set_active_runner(self.store, NS, 'render')
        os.environ['PAPER_RUNNER_ENFORCE'] = '1'
        for cmd in ('probe', 'init', 'reference-feeds', 'screen-only',
                    'exit-selftest', 'export-logs'):
            self.assertIsNone(self.engine.runner_guard(cmd), cmd)

    def test_guard_fail_open_for_box_when_store_unreachable(self):
        os.environ.pop('PAPER_TEST_DATABASE')
        os.environ['PAPER_RUNNER_ENFORCE'] = '1'
        self.assertIsNone(self.engine.runner_guard('worker'))

    def test_guard_fail_closed_for_render_when_store_unreachable(self):
        os.environ.pop('PAPER_TEST_DATABASE')
        os.environ['PAPER_RUNTIME'] = 'render'
        self.assertEqual(
            self.engine.runner_guard('worker'),
            {'status': 'runner_not_active',
             'reason': 'render_store_unreachable'})


class NamespaceRefusalTests(unittest.TestCase):
    def test_refuses_paper_v1_and_demo_namespace(self):
        for ns in ('paper_v1', 'paper_demo_v1'):
            with self.assertRaises(rt.NamespaceRefused, msg=ns):
                rt.resolve('live', ns, None, True)


if __name__ == '__main__':
    unittest.main()
