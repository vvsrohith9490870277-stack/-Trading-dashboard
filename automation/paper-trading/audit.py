"""Durable allowlisted paper audit; no credentials, provider dumps, or public endpoint."""
import contextlib,contextvars,csv,fcntl,hashlib,json,math,os,re,time,uuid
from datetime import datetime,timezone,date
from pathlib import Path
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata');CTX=contextvars.ContextVar('paper_audit',default=None)
SCHEMA="""CREATE TABLE IF NOT EXISTS paper_events(namespace TEXT NOT NULL,event_id TEXT NOT NULL,run_id TEXT NOT NULL,session_day TEXT NOT NULL,source_at TEXT NOT NULL,recorded_at TEXT NOT NULL,event_type TEXT NOT NULL,severity TEXT NOT NULL,outcome TEXT NOT NULL,strategy INTEGER,symbol TEXT,bar_date TEXT,signal_id TEXT,policy_version TEXT,input_policy_version TEXT,data_json TEXT NOT NULL,persisted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),PRIMARY KEY(namespace,event_id));CREATE INDEX IF NOT EXISTS paper_events_session ON paper_events(namespace,session_day,recorded_at,event_id);CREATE INDEX IF NOT EXISTS paper_events_run ON paper_events(namespace,run_id);"""
COLUMNS='namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,strategy,symbol,bar_date,signal_id,policy_version,input_policy_version,data_json'.split(',')
FIELDS=set('snapshot_written daily_gross_paise daily_net_accrued_paise batch_id symbols provider_absent_sessions provider_rejected_sessions invalid_rows_excluded data_refresh_status fill_id estimated_cost_paise charge_policy_version parent_run_id status reason reasons duration_ms request_started_at response_received_at quote_at price accepted age_seconds quantity side fills execution_path correlation_id fresh_quotes quotes symbols_count open_positions pending_signals poll_seconds guardian_seconds heartbeat_lag_seconds missed_checks started stopped scan_status target_completed_bar signals_queued latest_qualifiers universe_symbols original_audit_universe excluded_audit_symbols screen_only prefilter_counts PASS FAIL UNAVAILABLE data_usable beta_pass beta beta_pairs return63 return20 relative63 sma50 sma200 close volume_ratio volume_expansion excluded_rows unverified_recent_dates missing_recent_sessions coverage_from coverage_through full_warmup_retained earliest_stored_history outcome strategy symbol bar_date signal_id policy_version input_policy_version checks decisions rows count total stale missing trigger threshold high_water target_defined stop_defined failed failures error_code replayed_events pending_spool_files truncated complete gross_total_paise net_total_accrued_paise closing_valuation_complete daily_baseline_known ledger_reconciled session_started session_finished'.split())
CHECKS=set('entry_condition beta_at_least_1_2 close_above_sma50 sma50_above_sma200 return63_at_least_10pct relative63_at_least_5pp return20_positive close_above_prior20_high prior_red current_green close_above_supertrend close_above_sma200 rsi14_below40 ema20_above_ema50 prior_ema20_at_or_below_ema50 close_at_or_below_lower_band macd_above_signal prior_macd_at_or_below_signal within10pct_high252'.split())
REASONS=CHECKS|set('fresh nonpositive_or_missing_price market_closed_or_unverified above_stop nonpositive_or_nonfinite_price invalid_or_missing_price ok idle closed market_closed_or_calendar_unverified not_verified_weekday_scan already_bootstrapped missing_stock_or_benchmark stale_stock_or_benchmark invalid_raw_OHLC raw_return_above_50pct_requires_corporate_action_review fewer_than_200_aligned_returns nonpositive_benchmark_variance nonfinite_beta incomplete_latest_200_session_stock_history unverified_stock_session_dates original_audit_exclusion:missing original_audit_exclusion:stale NOT_EVALUATED_PREFILTER original_audit_exclusion not_pending position_guard missing_or_failed_current_prefilter_proof position_identity_guard same_bar_reentry_guard not_yet_eligible expired_entry_signal UNAFFORDABLE_ZERO_SHARES SLEEVE_SLOT_FULL ISIN_CAP INSUFFICIENT_CASH ADV_FILTER_SKIP ADV_PARTICIPATION_CAP CIRCUIT_LOCKED SERIES_TRANSITION_BE_BZ ASM_GSM_RESTRICTION LOCK_UNKNOWN STATE_SCALING CORP_ACTION_MANUAL_REVIEW CASH_CREDITED KILL_PAUSE missing_quote nonpositive_price provider_quote_error missing_exchange_timestamp stale_or_future_quote quote_outside_verified_session accepted calendar_unverified session_ended bound_reached auth_backoff audit_unavailable singleton_busy lease_identity_mismatch no_target_defined below_stop observed_stop_crossing trail_gap trail_touch completed_daily_exit paper_only pending filled blocked not_evaluated failed qualified rejected RuntimeError TimeoutError ConnectionError OSError ValueError PermissionError auth_circuit_open token_refresh_failed FYERS_QUOTES FYERS_DEPTH_LTT market_data_rate_limit market_data_transport_failed market_data_budget_exhausted market_data_budget_exhausted_second market_data_budget_exhausted_minute market_data_budget_exhausted_day market_data_budget_exhausted_reserve fyers_rate_limited fyers_budget_exhausted'.split())
SECRET=re.compile(r'(?i)(bearer|password|secret|authorization|https?://|\beyJ[A-Za-z0-9_-]+\.|[/\\].*[/\\]|\b\d{6}\b)')
def text(value):
 value=str(value)
 if SECRET.search(value):return '[redacted]'
 return value[:160]
def clean(value,key='',depth=0):
 if depth>5:return None
 if isinstance(value,dict):return {k:clean(v,k,depth+1) for k,v in value.items() if k in (CHECKS if key=='checks' else FIELDS)}
 if isinstance(value,(list,tuple)):
  if len(value)>(253 if key=='missing_recent_sessions' else 50):raise ValueError('audit_list_bounds')
  return [clean(v,key,depth+1) for v in value]
 if value is None or isinstance(value,bool):return value
 if isinstance(value,(int,float)):return value if math.isfinite(value) and abs(value)<=9007199254740991 else None
 if key in ('reason','reasons','error_code'):
  s=str(value);return s if s in REASONS or re.fullmatch(r'(?:SQLITE|TURSO|HTTP)_[A-Z0-9_]{1,45}',s) else 'unclassified_condition'
 if key in ('quote_at','request_started_at','response_received_at'):
  try:return utc(value)
  except (ValueError,TypeError):return None
 if key in ('symbol','symbols'):return str(value) if re.fullmatch(r'[A-Z0-9&_.-]{1,80}',str(value)) else None
 if key=='execution_path':
  s=str(value);return s if s in ('LTP_LEGACY','FEED_OPEN','AUCTION_CAPTURE','FALLBACK_OPEN') else None
 if key=='correlation_id':
  s=str(value);return s if re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{64}',s) else None
 return text(value)
def utc(value):
 d=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
 if d.tzinfo is None:raise ValueError('aware_timestamp_required')
 return d.astimezone(timezone.utc).isoformat()
def init(db):db.batch([(s,()) for s in SCHEMA.split(';') if s.strip()])
def run_id():return (CTX.get() or {}).get('run_id') or uuid.uuid4().hex
def event(e,kind,outcome='ok',severity='INFO',source_at=None,event_id=None,**data):
 if not re.fullmatch('[a-z_]{1,48}',kind):raise ValueError('invalid_event_type')
 ctx=CTX.get() or {};rid=ctx.get('run_id') or uuid.uuid4().hex;t=e.now();payload=clean({'batch_id':rid,**data,'parent_run_id':ctx.get('parent_run_id')});raw=json.dumps(payload,separators=(',',':'),allow_nan=False)
 if len(raw.encode())>32768:raise ValueError('audit_payload_bounds')
 identity=event_id or uuid.uuid4().hex
 return (e.NS,identity,rid,ctx.get('session_day',t.date().isoformat()),utc(source_at or t),utc(datetime.now(timezone.utc)),kind,severity,outcome if re.fullmatch('[A-Za-z0-9_:]{1,80}',str(outcome)) else 'unclassified',data.get('strategy') if data.get('strategy') in range(1,8) else None,clean(data.get('symbol'),'symbol'),str(data['bar_date']) if re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(data.get('bar_date',''))) else None,data.get('signal_id') if re.fullmatch('[a-f0-9]{1,80}',str(data.get('signal_id',''))) else None,getattr(getattr(e,'pf',None),'POLICY',{}).get('version','beta_momentum_v1'),getattr(getattr(e,'si',None),'POLICY',{}).get('version','verified_live_inputs_v1'),raw)
def statements(events):return [('INSERT OR IGNORE INTO paper_events('+','.join(COLUMNS)+') VALUES('+','.join('?' for _ in COLUMNS)+')',tuple(r)) for r in events]
def spool_dir(e,db):
 suffix=hashlib.sha256((str(getattr(db,'test_database',None) or 'turso')+'|'+e.NS).encode()).hexdigest()[:16];p=e.ROOT/'logs'/'audit-spool'/suffix;p.mkdir(parents=True,exist_ok=True,mode=0o700);return p
def spool(e,db,events):
 p=spool_dir(e,db)
 for i in range(0,len(events),20):
  name=p/(uuid.uuid4().hex+'.jsonl');tmp=name.with_suffix('.tmp')
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:
   for row in events[i:i+20]:f.write(json.dumps(row,allow_nan=False)+'\n')
   f.flush();os.fsync(f.fileno())
  os.replace(tmp,name)
  dfd=os.open(p,os.O_DIRECTORY);os.fsync(dfd);os.close(dfd)
 return False
def write(e,db,events):
 if not events:return True
 try:db.batch(statements(events));return True
 except Exception:return spool(e,db,events)
def emit(e,db,kind,**data):
 row=event(e,kind,**data)
 return spool(e,db,[row]) if CTX.get() else write(e,db,[row])
def replay(e,db,max_files=20):
 p=spool_dir(e,db);count=0
 with (p/'replay.lock').open('a+') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'replayed_events':0,'pending_spool_files':len(list(p.glob('*.jsonl')))}
  files=sorted(p.glob('*.jsonl'))[:max_files]
  for i in range(0,len(files),10):
   group=files[i:i+10];events=[]
   for f in group:
    if f.stat().st_size>1000000:raise RuntimeError('audit_spool_bounds')
    for line in f.read_text().splitlines():
     row=json.loads(line)
     if len(row)!=len(COLUMNS) or row[0]!=e.NS or not re.fullmatch('[a-f0-9]{32}',row[1]):raise RuntimeError('invalid_audit_spool')
     original=json.loads(row[-1]);safe=clean(original)
     if safe!=original:raise RuntimeError('invalid_audit_spool_payload')
     events.append(row)
   try:db.batch(statements(events))
   except Exception:break
   for f in group:f.unlink()
   count+=len(events)
 return {'replayed_events':count,'pending_spool_files':len(list(p.glob('*.jsonl')))}
@contextlib.contextmanager
def scope(e,db,kind):
 parent=CTX.get();token=CTX.set({'run_id':uuid.uuid4().hex,'session_day':e.now().date().isoformat(),'parent_run_id':parent.get('run_id') if parent else None,'engine':e,'db':db,'kind':kind});start=time.monotonic()
 try:
  if parent is None:replay(e,db)
  emit(e,db,'run_started',status=kind)
  yield
 except Exception as ex:
  emit(e,db,'run_failed',outcome='failed',severity='ERROR',status=kind,error_code=type(ex).__name__,duration_ms=round((time.monotonic()-start)*1000));raise
 else:emit(e,db,'run_completed',status=kind,duration_ms=round((time.monotonic()-start)*1000))
 finally:
  try:
   if parent is None or kind in ['monitor','scan','eod']:replay(e,db,1000)
  finally:CTX.reset(token)
def note(kind,**data):
 ctx=CTX.get()
 if ctx:emit(ctx['engine'],ctx['db'],kind,**data)

def export(e,db,session,fmt='jsonl',max_events=200000):
 date.fromisoformat(session);assert fmt in ['jsonl','csv'];out=e.ROOT/'out';out.mkdir(exist_ok=True);path=out/('audit-'+session+'.'+fmt);temp=out/('audit-'+session+'-'+uuid.uuid4().hex+'.tmp');cursor=('','');count=0;types={};times=[];scan_runs={};successful_eod=False;complete=True
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,'w',newline='') as f:
  writer=csv.DictWriter(f,fieldnames=COLUMNS+['persisted_at','source_at_UTC','recorded_at_UTC','persisted_at_UTC','source_at_IST','recorded_at_IST','persisted_at_IST']) if fmt=='csv' else None
  if writer:writer.writeheader()
  while True:
   rows=db.query('SELECT '+','.join(COLUMNS)+',persisted_at FROM paper_events WHERE namespace=? AND session_day=? AND (recorded_at>? OR (recorded_at=? AND event_id>?)) ORDER BY recorded_at,event_id LIMIT 250',(e.NS,session,cursor[0],cursor[0],cursor[1]))
   if not rows:break
   for row in rows:
    if count>=max_events:complete=False;break
    count+=1;types[row['event_type']]=types.get(row['event_type'],0)+1
    payload=json.loads(row['data_json'])
    if row['event_type']=='monitor_completed' and payload.get('status') in ['ok','idle']:times.append(datetime.fromisoformat(row['source_at']))
    sr=scan_runs.setdefault(row['run_id'],{'prefilter_symbols':set(),'strategy_symbols':set(),'strategy_cells':0,'completed':False})
    if row['event_type']=='prefilter_decision':sr['prefilter_symbols'].add(row['symbol'])
    if row['event_type']=='strategy_decisions':sr['strategy_symbols'].add(row['symbol']);sr['strategy_cells']+=len(payload.get('decisions',[]))
    if row['event_type']=='scan_completed':sr['completed']=True
    if row['event_type']=='eod_completed' and payload.get('snapshot_written') and payload.get('closing_valuation_complete') and payload.get('ledger_reconciled'):successful_eod=True
    for key in ['source_at','recorded_at','persisted_at']:
     row[key+'_UTC']=row[key]
     row[key+'_IST']=datetime.fromisoformat(row[key]).astimezone(IST).isoformat()
    if writer:writer.writerow({k:('field:'+str(v) if str(v).startswith(('=','+','-','@')) else v) for k,v in row.items()})
    else:f.write(json.dumps(row,separators=(',',':'))+'\n')
    cursor=(row['recorded_at'],row['event_id'])
   if not complete:break
  f.flush();os.fsync(f.fileno())
 os.replace(temp,path);pending=len(list(spool_dir(e,db).glob('*.jsonl')));warnings=[]
 if pending:warnings.append('unreplayed_spool')
 if not complete:warnings.append('export_event_limit')
 if not types.get('scan_completed'):warnings.append('scan_segment_missing')
 if not successful_eod:warnings.append('verified_eod_segment_missing')
 verified_scans=[r for r in scan_runs.values() if r['completed'] and len(r['prefilter_symbols'])>=751 and len(r['strategy_symbols'])>=751 and r['strategy_cells']>=5257]
 if not verified_scans:warnings.append('full_universe_decision_segment_missing')
 times.sort();gaps=[(b-a).total_seconds() for a,b in zip(times,times[1:])];day=date.fromisoformat(session);ss=e.session(day)
 if ss and day.weekday()<5:
  begin=datetime.combine(day,ss[0],IST);end=datetime.combine(day,min(ss[1],__import__('datetime').time(15,30)),IST)
  if not times or times[0]>(begin+__import__('datetime').timedelta(seconds=120)) or times[-1]<(end-__import__('datetime').timedelta(seconds=120)) or max(gaps,default=0)>120:warnings.append('monitor_coverage_incomplete')
 else:warnings.append('not_verified_weekday_session')
 report={'session':session,'path':str(path),'events':count,'complete_export':complete and pending==0,'event_types':types,'pending_spool_files':pending,'monitor_checks':len(times),'maximum_check_gap_seconds':max(gaps,default=None),'first_check':times[0].astimezone(IST).isoformat() if times else None,'last_check':times[-1].astimezone(IST).isoformat() if times else None,'warnings':warnings,'full_flow_verified':not warnings,'verified_full_universe_scans':len(verified_scans),'paper_execution_observed':bool(types.get('paper_order_executed')),'stop_crossing_observed':bool(types.get('risk_hit')),'trades_or_hits_required':False,'note':'Absent trades/hits may be legitimate; missing monitor/scan/EOD segments are not proof of completion.'};report_path=out/('audit-'+session+'-report.json');report_fd=os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(report_fd,'w') as f:f.write(json.dumps(report,indent=2))
 return report
