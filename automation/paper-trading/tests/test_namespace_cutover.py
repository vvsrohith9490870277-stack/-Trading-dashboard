"""paper_v2 cutover (Task 1): config namespace, protected books, support-agent defaults.

Task 5: proof nothing writes paper_v1 (static literal scan + live engine run)."""
import hashlib, inspect, json, re, sys, tempfile, unittest, uuid
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e
import exit_selftest as xst
import support_agent as sa

T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
NS_V1 = 'paper_v1'
NS_V2 = 'paper_v2'


class FakeAPI:
    def __init__(self, quotes):
        self._quotes = quotes
        self.calls = 0
        self.budget = {'day': '2026-11-09', 'requests': 10, 'remaining_today': 3000,
                       'per_minute': 0, 'per_second': 0,
                       'limits': {'per_day': 4000, 'per_minute': 40, 'per_second': 4,
                                  'reserve_for_session_monitor': 1500}}

    def quotes(self, symbols):
        self.calls += 1
        return {s: dict(self._quotes[s]) for s in symbols if s in self._quotes}

    def budget_snapshot(self):
        snap = dict(self.budget)
        snap['requests'] = int(self.budget.get('requests') or 0) + self.calls
        snap['remaining_today'] = int(self.budget.get('remaining_today') or 0) - self.calls
        return snap


class CutoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = e.ROOT / 'tests' / ('test_cutover_' + uuid.uuid4().hex + '.sqlite')
        cls.db = e.DB(str(cls.path))
        cls._prev_ns = e.NS
        cls._prev_now = e.now
        e.NS = 'test_fixture'
        e.now = lambda: T
        e.init(cls.db)
        cls.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)', ())])

    @classmethod
    def tearDownClass(cls):
        e.NS = cls._prev_ns
        e.now = cls._prev_now
        cls.path.unlink(missing_ok=True)

    def setUp(self):
        e.now = lambda: T
        self.ns = xst.TEST_NS
        e.NS = self.ns
        xst.cleanup(e, self.db, self.ns)
        # A protected paper_v2 row must survive the harness (paper_v1 is covered
        # by test_exit_selftest.py's NS_V1 seed).
        self.db.batch([
            ('DELETE FROM paper_positions WHERE namespace=?', (NS_V1,)),
            ('DELETE FROM paper_positions WHERE namespace=?', (NS_V2,)),
            ('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',
             (NS_V2, 1, 'TCS-EQ', 3, 100, T.isoformat(), 100, T.isoformat())),
        ])

    def tearDown(self):
        xst.cleanup(e, self.db, self.ns)
        e.NS = 'test_fixture'

    def test_repo_config_namespace_is_paper_v2(self):
        cfg = json.loads((e.ROOT / 'config.json').read_text())
        self.assertEqual(cfg['namespace'], 'paper_v2')

    def test_exit_selftest_protects_both_books(self):
        self.assertTrue({'paper_v1', 'paper_v2'} <= set(xst.PROTECTED))
        for ns in ('paper_v1', 'paper_v2'):
            with self.assertRaisesRegex(ValueError, 'protected'):
                xst._require_test_ns(ns)
            with self.assertRaisesRegex(ValueError, 'protected'):
                xst.cleanup(e, self.db, ns)
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V2, 'TCS-EQ'))[0]['qty'], 3)

    def test_exit_selftest_reports_live_ns_unchanged(self):
        q = {'symbol': 'RELIANCE-EQ', 'price': 101, 'open': 100, 'low': 99, 'high': 102,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        api = FakeAPI({'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        r = xst.run(e, self.db, api, max_seconds=1, max_quotes=5, poll_seconds=0, cleanup_after=False, live=False,
                    quotes_seed={'RELIANCE-EQ': q, 'HDFCBANK-EQ': dict(q, symbol='HDFCBANK-EQ')})
        self.assertEqual(r['live_ns'], 'paper_v2')
        self.assertTrue(r['live_ns_positions_unchanged'])
        self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?', (NS_V2, 'TCS-EQ'))[0]['qty'], 3)
        # Existing paper_v1 report keys are kept for other tools.
        self.assertIn('paper_v1_before', r)
        self.assertIn('paper_v1_after', r)
        self.assertIn('paper_v1_positions_unchanged', r)

    def test_support_agent_defaults_follow_config(self):
        for fn in (sa.build_nov9_replay_fixture, sa.run_virtual_clock_replay, sa.write_heartbeat):
            self.assertIsNone(inspect.signature(fn).parameters['namespace'].default)
        prev = sa.CFG['namespace']
        try:
            sa.CFG['namespace'] = 'paper_cutover_test_v1'
            with tempfile.TemporaryDirectory() as tmp:
                payload = sa.write_heartbeat(tmp)
                self.assertEqual(payload['namespace'], 'paper_cutover_test_v1')
                explicit = sa.write_heartbeat(tmp, 'paper_v1')
                self.assertEqual(explicit['namespace'], 'paper_v1')
        finally:
            sa.CFG['namespace'] = prev

    # ---- Task 5: proof nothing writes paper_v1 ----

    def test_no_paper_v1_write_literals(self):
        repo = Path(__file__).resolve().parents[3]
        pat = re.compile(r"INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|namespace\s*=|NS\s*=|['\"]namespace['\"]\s*:|namespace:")
        allowed = {
            # Cleanup return payload reports the paper_v1 snapshot (read-only);
            # the cleanup itself runs under TEST_NS.
            'automation/paper-trading/exit_selftest.py:506': 'cleanup report key, not a write',
        }
        excluded, bad = [], []
        skip_dirs = {'node_modules', 'tests', 'docs', 'reports'}
        for p in sorted(repo.rglob('*')):
            if not p.is_file():
                continue
            rel = p.relative_to(repo)
            if any(part in skip_dirs or part.startswith('.venv') for part in rel.parts):
                continue
            if p.suffix == '.md':
                continue
            if p.suffix == '.json' and str(rel) != 'live/adaptive_live_config.json':
                continue
            try:
                text = p.read_text(errors='replace')
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if 'paper_v1' not in line or not pat.search(line):
                    continue
                key = f'{rel}:{i}'
                if key in allowed:
                    excluded.append(f'{key}: {allowed[key]}')
                else:
                    bad.append(f'{key}: {line.strip()[:160]}')
        print('excluded paper_v1 write-pattern hits:')
        for hit in excluded:
            print(' ', hit)
        self.assertEqual(bad, [], 'paper_v1 lines matching write patterns:\n' + '\n'.join(bad))

    def _seed_adv(self, symbol, turnover, sessions=40):
        self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?', (symbol,))])
        if sessions:
            self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,turnover) VALUES(?,?,?,?)',
                            (symbol, (T.date() - timedelta(days=i)).isoformat(), 100, turnover))
                           for i in range(1, sessions + 1)])

    def _signal(self, side='BUY', symbol='RELIANCE-EQ', strategy=1, eligible='2026-11-09'):
        self._seed_adv(symbol, 200000000)
        s = {'id': uuid.uuid4().hex, 'strategy': strategy, 'symbol': symbol, 'side': side,
             'bar_date': '2026-11-08', 'eligible_date': eligible, 'reason': 'fixture'}
        self.db.batch([('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (e.NS, s['id'], strategy, symbol, s['bar_date'], side, 'pending', 'fixture', T.isoformat(), eligible))])
        if side == 'BUY':
            self.db.batch([('INSERT OR REPLACE INTO paper_entry_input_proofs VALUES(?,?,?,?,?)',
                            (e.NS, s['id'], e.si.POLICY['version'], s['bar_date'], T.isoformat())),
                           ('INSERT OR REPLACE INTO paper_prefilter_states VALUES(?,?,?,?,?,?,?)',
                            (e.NS, s['bar_date'], symbol, e.pf.POLICY['version'], 'PASS', '{}', T.isoformat())),
                           ('INSERT OR REPLACE INTO paper_entry_gate_proofs VALUES(?,?,?,?,?)',
                            (e.NS, s['id'], s['bar_date'], symbol, e.pf.POLICY['version'])),
                           ('INSERT OR IGNORE INTO paper_qualifier_states VALUES(?,?,?,?,?,?,?,?)',
                            (e.NS, s['bar_date'], strategy, symbol, 1,
                             e.js({'input_policy': {'version': e.si.POLICY['version']}}), '{}', T.isoformat()))])
        return s

    def _quote(self, p=100, age=0):
        return {'symbol': 'RELIANCE-EQ', 'price': p, 'quote_at': (T - timedelta(seconds=age)).isoformat()}

    def _hash_ns_rows(self, ns):
        out = {}
        for table in xst.namespaced_tables(e, self.db):
            rows = self.db.query(f'SELECT * FROM {table} WHERE namespace=? ORDER BY rowid', (ns,))
            out[table] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        return out

    def _seed_v1_row(self, table):
        cols = self.db.query(f'PRAGMA table_info({table})')
        sql = self.db.query("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))[0]['sql']
        pk_cols = [c for c in cols if c['pk']]
        rowid_alias = len(pk_cols) == 1 and (pk_cols[0]['type'] or '').upper() == 'INTEGER'
        first_in = {}
        for m in re.finditer(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", sql, re.I):
            vals = [v.strip().strip("'\"") for v in m.group(2).split(',') if v.strip()]
            if vals:
                first_in[m.group(1)] = vals[0]
        names, values = [], []
        for c in cols:
            name, typ = c['name'], (c['type'] or '').upper()
            if rowid_alias and c['pk']:
                continue  # single INTEGER PRIMARY KEY is the rowid alias, auto-assigned
            if name == 'namespace':
                names.append(name)
                values.append(NS_V1)
                continue
            if c['dflt_value'] is not None:
                continue  # server default fills it
            if not c['notnull']:
                continue  # nullable, leave NULL
            if name in first_in:
                raw = first_in[name]
                if 'INT' in typ:
                    try:
                        raw = int(raw)
                    except ValueError:
                        pass
                elif 'REAL' in typ or 'FLOA' in typ or 'DOUB' in typ:
                    try:
                        raw = float(raw)
                    except ValueError:
                        pass
                names.append(name)
                values.append(raw)
                continue
            if 'INT' in typ:
                v = 1
            elif 'REAL' in typ or 'FLOA' in typ or 'DOUB' in typ:
                v = 1.0
            else:
                v = 'x'
            names.append(name)
            values.append(v)
        self.db.batch([(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})", tuple(values))])

    def test_engine_run_as_paper_v2_leaves_paper_v1_untouched(self):
        e.NS = NS_V2
        q = {'symbol': 'RELIANCE-EQ', 'price': 100, 'open': 100, 'low': 99, 'high': 102,
             'quote_at': T.isoformat(), 'provider_status': 'ok', 'source': 'FYERS_QUOTES'}
        # The surveillance gate is live for a real namespace: record all sources ok.
        e.record_reference_status(self.db, '2026-11-09',
                                  {'sources': {'asm': {'status': 'ok'}, 'gsm': {'status': 'ok'},
                                              'series': {'status': 'ok'}}})
        s = self._signal()
        for table in xst.namespaced_tables(e, self.db):
            self._seed_v1_row(table)
        for table in xst.namespaced_tables(e, self.db):
            n = self.db.query(f'SELECT COUNT(*) AS n FROM {table} WHERE namespace=?', (NS_V1,))[0]['n']
            self.assertGreaterEqual(n, 1, f'{table} has no paper_v1 seed row')
        before = self._hash_ns_rows(NS_V1)
        # Spy installed AFTER seeding: seeding itself binds paper_v1.
        seen = []
        orig_batch, orig_call = self.db.batch, self.db.call

        def spy_batch(statements):
            seen.append({'batch': [{'sql': sql, 'args': list(args)} for sql, args in statements]})
            return orig_batch(statements)

        def spy_call(a):
            seen.append({'call': a})
            return orig_call(a)

        self.db.batch, self.db.call = spy_batch, spy_call
        try:
            r = e.fill(self.db, s, self._quote(), T)
            self.assertTrue(r['filled'], r)
            mon = e.monitor(self.db, FakeAPI({'RELIANCE-EQ': q}))
            self.assertIn('status', mon)
        finally:
            self.db.batch, self.db.call = orig_batch, orig_call
        self.assertEqual(before, self._hash_ns_rows(NS_V1),
                         'paper_v1 rows changed while the engine ran as paper_v2')
        self.assertNotIn('paper_v1', json.dumps(seen, default=str),
                         'a write/read statement bound paper_v1 while the engine ran as paper_v2')


if __name__ == '__main__':
    unittest.main()
