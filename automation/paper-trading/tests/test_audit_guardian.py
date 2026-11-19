import unittest,tempfile,sqlite3,json,os,fcntl,csv,uuid
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,time,timedelta
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine as e
import audit as a
import guardian as g
import automation as automation
import test_engine as fixtures
class LocalDB:
 def __init__(self):self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.offline=False;self.test_database='isolated';self.fail=False
 def batch(self,stm):
  if self.offline:raise RuntimeError('SQLITE_BUSY')
  with self.c:
   for sql,args in stm:
    if self.fail and sql.startswith('INSERT'):raise RuntimeError('SQLITE_BUSY')
    self.c.execute(sql,args)
 def query(self,sql,args=()):
  if self.offline:raise RuntimeError('SQLITE_BUSY')
  return [dict(r) for r in self.c.execute(sql,args).fetchall()]
class AuditTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'logs').mkdir();(self.root/'out').mkdir();self.t=datetime(2026,11,9,9,20,tzinfo=e.IST)
  self.e=SimpleNamespace(ROOT=self.root,NS='test_audit',now=lambda:self.t,session=lambda d:None if d.weekday()>4 or d.isoformat()=='2026-11-10' else (time(9,15),time(15,30)),CFG={**json.loads((Path(__file__).resolve().parents[1]/'config.json').read_text()),'poll_seconds':30});self.db=LocalDB();a.init(self.db);self.db.batch([(g.SCHEMA,())])
 def tearDown(self):self.db.c.close();self.tmp.cleanup()
 def events(self):return self.db.query('SELECT * FROM paper_events')
 def test_allowlist_drops_headers_tokens_paths_and_raw_provider(self):
  p=a.clean({'price':123.,'headers':{'Authorization':'Bearer ABC'},'token':'secrets','raw_provider':{'s':'ok'},'reason':'https://private/secret','status':'Bearer abc','symbols':['TEST-EQ'],'path':'/home/secret'})
  self.assertEqual(p,{'price':123.,'reason':'unclassified_condition','status':'[redacted]','symbols':['TEST-EQ']});self.assertNotIn('secret',json.dumps(p))
 def test_nonfinite_and_payload_bounds(self):
  self.assertIsNone(a.clean(float('inf')));self.assertIsNone(a.clean(float('nan')))
  with self.assertRaisesRegex(ValueError,'audit_list_bounds'):a.event(self.e,'quote_checks',rows=[{}]*51)
 def test_all_253_coverage_dates_preserved(self):
  dates=[str(datetime(2025,1,1).date()+timedelta(days=i)) for i in range(253)];self.assertEqual(a.clean({'missing_recent_sessions':dates})['missing_recent_sessions'],dates)
 def test_stable_event_replay_deduplicated(self):
  row=a.event(self.e,'test_event',price=100);a.write(self.e,self.db,[row]);a.write(self.e,self.db,[row]);self.assertEqual(len(self.events()),1)
 def test_offline_spool_fsynced_and_replayed_dedup(self):
  row=a.event(self.e,'quote_checks',rows=[{'symbol':'TEST-EQ','price':123.,'accepted':False,'reason':'stale_or_future_quote'}]);self.db.offline=True;self.assertFalse(a.write(self.e,self.db,[row]));files=list(a.spool_dir(self.e,self.db).glob('*.jsonl'));self.assertEqual(len(files),1);self.assertEqual(files[0].stat().st_mode&0o777,0o600)
  self.db.offline=False;self.db.batch(a.statements([row]));r=a.replay(self.e,self.db);self.assertEqual(r['replayed_events'],1);self.assertEqual(len(self.events()),1);self.assertEqual(r['pending_spool_files'],0)
 def test_database_and_spool_failure_propagates(self):
  self.db.offline=True
  with patch.object(a,'spool',side_effect=OSError('disk_full')):
   with self.assertRaises(OSError):a.emit(self.e,self.db,'order_attempt')
 def test_spool_replay_failure_preserves_file(self):
  self.db.offline=True;a.emit(self.e,self.db,'monitor_idle');r=a.replay(self.e,self.db);self.assertEqual(r['pending_spool_files'],1);self.db.offline=False
 def test_spool_namespace_tampering_rejected(self):
  self.db.offline=True;a.emit(self.e,self.db,'monitor_idle');f=next(a.spool_dir(self.e,self.db).glob('*.jsonl'));r=json.loads(f.read_text());r[0]='paper_v1';f.write_text(json.dumps(r)+'\n');self.db.offline=False
  with self.assertRaisesRegex(RuntimeError,'invalid_audit_spool'):a.replay(self.e,self.db)
 def test_scopes_correlate_children_and_record_failure_without_exception_text(self):
  with self.assertRaises(RuntimeError):
   with a.scope(self.e,self.db,'scan'):
    parent=a.run_id()
    with a.scope(self.e,self.db,'fill'):a.emit(self.e,self.db,'paper_order_attempt',symbol='TEST-EQ')
    raise RuntimeError('Bearer SECRET_TOKEN')
  rows=self.events();self.assertNotIn('SECRET_TOKEN',json.dumps(rows));self.assertIn('run_failed',[r['event_type'] for r in rows]);child=next(r for r in rows if r['event_type']=='paper_order_attempt');self.assertEqual(json.loads(child['data_json'])['parent_run_id'],parent)
 def test_export_utc_ist_streaming_no_false_full_session(self):
  a.emit(self.e,self.db,'monitor_idle',open_positions=0);r=a.export(self.e,self.db,'2026-11-09');self.assertTrue(r['complete_export']);self.assertFalse(r['full_flow_verified']);self.assertIn('monitor_coverage_incomplete',r['warnings']);row=json.loads(Path(r['path']).read_text());self.assertTrue(row['source_at_IST'].endswith('+05:30'));self.assertTrue(row['recorded_at_UTC'].endswith('+00:00'));self.assertNotIn('pid',row)
 def test_csv_and_export_limit_are_explicit(self):
  for i in range(3):a.emit(self.e,self.db,'monitor_idle',count=i)
  r=a.export(self.e,self.db,'2026-11-09','csv',2);self.assertFalse(r['complete_export']);self.assertIn('export_event_limit',r['warnings'])
  with open(r['path']) as f:self.assertEqual(len(list(csv.DictReader(f))),2)
 def test_closed_day_export_not_market_proof(self):
  r=a.export(self.e,self.db,'2026-11-07');self.assertIn('not_verified_weekday_session',r['warnings']);self.assertFalse(r['full_flow_verified'])
 def test_guardian_weekend_holiday_and_boundaries(self):
  for t in [datetime(2026,11,7,9,tzinfo=e.IST),datetime(2026,11,8,14,30,tzinfo=e.IST),datetime(2026,11,10,9,tzinfo=e.IST),self.t.replace(hour=8,minute=29),self.t.replace(hour=15,minute=30)]:self.assertFalse(g.armable(self.e,t))
  self.assertTrue(g.armable(self.e,self.t.replace(hour=8,minute=45)));self.assertTrue(g.armable(self.e,self.t.replace(hour=15,minute=29)))
 def test_closed_ensure_never_launches(self):
  self.t=self.t.replace(day=7)
  with patch.object(g.subprocess,'Popen',side_effect=AssertionError('closed launch')):self.assertFalse(g.ensure(self.e,self.db)['started'])
 def test_preopen_guardian_waits_no_worker_or_quotes(self):
  self.t=self.t.replace(hour=8,minute=45);self.e.au=SimpleNamespace(active=lambda e,t:False,ensure_monitor=lambda *a:(_ for _ in ()).throw(AssertionError('preopen worker')))
  sleeps=[];r=g.run(self.e,self.db,sleep=sleeps.append,max_checks=2);self.assertEqual(r['checks'],2);self.assertEqual(len(sleeps),2);self.assertTrue(all(59<=x<=60 for x in sleeps));self.assertIn('guardian_armed',[x['event_type'] for x in self.events()])
 def test_market_guardian_checks_every60s_and_stops_at_close(self):
  self.t=self.t.replace(hour=15,minute=29);calls=[];self.e.au=SimpleNamespace(active=lambda e,t:True,health=lambda *a:{'heartbeat':None},ensure_monitor=lambda *a:calls.append('ensure') or {'started':False,'status':'already_running'})
  def sleep(seconds):self.t+=timedelta(seconds=60)
  r=g.run(self.e,self.db,sleep=sleep);self.assertEqual(r['checks'],1);self.assertEqual(calls,['ensure'])
 def test_auth_backoff_does_not_respawn_each_minute(self):
  self.e.au=SimpleNamespace(active=lambda e,t:True,health=lambda *a:{'heartbeat':{'status':'paused_auth','heartbeat_at':self.t.isoformat()}},ensure_monitor=lambda *a:(_ for _ in ()).throw(AssertionError('auth retry')))
  self.assertEqual(g.run(self.e,self.db,sleep=lambda x:None,max_checks=1)['checks'],1);self.assertIn('auth_backoff',json.dumps(self.events()))
 def test_guardian_singleton_lease(self):
  with (self.root/'guardian.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(g.ensure(self.e,self.db)['status'],'already_running');self.assertEqual(g.run(self.e,self.db,max_checks=1)['status'],'already_running')
 def test_only_owned_stale_guardian_can_be_signalled(self):
  self.db.batch([('INSERT INTO paper_guardian_health VALUES(?,?,?,?,?,?)',(self.e.NS,999999,'original','2026-11-09',(self.t-timedelta(seconds=200)).isoformat(),'checking'))])
  with (self.root/'guardian.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with patch.object(g,'identity',return_value=False),patch.object(g.os,'kill') as kill:self.assertEqual(g.ensure(self.e,self.db)['status'],'already_running');kill.assert_not_called()
   with patch.object(g,'identity',return_value=True),patch.object(g.os,'kill') as kill:self.assertEqual(g.ensure(self.e,self.db)['status'],'stale_owned_guardian_signalled');kill.assert_called_once()
 def test_detached_launch_inherits_singleton_lease(self):
  with patch.object(g.subprocess,'Popen',return_value=SimpleNamespace(pid=12345)) as spawn:
   self.assertTrue(g.ensure(self.e,self.db)['started']);kw=spawn.call_args.kwargs;self.assertTrue(kw['start_new_session']);self.assertEqual(kw['stdin'],g.subprocess.DEVNULL);self.assertEqual(len(kw['pass_fds']),1)
 def test_malformed_inherited_guardian_lease_is_rejected(self):
  (self.root/'guardian.lock').touch()
  with tempfile.TemporaryFile() as wrong,patch.dict(os.environ,{'PAPER_GUARDIAN_LOCK_FD':str(wrong.fileno())}):
   with self.assertRaisesRegex(RuntimeError,'invalid_guardian_lease'):g.run(self.e,self.db,max_checks=1)
class AtomicAuditTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 @classmethod
 def tearDownClass(cls):fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp;seed_adv=fixtures.PaperTests.seed_adv;signal=fixtures.PaperTests.signal;quote=fixtures.PaperTests.quote
 def test_trade_execution_event_is_atomic_and_idempotent(self):
  s=self.signal();self.assertTrue(e.fill(self.db,s,self.quote(),fixtures.T)['filled']);self.assertFalse(e.fill(self.db,s,self.quote(),fixtures.T)['filled']);rows=self.db.query("SELECT * FROM paper_events WHERE namespace=? AND event_type='paper_order_executed'",(e.NS,));self.assertEqual(len(rows),1);data=json.loads(rows[0]['data_json']);self.assertEqual(data['quantity'],500);self.assertIn('charge_policy_version',data);self.assertEqual(len(self.db.positions()),1)
 def test_failed_atomic_audit_rolls_back_fill_and_fee(self):
  self.db.batch([("CREATE TRIGGER fail_event BEFORE INSERT ON paper_events WHEN NEW.event_type='paper_order_executed' BEGIN SELECT RAISE(ABORT,'audit_fail'); END",())])
  try:
   with self.assertRaises(RuntimeError):e.fill(self.db,self.signal(),self.quote(),fixtures.T)
   self.assertEqual(self.db.positions(),[]);self.assertEqual(self.db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,)),[])
  finally:self.db.batch([('DROP TRIGGER fail_event',())])
 def test_blocked_policy_guard_has_atomic_audit_trace(self):
  s=self.signal();self.db.batch([('DELETE FROM paper_entry_input_proofs WHERE namespace=?',(e.NS,))]);self.assertFalse(e.fill(self.db,s,self.quote(),fixtures.T)['filled']);rows=self.db.query("SELECT outcome FROM paper_events WHERE namespace=? AND event_type='paper_order_blocked'",(e.NS,));self.assertEqual(rows[0]['outcome'],'missing_or_failed_current_prefilter_proof')
 def test_idle_monitor_has_no_quote_request(self):
  class API:
   def quotes(self,*a):raise AssertionError('no symbols')
  with patch.object(e,'now',return_value=fixtures.T):self.assertEqual(e.monitor(self.db,API())['status'],'idle')
  rows=self.db.query("SELECT event_type FROM paper_events WHERE namespace=?",(e.NS,));self.assertIn('monitor_idle',[r['event_type'] for r in rows]);self.assertNotIn('quote_request',[r['event_type'] for r in rows])
 def test_stop_hit_logs_observed_threshold_not_target_and_fill(self):
  bought=e.fill(self.db,self.signal(strategy=7),self.quote(100),fixtures.T);self.assertTrue(bought['filled'])
  class API:
   def quotes(self,syms):return {'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':89.,'open':95.,'low':89.,'quote_at':fixtures.T.isoformat(),'source':'FYERS_QUOTES'}}
  with patch.object(e,'now',return_value=fixtures.T):self.assertEqual(e.monitor(self.db,API())['fills'],1)
  hit=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='risk_hit'",(e.NS,));self.assertEqual(json.loads(hit[0]['data_json'])['threshold'],round(.9*bought['price'],2));self.assertFalse(json.loads(hit[0]['data_json'])['target_defined'])
 def test_full751_audit_decisions_all7_without_live_transactions(self):
  import pandas as pd
  syms=['FAKE'+str(i).zfill(4)+'-EQ' for i in range(692)];bars=pd.DataFrame([{'symbol':sym,'trade_date':d,'open':100.,'high':102.,'low':99.,'close':101.,'volume':1000.} for sym in syms for d in ['2026-11-05','2026-11-06','2026-11-09']]);benchmark=bars[bars.symbol==syms[0]]
  def gate(stock,index,target):return {'status':'PASS' if stock.iloc[0].symbol==syms[0] else 'FAIL','reasons':[],'checks':{'beta_at_least_1_2':True}}
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=syms),patch.object(e,'read_history',return_value=bars),patch.object(e.pf,'benchmark',return_value=benchmark),patch.object(e.pf,'evaluate',side_effect=gate),patch.object(e,'indicator',side_effect=lambda i,g:g.assign(entry_condition=i==1,exit_condition=False)),patch.object(e.au,'checklist',return_value={'checks':{'close_above_sma200':True}}):r=e.scan(self.db,None,snapshot_only=True)
  self.assertEqual(r['signals_queued'],0);self.assertFalse(self.db.query('SELECT * FROM paper_signals WHERE namespace=?',(e.NS,)));self.assertFalse(self.db.query('SELECT * FROM paper_watermarks WHERE namespace=?',(e.NS,)))
  logs=self.db.query("SELECT event_type,data_json FROM paper_events WHERE namespace=? AND event_type IN ('prefilter_decision','strategy_decisions')",(e.NS,));self.assertEqual(sum(x['event_type']=='prefilter_decision' for x in logs),751);groups=[json.loads(x['data_json']) for x in logs if x['event_type']=='strategy_decisions'];self.assertEqual(len(groups),751);self.assertEqual(sum(len(x['decisions']) for x in groups),5257)
 def test_completed_scan_and_eod_logs_have_current_run_provenance(self):
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16,minute=10)):
   r=automation.eod(e,self.db)
  rows=self.db.query("SELECT * FROM paper_events WHERE namespace=? AND event_type='eod_completed'",(e.NS,));self.assertEqual(len(rows),1);self.assertTrue(json.loads(rows[0]['data_json'])['snapshot_written']);self.assertTrue(json.loads(rows[0]['data_json'])['ledger_reconciled']);self.assertIsNotNone(rows[0]['persisted_at'])
 def test_audit_delay_rechecks_session_boundary_before_fill(self):
  clock=[fixtures.T.replace(hour=15,minute=29,second=59)];t=clock[0];s=self.signal();q={'price':100,'quote_at':t.isoformat()};emit=a.emit
  def delayed(engine,db,kind,**data):
   result=emit(engine,db,kind,**data)
   if kind=='paper_order_attempt':clock[0]+=timedelta(seconds=2)
   return result
  with patch.object(e,'now',side_effect=lambda:clock[0]),patch.object(a,'emit',side_effect=delayed):r=e.fill(self.db,s,q,t)
  self.assertFalse(r['filled']);self.assertEqual(r['reason'],'market_closed_or_unverified');self.assertFalse(self.db.positions())
 def test_export_keyset_pagination_and_idle_not_doublecounted(self):
  for i in range(260):a.write(e,self.db,[a.event(e,'monitor_idle')])
  a.write(e,self.db,[a.event(e,'monitor_completed',status='idle')]);r=a.export(e,self.db,fixtures.T.date().isoformat());self.assertEqual(r['events'],261);self.assertEqual(r['monitor_checks'],1);self.assertTrue(r['complete_export']);self.assertFalse(r['full_flow_verified'])

 def test_export_temporary_files_are_unique_between_formats(self):
  seen=[];opening=os.open
  def remember(path,*args,**kwargs):
   if str(path).endswith('.tmp'):seen.append(str(path))
   return opening(path,*args,**kwargs)
  with patch.object(a.os,'open',side_effect=remember):
   a.export(e,self.db,fixtures.T.date().isoformat(),'jsonl');a.export(e,self.db,fixtures.T.date().isoformat(),'csv')
  self.assertEqual(len(seen),2);self.assertNotEqual(seen[0],seen[1])
