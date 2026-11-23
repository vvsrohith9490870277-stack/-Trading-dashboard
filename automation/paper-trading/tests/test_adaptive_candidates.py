"""B3 candidate-strategy registry tests + frozen-seven golden regression.

Golden file: automation/paper-trading/tests/fixtures/golden/frozen_seven.json
was generated on origin/main BEFORE any adaptive_engine.py edit, from
make_market(n_days=300, seed=7, volume=300_000) with warm-start 60 for the
fast-swing strategies (3, 5) and 110 for the trend strategies (1, 2, 4, 6, 7).
ADV is available throughout the fixture (turnover ~Rs 30 cr > Rs 20 cr floor),
so the B1 base-only fallback never triggers there and the seven are unchanged
by B1. The regression test re-runs the same simulation and compares the
serialised payload byte-identically with the golden file.

Candidate tests import adaptive_candidates lazily (inside the test methods)
so this module also runs before the registry exists; only the golden test
needs adaptive_engine + fixtures.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import adaptive_candidates as ac
import adaptive_engine as ae
import capital as cap
from adaptive_fixtures import fresh_db, make_market, seed_pit, trading_days

LIVE_DIR = ROOT.parent.parent / 'live'
if str(LIVE_DIR) not in sys.path:
    sys.path.insert(0, str(LIVE_DIR))

GOLDEN_PATH = ROOT / 'tests' / 'fixtures' / 'golden' / 'frozen_seven.json'

# Fast-swing strategies need exactly 60 warm-start days; trend strategies
# need 100-120 (validate_warm_start_days). Eval = the remaining days.
GOLDEN_WARM_START = {1: 110, 2: 110, 3: 60, 4: 110, 5: 60, 6: 110, 7: 110}


def build_golden_payload():
    """Deterministic frozen-seven simulation payload (origin/main behaviour).

    Returns (payload, slippage_rules): the payload strips the B1-added
    'exit_slippage_rule' diagnostic per closed trade, because that field did
    not exist when the golden file was made on origin/main — every other
    result field (prices, dates, qty, P&L, equity) is byte-identical. The
    rules are returned separately so the test can pin that B1's fallback
    never triggers in this fixture (ADV is available throughout, so every
    seven exit must still be 'tiered_adv_slippage').
    """
    days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
    out = {'generator': 'make_market(n_days=300, seed=7, volume=300_000)',
           'warm_start': {str(s): GOLDEN_WARM_START[s] for s in sorted(GOLDEN_WARM_START)},
           'strategies': {}}
    rules = {}
    for s in sorted(ae.BASELINE_PARAMS):
        db = fresh_db(ROOT)
        ns = 'golden'
        syms = sorted(df.symbol.unique())
        seed_pit(db, ns, days, syms, series='EQ')
        ws = GOLDEN_WARM_START[s]
        res = ae.simulate_track_window(db, ns, s, dict(ae.BASELINE_PARAMS[s]),
                                       df, idx, days[:ws], days[ws:])
        rules[str(s)] = [t.pop('exit_slippage_rule', None) for t in res['closed_trades']]
        out['strategies'][str(s)] = res
    return out, rules


def golden_bytes(payload):
    return (json.dumps(payload, sort_keys=True, indent=2, default=str) + '\n').encode()


class FrozenSevenGoldenTests(unittest.TestCase):
    def test_frozen_seven_unchanged_golden(self):
        """Byte-identical to the golden file made on origin/main (modulo the
        B1 diagnostic field, which is pinned separately below)."""
        self.assertTrue(GOLDEN_PATH.exists(), f'golden file missing: {GOLDEN_PATH}')
        payload, rules = build_golden_payload()
        self.assertEqual(golden_bytes(payload), GOLDEN_PATH.read_bytes())
        # B1 must not change any seven exit in this fixture: ADV is available
        # throughout, so every exit stays tiered (fallback never triggers).
        for s, rs in sorted(rules.items()):
            # Strategy 5 has no closed trades in this fixture (pinned by the
            # golden file itself); every exit that does occur must be tiered.
            self.assertTrue(all(r == 'tiered_adv_slippage' for r in rs),
                            f'strategy {s}: unexpected slippage rules {sorted(set(rs))}')


def _bars(closes, symbol='SYN-EQ'):
    """Synthetic single-symbol bars from a close series (marked synthetic)."""
    from datetime import date
    days = trading_days(len(closes), date(2024, 1, 1))
    rows = []
    for i, (d, c) in enumerate(zip(days, closes)):
        o = closes[i - 1] if i else c
        rows.append({'symbol': symbol, 'trade_date': d, 'open': o,
                     'high': round(max(o, c) * 1.002, 2), 'low': round(min(o, c) * 0.998, 2),
                     'close': c, 'volume': 300000})
    import pandas as pd
    return pd.DataFrame(rows)


def _edge_dates(out, col):
    cond = out[col].fillna(False).astype(bool).to_numpy()
    days = out['trade_date'].astype(str).tolist()
    return [days[i] for i in range(1, len(cond)) if cond[i] and not cond[i - 1]]


def _c101_closes():
    px, cl = 1000.0, []
    for i in range(300):
        r = 0.003 if i < 260 else (-0.02 if i < 280 else 0.0)
        px *= (1 + r)
        cl.append(round(px, 2))
    return cl


def _c102_closes():
    px, cl = 1000.0, []
    for i in range(220):
        r = 0.005 if i < 200 else (-0.04 if i < 203 else 0.01)
        px *= (1 + r)
        cl.append(round(px, 2))
    return cl


def _c103_closes():
    import numpy as np
    rng = np.random.default_rng(3)
    px, cl = 1000.0, []
    for i in range(150):
        if i < 110:
            px *= (1 + rng.normal(0, 0.002))
            cl.append(round(px, 2))
        elif i < 130:
            cl.append(cl[-1])  # flat: BandWidth -> 0, a new 126-session low
        elif i == 130:
            cl.append(round(cl[-1] * 1.03, 2))  # breakout above the upper band
        else:
            cl.append(round(cl[-1] * 0.99, 2))
    return cl


def _c104_closes():
    px, cl = 1000.0, []
    for i in range(120):
        r = -0.01 if i < 40 else (0.015 if i < 80 else -0.01)
        px *= (1 + r)
        cl.append(round(px, 2))
    return cl


def _c105_universe(n_days=400):
    import numpy as np
    import pandas as pd
    days = trading_days(n_days)
    rows = []
    for k in range(10):
        sym = 'WIN-EQ' if k == 0 else f'FLAT{k}-EQ'
        px = 1000.0
        for i, d in enumerate(days):
            if k == 0 and i < 260:
                r = 0.01
            elif k == 0:
                r = -0.02
            elif i < 260:
                r = 0.001 + k * 0.0001
            else:
                r = -0.005
            px *= (1 + r)
            o = px / (1 + r)
            rows.append({'symbol': sym, 'trade_date': d, 'open': round(o, 2),
                         'high': round(max(o, px) * 1.002, 2), 'low': round(min(o, px) * 0.998, 2),
                         'close': round(px, 2), 'volume': 300000})
    return pd.DataFrame(rows)


def _c106_universe(n_days=400):
    """40-symbol universe for C106: 4 sectors x 10 stocks, constant daily
    growth for the first 260 sessions, then -0.5%/day. Sector means differ
    sharply, so the sector-adjusted ranking differs from the raw ranking:
    COLD-STAR (raw 12-1 return only ~+59%, mid-pack raw) is #2 by
    sector-adjusted return (+0.52) because its sector is flat, while HOT0
    (raw ~+900%, top by raw return) is LAST by sector-adjusted return
    (-0.53) because its hot sector's mean is higher than its own return."""
    import numpy as np
    import pandas as pd
    days = trading_days(n_days)
    rows, smap = [], {}
    specs = []
    for k in range(10):
        specs.append((f'HOT{k}-EQ', 'HOT', 0.0100 + k * 0.00005))
        specs.append((f'WARM{k}-EQ', 'WARM', 0.0050 + k * 0.00005))
        specs.append((f'COOL{k}-EQ', 'COOL', 0.0010 + k * 0.00005))
    for k in range(9):
        specs.append((f'COLD{k}-EQ', 'COLD', 0.0000 + k * 0.00001))
    specs.append(('COLD-STAR-EQ', 'COLD', 0.002))
    for sym, sec, r in specs:
        smap[sym] = sec
        px = 1000.0
        for i, d in enumerate(days):
            g = r if i < 260 else -0.005
            px *= (1 + g)
            o = px / (1 + g)
            rows.append({'symbol': sym, 'trade_date': d, 'open': round(o, 2),
                         'high': round(max(o, px) * 1.002, 2), 'low': round(min(o, px) * 0.998, 2),
                         'close': round(px, 2), 'volume': 300000})
    return pd.DataFrame(rows), smap


def _c106_fade_universe(n_days=320):
    """32-symbol universe for the C106 exit rule: 31 STEADY stocks grow
    +0.5%/day throughout; FADE grows +1%/day for 200 sessions, then falls
    -1%/day. FADE is rank 1 by sector-adjusted return at the 2025-01-01
    rebalance (enters) and rank 32 at the 2025-02-03 rebalance (exits)."""
    import pandas as pd
    days = trading_days(n_days)
    rows, smap = [], {}
    for k in range(31):
        sym = f'STEADY{k}-EQ'
        smap[sym] = 'BIG'
        px = 1000.0
        for i, d in enumerate(days):
            px *= 1.005
            o = px / 1.005
            rows.append({'symbol': sym, 'trade_date': d, 'open': round(o, 2),
                         'high': round(max(o, px) * 1.002, 2), 'low': round(min(o, px) * 0.998, 2),
                         'close': round(px, 2), 'volume': 300000})
    smap['FADE-EQ'] = 'BIG'
    px = 1000.0
    for i, d in enumerate(days):
        g = 0.01 if i < 200 else -0.01
        px *= (1 + g)
        o = px / (1 + g)
        rows.append({'symbol': 'FADE-EQ', 'trade_date': d, 'open': round(o, 2),
                     'high': round(max(o, px) * 1.002, 2), 'low': round(min(o, px) * 0.998, 2),
                     'close': round(px, 2), 'volume': 300000})
    return pd.DataFrame(rows), smap


def _c101_universe():
    """5-symbol universe for C101: SYN-EQ (the _c101_closes series) has the
    best 252-session return, so its derived_rs_rank is 100 at idx 252; the
    four siblings grow slower, giving ranks 80/60/40/20 at idx 252."""
    import pandas as pd
    series = {'SYN-EQ': _c101_closes()}
    for k, r in enumerate((0.002, 0.001, 0.0005, 0.0)):
        px, cl = 1000.0, []
        for i in range(300):
            px *= (1 + r)
            cl.append(round(px, 2))
        series[f'SIB{k + 1}-EQ'] = cl
    frames = [_bars(cl, symbol=sym) for sym, cl in series.items()]
    return frames[0], pd.concat(frames, ignore_index=True)


def _c108_closes(kind):
    """Single-symbol closes for C108 (S4 + 20-session time stop). All
    series share the same S4 entry: 200 sessions of -0.1%/day, then a
    +2%/day rise; the EMA20/50 cross-up fires at idx 205 (2024-10-14,
    close 921.93) once the close clears SMA200. Entry + 20 sessions is
    idx 225 (2024-11-11).
      'flat'       — rise for 10 sessions, then -2%/day for 5 sessions
                     (price back below the entry, close 902.05), then flat:
                     the trade is not profitable at idx 225, so the time
                     stop fires there (2024-11-11), with no cross-down yet.
      'profitable' — rise for 10 sessions, then +0.5%/day: the trade is
                     profitable at idx 225 and holds; no exit at all in
                     the window (no cross-down either).
      'cross'      — rise for 30 sessions, then -0.5%/day: the slow fall
                     crosses EMA20 below EMA50 at idx 271 (2025-01-14,
                     close 1201.36), far above the entry, so the time stop
                     never fires and the exit is the S4 cross-down."""
    phases = {'flat': [(200, -0.001), (10, 0.02), (5, -0.02), (55, 0.0)],
              'profitable': [(200, -0.001), (10, 0.02), (60, 0.005)],
              'cross': [(200, -0.001), (30, 0.02), (80, -0.005)]}[kind]
    px, cl = 1000.0, []
    for n, r in phases:
        for _ in range(n):
            px *= (1 + r)
            cl.append(round(px, 2))
    return cl


class CandidateSignalTests(unittest.TestCase):
    def test_c101_trend_template_entry_exit(self):
        df, uni = _c101_universe()
        out = ac.candidate_signals(101, df, universe=uni)
        # Rank first computable at idx 252 (2024-12-18): criteria 1-7 hold
        # from idx 251, but criterion 8 (rs_rank >= 70) needs the 252-session
        # return, so the first entry edge is one session later than the 7/8
        # template's 2024-12-17.
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-12-18'])
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2025-01-02'])
        # Independent check of the 8 criteria on the entry day.
        en = ac.CANDIDATE_STRATEGIES[101]['entries'](df, universe=uni)
        row = en.iloc[252]
        self.assertGreater(row['close'], row['sma50'])
        self.assertGreater(row['sma50'], row['sma150'])
        self.assertGreater(row['sma150'], row['sma200'])
        self.assertGreater(row['sma200'], en.iloc[232]['sma200'])
        self.assertGreaterEqual(row['close'], 1.30 * row['low252'])
        self.assertGreaterEqual(row['close'], 0.75 * row['high252'])
        # Criterion 8: derived_rs_rank (in-universe) is 100 on the entry day.
        self.assertEqual(row['derived_rs_rank'], 100.0)
        self.assertGreaterEqual(row['derived_rs_rank'],
                                ac.CANDIDATE_STRATEGIES[101]['params']['rs_rank_min'])
        # Full 8-criterion template: no partial_template / not_evaluable keys.
        self.assertNotIn('partial_template', ac.CANDIDATE_STRATEGIES[101])
        self.assertNotIn('not_evaluable', ac.CANDIDATE_STRATEGIES[101])

    def test_c101_rs_rank_percentiles_at_idx_252(self):
        # Percentile (0-100) of each stock's 252-session return within the
        # universe on the same bar: best -> 100, then 80/60/40/20.
        _, uni = _c101_universe()
        expected = {'SYN-EQ': 100.0, 'SIB1-EQ': 80.0, 'SIB2-EQ': 60.0,
                    'SIB3-EQ': 40.0, 'SIB4-EQ': 20.0}
        for sym, rank in expected.items():
            sdf = uni[uni['symbol'] == sym].reset_index(drop=True)
            en = ac.CANDIDATE_STRATEGIES[101]['entries'](sdf, universe=uni)
            self.assertEqual(en.iloc[252]['derived_rs_rank'], rank, sym)
            # Rank is NaN before idx 252 (no 252-session return yet).
            self.assertTrue(en['derived_rs_rank'].iloc[:252].isna().all(), sym)

    def test_c101_rs_rank_short_history_all_nan(self):
        # A stock with only 100 bars has no 252-session return: its rank is
        # all-NaN (criterion 8 not_evaluable, rs_rank_unavailable, for that
        # stock only) and it never enters; its siblings are unaffected.
        df, uni = _c101_universe()
        short = _bars(_c101_closes()[:100], symbol='SHORT-EQ')
        import pandas as pd
        uni2 = pd.concat([uni, short], ignore_index=True)
        out_short = ac.candidate_signals(101, short, universe=uni2)
        self.assertTrue(out_short['derived_rs_rank'].isna().all())
        self.assertEqual(int(out_short['entry_condition'].sum()), 0)
        # Siblings keep their idx-252 ranks with the short symbol present.
        sdf = uni[uni['symbol'] == 'SYN-EQ'].reset_index(drop=True)
        en = ac.CANDIDATE_STRATEGIES[101]['entries'](sdf, universe=uni2)
        self.assertEqual(en.iloc[252]['derived_rs_rank'], 100.0)

    def test_c101_rs_rank_label_not_ibd(self):
        # The rank is labelled "derived_rs_rank (in-universe)", NOT IBD's
        # rating: it is computed from the bars already held.
        c101 = ac.CANDIDATE_STRATEGIES[101]
        self.assertEqual(c101['rs_rank_label'], 'derived_rs_rank (in-universe)')
        self.assertIn('derived_rs_rank (in-universe)', c101['notes'])
        self.assertIn('NOT IBD', c101['notes'])
        self.assertEqual(c101['params']['rs_rank_min'], 70)
        # Without a universe there is no rank at all: criterion 8 is
        # not_evaluable for that stock, so no entry fires.
        df = _bars(_c101_closes())
        out = ac.candidate_signals(101, df)
        self.assertTrue(out['derived_rs_rank'].isna().all())
        self.assertEqual(int(out['entry_condition'].sum()), 0)

    def test_c106_sector_adjusted_entry_exit(self):
        uni, smap = _c106_universe()
        # Sector-adjusted 12-1 return at the first rebalance (2025-01-01):
        # COLD-STAR is #2 (+0.52) despite a mid-pack raw return, because its
        # sector is flat; HOT0 is LAST (-0.53) despite the highest raw
        # return, because its hot sector's mean beats it. This is the
        # discrimination C106 adds over C105's whole-market rank.
        closes = ac._universe_closes(uni)
        adj = ac._sector_adjusted_frame(closes, smap)
        row = adj.loc['2025-01-01']
        self.assertAlmostEqual(row['COLD-STAR-EQ'], 0.5195, places=3)
        self.assertAlmostEqual(row['HOT0-EQ'], -0.5315, places=3)
        rank = row.rank(ascending=False, method='min')
        self.assertEqual(int(rank['COLD-STAR-EQ']), 2)
        self.assertEqual(int(rank['HOT0-EQ']), 40)
        # Entries at the first rebalance where ranks are computable.
        star = uni[uni['symbol'] == 'COLD-STAR-EQ'].reset_index(drop=True)
        out_star = ac.candidate_signals(106, star, universe=uni, sector_map=smap)
        self.assertEqual(_edge_dates(out_star, 'entry_condition'), ['2025-01-01'])
        hot0 = uni[uni['symbol'] == 'HOT0-EQ'].reset_index(drop=True)
        out_hot0 = ac.candidate_signals(106, hot0, universe=uni, sector_map=smap)
        self.assertEqual(int(out_hot0['entry_condition'].sum()), 0)
        # Every entry fires at a monthly rebalance.
        dates = star['trade_date'].astype(str).tolist()
        reb = {dates[i] for i in range(len(dates))
               if i == 0 or dates[i][:7] != dates[i - 1][:7]}
        entry_true = [dates[i] for i in range(len(dates)) if out_star['entry_condition'].iloc[i]]
        self.assertTrue(all(d in reb for d in entry_true))
        # Registry entry: top 30, monthly rebalance, no 63-session expiry.
        c106 = ac.CANDIDATE_STRATEGIES[106]
        self.assertEqual(c106['params']['top_n'], 30)
        self.assertEqual(c106['params']['rebalance'], 'monthly')
        self.assertNotIn('hold_sessions', c106['params'])
        self.assertEqual(c106['warm_start_days'], 252)
        self.assertIn('C105', c106['notes'])
        self.assertIn('sector', c106['name'].lower())

    def test_c106_exit_on_drop_out_of_top30(self):
        uni, smap = _c106_fade_universe()
        fade = uni[uni['symbol'] == 'FADE-EQ'].reset_index(drop=True)
        out = ac.candidate_signals(106, fade, universe=uni, sector_map=smap)
        # Rank 1 at 2025-01-01 (adjusted +1.099) -> enter; rank 32 at
        # 2025-02-03 (adjusted -0.437) -> exit at that rebalance.
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2025-01-01'])
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2025-02-03'])
        closes = ac._universe_closes(uni)
        adj = ac._sector_adjusted_frame(closes, smap)
        self.assertAlmostEqual(adj.loc['2025-01-01', 'FADE-EQ'], 1.0991, places=3)
        self.assertAlmostEqual(adj.loc['2025-02-03', 'FADE-EQ'], -0.4367, places=3)

    def test_c106_no_sector_map_no_entries(self):
        uni, smap = _c106_universe()
        star = uni[uni['symbol'] == 'COLD-STAR-EQ'].reset_index(drop=True)
        # No map and no sector column on the universe: enters nothing.
        out = ac.candidate_signals(106, star, universe=uni)
        self.assertEqual(int(out['entry_condition'].sum()), 0)
        # Symbol missing from the map: enters nothing.
        out = ac.candidate_signals(106, star, universe=uni,
                                   sector_map={'OTHER-EQ': 'COLD'})
        self.assertEqual(int(out['entry_condition'].sum()), 0)
        # A 'sector' column on the universe frame supplies the map.
        uni2 = uni.copy()
        uni2['sector'] = uni2['symbol'].map(smap)
        out = ac.candidate_signals(106, star, universe=uni2)
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2025-01-01'])
        # An 'industry' column works the same way.
        uni3 = uni.copy()
        uni3['industry'] = uni3['symbol'].map(smap)
        out = ac.candidate_signals(106, star, universe=uni3)
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2025-01-01'])

    def test_c108_time_stop_triggers_after_20_sessions_when_not_profitable(self):
        df = _bars(_c108_closes('flat'))
        out = ac.candidate_signals(108, df)
        ex = ac.CANDIDATE_STRATEGIES[108]['exits'](df)
        # Entry 2024-10-14 (close 921.93); at idx 225, exactly 20 sessions
        # later (2024-11-11), close 902.05 <= entry, so the time stop fires.
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-10-14'])
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2024-11-11'])
        i = out.index[out['trade_date'] == '2024-11-11'][0]
        self.assertEqual(int(ex['sessions_held'].iloc[i]), 20)
        self.assertLessEqual(out['close'].iloc[i], ex['entry_price'].iloc[i])
        # The time stop, not the S4 signal, causes this exit: no cross-down yet.
        self.assertFalse(bool(ex['cross_down'].iloc[i]))

    def test_c108_time_stop_does_not_trigger_before_20_sessions(self):
        df = _bars(_c108_closes('flat'))
        out = ac.candidate_signals(108, df)
        ex = ac.CANDIDATE_STRATEGIES[108]['exits'](df)
        # sessions_held ramps 0..19 in the 19 sessions after entry; no exit
        # fires in that window, even though the price is already below the
        # entry from the dip.
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2024-11-11'])
        self.assertEqual(int(ex['sessions_held'].iloc[224]), 19)
        self.assertFalse(bool(out['exit_condition'].iloc[224]))
        self.assertFalse(bool(out['exit_condition'].iloc[206:225].any()))

    def test_c108_time_stop_does_not_trigger_when_profitable(self):
        df = _bars(_c108_closes('profitable'))
        out = ac.candidate_signals(108, df)
        ex = ac.CANDIDATE_STRATEGIES[108]['exits'](df)
        # At idx 225 (20 sessions after entry) the close is above the entry
        # price, so the time stop does not fire; and there is no cross-down
        # either, so there is no exit at all in the window.
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-10-14'])
        self.assertEqual(int(out['exit_condition'].sum()), 0)
        self.assertEqual(int(ex['sessions_held'].iloc[225]), 20)
        self.assertGreater(out['close'].iloc[225], ex['entry_price'].iloc[225])

    def test_c108_exit_on_s4_cross_down_still_works(self):
        df = _bars(_c108_closes('cross'))
        out = ac.candidate_signals(108, df)
        ex = ac.CANDIDATE_STRATEGIES[108]['exits'](df)
        # Long rise, then a slow -0.5%/day fall: exit is the S4 cross-down
        # (2025-01-14), at a close of 1201.36 — far above the 921.93 entry,
        # so the time stop never fired (the trade stayed profitable).
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-10-14'])
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2025-01-14'])
        i = out.index[out['trade_date'] == '2025-01-14'][0]
        self.assertTrue(bool(ex['cross_down'].iloc[i]))
        self.assertGreater(out['close'].iloc[i], ex['entry_price'].iloc[i])
        self.assertEqual(int(ex['sessions_held'].iloc[i]), 66)

    def test_c108_mirrors_s4_entry_and_registry_shadow_only(self):
        import strategy_indicators as si
        df = _bars(_c108_closes('flat'))
        out = ac.candidate_signals(108, df)
        s4 = si.strategy_4(df)
        # Entry is exactly S4's entry: same edge, same SMA200 gate.
        self.assertEqual(_edge_dates(out, 'entry_condition'),
                         _edge_dates(s4, 'entry_condition'))
        en = ac.CANDIDATE_STRATEGIES[108]['entries'](df)
        # Series.equals treats NaN == NaN (the SMA200 warm-up rows).
        self.assertTrue(en['ema20'].equals(s4['ema20']))
        self.assertTrue(en['ema50'].equals(s4['ema50']))
        self.assertTrue(en['sma200'].equals(s4['sma200']))
        # Registry: shadow-only, S4 + 20-session time stop, warm start 200.
        # No protective price stop: initial_stop is the shared _NO_STOP.
        c108 = ac.CANDIDATE_STRATEGIES[108]
        self.assertEqual(c108['params']['ema_fast'], 20)
        self.assertEqual(c108['params']['ema_slow'], 50)
        self.assertEqual(c108['params']['sma_trend'], 200)
        self.assertEqual(c108['params']['time_stop_sessions'], 20)
        self.assertEqual(c108['warm_start_days'], 200)
        self.assertIs(c108['initial_stop'], ac._NO_STOP)

    def test_c102_connors_rsi2_entry_exit(self):
        df = _bars(_c102_closes())
        out = ac.candidate_signals(102, df)
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-10-08'])
        after_entry = [d for d in _edge_dates(out, 'exit_condition') if d >= '2024-10-08']
        self.assertEqual(after_entry, ['2024-10-14'])
        en = ac.CANDIDATE_STRATEGIES[102]['entries'](df)
        row = en[en['trade_date'] == '2024-10-08'].iloc[0]
        self.assertLess(row['rsi2'], 10.0)
        self.assertGreater(row['close'], row['sma200'])

    def test_c103_bollinger_squeeze_entry_exit(self):
        df = _bars(_c103_closes())
        out = ac.candidate_signals(103, df)
        self.assertEqual(_edge_dates(out, 'entry_condition'),
                         ['2024-02-06', '2024-04-25', '2024-05-31', '2024-07-01'])
        after_last = [d for d in _edge_dates(out, 'exit_condition') if d >= '2024-07-01']
        self.assertEqual(after_last, ['2024-07-04'])
        # The 2024-07-01 breakout day: squeeze present, close above the band.
        en = ac.CANDIDATE_STRATEGIES[103]['entries'](df)
        row = en[en['trade_date'] == '2024-07-01'].iloc[0]
        self.assertTrue(bool(row['squeeze_flag']))
        self.assertGreater(row['close'], row['bb_upper'])

    def test_c104_adx_dmi_entry_exit(self):
        df = _bars(_c104_closes())
        out = ac.candidate_signals(104, df)
        self.assertEqual(_edge_dates(out, 'entry_condition'), ['2024-03-06'])
        self.assertEqual(_edge_dates(out, 'exit_condition'), ['2024-05-07'])
        en = ac.CANDIDATE_STRATEGIES[104]['entries'](df)
        i = en.index[en['trade_date'] == '2024-03-06'][0]
        row, prev = en.iloc[i], en.iloc[i - 1]
        self.assertGreater(row['plus_di'], row['minus_di'])
        self.assertLessEqual(prev['plus_di'], prev['minus_di'])  # the cross
        self.assertGreater(row['adx'], 25.0)

    def test_c105_momentum_adapted_top_decile(self):
        uni = _c105_universe()
        win = uni[uni['symbol'] == 'WIN-EQ'].reset_index(drop=True)
        out_w = ac.candidate_signals(105, win, universe=uni)
        # Monthly rebalances, state-aware walk: enter only when not holding.
        # 2025-01-01 is the first rebalance after the rank becomes known
        # (2024-12-18); re-entry at 2025-04-01, the next rebalance after the
        # first position's expiry.
        self.assertEqual(_edge_dates(out_w, 'entry_condition'), ['2025-01-01', '2025-04-01'])
        # 2025-03-31 is exactly entry+63 sessions after the 2025-01-01 entry;
        # 2025-06-02 is the first rebalance after the rank left the top 30%
        # (2025-05-21), before the second entry's 2025-06-27 expiry.
        self.assertEqual(_edge_dates(out_w, 'exit_condition'), ['2025-03-31', '2025-06-02'])
        # Invariants: every entry fires at a rebalance; every exit is either
        # a rebalance (rank exit) or an entry+63 expiry from an actual entry.
        # No ghost expiry: the 2025-05-01 expiry from the never-opened
        # 2025-02-03 edge (monthly gating without state) is gone.
        dates = win['trade_date'].astype(str).tolist()
        reb = {dates[i] for i in range(len(dates))
               if i == 0 or dates[i][:7] != dates[i - 1][:7]}
        entry_true = [dates[i] for i in range(len(dates)) if out_w['entry_condition'].iloc[i]]
        self.assertTrue(all(d in reb for d in entry_true))
        expiries = {dates[i + 63] for i in (dates.index(d) for d in entry_true)
                    if i + 63 < len(dates)}
        exit_true = [dates[i] for i in range(len(dates)) if out_w['exit_condition'].iloc[i]]
        self.assertTrue(all(d in reb or d in expiries for d in exit_true))
        self.assertNotIn('2025-05-01', exit_true)
        flat = uni[uni['symbol'] == 'FLAT1-EQ'].reset_index(drop=True)
        self.assertEqual(int(ac.candidate_signals(105, flat, universe=uni)['entry_condition'].sum()), 0)
        # Flagged as adapted, not the paper's exact rule, everywhere.
        self.assertTrue(ac.CANDIDATE_STRATEGIES[105]['adapted'])
        self.assertIn("not the paper's exact rule", ac.CANDIDATE_STRATEGIES[105]['notes'])

    def test_no_lookahead(self):
        # Truncating future bars must not change past signals.
        cases = [(102, _bars(_c102_closes()), 210, None),
                 (103, _bars(_c103_closes()), 140, None),
                 (104, _bars(_c104_closes()), 100, None)]
        for s, df, cut, _ in cases:
            full = ac.candidate_signals(s, df)
            trunc = ac.candidate_signals(s, df.iloc[:cut])
            for col in ('entry_condition', 'exit_condition'):
                self.assertTrue((full[col].iloc[:cut].to_numpy() == trunc[col].to_numpy()).all(),
                                f'candidate {s}: {col} changes when future bars are removed')
        # C101 needs its universe for the rank; truncating both the symbol
        # bars and the universe must not change past signals.
        df101, uni101 = _c101_universe()
        cut = 280
        cut_date = df101['trade_date'].iloc[cut - 1]
        uni_trunc = uni101[uni101['trade_date'] <= cut_date]
        full = ac.candidate_signals(101, df101, universe=uni101)
        trunc = ac.candidate_signals(101, df101.iloc[:cut], universe=uni_trunc)
        for col in ('entry_condition', 'exit_condition'):
            self.assertTrue((full[col].iloc[:cut].to_numpy() == trunc[col].to_numpy()).all(),
                            f'candidate 101: {col} changes when future bars are removed')
        import numpy as np
        self.assertTrue(np.array_equal(full['derived_rs_rank'].iloc[:cut].to_numpy(),
                                       trunc['derived_rs_rank'].to_numpy(), equal_nan=True),
                        'candidate 101: derived_rs_rank changes when future bars are removed')
        uni = _c105_universe()
        win = uni[uni['symbol'] == 'WIN-EQ'].reset_index(drop=True)
        full = ac.candidate_signals(105, win, universe=uni)
        trunc_uni = uni[uni['trade_date'] <= win['trade_date'].iloc[349]]
        trunc = ac.candidate_signals(105, win.iloc[:350], universe=trunc_uni)
        for col in ('entry_condition', 'exit_condition'):
            self.assertTrue((full[col].iloc[:350].to_numpy() == trunc[col].to_numpy()).all(),
                            f'candidate 105: {col} changes when future bars are removed')
        # C106 needs its universe and sector map; truncating both must not
        # change past signals.
        uni106, smap106 = _c106_universe()
        star = uni106[uni106['symbol'] == 'COLD-STAR-EQ'].reset_index(drop=True)
        full = ac.candidate_signals(106, star, universe=uni106, sector_map=smap106)
        trunc_uni = uni106[uni106['trade_date'] <= star['trade_date'].iloc[349]]
        trunc = ac.candidate_signals(106, star.iloc[:350], universe=trunc_uni, sector_map=smap106)
        for col in ('entry_condition', 'exit_condition'):
            self.assertTrue((full[col].iloc[:350].to_numpy() == trunc[col].to_numpy()).all(),
                            f'candidate 106: {col} changes when future bars are removed')
        # C108's walk tracks the entry price and sessions held from past
        # bars only; truncating future bars must not change past signals or
        # the sessions held in force.
        df108 = _bars(_c108_closes('flat'))
        full = ac.candidate_signals(108, df108)
        trunc = ac.candidate_signals(108, df108.iloc[:220])
        for col in ('entry_condition', 'exit_condition'):
            self.assertTrue((full[col].iloc[:220].to_numpy() == trunc[col].to_numpy()).all(),
                            f'candidate 108: {col} changes when future bars are removed')
        ex_full = ac.CANDIDATE_STRATEGIES[108]['exits'](df108)
        ex_trunc = ac.CANDIDATE_STRATEGIES[108]['exits'](df108.iloc[:220])
        import numpy as np
        self.assertTrue(np.array_equal(ex_full['sessions_held'].iloc[:220].to_numpy(),
                                       ex_trunc['sessions_held'].to_numpy()),
                        'candidate 108: sessions_held changes when future bars are removed')
        self.assertTrue(np.array_equal(ex_full['entry_price'].iloc[:220].to_numpy(),
                                       ex_trunc['entry_price'].to_numpy(), equal_nan=True),
                        'candidate 108: entry_price changes when future bars are removed')

    def test_registry_not_in_baseline_params(self):
        ids = set(ac.CANDIDATE_STRATEGIES)
        self.assertTrue(len(ids) >= 4 and min(ids) >= 101)
        self.assertFalse(ids & set(ae.BASELINE_PARAMS))
        pol = json.loads((ROOT / 'adaptive_policy.json').read_text())
        self.assertFalse(ids & set(pol['fast_swing_strategies'] + pol['trend_strategies']))
        rules = json.loads((ROOT / 'strategy_rules.json').read_text())
        self.assertFalse(set(map(str, ids)) & set(rules.keys()))
        for s, c in ac.CANDIDATE_STRATEGIES.items():
            self.assertTrue(c['source'] and c['name'])
            self.assertTrue(callable(c['entries']) and callable(c['exits']))
            self.assertIn('warm_start_days', c)

    def test_candidates_share_frozen_sizing_and_costs(self):
        # Same simulate_track_window, same frozen sizing/costs as the seven:
        # sleeve capital, 9 slots, ADV floor, participation, charges, slippage.
        days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
        db = fresh_db(ROOT)
        seed_pit(db, 'cand', days, sorted(df.symbol.unique()), series='EQ')
        cand = ac.CANDIDATE_STRATEGIES[102]
        res = ae.simulate_track_window(db, 'cand', 102, dict(cand['params']), df, idx,
                                       days[:200], days[200:],
                                       signal_evaluator=lambda s, d: ac.candidate_signals(s, d))
        base = ae.simulate_track_window(db, 'cand', 3, dict(ae.BASELINE_PARAMS[3]), df, idx,
                                        days[:60], days[60:])
        for r in (res, base):
            self.assertEqual(r['starting_equity']['starting_equity_gross_paise'], 50_000_000)
            self.assertLessEqual(len(r['open_positions']), 9)
            # Locks enforced are the same set (values record stress mode, not
            # enforcement: slippage_1_5x_liquidity_scaled is False when the
            # run is not stress-tested, exactly as for the seven).
            self.assertEqual(set(r['microstructure_locks']),
                             {'slippage_1_5x_liquidity_scaled', 'circuit_carry_forward',
                              'gap_aware_stops', 'indicator_back_adjustment',
                              'eq_only_new_entries', 'higher_slippage_be_bz'})
        self.assertGreater(len(res['closed_trades']), 0)
        bar_by_day = {(str(x['symbol']), str(x['trade_date'])): x for _, x in df.iterrows()}
        for t in res['closed_trades']:
            self.assertIn(t['exit_slippage_rule'], ('tiered_adv_slippage', 'base_only_fallback'))
            open_px = float(bar_by_day[(t['symbol'], t['entry_date'])]['open'])
            self.assertEqual(t['qty'], cap.integer_shares(open_px, 50_000))
            ep, xp = int(round(t['entry_price'] * 100)), int(round(t['exit_price'] * 100))
            buy_fees = ae.fill_charges_paise(t['qty'] * ep, 'BUY')['total']
            sell_fees = ae.fill_charges_paise(t['qty'] * xp, 'SELL', overnight_dp=True)['total']
            self.assertEqual(t['net_pnl_paise'], (t['qty'] * xp - sell_fees) - (t['qty'] * ep + buy_fees))


class SpyDB:
    """Records every batch SQL statement for the shadow write-spy tests."""
    def __init__(self, db):
        self.db = db
        self.sql = []

    def batch(self, stm):
        for sql, args in stm:
            self.sql.append((sql, args))
        self.db.batch(stm)

    def query(self, sql, args=()):
        return self.db.query(sql, args)


class ShadowRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import candidate_shadow as cs
        cls.cs = cs
        days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
        db = fresh_db(ROOT)
        seed_pit(db, 'shadow', days, sorted(df.symbol.unique()), series='EQ')
        cls.spy = SpyDB(db)
        cls.out = cs.run_shadow(cls.spy, 'adaptive_v1', df, idx, days[:270], days[270:])
        cls.db = db

    def test_shadow_never_writes_turso_or_protected_namespaces(self):
        # Writes go only to the sidecar: adaptive_candidate_* tables, plus the
        # simulator's own PIT prefilter cache (daily_prefilter_universe) —
        # the same cache write the frozen-seven simulation performs. No Turso,
        # no paper_v1 / paper_v2 / paper_demo_v1 anywhere.
        allowed = ('adaptive_candidate_daily', 'adaptive_candidate_trades',
                   'daily_prefilter_universe')
        for sql, args in self.spy.sql:
            tokens = sql.replace('(', ' ').split()
            lowered = [t.strip().lower() for t in tokens]
            hits = [t for t in lowered if t in allowed]
            if sql.strip().upper().startswith(('INSERT', 'REPLACE', 'CREATE')):
                self.assertTrue(hits, sql)
                self.assertTrue(all(h in allowed for h in hits), sql)
            blob = json.dumps(args, default=str)
            for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1'):
                self.assertNotIn(ns, blob)
        self.assertEqual(set(self.out), {101, 102, 103, 104, 105, 106, 108})
        for s, r in self.out.items():
            self.assertEqual(r['status'], 'SHADOW_SIMULATED')
            self.assertEqual(r['mode'], 'shadow')
        with self.assertRaises(ValueError):
            self.cs.run_shadow(self.spy, 'paper_v2', *self._market_args())

    def _market_args(self):
        days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
        return df, idx, days[:270], days[270:]

    def test_shadow_places_no_orders(self):
        src = (LIVE_DIR / 'candidate_shadow.py').read_text()
        for marker in ('TursoRO', 'createClient', 'libsql', 'paper_order', 'place_order',
                       'submit_order', 'fyers'):
            self.assertNotIn(marker, src)
        reg = (ROOT / 'adaptive_candidates.py').read_text()
        for marker in ('TursoRO', 'createClient', 'libsql', 'paper_order', 'place_order'):
            self.assertNotIn(marker, reg)
        # run_shadow returns simulation summaries only — no order-like records.
        for r in self.out.values():
            self.assertNotIn('order_id', r)
            self.assertNotIn('filled', r)

    def test_candidate_results_are_marked_shadow_and_simulated(self):
        daily = self.db.query('SELECT * FROM adaptive_candidate_daily')
        self.assertGreater(len(daily), 0)
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in daily))
        self.assertEqual({r['strategy'] for r in daily}, {101, 102, 103, 104, 105, 106, 108})
        trades = self.db.query('SELECT * FROM adaptive_candidate_trades')
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in trades))


if __name__ == '__main__':
    unittest.main()
