#!/usr/bin/env python3
"""PAPER ONLY: allowlisted read-only market data; no broker order methods."""
import argparse,ast,contextlib,csv,fcntl,hashlib,io,json,math,os,subprocess,sys,time
from datetime import datetime,date,timedelta,time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import automation as au
import prefilter as pf
import session_inputs as si
import audit as al
import guardian as gd
import history_repair as hr
import capital as cap
import corporate_actions as ca
import pauses as pz
import support_agent as sa
import reference_feeds as rf
ROOT=Path(__file__).resolve().parent; LEGACY=Path('/workspace/sahil-fyers-turso'); IST=ZoneInfo('Asia/Kolkata'); CFG=json.loads((ROOT/'config.json').read_text()); PY=sys.executable
RULES=json.loads((ROOT/'strategy_rules.json').read_text());ISINS={r['db_symbol']:r['ISIN Code'] for r in csv.DictReader((ROOT/'universe.csv').open())}
RULE_HASH={str(k):hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest() for k,v in RULES.items() if str(k).isdigit()}
for folder in ['logs','out','tests']:(ROOT/folder).mkdir(exist_ok=True)
if CFG.get('paper_only') is not True:raise SystemExit('PAPER_ONLY_MODE_REQUIRED')
cap.assert_freeze()
NS=CFG['namespace']; MD=CFG['market_data']; AUTH_ROUTES=dict(MD['routes'])
REGULAR_SESSION=tuple(dt_time.fromisoformat(x) for x in CFG['regular_session']); COMPLETED_CUTOFF=dt_time.fromisoformat(CFG['completed_bar_cutoff'])
# Strategy 07 trail: the fraction is config, but it must equal the frozen rule text (".9*H" for 0.9), so a config edit cannot silently change the frozen formula.
TRAIL=float(CFG['strategy_07_trail_fraction'])
if not 0<TRAIL<1 or (('%g'%TRAIL).lstrip('0')+'*H') not in RULES['7']['trailing_update']:raise SystemExit('STRATEGY_07_TRAIL_CONFIG_DISAGREES_WITH_FROZEN_RULE')
def trail_level(high_water):return round(TRAIL*high_water,2)
def now():return datetime.now(IST)
def js(v):return json.dumps(v,default=str,allow_nan=False)
def log(kind,**data):
 r={'at_IST':now().isoformat(),'namespace':NS,'kind':kind,**data}
 with (ROOT/('logs/tests.jsonl' if NS.startswith('test_') else 'logs/engine.jsonl')).open('a') as f:f.write(js(r)+'\n')
 return r
class DB:
 def __init__(self,test_database=None):self.test_database=test_database
 def call(self,a):
  if self.test_database:a={**a,'test_database':self.test_database}
  r=subprocess.run(['node',str(ROOT/'db.mjs')],input=js(a),text=True,capture_output=True,timeout=120)
  if r.returncode:
   try:code=json.loads(r.stderr).get('code','Turso_operation_failed')
   except (ValueError,AttributeError):code='Turso_operation_failed'
   raise RuntimeError(code if len(code)<90 and all(c.isalnum() or c=='_' for c in code) else 'Turso_operation_failed')
  return json.loads(r.stdout)
 def query(self,sql,args=()):return self.call({'action':'query','sql':sql,'args':list(args)})
 def batch(self,statements):
  if statements:self.call({'action':'batch','statements':[{'sql':s,'args':list(a)} for s,a in statements]})
 def run(self,kind,run_status,**detail):self.batch([('INSERT INTO paper_runs(namespace,kind,at,status,details_json) VALUES(?,?,?,?,?)',(NS,kind,now().isoformat(),run_status,js(detail)))])
 def positions(self):return self.query('SELECT * FROM paper_positions WHERE namespace=? AND qty>0 AND strategy BETWEEN 1 AND 7',(NS,))
def session(d):
 key=d.isoformat()
 if not CFG['calendar_valid_from']<=key<=CFG['calendar_valid_through']:return None
 if key in CFG['special_sessions']:return tuple(dt_time.fromisoformat(t) for t in CFG['special_sessions'][key])
 if d.weekday()>=5 or key in CFG['holidays']:return ()
 return REGULAR_SESSION
def is_open(t):
 s=session(t.date());return bool(s and s[0]<=t.timetz().replace(tzinfo=None)<s[1])
def next_session(d):
 for k in range(1,370):
  t=d+timedelta(days=k);s=session(t)
  if s is None:raise RuntimeError('calendar_unverified_next_session')
  if s:return t
 raise RuntimeError('no_session')
def completed_day(t):
 for k in range(370):
  d=t.date()-timedelta(days=k);s=session(d)
  if s is None:raise RuntimeError('calendar_unverified_completed_session')
  if s and (d<t.date() or t.timetz().replace(tzinfo=None)>=COMPLETED_CUTOFF):return d
 raise RuntimeError('no_completed_session')
def reference_day(t):
 """Session the guards apply to: today until today's close, otherwise the next verified session."""
 s=session(t.date())
 return t.date() if s and t.timetz().replace(tzinfo=None)<s[1] else next_session(t.date())
def load_env():
 sys.path.insert(0,str(LEGACY));from fyers_auth import load_env as f
 return f(LEGACY/'.env')
def last_trade_stamp(ltt):
 """Exchange last-traded epoch seconds -> IST ISO string; None (fail closed) when missing/invalid."""
 if isinstance(ltt,bool) or ltt is None:return None
 try:v=float(ltt)
 except (TypeError,ValueError):return None
 if not math.isfinite(v) or v<=946684800 or v>=4102444800:return None
 return datetime.fromtimestamp(v,IST).isoformat()
def best_level(levels):
 """Best book price from a Fyers depth side; an empty side (price 0) stays 0 so market_guard can read a lock."""
 if not isinstance(levels,list) or not levels or not isinstance(levels[0],dict):return None
 return book_price(levels[0].get('price'))
def depth_quote(sym,d):
 """Map one Fyers /data/depth row. price=ltp, quote_at=exchange ltt (never the midnight quotes `tt`).
 o/h/l feed the upgrade's open-price path; best bid/ask and upper/lower circuit feed market_guard."""
 x=(d.get('d') or {}).get('NSE:'+sym) or {}
 p=x.get('ltp')
 try:p=float(p);p=p if math.isfinite(p) else None
 except (TypeError,ValueError):p=None
 row={'symbol':sym,'price':p,'quote_at':last_trade_stamp(x.get('ltt')),'provider_status':'ok' if x else 'missing','source':'FYERS_DEPTH_LTT','open':positive_price(x.get('o')),'high':positive_price(x.get('h')),'low':positive_price(x.get('l')),'ask':best_level(x.get('ask')),'bid':best_level(x.get('bids'))}
 upper,lower=positive_price(x.get('upper_ckt')),positive_price(x.get('lower_ckt'))
 if upper is not None and lower is not None and upper>lower:row['upper_band']=upper;row['lower_band']=lower
 return row
class MarketData:
 def __init__(self):self.env=load_env();self.refreshed=False;self.last=0
 def refresh(self):
  if self.refreshed:
   (ROOT/'auth_circuit.json').write_text(js({'open':True,'at':now().isoformat()}));raise RuntimeError('auth_failed_after_single_refresh')
  circuit=ROOT/'auth_circuit.json'
  if circuit.exists():
   c=json.loads(circuit.read_text())
   if c.get('open') and (now()-datetime.fromisoformat(c['at'])).total_seconds()<MD['auth_circuit_open_seconds']:raise RuntimeError('auth_circuit_open_30min')
  self.refreshed=True
  src=(LEGACY/'fyers_auth.py').read_text().replace('for url, payload in otp_urls:','for url, payload in otp_urls[:1]:').replace('for label, ident in (("b64", pin_b64), ("sha256", pin_sha), ("raw", pin)):', 'for label, ident in (("b64", pin_b64),):')
  ns={'__file__':str(LEGACY/'fyers_auth.py'),'__name__':'paper_authorized_refresh'};exec(compile(src,str(LEGACY/'fyers_auth.py'),'exec'),ns)
  try:
   with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):code=ns['main']()
  except Exception:
   circuit.write_text(js({'open':True,'at':now().isoformat()}));raise RuntimeError('token_refresh_failed') from None
  if code:
   circuit.write_text(js({'open':True,'at':now().isoformat()}));raise RuntimeError('token_refresh_failed')
  self.env=load_env();circuit.write_text(js({'open':False,'at':now().isoformat()}));log('auth_refresh',success=True)
 def get(self,route,params):
  circuit=ROOT/'auth_circuit.json'
  if circuit.exists():
   c=json.loads(circuit.read_text())
   if c.get('open') and (now()-datetime.fromisoformat(c['at'])).total_seconds()<MD['auth_circuit_open_seconds']:raise RuntimeError('auth_circuit_open_30min')
  assert route in AUTH_ROUTES
  attempts=int(MD['max_attempts'])
  for attempt in range(attempts):
   wait=MD['min_request_interval_seconds']-(time.monotonic()-self.last)
   if wait>0:time.sleep(wait)
   self.last=time.monotonic()
   try:r=requests.get(AUTH_ROUTES[route],params=params,headers={'Authorization':self.env['FYERS_APP_ID']+':'+self.env['FYERS_ACCESS_TOKEN']},timeout=MD['request_timeout_seconds']);d=r.json()
   except Exception:raise RuntimeError('market_data_transport_failed') from None
   if r.status_code in (401,403) or d.get('code') in (-8,-15,-16,-17):
    self.refresh();continue
   if r.status_code==429 or d.get('code')==-360:
    if attempt<attempts-1:time.sleep(MD['rate_limit_backoff_seconds']);continue
    raise RuntimeError('market_data_rate_limit')
   if d.get('s')!='ok':raise RuntimeError('market_data_error_'+str(d.get('code')))
   return d
  raise RuntimeError('market_data_request_failed')
 def quotes(self,symbols):
  # Fyers /data/quotes `tt` is the session date (midnight), not a trade time. Freshness uses the
  # exchange last-traded time `ltt` from read-only /data/depth, which accepts one symbol per request;
  # MarketData.get paces every request (>=1.3s) and handles auth refresh/429 backoff.
  out={};batch_at=now();clock=time.monotonic();batch_id=al.run_id()+':depth';al.note('quote_batch_request',batch_id=batch_id,symbols=list(symbols),request_started_at=batch_at,symbols_count=len(symbols),source='FYERS_DEPTH_LTT')
  for sym in symbols:
   try:d=self.get('depth',{'symbol':'NSE:'+sym,'ohlcv_flag':'1'})
   except RuntimeError as ex:
    if str(ex).startswith('market_data_error_'):
     al.note('quote_symbol_failed',outcome='failed',severity='WARN',symbol=sym,error_code=str(ex));out[sym]={'symbol':sym,'price':None,'quote_at':None,'provider_status':'error','source':'FYERS_DEPTH_LTT'};continue
    al.note('quote_batch_failed',outcome='failed',severity='ERROR',error_code=type(ex).__name__,duration_ms=round((time.monotonic()-clock)*1000));raise
   except Exception as ex:
    al.note('quote_batch_failed',outcome='failed',severity='ERROR',error_code=type(ex).__name__,duration_ms=round((time.monotonic()-clock)*1000));raise
   out[sym]=depth_quote(sym,d)
  al.note('quote_batch_response',batch_id=batch_id,request_started_at=batch_at,response_received_at=now(),duration_ms=round((time.monotonic()-clock)*1000),symbols_count=len(symbols))
  for sym in symbols:out.setdefault(sym,{'symbol':sym,'price':None,'quote_at':None,'provider_status':'missing','source':'FYERS_DEPTH_LTT'})
  return out
 def history(self,symbol,start,end):
  return self.get('history',{'symbol':'NSE:'+symbol,'resolution':'1D','date_format':'1','range_from':str(start),'range_to':str(end) }).get('candles') or []
def positive_price(raw):
 try:v=float(raw)
 except (TypeError,ValueError):return None
 return v if math.isfinite(v) and v>0 else None
def quote_from_fyers(x,stamp=None):
 """Map one Fyers quote. open_price is the session open. indicative_price is an auction only while reference_price is positive; otherwise Fyers repeats the last trade."""
 v=x.get('v') or {};sym=(x.get('n') or v.get('symbol') or '').replace('NSE:','')
 if stamp is None:
  tt=v.get('tt')
  try:stamp=datetime.fromtimestamp(float(tt),IST).isoformat() if str(tt).replace('.','',1).isdigit() else datetime.fromisoformat(str(tt)).astimezone(IST).isoformat()
  except Exception:stamp=None
 official=positive_price(v.get('open_price'));indicative=positive_price(v.get('indicative_price'));cas=positive_price(v.get('reference_price'))
 row={'symbol':sym,'price':positive_price(v.get('lp')),'quote_at':stamp,'provider_status':x.get('s'),'source':'FYERS_QUOTES','open':official,'high':positive_price(v.get('high_price')),'low':positive_price(v.get('low_price')),'ask':book_price(v.get('ask')),'bid':book_price(v.get('bid'))}
 if official is None and indicative is not None and cas is not None:row['auction_price']=indicative
 return row
def execution_reference(q,stop=False,stop_level=None,pending_exit=False):
 """Next-open orders use the official open. Strategy 07 stops fill at the open on a gap through the prior stop (trail_gap) or at the stop level when the bar low touches it (trail_touch)."""
 if not q:return None,'LTP_LEGACY'
 if stop and not pending_exit:
  official=positive_price(q.get('open'));low=positive_price(q.get('low'));level=positive_price(stop_level)
  if official is not None and level is not None and official<=level:return official,'FEED_OPEN'
  if low is not None and level is not None and low<=level:return round(level,2),'LTP_LEGACY'
  return positive_price(q.get('price')),'LTP_LEGACY'
 official=positive_price(q.get('open'))
 if official is not None:return official,'FEED_OPEN'
 auction=positive_price(q.get('auction_price'))
 if auction is not None:return auction,'AUCTION_CAPTURE'
 first=positive_price(q.get('first_trade'))
 if first is not None:return first,'FALLBACK_OPEN'
 return positive_price(q.get('price')),'LTP_LEGACY'
def book_price(raw):
 if raw is None or raw=='':return None
 try:v=float(raw)
 except (TypeError,ValueError):return None
 return v if math.isfinite(v) and v>=0 else None
def market_guard(q,band=None):
 """Upper blocks buys and lower blocks sells. A missing band is not a lock. LOCK_UNKNOWN is a flag, not a skip."""
 q=q or {};band=band or {};series=str(q.get('series') or band.get('series') or 'EQ').upper();surveillance=str(q.get('surveillance') or band.get('surveillance') or '').upper();surveillance=surveillance if surveillance in ('ASM','GSM') else None
 fo=bool(q.get('fo_eligible') or band.get('fo_eligible'));explicit=str(q.get('circuit') or '').upper()
 if explicit in ('UPPER','LOWER','NONE'):state=explicit
 else:
  ask,bid=q.get('ask'),q.get('bid')
  if ask==0 and isinstance(bid,(int,float)) and not isinstance(bid,bool) and bid>0:state='UPPER'
  elif bid==0 and isinstance(ask,(int,float)) and not isinstance(ask,bool) and ask>0:state='LOWER'
  else:
   upper=positive_price(q.get('upper_band') if q.get('upper_band') is not None else band.get('upper_price'));lower=positive_price(q.get('lower_band') if q.get('lower_band') is not None else band.get('lower_price'));px=positive_price(q.get('price'))
   if upper and lower and upper>lower and px:state='UPPER' if px>=upper else 'LOWER' if px<=lower else 'NONE'
   else:state='NONE'
 has_band=positive_price(band.get('upper_price')) is not None or q.get('upper_band') is not None
 prices=[positive_price(q.get(k)) for k in ('open','high','low','price')]
 unknown=state=='NONE' and not fo and not has_band and all(v is not None for v in prices) and len(set(prices))==1
 return {'circuit':state,'series':series if series in ('EQ','BE','BZ') else 'EQ','surveillance':surveillance,'lock_unknown':unknown}
def circuit_haircut(locked_price,locked_days):
 """Additional capital_policy circuit_lock_haircut_pct_per_extra_day of the locked price for each locked session after the first."""
 pct=cap.POLICY['circuit_lock_haircut_pct_per_extra_day'];cut=max(0,int(locked_days)-1)*pct
 if cut>=100:return None
 reduced=int(cap.price_paise(locked_price)*(100-cut)//100)
 if reduced<=0:return None
 return reduced/100
def band_for(db,symbol,day):
 try:rows=db.query('SELECT * FROM paper_price_bands WHERE symbol=? AND trade_date=?',(symbol,day))
 except Exception:return None
 return rows[0] if rows else None
def valid_quote(q,t):
 if not is_open(t):return False,'market_closed_or_unverified'
 if q and q.get('provider_status','ok')!='ok':return False,'provider_quote_error'
 if not q or q.get('price') is None or not math.isfinite(q['price']) or q['price']<=0:return False,'nonpositive_or_missing_price'
 if not q.get('quote_at'):return False,'missing_exchange_timestamp'
 qt=datetime.fromisoformat(q['quote_at']);age=(t-qt).total_seconds()
 if qt.date()!=t.date() or age>CFG['quote_max_age_seconds'] or age< -5:return False,'stale_or_future_quote'
 if not is_open(qt):return False,'quote_outside_verified_session'
 return True,'fresh'
def audit_quotes(db,quotes,t):
 rows=[]
 for sym,q in quotes.items():
  ok,why=valid_quote(q,t);rows.append(('INSERT INTO paper_quote_audit(namespace,symbol,observed_at,quote_at,price,accepted,reason) VALUES(?,?,?,?,?,?,?)',(NS,sym,t.isoformat(),q.get('quote_at'),q.get('price'),int(ok),why)))
 db.batch(rows)
def stable_id(*parts):return hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()
def decide(db,s,t,decision_type,cause,rule,payload=None,execution_path='LTP_LEGACY'):
 correlation=s.get('id') or stable_id(decision_type,cause,s.get('strategy'),s.get('symbol'),t.isoformat())
 db.call({'action':'decision','namespace':NS,'correlation_id':correlation,'decision_type':decision_type,'strategy':s.get('strategy'),'symbol':s.get('symbol'),'signal_id':s.get('id'),'cause':cause,'rule':rule,'execution_path':execution_path,'source_at':t.isoformat(),'payload':{'strategy_version':RULE_HASH.get(str(s.get('strategy'))),**(payload or {})}})
def _fill(db,s,q,t):
 ok,why=valid_quote(q,t);reference,path=execution_reference(q,stop=bool(s.get('stop')),stop_level=s.get('stop_level'),pending_exit=bool(s.get('pending_exit')));guard=market_guard(q,band_for(db,s['symbol'],t.date().isoformat()) if q else None)
 if not ok:
  decide(db,s,t,'SKIP',why,'quote_not_fillable',{'price':q.get('price') if q else None},path);return {'filled':False,'reason':why,'execution_path':path}
 if s['eligible_date']>t.date().isoformat():
  decide(db,s,t,'HOLD','not_yet_eligible','entry_waits_for_eligible_session',{},path);return {'filled':False,'reason':'not_yet_eligible','execution_path':path}
 if s['side']=='SELL':
  # Exits sell the whole open position; db.mjs sells the stored quantity. Never a fixed 1-share default.
  held=db.query('SELECT qty FROM paper_positions WHERE namespace=? AND strategy=? AND symbol=? AND qty>0',(NS,s['strategy'],s['symbol']))
  quantity=int(held[0]['qty']) if held else 0
 elif s.get('stop'):
  decide(db,s,t,'SKIP','invalid_stop_side','stops_are_sell_only',{},path);return {'filled':False,'reason':'invalid_stop_side','execution_path':path}
 else:quantity=None
 if s['side']=='BUY':
  # A delayed pending entry must not turn into an arbitrarily old new entry.
  eligible=date.fromisoformat(s['eligible_date'])
  if eligible<t.date():
   db.batch([("UPDATE paper_signals SET status='expired' WHERE namespace=? AND id=?",(NS,s['id']))]);decide(db,s,t,'SKIP','expired_entry_signal','entry_expires_after_eligible_session',{},path);return {'filled':False,'reason':'expired_entry_signal','execution_path':path}
  try:quantity=cap.integer_shares(reference)
  except ValueError:
   decide(db,s,t,'SKIP','nonpositive_price','price_requires_positive_paise',{'price':reference},path);return {'filled':False,'reason':'nonpositive_price','quantity':0,'execution_path':path}
  if quantity<1:
   log('order_skipped',strategy=s['strategy'],symbol=s['symbol'],signal_id=s['id'],cause='UNAFFORDABLE_ZERO_SHARES',price=reference,target_notional_inr=cap.POLICY['target_notional_inr'],quantity=0)
   decide(db,s,t,'SKIP','UNAFFORDABLE_ZERO_SHARES','floor_notional_over_fill_price_is_zero',{'price':reference,'quantity':0,'target_notional_inr':cap.POLICY['target_notional_inr']},path)
   return {'filled':False,'reason':'UNAFFORDABLE_ZERO_SHARES','quantity':0,'execution_path':path}
 r=db.call({'action':'fill','namespace':NS,'signal':s,'quantity':quantity,'price':reference,'isin':ISINS.get(s['symbol']),'quote_at':q['quote_at'],'at':t.isoformat(),'audit_run_id':al.run_id(),'execution_path':path,'strategy_version':RULE_HASH.get(str(s['strategy'])),'circuit':guard['circuit'],'series':guard['series'],'surveillance':guard['surveillance'],'lock_unknown':guard['lock_unknown']});log('paper_fill_attempt',strategy=s['strategy'],symbol=s['symbol'],result=r)
 if isinstance(r,dict) and r.get('reason') in ('SLEEVE_SLOT_FULL','ISIN_CAP','INSUFFICIENT_CASH','ADV_FILTER_SKIP','ADV_PARTICIPATION_CAP','CIRCUIT_LOCKED','SERIES_TRANSITION_BE_BZ','ASM_GSM_RESTRICTION','KILL_PAUSE'):
  log('order_skipped',strategy=s['strategy'],symbol=s['symbol'],signal_id=s['id'],cause=r['reason'],price=reference,open_positions=r.get('open_positions'),max_positions_per_sleeve=cap.POLICY['max_positions_per_sleeve'],max_strategies_per_isin=cap.POLICY['max_strategies_per_isin'],quantity=0)
 if isinstance(r,dict) and 'qty' in r:r['quantity']=r['qty']
 return r
STRATEGY_SOURCES={2:'02_supertrend/2026-11-07_run/run_strategy2.py',3:'03_rsi_pullback/2026-11-07_run/run_strategy3.py',4:'04_dual_ma/2026-11-07_run/run_strategy4.py',5:'05_bollinger_reversion/2026-11-07_run/run_strategy5.py',6:'06_macd_trend/2026-11-07_run/run_strategy6.py',7:'07_52week_momentum/2026-11-07_run/run_strategy7.py'}
def fill(db,s,q,t):
 e=sys.modules[__name__];began=now()
 with al.scope(e,db,'paper_order'):
  al.emit(e,db,'paper_order_attempt',strategy=s.get('strategy'),symbol=s.get('symbol'),signal_id=s.get('id'),bar_date=s.get('bar_date'),side=s.get('side'),price=q.get('price') if q else None,quote_at=q.get('quote_at') if q else None)
  r=_fill(db,s,q,t+max(now()-began,timedelta(0)))
  extra={}
  if r.get('open_positions') is not None:extra['open_positions']=r['open_positions']
  if r.get('execution_path'):extra['execution_path']=r['execution_path']
  if r.get('correlation_id'):extra['correlation_id']=r['correlation_id']
  al.emit(e,db,'paper_order_result',outcome='filled' if r.get('filled') else 'blocked',strategy=s.get('strategy'),symbol=s.get('symbol'),signal_id=s.get('id'),reason=r.get('reason','ok'),fills=int(bool(r.get('filled'))),quantity=r.get('quantity'),**extra)
  return r

# Explicit calendar exclusions (freeze plan A9) come from config.json, never a date literal in code.
MUHURAT_INDICATOR_EXCLUDED=frozenset(CFG['indicator_excluded_sessions'])
def indicator(strategy,g):
 if isinstance(g,pd.DataFrame) and 'trade_date' in g.columns:
  m=g.trade_date.astype(str).isin(MUHURAT_INDICATOR_EXCLUDED)
  if m.any():
   clean=g.loc[~m].reset_index(drop=True)
   if len(clean)==0:return g.assign(entry_condition=False,exit_condition=False)
   base=indicator(strategy,clean);base.index=g.index[~m];out=g.copy()
   for c in [c for c in base.columns if c not in g.columns]:
    if base[c].dtype==bool:out[c]=False;out.loc[~m,c]=base[c]
    else:
     s=base[c].reindex(out.index);s.loc[m]=s.ffill().bfill().loc[m]
     if not s.isna().any():s=s.astype(base[c].dtype)
     out[c]=s
   out.loc[m,'entry_condition']=False;out.loc[m,'exit_condition']=False
   return out
 if strategy==1:
  f=g.copy();f['entry_condition']=f.close>f.high.shift().rolling(20).max();f['exit_condition']=f.close<f.low.shift().rolling(10).min();return f
 import strategy_indicators
 return getattr(strategy_indicators,'strategy_'+str(strategy))(g)
def universe():
 return sorted(pd.read_csv(ROOT/'universe.csv').db_symbol.tolist())
def sync(db,api,target,symbols):
 cv={x['symbol']:x['dmax'] for x in db.query('SELECT symbol,MAX(trade_date) AS dmax FROM daily_ohlcv GROUP BY symbol')};n=0;invalid=[];pending_sync=[]
 for sym in symbols:
  if cv.get(sym,'')>=str(target):continue
  if not cv.get(sym):invalid.append({'symbol':sym,'reason':'no_existing_history_unverified_identity'});continue
  start=max(date.fromisoformat(cv[sym])+timedelta(days=1),target-timedelta(days=CFG['history_sync_lookback_days']))
  try:candles=api.history(sym,start,target)
  except RuntimeError as e:
   if str(e).startswith('market_data_error_') and '300' in str(e):invalid.append({'symbol':sym,'reason':'invalid_symbol'});continue
   raise
  st=[]
  for c in candles:
   ts,o,h,l,cl,v=c[:6];d=datetime.fromtimestamp(ts,IST).date()
   if d>target:continue
   if not session(d):
    st.append(('INSERT OR IGNORE INTO paper_data_audit(namespace,symbol,bar_date,reason,observed_at) VALUES(?,?,?,?,?)',(NS,sym,str(d),'unverified_or_closed_session_candle',now().isoformat())));continue
   if not all(math.isfinite(float(x)) and x>0 for x in [o,h,l,cl]) or not (h>=max(o,l,cl) and l<=min(o,h,cl)):
    st.append(('INSERT OR IGNORE INTO paper_data_audit(namespace,symbol,bar_date,reason,observed_at) VALUES(?,?,?,?,?)',(NS,sym,str(d),'invalid_Fyers_OHLC',now().isoformat())));continue
   st.append(('INSERT OR IGNORE INTO daily_ohlcv(trade_date,symbol,open,high,low,close,volume,turnover,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(str(d),sym,o,h,l,cl,v,float(cl)*float(v),'FYERS_PAPER_SYNC',now().isoformat())));n+=1
  pending_sync.extend(st)
  if len(pending_sync)>=150:db.batch(pending_sync);pending_sync=[]
 db.batch(pending_sync)
 log('history_sync',candidate_candles=n,unavailable=invalid);return {'candidate_candles':n,'unavailable':invalid}
def read_history(db,symbols,target):
 rows=[]
 for i in range(0,len(symbols),25):
  sy=symbols[i:i+25];rows+=db.query('SELECT symbol,trade_date,open,high,low,close,volume FROM daily_ohlcv WHERE symbol IN ('+','.join('?'*len(sy))+') AND trade_date<=? ORDER BY symbol,trade_date',sy+[str(target)])
 return pd.DataFrame(rows)
def _scan(db,api,do_sync=False,bootstrap=False,snapshot_only=False):
 t=now()
 if not bootstrap and not snapshot_only and (t.weekday()>=5 or not session(t.date())):return {'status':'not_verified_weekday_scan','signals_queued':0}
 target=completed_day(t);positions=db.positions();syms=sorted(set(universe())|{p['symbol'] for p in positions})
 existing=db.query('SELECT strategy,symbol,bar_date FROM paper_watermarks WHERE namespace=?',(NS,))
 if bootstrap and existing and not snapshot_only:return {'status':'already_bootstrapped','signals_queued':0,'no_backlog_fills':True}
 if do_sync:al.emit(sys.modules[__name__],db,'sync_started',target_completed_bar=str(target),symbols_count=len(syms))
 sync_report=sync(db,api,target,syms) if do_sync else None
 try:benchmark=pf.benchmark(sys.modules[__name__],db,target,api,do_sync);benchmark_error=None
 except RuntimeError:benchmark=pd.DataFrame();benchmark_error="benchmark_sync_unavailable"
 repair_report=hr.repair(sys.modules[__name__],db,api,target) if do_sync and len(benchmark)>0 else None
 if do_sync:al.emit(sys.modules[__name__],db,'sync_completed',target_completed_bar=str(target),symbols_count=len(syms))
 reference_report=None
 if do_sync and not snapshot_only:
  # Plan A6/A7 inputs (corporate actions, ASM/GSM, BE/BZ). Fail-soft: a feed outage is reported, never invented.
  try:reference_report=rf.refresh(sys.modules[__name__],db,reference_day(t))
  except Exception as ex:reference_report={'status':'failed','error':au.safe_error(ex)}
 raw=read_history(db,syms,target);assert len(raw)>0
 raw,input_details,input_audit=si.filter_history(raw,benchmark)
 gates={}
 for sym,g in raw.groupby('symbol'):
  gates[sym]=pf.evaluate(g,benchmark,target);gates[sym]['earliest_stored_history']=str(g.trade_date.min());gates[sym]['input_policy']=input_details[sym]
  if input_details[sym]['unverified_recent_dates']:gates[sym].update(status='UNAVAILABLE',reasons=['unverified_stock_session_dates'])
 if isinstance(benchmark,pd.DataFrame) and len(benchmark)>0:
  coverage=db.query("SELECT symbol,SUM(outcome='NO_PROVIDER_BAR') absent,SUM(outcome='PROVIDER_REJECTED_RANGE') rejected FROM paper_history_gap_checks WHERE namespace=? AND trade_date>=? AND trade_date<=? GROUP BY symbol",(NS,str(benchmark.iloc[-min(pf.POLICY['beta_intervals']+1,len(benchmark))].trade_date),str(target)))
  for evidence in coverage:
   if evidence['symbol'] in gates:gates[evidence['symbol']].update(provider_absent_sessions=evidence['absent'],provider_rejected_sessions=evidence['rejected'])
 for sym in syms:
  if sym not in gates:gates[sym]=pf.evaluate(pd.DataFrame(),benchmark,target)
 audit_events=[]
 for sym,r in gates.items():audit_events.append(al.event(sys.modules[__name__],'prefilter_decision',outcome=r['status'],symbol=sym,bar_date=str(target),**{k:v for k,v in r.items() if k not in ['status','symbol','bar_date']},excluded_rows=input_details.get(sym,{}).get('excluded_rows',0)))
 valid=(raw[['open','high','low','close']]>0).all(axis=1)&np.isfinite(raw[['open','high','low','close']]).all(axis=1)&(raw.high>=raw[['open','close','low']].max(axis=1))&(raw.low<=raw[['open','close','high']].min(axis=1))
 stm=[('INSERT INTO paper_prefilter_states(namespace,bar_date,symbol,policy_version,status,metrics_json,observed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,bar_date,symbol,policy_version) DO UPDATE SET status=excluded.status,metrics_json=excluded.metrics_json,observed_at=excluded.observed_at',(NS,str(target),sym,pf.POLICY['version'],r['status'],js(r),t.isoformat())) for sym,r in gates.items()]
 stm +=[('INSERT OR IGNORE INTO paper_data_audit(namespace,symbol,bar_date,reason,observed_at) VALUES(?,?,?,?,?)',(NS,x.symbol,x.trade_date,'nonpositive_nonfinite_or_inconsistent_OHLC',t.isoformat())) for x in raw[~valid].itertuples(index=False)]
 exclusions=json.loads((ROOT/'prefilter_exclusions.json').read_text())
 for row in exclusions:
  r={'status':'UNAVAILABLE','reasons':[row['reason']],'audit_excluded':True,'policy_version':pf.POLICY['version']}
  audit_events.append(al.event(sys.modules[__name__],'prefilter_decision',outcome='UNAVAILABLE',symbol=row['symbol'],bar_date=str(target),reasons=r['reasons']))
  audit_events.append(al.event(sys.modules[__name__],'strategy_decisions',outcome='not_evaluated',symbol=row['symbol'],bar_date=str(target),decisions=[{'strategy':i,'outcome':'not_evaluated','reason':'original_audit_exclusion'} for i in range(1,8)]))
  stm.append(('INSERT INTO paper_prefilter_states VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,bar_date,symbol,policy_version) DO UPDATE SET status=excluded.status,metrics_json=excluded.metrics_json,observed_at=excluded.observed_at',(NS,str(target),row['symbol'],pf.POLICY['version'],'UNAVAILABLE',js(r),t.isoformat())))
 raw=raw[valid].copy();wm={(r['strategy'],r['symbol']):r['bar_date'] for r in db.query('SELECT * FROM paper_watermarks WHERE namespace=?',(NS,))};pos={(p['strategy'],p['symbol']):p for p in positions};pending_rows=db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(NS,));pending=set()
 for s in pending_rows:
  if not snapshot_only and s['side']=='BUY' and s['eligible_date']<=str(target):stm.append(("UPDATE paper_signals SET status='expired' WHERE namespace=? AND id=?",(NS,s['id'])))
  else:pending.add((s['strategy'],s['symbol'],s['side']))
 strategy_audit={};qual=[];created=0;funnel={str(i):{'current_symbols':0,'individual_pass_counts':{},'sequential_pass_counts':{},'qualified':0} for i in range(1,8)};skipped=[sym for sym in syms if sym not in set(raw.symbol)]
 for sym,g in raw.groupby('symbol'):
  g=g.sort_values('trade_date').reset_index(drop=True)
  if str(g.iloc[-1].trade_date)!=str(target):skipped.append(sym);continue
  for strategy in range(1,8):
   key=(strategy,sym);p=pos.get(key);gate=gates[sym];gatepass=gate['status']=='PASS'
   if gatepass or p:f=indicator(strategy,g)
   else:f=g.assign(entry_condition=False,exit_condition=False)
   x=f.iloc[-1];bar=str(x.trade_date)
   if p:audit_events.append(al.event(sys.modules[__name__],'held_exit_evaluation',strategy=strategy,symbol=sym,bar_date=bar,trigger=bool(x.exit_condition),reason='completed_daily_exit',target_defined=False))
   stage2=au.checklist(sys.modules[__name__],strategy,f) if gatepass else {'all_filters_satisfied':None,'checks':{},'reason':'NOT_EVALUATED_PREFILTER'}
   checklist={**stage2,'prefilter':gate,'entry_evaluated':gatepass,'input_policy':input_details[sym]}
   strategy_audit.setdefault(sym,[]).append({'strategy':strategy,'outcome':'qualified' if gatepass and bool(x.entry_condition) else 'rejected' if gatepass else 'not_evaluated','checks':stage2.get('checks',{}),'reason':'ok' if gatepass else 'NOT_EVALUATED_PREFILTER'})
   stm.append(('INSERT INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(namespace,bar_date,strategy,symbol) DO UPDATE SET qualified=excluded.qualified,checklist_json=excluded.checklist_json,plan_json=excluded.plan_json,scanned_at=excluded.scanned_at',(NS,bar,strategy,sym,int(gatepass and bool(x.entry_condition)),js(checklist),js(au.plan(sys.modules[__name__],strategy,x)),t.isoformat())))
   checks=stage2.get('checks',{});counts=funnel[str(strategy)];counts['current_symbols']+=int(gatepass);sofar=True
   for name,passed in checks.items():
    counts['individual_pass_counts'][name]=counts['individual_pass_counts'].get(name,0)+int(passed);sofar=sofar and passed;counts['sequential_pass_counts'][name]=counts['sequential_pass_counts'].get(name,0)+int(sofar)
   counts['qualified']+=int(gatepass and bool(x.entry_condition))
   if gatepass and x.entry_condition:qual.append({'strategy':strategy,'symbol':sym,'bar_date':bar,'held':bool(p),'close':float(x.close),'indicator_values_json':json.dumps(x.to_dict(),default=str)})
   if snapshot_only:continue
   previous=wm.get(key)
   if bootstrap or previous is None:
    stm.append(('INSERT INTO paper_watermarks(namespace,strategy,symbol,bar_date) VALUES(?,?,?,?) ON CONFLICT(namespace,strategy,symbol) DO UPDATE SET bar_date=excluded.bar_date',(NS,strategy,sym,bar)));continue
   if previous>=bar:continue
   newbars=f[(f.trade_date>previous)&(f.trade_date>=datetime.fromisoformat(p['entry_at']).date().isoformat())] if p else f.iloc[0:0]
   missed_exit=bool(newbars.exit_condition.any()) if len(newbars) else False
   side='SELL' if p and missed_exit else ('BUY' if gatepass and not p and bool(x.entry_condition) else None)
   if side and (strategy,sym,side) not in pending:
    sid=stable_id(strategy,sym,bar,side);eligible=str(next_session(target));stm.append(('INSERT OR IGNORE INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',(NS,sid,strategy,sym,bar,side,'pending','daily_exit:'+p['entry_at'] if side=='SELL' else 'daily_entry:'+pf.POLICY['version'],t.isoformat(),eligible)));created+=1
    audit_events.append(al.event(sys.modules[__name__],'paper_order_queued',strategy=strategy,symbol=sym,signal_id=sid,bar_date=bar,side=side,reason='completed_daily_exit' if side=='SELL' else 'qualified'))
    if side=='BUY':
     stm.append(('INSERT OR IGNORE INTO paper_entry_gate_proofs VALUES(?,?,?,?,?)',(NS,sid,bar,sym,pf.POLICY['version'])))
     stm.append(('INSERT OR IGNORE INTO paper_entry_input_proofs VALUES(?,?,?,?,?)',(NS,sid,si.POLICY['version'],bar,t.isoformat())))
   if p and strategy==7:
    # Only completed closes on/after the actual paper entry can raise the stop.
    since=g[(g.trade_date>=datetime.fromisoformat(p['entry_at']).date().isoformat())&(g.trade_date<=bar)]
    H=max(p['high_water'],float(since.close.max())) if len(since) else p['high_water'];stm.append(('UPDATE paper_positions SET high_water=MAX(high_water,?),updated_at=? WHERE namespace=? AND strategy=7 AND symbol=? AND qty>0',(H,t.isoformat(),NS,sym)))
   stm.append(('UPDATE paper_watermarks SET bar_date=? WHERE namespace=? AND strategy=? AND symbol=?',(bar,NS,strategy,sym)))
 for sym in sorted(set(skipped)):
  if not snapshot_only:
   for strategy in range(1,8):stm.append(('INSERT INTO paper_watermarks VALUES(?,?,?,?) ON CONFLICT(namespace,strategy,symbol) DO UPDATE SET bar_date=excluded.bar_date',(NS,strategy,sym,str(target))))
  for strategy in range(1,8):stm.append(('INSERT INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(namespace,bar_date,strategy,symbol) DO UPDATE SET qualified=0,checklist_json=excluded.checklist_json,scanned_at=excluded.scanned_at',(NS,str(target),strategy,sym,0,js({'data_current':False,'reason':'NOT_EVALUATED_PREFILTER','prefilter':gates[sym],'entry_evaluated':False,'all_filters_satisfied':None}),'{}',t.isoformat())))
 for sym,decisions in strategy_audit.items():audit_events.append(al.event(sys.modules[__name__],'strategy_decisions',symbol=sym,bar_date=str(target),decisions=decisions))
 for sym in sorted(set(skipped)):audit_events.append(al.event(sys.modules[__name__],'strategy_decisions',outcome='not_evaluated',symbol=sym,bar_date=str(target),decisions=[{'strategy':i,'outcome':'not_evaluated','reason':'stale_stock_or_benchmark'} for i in range(1,8)]))
 stm+=al.statements(audit_events)
 if not NS.startswith('test_'):pd.DataFrame(qual).to_csv(ROOT/'out/latest_qualifiers.csv',index=False)
 report={'input_validation':input_audit,'target_completed_bar':str(target),'bootstrap':bootstrap,'funnel':funnel,'universe_symbols':len(syms),'frozen_universe_symbols':len(universe()),'excluded_audit_symbols':len(exclusions),'original_audit_universe':len(universe())+len(exclusions),'signals_queued':created,'latest_qualifiers':len(qual),'unavailable_current_symbols':skipped,'invalid_rows_excluded':int((~valid).sum()),'sync':sync_report,'no_backlog_fills':True,'screen_only':snapshot_only,'prefilter_policy':pf.POLICY,'prefilter_counts':{k:sum(r['status']==k for r in gates.values()) for k in ['PASS','FAIL','UNAVAILABLE']},'benchmark_sync_error':benchmark_error,'history_repair':repair_report,'reference_feeds':reference_report,'data_refresh_status':repair_report.get('status') if repair_report else 'not_requested'};stm.append(('INSERT INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,bar_date) DO UPDATE SET scanned_at=excluded.scanned_at,status=excluded.status,report_json=excluded.report_json',(NS,str(target),t.isoformat(),'partial' if skipped else 'ok',js(report))));db.batch(stm);db.run('scan','partial' if skipped else 'ok',**report);return report

def scan(db,api,do_sync=False,bootstrap=False,snapshot_only=False):
 e=sys.modules[__name__];start=time.monotonic()
 with al.scope(e,db,'scan'):
  al.emit(e,db,'scan_started',screen_only=snapshot_only)
  try:r=_scan(db,api,do_sync,bootstrap,snapshot_only)
  except Exception as ex:
   al.emit(e,db,'scan_failed',outcome='failed',severity='ERROR',error_code=type(ex).__name__);raise
  al.emit(e,db,'scan_completed',duration_ms=round((time.monotonic()-start)*1000),**r)
  return r

def entry_priority(day,s):
 if s.get('side')=='SELL':
  rank=2 if s.get('rule')=='trail_touch' else (0 if s.get('stop') else 1)
  return (0,rank,int(s.get('strategy') or 0),str(s.get('id') or ''))
 return (1,cap.slot_rank(day,s.get('symbol') or ''),int(s.get('strategy') or 0),str(s.get('id') or ''))
def _monitor(db,api):
 t=now()
 if not au.active(sys.modules[__name__],t):r={'status':'market_closed_or_calendar_unverified','at_IST':t.isoformat(),'fills':0};db.run('monitor','closed',**r);return r
 positions=db.positions();signals=db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(NS,));symbols=sorted({p['symbol'] for p in positions}|{s['symbol'] for s in signals})
 if not symbols:
  pz.evaluate(sys.modules[__name__],db,t.date().isoformat())
  al.emit(sys.modules[__name__],db,'monitor_idle',open_positions=0,pending_signals=0,poll_seconds=CFG['poll_seconds'])
  return {'status':'idle','fills':0,'at_IST':t.isoformat()}
 request_started=now();request_clock=time.monotonic();al.emit(sys.modules[__name__],db,'quote_request',request_started_at=request_started, symbols_count=len(symbols),open_positions=len(positions),pending_signals=len(signals))
 quotes=api.quotes(symbols);t=now();al.emit(sys.modules[__name__],db,'quote_response',request_started_at=request_started,response_received_at=t,duration_ms=round((time.monotonic()-request_clock)*1000),symbols_count=len(symbols))
 quote_rows=[]
 for sym,q in quotes.items():
  ok,why=valid_quote(q,t);quote_rows.append({'symbol':sym,'price':q.get('price'),'quote_at':q.get('quote_at'),'accepted':ok,'reason':why,'request_started_at':request_started,'response_received_at':t})
 for i in range(0,len(quote_rows),50):al.emit(sys.modules[__name__],db,'quote_checks',rows=quote_rows[i:i+50],count=len(quote_rows[i:i+50]))
 audit_quotes(db,quotes,t)
 opens={sym:q['open'] for sym,q in quotes.items() if isinstance(q,dict) and isinstance(q.get('open'),(int,float)) and not isinstance(q.get('open'),bool) and q['open']>0}
 ca.apply(sys.modules[__name__],db,t.date().isoformat(),opens)
 pz.evaluate(sys.modules[__name__],db,t.date().isoformat())
 positions=db.positions();fills=0;used=set();by={(p['strategy'],p['symbol']):p for p in positions}
 # Exits run before entries so a close frees a sleeve slot in the same poll. Entries then follow sha256(session, symbol, seed), then strategy 01-07.
 actions=[];position_rows=[]
 for p in positions:
  q=quotes.get(p['symbol']);ok,why=valid_quote(q,t)
  official=positive_price(q.get('open')) if q else None;low=positive_price(q.get('low')) if q else None
  level=trail_level(p['high_water']) if p['strategy']==7 else None
  stop_rule='trail_gap' if (ok and level is not None and official is not None and official<=level) else ('trail_touch' if (ok and level is not None and low is not None and low<=level) else None)
  triggered=stop_rule is not None;carried=bool(p.get('pending_exit'))
  position_rows.append({'strategy':p['strategy'],'symbol':p['symbol'],'quantity':p['qty'],'price':q.get('price') if q else None,'accepted':ok,'reason':why,'stop_defined':p['strategy']==7,'target_defined':False,'threshold':level,'high_water':p['high_water'],'trigger':triggered})
  if ok and (triggered or carried):
   actions.append({'id':stable_id(p['strategy'],p['symbol'],p['entry_at'],'pending' if carried else stop_rule),'strategy':p['strategy'],'symbol':p['symbol'],'side':'SELL','bar_date':t.date().isoformat(),'eligible_date':t.date().isoformat(),'reason':'pending_exit_carry' if carried else stop_rule,'rule':None if carried else stop_rule,'expected_entry_at':p['entry_at'],'stop':True,'pending_exit':carried,'stop_level':level})
 for i in range(0,len(position_rows),50):al.emit(sys.modules[__name__],db,'position_checks',rows=position_rows[i:i+50],count=len(position_rows[i:i+50]))
 actions+=signals;day=t.date().isoformat();actions.sort(key=lambda s:entry_priority(day,s))
 for s in actions:
  key=(s['strategy'],s['symbol'])
  if key in used:continue
  q=quotes.get(s['symbol'])
  if s.get('stop') and not s.get('pending_exit') and q:
   al.emit(sys.modules[__name__],db,'risk_hit',strategy=7,symbol=s['symbol'],price=q.get('open') if s.get('rule')=='trail_gap' else s.get('stop_level'),quote_at=q.get('quote_at'),threshold=s.get('stop_level'),reason=s.get('rule') or 'observed_stop_crossing',target_defined=False)
  r=fill(db,s,q,now())
  if r['filled']:fills+=1;used.add(key)
 # Entry-day stop cannot be hit by the exact same entry LTP; it is checked on subsequent fresh polls.
 # Positions change only through fill(); with no fill attempted this cycle the post-gate snapshot is current.
 # The price band feeds market_guard only for a carried exit's lower-circuit haircut, so it is read only then.
 st=[];t=now()
 for p in (db.positions() if actions else positions):
  q=quotes.get(p['symbol']);ok,_=valid_quote(q,t)
  if not ok:continue
  price=q['price']
  if p.get('pending_exit') and market_guard(q,band_for(db,p['symbol'],t.date().isoformat()))['circuit']=='LOWER':
   days=db.query('SELECT COUNT(*) AS n FROM paper_circuit_locks WHERE namespace=? AND strategy=? AND symbol=?',(NS,p['strategy'],p['symbol']))[0]['n'];marked=circuit_haircut(price,days)
   if marked is not None:price=marked
  st.append(('UPDATE paper_positions SET mark_price=?,mark_at=?,updated_at=? WHERE namespace=? AND strategy=? AND symbol=?',(price,q['quote_at'],t.isoformat(),NS,p['strategy'],p['symbol'])))
 db.batch(st);report={'status':'ok','fresh_quotes':sum(valid_quote(q,t)[0] for q in quotes.values()),'quotes':len(quotes),'fills':fills,'at_IST':t.isoformat()};db.run('monitor','ok',**report);return report

def monitor(db,api):
 e=sys.modules[__name__];start=time.monotonic()
 with al.scope(e,db,'monitor'):
  al.emit(e,db,'monitor_started',poll_seconds=CFG['poll_seconds'])
  r=_monitor(db,api)
  al.emit(e,db,'monitor_completed',status=r.get('status'),fills=r.get('fills',0),fresh_quotes=r.get('fresh_quotes',0),duration_ms=round((time.monotonic()-start)*1000),poll_seconds=CFG['poll_seconds'])
  return r

def prior_closes(db,symbols,day,chunk=None):
 """Latest positive close strictly before day for each symbol, as {symbol:[{trade_date,close}]} (empty list when none).
 Same row the per-symbol 'ORDER BY trade_date DESC LIMIT 1' lookup returns, fetched in a few round-trips."""
 chunk=chunk or CFG['pnl_prior_close_query_chunk'];out={s:[] for s in symbols}
 for i in range(0,len(symbols),chunk):
  sy=symbols[i:i+chunk];marks=','.join('?'*len(sy))
  for r in db.query('SELECT d.symbol,d.trade_date,d.close FROM daily_ohlcv d JOIN (SELECT symbol,MAX(trade_date) AS td FROM daily_ohlcv WHERE symbol IN ('+marks+') AND trade_date<? AND close>0 GROUP BY symbol) m ON d.symbol=m.symbol AND d.trade_date=m.td WHERE d.close>0',sy+[day]):
   if not out[r['symbol']]:out[r['symbol']]=[{'trade_date':r['trade_date'],'close':r['close']}]
 return out
def pnl(db):
 t=now();day=t.date().isoformat();ps=db.positions();fills=db.query('SELECT * FROM paper_fills WHERE namespace=?',(NS,));today=[x for x in fills if datetime.fromisoformat(x['filled_at']).astimezone(IST).date()==t.date()];realized=sum(x['realized_pnl'] for x in fills);unrealized=0.;unknown=[];stale=[]
 for p in ps:
  if p['mark_price'] is None:unknown.append(p['symbol']);continue
  unrealized+=(p['mark_price']-p['entry_price'])*p['qty']
  if not p['mark_at'] or (t-datetime.fromisoformat(p['mark_at'])).total_seconds()>CFG['quote_max_age_seconds']:stale.append(p['symbol'])
 keys={(p['strategy'],p['symbol']) for p in ps}|{(x['strategy'],x['symbol']) for x in today};daily=0.;missing=[]
 prior=prior_closes(db,sorted({sym for _,sym in keys}),day)
 for strategy,sym in keys:
  pos=next((p for p in ps if p['strategy']==strategy and p['symbol']==sym),None);ff=[x for x in today if x['strategy']==strategy and x['symbol']==sym];endqty=pos['qty'] if pos else 0;startqty=endqty-sum(x['qty'] if x['side']=='BUY' else -x['qty'] for x in ff)
  base=0
  if startqty:
   r=prior.get(sym)
   if not r or r[0]['trade_date']!=str(completed_day(t.replace(hour=0,minute=0,second=0,microsecond=0))):missing.append(sym);continue
   base=startqty*r[0]['close']
  if endqty and pos['mark_price'] is None:missing.append(sym);continue
  daily+=(endqty*pos['mark_price'] if pos else 0)-base+sum(x['cashflow'] for x in ff)
 report={'namespace':NS,'as_of_IST':t.isoformat(),'today_fills':len(today),'open_positions':len(ps),'cumulative_realized_pnl_INR':realized,'latest_known_unrealized_pnl_INR':None if unknown else unrealized,'latest_known_total_pnl_INR':None if unknown else realized+unrealized,'daily_mark_to_market_pnl_INR':None if missing else daily,'realized_on_closures_today_INR':sum(x['realized_pnl'] for x in today),'missing_marks':unknown,'stale_mark_symbols':sorted(set(stale)),'missing_daily_basis_symbols':missing,'daily_formula':'ending marked value - prior-close basis + sell proceeds - buy payments; legacy GROSS before modeled costs','valuation_is_live':is_open(t) and not(stale or unknown),'capital_return_pct':None,'costs_INR':None}
 fin=au.finance(sys.modules[__name__],db);capped=fin['capped_master_ledger']
 report['estimated_finance']=fin;report['costs_INR']=capped['costs_INR'];report['capital_return_pct']=capped['capital_return_pct'];report['headline_pnl_source']='capped_master_ledger';report['headline_net_pnl_paise']=capped['headline_net_pnl_paise'];report['headline_net_pnl_INR']=capped['headline_net_pnl_INR'];report['capped_master_ledger']=capped;report['public_labels']=capped['public_labels']
 db.batch([('INSERT INTO paper_pnl(namespace,day,as_of,report_json) VALUES(?,?,?,?) ON CONFLICT(namespace,day) DO UPDATE SET as_of=excluded.as_of,report_json=excluded.report_json',(NS,day,t.isoformat(),js(report)))]);return report

def init(db):
 al.init(db)
 db.batch([(gd.SCHEMA,())])
 si.init(db)
 pf.init(sys.modules[__name__],db)
 db.batch([(hr.SCHEMA,())])
 stm=[(sql.strip(),()) for sql in (ROOT/'schema.sql').read_text().split(';') if sql.strip()];db.batch(stm);migrate(db);db.batch([('INSERT OR IGNORE INTO paper_meta(namespace,key,value) VALUES(?,?,?)',(NS,'mode','PAPER_ONLY')),('INSERT OR IGNORE INTO paper_meta(namespace,key,value) VALUES(?,?,?)',(NS,'automation_schema_version','2'))]);db.call({'action':'register_policy'});return {'schema':'ready','namespace':NS,'paper_only':True}
def migrate(db):
 cols=db.query('PRAGMA table_info(paper_fills)')
 if cols and not any(c.get('name')=='execution_path' for c in cols):
  db.batch([("ALTER TABLE paper_fills ADD COLUMN execution_path TEXT NOT NULL DEFAULT 'LTP_LEGACY'",())])
 pos=db.query('PRAGMA table_info(paper_positions)')
 if pos and not any(c.get('name')=='pending_exit' for c in pos):
  db.batch([('ALTER TABLE paper_positions ADD COLUMN pending_exit INTEGER NOT NULL DEFAULT 0',()),('ALTER TABLE paper_positions ADD COLUMN pending_exit_since TEXT',())])
def main():
 parser=argparse.ArgumentParser(description='Paper-only Fyers-data/Turso-ledger engine; never broker orders.')
 parser.add_argument('command',choices=['init','probe','bootstrap','scan','monitor-once','positions','trades-today','pnl','status','worker','ensure-monitor','health','screen-only','finance','charges','overlap','funnel','eod','weekly','repair-history','ensure-session','guardian','replay-audit','export-logs','support','reference-feeds']);parser.add_argument('--sync',action='store_true');parser.add_argument('--max-history-requests',type=int,default=20);parser.add_argument('--max-history-seconds',type=int,default=240);parser.add_argument('--session',default=None);parser.add_argument('--format',choices=['jsonl','csv'],default='jsonl');parser.add_argument('--max-events',type=int,default=200000);parser.add_argument('--max-files',type=int,default=20);args=parser.parse_args()
 if not 1<=args.max_history_requests<=1100 or not 1<=args.max_history_seconds<=1080:parser.error('historical repair budget outside safe bounds')
 if args.command in ['worker','ensure-monitor','health','ensure-session','guardian','replay-audit','export-logs','support']:
  # Supervision must remain reachable even while a worker/scan holds engine.lock.
  try:
   db=DB();e=sys.modules[__name__]
   if args.command=='export-logs':
    if not args.session or not 1<=args.max_events<=200000:raise ValueError('export_arguments')
    print(js(al.export(e,db,args.session,args.format,args.max_events)))
   elif args.command=='replay-audit':
    if not 1<=args.max_files<=1000:raise ValueError('replay_arguments')
    print(js(al.replay(e,db,args.max_files)))
   elif args.command=='support':
    print(js(sa.run_cli(e,db,args.session)))
   else:
    fn={'worker':au.worker,'ensure-monitor':au.ensure_monitor,'health':au.health,'ensure-session':gd.ensure,'guardian':gd.run}[args.command]
    with al.scope(e,db,args.command):print(js(fn(e,db)))
  except Exception as ex:
   print(js({'status':'failed','error':au.safe_error(ex),'paper_only':True}));sys.exit(1)
  return
 with (ROOT/'engine.lock').open('a+') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:print(js({'status':'another_engine_command_running'}));return
  db=DB()
  audit_scope=al.scope(sys.modules[__name__],db,args.command) if args.command!='init' else contextlib.nullcontext()
  try:
   with audit_scope:
    if args.command=='init':out=init(db)
    elif args.command=='probe':
     api=MarketData();q=api.quotes(['RELIANCE-EQ']);t=now();audit_quotes(db,q,t);out={'market_open':is_open(t),'at_IST':t.isoformat(),'quotes':q,'acceptance':{k:valid_quote(v,t) for k,v in q.items()},'orders_sent_to_broker':0,'token_refreshed':api.refreshed};db.run('probe','ok',**out)
    elif args.command=='repair-history':out=hr.repair(sys.modules[__name__],db,MarketData(),completed_day(now()),budget=args.max_history_requests,deadline_seconds=args.max_history_seconds,progress=str(ROOT/'out/history-repair-progress.json'))
    elif args.command in ['bootstrap','scan','screen-only']:out=scan(db,MarketData(),args.sync,args.command=='bootstrap',args.command=='screen-only')
    elif args.command=='monitor-once':
     out=monitor(db,MarketData())
     if au.active(sys.modules[__name__],now()):out['pnl']=pnl(db)
     else:out['snapshot_skipped_market_closed']=True
    elif args.command=='positions':out=db.positions()
    elif args.command=='trades-today':out=[x for x in db.query('SELECT * FROM paper_fills WHERE namespace=? ORDER BY id',(NS,)) if datetime.fromisoformat(x['filled_at']).astimezone(IST).date()==now().date()]
    elif args.command=='pnl':out=pnl(db)
    elif args.command=='status':out={'paper_only':True,'namespace':NS,'market_open':is_open(now()),'calendar_valid_through':CFG['calendar_valid_through'],'last_runs':db.query('SELECT kind,at,status,details_json FROM paper_runs WHERE namespace=? ORDER BY id DESC LIMIT 5',(NS,)),'pending':db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(NS,)),'pnl':pnl(db)}
    elif args.command=='ensure-monitor':out=au.ensure_monitor(sys.modules[__name__],db)
    elif args.command=='health':out=au.health(sys.modules[__name__],db)
    elif args.command=='finance':out=au.finance(sys.modules[__name__],db)
    elif args.command=='charges':out=db.query('SELECT fill_id,day,strategy,symbol,side,policy_version,components_json,total_paise FROM paper_fill_costs WHERE namespace=? ORDER BY fill_id',(NS,))
    elif args.command=='overlap':out=au.overlap(sys.modules[__name__],db)
    elif args.command=='funnel':out=au.funnel(sys.modules[__name__],db)
    elif args.command=='eod':out=au.eod(sys.modules[__name__],db)
    elif args.command=='weekly':out=au.weekly(sys.modules[__name__],db)
    elif args.command=='reference-feeds':out=rf.refresh(sys.modules[__name__],db,reference_day(now()))
    log('command_completed',command=args.command,namespace=NS)
    print(js(out))
  except Exception as e:
   # Exception contents can contain remote request details; only vetted internal error labels exposed.
   reason=au.safe_error(e);log('command_failed',command=args.command,error=reason)
   try:db.run(args.command,'failed',error=reason)
   except Exception:pass
   print(js({'status':'failed','error':reason,'paper_only':True}));sys.exit(1)
if __name__=='__main__':main()
