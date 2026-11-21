"""Read counter (turso-move Task 4): every query action dispatched counts once; writes never count.

Semantics (see engine.DB.read_count and db_helper.Helper.stats['reads']):
- A read is one 'query' action dispatched for execution, counted at dispatch
  (so a retry that re-executes the query counts again, and an attempt that
  then fails still counts: a failed read consumed a Turso read if it reached
  the server). Each execution is counted at exactly one layer: helper
  dispatches in Helper.stats['reads'], per-call executions (including the
  helper's fallback and its freeze-lock gate) in DB._reads.
- Batches and every other action ('decision', 'fill', 'register_policy',
  'estimate', 'verify_decisions', ...) never count, even when the action
  runs SELECTs inside db.mjs: they are not visible as query actions here.
- The counter lives on the DB/Helper objects in process memory: it survives
  a helper restart (the child dies, the Helper object keeps its stats) and
  resets only when a new DB/Helper object is created (process restart).
- No limits are enforced anywhere: this only counts.
"""
import json,sys,textwrap,unittest
from pathlib import Path
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
 if mode=='sqlerr':out({'ok':False,'code':'SQLITE_ERROR','type':'LibsqlError','transient':False,'phase':'unknown'});continue
 out({'ok':True,'result':[{'via':'fake'}] if act=='query' else {}})
''')
class ReadCounterTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  fixtures.PaperTests.setUpClass.__func__(cls)
  import tempfile
  cls.tmp=tempfile.TemporaryDirectory();cls.fake=Path(cls.tmp.name)/'fake_helper.py';cls.fake.write_text(FAKE)
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup();fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp
 def fresh(self):return e.DB(self.db.test_database)
 def helper(self,per_call,mode=None,**over):
  popen=None
  if mode is not None:
   real_popen=dbh.subprocess.Popen
   popen=lambda args,**kw:real_popen([sys.executable,str(self.fake),mode],**kw)
  h=dbh.Helper(e.ROOT,self.db.test_database,{**CFG,**over},per_call,popen=popen,sleep=lambda s:None) if popen else dbh.Helper(e.ROOT,self.db.test_database,{**CFG,**over},per_call,sleep=lambda s:None)
  self.addCleanup(h.close);return h
 def test_query_increments_counter_per_call_path(self):
  db=self.fresh();self.assertEqual(db.read_count(),0)
  self.assertEqual(db.query('SELECT 1 AS n'),[{'n':1}]);self.assertEqual(db.read_count(),1)
  self.assertEqual(db.query('SELECT 2 AS n'),[{'n':2}]);self.assertEqual(db.read_count(),2)
  self.assertEqual(db.call({'action':'query','sql':'SELECT 3 AS n','args':[]}),[{'n':3}]);self.assertEqual(db.read_count(),3)
 def test_writes_and_non_query_actions_never_count(self):
  db=self.fresh()
  db.batch([('CREATE TABLE IF NOT EXISTS rc_probe(k TEXT PRIMARY KEY)',()),('INSERT OR REPLACE INTO rc_probe VALUES(?)',('a',))])
  db.call({'action':'estimate','price':100,'quantity':1,'side':'BUY'})
  self.assertEqual(db.call({'action':'verify_decisions','namespace':e.NS})['ok'],True,'verify_decisions runs SELECTs inside db.mjs but is not a query action, so it must not count')
  db.run('probe','ok',x=1)
  self.assertEqual(db.read_count(),0)
 def test_helper_counts_each_dispatch_including_retries(self):
  base=self.fresh()
  def per_call(a):return [{'via':'per_call'}] if a['action']=='query' else None
  h=self.helper(per_call,'transient_once');base.helper=h
  self.assertEqual(h.stats['reads'],0);self.assertIn('reads',h.stats)
  self.assertEqual(h.call({'action':'query','sql':'SELECT 1','args':[]}),[{'via':'fake'}])
  self.assertEqual(h.stats['retries'],1)
  self.assertEqual(h.stats['reads'],2,'one dispatch for the failed attempt, one for the retry')
  self.assertEqual(base.read_count(),2,'the stub per-call path ran no real query, so only the helper dispatches count')
  h.call({'action':'batch','statements':[{'sql':'INSERT INTO rc_probe VALUES(?)','args':['b']}]})
  self.assertEqual(h.stats['reads'],2,'a batch through the helper is a write and never counts')
  self.assertEqual(base.read_count(),2)
 def test_fallback_per_call_reads_count_once_each(self):
  base=self.fresh()
  def per_call(a):return base._call_per_process(a)
  h=self.helper(per_call,'silent',query_timeout_seconds=0.3,read_retries=1);base.helper=h
  self.assertEqual(h.call({'action':'query','sql':'SELECT 1 AS n','args':[]}),[{'n':1}])
  self.assertEqual(h.stats['fallbacks'],1);self.assertGreaterEqual(h.stats['timeouts'],2)
  self.assertEqual(h.stats['reads'],2,'two helper dispatches, both timed out')
  # Per-call path: one freeze-lock gate per helper start (2) + the fallback query (1).
  self.assertEqual(base.read_count(),5,'2 helper dispatches + 3 per-call executions, each counted exactly once')
 def test_counter_survives_helper_restart(self):
  base=self.fresh()
  def per_call(a):return base._call_per_process(a)
  h=self.helper(per_call);base.helper=h
  self.assertEqual(h.call({'action':'query','sql':'SELECT 1 AS x','args':[]}),[{'x':1}])
  self.assertEqual(h.stats['starts'],1);self.assertEqual(h.stats['reads'],1)
  self.assertEqual(base.read_count(),2,'helper dispatch + freeze-lock gate')
  first=h.proc;first.kill();first.wait()
  self.assertEqual(h.call({'action':'query','sql':'SELECT 2 AS x','args':[]}),[{'x':2}])
  self.assertEqual(h.stats['starts'],2);self.assertEqual(h.stats['restarts'],1)
  self.assertEqual(h.stats['reads'],2,'the restart kept the stats object: the count was not reset')
  self.assertEqual(base.read_count(),4,'2 helper dispatches + 2 gates')
  self.assertEqual(self.fresh().read_count(),0,'a new DB object starts at 0: the counter resets only on process restart')
 def test_heartbeat_detail_carries_count(self):
  db=self.fresh()
  db.query('SELECT 1 AS n');db.query('SELECT 2 AS n')
  before=db.read_count();self.assertEqual(before,2)
  a.heartbeat(e,db,'healthy',polls=3)
  self.assertEqual(db.read_count(),before,'the heartbeat write itself is a batch, never a read')
  rows=db.query('SELECT detail_json FROM paper_monitor_health WHERE namespace=?',(e.NS,))
  self.assertEqual(len(rows),1)
  detail=json.loads(rows[0]['detail_json'])
  self.assertEqual(detail['db_reads_total'],before)
  self.assertEqual(detail['db_reads_since_start'],before,'counter epoch is process start, so both are the same number')
if __name__=='__main__':unittest.main()
