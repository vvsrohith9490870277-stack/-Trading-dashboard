"""One verified-weekday local guardian. Native scheduler remains five-minute recovery."""
import fcntl,json,os,signal,subprocess,sys,time
from datetime import datetime,time as walltime
from pathlib import Path
import audit as al
SCHEMA='CREATE TABLE IF NOT EXISTS paper_guardian_health(namespace TEXT PRIMARY KEY,pid INTEGER NOT NULL,process_start TEXT NOT NULL,session_day TEXT NOT NULL,heartbeat_at TEXT NOT NULL,status TEXT NOT NULL);'
def armable(e,t):
 s=e.session(t.date())
 return bool(s and t.weekday()<5 and walltime(8,30)<=t.time().replace(tzinfo=None)<min(s[1],walltime(15,30)))
def identity(pid,root,command,start):
 try:
  p=Path('/proc')/str(pid);return p.joinpath('stat').read_text().split()[21]==str(start) and str(root/'engine.py').encode() in p.joinpath('cmdline').read_bytes().split(b'\0') and command.encode() in p.joinpath('cmdline').read_bytes().split(b'\0') and p.joinpath('cwd').resolve()==root
 except (OSError,ValueError,IndexError):return False
def heartbeat(e,db,status):
 start=(Path('/proc')/str(os.getpid())/'stat').read_text().split()[21];db.batch([('INSERT INTO paper_guardian_health VALUES(?,?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET pid=excluded.pid,process_start=excluded.process_start,session_day=excluded.session_day,heartbeat_at=excluded.heartbeat_at,status=excluded.status',(e.NS,os.getpid(),start,str(e.now().date()),e.now().isoformat(),status))])
def ensure(e,db):
 if not armable(e,e.now()):return {'status':'closed','started':False,'worker_poll_seconds':e.CFG['poll_seconds'],'guardian_seconds':e.CFG['guardian_seconds']}
 with (e.ROOT/'guardian.lock').open('a+') as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   row=db.query('SELECT * FROM paper_guardian_health WHERE namespace=?',(e.NS,));r=row[0] if row else None
   if r and (e.now()-datetime.fromisoformat(r['heartbeat_at'])).total_seconds()>180 and identity(r['pid'],e.ROOT,'guardian',r['process_start']):os.kill(r['pid'],signal.SIGTERM);return {'status':'stale_owned_guardian_signalled','started':False}
   return {'status':'already_running','started':False}
  fd=os.open(e.ROOT/'logs'/'session-guardian.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
  try:p=subprocess.Popen([sys.executable,str(e.ROOT/'engine.py'),'guardian'],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True,cwd=e.ROOT,pass_fds=(lease.fileno(),),env={**os.environ,'PAPER_GUARDIAN_LOCK_FD':str(lease.fileno())})
  finally:os.close(fd)
 return {'status':'starting','started':True,'pid':p.pid,'worker_poll_seconds':e.CFG['poll_seconds'],'guardian_seconds':e.CFG['guardian_seconds']}
def run(e,db,sleep=time.sleep,max_checks=480):
 if not armable(e,e.now()):return {'status':'closed','checks':0}
 fd=os.environ.get('PAPER_GUARDIAN_LOCK_FD')
 if fd and os.fstat(int(fd)).st_ino!=(e.ROOT/'guardian.lock').stat().st_ino:raise RuntimeError('invalid_guardian_lease')
 with (os.fdopen(int(fd),'a+') if fd else (e.ROOT/'guardian.lock').open('a+')) as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'status':'already_running','checks':0}
  initial=e.now().date();checks=0;began=time.monotonic();last=began;last_wall=None
  while e.now().date()==initial and armable(e,e.now()) and checks<max_checks and time.monotonic()-began<e.CFG['guardian_max_session_seconds']:
   tick=time.monotonic();lag=max(0,tick-last-e.CFG['guardian_seconds']) if checks else 0;last=tick
   # Wall-clock gap since this guardian's previous check. A gap beyond guardian_gap_grace_seconds means
   # the guardian itself was not running (host pause or overload); the worker gets one check to refresh
   # its heartbeat before it can be judged stale, instead of being killed on the first look back.
   wall=e.now();gap=(wall-last_wall).total_seconds() if last_wall else 0;last_wall=wall;resumed=gap>e.CFG['guardian_gap_grace_seconds']
   try:
    heartbeat(e,db,'checking');al.replay(e,db);reaped=getattr(e.au,'reap_children',lambda:0)()
    if e.au.active(e,e.now()):
     h=e.au.health(e,db);r=h.get('heartbeat');age=(e.now()-datetime.fromisoformat(r['heartbeat_at'])).total_seconds() if r else None
     result={'status':'auth_backoff','started':False} if r and r['status']=='paused_auth' and age<e.CFG['guardian_auth_backoff_seconds'] else (e.au.ensure_monitor(e,db,defer_stale_signal=True) if resumed else e.au.ensure_monitor(e,db))
     al.emit(e,db,'guardian_check',status=result.get('status'),started=result.get('started'),heartbeat_lag_seconds=age,count=reaped,missed_checks=int(lag//e.CFG['guardian_seconds']),guardian_seconds=e.CFG['guardian_seconds'],poll_seconds=e.CFG['poll_seconds'])
    else:al.emit(e,db,'guardian_armed',status='waiting_for_verified_open',guardian_seconds=e.CFG['guardian_seconds'],missed_checks=int(lag//e.CFG['guardian_seconds']))
   except Exception as ex:
    al.emit(e,db,'guardian_failure',outcome='failed',severity='ERROR',error_code=type(ex).__name__,missed_checks=int(lag//e.CFG['guardian_seconds']))
   checks+=1;sleep(max(0,e.CFG['guardian_seconds']-(time.monotonic()-tick)))
  getattr(e.au,'reap_children',lambda:0)()
  heartbeat(e,db,'stopped');al.emit(e,db,'guardian_stopped',reason='session_ended' if not armable(e,e.now()) else 'bound_reached',count=checks)
  return {'status':'session_finished','checks':checks,'guardian_seconds':e.CFG['guardian_seconds']}
