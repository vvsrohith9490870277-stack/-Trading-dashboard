"""Task 10 trial-mode rule tests (resolve table only in the Task 1 commit).

The budget/token/init/compare/purge tests land with the Task 10 wiring.
"""
import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render_ctl as ctl
import render_state as rs
import render_trial as t

BAR = '2026-11-18'
TRIAL_NS = 'paper_render_trial_v1'
LIVE_NS = 'paper_v2'


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
        # The real path: baseline the trial at BAR on a paper_v2-baselined
        # DB, then the marker row and the watermark match are both present.
        store, db, path = self._baselined_db()
        try:
            out = t.init_trial(store, db, TRIAL_NS, BAR)
            self.assertEqual(out['namespace'], TRIAL_NS)
            self.assertTrue(out['bootstrapped'])
            self.assertTrue(out['watermarks_match_paper_v2'])
            rows = store.execute(
                "SELECT value FROM paper_runtime_kv WHERE scope=?"
                " AND key='trial_init'", (TRIAL_NS,))
            self.assertEqual(len(rows), 1)
        finally:
            ctl.close_baselined_db(store, path)

    def _baselined_db(self):
        """A local SQLite DB with a paper_v2 baseline at BAR.

        The file must live under tests/ as test_*.sqlite (db.mjs rejects
        any other test-database path)."""
        fd, raw = tempfile.mkstemp(dir=Path(__file__).resolve().parent,
                                   prefix='test_trial_init_',
                                   suffix='.sqlite')
        os.close(fd)
        path = Path(raw)
        return (*ctl.build_baselined_db(path, BAR, self.tmp.name), path)

    def test_trial_init_refuses_non_trial(self):
        # Refusal happens before any DB access: db=None must still raise
        # NamespaceRefused, never an AttributeError from touching the DB.
        with self.assertRaises(t.NamespaceRefused):
            t.init_trial(self.store, None, 'paper_v2', BAR)
        with self.assertRaises(t.NamespaceRefused):
            t.init_trial(self.store, None, 'paper_render_trial_BAD', BAR)

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
        # Realistic rows per query shape (the report reads detail rows,
        # not just counts).
        live_sig = [{'strategy': 1, 'symbol': '360ONE-EQ'},
                    {'strategy': 2, 'symbol': 'ABB-EQ'},
                    {'strategy': 1, 'symbol': 'ACC-EQ'}]
        trial_sig = [{'strategy': 1, 'symbol': '360ONE-EQ'},
                     {'strategy': 2, 'symbol': 'ABB-EQ'},
                     {'strategy': 1, 'symbol': 'ADANIENT-EQ'}]
        live_fill = [{'signal_id': 'fix-sig-1', 'strategy': 1,
                      'symbol': '360ONE-EQ', 'side': 'BUY', 'qty': 10,
                      'price': 100.5, 'filled_at': '2026-11-20T09:30:00'},
                     {'signal_id': 'fix-sig-3', 'strategy': 1,
                      'symbol': 'ACC-EQ', 'side': 'SELL', 'qty': 5,
                      'price': 200.0, 'filled_at': '2026-11-20T10:00:00'}]
        trial_fill = [{'signal_id': 'fix-sig-1', 'strategy': 1,
                       'symbol': '360ONE-EQ', 'side': 'BUY', 'qty': 10,
                       'price': 101.0, 'filled_at': '2026-11-20T09:31:00'},
                      {'signal_id': 'fix-sig-4', 'strategy': 1,
                       'symbol': 'ADANIENT-EQ', 'side': 'BUY', 'qty': 5,
                       'price': 50.0, 'filled_at': '2026-11-20T09:32:00'}]

        class FakeDB:
            def query(self, sql, args=()):
                ns = args[0]
                trial = ns == 'paper_render_trial_v1'
                if 'MAX(bar_date)' in sql:
                    return [{'bar': '2026-11-19'}]
                if 'paper_runs' in sql:
                    ats = ['2026-11-20T09:15:00', '2026-11-20T09:15:30',
                           '2026-11-20T09:17:30'] if trial else \
                          ['2026-11-20T09:15:00', '2026-11-20T09:15:30']
                    return [{'at': a} for a in ats]
                if 'COUNT(*)' in sql:
                    if 'paper_signals' in sql:
                        return [{'n': 5 if trial else 4}]
                    if 'paper_fills' in sql:
                        return [{'n': 2 if trial else 3}]
                    if 'paper_positions' in sql:
                        return [{'n': 1}]
                    if 'paper_eod' in sql:
                        return [{'n': 0 if trial else 1}]
                    if 'paper_events' in sql:
                        return [{'n': 1 if trial else 2}]
                    return [{'n': 0}]
                if 'paper_signals' in sql:
                    return trial_sig if trial else live_sig
                if 'paper_fills' in sql:
                    return trial_fill if trial else live_fill
                if 'paper_positions' in sql:
                    return [{'strategy': 1, 'symbol': '360ONE-EQ',
                             'qty': 10, 'entry_price': 100.5,
                             'entry_at': '2026-11-20T09:30:00'}] if not trial \
                        else [{'strategy': 1, 'symbol': '360ONE-EQ',
                              'qty': 10, 'entry_price': 101.0,
                              'entry_at': '2026-11-20T09:31:00'},
                             {'strategy': 1, 'symbol': 'ADANIENT-EQ',
                              'qty': 5, 'entry_price': 50.0,
                              'entry_at': '2026-11-20T09:32:00'}]
                return []
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
        store, db, path = self._baselined_db()
        try:
            t.init_trial(store, db, TRIAL_NS, BAR)
            enc = ts.generate_key()
            tstore = ts.TokenStore(store, enc, {})
            tstore.put_budget({'day': '2026-11-19', 'requests': 1,
                               'by_route': {}, 'times': []}, trial=True)
            rows = store.execute(
                "SELECT scope, key FROM paper_runtime_kv")
            scopes = {(r['scope'], r['key']) for r in rows}
            self.assertNotIn((LIVE_NS, 'trial_init'), scopes)
            # no lease or job rows for the live namespace
            self.assertEqual(store.execute(
                "SELECT * FROM paper_runner_lease WHERE namespace='paper_v2'"), [])
            self.assertEqual(store.execute(
                "SELECT * FROM paper_job_runs WHERE namespace='paper_v2'"), [])
        finally:
            ctl.close_baselined_db(store, path)


class CompareReportTests(unittest.TestCase):
    """Task 4 (render-deploy-02): trial-compare is a full report.

    The fixture DB (baseline=False) already seeds live-side rows for
    session 2026-11-20; each test adds the trial-side rows it needs.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE',
                  'PAPER_NAMESPACE_OVERRIDE', 'PAPER_TEST_DATABASE',
                  'PAPER_STATE_DIR', 'TURSO_DATABASE_URL',
                  'TURSO_AUTH_TOKEN'):
            self.old[k] = os.environ.pop(k, None)
        os.environ['PAPER_RUNTIME'] = 'render'
        os.environ['RENDER_MODE'] = 'trial'
        os.environ['PAPER_NAMESPACE_OVERRIDE'] = TRIAL_NS
        os.environ['PAPER_STATE_DIR'] = self.tmp.name
        self.store, self.db, self.path = ctl._fixture_db(baseline=False)

    def tearDown(self):
        ctl._close_fixture(self.store, self.db, self.path)
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_trial_rows(self):
        """Trial-side rows mirroring the fixture's live rows, with one
        extra symbol and one fill at a different price."""
        self.db.batch([
            ("INSERT INTO paper_signals(namespace, id, strategy, symbol,"
             " bar_date, side, status, reason, created_at, eligible_date)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 'fix-sig-1', 1, '360ONE-EQ', '2026-11-19', 'BUY',
              'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20')),
            ("INSERT INTO paper_signals(namespace, id, strategy, symbol,"
             " bar_date, side, status, reason, created_at, eligible_date)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 'fix-sig-2', 2, 'ABB-EQ', '2026-11-19', 'BUY',
              'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20')),
            ("INSERT INTO paper_signals(namespace, id, strategy, symbol,"
             " bar_date, side, status, reason, created_at, eligible_date)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 'fix-sig-4', 1, 'ADANIENT-EQ', '2026-11-19', 'BUY',
              'pending', 'fixture', '2026-11-20T08:45:00', '2026-11-20')),
            ("INSERT INTO paper_fills(namespace, signal_id, strategy,"
             " symbol, side, qty, price, quote_at, filled_at, reason,"
             " realized_pnl, cashflow, execution_path)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 'fix-sig-1', 1, '360ONE-EQ', 'BUY', 10, 101.0,
              '2026-11-20T09:31:00', '2026-11-20T09:31:00', 'fixture', 0,
              -1010.0, 'LTP_LEGACY')),
            ("INSERT INTO paper_fills(namespace, signal_id, strategy,"
             " symbol, side, qty, price, quote_at, filled_at, reason,"
             " realized_pnl, cashflow, execution_path)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 'fix-sig-4', 1, 'ADANIENT-EQ', 'BUY', 5, 50.0,
              '2026-11-20T09:32:00', '2026-11-20T09:32:00', 'fixture', 0,
              -250.0, 'LTP_LEGACY')),
            ("INSERT INTO paper_positions(namespace, strategy, symbol,"
             " qty, entry_price, entry_at, high_water, updated_at)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 1, '360ONE-EQ', 10, 101.0, '2026-11-20T09:31:00',
              101.5, '2026-11-20T09:31:00')),
            ("INSERT INTO paper_positions(namespace, strategy, symbol,"
             " qty, entry_price, entry_at, high_water, updated_at)"
             " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, 1, 'ADANIENT-EQ', 5, 50.0, '2026-11-20T09:32:00',
              50.5, '2026-11-20T09:32:00')),
            ("INSERT INTO paper_scan_batches(namespace, bar_date,"
             " scanned_at, status, report_json) VALUES(?, ?, ?, ?, ?)",
             (TRIAL_NS, '2026-11-19', '2026-11-20T08:45:00', 'ok', '{}')),
            ("INSERT INTO paper_events(namespace, event_id, run_id,"
             " session_day, source_at, recorded_at, event_type, severity,"
             " outcome, data_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (TRIAL_NS, '%032x' % 9, 'fixrun', '2026-11-20',
              '2026-11-20T12:00:00', '2026-11-20T12:00:00',
              'fixture_error', 'ERROR', 'failed', '{}')),
        ])

    def test_compare_symbol_sets_and_fill_bps(self):
        self._add_trial_rows()
        out = t.compare_trial(self.db, TRIAL_NS, LIVE_NS, '2026-11-20')
        # counts are kept (signals by eligible_date = the session day)
        self.assertEqual(out['trial']['signals'], 3)
        self.assertEqual(out['live']['signals'], 3)
        self.assertEqual(out['trial']['fills'], 2)
        self.assertEqual(out['live']['fills'], 2)
        self.assertEqual(out['trial']['positions'], 2)
        self.assertEqual(out['live']['positions'], 1)
        by_strat = out['signals_by_strategy']
        self.assertEqual(by_strat['1'], {'live': 2, 'trial': 2,
                                         'only_box': ['ACC-EQ'],
                                         'only_trial': ['ADANIENT-EQ'],
                                         'both': ['360ONE-EQ']})
        self.assertEqual(by_strat['2'], {'live': 1, 'trial': 1,
                                         'only_box': [], 'only_trial': [],
                                         'both': ['ABB-EQ']})
        fills = out['fills_by_strategy']['1']
        self.assertEqual(len(fills['live']), 2)
        self.assertEqual(len(fills['trial']), 2)
        self.assertEqual(fills['matched'], [{
            'signal_id': 'fix-sig-1', 'symbol': '360ONE-EQ',
            'side': 'BUY', 'live_qty': 10, 'trial_qty': 10,
            'live_price': 100.5, 'trial_price': 101.0,
            'price_diff_bps': 49.75}])
        self.assertEqual(out['fills_by_strategy']['2']['matched'], [])
        self.assertEqual(len(out['open_positions']['live']), 1)
        self.assertEqual(len(out['open_positions']['trial']), 2)
        self.assertEqual(out['open_positions']['trial'][1]['symbol'],
                         'ADANIENT-EQ')
        self.assertEqual(out['scan_batches'],
                         {'live': {'present': True,
                                   'bar_date': '2026-11-19'},
                          'trial': {'present': True,
                                    'bar_date': '2026-11-19'}})
        self.assertEqual(out['paper_eod'],
                         {'live': True, 'trial': False})
        self.assertEqual(out['errors'], {'live': 2, 'trial': 1})
        self.assertEqual(out['heartbeat_gaps'], [{
            'from': '2026-11-20T09:15:30', 'to': '2026-11-20T09:17:30',
            'gap_seconds': 120}])

    def test_compare_read_only(self):
        self._add_trial_rows()
        writes = []
        orig_query, orig_batch = self.db.query, self.db.batch

        def _spy_query(sql, args=()):
            for word in ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER'):
                if word in sql.upper():
                    writes.append(sql)
            return orig_query(sql, args)

        def _spy_batch(items):
            writes.append('batch: %d items' % len(items))
            return orig_batch(items)
        self.db.query, self.db.batch = _spy_query, _spy_batch
        try:
            before = {table: orig_query(
                'SELECT COUNT(*) AS n FROM %s' % table)[0]['n']
                for table in ('paper_signals', 'paper_fills',
                              'paper_positions', 'paper_scan_batches',
                              'paper_eod', 'paper_events', 'paper_runs')}
            t.compare_trial(self.db, TRIAL_NS, LIVE_NS, '2026-11-20')
            after = {table: orig_query(
                'SELECT COUNT(*) AS n FROM %s' % table)[0]['n']
                for table in before}
        finally:
            self.db.query, self.db.batch = orig_query, orig_batch
        self.assertEqual(writes, [])
        self.assertEqual(before, after)

    def test_compare_summary_present(self):
        out = t.compare_trial(self.db, TRIAL_NS, LIVE_NS, '2026-11-20')
        for key in ('day', 'trial', 'live', 'signals_by_strategy',
                    'fills_by_strategy', 'open_positions', 'scan_batches',
                    'paper_eod', 'errors', 'heartbeat_gaps', 'summary'):
            self.assertIn(key, out, key)
        summary = out['summary']
        self.assertIsInstance(summary, list)
        self.assertEqual(len(summary), 5)
        for line in summary:
            self.assertIsInstance(line, str)
            self.assertTrue(line)
        joined = '\n'.join(summary)
        for word in ('signals', 'fills', 'positions', 'EOD', 'errors'):
            self.assertIn(word, joined, word)


class TrialInitRealPathTests(unittest.TestCase):
    """Task 1 (render-deploy-02): the real trial-init baseline.

    Each test builds a local SQLite DB with a paper_v2 baseline at BAR
    (real bootstrap, no network) and calls the same init_trial function
    the CLI calls, without --fixture.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {}
        for k in ('PAPER_RUNTIME', 'RENDER_MODE',
                  'PAPER_NAMESPACE_OVERRIDE', 'PAPER_TEST_DATABASE',
                  'PAPER_STATE_DIR', 'TURSO_DATABASE_URL',
                  'TURSO_AUTH_TOKEN'):
            self.old[k] = os.environ.pop(k, None)
        os.environ['PAPER_RUNTIME'] = 'render'
        os.environ['RENDER_MODE'] = 'trial'
        os.environ['PAPER_NAMESPACE_OVERRIDE'] = TRIAL_NS
        os.environ['PAPER_STATE_DIR'] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _baselined_db(self):
        fd, raw = tempfile.mkstemp(dir=Path(__file__).resolve().parent,
                                   prefix='test_trial_init_',
                                   suffix='.sqlite')
        os.close(fd)
        path = Path(raw)
        return (*ctl.build_baselined_db(path, BAR, self.tmp.name), path)

    def _watermarks(self, db, namespace):
        return {(r['strategy'], r['symbol']): r['bar_date'] for r in
                db.query('SELECT strategy, symbol, bar_date'
                         ' FROM paper_watermarks WHERE namespace=?',
                         (namespace,))}

    def test_trial_init_real_path_runs_bootstrap_and_matches(self):
        store, db, path = self._baselined_db()
        try:
            out = t.init_trial(store, db, TRIAL_NS, BAR)
            self.assertEqual(out['namespace'], TRIAL_NS)
            self.assertEqual(out['bar'], BAR)
            self.assertTrue(out['bootstrapped'])
            self.assertFalse(out['already_bootstrapped'])
            self.assertTrue(out['watermarks_match_paper_v2'])
            self.assertEqual(out['differences'], [])
            # 692 universe symbols x 7 strategies, all at the bar
            self.assertEqual(out['rows_compared'], 692 * 7)
            self.assertEqual(
                self._watermarks(db, TRIAL_NS),
                self._watermarks(db, LIVE_NS))
        finally:
            ctl.close_baselined_db(store, path)

    def test_trial_init_real_path_no_network(self):
        import requests
        calls = []
        orig_get, orig_post, orig_request = (
            requests.get, requests.post, requests.request)

        def _record(name):
            def _f(*a, **k):
                calls.append(name)
                raise AssertionError('network call during trial-init: ' + name)
            return _f
        requests.get = _record('GET')
        requests.post = _record('POST')
        requests.request = _record('request')
        store, db, path = self._baselined_db()
        try:
            out = t.init_trial(store, db, TRIAL_NS, BAR)
        finally:
            requests.get, requests.post, requests.request = (
                orig_get, orig_post, orig_request)
            ctl.close_baselined_db(store, path)
        self.assertTrue(out['watermarks_match_paper_v2'])
        self.assertEqual(calls, [])

    def test_trial_init_real_path_writes_only_trial_ns(self):
        store, db, path = self._baselined_db()
        try:
            # write spy on the state store: every write must target the
            # trial scope (or the trial's own _global budget key)
            seen = []
            orig_execute = store.execute

            def _spy(sql, args=()):
                seen.append((sql, tuple(args)))
                return orig_execute(sql, args)
            store.execute = _spy
            before_live = {}
            for table in ('paper_signals', 'paper_fills',
                          'paper_positions', 'paper_watermarks',
                          'paper_scan_batches'):
                before_live[table] = len(db.query(
                    'SELECT 1 FROM %s WHERE namespace=?' % table,
                    (LIVE_NS,)))
            conn = sqlite3.connect(str(path))
            before_history = conn.execute(
                'SELECT COUNT(*) FROM daily_ohlcv').fetchone()[0]
            conn.close()
            out = t.init_trial(store, db, TRIAL_NS, BAR)
            self.assertTrue(out['watermarks_match_paper_v2'])
            for sql, args in seen:
                if sql.strip().upper().startswith(
                        ('INSERT', 'UPDATE', 'DELETE')) \
                        and 'paper_runtime_kv' in sql and args:
                    self.assertEqual(args[0], TRIAL_NS, sql)
            # the live book is untouched, and the shared history table too
            for table, n in before_live.items():
                self.assertEqual(len(db.query(
                    'SELECT 1 FROM %s WHERE namespace=?' % table,
                    (LIVE_NS,))), n, table)
            conn = sqlite3.connect(str(path))
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM daily_ohlcv').fetchone()[0],
                before_history)
            # no rows for any third namespace anywhere
            namespaces = set()
            for table in ('paper_signals', 'paper_fills',
                          'paper_positions', 'paper_watermarks'):
                namespaces |= {r['namespace'] for r in db.query(
                    'SELECT DISTINCT namespace FROM %s' % table)}
            self.assertEqual(namespaces, {LIVE_NS, TRIAL_NS})
            conn.close()
        finally:
            ctl.close_baselined_db(store, path)

    def test_trial_init_idempotent_real_path(self):
        store, db, path = self._baselined_db()
        try:
            first = t.init_trial(store, db, TRIAL_NS, BAR)
            self.assertTrue(first['bootstrapped'])
            self.assertFalse(first['already_bootstrapped'])
            second = t.init_trial(store, db, TRIAL_NS, BAR)
            self.assertFalse(second['bootstrapped'])
            self.assertTrue(second['already_bootstrapped'])
            # the second run still runs the watermark comparison
            self.assertTrue(second['watermarks_match_paper_v2'])
            self.assertEqual(second['rows_compared'],
                             first['rows_compared'])
            rows = store.execute(
                "SELECT value FROM paper_runtime_kv WHERE scope=?"
                " AND key='trial_init'", (TRIAL_NS,))
            self.assertEqual(len(rows), 1)
        finally:
            ctl.close_baselined_db(store, path)


if __name__ == '__main__':
    unittest.main()
