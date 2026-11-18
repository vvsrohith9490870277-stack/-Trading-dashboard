"""Phase 2 (18 Nov 2026 audit): late-entry fill price, surveillance fail-closed gate, full-sleeve decision rows,
session daemon recovery, outage records, EOD revaluation from official closes and performance metrics."""
import json,sys,unittest,uuid
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine as e
import automation as au
import guardian as gd
import sessiond as sd
T=datetime(2026,11,9,10,tzinfo=e.IST);OPEN=datetime(2026,11,9,9,16,tzinfo=e.IST)

class Base(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.db=e.DB(str(e.ROOT/'tests'/'test_')+uuid.uuid4().hex+'.sqlite');e.NS='test_fixture';e.init(cls.db)
  cls.db.batch([('CREATE TABLE IF NOT EXISTS daily_ohlcv(symbol TEXT,trade_date TEXT,open REAL,close REAL,volume REAL,turnover REAL)',())])
 def setUp(self):
  e.NS='test_'+uuid.uuid4().hex;e.now=lambda:T;e._SURV_CACHE.clear();e._FULL_NOTED.clear()
  self.saved={k:e.CFG.get(k) for k in ('surveillance_fail_closed','surveillance_fail_closed_in_tests','sessiond_exit_selftest')}
 def tearDown(self):
  for k,v in self.saved.items():e.CFG[k]=v
 def seed_adv(self,symbol,turnover=200000000,sessions=40):
  self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?',(symbol,))])
  self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,turnover) VALUES(?,?,?,?)',(symbol,(T.date()-timedelta(days=i)).isoformat(),100,turnover)) for i in range(1,sessions+1)])
 signal=__import__('tests.test_engine',fromlist=['PaperTests']).PaperTests.signal
 def quote(self,at,p=100,open_=90):return {'symbol':'RELIANCE-EQ','price':p,'open':open_,'quote_at':at.isoformat()}

class LateEntryFillPrice(Base):
 def test_entry_inside_open_window_uses_official_open(self):
  self.assertEqual(e.execution_reference({'price':100,'open':90},at=OPEN),(90,'FEED_OPEN'))
 def test_entry_after_window_uses_actual_quote_not_the_open(self):
  self.assertEqual(e.execution_reference({'price':100,'open':90},at=T),(100,'LTP_LEGACY'))
  e.now=lambda:T;s=self.signal();r=e.fill(self.db,s,self.quote(T),T)
  self.assertTrue(r['filled']);self.assertEqual(r['execution_path'],'LTP_LEGACY');self.assertGreater(r['price'],100)
 def test_window_edge_is_configurable(self):
  edge=datetime(2026,11,9,9,15,tzinfo=e.IST)+timedelta(seconds=e.CFG['entry_open_fill_window_seconds'])
  self.assertEqual(e.execution_reference({'price':100,'open':90},at=edge)[1],'FEED_OPEN')
  self.assertEqual(e.execution_reference({'price':100,'open':90},at=edge+timedelta(seconds=1))[1],'LTP_LEGACY')
 def test_exits_keep_open_semantics_after_window(self):
  self.assertEqual(e.execution_reference({'price':100,'open':90},at=T,side='SELL'),(90,'FEED_OPEN'))
  self.assertEqual(e.execution_reference({'price':85,'open':90},stop=True,at=T),(85,'LTP_LEGACY'))
 def test_no_quote_price_means_no_fill_reference(self):
  self.assertEqual(e.execution_reference({'open':90},at=T),(None,'LTP_LEGACY'))

class SurveillanceGate(Base):
 def gate_on(self):e.CFG['surveillance_fail_closed']=True;e.CFG['surveillance_fail_closed_in_tests']=True
 def test_missing_reference_rows_block_new_entries_and_are_logged(self):
  self.gate_on();s=self.signal();r=e.fill(self.db,s,self.quote(T),T)
  self.assertFalse(r['filled']);self.assertEqual(r['reason'],'SURVEILLANCE_DATA_MISSING')
  d=self.db.query('SELECT decision_type,cause FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]
  self.assertEqual((d['decision_type'],d['cause']),('SKIP','SURVEILLANCE_DATA_MISSING'))
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,s['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT 1 FROM paper_fills WHERE namespace=?',(e.NS,)))
 def test_partial_source_failure_still_blocks(self):
  self.gate_on();e.record_reference_status(self.db,'2026-11-09',{'sources':{'asm':{'status':'ok'},'gsm':{'status':'unavailable'},'series':{'status':'ok'}}})
  ready=e.surveillance_ready(self.db,'2026-11-09');self.assertFalse(ready['ready']);self.assertEqual(ready['unavailable'],['gsm'])
 def test_loaded_sources_allow_entries(self):
  self.gate_on();e.record_reference_status(self.db,'2026-11-09',{'sources':{k:{'status':'ok'} for k in ('asm','gsm','series')}})
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(T),T)['filled'])
 def test_dry_run_reports_are_not_recorded(self):
  self.gate_on();e.record_reference_status(self.db,'2026-11-09',{'dry_run':True,'sources':{k:{'status':'ok'} for k in ('asm','gsm','series')}})
  self.assertFalse(e.surveillance_ready(self.db,'2026-11-09')['ready'])
 def test_exits_are_never_blocked_by_the_gate(self):
  e.CFG['surveillance_fail_closed']=False;self.assertTrue(e.fill(self.db,self.signal(),self.quote(T),T)['filled'])
  self.gate_on();e._SURV_CACHE.clear();self.assertTrue(e.fill(self.db,self.signal('SELL'),self.quote(T,110),T)['filled'])
 def test_gate_is_not_a_strategy_or_capital_lock_change(self):
  lock=json.loads((e.ROOT/'freeze_lock.json').read_text()) if (e.ROOT/'freeze_lock.json').exists() else {}
  self.assertNotIn('config.json',json.dumps(lock))

class FullSleeveDecisionRows(Base):
 def test_signal_skipped_because_sleeve_filled_gets_one_decision_per_session(self):
  s=self.signal()
  for _ in range(3):e._note_full_sleeve(self.db,s,T,T.date().isoformat())
  rows=self.db.query("SELECT decision_type,cause FROM paper_decisions WHERE namespace=? AND correlation_id=?",(e.NS,s['id']))
  self.assertEqual([(r['decision_type'],r['cause']) for r in rows],[('SKIP','SLEEVE_SLOT_FULL')])

class Clock:
 def __init__(self,t):self.t=t
 def __call__(self):return self.t

class SessionDaemon(Base):
 def test_due_actions_through_the_day(self):
  e.CFG['sessiond_exit_selftest']=False
  at=lambda h,m:datetime(2026,11,9,h,m,tzinfo=e.IST)
  self.assertEqual(sd.due(e,self.db,at(7,0)),[])
  self.assertIn('guardian',sd.due(e,self.db,at(9,15)))
  self.assertIn('scan',sd.due(e,self.db,at(9,15)))
  self.assertNotIn('scan',sd.due(e,self.db,at(8,40)))
  late=sd.due(e,self.db,at(18,5));self.assertIn('eod',late);self.assertIn('sync',late);self.assertNotIn('guardian',late)
  self.assertEqual(sd.due(e,self.db,datetime(2026,11,8,10,0,tzinfo=e.IST)),[])
 def test_missed_scan_alerts_once_and_reruns_with_bounded_retries(self):
  e.CFG['sessiond_exit_selftest']=False;t=datetime(2026,11,9,9,0,tzinfo=e.IST);e.now=lambda:t;started=[]
  class P:
   def poll(self):return 0
  def run(_,args):started.append(args);return P()
  saved=gd.ensure;gd.ensure=lambda e_,db:{'started':False}
  try:
   state=sd.State()
   for i in range(6):sd.tick(e,self.db,state,t,mono=i*float(e.CFG['sessiond_retry_seconds']),run=run)
  finally:gd.ensure=saved
  self.assertEqual([a for a in started if a[0]=='scan'],[['scan','--sync']]*int(e.CFG['sessiond_max_attempts']))
  alerts=self.db.query("SELECT 1 FROM paper_events WHERE namespace=? AND event_type='scan_missed'",(e.NS,));self.assertEqual(len(alerts),1)
 def test_guardian_is_armed_on_every_tick_during_session(self):
  e.CFG['sessiond_exit_selftest']=False;calls=[];saved=gd.ensure;gd.ensure=lambda e_,db:calls.append(1) or {'started':True,'status':'starting'}
  t=datetime(2026,11,9,9,15,tzinfo=e.IST)
  self.db.batch([('INSERT INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?)',(e.NS,'2026-11-06',t.isoformat(),'ok','{}'))])
  try:acted=sd.tick(e,self.db,sd.State(),t,mono=0,run=lambda *_:None)
  finally:gd.ensure=saved
  self.assertEqual(calls,[1]);self.assertIn(('guardian','starting'),acted)
 def test_ensure_is_disabled_for_test_namespaces(self):
  self.assertEqual(sd.ensure(e,self.db)['status'],'disabled')
 def test_run_loop_detects_box_resume_and_writes_heartbeat(self):
  e.CFG['sessiond_exit_selftest']=False;times=[datetime(2026,11,8,10,0,tzinfo=e.IST),datetime(2026,11,8,11,0,tzinfo=e.IST)];i={'n':0}
  def now():return times[min(i['n'],1)]
  def sleep(_):i['n']+=1
  e.now=now
  # Use a private lock file: the live runtime's own sessiond holds the real sessiond.lock while tests run.
  import tempfile;lockdir=tempfile.TemporaryDirectory();saved_lock=sd.LOCK;sd.LOCK=str(Path(lockdir.name)/'sessiond.lock')
  try:
   with __import__('audit').scope(e,self.db,'sessiond'):out=sd.run(e,self.db,sleep=sleep,max_ticks=2)
  finally:sd.LOCK=saved_lock;lockdir.cleanup()
  self.assertEqual(out['ticks'],2)
  self.assertTrue(self.db.query("SELECT 1 FROM paper_events WHERE namespace=? AND event_type='sessiond_wake'",(e.NS,)))
  hb=json.loads(self.db.query("SELECT value FROM paper_meta WHERE namespace=? AND key='sessiond_heartbeat'",(e.NS,))[0]['value']);self.assertEqual(hb['status'],'stopped')

class ExitSelftestScheduling(Base):
 def test_exit_selftest_launches_once_per_session_day(self):
  e.CFG['sessiond_exit_selftest']=True;t=datetime(2026,11,9,10,0,tzinfo=e.IST);started=[]
  self.db.batch([('INSERT INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?)',(e.NS,'2026-11-06',t.isoformat(),'ok','{}'))])
  class P:
   def poll(self):return 0
  saved=gd.ensure;gd.ensure=lambda e_,db:{'started':False}
  try:
   for i in range(3):sd.tick(e,self.db,sd.State(),t,mono=i*10**4,run=lambda _,a:started.append(a) or P())
  finally:gd.ensure=saved
  self.assertEqual([a for a in started if a==['exit-selftest']],[['exit-selftest']])

class GuardianStoppedOnAbnormalExit(Base):
 def test_exception_writes_stopped_heartbeat(self):
  e.now=lambda:datetime(2026,11,9,10,0,tzinfo=e.IST)
  def boom(*a,**k):raise SystemExit(1)
  saved=gd._loop;gd._loop=boom
  try:
   with self.assertRaises(SystemExit):gd.run(e,self.db)
  finally:gd._loop=saved
  self.assertEqual(self.db.query('SELECT status FROM paper_guardian_health WHERE namespace=?',(e.NS,))[0]['status'],'stopped')

class Outages(Base):
 def test_missing_sessions_are_recorded_once(self):
  e.now=lambda:datetime(2026,11,11,17,0,tzinfo=e.IST)
  self.assertEqual(au.record_outages(e,self.db,lookback=2)['new_records'],[])  # before go-live: nothing expected
  self.db.batch([('INSERT INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?)',(e.NS,'2026-11-01','2026-11-02T08:45:00+05:30','ok','{}'))])
  r=au.record_outages(e,self.db,lookback=2);kinds={(x['session_day'],x['kind']) for x in r['new_records']}
  self.assertEqual(r['checked_sessions'][0],'2026-11-11');self.assertEqual(len(r['checked_sessions']),3)
  for d in r['checked_sessions']:
   for k in ('no_monitoring','scan_missing','eod_missing'):self.assertIn((d,k),kinds)
  self.assertTrue(all(x['coverage']==0 for x in r['new_records'] if x['kind']=='no_monitoring'))
  self.assertEqual(au.record_outages(e,self.db,lookback=2)['new_records'],[])
  ev=self.db.query("SELECT COUNT(*) AS n FROM paper_events WHERE namespace=? AND event_type='session_outage_recorded'",(e.NS,))[0]['n'];self.assertEqual(ev,len(kinds))

class Revaluation(Base):
 def buy(self):
  e.now=lambda:T;r=e.fill(self.db,self.signal(),self.quote(T),T);self.assertTrue(r['filled']);return r
 def test_revalues_from_official_close_and_labels_it(self):
  self.buy();e.now=lambda:datetime(2026,11,10,19,0,tzinfo=e.IST)
  fetch=lambda e_,d,syms:{'status':'ok','closes':{s:105.0 for s in syms},'source':'nse_udiff_cm_bhavcopy:fixture','reason':'fixture'}
  out=au.revalue_eod(e,self.db,'2026-11-09',fetch=fetch);self.assertEqual(out['status'],'written')
  rep=json.loads(self.db.query('SELECT report_json FROM paper_eod WHERE namespace=? AND day=?',(e.NS,'2026-11-09'))[0]['report_json'])
  self.assertTrue(rep['revaluation']);self.assertEqual(rep['mark_basis'],'official_close_revaluation');self.assertTrue(rep['closing_valuation_complete'])
  self.assertIn('performance',rep)
 def test_refuses_without_a_published_close(self):
  self.buy();e.now=lambda:datetime(2026,11,10,19,0,tzinfo=e.IST)
  out=au.revalue_eod(e,self.db,'2026-11-09',fetch=lambda *a:{'status':'unavailable','reason':'http_404'})
  self.assertEqual(out['status'],'official_close_unavailable');self.assertFalse(self.db.query('SELECT 1 FROM paper_eod WHERE namespace=?',(e.NS,)))
 def test_refuses_when_ledger_changed_after_the_day(self):
  self.buy();later=datetime(2026,11,10,10,0,tzinfo=e.IST);e.now=lambda:later
  self.db.batch([('UPDATE paper_fills SET filled_at=? WHERE namespace=?',(later.isoformat(),e.NS))])
  e.now=lambda:datetime(2026,11,10,19,0,tzinfo=e.IST)
  self.assertEqual(au.revalue_eod(e,self.db,'2026-11-09',fetch=lambda *a:{'status':'ok','closes':{}})['status'],'ledger_changed_after_day_unsupported')
 def test_refuses_an_unfinished_session(self):
  e.now=lambda:T;self.assertEqual(au.revalue_eod(e,self.db,'2026-11-09')['status'],'session_not_completed')

class Performance(Base):
 def test_metrics_present_and_none_when_no_closed_trades(self):
  p=au.performance(e,self.db,{'gross_total_paise':1000,'accrued_cost_paise':136,'net_total_accrued_paise':864},'2026-11-09')
  self.assertEqual(p['cost_to_gross']['cost_to_gross'],0.136);self.assertIsNone(p['expectancy']['expectancy_net_paise']);self.assertEqual(p['drawdown']['max_drawdown_net_paise'],0)
 def test_drawdown_over_snapshots(self):
  for d,v in [('2026-11-05',1000),('2026-11-06',400)]:self.db.batch([('INSERT INTO paper_eod(namespace,day,as_of,report_json) VALUES(?,?,?,?)',(e.NS,d,d,json.dumps({'net_total_accrued_paise':v})))])
  p=au.performance(e,self.db,{'net_total_accrued_paise':700},'2026-11-09')
  self.assertEqual(p['drawdown']['max_drawdown_net_paise'],-600);self.assertEqual(p['drawdown']['current_drawdown_net_paise'],-300)

class AuditExportIsolation(unittest.TestCase):
 def test_test_namespace_in_runtime_tree_never_writes_production_out(self):
  import audit as al
  from types import SimpleNamespace
  root=Path(al.__file__).resolve().parent
  self.assertEqual(al.out_dir(SimpleNamespace(ROOT=root,NS='test_iso')),root/'out'/'test-exports'/'test_iso')
  self.assertEqual(al.out_dir(SimpleNamespace(ROOT=root,NS='paper_v1')),root/'out')
 def test_temp_root_keeps_plain_out(self):
  import audit as al,tempfile
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as d:
   self.assertEqual(al.out_dir(SimpleNamespace(ROOT=Path(d),NS='test_x')),Path(d)/'out')

if __name__=='__main__':unittest.main()
