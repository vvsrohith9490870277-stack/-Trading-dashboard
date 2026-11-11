import json,os,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine as e

T=datetime(2026,11,12,10,0,tzinfo=e.IST)

class BudgetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.NamedTemporaryFile(delete=False);self.tmp.close()
  self.prev=dict(e.MD)
  e.MD['budget_state_path']=self.tmp.name
  e.MD['budget_requests_per_second']=10
  e.MD['budget_requests_per_minute']=20
  e.MD['budget_requests_per_day']=5
  e.MD['budget_reserve_for_session_monitor']=2
  self.clock={'t':T}
  self.b=e.RequestBudget(self.tmp.name,clock=lambda:self.clock['t'])
 def tearDown(self):
  e.MD.clear();e.MD.update(self.prev)
  Path(self.tmp.name).unlink(missing_ok=True)
 def test_records_and_daily_exhaustion(self):
  for i in range(5):
   self.clock['t']=T+timedelta(seconds=i+10);self.b.check('quotes');self.b.record('quotes')
  self.clock['t']=T+timedelta(seconds=30)
  with self.assertRaisesRegex(RuntimeError,'market_data_budget_exhausted_day'):self.b.check('quotes')
  snap=self.b.snapshot();self.assertEqual(snap['requests'],5);self.assertEqual(snap['remaining_today'],0);self.assertEqual(snap['by_route']['quotes'],5)
 def test_history_cannot_spend_session_reserve(self):
  # day=5, reserve=2, so history may use 3
  for i in range(3):
   self.clock['t']=T+timedelta(seconds=i+10);self.b.check('history');self.b.record('history')
  self.clock['t']=T+timedelta(seconds=40)
  with self.assertRaisesRegex(RuntimeError,'market_data_budget_exhausted_reserve'):self.b.check('history')
  self.b.check('quotes')  # monitor may still use the reserve
 def test_minute_cap(self):
  e.MD['budget_requests_per_minute']=3
  for i in range(3):
   self.clock['t']=T+timedelta(seconds=i);self.b.check('quotes');self.b.record('quotes')
  self.clock['t']=T+timedelta(seconds=10)
  with self.assertRaisesRegex(RuntimeError,'market_data_budget_exhausted_minute'):self.b.check('quotes')
  self.clock['t']=T+timedelta(seconds=71);self.b.check('quotes')
 def test_new_ist_day_resets(self):
  self.b.check('quotes');self.b.record('quotes')
  self.clock['t']=T+timedelta(days=1);self.b.check('quotes')
  snap=self.b.snapshot();self.assertEqual(snap['day'],'2026-11-13');self.assertEqual(snap['requests'],0)

class ProbeReportTests(unittest.TestCase):
 def test_config_budget_is_below_documented_standard_limits(self):
  documented=e.MD['fyers_documented_limits']
  self.assertEqual(documented,{'requests_per_second':5,'requests_per_minute':50,'requests_per_day':5000})
  self.assertLess(e.MD['budget_requests_per_second'],documented['requests_per_second'])
  self.assertLess(e.MD['budget_requests_per_minute'],documented['requests_per_minute'])
  self.assertLess(e.MD['budget_requests_per_day'],documented['requests_per_day'])
  self.assertEqual(e.MD['quotes_freshness'],'exchange_ltt_or_session_observation')
  self.assertEqual(e.MD['depth_when'],['official_open_or_auction_missing'])
  self.assertLessEqual(e.MD['quotes_max_symbols_per_request'],50)

if __name__=='__main__':unittest.main()
