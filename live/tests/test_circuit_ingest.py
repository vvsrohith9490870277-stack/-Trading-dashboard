import sqlite3, sys, unittest
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LIVE = HERE.parent
LIB = LIVE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(LIVE))
sys.path.insert(0, str(LIB))

import circuit_bands as cb  # noqa: E402


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


FIXTURE = HERE / 'fixtures' / 'sec_list-2026-11-06.csv'


def _stock():
    rows = []
    prev, day = '2026-11-05', '2026-11-06'
    # prev close 100 for the lock/free names; 11-06 OHLC set to hit or miss the 5% band.
    specs = {
        'LOCKUP-EQ': dict(prev=100, o=101, h=105, l=99, c=105),
        'LOCKDN-EQ': dict(prev=100, o=99, h=101, l=95, c=95),
        'FREE-EQ': dict(prev=100, o=100, h=102, l=98, c=100),
        'FNO-EQ': dict(prev=100, o=100, h=110, l=90, c=100),  # would look locked if we invented 10%
        'BAJAJ-AUTO-EQ': dict(prev=1000, o=1010, h=1020, l=990, c=1015),
        'HFCL-BE': dict(prev=200, o=201, h=210, l=199, c=210),  # 5% of 200 = 210 upper
        'NOFILE-EQ': dict(prev=50, o=50, h=60, l=40, c=50),
    }
    for sym, s in specs.items():
        rows.append(dict(symbol=sym, trade_date=prev, open=s['prev'], high=s['prev'], low=s['prev'], close=s['prev'], volume=1))
        rows.append(dict(symbol=sym, trade_date=day, open=s['o'], high=s['h'], low=s['l'], close=s['c'], volume=1))
    return pd.DataFrame(rows)


class CircuitIngest(unittest.TestCase):
    def test_filename_dates(self):
        self.assertEqual(cb.date_from_sec_list_name('sec_list-2026-11-06.csv'), '2026-11-06')
        self.assertEqual(cb.date_from_sec_list_name('/tmp/sec_list_06112026.csv'), '2026-11-06')

    def test_ingest_pit_and_build_bands(self):
        db = MemDB()
        cb.init(db)
        rec = cb.ingest_file(db, FIXTURE, recorded_at='2026-11-06T19:00:00+05:30')
        self.assertEqual(rec['trade_date'], '2026-11-06')
        self.assertEqual(rec['ingested'], 6)
        self.assertEqual(rec['static_band'], 5)
        self.assertEqual(rec['unknown_band'], 1)
        # Hyphenated and BE keys preserved
        syms = {r['symbol'] for r in db.query('SELECT symbol,series FROM nse_price_band_archive')}
        self.assertIn('BAJAJ-AUTO-EQ', syms)
        self.assertIn('HFCL-BE', syms)
        self.assertNotIn('BAJAJ-EQ', syms)

        # PIT: as_of before the file date must not see it
        self.assertEqual(cb.get_bands_for_session(db, '2026-11-06', as_of_date='2026-11-05'), [])
        self.assertEqual(len(cb.get_bands_for_session(db, '2026-11-06', as_of_date='2026-11-06')), 6)

        # A later file must not be ingested when as_of is earlier
        raw = HERE / 'fixtures'
        skipped = cb.ingest_raw_dir(db, raw, as_of='2026-11-05')
        self.assertEqual(skipped['skipped_future'], 1)

        stock = _stock()
        sessions = ['2026-11-05', '2026-11-06']
        # as_of 11-05: 11-06 official file is in the future — all 11-06 bars stay UNKNOWN
        early = cb.build_bands_by_date_symbol(db, stock, sessions, as_of='2026-11-05')
        self.assertEqual(early.get(('2026-11-06', 'LOCKUP-EQ'), {}).get('circuit'), None)  # date after as_of dropped
        self.assertTrue(all(d != '2026-11-06' for d, _ in early))

        bands = cb.build_bands_by_date_symbol(db, stock, sessions, as_of='2026-11-06')
        self.assertEqual(bands[('2026-11-06', 'LOCKUP-EQ')]['circuit'], 'UPPER')
        self.assertEqual(bands[('2026-11-06', 'LOCKUP-EQ')]['upper'], 105.0)
        self.assertTrue(bands[('2026-11-06', 'LOCKUP-EQ')]['known'])
        self.assertEqual(bands[('2026-11-06', 'LOCKDN-EQ')]['circuit'], 'LOWER')
        self.assertEqual(bands[('2026-11-06', 'FREE-EQ')]['circuit'], 'NONE')
        # Dynamic / F&O: never invent a 10% lock even though the range would hit one
        fno = bands[('2026-11-06', 'FNO-EQ')]
        self.assertEqual(fno['circuit'], 'UNKNOWN')
        self.assertFalse(fno['known'])
        self.assertEqual(fno['reason'], 'no_static_band')
        self.assertIsNone(fno['upper'])
        # No official file for this name
        missing = bands[('2026-11-06', 'NOFILE-EQ')]
        self.assertEqual(missing['circuit'], 'UNKNOWN')
        self.assertEqual(missing['reason'], 'no_official_sec_list')
        # BE / hyphen keys
        self.assertEqual(bands[('2026-11-06', 'HFCL-BE')]['circuit'], 'UPPER')
        self.assertEqual(bands[('2026-11-06', 'BAJAJ-AUTO-EQ')]['circuit'], 'NONE')
        self.assertEqual(bands[('2026-11-06', 'BAJAJ-AUTO-EQ')]['band_pct'], 20.0)

        # No previous close -> UNKNOWN, no invented level
        stock2 = stock[stock['trade_date'] == '2026-11-06']
        no_prev = cb.build_bands_by_date_symbol(db, stock2, ['2026-11-06'], as_of='2026-11-06')
        self.assertEqual(no_prev[('2026-11-06', 'LOCKUP-EQ')]['circuit'], 'UNKNOWN')
        self.assertEqual(no_prev[('2026-11-06', 'LOCKUP-EQ')]['reason'], 'no_previous_close')

    def test_simulator_lock_never_invents_unknown(self):
        self.assertTrue(cb.simulator_lock('UPPER')['block_buy'])
        self.assertFalse(cb.simulator_lock('UPPER')['lock_sell'])
        self.assertTrue(cb.simulator_lock('LOWER')['lock_sell'])
        self.assertTrue(cb.simulator_lock('BOTH')['block_buy'] and cb.simulator_lock('BOTH')['lock_sell'])
        self.assertFalse(cb.simulator_lock('NONE')['block_buy'] or cb.simulator_lock('NONE')['lock_sell'])
        unk = cb.simulator_lock('UNKNOWN')
        self.assertFalse(unk['block_buy'])
        self.assertFalse(unk['lock_sell'])
        self.assertFalse(unk['known'])
        missing = cb.simulator_lock(None)
        self.assertFalse(missing['block_buy'] or missing['lock_sell'])
        self.assertFalse(missing['known'])

    def test_existing_parse_and_infer_api(self):
        # committed tests also cover this; keep the public API stable
        rows = cb.parse_sec_list(FIXTURE)
        self.assertTrue(any(r['symbol'] == 'FNO' and not r['static_band'] for r in rows))
        inferred = cb.infer_circuit(5, 100, high=105, low=99, close=105)
        self.assertEqual(inferred['circuit'], 'UPPER')
        self.assertIsNone(cb.infer_circuit(None, 100, high=105, low=95))
        doc = cb.document()
        self.assertIn('not used', doc['fyers'])

    def test_process_session_offline_no_fetch(self):
        db = MemDB()
        rec = cb.process_session_file('2026-11-06', HERE / 'fixtures', db=db, fetch=False)
        self.assertEqual(rec['status'], 'ingested')
        self.assertGreater(rec['ingested'], 0)
        missing = cb.process_session_file('2026-11-09', HERE / 'fixtures', db=db, fetch=False)
        self.assertEqual(missing['status'], 'missing')


if __name__ == '__main__':
    unittest.main()
