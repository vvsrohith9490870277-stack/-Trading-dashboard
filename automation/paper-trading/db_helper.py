"""Persistent Turso helper for long-running paper processes (the worker).

One `node db_helper.mjs` child keeps a single libsql client open, so a query or batch costs one network
round-trip instead of a fresh process plus a fresh TLS connection. Only 'query' and 'batch' go through it;
every other action (fill, decision, policy registration, estimates) stays on the per-call db.mjs path.

Safety rules:
- Reads that fail for a helper or transient reason are retried, then served by the per-call path.
- A write is re-sent only when it provably never reached Turso (helper not running, pipe refused, or a
  connect-phase failure). A write whose outcome is unknown (timeout, helper died after sending) raises,
  exactly like a failed per-call write, so the caller's existing spool/backoff handling applies.
- Every (re)start first runs one per-call query, so db.mjs's freeze-lock check still gates the session.
- Restarts are bounded per window; past the bound the helper stays off and the per-call path serves all.
"""
import atexit,collections,itertools,json,subprocess,threading,time

def _safe_code(code):
 code=str(code or 'TURSO_OPERATION_FAILED')
 return code if len(code)<90 and all(c.isalnum() or c=='_' for c in code) else 'Turso_operation_failed'

class HelperUnavailable(Exception):
 """The request was never sent; replaying it on another path is safe."""
class HelperError(Exception):
 def __init__(self,code,transient=False,phase='unknown',kind=None):
  super().__init__(code);self.code=_safe_code(code);self.transient=bool(transient);self.phase=phase;self.kind=kind

class Helper:
 def __init__(self,root,test_database,cfg,per_call,log=None,popen=subprocess.Popen,sleep=time.sleep,clock=time.monotonic):
  self.root,self.test_database,self.cfg,self.per_call=root,test_database,cfg,per_call
  self.log_fn,self.popen,self.sleep,self.clock=log,popen,sleep,clock
  self.proc=None;self.lock=threading.RLock();self.pending={};self.ids=itertools.count(1);self.restarts=collections.deque();self.disabled_until=0.0;self.closed=False
  self.stats={'helper_requests':0,'helper_ok':0,'fallbacks':0,'starts':0,'restarts':0,'retries':0,'timeouts':0,'errors':0,'write_unconfirmed':0}
  atexit.register(self.close)
 def log(self,event,**data):
  if self.log_fn:
   try:self.log_fn(event,**data)
   except Exception:pass
 # ---- process management ----
 def alive(self):return self.proc is not None and self.proc.poll() is None
 def _start(self):
  now=self.clock()
  while self.restarts and now-self.restarts[0]>self.cfg['restart_window_seconds']:self.restarts.popleft()
  if len(self.restarts)>=self.cfg['max_restarts']:
   self.disabled_until=now+self.cfg['restart_window_seconds'];self.log('db_helper_disabled',reason='restart_budget_exhausted',restarts=len(self.restarts));return False
  self.restarts.append(now)
  try:
   self.per_call({'action':'query','sql':'SELECT 1 AS ok','args':[]})  # db.mjs freeze-lock gate
   err=(self.root/'logs'/'db-helper.log').open('a')
   try:
    args=['node',str(self.root/'db_helper.mjs')]+([str(self.test_database)] if self.test_database else [])
    proc=self.popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,bufsize=1,cwd=str(self.root))
   finally:err.close()
   self.proc=proc;threading.Thread(target=self._reader,args=(proc,),name='db-helper-reader',daemon=True).start()
   self._roundtrip({'action':'ping'},self.cfg['start_timeout_seconds'])
  except Exception as ex:
   self.log('db_helper_start_failed',error=type(ex).__name__,code=getattr(ex,'code',None));self._kill('start_failed');return False
  self.stats['starts']+=1;self.stats['restarts']=self.stats['starts']-1
  self.log('db_helper_started',pid=self.proc.pid,starts=self.stats['starts']);return True
 def _ensure(self):
  with self.lock:
   if self.closed:return False
   if self.alive():return True
   if self.proc is not None:self.log('db_helper_exited',returncode=self.proc.poll());self._kill('exited')
   if self.clock()<self.disabled_until:return False
   return self._start()
 def _kill(self,reason):
  with self.lock:
   proc,self.proc=self.proc,None
  if proc is None:return
  try:
   if proc.poll() is None:proc.kill()
   proc.wait(self.cfg['shutdown_timeout_seconds'])
  except Exception:pass
  for stream in (proc.stdin,proc.stdout):
   try:stream and stream.close()
   except Exception:pass
  for slot in list(self.pending.values()):slot['error']=HelperError('HELPER_EXITED',True,'unknown','helper');slot['event'].set()
 def _reader(self,proc):
  try:
   for line in proc.stdout:
    try:msg=json.loads(line)
    except ValueError:continue
    slot=self.pending.get(msg.get('id'))
    if slot is None:continue
    if msg.get('ok'):slot['result']=msg.get('result')
    else:slot['error']=HelperError(msg.get('code'),msg.get('transient'),msg.get('phase','unknown'),'turso')
    slot['event'].set()
  except Exception:pass
  for slot in list(self.pending.values()):
   if not slot['event'].is_set():slot['error']=HelperError('HELPER_EXITED',True,'unknown','helper');slot['event'].set()
 def _roundtrip(self,req,timeout):
  proc=self.proc
  if proc is None or proc.poll() is not None:raise HelperUnavailable('not_running')
  rid=next(self.ids);slot={'event':threading.Event(),'result':None,'error':None};self.pending[rid]=slot
  try:
   try:
    with self.lock:proc.stdin.write(json.dumps({**req,'id':rid},default=str)+'\n');proc.stdin.flush()
   except (OSError,ValueError):raise HelperUnavailable('pipe_refused')
   if not slot['event'].wait(timeout):
    self.stats['timeouts']+=1;self.log('db_helper_timeout',action=req.get('action'),timeout_seconds=timeout);self._kill('timeout')
    raise HelperError('HELPER_TIMEOUT',True,'unknown','helper')
   if slot['error'] is not None:raise slot['error']
   return slot['result']
  finally:self.pending.pop(rid,None)
 # ---- public ----
 def call(self,a):
  action=a.get('action')
  if action not in ('query','batch'):return self.per_call(a)
  req={'action':'query','sql':a['sql'],'args':a.get('args') or []} if action=='query' else {'action':'batch','statements':a['statements']}
  timeout=self.cfg['query_timeout_seconds'] if action=='query' else self.cfg['batch_timeout_seconds']
  tries=1+(self.cfg['read_retries'] if action=='query' else self.cfg['write_connect_retries'])
  for attempt in range(tries):
   last=attempt==tries-1
   try:
    if not self._ensure():raise HelperUnavailable('disabled_or_start_failed')
    self.stats['helper_requests']+=1;r=self._roundtrip(req,timeout);self.stats['helper_ok']+=1;return r
   except HelperUnavailable as ex:
    return self._fallback(a,str(ex))
   except HelperError as ex:
    self.stats['errors']+=1;self.log('db_helper_error',action=action,code=ex.code,error_kind=ex.kind,transient=ex.transient,phase=ex.phase,attempt=attempt+1)
    if action=='query':
     if ex.transient or ex.kind=='helper':
      if not last:self.stats['retries']+=1;self.sleep(self.cfg['retry_backoff_seconds']*(attempt+1));continue
      return self._fallback(a,ex.code)
     raise RuntimeError(ex.code) from None   # same code the per-call path reports
    if ex.kind=='turso' and ex.phase=='connect' and not last:
     self.stats['retries']+=1;self.sleep(self.cfg['retry_backoff_seconds']*(attempt+1));continue
    if ex.kind=='helper' or ex.phase!='connect':self.stats['write_unconfirmed']+=ex.kind=='helper'
    raise RuntimeError(ex.code if ex.kind=='turso' else 'database_helper_write_unconfirmed') from None
 def _fallback(self,a,reason):
  self.stats['fallbacks']+=1;self.log('db_helper_fallback',action=a.get('action'),reason=reason)
  if a.get('action')!='query':return self.per_call(a)   # never sent through the helper, so exactly once
  for attempt in range(1+self.cfg['per_call_read_retries']):
   try:return self.per_call(a)
   except RuntimeError:
    if attempt==self.cfg['per_call_read_retries']:raise
    self.stats['retries']+=1;self.sleep(self.cfg['retry_backoff_seconds']*(attempt+1))
 def close(self):
  with self.lock:
   self.closed=True;proc=self.proc
  if proc is None:return
  try:
   proc.stdin.close();proc.wait(self.cfg['shutdown_timeout_seconds'])
  except Exception:pass
  self._kill('close')
