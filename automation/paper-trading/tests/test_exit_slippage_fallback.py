"""B1: exit-slippage fallback — base 8 bps when ADV is unavailable (option (a)).

User-decided option (a) from docs/proposals/exit-slippage-fallback.md: when
lagged ADV is unavailable (adv_below_floor or lagged_adv_history_short) an
exit applies the base bps from capital_policy.json
(slippage_base_bps_through_20cr, read from the file, never typed in code),
with no participation term and no 1.5x stress (also for BE/BZ without ADV).
Example: SELL 500 at 110.00 gives 109.91.

Node-side tests reuse the engine harness from tests/test_engine.py
(file-backed sqlite, e.fill through db.mjs). Python-side tests drive
adaptive_engine directly plus node --input-type=module for fees.mjs.
"""
import json
import subprocess
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import adaptive_engine as ae
import engine as e
from adaptive_fixtures import fresh_db, seed_pit, trading_days

T = datetime(2026, 11, 9, 10, tzinfo=e.IST)


def _node_fees(expr):
    out = subprocess.run(['node', '--input-type=module', '-e',
                          f"import {{executionSlippage,estimate}} from './fees.mjs';"
                          f"console.log(JSON.stringify({expr}))"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _node_base(expr):
    out = subprocess.run(['node', '--input-type=module', '-e',
                          f"import {{baseOnlySlippage}} from './fees.mjs';"
                          f"console.log(JSON.stringify({expr}))"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _policy_base_bps():
    return int(json.loads((ROOT / 'capital_policy.json').read_text())['slippage_base_bps_through_20cr'])


class FallbackNodeTests(unittest.TestCase):
    """Live db.mjs SELL branch through the engine harness (as test_engine.py)."""

    @classmethod
    def setUpClass(cls):
        cls.db = e.DB(str(e.ROOT / 'tests' / 'test_') + uuid.uuid4().hex + '.sqlite')
        e.NS = 'test_fixture'
        e.init(cls.db)
        cls.db.batch([('CREATE TABLE daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)', ())])
        e.now = lambda: T

    @classmethod
    def tearDownClass(cls):
        db = getattr(cls, 'db', None)
        if db is None:
            return
        try:
            db.close_helper()
        except Exception:
            pass
        p = Path(db.test_database)
        p.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(p) + suffix).unlink(missing_ok=True)

    def setUp(self):
        e.NS = 'test_' + uuid.uuid4().hex
        e.now = lambda: T
        self.db.batch([('DELETE FROM paper_corporate_actions', ()), ('DELETE FROM paper_price_bands', ())])

    def seed_adv(self, symbol, turnover, sessions=40):
        self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?', (symbol,))])
        if sessions:
            self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,turnover) VALUES(?,?,?,?)',
                            (symbol, (T.date() - timedelta(days=i)).isoformat(), 100, turnover))
                           for i in range(1, sessions + 1)])

    def signal(self, side='BUY', symbol='RELIANCE-EQ', strategy=1, eligible='2026-11-09'):
        self.seed_adv(symbol, 200000000)
        s = {'id': uuid.uuid4().hex, 'strategy': strategy, 'symbol': symbol, 'side': side,
             'bar_date': '2026-11-08', 'eligible_date': eligible, 'reason': 'fixture'}
        self.db.batch([('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (e.NS, s['id'], strategy, symbol, s['bar_date'], side, 'pending', 'fixture', T.isoformat(), eligible))])
        if side == 'BUY':
            self.db.batch([('INSERT OR REPLACE INTO paper_entry_input_proofs VALUES(?,?,?,?,?)', (e.NS, s['id'], e.si.POLICY['version'], s['bar_date'], T.isoformat())),
                           ('INSERT OR REPLACE INTO paper_prefilter_states VALUES(?,?,?,?,?,?,?)', (e.NS, s['bar_date'], symbol, e.pf.POLICY['version'], 'PASS', '{}', T.isoformat())),
                           ('INSERT OR REPLACE INTO paper_entry_gate_proofs VALUES(?,?,?,?,?)', (e.NS, s['id'], s['bar_date'], symbol, e.pf.POLICY['version'])),
                           ('INSERT OR IGNORE INTO paper_qualifier_states VALUES(?,?,?,?,?,?,?,?)', (e.NS, s['bar_date'], strategy, symbol, 1, e.js({'input_policy': {'version': e.si.POLICY['version']}}), '{}', T.isoformat()))])
        return s

    def quote(self, p=100, age=0, **kw):
        q = {'symbol': 'RELIANCE-EQ', 'price': p, 'quote_at': (T - timedelta(seconds=age)).isoformat()}
        q.update(kw)
        return q

    def _payload(self, signal_id):
        return json.loads(self.db.query('SELECT payload_json FROM paper_decisions WHERE namespace=? AND correlation_id=?',
                                        (e.NS, signal_id))[0]['payload_json'])

    def test_sell_without_adv_fills_at_base_bps(self):
        # ADV below the floor: SELL 500 @ 110.00 -> 109.91 (base 8 bps only).
        bought = self.signal()
        self.assertTrue(e.fill(self.db, bought, self.quote(), T)['filled'])
        sell = self.signal('SELL')
        self.seed_adv('RELIANCE-EQ', 90000000)  # below the Rs 20 cr floor
        r = e.fill(self.db, sell, self.quote(110), T)
        self.assertTrue(r['filled'])
        self.assertEqual(r['price'], 109.91)
        payload = self._payload(sell['id'])
        self.assertEqual(payload['slippage_rule'], 'base_only_fallback')
        self.assertTrue(payload['slippage_applied'])
        self.assertEqual(payload['slippage_reason'], 'adv_below_floor')
        self.assertEqual(payload['slippage_base_bps'], _policy_base_bps())
        self.assertEqual(r['slippage_rule'], 'base_only_fallback')
        self.assertEqual(r['slippage_reason'], 'adv_below_floor')

    def test_sell_without_adv_short_history_fills_at_base_bps(self):
        # No usable ADV history: same base-only fill, reason lagged_adv_history_short.
        bought = self.signal()
        self.assertTrue(e.fill(self.db, bought, self.quote(), T)['filled'])
        sell = self.signal('SELL')
        self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?', ('RELIANCE-EQ',))])
        r = e.fill(self.db, sell, self.quote(110), T)
        self.assertTrue(r['filled'])
        self.assertEqual(r['price'], 109.91)
        payload = self._payload(sell['id'])
        self.assertEqual(payload['slippage_rule'], 'base_only_fallback')
        self.assertTrue(payload['slippage_applied'])
        self.assertEqual(payload['slippage_reason'], 'lagged_adv_history_short')
        self.assertEqual(r['slippage_rule'], 'base_only_fallback')
        self.assertEqual(r['slippage_reason'], 'lagged_adv_history_short')

    def test_sell_with_adv_still_uses_tiered_model(self):
        # ADV at/above the floor: tiered model unchanged (rule + participation term).
        bought = self.signal()
        self.assertTrue(e.fill(self.db, bought, self.quote(), T)['filled'])
        sell = self.signal('SELL')
        r = e.fill(self.db, sell, self.quote(110), T)
        self.assertTrue(r['filled'])
        payload = self._payload(sell['id'])
        self.assertEqual(payload['slippage_rule'], 'tiered_adv_slippage')
        self.assertTrue(payload['slippage_applied'])
        # Tiered bps here (8 + 2000 * order/ADV) is above the 8 bps base, so the
        # fill is strictly below the base-only price for a larger order: check
        # the tiered model directly rather than the coincidental 109.91.
        direct = self.db.call({'action': 'slippage', 'adv_paise': 20000000000,
                               'order_paise': 55000000, 'reference_paise': 11000,
                               'side': 'SELL', 'stress': False})
        self.assertGreater(direct['bps_numerator'], direct['bps_denominator'] * 8)

    def test_be_bz_without_adv_gets_flat_base_no_stress(self):
        # BE/BZ exit without ADV: flat base 8 bps, NO 1.5x stress (109.91, not 109.86).
        sbin = self.signal(symbol='SBIN-EQ')
        self.assertTrue(e.fill(self.db, sbin, self.quote(), T)['filled'])
        be_sell = self.signal('SELL', symbol='SBIN-EQ')
        self.seed_adv('SBIN-EQ', 90000000)  # below the floor (after the signal seeds ADV)
        be_exit = e.fill(self.db, be_sell,
                         {**self.quote(110), 'symbol': 'SBIN-EQ', 'series': 'BE'}, T)
        self.assertTrue(be_exit['filled'])
        self.assertEqual(be_exit['price'], 109.91)
        payload = json.loads(self.db.query(
            "SELECT payload_json FROM paper_decisions WHERE namespace=? AND decision_type='EXIT' AND symbol='SBIN-EQ'",
            (e.NS,))[0]['payload_json'])
        self.assertEqual(payload['slippage_rule'], 'base_only_fallback')
        self.assertTrue(payload['series_exit_stress'])  # series flag stays true; price has no stress

    def test_buy_path_unchanged(self):
        # BUY entries are untouched: tiered slippage when ADV ok, ADV_FILTER_SKIP below floor.
        s = self.signal()
        bought = e.fill(self.db, s, self.quote(), T)
        self.assertTrue(bought['filled'])
        self.assertEqual(bought['price'], 100.09)
        self.assertEqual(bought['qty'], 500)
        thin = self.signal(strategy=2)
        self.seed_adv('RELIANCE-EQ', 90000000)
        blocked = e.fill(self.db, thin, self.quote(), T)
        self.assertFalse(blocked['filled'])
        self.assertEqual(blocked['reason'], 'ADV_FILTER_SKIP')


class FallbackHelperTests(unittest.TestCase):
    def test_fallback_reads_policy_number(self):
        # The base bps comes from capital_policy.json (8 today), never typed in code.
        base = _policy_base_bps()
        out = _node_base(f"baseOnlySlippage({{referencePaise:11000,side:'SELL'}})")
        self.assertEqual(out['base_bps'], base)
        self.assertEqual(out['price_paise'], 10991)
        self.assertEqual(out['price'], 109.91)
        buy = _node_base(f"baseOnlySlippage({{referencePaise:11000,side:'BUY'}})")
        self.assertEqual(buy['price_paise'], 11009)
        self.assertEqual(buy['base_bps'], base)
        # fees.mjs must not hard-code the base: the helper reads capitalPolicy.
        src = (ROOT / 'fees.mjs').read_text()
        self.assertIn('slippage_base_bps_through_20cr', src)

    def test_adaptive_mirror_matches_node_helper(self):
        for ref, side in ((11000, 'SELL'), (11000, 'BUY'), (99753, 'SELL'), (99753, 'BUY'),
                          (25000, 'SELL'), (100, 'SELL')):
            js = _node_base(f"baseOnlySlippage({{referencePaise:{ref},side:'{side}'}})")
            py = ae.base_only_slippage_paise(ref, side)
            self.assertEqual(py['price_paise'], js['price_paise'], (ref, side))
            self.assertEqual(py['base_bps'], js['base_bps'], (ref, side))
        with self.assertRaises(ValueError):
            ae.base_only_slippage_paise(0, 'SELL')
        with self.assertRaises(ValueError):
            ae.base_only_slippage_paise(11000, 'HOLD')

    def test_estimate_exit_haircut_includes_fallback_slippage(self):
        # qty 500 @ 110.00, no ADV: fallback slippage = 500 * (11000-10991) = 4500 paise.
        got = ae.estimate_exit_haircut_paise(500, 110.00, adv_paise=None)
        fees = ae.fill_charges_paise(500 * 11000, 'SELL', overnight_dp=True)['total']
        self.assertEqual(got, 4500 + fees)
        # ADV below the floor is the same fallback (not the tiered model).
        self.assertEqual(ae.estimate_exit_haircut_paise(500, 110.00, adv_paise=100_000_000), got)
        # ADV at/above the floor uses the tiered model (distinguishable at qty 5000).
        tiered = ae.estimate_exit_haircut_paise(5000, 110.00, adv_paise=20_000_000_000)
        fallback = ae.estimate_exit_haircut_paise(5000, 110.00, adv_paise=None)
        self.assertEqual(fallback, 5000 * 9 + ae.fill_charges_paise(5000 * 11000, 'SELL', overnight_dp=True)['total'])
        self.assertNotEqual(tiered, fallback)
        self.assertGreater(tiered, fallback)  # tiered adds the participation term

    def test_fee_charges_unchanged(self):
        # B1 is a price adjustment, not a charge: statutory charges are identical to fees.mjs.
        for turnover in (4_987_650, 123_456, 19_999_999, 50_000_00):
            js = _node_fees(f"[estimate({{turnover:{turnover}n,side:'BUY'}}),estimate({{turnover:{turnover}n,side:'SELL',dp:true}})]")
            self.assertEqual(ae.fill_charges_paise(turnover, 'BUY'), js[0])
            self.assertEqual(ae.fill_charges_paise(turnover, 'SELL', overnight_dp=True), js[1])


def _fallback_market(low_volume_from=60):
    """Synthetic single-symbol market (marked synthetic): steady rise, then a
    crash. Volume is high (Rs ~30 cr/day turnover) before `low_volume_from`,
    then Rs ~0.2 cr/day, so lagged ADV is above the floor at entry but below
    it at the crash exit. Strategy 1 (Donchian) enters on the rise and exits
    on the crash."""
    days = trading_days(200, start=date(2024, 1, 1))
    closes = []
    for i in range(200):
        if i < 120:
            closes.append(100.0 + i)
        elif i < 126:
            closes.append(219.0 - 20.0 * (i - 119))
        else:
            closes.append(99.0)
    rows = []
    for i, d in enumerate(days):
        c = closes[i]
        o = closes[i - 1] if i else c
        vol = 3_000_000 if i < low_volume_from else 10_000
        rows.append({'symbol': 'AAA-EQ', 'trade_date': d, 'open': o,
                     'high': round(max(o, c) * 1.005, 2), 'low': round(min(o, c) * 0.995, 2),
                     'close': c, 'volume': vol})
    df = pd.DataFrame(rows)
    idx = pd.DataFrame([{'symbol': 'NIFTY500', 'trade_date': d, 'open': 1000 + i,
                         'high': 1001 + i, 'low': 999 + i, 'close': 1000 + i, 'volume': 1}
                        for i, d in enumerate(days)])
    return days, df, idx


class FallbackSimTests(unittest.TestCase):
    def _sim(self, low_volume_from):
        days, df, idx = _fallback_market(low_volume_from)
        db = fresh_db(ROOT)
        seed_pit(db, 'fallback', days, ['AAA-EQ'], series='EQ')
        return ae.simulate_track_window(db, 'fallback', 1, dict(ae.BASELINE_PARAMS[1]),
                                        df, idx, days[:110], days[110:]), df

    def test_adaptive_sim_exit_applies_fallback_and_reports_rule(self):
        res, _ = self._sim(low_volume_from=60)
        self.assertGreater(len(res['closed_trades']), 0)
        t = res['closed_trades'][0]
        # Exit fill day 121 at open 199.00; ADV below floor -> base-only 8 bps.
        self.assertEqual(t['exit_slippage_applied'], True)
        self.assertEqual(t['exit_slippage_rule'], 'base_only_fallback')
        ref_paise = int(round(t['reference_exit_price'] * 100))
        expected = ae.base_only_slippage_paise(ref_paise, 'SELL')['price_paise']
        self.assertEqual(int(round(t['exit_price'] * 100)), expected)
        self.assertLess(t['exit_price'], t['reference_exit_price'])
        # Control: ADV available throughout -> tiered rule, not the fallback.
        res2, _ = self._sim(low_volume_from=10 ** 9)
        t2 = res2['closed_trades'][0]
        self.assertEqual(t2['exit_slippage_rule'], 'tiered_adv_slippage')
        self.assertTrue(t2['exit_slippage_applied'])


if __name__ == '__main__':
    unittest.main()
