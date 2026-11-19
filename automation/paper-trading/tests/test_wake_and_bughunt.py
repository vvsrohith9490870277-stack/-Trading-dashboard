"""14 Nov bug-hunt: host-freeze (VM pause) handling, session-sticky ADV skips, evening sync budget waits, S7 missed-touch alert."""
import json,os,threading,time,unittest,uuid
from datetime import date,datetime,timedelta
from unittest.mock import patch
import pandas as pd
import test_engine as fixtures
import engine as e
import automation as a
T=fixtures.T

class FrozenTimeTests(unittest.TestCase):
 def test_frozen_since_same_boot_counts_the_pause(self):
  d={'mono_offset':a.mono_offset()-600,'boot_id':a.boot_id()}
  self.assertAlmostEqual(a.frozen_since(d),600,delta=2)
 def test_frozen_since_ignores_other_boot_and_missing(self):
  self.assertEqual(a.frozen_since({'mono_offset':a.mono_offset()-600,'boot_id':'another-boot'}),0.0)
  self.assertEqual(a.frozen_since({}),0.0)
  self.assertEqual(a.frozen_since({'mono_offset':'x','boot_id':a.boot_id()}),0.0)
 def row(self,age,detail):
  return {'heartbeat_at':(T-timedelta(seconds=age)).isoformat(),'status':'healthy','detail_json':json.dumps(detail)}
 def test_paused_worker_is_not_stale_but_hung_one_is(self):
  e.now=lambda:T
  paused=self.row(600,{'mono_offset':a.mono_offset()-590,'boot_id':a.boot_id()})
  self.assertFalse(a.heartbeat_is_stale(e,paused,T))
  hung=self.row(600,{'mono_offset':a.mono_offset()-100,'boot_id':a.boot_id()})
  self.assertTrue(a.heartbeat_is_stale(e,hung,T))
  rebooted=self.row(600,{'mono_offset':a.mono_offset()-590,'boot_id':'old-boot'})
  self.assertTrue(a.heartbeat_is_stale(e,rebooted,T),'a worker from before a VM restart must still turn stale')
  legacy=self.row(600,{})
  self.assertTrue(a.heartbeat_is_stale(e,legacy,T))

class FakeDB:
 def __init__(self):self.resets=[]
 def reset_helper(self,reason):self.resets.append(reason)

class WakeWatchTests(unittest.TestCase):
 def setUp(self):
  self.logs=[];self.real_log=e.log
  e.log=lambda kind,**d:self.logs.append((kind,d))
  e.now=lambda:T
 def tearDown(self):e.log=self.real_log
 def test_jump_in_wall_minus_monotonic_logs_gap_resets_helper_and_wakes_nap(self):
  offs=[1000.0];db=FakeDB()
  with patch.object(a,'mono_offset',side_effect=lambda:offs[0]),patch.object(a.al,'emit'):
   w=a.WakeWatch(e,db,on_wake=lambda:db.reset_helper('host_resume'),interval=0.01,threshold=20)
   with w:
    time.sleep(0.05);self.assertEqual(w.wakes,0)
    offs[0]+=313   # the VM was paused for 313 s
    began=time.monotonic();w.nap(5);woke_after=time.monotonic()-began
  self.assertEqual(w.wakes,1);self.assertLess(woke_after,2,'nap must end as soon as the host wakes')
  self.assertEqual(db.resets,['host_resume'])
  kinds=[k for k,_ in self.logs];self.assertIn('host_freeze_detected',kinds)
  gap=dict(self.logs[kinds.index('host_freeze_detected')][1]);self.assertAlmostEqual(gap['frozen_seconds'],313,delta=1);self.assertEqual(gap['reason'],'host_vm_paused')
 def test_small_scheduler_jitter_is_not_a_wake(self):
  offs=[1000.0]
  with patch.object(a,'mono_offset',side_effect=lambda:offs[0]),patch.object(a.al,'emit'):
   w=a.WakeWatch(e,FakeDB(),interval=0.01,threshold=20)
   with w:offs[0]+=3;time.sleep(0.06)
  self.assertEqual(w.wakes,0)

class WorkerWakeTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 @classmethod
 def tearDownClass(cls):fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp
 def test_failure_stranded_by_freeze_is_retried_at_once_and_post_wake_check_logged(self):
  watches=[];logs=[];real_log=e.log
  class Watch(a.WakeWatch):
   def __init__(s,*x,**k):super().__init__(*x,**k);watches.append(s)
   def run(s):pass   # driven by the test
  calls=[]
  def monitor(db,api):
   calls.append(1)
   if len(calls)==1:
    watches[0].woke(T-timedelta(seconds=400),T,400.0)
    raise RuntimeError('database_helper_write_unconfirmed')
   return {'status':'ok','fills':0,'fresh_quotes':3,'quotes':3}
  e.log=lambda kind,**d:(logs.append((kind,d)),real_log(kind,**d))[1]
  try:
   with patch.dict(e.CFG,{'worker_wake_watch':True,'poll_seconds':1}),patch.object(a,'WakeWatch',Watch),patch.object(e,'monitor',side_effect=monitor),patch.object(e,'pnl',return_value={}),patch.object(a.al,'emit'):
    began=time.monotonic();r=a.worker(e,self.db,sleep=time.sleep,max_polls=2,api_factory=lambda:object());took=time.monotonic()-began
  finally:e.log=real_log
  self.assertEqual(len(calls),2);self.assertLess(took,10,'the retry after a freeze must not wait out the backoff')
  kinds=[k for k,_ in logs];self.assertIn('host_freeze_detected',kinds);self.assertIn('post_wake_retry',kinds);self.assertIn('post_wake_check',kinds)
  check=dict(logs[kinds.index('post_wake_check')][1]);self.assertEqual(check['frozen_seconds'],400.0);self.assertEqual(check['status'],'ok')
 def test_ordinary_failure_still_backs_off(self):
  slept=[]
  def monitor(db,api):raise RuntimeError('TURSO_OPERATION_FAILED')
  with patch.dict(e.CFG,{'worker_wake_watch':True}),patch.object(e,'monitor',side_effect=monitor),patch.object(a.al,'emit'):
   a.worker(e,self.db,sleep=lambda s:slept.append(s),max_polls=1,api_factory=lambda:object())
  self.assertTrue(slept and slept[0]>0)

class StickySkipTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 @classmethod
 def tearDownClass(cls):fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp
 signal=fixtures.PaperTests.signal
 seed_adv=fixtures.PaperTests.seed_adv
 def test_adv_block_is_evaluated_once_per_session_and_exits_are_never_skipped(self):
  s=self.signal(symbol='TCS-EQ',strategy=2);self.seed_adv('TCS-EQ',1000)   # far below the ADV floor
  class Feed:
   def quotes(_,requested):return {sym:{'symbol':sym,'price':100,'quote_at':T.isoformat(),'provider_status':'ok'} for sym in requested}
  real=e.fill;calls=[]
  def counted(db,sig,q,t):calls.append(sig['id']);return real(db,sig,q,t)
  e._SESSION_SKIPS.clear()
  with patch.object(e,'fill',side_effect=counted):
   r1=e.monitor(self.db,Feed());r2=e.monitor(self.db,Feed())
  self.assertEqual(calls.count(s['id']),1,'identical ADV re-check skipped on later polls of the same session')
  self.assertEqual(r2.get('session_sticky_skips'),1)
  rows=self.db.query("SELECT cause FROM paper_decisions WHERE namespace=? AND correlation_id=?",(e.NS,s['id']));self.assertEqual([x['cause'] for x in rows],['ADV_FILTER_SKIP'])
  self.assertFalse(e._sticky_skip(T.date().isoformat(),{'side':'SELL','id':s['id']}))
  self.assertFalse(e._sticky_skip('2026-11-10',{'side':'BUY','id':s['id']}),'a new session re-evaluates')

class SyncBudgetWaitTests(unittest.TestCase):
 def setUp(self):
  self.prev_now=e.now;e.now=lambda:datetime(2026,11,11,18,0,tzinfo=e.IST)
  self.db=e.DB(str(e.ROOT/'tests'/'test_hsync_')+uuid.uuid4().hex+'.sqlite');self.path=self.db.test_database
  e.NS='test_'+uuid.uuid4().hex;e.init(self.db)
  self.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL,source TEXT,loaded_at TEXT)',())])
  self.db.batch([('INSERT INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('2026-11-10',sym,10,11,9,10,100,1000,'FIX','x')) for sym in ('AAA-EQ','BBB-EQ','CCC-EQ')])
 def tearDown(self):
  e.now=self.prev_now
  try:os.remove(self.path)
  except OSError:pass
 def api(self,fail):
  calls=[]
  class API:
   def history(_,sym,start,target):
    calls.append(sym);err=fail(len(calls))
    if err:raise RuntimeError(err)
    return [[int(datetime(2026,11,11,15,30,tzinfo=e.IST).timestamp()),10,11,9,10,100]]
   def budget_snapshot(_):return {'requests':len(calls)}
  return API(),calls
 def test_evening_job_waits_out_the_minute_window(self):
  api,calls=self.api(lambda n:'market_data_budget_exhausted_minute' if n in (2,3) else None);slept=[]
  r=e.sync(self.db,api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],sleep=slept.append,wait_budget=True)
  self.assertIsNone(r['stopped']);self.assertEqual(r['deferred'],0);self.assertEqual(r['candidate_candles'],3);self.assertEqual(len(slept),2);self.assertGreater(r['budget_wait_seconds'],0)
 def test_default_and_morning_paths_still_stop(self):
  api,calls=self.api(lambda n:'market_data_budget_exhausted_minute' if n==2 else None);slept=[]
  r=e.sync(self.db,api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],sleep=slept.append)
  self.assertEqual(r['stopped'],'market_data_budget_exhausted_minute');self.assertEqual(slept,[])
 def test_wait_is_bounded(self):
  api,calls=self.api(lambda n:None if n==1 else 'market_data_budget_exhausted_minute');slept=[]
  with patch.dict(e.CFG,{'history_sync_max_budget_wait_seconds':20,'history_sync_budget_wait_step_seconds':5}):
   r=e.sync(self.db,api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],sleep=slept.append,wait_budget=True)
  self.assertEqual(sum(slept),20);self.assertEqual(r['stopped'],'market_data_budget_exhausted_minute')
 def test_isolated_transport_failure_defers_symbol_and_continues(self):
  api,calls=self.api(lambda n:'market_data_transport_failed' if n==1 else None)
  r=e.sync(self.db,api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],sleep=lambda s:None,wait_budget=True)
  self.assertEqual(r['transport_failed'],['AAA-EQ']);self.assertEqual(r['candidate_candles'],2);self.assertEqual(r['deferred'],1)
 def test_repeated_transport_failures_still_abort(self):
  api,calls=self.api(lambda n:'market_data_transport_failed')
  with self.assertRaises(RuntimeError):e.sync(self.db,api,date(2026,11,11),['AAA-EQ','BBB-EQ','CCC-EQ'],sleep=lambda s:None,wait_budget=True)

class MissedTouchTests(unittest.TestCase):
 def g(self,lows):
  return pd.DataFrame({'trade_date':['2026-11-11','2026-11-12','2026-11-13'],'low':lows,'close':[100,100,100]})
 def p(self,**k):return {'strategy':7,'symbol':'PDSL-EQ','high_water':100.0,'entry_at':'2026-11-11T09:15:00+05:30','pending_exit':0,**k}
 def test_touch_after_watermark_with_open_position_is_flagged(self):
  m=e.missed_trail_touch(self.p(),self.g([95,95,89.5]),'2026-11-12')
  self.assertEqual(m['bar_date'],'2026-11-13');self.assertEqual(m['threshold'],e.trail_level(100.0));self.assertEqual(m['price'],89.5)
 def test_no_flag_when_untouched_already_seen_carried_or_other_strategy(self):
  self.assertIsNone(e.missed_trail_touch(self.p(),self.g([95,95,91]),'2026-11-12'))
  self.assertIsNone(e.missed_trail_touch(self.p(),self.g([95,80,95]),'2026-11-12'),'bars at or before the watermark were already handled')
  self.assertIsNone(e.missed_trail_touch(self.p(pending_exit=1),self.g([95,95,80]),'2026-11-12'))
  self.assertIsNone(e.missed_trail_touch(self.p(strategy=1),self.g([95,95,80]),'2026-11-12'))

if __name__=='__main__':unittest.main()
