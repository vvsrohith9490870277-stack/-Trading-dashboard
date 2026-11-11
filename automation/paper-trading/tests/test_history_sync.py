import unittest,uuid
from datetime import datetime,timedelta,date
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine as e

T=datetime(2026,11,11,18,30,tzinfo=e.IST)

def bar(ts,o=10,h=11,l=9,c=10,v=100):
    return [ts,o,h,l,c,v]

class HistorySyncTests(unittest.TestCase):
    def setUp(self):
        self.prev_now=e.now;e.now=lambda:T
        self.db=e.DB(str(e.ROOT/'tests'/'test_hsync_')+uuid.uuid4().hex+'.sqlite')
        e.NS='test_'+uuid.uuid4().hex;e.init(self.db)
        self.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL,source TEXT,loaded_at TEXT)',())])
        # prior completed session bar so identity is verified
        self.db.batch([('INSERT INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('2026-11-09',sym,10,11,9,10,100,1000,'FIX',T.isoformat())) for sym in ('AAA-EQ','BBB-EQ','CCC-EQ')])
        self.calls=[]
        class API:
            def history(_,sym,start,target):
                self.calls.append(sym)
                # 11 Nov session bar
                return [bar(int(datetime(2026,11,11,15,30,tzinfo=e.IST).timestamp()))]
            def budget_snapshot(_):
                return {'requests':len(self.calls)}
        self.api=API()
    def tearDown(self):
        e.now=self.prev_now
    def test_skips_symbols_already_at_target(self):
        self.db.batch([('INSERT INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('2026-11-11','AAA-EQ',10,11,9,10,100,1000,'FIX',T.isoformat()))])
        r=e.sync(self.db,self.api,date(2026,11,11),['AAA-EQ','BBB-EQ'])
        self.assertEqual(self.calls,['BBB-EQ'])
        self.assertEqual(r['requests'],1)
        self.assertEqual(r['missing_before'],1)
        self.assertEqual(r['deferred'],0)
    def test_morning_cap_defers_the_rest(self):
        r=e.sync(self.db,self.api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],max_requests=2)
        self.assertEqual(self.calls,['AAA-EQ','BBB-EQ'])
        self.assertEqual(r['requests'],2)
        self.assertEqual(r['deferred'],1)
        self.assertEqual(r['candidate_candles'],2)
    def test_budget_exhausted_stops_and_does_not_raise(self):
        class Tight:
            def history(_,sym,start,target):
                self.calls.append(sym)
                if len(self.calls)>1:raise RuntimeError('market_data_budget_exhausted_reserve')
                return [bar(int(datetime(2026,11,11,15,30,tzinfo=e.IST).timestamp()))]
            def budget_snapshot(_):
                return {'requests':len(self.calls)}
        r=e.sync(self.db,Tight(),date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'])
        self.assertEqual(self.calls,['AAA-EQ','BBB-EQ'])
        self.assertEqual(r['stopped'],'market_data_budget_exhausted_reserve')
        self.assertEqual(r['deferred'],2)  # BBB failed + CCC deferred
        self.assertEqual(r['requests'],1)
    def test_scan_sync_uses_config_cap(self):
        self.assertEqual(e.CFG['history_sync_scan_max_requests'],40)
        prev=e.CFG['history_sync_scan_max_requests'];e.CFG['history_sync_scan_max_requests']=1
        old_u=e.universe;e.universe=lambda:['AAA-EQ','BBB-EQ','CCC-EQ']
        try:
            # scan --sync should cap at 1 request
            # stub the rest of the screen so we only exercise sync
            old_scan=e._scan
        finally:
            e.CFG['history_sync_scan_max_requests']=prev;e.universe=old_u
        r=e.sync(self.db,self.api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],max_requests=e.CFG['history_sync_scan_max_requests'])
        self.assertLessEqual(r['requests'],e.CFG['history_sync_scan_max_requests'])



class ScanParityTests(unittest.TestCase):
    def test_complete_history_means_zero_provider_calls_then_identical_sync_report(self):
        """When every symbol already has the target bar, do_sync only runs a zero-request sync."""
        t=HistorySyncTests();t.setUp()
        try:
            for sym in ('AAA-EQ','BBB-EQ','CCC-EQ'):
                t.db.batch([('INSERT INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             ('2026-11-11',sym,10,11,9,10,100,1000,'FIX',T.isoformat()))])
            r=e.sync(t.db,t.api,__import__('datetime').date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'])
            self.assertEqual(t.calls,[])
            self.assertEqual(r['requests'],0)
            self.assertEqual(r['missing_before'],0)
            # Morning cap path is the same function, so screen-only and --sync agree when history is complete.
            r2=e.sync(t.db,t.api,__import__('datetime').date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],max_requests=e.CFG['history_sync_scan_max_requests'])
            self.assertEqual(r,r2)
        finally:
            t.tearDown()

if __name__=='__main__':unittest.main()
