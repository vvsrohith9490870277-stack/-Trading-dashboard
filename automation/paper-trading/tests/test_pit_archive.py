import sqlite3, sys, unittest
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pit_archive as pit


class MemDB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row

    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)

    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def _make_history(days=260, start_date=date(2025, 1, 1)):
    dates = []
    cur = start_date
    while len(dates) < days:
        if cur.weekday() < 5 and cur.isoformat() != '2026-11-08':
            dates.append(cur.isoformat())
        cur += timedelta(days=1)

    idx_rows = []
    stock_rows = []
    idx_px = 1000.0
    rel_px = 500.0
    weak_px = 500.0
    for i, d in enumerate(dates):
        idx_ret = 0.001 if i % 2 == 0 else -0.0004
        idx_px = idx_px * (1.0 + idx_ret)
        idx_rows.append({
            'trade_date': d,
            'symbol': 'NIFTY500',
            'open': idx_px * 0.998,
            'high': idx_px * 1.005,
            'low': idx_px * 0.995,
            'close': idx_px,
            'volume': 1_000_000,
        })
        # High-beta strong momentum stock (beta > 1.2, positive returns)
        rel_ret = idx_ret * 1.5 + 0.0012
        rel_px = rel_px * (1.0 + rel_ret)
        vol = 100_000 if i < days - 20 else 150_000
        stock_rows.append({
            'trade_date': d,
            'symbol': 'RELIANCE-EQ',
            'open': rel_px * 0.997,
            'high': rel_px * 1.006,
            'low': rel_px * 0.994,
            'close': rel_px,
            'volume': vol,
        })
        # Weak flat stock (fails beta / momentum)
        weak_px = weak_px * (1.0 + 0.0001)
        stock_rows.append({
            'trade_date': d,
            'symbol': 'WEAKCO-EQ',
            'open': weak_px * 0.999,
            'high': weak_px * 1.001,
            'low': weak_px * 0.999,
            'close': weak_px,
            'volume': 50_000,
        })
    return dates, pd.DataFrame(stock_rows), pd.DataFrame(idx_rows)


class PitArchiveTests(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
            if sql.strip():
                self.db.batch([(sql.strip(), ())])
        pit.init(self.db)

    def test_bhavcopy_series_archive_classic_and_udiff_and_pit_lock(self):
        pit.ingest_bhavcopy_series(
            self.db,
            [
                {'SYMBOL': 'RELIANCE', 'SERIES': 'EQ', 'OPEN': '1200', 'HIGH': '1220', 'LOW': '1190', 'CLOSE': '1210', 'TOTTRDQTY': '500000', 'ISIN': 'INE002A01018'},
                {'TckrSymb': 'WEAKCO', 'SctySrs': 'BE', 'OpnPric': '80', 'HghPric': '82', 'LwPric': '79', 'ClsPric': '81', 'TtlTradgVol': '12000'},
            ],
            trade_date='2025-12-10',
        )
        pit.ingest_bhavcopy_series(
            self.db,
            [
                {'SYMBOL': 'RELIANCE', 'SERIES': 'BE', 'OPEN': '1215', 'HIGH': '1225', 'LOW': '1200', 'CLOSE': '1205'},
            ],
            trade_date='2025-12-11',
        )
        self.assertEqual(pit.get_pit_series(self.db, 'RELIANCE-EQ', '2025-12-10', as_of_date='2025-12-10'), 'EQ')
        self.assertEqual(pit.get_pit_series(self.db, 'WEAKCO-EQ', '2025-12-10', as_of_date='2025-12-10'), 'BE')
        self.assertEqual(pit.get_pit_series(self.db, 'RELIANCE-EQ', '2025-12-11', as_of_date='2025-12-11'), 'BE')
        # Strict point-in-time guard prevents reading 2025-12-11 when as_of_date is 2025-12-10
        with self.assertRaises(ValueError):
            pit.get_pit_series(self.db, 'RELIANCE-EQ', '2025-12-11', as_of_date='2025-12-10')

    def test_multiyear_daily_prefilter_universe_pit_invariance_and_series_filter(self):
        dates, stock_df, idx_df = _make_history(days=260)
        target_dates = dates[-5:]
        for d in target_dates:
            pit.ingest_bhavcopy_series(
                self.db,
                [
                    {'symbol': 'RELIANCE-EQ', 'series': 'BE' if d == target_dates[-1] else 'EQ', 'close': 1000},
                    {'symbol': 'WEAKCO-EQ', 'series': 'EQ', 'close': 500},
                ],
                trade_date=d,
            )

        res = pit.build_multiyear_prefilter_archive(
            self.db,
            namespace='test_pit',
            trade_dates=target_dates,
            stock_df=stock_df,
            index_df=idx_df,
            as_of_date=target_dates[-1],
        )
        self.assertEqual(res['dates_archived'], 5)

        d0 = target_dates[0]
        u0 = pit.get_pit_prefilter_universe(self.db, 'test_pit', d0, as_of_date=d0, require_eq_series=True)
        self.assertEqual(u0['eligible_symbols'], ['RELIANCE-EQ'])
        self.assertEqual(u0['by_symbol']['WEAKCO-EQ']['status'], 'FAIL')

        # Truncate future data after d0 and re-archive into another namespace: must produce identical hash & universe
        trunc_stock = stock_df[stock_df['trade_date'] <= d0].copy()
        trunc_idx = idx_df[idx_df['trade_date'] <= d0].copy()
        pit.archive_daily_prefilter(self.db, 'test_pit_trunc', d0, trunc_stock, trunc_idx, as_of_date=d0)
        u0_trunc = pit.get_pit_prefilter_universe(self.db, 'test_pit_trunc', d0, as_of_date=d0, require_eq_series=True)
        self.assertEqual(u0['eligible_symbols'], u0_trunc['eligible_symbols'])
        self.assertEqual(u0['by_symbol']['RELIANCE-EQ']['metrics'], u0_trunc['by_symbol']['RELIANCE-EQ']['metrics'])

        # On target_dates[-1], RELIANCE-EQ transitioned to BE in Bhavcopy; require_eq_series=True excludes it for new entries
        u_last_all = pit.get_pit_prefilter_universe(self.db, 'test_pit', target_dates[-1], require_eq_series=False)
        u_last_eq = pit.get_pit_prefilter_universe(self.db, 'test_pit', target_dates[-1], require_eq_series=True)
        self.assertIn('RELIANCE-EQ', u_last_all['eligible_symbols'])
        self.assertNotIn('RELIANCE-EQ', u_last_eq['eligible_symbols'])

        # Future look-ahead on prefilter universe raises ValueError
        with self.assertRaises(ValueError):
            pit.get_pit_prefilter_universe(self.db, 'test_pit', target_dates[-1], as_of_date=d0)



class CanonicalSymbolTests(unittest.TestCase):
    def test_plain_and_existing_eq_suffix(self):
        self.assertEqual(pit.canonical_symbol('RELIANCE'), 'RELIANCE-EQ')
        self.assertEqual(pit.canonical_symbol('reliance-eq'), 'RELIANCE-EQ')
        self.assertEqual(pit.canonical_symbol('WEAKCO-EQ'), 'WEAKCO-EQ')

    def test_hyphenated_tickers_keep_the_full_base(self):
        self.assertEqual(pit.symbol_base('BAJAJ-AUTO'), 'BAJAJ-AUTO')
        self.assertEqual(pit.canonical_symbol('BAJAJ-AUTO'), 'BAJAJ-AUTO-EQ')
        self.assertEqual(pit.canonical_symbol('BAJAJ-AUTO-EQ'), 'BAJAJ-AUTO-EQ')
        self.assertEqual(pit.canonical_symbol('NAM-INDIA'), 'NAM-INDIA-EQ')
        self.assertEqual(pit.canonical_symbol('NAM-INDIA-EQ'), 'NAM-INDIA-EQ')
        self.assertEqual(pit.canonical_symbol('bajaj-auto'), 'BAJAJ-AUTO-EQ')

    def test_known_series_suffix_is_not_eaten_as_the_base(self):
        self.assertEqual(pit.canonical_symbol('HFCL-BE'), 'HFCL-BE')
        self.assertEqual(pit.canonical_symbol('RELINFRA-BE'), 'RELINFRA-BE')
        self.assertEqual(pit.symbol_base('HFCL-BE'), 'HFCL')
        # Archive convention: no series override -> still -EQ
        self.assertEqual(pit.canonical_symbol('HFCL'), 'HFCL-EQ')

    def test_market_symbol_uses_the_row_series(self):
        self.assertEqual(pit.market_symbol('HFCL', 'BE'), 'HFCL-BE')
        self.assertEqual(pit.market_symbol('STLTECH', 'BE'), 'STLTECH-BE')
        self.assertEqual(pit.market_symbol('TVSHLTD', 'BE'), 'TVSHLTD-BE')
        self.assertEqual(pit.market_symbol('BAJAJ-AUTO', 'EQ'), 'BAJAJ-AUTO-EQ')
        self.assertEqual(pit.market_symbol('NAM-INDIA', 'EQ'), 'NAM-INDIA-EQ')

    def test_normalize_does_not_treat_auto_as_a_series(self):
        row = pit.normalize_bhavcopy_row({
            'TckrSymb': 'BAJAJ-AUTO', 'SctySrs': 'EQ', 'TradDt': '2026-11-06',
            'OpnPric': '1', 'HghPric': '1', 'LwPric': '1', 'ClsPric': '1', 'TtlTradgVol': '1',
        })
        self.assertEqual(row['symbol'], 'BAJAJ-AUTO-EQ')
        self.assertEqual(row['series'], 'EQ')
        # No SctySrs: hyphenated name must not become series=AUTO
        row2 = pit.normalize_bhavcopy_row({
            'TckrSymb': 'NAM-INDIA', 'TradDt': '2026-11-06',
            'OpnPric': '1', 'HghPric': '1', 'LwPric': '1', 'ClsPric': '1', 'TtlTradgVol': '1',
        })
        self.assertEqual(row2['symbol'], 'NAM-INDIA-EQ')
        self.assertEqual(row2['series'], 'EQ')
        # BE still recorded in the series column; archive key stays -EQ
        row3 = pit.normalize_bhavcopy_row({
            'SYMBOL': 'HFCL', 'SERIES': 'BE', 'TIMESTAMP': '2026-11-06',
            'OPEN': '1', 'HIGH': '1', 'LOW': '1', 'CLOSE': '1', 'TOTTRDQTY': '1',
        })
        self.assertEqual(row3['symbol'], 'HFCL-EQ')
        self.assertEqual(row3['series'], 'BE')



if __name__ == '__main__':
    unittest.main()
