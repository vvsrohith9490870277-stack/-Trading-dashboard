"""Persistent DB helper: protocol, parity with per-call db.mjs, timeouts, restarts, fallback, write safety, shutdown."""
import unittest,json,os,sys,time,uuid,tempfile,textwrap
from pathlib import Path
from unittest.mock import patch
import test_engine as fixtures
import engine as e
import automation as a
import db_helper as dbh
CFG={**e.CFG['db_helper'],'query_timeout_seconds':5,'batch_timeout_seconds':5,'start_timeout_seconds':15,'retry_backoff_seconds':0.01,'shutdown_timeout_seconds':3}
FAKE=textwrap.dedent('''
import sys,json,os
mode=sys.argv[1];seen=0
for line in sys.stdin:
 req=json.loads(line);act=req.get('action')
 def out(o):sys.stdout.write(json.dumps({'id':req['id'],**o})+'\\n');sys.stdout.flush()
 if act=='ping':out({'ok':True,'result':{}});continue
 seen+=1
 if mode=='silent':continue
 if mode=='die':os._exit(3)
 if mode=='transient_once' and seen==1:out({'ok':False,'code':'TURSO_OPERATION_FAILED','type':'TypeError','transient':True,'phase':'unknown'});continue
 if mode=='connect_once' and seen==1:out({'ok':False,'code':'TURSO_OPERATION_FAILED','type':'TypeError','transient':True,'phase':'connect'});continue
 if mode=='sqlerr':out({'ok':False,'code':'SQLITE_ERROR','type':'LibsqlError','transient':False,'phase':'unknown'});continue
 out({'ok':True,'result':[{'via':'fake'}] if act=='query' else {}})
''')
class HelperTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  fixtures.PaperTests.setUpClass.__func__(cls)
  cls.tmp=tempfile.TemporaryDirectory();cls.fake=Path(cls.tmp.name)/'fake_helper.py';cls.fake.write_text(FAKE)
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 setUp=fixtures.PaperTests.setUp
 def real(self,**over):
  self.calls=[];base=e.DB(self.db.test_database)
  def per_call(a):self.calls.append(a);return base._call_per_process(a)
  h=dbh.Helper(e.ROOT,self.db.test_database,{**CFG,**over},per_call);self.addCleanup(h.close);return h
 def fake_helper(self,mode,**over):
  self.calls=[]
  def per_call(a):self.calls.append(a);return [{'via':'per_call'}] if a['action']=='query' else None
  real_popen=dbh.subprocess.Popen
  popen=lambda args,**kw:real_popen([sys.executable,str(self.fake),mode],**kw)
  h=dbh.Helper(e.ROOT,self.db.test_database,{**CFG,**over},per_call,popen=popen,sleep=lambda s:None);self.addCleanup(h.close);return h
 def outcome(self,fn,sql,args):
  try:return ('ok',fn({'action':'query','sql':sql,'args':args}))
  except RuntimeError as ex:return ('error',str(ex))
 def no_zombie(self,proc):
  self.assertIsNotNone(proc.poll(),'helper process reaped')
 # ---- protocol and parity ----
 def test_query_parity_with_per_call_path(self):
  tbl='t_'+uuid.uuid4().hex[:8];self.db.batch([(f'CREATE TABLE {tbl}(i INTEGER,r REAL,s TEXT,n TEXT)',()),(f'INSERT INTO {tbl} VALUES(?,?,?,?)',(9007199254740991,1.5,'x',None)),(f'INSERT INTO {tbl} VALUES(?,?,?,?)',(2,0.0,'',None))])
  h=self.real()
  for sql,args in [(f'SELECT * FROM {tbl} ORDER BY i',[]),(f'SELECT COUNT(*) AS n FROM {tbl} WHERE s=?',['x']),(f'SELECT * FROM {tbl} WHERE i<0',[]),('SELECT 1 AS one, NULL AS nothing',[])]:
   self.assertEqual(self.outcome(h.call,sql,args),self.outcome(self.db._call_per_process,sql,args),sql)
  self.assertEqual(h.stats['starts'],1);self.assertEqual(h.stats['fallbacks'],0)
  self.assertEqual(len(self.calls),1,'only the freeze-lock gate used the per-call path')
 def test_batch_through_helper_is_one_transaction_visible_to_per_call(self):
  tbl='t_'+uuid.uuid4().hex[:8];h=self.real();h.call({'action':'batch','statements':[{'sql':f'CREATE TABLE {tbl}(k TEXT PRIMARY KEY)','args':[]}]})
  h.call({'action':'batch','statements':[{'sql':f'INSERT INTO {tbl} VALUES(?)','args':['a']},{'sql':f'INSERT INTO {tbl} VALUES(?)','args':['b']}]})
  with self.assertRaises(RuntimeError):h.call({'action':'batch','statements':[{'sql':f'INSERT INTO {tbl} VALUES(?)','args':['c']},{'sql':f'INSERT INTO {tbl} VALUES(?)','args':['a']}]})
  self.assertEqual(self.db._call_per_process({'action':'query','sql':f'SELECT k FROM {tbl} ORDER BY k','args':[]}),[{'k':'a'},{'k':'b'}],'failed batch rolled back as a whole')
 def test_sql_error_code_matches_per_call_path(self):
  h=self.real();bad={'action':'query','sql':'SELECT * FROM no_such_table_xyz','args':[]}
  with self.assertRaises(RuntimeError) as per:self.db._call_per_process(bad)
  with self.assertRaises(RuntimeError) as via:h.call(bad)
  self.assertEqual(str(via.exception),str(per.exception))
 def test_non_query_actions_never_use_helper(self):
  h=self.fake_helper('ok');a_={'action':'estimate','price':100,'quantity':1,'side':'BUY'}
  h.call(a_);self.assertEqual(self.calls,[a_]);self.assertIsNone(h.proc)
 def test_db_routes_through_helper_only_when_enabled(self):
  d=e.DB(self.db.test_database)
  with patch.dict(e.CFG,{'db_helper':{**CFG,'enabled':False}}):self.assertIsNone(d.enable_helper())
  self.assertEqual(d.query('SELECT 7 AS n'),[{'n':7}])
  with patch.dict(e.CFG,{'db_helper':CFG}):h=d.enable_helper()
  self.assertIsNotNone(h);self.assertEqual(d.query('SELECT 8 AS n'),[{'n':8}]);self.assertEqual(h.stats['helper_ok'],1)
  proc=h.proc;d.close_helper();self.no_zombie(proc);self.assertIsNone(d.helper)
 # ---- timeouts, restarts, fallback ----
 def test_query_timeout_kills_helper_retries_then_falls_back(self):
  h=self.fake_helper('silent',query_timeout_seconds=0.3,read_retries=1)
  r=h.call({'action':'query','sql':'SELECT 1','args':[]})
  self.assertEqual(r,[{'via':'per_call'}]);self.assertGreaterEqual(h.stats['timeouts'],2);self.assertEqual(h.stats['fallbacks'],1)
 def test_crashed_helper_is_restarted_on_next_call(self):
  h=self.real();self.assertEqual(h.call({'action':'query','sql':'SELECT 1 AS x','args':[]}),[{'x':1}])
  first=h.proc;first.kill();first.wait()
  self.assertEqual(h.call({'action':'query','sql':'SELECT 2 AS x','args':[]}),[{'x':2}]);self.assertEqual(h.stats['starts'],2);self.assertEqual(h.stats['restarts'],1);self.no_zombie(first)
 def test_unstartable_helper_falls_back_and_restart_budget_disables(self):
  self.calls=[]
  def per_call(a):self.calls.append(a);return [{'via':'per_call'}]
  def popen(*x,**kw):raise FileNotFoundError('node')
  h=dbh.Helper(e.ROOT,None,{**CFG,'max_restarts':2},per_call,popen=popen,sleep=lambda s:None);self.addCleanup(h.close)
  for _ in range(4):self.assertEqual(h.call({'action':'query','sql':'SELECT 1','args':[]}),[{'via':'per_call'}])
  self.assertGreater(h.disabled_until,0);self.assertEqual(h.stats['starts'],0);self.assertEqual(h.stats['fallbacks'],4)
  gates=[c for c in self.calls if c.get('sql')=='SELECT 1 AS ok'];self.assertEqual(len(gates),2,'start attempts bounded by max_restarts')
 def test_freeze_gate_failure_keeps_helper_off(self):
  def per_call(a):
   if a.get('sql')=='SELECT 1 AS ok':raise RuntimeError('freeze_policy_lock_mismatch')
   raise RuntimeError('freeze_policy_lock_mismatch')
  h=dbh.Helper(e.ROOT,self.db.test_database,CFG,per_call,sleep=lambda s:None);self.addCleanup(h.close)
  with self.assertRaisesRegex(RuntimeError,'freeze_policy_lock_mismatch'):h.call({'action':'query','sql':'SELECT 1','args':[]})
  self.assertIsNone(h.proc)
 # ---- Turso error classes and retries ----
 def test_transient_read_error_is_retried_through_helper(self):
  h=self.fake_helper('transient_once');self.assertEqual(h.call({'action':'query','sql':'SELECT 1','args':[]}),[{'via':'fake'}]);self.assertEqual(h.stats['retries'],1);self.assertEqual(h.stats['fallbacks'],0)
 def test_sql_error_is_not_retried(self):
  h=self.fake_helper('sqlerr')
  with self.assertRaisesRegex(RuntimeError,'SQLITE_ERROR'):h.call({'action':'query','sql':'SELECT 1','args':[]})
  self.assertEqual(h.stats['retries'],0)
 def test_write_with_unknown_outcome_is_never_resent(self):
  for mode in ['transient_once','die']:
   h=self.fake_helper(mode)
   with self.assertRaises(RuntimeError) as cm:h.call({'action':'batch','statements':[{'sql':'INSERT INTO x VALUES(1)','args':[]}]})
   self.assertEqual(h.stats['retries'],0,mode);self.assertFalse([c for c in self.calls if c['action']=='batch'],mode)
   if mode=='die':self.assertEqual(str(cm.exception),'database_helper_write_unconfirmed')
 def test_write_connect_failure_is_retried(self):
  h=self.fake_helper('connect_once');self.assertEqual(h.call({'action':'batch','statements':[{'sql':'INSERT INTO x VALUES(1)','args':[]}]}),{});self.assertEqual(h.stats['retries'],1)
 def test_write_when_helper_unavailable_goes_per_call_exactly_once(self):
  self.calls=[]
  def per_call(a):self.calls.append(a);return None if a['action']=='batch' else [{'ok':1}]
  def popen(*x,**kw):raise FileNotFoundError('node')
  h=dbh.Helper(e.ROOT,None,CFG,per_call,popen=popen,sleep=lambda s:None);self.addCleanup(h.close)
  h.call({'action':'batch','statements':[{'sql':'INSERT INTO x VALUES(1)','args':[]}]})
  self.assertEqual(len([c for c in self.calls if c['action']=='batch']),1)
 def test_concurrent_requests_from_threads(self):
  import threading
  h=self.real();res={};
  def go(i):res[i]=h.call({'action':'query','sql':'SELECT ? AS v','args':[i]})
  ts=[threading.Thread(target=go,args=(i,)) for i in range(8)];[t.start() for t in ts];[t.join(30) for t in ts]
  self.assertEqual(res,{i:[{'v':i}] for i in range(8)})
 # ---- shutdown ----
 def test_close_and_stdin_eof_end_helper_without_zombie(self):
  h=self.real();h.call({'action':'query','sql':'SELECT 1','args':[]});proc=h.proc;h.close();self.no_zombie(proc)
  h2=self.real();h2.call({'action':'query','sql':'SELECT 1','args':[]});p2=h2.proc;p2.stdin.close();self.assertEqual(p2.wait(10),0)
 def test_worker_uses_helper_and_closes_it(self):
  seen={}
  orig=e.DB.enable_helper
  def spy(db):h=orig(db);seen['h']=h;return h
  with patch.object(e.DB,'enable_helper',spy),patch.object(e,'monitor',return_value={'status':'ok'}),patch.object(e,'pnl',return_value={}):
   r=a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
  self.assertEqual(r['polls'],1);h=seen['h'];self.assertIsNotNone(h);self.assertIsNone(self.db.helper);self.assertTrue(h.closed)
  self.assertGreater(h.stats['helper_ok'],0)
  hb=a.health(e,self.db)['heartbeat'];self.assertEqual(hb['status'],'stopped');self.assertIn('db_helper',json.loads(hb['detail_json']))
 def test_hung_per_call_raises_helper_timeout_within_budget(self):
  """A hung db.mjs path must fail closed as HELPER_TIMEOUT inside the configured per-call timeout."""
  import audit as al
  seen=[];real_run=__import__('subprocess').run
  def fake_run(args,*a,**k):
   seen.append(k.get('timeout'))
   return real_run([sys.executable,'-c','import time; time.sleep(5)'],timeout=k.get('timeout'),capture_output=True,text=True)
  db=e.DB()
  with patch.dict(e.CFG['db_helper'],per_call_timeout_seconds=0.4),patch.object(e.subprocess,'run',side_effect=fake_run):
   t0=time.monotonic()
   with self.assertRaises(RuntimeError) as ctx:
    db._call_per_process({'action':'query','sql':'SELECT 1','args':[]})
   self.assertEqual(str(ctx.exception),'HELPER_TIMEOUT')
   self.assertLess(time.monotonic()-t0,2)
  self.assertEqual(seen,[0.4])
  self.assertEqual(al.classified_error(ctx.exception),'HELPER_TIMEOUT')
if __name__=='__main__':unittest.main()

