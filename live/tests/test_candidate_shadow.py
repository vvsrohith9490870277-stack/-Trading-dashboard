"""B3: candidate_shadow runner tests (live side).

Runs the shadow challengers offline on a synthetic market (make_market) with
an in-memory sidecar DB. No Turso, no network, no orders.
"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))

import candidate_shadow as cs
# adaptive_fixtures lives in automation/paper-trading/tests, but that
# directory must NOT go on sys.path: it also holds test_namespace_cutover,
# and live/tests holds a module of the same name, so inserting it breaks
# unittest discovery of live/tests (ImportError: module incorrectly
# imported from the automation copy). Load the fixtures by file path.
import importlib.util as _ilu
_FIXTURES_PATH = HERE.parent / 'automation' / 'paper-trading' / 'tests' / 'adaptive_fixtures.py'
_spec = _ilu.spec_from_file_location('adaptive_fixtures', _FIXTURES_PATH)
adaptive_fixtures = _ilu.module_from_spec(_spec)
sys.modules['adaptive_fixtures'] = adaptive_fixtures
_spec.loader.exec_module(adaptive_fixtures)
from adaptive_fixtures import fresh_db, make_market, seed_pit, trading_days


def _real_sector_universe(n_days=400):
    """40-symbol synthetic universe on REAL universe.csv symbols: 4 real
    Industries x 10 stocks, same growth pattern as _c106_universe (paper
    tests). Returns (bars_frame, sector_map, star_symbol). The sector
    values are the real Industry values from the loader, so attaching
    the loader's map to this frame is the same wiring the live flow does.
    Growth: Financial Services 0.0100/day (HOT), Capital Goods 0.0050
    (WARM), Healthcare 0.0010 (COOL), Chemicals 0.0000 (COLD) with a
    0.002/day star in Chemicals. First 260 sessions at the growth rate,
    then -0.5%/day."""
    import numpy as np
    smap = cs.load_universe_sector_map()
    by_ind = {}
    for sym, ind in sorted(smap.items()):
        by_ind.setdefault(ind, []).append(sym)
    inds = sorted(by_ind, key=lambda i: (-len(by_ind[i]), i))[:4]
    days = trading_days(n_days)
    rows = []
    specs = []
    for k in range(10):
        specs.append((by_ind[inds[0]][k], 0.0100 + k * 0.00005))
        specs.append((by_ind[inds[1]][k], 0.0050 + k * 0.00005))
        specs.append((by_ind[inds[2]][k], 0.0010 + k * 0.00005))
    for k in range(9):
        specs.append((by_ind[inds[3]][k], 0.0000 + k * 0.00001))
    star = by_ind[inds[3]][9]
    specs.append((star, 0.002))
    for sym, r in specs:
        px = 1000.0
        for i, d in enumerate(days):
            g = r if i < 260 else -0.005
            px *= (1 + g)
            o = px / (1 + g)
            rows.append({'symbol': sym, 'trade_date': d, 'open': round(o, 2),
                         'high': round(max(o, px) * 1.002, 2), 'low': round(min(o, px) * 0.998, 2),
                         'close': round(px, 2), 'volume': 300000})
    import pandas as pd
    return pd.DataFrame(rows), smap, star

# Snapshot sys.path right after this module's own imports: the regression
# test below asserts this module did not add the automation tests dir.
_SYS_PATH_AFTER_IMPORT = list(sys.path)


class SpyDB:
    def __init__(self, db):
        self.db = db
        self.sql = []

    def batch(self, stm):
        for sql, args in stm:
            self.sql.append((sql, args))
        self.db.batch(stm)

    def query(self, sql, args=()):
        return self.db.query(sql, args)


class CandidateShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        days, df, idx = make_market(n_days=280, seed=7, volume=300_000)
        cls.days, cls.df, cls.idx = days, df, idx
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db, 'shadow', days, sorted(df.symbol.unique()), series='EQ')
        cls.spy = SpyDB(db)
        cls.out = cs.run_shadow(cls.spy, 'adaptive_v1', df, idx, days[:260], days[260:])
        cls.db = db

    def test_import_does_not_pollute_sys_path(self):
        # Regression test for the G3 discovery failure: this module used to
        # insert automation/paper-trading/tests into sys.path at import time,
        # so unittest discovery of live/tests then resolved the duplicate
        # module name test_namespace_cutover to the automation copy and died
        # with ImportError ('incorrectly imported from ...'). The snapshot
        # is taken at this module's import time, so later imports by other
        # test modules cannot affect it.
        import sys
        tests_dir = str(HERE.parent / 'automation' / 'paper-trading' / 'tests')
        self.assertNotIn(tests_dir, _SYS_PATH_AFTER_IMPORT)

    def test_shadow_runs_all_nine_offline(self):
        self.assertEqual(set(self.out), {101, 102, 103, 104, 105, 106, 107, 108, 109})
        for s, r in self.out.items():
            self.assertEqual(r['status'], 'SHADOW_SIMULATED')
            self.assertEqual(r['mode'], 'shadow')
            self.assertTrue(r['simulated'])
            self.assertGreater(r['eval_days'], 0)

    def test_c109_sim_uses_atr_qty_not_fixed_qty(self):
        # End-to-end proof the simulator honours C109's ATR sizing: a
        # volatile single-symbol market (range +/-10 around a ~100 close,
        # ATR ~20) trades 125 shares — qty = 5000 / (2 x 20) — not the
        # frozen fixed size of 462 shares (50000 / 108). The position
        # value stays within the Rs 50,000 per-trade maximum.
        import pandas as pd
        closes = ([100.0] * 200
                  + [98, 96, 94, 92, 90, 88, 86, 84, 82, 80,
                     80, 82, 84, 86, 88, 90, 92, 94, 96, 98,
                     100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120,
                     118, 116, 114, 112, 110, 108, 106, 104, 102, 100,
                     98, 96, 94, 92, 90, 88, 86, 84, 82, 80]
                  + [80.0] * (280 - 251))
        rows = []
        for i, (d, c) in enumerate(zip(self.days, closes)):
            o = closes[i - 1] if i else c
            rows.append({'symbol': 'VOL-EQ', 'trade_date': d, 'open': o,
                         'high': round(c + 10, 2), 'low': round(c - 10, 2),
                         'close': c, 'volume': 3000000})
        vol = pd.DataFrame(rows)
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        # Seed the PIT universe under the namespace run_shadow actually
        # simulates in ('adaptive_v1'); seeding only 'shadow' would make
        # the simulator archive the prefilter on the fly instead.
        seed_pit(db, 'adaptive_v1', self.days, ['VOL-EQ'], series='EQ')
        out = cs.run_shadow(db, 'adaptive_v1', vol, self.idx,
                            self.days[:260], self.days[260:], strategies=[109])
        self.assertEqual(out[109]['status'], 'SHADOW_SIMULATED')
        trades = db.query('SELECT qty, entry_price FROM adaptive_candidate_trades '
                          'WHERE strategy=109 AND symbol=?', ('VOL-EQ',))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['qty'], 125)
        self.assertLess(trades[0]['qty'] * trades[0]['entry_price'], 50000)
        import sys as _sys
        _sys.path.insert(0, str(HERE.parent / 'automation' / 'paper-trading'))
        import capital as _cap
        self.assertLess(trades[0]['qty'],
                        _cap.integer_shares(trades[0]['entry_price'], 50000))

    def test_shadow_tables_only(self):
        # Every write targets adaptive_candidate_* (or the simulator's own PIT
        # prefilter cache). Nothing mentions a trading namespace.
        allowed = ('adaptive_candidate_daily', 'adaptive_candidate_trades',
                   'daily_prefilter_universe')
        for sql, args in self.spy.sql:
            tokens = [t.strip().lower() for t in sql.replace('(', ' ').split()]
            hits = [t for t in tokens if t in allowed]
            if sql.strip().upper().startswith(('INSERT', 'REPLACE', 'CREATE')):
                self.assertTrue(hits, sql)
            blob = json.dumps(args, default=str)
            for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1'):
                self.assertNotIn(ns, blob)

    def test_shadow_refuses_protected_namespace(self):
        for ns in ('paper_v1', 'paper_demo_v1', 'paper_v2'):
            with self.assertRaises(ValueError):
                cs.run_shadow(self.spy, ns, self.df, self.idx, self.days[:260], self.days[260:])

    def test_shadow_results_are_marked_shadow_and_simulated(self):
        daily = self.db.query('SELECT * FROM adaptive_candidate_daily')
        self.assertGreater(len(daily), 0)
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in daily))
        trades = self.db.query('SELECT * FROM adaptive_candidate_trades')
        self.assertTrue(all(r['mode'] == 'shadow' and r['simulated'] == 1 for r in trades))

    def test_shadow_uses_same_frozen_costs(self):
        # A shadow run starts from the same sleeve capital as Track A.
        import adaptive_engine as ae
        days, df, idx = make_market(n_days=300, seed=7, volume=300_000)
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db, 'shadow', days, sorted(df.symbol.unique()), series='EQ')
        import adaptive_candidates as ac
        res = ae.simulate_track_window(db, 'shadow', 102, dict(ac.CANDIDATE_STRATEGIES[102]['params']),
                                       df, idx, days[:200], days[200:],
                                       signal_evaluator=lambda s, d: ac.candidate_signals(s, d))
        self.assertEqual(res['starting_equity']['starting_equity_gross_paise'], 50_000_000)


class UniverseSectorMapTests(unittest.TestCase):
    """C106 sector map wiring (Grok, 2026-11-22): universe.csv Industry."""

    def test_universe_sector_map_covers_all_692(self):
        m = cs.load_universe_sector_map()
        self.assertEqual(len(m), 692)
        # Spot-checks (Grok's own examples): NSE list missed these.
        self.assertEqual(m['3MINDIA-EQ'], 'Diversified')
        self.assertEqual(m['SANOFI-EQ'], 'Healthcare')
        self.assertEqual(m['PGHH-EQ'], 'Fast Moving Consumer Goods')
        for k, v in m.items():
            self.assertIsInstance(k, str)
            self.assertTrue(k)
            self.assertIsInstance(v, str)
            self.assertTrue(v)
            self.assertIn('-', k)  # db_symbol style, e.g. '3MINDIA-EQ'

    def test_universe_sector_map_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            cs.load_universe_sector_map('/nonexistent/universe.csv')

    def test_universe_sector_map_empty_file_raises(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False) as f:
            f.write('Company Name,Industry,Symbol,Series,ISIN Code,db_symbol\n')
            path = f.name
        try:
            with self.assertRaises(ValueError):
                cs.load_universe_sector_map(path)
        finally:
            os.unlink(path)

    def test_with_sector_column_maps_by_db_symbol(self):
        import pandas as pd
        m = cs.load_universe_sector_map()
        df = pd.DataFrame([
            {'symbol': '3MINDIA-EQ', 'trade_date': '2024-01-01', 'close': 100.0},
            {'symbol': 'UNKNOWN-EQ', 'trade_date': '2024-01-01', 'close': 100.0},
            {'symbol': 'NIFTY500', 'trade_date': '2024-01-01', 'close': 1000.0},
        ])
        out = cs.with_sector_column(df, m)
        self.assertEqual(out['sector'].iloc[0], 'Diversified')
        self.assertTrue(pd.isna(out['sector'].iloc[1]))  # unknown: no sector
        self.assertTrue(pd.isna(out['sector'].iloc[2]))  # index: no sector
        self.assertNotIn('sector', df.columns)  # original frame untouched

    def test_c106_enters_with_sector_column_from_universe_csv(self):
        # The universe frame carries the Industry column from universe.csv
        # (attached via the loader map); C106 enters on the frame column
        # alone, with NO explicit sector_map passed.
        import adaptive_candidates as ac
        uni, smap, star = _real_sector_universe()
        frame = cs.with_sector_column(uni, smap)
        self.assertEqual(frame[frame['symbol'] == star]['sector'].iloc[0],
                         smap[star])
        sdf = frame[frame['symbol'] == star].reset_index(drop=True)
        out = ac.candidate_signals(106, sdf, universe=frame)
        entries = out[out['entry_condition']]['trade_date'].astype(str).tolist()
        self.assertEqual(entries, ['2025-01-01'])
        # Sector-adjusted return matches the _c106_universe pattern.
        closes = ac._universe_closes(frame)
        adj = ac._sector_adjusted_frame(closes, smap)
        self.assertAlmostEqual(adj.loc['2025-01-01', star], 0.5195, places=3)


    def test_run_shadow_passes_sector_map_to_c106(self):
        # End-to-end: the live shadow flow passes the sector map through
        # to C106. With the map, C106 enters (9 open positions, the sleeve
        # filled); without it, C106 enters nothing (0 open positions).
        uni, smap, star = _real_sector_universe(n_days=300)
        days = sorted(uni['trade_date'].astype(str).unique())
        import pandas as pd
        idx = pd.DataFrame([{'symbol': 'NIFTY500', 'trade_date': d, 'open': 1000 + i,
                             'high': 1001 + i, 'low': 999 + i, 'close': 1000 + i,
                             'volume': 1} for i, d in enumerate(days)])
        syms = sorted(uni['symbol'].unique())
        db = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db, 'shadow', days, syms, series='EQ')
        out = cs.run_shadow(db, 'adaptive_v1', uni, idx, days[:260], days[260:],
                            strategies=[106], sector_map=smap)
        self.assertEqual(out[106]['status'], 'SHADOW_SIMULATED')
        self.assertGreater(out[106]['open_positions'], 0)
        daily = db.query('SELECT * FROM adaptive_candidate_daily WHERE strategy=106')
        self.assertGreater(len(daily), 0)
        # Control: no map and no sector column on the frame -> nothing.
        db2 = fresh_db(HERE.parent / 'automation' / 'paper-trading')
        seed_pit(db2, 'shadow', days, syms, series='EQ')
        out2 = cs.run_shadow(db2, 'adaptive_v1', uni, idx, days[:260], days[260:],
                             strategies=[106])
        self.assertEqual(out2[106]['open_positions'], 0)
        self.assertEqual(out2[106]['closed_trades'], 0)


if __name__ == '__main__':
    unittest.main()
