"""Worker supervision: in-cycle heartbeat, polling stale budget, same-call replacement, reaping, SIGTERM."""
import unittest,json,fcntl,os,signal,time,tempfile,sqlite3
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timedelta,time as walltime
from unittest.mock import patch
import test_engine as fixtures
import engine as e
import automation as a
import guardian as g
T=fixtures.T
import subprocess as _sp
REAL_POPEN=_sp.Popen
def fake_spawn(child):
 """Popen stand-in for the worker launch only; database node calls still run for real."""
 calls=[]
 def popen(args,*x,**kw):
  if any(str(v).endswith('engine.py') for v in args):calls.append(args);return child
  return REAL_POPEN(args,*x,**kw)
 return popen,calls
class Proc:
 def __truediv__(self,k):return self
 def read_bytes(self):return os.fsencode(str(e.ROOT/'engine.py'))+b'\0worker\0'
 def read_text(self):return ' '.join(['0']*21+['42'])
 def resolve(self):return e.ROOT
class WorkerSupervisionTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp
 def set_heartbeat(self,status,age,pid=123456789):
  self.db.batch([('INSERT INTO paper_monitor_health(namespace,pid,heartbeat_at,status,detail_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,status=excluded.status,detail_json=excluded.detail_json',(e.NS,pid,(T-timedelta(seconds=age)).isoformat(),status,json.dumps({'process_start':'42'})))])
 def test_config_keys_present_and_polling_budget_above_idle(self):
  for k in ['worker_polling_stale_seconds','worker_heartbeat_interval_seconds','worker_cycle_max_seconds','worker_stop_wait_seconds','guardian_gap_grace_seconds']:self.assertIn(k,e.CFG)
  self.assertGreaterEqual(e.CFG['worker_polling_stale_seconds'],e.CFG['worker_stale_seconds'])
  self.assertLessEqual(e.CFG['worker_stale_seconds'],90)
  self.assertLessEqual(e.CFG['worker_polling_stale_seconds'],90)
  # Backoff is capped at worker_backoff_max_seconds (not two 65s rate-limit sleeps), so 90s is enough.
  self.assertGreater(e.CFG['worker_polling_stale_seconds'],e.CFG['worker_backoff_max_seconds'])
  h=e.CFG['db_helper']
  for k in ('query_timeout_seconds','batch_timeout_seconds','per_call_timeout_seconds'):
   self.assertIn(k,h);self.assertLessEqual(h[k],e.CFG['worker_polling_stale_seconds'])
  self.assertLess(e.CFG['worker_heartbeat_interval_seconds'],e.CFG['worker_stale_seconds'])
 def test_stale_budget_by_status(self):
  self.assertEqual(a.stale_budget(e,'polling'),e.CFG['worker_polling_stale_seconds'])
  self.assertEqual(a.stale_budget(e,'healthy'),e.CFG['worker_stale_seconds'])
  self.assertEqual(a.stale_budget(e,'backoff'),e.CFG['backoff_heartbeat_stale_seconds'])
 def test_polling_heartbeat_uses_polling_budget(self):
  idle=e.CFG['worker_stale_seconds'];busy=e.CFG['worker_polling_stale_seconds']
  self.set_heartbeat('polling',max(1,busy-10));self.assertFalse(a.health(e,self.db)['heartbeat_stale'])
  self.set_heartbeat('polling',busy+10);self.assertTrue(a.health(e,self.db)['heartbeat_stale'])
  self.set_heartbeat('healthy',idle+10);self.assertTrue(a.health(e,self.db)['heartbeat_stale'])
 def test_long_cycle_elapsed_is_stale_even_if_heartbeat_is_fresh(self):
  busy=e.CFG['worker_polling_stale_seconds']
  self.db.batch([('INSERT INTO paper_monitor_health(namespace,pid,heartbeat_at,status,detail_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,status=excluded.status,detail_json=excluded.detail_json',(e.NS,123456789,T.isoformat(),'polling',json.dumps({'process_start':'42','cycle_elapsed_seconds':busy+1})))])
  self.assertTrue(a.health(e,self.db)['heartbeat_stale'])
  self.db.batch([('INSERT INTO paper_monitor_health(namespace,pid,heartbeat_at,status,detail_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,status=excluded.status,detail_json=excluded.detail_json',(e.NS,123456789,T.isoformat(),'polling',json.dumps({'process_start':'42','cycle_elapsed_seconds':busy-10})))])
  self.assertFalse(a.health(e,self.db)['heartbeat_stale'])
 def test_cycle_heartbeat_refreshes_during_slow_cycle(self):
  beats=[]
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.02,'worker_cycle_max_seconds':60}),patch.object(a,'heartbeat',side_effect=lambda e_,db,status,**d:beats.append((status,d))):
   with a.CycleHeartbeat(e,self.db,3):time.sleep(0.2)
  n=len(beats);self.assertGreaterEqual(n,3);self.assertTrue(all(s=='polling' and 'cycle_elapsed_seconds' in d for s,d in beats))
  time.sleep(0.1);self.assertEqual(len(beats),n,'no beats after the cycle ends')
 def test_cycle_heartbeat_stops_after_cycle_max(self):
  beats=[]
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.02,'worker_cycle_max_seconds':0.05}),patch.object(a,'heartbeat',side_effect=lambda *x,**d:beats.append(1)):
   with a.CycleHeartbeat(e,self.db,0) as hb:time.sleep(0.3);self.assertFalse(hb.thread.is_alive())
  self.assertLessEqual(len(beats),3)
 def test_worker_slow_monitor_keeps_polling_heartbeat_fresh(self):
  statuses=[];real=a.heartbeat
  def record(e_,db,status,**d):statuses.append(status);real(e_,db,status,**d)
  def slow(db,api):time.sleep(1.5);return {'status':'ok'}
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.3}),patch.object(a,'heartbeat',side_effect=record),patch.object(e,'monitor',side_effect=slow),patch.object(e,'pnl',return_value={}):
   r=a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
  self.assertEqual(r['polls'],1);self.assertGreaterEqual(statuses.count('polling'),2);self.assertEqual(statuses[-2:],['healthy','stopped'])
 def test_stale_worker_replaced_in_same_call_after_lease_frees(self):
  self.set_heartbeat('polling',e.CFG['worker_polling_stale_seconds']+60)
  holder=(e.ROOT/'monitor.lock').open('a+');fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
  def kill(pid,sig):holder.close()   # the signalled worker exits and its lease frees
  class Child:
   pid=987654321
   def poll(self):return None
  try:
   popen,spawned=fake_spawn(Child())
   with patch.object(a,'Path',side_effect=lambda *x:Proc() if x and str(x[0])=='/proc' else Path(*x)),patch.object(a.os,'kill',side_effect=kill) as k,patch.object(a.subprocess,'Popen',side_effect=popen):
    r=a.ensure_monitor(e,self.db)
   self.assertEqual(r['status'],'stale_owned_worker_replaced');self.assertTrue(r['started']);self.assertEqual(r['replaced_pid'],123456789);k.assert_called_once_with(123456789,signal.SIGTERM);self.assertEqual(len(spawned),1)
  finally:
   holder.close();a._children.clear()
 def test_stale_worker_that_does_not_exit_defers_restart_bounded(self):
  self.set_heartbeat('polling',e.CFG['worker_polling_stale_seconds']+60)
  with patch.dict(e.CFG,{'worker_stop_wait_seconds':0.3}),(e.ROOT/'monitor.lock').open('a+') as lock,patch.object(a,'Path',side_effect=lambda *x:Proc() if x and str(x[0])=='/proc' else Path(*x)),patch.object(a.os,'kill') as k,patch.object(a.subprocess,'Popen',side_effect=fake_spawn(None)[0]) as spawn:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);t0=time.monotonic();r=a.ensure_monitor(e,self.db)
  self.assertEqual(r['status'],'stale_owned_worker_signalled');self.assertLess(time.monotonic()-t0,5);k.assert_called_once();self.assertFalse(any(str(v).endswith('engine.py') for c in spawn.call_args_list for v in c.args[0]))
 def test_deferred_signal_never_kills(self):
  self.set_heartbeat('polling',e.CFG['worker_polling_stale_seconds']+60)
  with (e.ROOT/'monitor.lock').open('a+') as lock,patch.object(a,'Path',side_effect=lambda *x:Proc() if x and str(x[0])=='/proc' else Path(*x)),patch.object(a.os,'kill') as k:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);r=a.ensure_monitor(e,self.db,defer_stale_signal=True)
  self.assertEqual(r['status'],'stale_signal_deferred_after_supervisor_gap');k.assert_not_called()
 def test_reap_children_removes_exited(self):
  class C:
   def __init__(self,rc):self.rc=rc;self.polled=0
   def poll(self):self.polled+=1;return self.rc
  live,dead=C(None),C(0);a._children[:]=[live,dead]
  try:self.assertEqual(a.reap_children(),1);self.assertEqual(a._children,[live]);self.assertEqual(dead.polled,1)
  finally:a._children.clear()
 def test_real_child_is_reaped_no_zombie(self):
  import subprocess,sys
  p=subprocess.Popen([sys.executable,'-c','pass']);a._children.append(p)
  try:
   for _ in range(100):
    if a.reap_children():break
    time.sleep(0.05)
   self.assertNotIn(p,a._children);self.assertFalse(Path('/proc/%d'%p.pid).exists() and 'Z' in Path('/proc/%d/stat'%p.pid).read_text().split()[2])
  finally:a._children.clear()
 def test_sigterm_mid_cycle_finishes_cycle_then_stops(self):
  original=signal.getsignal(signal.SIGTERM)
  def monitor(db,api):os.kill(os.getpid(),signal.SIGTERM);time.sleep(0.05);return {'status':'ok'}
  try:
   with patch.object(e,'monitor',side_effect=monitor),patch.object(e,'pnl',return_value={}):r=a.worker(e,self.db,sleep=lambda _:None,max_polls=5,api_factory=lambda:object())
   self.assertEqual(r,{'status':'terminated','polls':1});h=a.health(e,self.db)['heartbeat'];self.assertEqual(h['status'],'stopped');self.assertTrue(json.loads(h['detail_json'])['terminated'])
   self.assertEqual(signal.getsignal(signal.SIGTERM),signal.SIG_DFL,'second SIGTERM uses the default action')
  finally:signal.signal(signal.SIGTERM,original)
 def test_sigterm_between_cycles_exits_with_stopped_heartbeat(self):
  original=signal.getsignal(signal.SIGTERM)
  def sleep(_):os.kill(os.getpid(),signal.SIGTERM);time.sleep(0.05)
  try:
   with patch.object(e,'monitor',return_value={'status':'ok'}),patch.object(e,'pnl',return_value={}):
    with self.assertRaises(SystemExit):a.worker(e,self.db,sleep=sleep,max_polls=5,api_factory=lambda:object())
   self.assertEqual(a.health(e,self.db)['heartbeat']['status'],'stopped')
  finally:signal.signal(signal.SIGTERM,original)
 def test_normal_worker_restores_previous_sigterm_handler(self):
  original=signal.getsignal(signal.SIGTERM)
  with patch.object(e,'monitor',return_value={'status':'ok'}),patch.object(e,'pnl',return_value={}):a.worker(e,self.db,sleep=lambda _:None,max_polls=1,api_factory=lambda:object())
  self.assertEqual(signal.getsignal(signal.SIGTERM),original)
class LocalDB:
 def __init__(self):self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.test_database='isolated'
 def batch(self,stm):
  with self.c:
   for sql,args in stm:self.c.execute(sql,args)
 def query(self,sql,args=()):return [dict(r) for r in self.c.execute(sql,args).fetchall()]
class GuardianGapTests(unittest.TestCase):
 def setUp(self):
  import audit
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'logs').mkdir();self.t=datetime(2026,11,9,10,0,tzinfo=e.IST)
  self.e=SimpleNamespace(ROOT=self.root,NS='test_gap',now=lambda:self.t,session=lambda d:(walltime(9,15),walltime(15,30)),CFG={**e.CFG});self.db=LocalDB();audit.init(self.db);self.db.batch([(g.SCHEMA,())])
 def tearDown(self):self.db.c.close();self.tmp.cleanup()
 def run_with_gaps(self,gaps):
  calls=[];reaps=[];it=iter(gaps)
  def ensure(e_,db,**kw):calls.append(kw);return {'started':False,'status':'already_running'}
  self.e.au=SimpleNamespace(active=lambda e_,t:True,health=lambda *x:{'heartbeat':None},ensure_monitor=ensure,reap_children=lambda:reaps.append(1) or 0)
  def sleep(_):self.t+=timedelta(seconds=next(it,60))
  g.run(self.e,self.db,sleep=sleep,max_checks=len(gaps)+1);return calls,reaps
 def test_regular_cadence_never_defers(self):
  calls,reaps=self.run_with_gaps([60,60,60]);self.assertEqual(calls,[{}]*4);self.assertGreaterEqual(len(reaps),4)
 def test_guardian_gap_defers_signal_for_one_check(self):
  calls,_=self.run_with_gaps([60,self.e.CFG['guardian_gap_grace_seconds']+120,60])
  self.assertEqual(calls,[{},{},{'defer_stale_signal':True},{}])
 def test_preopen_sleep_wakes_at_official_open(self):
  self.t=datetime(2026,11,12,9,14,30,tzinfo=e.IST)
  wait=g.next_sleep(self.e,time.monotonic())
  self.assertGreater(wait,0)
  self.assertLessEqual(wait,30)
  self.assertLess(wait,self.e.CFG['guardian_seconds'])
  self.t=datetime(2026,11,12,10,0,tzinfo=e.IST)
  mid=g.next_sleep(self.e,time.monotonic())
  self.assertGreater(mid,50)
if __name__=='__main__':unittest.main()
