import json, sys, unittest, uuid
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as e
import reference_feeds as rf

T = datetime(2026, 11, 9, 10, tzinfo=e.IST)
ISIN = e.ISINS['RELIANCE-EQ']
MASTER = '\n'.join([
    f'1,RELIANCE,0,1,0.05,{ISIN},0915-1530,2026-11-08,,NSE:RELIANCE-EQ,10,10,1,RELIANCE,1,-1.0,XX,1,None,1,2.0',
    f"1,TCS,0,1,0.05,{e.ISINS['TCS-EQ']},0915-1530,2026-11-08,,NSE:TCS-BE,10,10,1,TCS,1,-1.0,XX,1,None,1,2.0",
])


def fixture_fetch(url, as_json=True):
    cfg = e.CFG['reference_feeds']
    if url.startswith(cfg['nse_corporate_actions_url']):
        return [
            {'symbol': 'RELIANCE', 'isin': ISIN, 'exDate': '09-Nov-2026', 'subject': 'Bonus 1:1'},
            {'symbol': 'RELIANCE', 'isin': ISIN, 'exDate': '09-Nov-2026', 'subject': 'Interim Dividend - Rs 5 Per Share'},
            {'symbol': 'RELIANCE', 'isin': 'INE000000000', 'exDate': '09-Nov-2026', 'subject': 'Bonus 1:1'},
            {'symbol': 'RELIANCE', 'isin': ISIN, 'series': 'GS', 'exDate': '09-Nov-2026', 'subject': 'Interest Payment'},
            {'symbol': 'NOTINUNIVERSE', 'isin': 'X', 'exDate': '09-Nov-2026', 'subject': 'Bonus 1:1'},
            {'symbol': 'RELIANCE', 'isin': ISIN, 'exDate': '30-Dec-2026', 'subject': 'Bonus 1:1'},
        ]
    if url == cfg['nse_asm_url']:
        return {'longterm': {'data': [{'symbol': 'INFY', 'isin': e.ISINS['INFY-EQ'], 'asmSurvIndicator': 'Stage I'}]}, 'shortterm': {'data': []}}
    if url == cfg['nse_gsm_url']:
        raise RuntimeError('http_403')
    if url == cfg['fyers_symbol_master_url']:
        return MASTER
    raise AssertionError(url)


class ReferenceFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = e.ROOT / 'tests' / ('test_' + uuid.uuid4().hex + '.sqlite'); cls.db = e.DB(str(cls.path))
        e.NS = 'test_fixture'; e.now = lambda: T; e.init(cls.db)
        cls.db.batch([('CREATE TABLE daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)', ())])

    @classmethod
    def tearDownClass(cls):
        cls.path.unlink(missing_ok=True)

    def test_classify_never_invents_extraordinary_threshold(self):
        self.assertEqual(rf.classify('Bonus 1:2'), [('BONUS', 3, 2, None)])
        self.assertEqual(rf.classify('Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share'), [('SPLIT', 5, 1, None)])
        self.assertEqual(rf.classify('Special Dividend - Rs 50 Per Share'), [('EXTRAORDINARY_DIVIDEND_REVIEW', None, None, None)])
        self.assertEqual(rf.classify('Rights 1:4 @ Premium Rs 10'), [('RIGHTS', None, None, None)])
        self.assertEqual(rf.classify('Annual General Meeting'), [])

    def test_refresh_writes_guards_and_reports_unavailable_sources(self):
        e.NS = 'test_' + uuid.uuid4().hex
        self.db.batch([('DELETE FROM paper_corporate_actions', ()), ('DELETE FROM paper_price_bands', ())])
        r = rf.refresh(e, self.db, '2026-11-09', fetch=fixture_fetch)
        self.assertEqual(r['sources']['corporate_actions']['rows_written'], 2)
        self.assertEqual(r['sources']['corporate_actions']['skipped'], {'not_in_universe': 1, 'not_equity_series': 1, 'isin_differs_symbol_matched': 1, 'outside_window': 1, 'not_a_position_event': 0})
        self.assertEqual(r['sources']['gsm']['status'], 'unavailable')
        rows = {(x['kind'], x['ratio_num'], x['ratio_den'], x['cash_paise']) for x in self.db.query('SELECT * FROM paper_corporate_actions WHERE symbol=?', ('RELIANCE-EQ',))}
        self.assertEqual(rows, {('BONUS', 2, 1, None), ('CASH_DIVIDEND', None, None, 500)})
        bands = {x['symbol']: (x['series'], x['surveillance']) for x in self.db.query('SELECT * FROM paper_price_bands WHERE trade_date=?', ('2026-11-09',))}
        self.assertEqual(bands, {'INFY-EQ': (None, 'ASM'), 'TCS-EQ': ('BE', None)})
        self.assertEqual(e.market_guard({'price': 100, 'open': 99, 'high': 101, 'low': 98}, e.band_for(self.db, 'INFY-EQ', '2026-11-09'))['surveillance'], 'ASM')
        self.assertEqual(e.market_guard({'price': 100, 'open': 99, 'high': 101, 'low': 98}, e.band_for(self.db, 'TCS-EQ', '2026-11-09'))['series'], 'BE')

    def test_reference_day_targets_next_session_after_close(self):
        from datetime import date
        self.assertEqual(e.reference_day(datetime(2026, 11, 9, 8, 45, tzinfo=e.IST)), date(2026, 11, 9))
        self.assertEqual(e.reference_day(datetime(2026, 11, 9, 17, 0, tzinfo=e.IST)), date(2026, 11, 11))
        self.assertEqual(e.reference_day(datetime(2026, 11, 10, 9, 0, tzinfo=e.IST)), date(2026, 11, 11))

    def test_total_feed_outage_is_reported_not_raised(self):
        def down(url, as_json=True):
            raise RuntimeError('http_403')
        r = rf.refresh(e, self.db, '2026-11-09', fetch=down)
        self.assertTrue(all(v['status'] == 'unavailable' for v in r['sources'].values()))
        self.assertEqual(r['guard_rows_written'], 0)


if __name__ == '__main__':
    unittest.main()
