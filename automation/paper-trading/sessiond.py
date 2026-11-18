"""Session daemon: keeps the paper session self-starting and self-recovering on the box without a routine.

Paper only. It never sends broker orders and never invents prices: every action it takes is an existing engine
command (guardian, scan, eod, sync-history, revalue-eod, exit-selftest in its isolated namespace, audit export).

Limits, stated honestly: this process only runs while the box is running. When the box VM is paused (no agent task
or routine active) nothing on it executes, including this daemon; on resume the next tick runs within
sessiond_tick_seconds and catches up whatever became due while paused (wake recovery)."""
import fcntl,json,os,subprocess,sys,time
from datetime import datetime,time as walltime
import audit as al
import guardian as gd

LOCK='sessiond.lock'
SELFTEST_KEY='sessiond_exit_selftest_launched:'
def _hm(v):return walltime.fromisoformat(v)

class State:
 """Per-process memory of what was attempted today (attempt counts and last try), so retries are bounded."""
 def __init__(self):self.day=None;self.tries={};self.children={};self.done=set()
 def roll(self,day):
  if self.day!=day:self.day=day;self.tries={};self.done=set()
 def may(self,key,now_mono,every,limit):
  n,last=self.tries.get(key,(0,None))
  return n<limit and (last is None or now_mono-last>=every)
 def mark(self,key,now_mono):
  n,_=self.tries.get(key,(0,None));self.tries[key]=(n+1,now_mono)

def spawn(e,args):
 fd=os.open(e.ROOT/'logs'/'sessiond-actions.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
 try:return subprocess.Popen([sys.executable,str(e.ROOT/'engine.py'),*args],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True,cwd=e.ROOT)
 finally:os.close(fd)

def _running(state,key):
 p=state.children.get(key)
 if p is None:return False
 if p.poll() is None:return True
 state.children.pop(key,None);return False

def due(e,db,t):
 """Pure check of which recovery actions are due at wall time t (IST). Used by tick() and by tests."""
 c=e.CFG;d=t.date();ss=e.session(d);hm=t.timetz().replace(tzinfo=None);out=[]
 if not ss or t.weekday()>=5:
  return out
 if _hm(c['sessiond_reference_from'])<=hm<ss[1]:
  if not e.surveillance_ready(db,str(d)).get('ready'):out.append('reference')
 if gd.armable(e,t):out.append('guardian')
 if _hm(c['sessiond_scan_due'])<=hm<ss[1]:
  if not db.query('SELECT 1 FROM paper_scan_batches WHERE namespace=? AND substr(scanned_at,1,10)=? LIMIT 1',(e.NS,str(d))):out.append('scan')
 if _hm(c['sessiond_exit_selftest_from'])<=hm<_hm(c['sessiond_exit_selftest_until']) and c.get('sessiond_exit_selftest'):
  if not db.query('SELECT 1 FROM paper_meta WHERE namespace=? AND key=? LIMIT 1',(e.NS,SELFTEST_KEY+str(d))):out.append('exit_selftest')
 if hm>=_hm(c['sessiond_eod_due']):
  rows=db.query('SELECT report_json FROM paper_eod WHERE namespace=? AND day=?',(e.NS,str(d)))
  if not rows:out.append('eod')
  elif hm>=_hm(c['sessiond_revalue_due']) and not json.loads(rows[0]['report_json']).get('closing_valuation_complete'):out.append('revalue')
 if hm>=_hm(c['sessiond_sync_due']):
  if not db.query("SELECT 1 FROM paper_runs WHERE namespace=? AND kind='sync-history' AND status='ok' AND substr(at,1,10)=? LIMIT 1",(e.NS,str(d))):out.append('sync')
 if hm>=_hm(c['sessiond_audit_export_due']):out.append('audit_export')
 return out

def tick(e,db,state,t=None,mono=None,run=spawn):
 t=t or e.now();mono=time.monotonic() if mono is None else mono;state.roll(t.date());c=e.CFG;acted=[]
 every=float(c['sessiond_retry_seconds']);limit=int(c['sessiond_max_attempts'])
 for action in due(e,db,t):
  key=action
  if action=='guardian':
   r=gd.ensure(e,db)
   if r.get('started'):acted.append(('guardian',r.get('status')))
   continue
  if action in state.done or _running(state,key) or not state.may(key,mono,every,limit):continue
  state.mark(key,mono)
  if action=='reference':
   r=e.refresh_reference(db,t.date());acted.append(('reference',(r.get('surveillance_status') or {}).get('sources')))
  elif action=='scan':
   if state.tries[key][0]==1:al.emit(e,db,'scan_missed',outcome='failed',severity='ERROR',reason='no_scan_batch_by_due_time',due=c['sessiond_scan_due'],recovery='scan_rerun_started')
   state.children[key]=run(e,['scan','--sync']);acted.append(('scan','started'))
  elif action=='eod':state.children[key]=run(e,['eod']);acted.append(('eod','started'))
  elif action=='revalue':state.children[key]=run(e,['revalue-eod','--session',str(t.date())]);acted.append(('revalue','started'))
  elif action=='sync':state.children[key]=run(e,['sync-history']);acted.append(('sync','started'))
  elif action=='exit_selftest':
   # Once per session day, even across daemon restarts: the marker is written before launch. It runs in the isolated
   # paper_exit_test_v1 namespace (refuses paper_v1) and cleans its dummy rows up afterwards.
   db.batch([('INSERT OR IGNORE INTO paper_meta(namespace,key,value) VALUES(?,?,?)',(e.NS,SELFTEST_KEY+str(t.date()),json.dumps({'at':t.isoformat()})))])
   state.done.add(key);state.children[key]=run(e,['exit-selftest']);acted.append(('exit_selftest','started'))
  elif action=='audit_export':
   r=al.ensure_daily_export(e,db,str(t.date()),force=True);state.done.add(key);acted.append(('audit_export',r.get('status')))
 if state.may('outages',mono,float(c['sessiond_outage_check_seconds']),10**9):
  state.mark('outages',mono)
  try:
   r=e.au.record_outages(e,db)
   if r.get('new_records'):acted.append(('outages',len(r['new_records'])))
  except Exception as ex:acted.append(('outages_failed',type(ex).__name__))
 return acted

def heartbeat(e,db,status,detail=None):
 db.batch([('INSERT INTO paper_meta(namespace,key,value) VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value',(e.NS,'sessiond_heartbeat',json.dumps({'pid':os.getpid(),'at':e.now().isoformat(),'status':status,'detail':detail or {}})))])

def run(e,db,sleep=time.sleep,max_ticks=None):
 with (e.ROOT/LOCK).open('a+') as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'status':'already_running'}
  lease.seek(0);lease.truncate();lease.write(str(os.getpid()));lease.flush()
  state=State();ticks=0;last_wall=e.now();last_mono=time.monotonic()
  al.emit(e,db,'sessiond_started',pid=os.getpid(),tick_seconds=e.CFG['sessiond_tick_seconds'])
  try:
   while max_ticks is None or ticks<max_ticks:
    wall=e.now();mono=time.monotonic()
    frozen=(wall-last_wall).total_seconds()-(mono-last_mono)
    if ticks and frozen>float(e.CFG['worker_wake_threshold_seconds']):
     al.emit(e,db,'sessiond_wake',outcome='ok',frozen_seconds=round(frozen,1),reason='box_resumed_catching_up')
    last_wall,last_mono=wall,mono
    try:
     # The engine runs this command inside an audit scope, which spools events to logs/audit-spool; replay every
     # tick (as the guardian does) so daemon alerts reach Turso within one tick instead of at the next command.
     try:al.replay(e,db)
     except Exception:pass
     acted=tick(e,db,state,wall,mono);heartbeat(e,db,'running',{'acted':acted} if acted else None)
     if acted:al.emit(e,db,'sessiond_actions',actions=[list(a) for a in acted])
    except Exception as ex:
     try:al.emit(e,db,'sessiond_failure',outcome='failed',severity='ERROR',error_code=type(ex).__name__)
     except Exception:pass
    ticks+=1
    if max_ticks is None or ticks<max_ticks:sleep(float(e.CFG['sessiond_tick_seconds']))
  finally:
   try:heartbeat(e,db,'stopped')
   except Exception:pass
 return {'status':'stopped','ticks':ticks}

def alive(e):
 """True when a sessiond process holds the lock."""
 p=e.ROOT/LOCK
 if not p.exists():return False
 with p.open('a+') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return True
  fcntl.flock(f,fcntl.LOCK_UN);return False

def ensure(e,db=None):
 """Start the daemon detached if none is running. Disabled for test namespaces and when sessiond_enabled is false."""
 if not e.CFG.get('sessiond_enabled') or e.NS.startswith('test_') or os.environ.get('PAPER_SESSIOND_DISABLE'):return {'status':'disabled','started':False}
 if alive(e):return {'status':'already_running','started':False}
 (e.ROOT/'logs').mkdir(exist_ok=True)
 fd=os.open(e.ROOT/'logs'/'sessiond.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
 try:p=subprocess.Popen([sys.executable,str(e.ROOT/'engine.py'),'sessiond'],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True,cwd=e.ROOT)
 finally:os.close(fd)
 return {'status':'starting','started':True,'pid':p.pid}
