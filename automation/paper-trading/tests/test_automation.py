import unittest,json,uuid,fcntl,os
from pathlib import Path
from datetime import datetime,timedelta
from decimal import Decimal,ROUND_HALF_UP
from unittest.mock import patch
import test_engine as fixtures
import engine as e
import automation as a
T=fixtures.T
class AutomationTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  fixtures.PaperTests.setUpClass.__func__(cls)
  have={r['name'] for r in cls.db.query('PRAGMA table_info(daily_ohlcv)')}
  for name,kind in [('open','REAL'),('high','REAL'),('low','REAL'),('volume','REAL'),('turnover','REAL'),('source','TEXT'),('loaded_at','TEXT')]:
   if name not in have:cls.db.batch([(f'ALTER TABLE daily_ohlcv ADD COLUMN {name} {kind}',())])
 def setUp(self):
  fixtures.PaperTests.setUp(self)
  # Existing EOD tests stay closed if bhavcopy is not injected; never hit NSE.
  self._oc=patch.object(a.oc,'closes_for',return_value={'status':'unavailable','reason':'test_default_no_network','closes':{},'source':None})
  self._oc.start();self.addCleanup(self._oc.stop)
 seed_adv=fixtures.PaperTests.seed_adv;signal=fixtures.PaperTests.signal
 quote=fixtures.PaperTests.quote
 def fees(self,price,side='BUY',prior=0,priorBuy=0,dp=False):return self.db.call({'action':'estimate','price':price,'quantity':1,'side':side,'prior':prior,'priorBuy':priorBuy,'dp':dp})
 def test_independent_decimal_fee_vectors(self):
  for price in ['0.01','100','2500','2600','6666.67','100000','9999999.99']:
   v=Decimal(price)*100;roundp=lambda x:int(x.quantize(Decimal('1'),rounding=ROUND_HALF_UP));expected={'brokerage':min(2000,roundp(v*Decimal('.003'))),'exchange':roundp(v*Decimal('.000030699')),'sebi':roundp(v*Decimal('.000001')),'ipft':roundp(v*Decimal('.000000001')),'clearing':0,'stt':roundp(v*Decimal('.001')/100)*100,'stamp':roundp(v*Decimal('.00015')),'dp':0,'dp_gst':0};expected['gst']=roundp(sum(expected[k] for k in ['brokerage','exchange','sebi','ipft'])*Decimal('.18'));expected['total']=sum(expected.values());self.assertEqual(self.fees(price),expected)
 def test_documented_delivery_roundtrip(self):
  buy=self.fees(2500);sell=self.fees(2600,'SELL',dp=True);self.assertEqual(buy['total'],1232);self.assertEqual(sell['total'],2705);self.assertEqual(10000-buy['total']-sell['total'],6063)
 def test_daily_stt_and_stamp_aggregation(self):
  first=self.fees(2500);second=self.fees(2500,prior=250000,priorBuy=250000);self.assertEqual(first['stt']+second['stt'],500);self.assertEqual(first['stamp']+second['stamp'],75)
 def test_fractional_price_rejected(self):
  with self.assertRaises(RuntimeError):self.fees('100.123')
 def test_fee_fill_atomic_rollback(self):
  self.db.batch([('CREATE TRIGGER fail_cost BEFORE INSERT ON paper_fill_costs BEGIN SELECT RAISE(ABORT,\'fixture\'); END',())])
  try:
   with self.assertRaises(RuntimeError):e.fill(self.db,self.signal(),self.quote(),T)
   for table in ['paper_fills','paper_positions','paper_fill_costs','paper_entry_cohorts']:self.assertFalse(self.db.query(f'SELECT * FROM {table} WHERE namespace=?',(e.NS,)))
   self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=?',(e.NS,))[0]['status'],'pending')
  finally:self.db.batch([('DROP TRIGGER fail_cost',())])
 def test_replay_cannot_double_charge(self):
  s=self.signal();e.fill(self.db,s,self.quote(2500),T);e.fill(self.db,s,self.quote(2500),T);self.assertEqual(len(self.db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,))),1)
 def test_overnight_dp_once_across_strategies(self):
  for strategy in [1,2]:e.fill(self.db,self.signal(strategy=strategy),self.quote(2500),T)
  later=T.replace(day=11);e.now=lambda:later
  for strategy in [1,2]:e.fill(self.db,self.signal('SELL',strategy=strategy,eligible='2026-11-11'),{'price':2600,'quote_at':later.isoformat()},later)
  cost=[json.loads(x['components_json']) for x in self.db.query("SELECT components_json FROM paper_fill_costs WHERE namespace=? AND side='SELL' ORDER BY fill_id",(e.NS,))]
  self.assertEqual([x['dp'] for x in cost],[625,625]);self.assertEqual([x['dp_gst'] for x in cost],[112,113]);self.assertEqual(sum(x['dp']+x['dp_gst'] for x in cost),1475)
  debits=self.db.query('SELECT amount_paise FROM paper_dp_debits WHERE namespace=?',(e.NS,));self.assertEqual([x['amount_paise'] for x in debits],[1475])
  second=json.loads(self.db.query("SELECT payload_json FROM paper_decisions WHERE namespace=? AND decision_type='EXIT' ORDER BY seq DESC LIMIT 1",(e.NS,))[0]['payload_json'])
  self.assertEqual(second['dp_rule'],'pro_rata_turnover_once_per_isin_day');self.assertEqual(second['dp_adjustments'][0]['dp_before'],1250);self.assertEqual(second['dp_adjustments'][0]['dp_after'],625)
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_overnight_dp_splits_unequal_turnover(self):
  e.fill(self.db,self.signal(strategy=1),self.quote(2500),T);e.fill(self.db,self.signal(strategy=2),self.quote(100),T)
  later=T.replace(day=11);e.now=lambda:later
  e.fill(self.db,self.signal('SELL',strategy=1,eligible='2026-11-11'),{'price':2600,'quote_at':later.isoformat()},later)
  e.fill(self.db,self.signal('SELL',strategy=2,eligible='2026-11-11'),{'price':200,'quote_at':later.isoformat()},later)
  rows=self.db.query("SELECT turnover_paise,components_json FROM paper_fill_costs WHERE namespace=? AND side='SELL' ORDER BY fill_id",(e.NS,))
  def split(charge,weights):
   total=sum(weights);used=0;out=[]
   for i,weight in enumerate(weights):
    share=charge-used if i==len(weights)-1 else charge*weight//total;used+=0 if i==len(weights)-1 else share;out.append(share)
   return out
  weights=[x['turnover_paise'] for x in rows];cost=[json.loads(x['components_json']) for x in rows]
  self.assertEqual([x['dp'] for x in cost],split(1250,weights));self.assertEqual([x['dp_gst'] for x in cost],split(225,weights));self.assertNotEqual(weights[0],weights[1])
 def test_same_day_no_dematerialization_fee(self):
  e.fill(self.db,self.signal(),self.quote(),T);e.fill(self.db,self.signal('SELL'),self.quote(110),T);self.assertFalse(self.db.query('SELECT * FROM paper_dp_debits WHERE namespace=?',(e.NS,)))
 def test_open_cost_net_realized_and_estimate_separation(self):
  filled=e.fill(self.db,self.signal(),self.quote(2500),T);self.assertEqual(filled['qty'],20);cost=self.db.call({'action':'estimate','price':filled['price'],'quantity':20,'side':'BUY','prior':0,'priorBuy':0,'dp':False});r=a.finance(e,self.db);self.assertEqual(r['gross_total_paise'],0);self.assertEqual(r['net_realized_paise'],0);self.assertEqual(r['net_unrealized_accrued_paise'],-cost['total']);self.assertEqual(r['net_total_accrued_paise'],-cost['total']);self.assertLess(r['net_after_estimated_exit_paise'],r['net_total_accrued_paise']);self.assertIsNone(r['funded_capital'])
 def test_closed_net_allocates_entry_and_exit_once(self):
  e.fill(self.db,self.signal(),self.quote(2500),T);e.fill(self.db,self.signal('SELL'),self.quote(2600),T);r=a.finance(e,self.db);self.assertGreater(r['gross_realized_paise'],0);self.assertLess(r['gross_realized_paise'],200000);self.assertEqual(r['net_realized_paise'],r['gross_realized_paise']-r['accrued_cost_paise']);self.assertEqual(r['net_unrealized_accrued_paise'],0)
 def states(self,strategies=(1,2),symbol='RELIANCE-EQ'):
  self.db.batch([('INSERT INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,'2026-11-08',i,symbol,1,e.js({'input_policy':{'version':e.si.POLICY['version']}}),'{}',T.isoformat())) for i in strategies]);self.db.batch([('INSERT OR IGNORE INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?)',(e.NS,'2026-11-08',T.isoformat(),'ok','{}'))])
 def test_entry_cohort_immutable_and_groups_reconcile(self):
  self.states();e.fill(self.db,self.signal(),self.quote(2500),T);self.db.batch([('UPDATE paper_qualifier_states SET qualified=0 WHERE namespace=?',(e.NS,))]);r=a.finance(e,self.db);self.assertIn('exactly_2',r['by_entry_cohort']);self.assertEqual(sum(x['gross_paise'] for x in r['by_entry_cohort'].values()),r['gross_total_paise']);self.assertEqual(sum(x['net_accrued_paise'] for x in r['by_entry_cohort'].values()),r['net_total_accrued_paise'])
 def test_overlap_exclusive_buckets(self):
  self.states((1,2,3));self.states((1,),'TCS-EQ');r=a.overlap(e,self.db);self.assertEqual(r['bucket_counts'],{'single':1,'exactly_2':0,'exactly_3':1,'4_plus':0});self.assertEqual(r['at_least_2'],1)
 def test_preopen_queues_without_quotes_or_fills(self):
  self.signal();e.now=lambda:T.replace(hour=8,minute=45)
  class Feed:
   def quotes(_,s):raise AssertionError('no preopen quotes')
  self.assertEqual(e.monitor(self.db,Feed())['fills'],0);self.assertEqual(len(self.db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(e.NS,))),1)
 def test_weekend_worker_never_constructs_feed(self):
  e.now=lambda:datetime(2026,11,7,10,tzinfo=e.IST)
  with patch.object(e,'MarketData',side_effect=AssertionError('closed')):self.assertEqual(a.worker(e,self.db)['polls'],0);self.assertFalse(a.ensure_monitor(e,self.db)['started'])
 def test_worker_bounded_idle_polls(self):
  class Feed:pass
  r=a.worker(e,self.db,sleep=lambda _:None,max_polls=2,api_factory=Feed);self.assertEqual(r['polls'],2);self.assertEqual(a.health(e,self.db)['heartbeat']['status'],'stopped')
 def test_worker_singleton_lock(self):
  with (e.ROOT/'monitor.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(a.worker(e,self.db,max_polls=1)['status'],'already_running')
 def test_worker_auth_pause_no_retry(self):
  with patch.object(e,'monitor',side_effect=RuntimeError('auth_circuit_open_30min')) as m:
   r=a.worker(e,self.db,sleep=lambda _:None,max_polls=4,api_factory=lambda:object());self.assertEqual(m.call_count,1);self.assertEqual(r['status'],'paused_auth');self.assertEqual(a.health(e,self.db)['heartbeat']['status'],'paused_auth')
 def test_worker_outage_backoff_is_bounded(self):
  delays=[]
  with patch.object(e,'monitor',side_effect=RuntimeError('market_data_transport_failed')):
   a.worker(e,self.db,sleep=delays.append,max_polls=3,api_factory=lambda:object())
  cap=int(e.CFG['worker_backoff_max_seconds'])
  self.assertEqual(len(delays),3);self.assertTrue(all(0<d<=cap for d in delays));self.assertLessEqual(cap,e.CFG['worker_stale_seconds'])
 def test_rate_limit_cooloff_is_not_consumed_by_cycle_elapsed(self):
  delays=[]
  with patch.object(e,'monitor',side_effect=RuntimeError('market_data_rate_limit')):
   a.worker(e,self.db,sleep=delays.append,max_polls=2,api_factory=lambda:object())
  cap=int(e.CFG['worker_backoff_max_seconds'])
  self.assertEqual(len(delays),2);self.assertTrue(all(d==cap for d in delays));self.assertLessEqual(cap,e.CFG['worker_stale_seconds'])
 def test_worker_failure_classifies_server_and_unknown_tokens(self):
  with patch.object(e,'monitor',side_effect=RuntimeError('SERVER_ERROR')):
   a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
  payload=json.loads(self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='worker_failure'",(e.NS,))[0]['data_json'])
  self.assertEqual(payload['error_code'],'SERVER_ERROR')
  e.NS='test_'+__import__('uuid').uuid4().hex
  with patch.object(e,'monitor',side_effect=RuntimeError('weird_token_not_allowlisted')):
   a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
  payload=json.loads(self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='worker_failure'",(e.NS,))[0]['data_json'])
  self.assertEqual(payload['error_code'],'RuntimeError')
  self.assertNotEqual(payload['error_code'],'unclassified_condition')
 def test_monitor_and_worker_log_ticks_for_the_log_files_verifier(self):
  logged=[]
  orig=e.log
  def spy(kind,**data):
   logged.append(kind);return orig(kind,**data)
  e.log=spy
  try:
   class Feed:
    def quotes(_,requested):return {}
   e.monitor(self.db,Feed())
   self.assertIn('monitor_started',logged)
   self.assertIn('monitor_completed',logged)
   logged.clear()
   with patch.object(e,'monitor',side_effect=RuntimeError('HELPER_TIMEOUT')):
    a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
   self.assertIn('monitor_check',logged)
   payload=json.loads(self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='worker_failure'",(e.NS,))[-1]['data_json'])
   self.assertEqual(payload['error_code'],'HELPER_TIMEOUT')
  finally:
   e.log=orig

 def test_eod_refuses_closed_day_or_before_close(self):
  self.assertFalse(a.eod(e,self.db)['snapshot_written']);e.now=lambda:datetime(2026,11,7,16,10,tzinfo=e.IST);self.assertFalse(a.eod(e,self.db)['snapshot_written']);self.assertFalse(self.db.query('SELECT * FROM paper_eod WHERE namespace=?',(e.NS,)))
 def test_eod_idempotent_daily_and_weekly_not_sum_cumulative(self):
  e.fill(self.db,self.signal(),self.quote(2500),T);self.db.batch([('UPDATE paper_positions SET mark_price=2510,mark_at=? WHERE namespace=?',(T.replace(hour=15,minute=29).isoformat(),e.NS))]);e.now=lambda:T.replace(hour=16,minute=10);first=a.eod(e,self.db);a.eod(e,self.db);self.assertGreater(first['daily_gross_paise'],0);self.assertEqual(len(self.db.query('SELECT * FROM paper_eod WHERE namespace=?',(e.NS,))),1)
  later=T.replace(day=11,hour=16,minute=10);e.now=lambda:later;self.db.batch([('UPDATE paper_positions SET mark_price=2520,mark_at=? WHERE namespace=?',(later.replace(hour=15,minute=29).isoformat(),e.NS))]);second=a.eod(e,self.db);self.assertEqual(second['daily_gross_paise'],20000);w=a.weekly(e,self.db);self.assertTrue(w['complete']);self.assertEqual(w['weekly_gross_paise'],first['daily_gross_paise']+second['daily_gross_paise']);self.assertEqual(w['weekly_net_paise'],w['weekly_gross_paise']-first['accrued_cost_paise'])
 def test_missing_prior_eod_daily_unavailable(self):
  e.fill(self.db,self.signal(),self.quote(),T);e.now=lambda:T.replace(day=11,hour=16);r=a.eod(e,self.db);self.assertIsNone(r['daily_gross_paise']);self.assertFalse(r['daily_baseline_known'])
 def test_partial_week_unavailable(self):
  e.now=lambda:T.replace(day=11,hour=16);a.eod(e,self.db);self.assertFalse(a.weekly(e,self.db)['complete'])
 def test_unknown_calendar_and_policy_expiry_fail_closed(self):
  e.now=lambda:datetime(2027,1,4,10,tzinfo=e.IST);self.assertFalse(a.ensure_monitor(e,self.db)['started']);self.assertFalse(a.health(e,self.db)['calendar_verified']);self.assertTrue(a.health(e,self.db)['calendar_review_warning'])
 def test_test_namespace_data_does_not_mix_manual_demo(self):
  e.fill(self.db,self.signal(),self.quote(),T);self.assertEqual(len(self.db.positions()),1);self.assertEqual(a.finance(e,self.db)['gross_total_paise'],0);self.assertNotIn('paper_demo',Path(e.__file__).read_text()+Path(a.__file__).read_text())
 def test_manual_screen_does_not_advance_watermarks_or_queue(self):
  import pandas as pd
  g=pd.DataFrame({'symbol':'RELIANCE-EQ','trade_date':pd.bdate_range(end='2026-11-06',periods=260).strftime('%Y-%m-%d'),'open':100.,'high':101.,'low':99.,'close':100.,'volume':10})
  with patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=g):
   e.now=lambda:datetime(2026,11,7,10,tzinfo=e.IST);r=e.scan(self.db,None,snapshot_only=True)
  self.assertEqual(r['signals_queued'],0);self.assertTrue(r['screen_only']);self.assertEqual(len(self.db.query('SELECT * FROM paper_qualifier_states WHERE namespace=?',(e.NS,))),7);self.assertFalse(self.db.query('SELECT * FROM paper_watermarks WHERE namespace=?',(e.NS,)));self.assertFalse(self.db.query('SELECT * FROM paper_signals WHERE namespace=?',(e.NS,)))
 def test_sync_accepts_only_verified_completed_candles(self):
  sym='SYNCONLY-EQ';self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close) VALUES(?,?,?)',(sym,'2026-11-06',100))]);stamp=lambda d:int(datetime(2026,11,d,tzinfo=e.IST).timestamp())
  class Feed:
   def history(_,symbol,start,end):return [[stamp(7),100,101,99,100,10],[stamp(9),100,101,99,100,10],[stamp(11),100,101,99,100,10]]
  r=e.sync(self.db,Feed(),T.date(),[sym]);self.assertEqual(r['candidate_candles'],1);self.assertEqual([x['trade_date'] for x in self.db.query('SELECT trade_date FROM daily_ohlcv WHERE symbol=? ORDER BY trade_date',(sym,))],['2026-11-06','2026-11-09'])
 def test_monitor_rechecks_freshness_for_each_fill(self):
  self.signal();counter=[0]
  def clock():counter[0]+=1;return T if counter[0]<=2 else T+timedelta(seconds=61)
  class Feed:
   def quotes(_,symbols):return {'RELIANCE-EQ':{'price':100,'quote_at':T.isoformat()}}
  e.now=clock;r=e.monitor(self.db,Feed());self.assertEqual(r['fills'],0);self.assertFalse(self.db.positions())
 def test_supervisor_passes_singleton_lease_without_spawn_race(self):
  class Child:pid=123456789
  with patch.object(a.subprocess,'Popen',return_value=Child()) as spawn:
   r=a.ensure_monitor(e,self.db);self.assertTrue(r['started']);opts=spawn.call_args.kwargs;self.assertEqual(len(opts['pass_fds']),1);self.assertEqual(opts['env']['PAPER_MONITOR_LOCK_FD'],str(opts['pass_fds'][0]))
 def test_missing_mark_cannot_produce_partial_group_profit(self):
  e.fill(self.db,self.signal(),self.quote(),T);self.db.batch([('UPDATE paper_positions SET mark_price=NULL WHERE namespace=?',(e.NS,))]);r=a.finance(e,self.db);self.assertIsNone(r['gross_total_paise']);self.assertIsNone(r['by_strategy']['1']['gross_paise'])
 def test_cashflow_ledger_is_integer_paise(self):
  filled=e.fill(self.db,self.signal(),self.quote(2500),T);r=self.db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,))[0];cost=self.db.call({'action':'estimate','price':filled['price'],'quantity':20,'side':'BUY','prior':0,'priorBuy':0,'dp':False});gross=-int((Decimal(str(filled['price']))*20*100).quantize(Decimal('1')));self.assertEqual(r['gross_cashflow_paise'],gross);self.assertEqual(r['net_cashflow_paise'],gross-cost['total'])
 def test_same_bar_reentry_blocked_after_close(self):
  e.fill(self.db,self.signal(),self.quote(),T);e.fill(self.db,self.signal('SELL'),self.quote(),T);r=e.fill(self.db,self.signal(),self.quote(),T);self.assertFalse(r['filled']);self.assertEqual(r['reason'],'same_bar_reentry_guard');self.assertEqual(len(self.db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,))),2)
 def test_policy_registration_is_immutable(self):
  policy=json.loads((e.ROOT/'charge_policy.json').read_text());old=self.db.query('SELECT policy_json FROM paper_charge_policies WHERE version=?',(policy['version'],))[0]['policy_json'];self.db.batch([('UPDATE paper_charge_policies SET policy_json=? WHERE version=?',('fixture changed',policy['version']))])
  try:
   with self.assertRaisesRegex(RuntimeError,'immutable_policy_changed'):self.db.call({'action':'register_policy'})
  finally:self.db.batch([('UPDATE paper_charge_policies SET policy_json=? WHERE version=?',(old,policy['version']))])
 def test_cashflow_reconciles_with_gross_less_accrued(self):
  e.fill(self.db,self.signal(),self.quote(2500),T);e.fill(self.db,self.signal('SELL'),self.quote(2600),T);r=a.finance(e,self.db);self.assertTrue(r['ledger_reconciled']);self.assertEqual(r['cashflow_net_paise'],r['net_total_accrued_paise'])
 def stale_heartbeat(self,pid=123456789):
  self.db.batch([('INSERT INTO paper_monitor_health(namespace,pid,heartbeat_at,status,detail_json) VALUES(?,?,?,?,?)',(e.NS,pid,(T-timedelta(seconds=700)).isoformat(),'polling',json.dumps({'process_start':'42'})))])
 def test_stale_pid_without_lease_does_not_block_restart(self):
  self.stale_heartbeat()
  class Child:pid=987654321
  with patch.object(a.subprocess,'Popen',return_value=Child()):self.assertTrue(a.ensure_monitor(e,self.db)['started'])
 def test_stale_unknown_pid_is_never_killed(self):
  self.stale_heartbeat()
  with (e.ROOT/'monitor.lock').open('a+') as lock,patch.object(a.os,'kill') as kill:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);r=a.ensure_monitor(e,self.db);self.assertEqual(r['status'],'unhealthy_worker_needs_attention');kill.assert_not_called()
 def test_stale_exact_owned_process_signalled_only_after_identity_check(self):
  self.stale_heartbeat()
  class Proc:
   def __truediv__(self,k):return self
   def read_bytes(self):return os.fsencode(str(e.ROOT/'engine.py'))+b'\0worker\0'
   def read_text(self):return ' '.join(['0']*21+['42'])
   def resolve(self):return e.ROOT
  with (e.ROOT/'monitor.lock').open('a+') as lock,patch.object(a,'Path',return_value=Proc()),patch.object(a.os,'kill') as kill:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);r=a.ensure_monitor(e,self.db);self.assertEqual(r['status'],'stale_owned_worker_signalled');kill.assert_called_once_with(123456789,a.signal.SIGTERM)
 def test_open_auth_circuit_prevents_http(self):
  import tempfile
  with tempfile.TemporaryDirectory() as folder,patch.object(e,'ROOT',Path(folder)),patch.object(e.requests,'get') as get:
   (Path(folder)/'auth_circuit.json').write_text(json.dumps({'open':True,'at':T.isoformat()}));api=e.MarketData.__new__(e.MarketData)
   with self.assertRaisesRegex(RuntimeError,'auth_circuit_open_30min'):api.get('quotes',{})
   get.assert_not_called()
 def test_history_sync_buffers_small_symbol_writes(self):
  target=T.date();stamp=int(T.replace(hour=0).timestamp())
  class DB:
   def __init__(_):_.batches=[]
   def query(_,sql,args=()):return [{'symbol':'A-EQ','dmax':'2026-11-06'},{'symbol':'B-EQ','dmax':'2026-11-06'}]
   def batch(_,st):
    if st:_.batches.append(st)
  class Feed:
   def history(_,s,start,end):return [[stamp,100,101,99,100,10]]
  db=DB();e.sync(db,Feed(),target,['A-EQ','B-EQ']);self.assertEqual(len(db.batches),1);self.assertEqual(len(db.batches[0]),2)
 def test_health_and_supervision_reachable_while_engine_mutex_held(self):
  import io,contextlib,sys
  with (e.ROOT/'engine.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   for command,method in [('health','health'),('ensure-monitor','ensure_monitor')]:
    with patch.object(sys,'argv',['engine.py',command]),patch.object(e,'DB',return_value=self.db),patch.object(a,method,return_value={'status':'reachable'}) as fn,contextlib.redirect_stdout(io.StringIO()) as out:
     e.main();fn.assert_called_once();self.assertEqual(json.loads(out.getvalue())['status'],'reachable')
 def test_missing_near_close_marks_make_daily_pnl_unavailable(self):
  e.fill(self.db,self.signal(),self.quote(),T);e.now=lambda:T.replace(hour=16,minute=10);r=a.eod(e,self.db);self.assertFalse(r['closing_valuation_complete']);self.assertIsNone(r['daily_gross_paise']);self.assertEqual(r['gross_total_paise'],0)
if __name__=='__main__':unittest.main(verbosity=2)
