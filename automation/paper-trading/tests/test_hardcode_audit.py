"""Freeze-upgrade hardcoded-value audit: policy numbers live in JSON, code reads them, and the lock fails closed."""
import copy, json, re, shutil, subprocess, sys, tempfile, unittest, uuid
from pathlib import Path
from datetime import datetime, timedelta
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import capital as cap
import engine as e
import pauses as pz

SOURCES = ['engine.py', 'automation.py', 'capital.py', 'pauses.py', 'corporate_actions.py', 'reconciliation.py', 'bhavcopy_compare.py', 'prefilter.py', 'guardian.py', 'reference_feeds.py', 'sessiond.py', 'db.mjs', 'fees.mjs', '../../server/capped-ledger.mjs']


def code_only(name):
    """Source text with comments and docstrings removed, so only executable literals are checked."""
    text = (ROOT / name).read_text()
    if name.endswith('.py'):
        text = re.sub(r'"""[\s\S]*?"""', '', text)
        text = re.sub(r'(?m)#.*$', '', text)
    else:
        text = re.sub(r'(?m)^\s*//.*$', '', text)
        text = re.sub(r'/\*[\s\S]*?\*/', '', text)
    return text


class FreezeLockTests(unittest.TestCase):
    def test_lock_matches_files_and_python_js_digests_agree(self):
        lock = json.loads((ROOT / 'freeze_lock.json').read_text())
        self.assertEqual(lock['files'], cap.current_lock())
        script = ("import crypto from 'node:crypto';import fs from 'fs';"
                  "function stable(v){if(Array.isArray(v))return '['+v.map(stable).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable(v[k])).join(',')+'}';return JSON.stringify(v===undefined?null:v)}"
                  "const out={};for(const f of %s)out[f]=crypto.createHash('sha256').update(stable(JSON.parse(fs.readFileSync(f,'utf8')))).digest('hex');console.log(JSON.stringify(out))") % json.dumps(list(cap.LOCKED_FILES))
        js = json.loads(subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(js, lock['files'])

    def test_any_policy_number_change_without_relock_fails_closed(self):
        for key, value in [('target_notional_inr', 60000), ('max_positions_per_sleeve', 8), ('adv_floor_inr', 50000000), ('participation_rate', 0.01), ('sleeve_pause_drawdown', 0.12)]:
            p = copy.deepcopy(cap.POLICY); p[key] = value
            with self.assertRaises(SystemExit):
                cap.assert_freeze(p)

    def test_relocked_policy_change_is_accepted_without_code_change(self):
        p = copy.deepcopy(cap.POLICY); p['version'] = 'freeze_capital_test_relock'; p['target_notional_inr'] = 40000
        lock = {'capital_policy_version': p['version'], 'files': {'capital_policy.json': cap.digest(p)}}
        self.assertEqual(cap.assert_freeze(p, lock)['target_notional_inr'], 40000)
        self.assertEqual(cap.integer_shares(100, p['target_notional_inr']), 400)

    def test_structural_guards_still_fail_closed_after_relock(self):
        cases = [('sleeve_capital_inr', 400000), ('max_positions_per_sleeve', 10), ('hysteresis_shadow_trades', 5), ('adv_lag_sessions', 2), ('adv_floor_review', 'frozen_by_agent')]
        for key, value in cases:
            p = copy.deepcopy(cap.POLICY); p[key] = value
            lock = {'capital_policy_version': p['version'], 'files': {'capital_policy.json': cap.digest(p)}}
            with self.assertRaises(SystemExit, msg=key):
                cap.assert_freeze(p, lock)

    def test_db_mjs_refuses_to_run_when_policy_differs_from_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ['db.mjs', 'fees.mjs', 'config.json', 'freeze_lock.json', 'session_input_policy.json', 'prefilter_policy.json', *cap.LOCKED_FILES]:
                shutil.copy(ROOT / name, tmp / name)
            (tmp / 'node_modules').symlink_to((ROOT / 'node_modules').resolve())
            (tmp / 'tests').mkdir()
            args = json.dumps({'action': 'estimate', 'price': '100', 'quantity': 10, 'side': 'BUY', 'test_database': str(tmp / 'tests' / 'test_lock.sqlite')})
            ok = subprocess.run(['node', 'db.mjs'], cwd=tmp, input=args, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            p = json.loads((tmp / 'capital_policy.json').read_text()); p['target_notional_inr'] = 60000
            (tmp / 'capital_policy.json').write_text(json.dumps(p))
            bad = subprocess.run(['node', 'db.mjs'], cwd=tmp, input=args, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('freeze_policy_lock_mismatch', bad.stderr)


class NoMagicNumberTests(unittest.TestCase):
    def test_no_policy_literals_left_in_engine_code(self):
        forbidden = {
            r'2026-11-08': 'Muhurat date must come from config.json indicator_excluded_sessions',
            r'quantity\s*=\s*1\b': 'no fixed 1-share quantity',
            r'\b1475\b|\b1250n\b|\b225n\b': 'DP charge must come from charge_rates.json',
            r'500000\b|3500000\b|50000\b': 'capital numbers must come from capital_policy.json',
            r'!==\s*9\b|>=\s*9\)|max_positions:9|\b0\.005\b|5/1000': 'sleeve/participation numbers must come from capital_policy.json',
            r'(?<![\w.])\.9\s*\*|0\.9\s*\*': 'Strategy 07 trail must come from config + frozen rule check',
            r'LIMIT 20\b': 'ADV lookback must come from capital_policy.json',
            r'100000000\b|200000000n': 'ADV floor / slippage tier must come from capital_policy.json',
            r'\b(TODO|FIXME|TBD)\b': 'no unfinished markers',
        }
        for name in SOURCES:
            if name.startswith('../') and not (ROOT / name).exists():
                # Installed runtime (/workspace/paper-trading) has no repo server/ tree; the repo checkout still audits it.
                continue
            text = code_only(name)
            if name == 'engine.py':
                # Strategy 01 is the frozen in-engine signal formula (Track A); its windows are not execution policy.
                text = text.replace("f.close>f.high.shift().rolling(20).max()", '').replace('f.low.shift().rolling(10).min()', '')
            for pattern, why in forbidden.items():
                self.assertIsNone(re.search(pattern, text), f'{name}: {why} ({pattern})')

    def test_config_values_drive_engine(self):
        self.assertEqual(e.MUHURAT_INDICATOR_EXCLUDED, frozenset(e.CFG['indicator_excluded_sessions']))
        self.assertEqual(e.pf.MUHURAT_INDICATOR_EXCLUDED, e.MUHURAT_INDICATOR_EXCLUDED)
        self.assertEqual(e.AUTH_ROUTES, e.CFG['market_data']['routes'])
        self.assertEqual([t.strftime('%H:%M') for t in e.REGULAR_SESSION], e.CFG['regular_session'])
        self.assertEqual(e.trail_level(1000), round(e.CFG['strategy_07_trail_fraction'] * 1000, 2))
        self.assertIn('.9*H', e.RULES['7']['trailing_update'])
        self.assertEqual(e.circuit_haircut(80, 3), 80 * (100 - 2 * cap.POLICY['circuit_lock_haircut_pct_per_extra_day']) / 100)

    def test_pause_rule_labels_follow_policy(self):
        self.assertEqual(pz.pct_label(cap.POLICY['sleeve_pause_drawdown']), '10')
        self.assertEqual(pz.pct_label(cap.POLICY['system_pause_drawdown']), '8')
        self.assertEqual(pz.pct_label(0.035), '3_5')
        self.assertIsNotNone(pz.breaches([100, 89], 0.1))
        self.assertIsNone(pz.breaches([100, 90], 0.1))
        self.assertTrue(pz.recovered(97, 100, 0.03))
        self.assertFalse(pz.recovered(96, 100, 0.03))


T = datetime(2026, 11, 9, 10, tzinfo=e.IST)


class SellQuantityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = e.DB(str(e.ROOT / 'tests' / 'test_') + uuid.uuid4().hex + '.sqlite'); cls.path = Path(cls.db.test_database)
        e.NS = 'test_fixture'; e.now = lambda: T; e.init(cls.db)
        cls.db.batch([('CREATE TABLE daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)', ())])

    @classmethod
    def tearDownClass(cls):
        cls.path.unlink(missing_ok=True)

    def signal(self, side):
        sym = 'RELIANCE-EQ'
        self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?', (sym,))])
        self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,turnover) VALUES(?,?,?,?)', (sym, (T.date() - timedelta(days=i)).isoformat(), 100, 200000000)) for i in range(1, 41)])
        s = {'id': uuid.uuid4().hex, 'strategy': 1, 'symbol': sym, 'side': side, 'bar_date': '2026-11-08', 'eligible_date': '2026-11-09', 'reason': 'fixture'}
        self.db.batch([('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)', (e.NS, s['id'], 1, sym, s['bar_date'], side, 'pending', 'fixture', T.isoformat(), '2026-11-09'))])
        if side == 'BUY':
            self.db.batch([('INSERT OR REPLACE INTO paper_entry_input_proofs VALUES(?,?,?,?,?)', (e.NS, s['id'], e.si.POLICY['version'], s['bar_date'], T.isoformat())), ('INSERT OR REPLACE INTO paper_prefilter_states VALUES(?,?,?,?,?,?,?)', (e.NS, s['bar_date'], sym, e.pf.POLICY['version'], 'PASS', '{}', T.isoformat())), ('INSERT OR REPLACE INTO paper_entry_gate_proofs VALUES(?,?,?,?,?)', (e.NS, s['id'], s['bar_date'], sym, e.pf.POLICY['version'])), ('INSERT OR REPLACE INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?)', (e.NS, s['bar_date'], 1, sym, 1, e.js({'input_policy': {'version': e.si.POLICY['version']}}), '{}', T.isoformat()))])
        return s

    def test_exit_audits_full_position_quantity_not_one_share(self):
        e.NS = 'test_' + uuid.uuid4().hex; e.now = lambda: T
        q = {'symbol': 'RELIANCE-EQ', 'price': 100, 'quote_at': T.isoformat()}
        buy = e.fill(self.db, self.signal('BUY'), q, T); self.assertTrue(buy['filled']); self.assertEqual(buy['qty'], cap.integer_shares(100))
        sell = e.fill(self.db, self.signal('SELL'), {**q, 'price': 105}, T); self.assertTrue(sell['filled']); self.assertEqual(sell['qty'], buy['qty'])
        rows = self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='paper_order_executed'", (e.NS,))
        self.assertEqual(sorted(json.loads(r['data_json'])['quantity'] for r in rows), [buy['qty'], buy['qty']])
        result = self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='paper_order_result'", (e.NS,))
        self.assertTrue(all(json.loads(r['data_json']).get('quantity') == buy['qty'] for r in result))


if __name__ == '__main__':
    unittest.main()
