"""Session supervision and additive analytics. No broker order endpoints."""
import contextlib,fcntl,json,os,signal,subprocess,sys,threading,time
from datetime import datetime,date,timedelta,time as walltime
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
import audit as al
import runtime_deps as rd
import official_close as oc
import capital as cap

def cents(v):return int((Decimal(str(v))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def safe_error(exc):
 s=str(exc)
 return s if s and len(s)<90 and all(c.isalnum() or c=='_' for c in s) else type(exc).__name__
def active(e,t):return t.weekday()<5 and e.is_open(t)
def clean(v):
 import numpy as np
 if isinstance(v,dict):return {k:clean(x) for k,x in v.items()}
 if isinstance(v,(list,tuple)):return [clean(x) for x in v]
 if isinstance(v,np.generic):return clean(v.item())
 if isinstance(v,float) and not __import__('math').isfinite(v):return None
 return v

def checklist(e,strategy,f):
 x=f.iloc[-1];c=x.close
 # Conditions mirror the frozen indicator functions, not relaxed state substitutes.
 try:
  p=f.iloc[-2]
  conditions={
   1:lambda:{'close_above_prior20_high':c>f.high.shift().rolling(20).max().iloc[-1]},
   2:lambda:{'prior_red':x.prior_direction==1,'current_green':x.direction==-1,'close_above_supertrend':c>x.supertrend},
   3:lambda:{'close_above_sma200':c>x.sma200,'rsi14_below40':x.rsi14<40},
   4:lambda:{'ema20_above_ema50':x.ema20>x.ema50,'prior_ema20_at_or_below_ema50':p.ema20<=p.ema50,'close_above_sma200':c>x.sma200},
   5:lambda:{'close_at_or_below_lower_band':c<=x.bb_lower,'close_above_sma200':c>x.sma200},
   6:lambda:{'macd_above_signal':x.macd>x.signal,'prior_macd_at_or_below_signal':p.macd<=p.signal,'close_above_sma50':c>x.sma50},
   7:lambda:{'close_above_sma200':c>x.sma200,'within10pct_high252':bool(x.within10pct)}}
  checks={k:bool(v) for k,v in conditions[strategy]().items()}
 except (AttributeError,IndexError):checks={'entry_condition':bool(x.entry_condition)}
 return {'data_current':True,'checks':checks,'all_filters_satisfied':bool(x.entry_condition),'rule':e.RULES[str(strategy)].get('entry',e.RULES[str(strategy)].get('long_entry')),'observed_values':clean(x.to_dict())}
def plan(e,strategy,x):
 r=e.RULES[str(strategy)]
 close=float(x.close)
 try:indicative=e.cap.integer_shares(close)
 except ValueError:indicative=None
 return {'entry':'Next eligible session official open from a fresh verified quote (FEED_OPEN), else auction or first-trade fallback (tagged); modeled slippage added; never a fabricated price','reference_close':close,'reference_bar':str(x.trade_date),'fixed_target':None,'exit_rule':r.get('exit',r.get('long_exit')),'stop':f"{round((1-e.TRAIL)*100,6):g}% below completed-close high-water from actual entry; completed daily bar only" if strategy==7 else 'No added fixed stop; frozen completed-candle exit rule','indicative_reference_trail':e.trail_level(close) if strategy==7 else None,'indicative_not_order_price':True,'target_notional_inr':e.cap.POLICY['target_notional_inr'],'share_rounding':'floor_notional_over_fill_price','indicative_shares_at_reference_close':indicative,'paper_only':True}


def _marked_positions(ps, overlay=None):
 """Return position copies, optionally overlaying official closes. Never writes back."""
 out=[]
 for p in ps:
  q=dict(p)
  if overlay and p['symbol'] in overlay:
   item=overlay[p['symbol']];q['mark_price']=item['price'];q['mark_at']=item['at'];q['_mark_source']=item['source']
  else:
   q['_mark_source']='last accepted fresh FYERS_QUOTES; not official closing price'
  out.append(q)
 return out

def finance(e,db,mark_overlay=None):
 t=e.now();day=str(t.date());ps=_marked_positions(db.positions(),mark_overlay);fs=db.query('SELECT * FROM paper_fills WHERE namespace=? AND strategy BETWEEN 1 AND 7',(e.NS,));cost=db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,));coh=db.query('SELECT * FROM paper_entry_cohorts WHERE namespace=?',(e.NS,))
 # Integer-paise reporting; gross legacy fields remain untouched.
 costs={r['fill_id']:r for r in cost};entrykeys={(p['strategy'],p['symbol'],p['entry_at']) for p in ps};cohorts={(r['strategy'],r['symbol'],r['entry_at']):r['cohort'] for r in coh}
 rows=[];missing=[];stale=[]
 for f in fs:
  cr=costs.get(f['id']);entry=cr['entry_at'] if cr else (f['filled_at'] if f['side']=='BUY' else None)
  rows.append({'strategy':f['strategy'],'cohort':cohorts.get((f['strategy'],f['symbol'],entry),'unavailable'),'gross':cents(f['realized_pnl']),'cost':cr['total_paise'] if cr else 0})
 for p in ps:
  if p['mark_price'] is None:missing.append(p['symbol']);continue
  val=int(((Decimal(str(p['mark_price']))-Decimal(str(p['entry_price'])))*p['qty']*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
  rows.append({'strategy':p['strategy'],'cohort':cohorts.get((p['strategy'],p['symbol'],p['entry_at']),'unavailable'),'gross':val,'cost':0})
  official=bool(str(p.get('_mark_source') or '').startswith('NSE_BHAVCOPY'));
  if not official and (not p['mark_at'] or (t-datetime.fromisoformat(p['mark_at'])).total_seconds()>e.CFG['quote_max_age_seconds']):stale.append(p['symbol'])
 uncovered=sum(f['id'] not in costs for f in fs)
 gross_realized=sum(cents(f['realized_pnl']) for f in fs);gross=sum(r['gross'] for r in rows);accrued=sum(r['total_paise'] for r in cost)
 open_entry=sum(r['total_paise'] for r in cost if r['side']=='BUY' and (r['strategy'],r['symbol'],r['entry_at']) in entrykeys);closed_cost=accrued-open_entry
 breakdown={}
 for r in cost:
  for k,v in json.loads(r['components_json']).items():
   if k!='total':breakdown[k]=breakdown.get(k,0)+v
 def groups(key):
  out={}
  for r in rows:
   k=str(r[key]);a=out.setdefault(k,{'gross_paise':0,'accrued_cost_paise':0,'net_accrued_paise':0});a['gross_paise']+=r['gross'];a['accrued_cost_paise']+=r['cost'];a['net_accrued_paise']+=r['gross']-r['cost']
  for p in ps:
   if p['mark_price'] is None:
    k=str(p['strategy']) if key=='strategy' else cohorts.get((p['strategy'],p['symbol'],p['entry_at']),'unavailable');a=out.setdefault(k,{'accrued_cost_paise':0});a.update(gross_paise=None,net_accrued_paise=None,valuation_incomplete=True)
  if uncovered:
   for r in out.values():r['net_accrued_paise']=None
  return out
 # A close-all scenario estimate, never accrued or booked; cap projected DP once per ISIN/day.
 projected=0;projection_missing=[];today=[r for r in cost if r['day']==day];prior=sum(r['turnover_paise'] for r in today);priorbuy=sum(r['turnover_paise'] for r in today if r['side']=='BUY');debited={r['isin'] for r in db.query('SELECT isin FROM paper_dp_debits WHERE namespace=? AND day=?',(e.NS,day))}
 for p in sorted(ps,key=lambda p:(p['symbol'],p['strategy'])):
  isin=e.ISINS.get(p['symbol'])
  if not isin or p['mark_price'] is None:projection_missing.append(p['symbol']);continue
  dp=p['entry_at'][:10]<day and isin not in debited
  f=db.call({'action':'estimate','price':p['mark_price'],'quantity':p['qty'],'side':'SELL','prior':prior,'priorBuy':priorbuy,'dp':dp});projected+=f['total'];prior+=cents(p['mark_price'])*p['qty']
  if dp:debited.add(isin)
 marked=sum(cents(p['mark_price'])*p['qty'] for p in ps if p['mark_price'] is not None);cashgross=sum(c['gross_cashflow_paise'] for c in cost)+marked;cashnet=sum(c['net_cashflow_paise'] for c in cost)+marked
 policy=json.loads((e.ROOT/'charge_policy.json').read_text())
 out={'namespace':e.NS,'as_of_IST':t.isoformat(),'day':day,'money_unit':'integer INR paise','estimated_not_actual_charges':True,'policy_version':policy['version'],'policy_current':policy['effective_from']<=day<=policy['valid_through'],'gross_realized_paise':gross_realized,'gross_unrealized_paise':None if missing else gross-gross_realized,'gross_total_paise':None if missing else gross,'accrued_cost_paise':accrued,'net_realized_paise':None if uncovered else gross_realized-closed_cost,'net_unrealized_accrued_paise':None if missing or uncovered else gross-gross_realized-open_entry,'net_total_accrued_paise':None if missing or uncovered else gross-accrued,'estimated_exit_cost_paise':None if projection_missing else projected,'net_after_estimated_exit_paise':None if missing or projection_missing or uncovered else gross-accrued-projected,'open_entry_cost_paise':open_entry,'charge_breakdown_paise':breakdown,'by_strategy':groups('strategy'),'by_entry_cohort':groups('cohort'),'fills_missing_cost_ledger':uncovered,'cost_coverage_complete':not uncovered,'ledger_reconciled':not(missing or uncovered) and cashgross==gross and cashnet==gross-accrued,'cashflow_net_paise':None if missing or uncovered else cashnet,'missing_marks':missing,'stale_mark_symbols':sorted(set(stale)),'valuation_is_live':active(e,t) and not(missing or stale),'funded_capital':None,'capital_return_pct':None,'mark_observations':[{'strategy':p['strategy'],'symbol':p['symbol'],'mark_at':p['mark_at'],'source':p.get('_mark_source') or 'last accepted fresh FYERS_QUOTES; not official closing price'} for p in ps]}
 out['capped_master_ledger']=capped_ledger(e,db,out,ps,cost)
 out['public_labels']=out['capped_master_ledger']['public_labels']
 return out

PUBLIC_LABELS={
 'historical_figures_gross':True,
 'survivorship_biased':True,
 'not_jointly_tested':True,
 'historical_disclaimer':'Historical backtest figures are gross of transaction costs, survivorship-biased on the present-day universe, and not jointly tested across strategies.',
 'headline_pnl_source':'capped_master_ledger',
 'shadow_book_diagnostic_only':True,
 'adv_floor_review':cap.POLICY['adv_floor_review'],
}

def capped_ledger(e,db,fin=None,ps=None,cost=None):
 if ps is None:ps=db.positions()
 if cost is None:cost=db.query('SELECT * FROM paper_fill_costs WHERE namespace=?',(e.NS,))
 accrued=fin['accrued_cost_paise'] if fin and fin.get('accrued_cost_paise') is not None else sum(int(r['total_paise']) for r in cost)
 master_cap=e.cap.POLICY['master_capital_inr']*100;sleeve_cap=e.cap.POLICY['sleeve_capital_inr']*100
 costs={};credits={}
 for r in cost:
  s=int(r['strategy']);costs[s]=costs.get(s,0)+int(r['net_cashflow_paise'])
 for r in db.query('SELECT strategy,COALESCE(SUM(amount_paise),0) AS n FROM paper_cash_credits WHERE namespace=? GROUP BY strategy',(e.NS,)):
  credits[int(r['strategy'])]=int(r['n'])
 shadow_costs={int(r['strategy']):int(r['n']) for r in db.query('SELECT strategy,COALESCE(SUM(net_cashflow_paise),0) AS n FROM paper_shadow_costs WHERE namespace=? GROUP BY strategy',(e.NS,))}
 shadows=db.query('SELECT strategy,symbol,qty,entry_price FROM paper_shadow_positions WHERE namespace=? AND qty>0',(e.NS,))
 sleeves={};missing=False;total_cash=0;total_marked=0
 for s in range(1,8):
  held=[p for p in ps if int(p['strategy'])==s];cash=sleeve_cap+costs.get(s,0)+credits.get(s,0);total_cash+=cash
  if any(p['mark_price'] is None for p in held):
   missing=True;sleeves[str(s)]={'sleeve_capital_paise':sleeve_cap,'cash_paise':cash,'market_value_paise':None,'equity_paise':None,'net_pnl_paise':None,'open_positions':len(held)}
  else:
   mv=sum(cents(p['mark_price'])*int(p['qty']) for p in held);total_marked+=mv;eq=cash+mv
   sleeves[str(s)]={'sleeve_capital_paise':sleeve_cap,'cash_paise':cash,'market_value_paise':mv,'equity_paise':eq,'net_pnl_paise':eq-sleeve_cap,'open_positions':len(held)}
 shadow_eq=sum(sleeve_cap+shadow_costs.get(s,0)+sum(cents(p['entry_price'])*int(p['qty']) for p in shadows if int(p['strategy'])==s) for s in range(1,8))
 master_eq=None if missing else total_cash+total_marked;net_paise=None if master_eq is None else master_eq-master_cap
 net_inr=None if net_paise is None else round(net_paise/100,2);ret_pct=None if net_paise is None else round((net_paise/master_cap)*100,4)
 labels={**PUBLIC_LABELS,'adv_floor_review':e.cap.POLICY['adv_floor_review'],'strategy_07_version':e.RULES['7'].get('version'),'strategy_07_evidence_clock':e.RULES['7'].get('evidence_clock')}
 return {'headline':True,'book':'capped','master_capital_paise':master_cap,'master_capital_INR':e.cap.POLICY['master_capital_inr'],'sleeve_capital_paise':sleeve_cap,'cash_paise':total_cash,'market_value_paise':None if missing else total_marked,'equity_paise':master_eq,'headline_net_pnl_paise':net_paise,'headline_net_pnl_INR':net_inr,'accrued_cost_paise':accrued,'costs_INR':round(accrued/100,2),'capital_return_pct':ret_pct,'sleeves':sleeves,'shadow_diagnostic':{'headline':False,'book':'shadow','equity_paise':shadow_eq,'net_pnl_paise':shadow_eq-master_cap,'open_positions':len(shadows)},'public_labels':labels}

def overlap(e,db,bar_date=None):
 if bar_date is None:
  r=db.query('SELECT MAX(bar_date) AS bar FROM paper_scan_batches WHERE namespace=?',(e.NS,));bar=r[0]['bar'] if r else None
 else:bar=str(bar_date)
 groups={}
 for x in db.query('SELECT symbol,strategy FROM paper_qualifier_states WHERE namespace=? AND bar_date=? AND qualified=1 ORDER BY symbol,strategy',(e.NS,bar)):
  groups.setdefault(x['symbol'],[]).append(int(x['strategy']))
 stocks=[{'symbol':s,'strategies':v,'count':len(v),'cohort':'single' if len(v)==1 else 'exactly_2' if len(v)==2 else 'exactly_3' if len(v)==3 else '4_plus'} for s,v in groups.items()]
 by_strategy={i:{s for s,v in groups.items() if i in v} for i in range(1,8)}
 matrix={str(i):{str(j):len(by_strategy[i]&by_strategy[j]) for j in range(1,8)} for i in range(1,8)}
 return {'as_of_bar':bar,'stocks':stocks,'bucket_counts':{k:sum(x['cohort']==k for x in stocks) for k in ['single','exactly_2','exactly_3','4_plus']},'at_least_2':sum(x['count']>=2 for x in stocks),'at_least_3':sum(x['count']>=3 for x in stocks),'matrix':matrix,'isin_cap_impact_symbols':[x['symbol'] for x in stocks if x['count']>e.cap.POLICY['max_strategies_per_isin']],'execution':'Independent virtual strategy holdings; not a combined funded portfolio. Entry attribution is immutable, never this latest cohort.'}

def funnel(e,db,bar_date=None):
 ov=overlap(e,db,bar_date);bar=ov['as_of_bar']
 pre=db.query('SELECT status,COUNT(*) AS n FROM paper_prefilter_states WHERE namespace=? AND bar_date=? GROUP BY status',(e.NS,bar)) if bar else []
 pre_counts={k:0 for k in ('PASS','FAIL','UNAVAILABLE')}
 for r in pre:
  if r['status'] in pre_counts:pre_counts[r['status']]=int(r['n'])
 q_rows=db.query('SELECT symbol,strategy FROM paper_qualifier_states WHERE namespace=? AND bar_date=? AND qualified=1 ORDER BY symbol,strategy',(e.NS,bar)) if bar else []
 syms=sorted({r['symbol'] for r in q_rows});lookback=e.cap.POLICY['adv_lookback_sessions'];floor_paise=e.cap.POLICY['adv_floor_inr']*100;adv_ok={}
 if syms:
  try:
   special=sorted(e.CFG.get('indicator_excluded_sessions',{}));ex=(' AND trade_date NOT IN ('+','.join('?' for _ in special)+')') if special else ''
   ohlcv=db.query('SELECT symbol,trade_date,COALESCE(turnover,CASE WHEN volume>0 AND close>0 THEN close*volume END) AS turnover FROM daily_ohlcv WHERE symbol IN ('+','.join('?' for _ in syms)+') AND trade_date<=?'+ex+' AND (turnover IS NOT NULL OR (volume>0 AND close>0)) ORDER BY symbol,trade_date DESC',[ *syms,str(bar),*special])
  except Exception:ohlcv=[]
  by_sym={}
  for r in ohlcv:
   bucket=by_sym.setdefault(r['symbol'],[])
   if len(bucket)<lookback:bucket.append(round(float(r['turnover'])*100))
  for sym in syms:
   vals=sorted(by_sym.get(sym,[]))
   adv_ok[sym]=len(vals)>=lookback and (vals[lookback//2] if lookback%2 else (vals[lookback//2-1]+vals[lookback//2])/2)>=floor_paise
 open_by_strat={i:0 for i in range(1,8)};open_by_sym={}
 for p in db.positions():
  s=int(p['strategy']);open_by_strat[s]=open_by_strat.get(s,0)+1;open_by_sym.setdefault(p['symbol'],set()).add(s)
 per_strat={str(i):{'strategy_qualified':0,'adv_floor_pass':0,'adv_floor_skipped':0,'isin_cap_pass':0,'isin_cap_skipped':0,'sleeve_slot_pass':0,'sleeve_slot_skipped':0} for i in range(1,8)}
 sym_admitted={sym:len(open_by_sym.get(sym,set())) for sym in syms};slot_used=dict(open_by_strat)
 for r in q_rows:
  sym=r['symbol'];s=int(r['strategy']);st=per_strat[str(s)];st['strategy_qualified']+=1
  if not adv_ok.get(sym,False):st['adv_floor_skipped']+=1;continue
  st['adv_floor_pass']+=1
  if sym_admitted.get(sym,0)>=e.cap.POLICY['max_strategies_per_isin']:st['isin_cap_skipped']+=1;continue
  sym_admitted[sym]=sym_admitted.get(sym,0)+1;st['isin_cap_pass']+=1
  if slot_used.get(s,0)>=e.cap.POLICY['max_positions_per_sleeve']:st['sleeve_slot_skipped']+=1;continue
  slot_used[s]=slot_used.get(s,0)+1;st['sleeve_slot_pass']+=1
 totals={k:sum(per_strat[str(i)][k] for i in range(1,8)) for k in ('strategy_qualified','adv_floor_pass','adv_floor_skipped','isin_cap_pass','isin_cap_skipped','sleeve_slot_pass','sleeve_slot_skipped')}
 decisions={}
 for r in db.query('SELECT cause,COUNT(*) AS n FROM paper_decisions WHERE namespace=? GROUP BY cause',(e.NS,)):
  decisions[str(r['cause'])]=int(r['n'])
 return {'as_of_bar':bar,'adv_floor_inr':e.cap.POLICY['adv_floor_inr'],'adv_lookback_sessions':lookback,'adv_floor_review':e.cap.POLICY['adv_floor_review'],'stages':{'universe_evaluated':sum(pre_counts.values()),'prefilter_pass':pre_counts['PASS'],**totals},'prefilter_counts':pre_counts,'by_strategy':per_strat,'recorded_decision_causes':decisions,'overlap':ov,'public_labels':PUBLIC_LABELS}

def _closing_quality(r, close, t, near_seconds, official=False):
 quality=[]
 for x in r['mark_observations']:
  qt=datetime.fromisoformat(x['mark_at']) if x['mark_at'] else None
  if official and qt:
   quality.append({**x,'seconds_before_session_close':0,'near_close':True})
  else:
   distance=(close-qt).total_seconds() if qt else None;quality.append({**x,'seconds_before_session_close':distance,'near_close':bool(qt and qt.date()==t.date() and 0<=distance<=near_seconds)})
 return quality

def _eod(e,db):
 t=e.now();ss=e.session(t.date())
 if t.weekday()>=5 or not ss or t.timetz().replace(tzinfo=None)<e.COMPLETED_CUTOFF:return {'status':'not_verified_weekday_eod','snapshot_written':False}
 r=finance(e,db);prior=db.query('SELECT day,report_json FROM paper_eod WHERE namespace=? AND day<? ORDER BY day DESC LIMIT 1',(e.NS,str(t.date())))
 expected=str(e.completed_day(t.replace(hour=0,minute=0,second=0,microsecond=0)));before=db.query('SELECT COUNT(*) AS n FROM paper_fills WHERE namespace=? AND substr(filled_at,1,10)<?',(e.NS,str(t.date())))[0]['n']
 p=json.loads(prior[0]['report_json']) if prior else None
 close=datetime.combine(t.date(),ss[1],tzinfo=e.IST)
 quality=_closing_quality(r,close,t,e.CFG['eod_near_close_seconds']);r['closing_mark_quality']=quality
 live_complete=not r['missing_marks'] and all(x['near_close'] for x in quality)
 r['official_close']=False;r['official_close_source']=None;r['official_close_status']='not_needed' if live_complete else 'not_applied';r['official_close_reason']=None
 if not live_complete:
  symbols=sorted({pos['symbol'] for pos in db.positions()})
  fetched=oc.closes_for(e,t.date(),symbols)
  r['official_close_status']=fetched.get('status');r['official_close_reason']=fetched.get('reason')
  if fetched.get('status')=='ok' and set(symbols)<=set(fetched.get('closes') or {}):
   overlay={sym:{'price':float(px),'at':close.isoformat(),'source':fetched['source']} for sym,px in fetched['closes'].items()}
   r=finance(e,db,mark_overlay=overlay)
   quality=_closing_quality(r,close,t,e.CFG['eod_near_close_seconds'],official=True)
   r['closing_mark_quality']=quality
   r['official_close']=True;r['official_close_source']=fetched['source'];r['official_close_status']='applied';r['official_close_reason']=fetched.get('reason')
 r['closing_valuation_complete']=not r['missing_marks'] and all(x['near_close'] for x in r['closing_mark_quality'])
 known=bool(prior and prior[0]['day']==expected and p.get('closing_valuation_complete')) or (not prior and not before)
 r['daily_baseline_day']=prior[0]['day'] if prior else None;r['daily_baseline_known']=known;r['daily_gross_paise']=None;r['daily_net_accrued_paise']=None
 if known and r['closing_valuation_complete']:
  for key,out in [('gross_total_paise','daily_gross_paise'),('net_total_accrued_paise','daily_net_accrued_paise')]:
   if r[key] is not None and (p is None or p.get(key) is not None):r[out]=r[key]-(p[key] if p else 0)
 if r.get('official_close'):
  r['missing_eod_warning']=None
 else:
  r['missing_eod_warning']='Last accepted in-session marks, potentially stale; never fabricated official close. Official NSE close used only when the UDiFF CM bhavcopy is available for every open symbol. Missing daily baseline means daily P&L unavailable.'
 try:r['performance']=performance(e,db,r,str(t.date()))
 except Exception as ex:r['performance']={'status':'unavailable','reason':safe_error(ex)}
 existing=db.query('SELECT as_of,report_json FROM paper_eod WHERE namespace=? AND day=?',(e.NS,str(t.date())))
 if existing:
  r['previous_snapshot_as_of']=existing[0]['as_of']
  try:
   out=al.out_dir(e)
   stamp=existing[0]['as_of'].replace(':','').replace('/','').replace(' ','')
   trail=out/('eod-'+str(t.date())+'-prior-'+stamp+'.json')
   fd=os.open(trail,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
   with os.fdopen(fd,'w') as f:f.write(existing[0]['report_json']);f.flush();os.fsync(f.fileno())
   r['previous_snapshot_path']=str(trail)
  except FileExistsError:
   r['previous_snapshot_path']=str(out/('eod-'+str(t.date())+'-prior-'+stamp+'.json'))
  except Exception:
   pass
 db.batch([('INSERT INTO paper_eod(namespace,day,as_of,report_json) VALUES(?,?,?,?) ON CONFLICT(namespace,day) DO UPDATE SET as_of=excluded.as_of,report_json=excluded.report_json',(e.NS,str(t.date()),t.isoformat(),e.js(r)))]);return r

def performance(e,db,r,day):
 """EOD/report metrics from the real ledger (paper_v1 only): closed-trade expectancy, drawdown on EOD snapshots,
 cost-to-gross and implementation shortfall. Values are None, never guessed, when the inputs do not exist yet."""
 fills=db.query('SELECT f.id,f.strategy,f.symbol,f.side,f.qty,f.price,f.realized_pnl,f.filled_at,f.execution_path,s.bar_date FROM paper_fills f LEFT JOIN paper_signals s ON s.namespace=f.namespace AND s.id=f.signal_id WHERE f.namespace=? AND f.strategy BETWEEN 1 AND 7 AND substr(f.filled_at,1,10)<=? ORDER BY f.id',(e.NS,day))
 closed=db.query("SELECT strategy,trade_id,net_pnl_paise,closed_at FROM paper_closed_trades WHERE namespace=? AND book='capped' AND substr(closed_at,1,10)<=?",(e.NS,day))
 sells=[f for f in fills if f['side']=='SELL'];gross=[cents(f['realized_pnl']) for f in sells];net=[int(c['net_pnl_paise']) for c in closed]
 exp={'closed_trades':len(sells),'expectancy_gross_paise':round(sum(gross)/len(gross)) if gross else None,'expectancy_net_paise':round(sum(net)/len(net)) if net else None,
      'win_rate_net':round(sum(x>0 for x in net)/len(net),4) if net else None,'definition':'mean per closed round trip; net uses paper_closed_trades (book=capped, both legs estimated charges)'}
 series=[]
 for x in db.query('SELECT day,report_json FROM paper_eod WHERE namespace=? AND day<? ORDER BY day',(e.NS,day)):
  try:v=json.loads(x['report_json']).get('net_total_accrued_paise')
  except (TypeError,ValueError):v=None
  if v is not None:series.append((x['day'],int(v)))
 if r.get('net_total_accrued_paise') is not None:series.append((day,int(r['net_total_accrued_paise'])))
 peak=None;peak_day=None;mdd=0;mdd_from=None;mdd_to=None
 for d,v in series:
  if peak is None or v>peak:peak,peak_day=v,d
  if v-peak<mdd:mdd,mdd_from,mdd_to=v-peak,peak_day,d
 dd={'max_drawdown_net_paise':mdd if series else None,'peak_day':mdd_from,'trough_day':mdd_to,'current_drawdown_net_paise':(series[-1][1]-max(v for _,v in series)) if series else None,
     'snapshot_days':[d for d,_ in series],'basis':'net_total_accrued_paise on stored EOD snapshots; days without a snapshot are absent, not interpolated'}
 g=r.get('gross_total_paise');c=r.get('accrued_cost_paise')
 ctg={'accrued_cost_paise':c,'gross_total_paise':g,'cost_to_gross':round(c/g,4) if (c is not None and g and g>0) else None,'cost_to_abs_gross':round(c/abs(g),4) if (c is not None and g) else None}
 rows=[];notional=0;wsum_close=0;wsum_open=0;n_open=0
 for f in fills:
  sc=db.query('SELECT close FROM daily_ohlcv WHERE symbol=? AND trade_date=?',(f['symbol'],f['bar_date'])) if f['bar_date'] else []
  op=db.query('SELECT open FROM daily_ohlcv WHERE symbol=? AND trade_date=?',(f['symbol'],f['filled_at'][:10]))
  sign=1 if f['side']=='BUY' else -1
  vs_close=round(sign*(f['price']/sc[0]['close']-1)*1e4,2) if sc and sc[0]['close'] else None
  vs_open=round(sign*(f['price']/op[0]['open']-1)*1e4,2) if op and op[0]['open'] else None
  rows.append({'fill_id':f['id'],'side':f['side'],'symbol':f['symbol'],'execution_path':f['execution_path'],'bps_vs_signal_close':vs_close,'bps_vs_session_open':vs_open})
  w=f['qty']*f['price']
  if vs_close is not None:notional+=w;wsum_close+=w*vs_close
  if vs_open is not None:n_open+=w;wsum_open+=w*vs_open
 isf={'fills':len(rows),'weighted_bps_vs_signal_close':round(wsum_close/notional,2) if notional else None,'weighted_bps_vs_session_open':round(wsum_open/n_open,2) if n_open else None,
      'today':[x for x in rows if any(f['id']==x['fill_id'] and f['filled_at'][:10]==day for f in fills)],'definition':'positive bps = worse than reference (buy above / sell below); reference = signal-bar close, and the fill-day official open from daily_ohlcv'}
 return {'status':'ok','expectancy':exp,'drawdown':dd,'cost_to_gross':ctg,'implementation_shortfall':isf}

def _preserve_prior(e,day,existing):
 """Copy an EOD snapshot that is about to be replaced into out/ (never overwritten); returns the path.
 Test namespaces write under out/test-exports/<namespace>/ (the audit.out_dir pattern), so a test
 run can never leave a prior snapshot in the production out/ directory."""
 out=al.out_dir(e)
 stamp=existing['as_of'].replace(':','').replace('/','').replace(' ','')
 trail=out/('eod-'+str(day)+'-prior-'+stamp+'.json')
 try:
  fd=os.open(trail,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:f.write(existing['report_json']);f.flush();os.fsync(f.fileno())
 except FileExistsError:pass
 return str(trail)

def revalue_eod(e,db,day,fetch=None):
 """Re-value a past (or today's completed) session from the real NSE UDiFF CM bhavcopy close, clearly labelled.
 Refuses when the ledger changed after that day (fills, corporate-action scales or cash credits), when the official
 file is not published or misses a symbol, or when a complete near-close snapshot already exists. Never invents a close."""
 d=date.fromisoformat(str(day));t=e.now();ss=e.session(d)
 if not ss or d.weekday()>=5:return {'status':'not_a_verified_session','day':str(d),'written':False}
 if d>t.date() or (d==t.date() and t.timetz().replace(tzinfo=None)<e.COMPLETED_CUTOFF):return {'status':'session_not_completed','day':str(d),'written':False}
 later=db.query('SELECT COUNT(*) AS n FROM paper_fills WHERE namespace=? AND substr(filled_at,1,10)>?',(e.NS,str(d)))[0]['n']
 scales=db.query('SELECT COUNT(*) AS n FROM paper_state_scales WHERE namespace=? AND ex_date>?',(e.NS,str(d)))[0]['n']
 credits=db.query('SELECT COUNT(*) AS n FROM paper_cash_credits WHERE namespace=? AND ex_date>?',(e.NS,str(d)))[0]['n']
 if later or scales or credits:return {'status':'ledger_changed_after_day_unsupported','day':str(d),'fills_after':later,'scales_after':scales,'credits_after':credits,'written':False}
 existing=db.query('SELECT as_of,report_json FROM paper_eod WHERE namespace=? AND day=?',(e.NS,str(d)))
 if existing and json.loads(existing[0]['report_json']).get('closing_valuation_complete'):return {'status':'complete_snapshot_exists','day':str(d),'written':False}
 symbols=sorted({p['symbol'] for p in db.positions()})
 fetched=(fetch or oc.closes_for)(e,d,symbols)
 if fetched.get('status')!='ok' or not set(symbols)<=set(fetched.get('closes') or {}):return {'status':'official_close_unavailable','day':str(d),'reason':fetched.get('reason'),'missing':fetched.get('missing'),'written':False}
 close=datetime.combine(d,ss[1],tzinfo=e.IST)
 overlay={sym:{'price':float(px),'at':close.isoformat(),'source':fetched['source']} for sym,px in fetched['closes'].items()}
 r=finance(e,db,mark_overlay=overlay)
 r['closing_mark_quality']=_closing_quality(r,close,close,e.CFG['eod_near_close_seconds'],official=True)
 r['official_close']=True;r['official_close_source']=fetched['source'];r['official_close_status']='applied';r['official_close_reason']=fetched.get('reason')
 r['closing_valuation_complete']=not r['missing_marks'] and all(x['near_close'] for x in r['closing_mark_quality'])
 prior=db.query('SELECT day,report_json FROM paper_eod WHERE namespace=? AND day<? ORDER BY day DESC LIMIT 1',(e.NS,str(d)));p=json.loads(prior[0]['report_json']) if prior else None
 expected=str(e.completed_day(datetime.combine(d,walltime(0),tzinfo=e.IST)));before=db.query('SELECT COUNT(*) AS n FROM paper_fills WHERE namespace=? AND substr(filled_at,1,10)<?',(e.NS,str(d)))[0]['n']
 known=bool(prior and prior[0]['day']==expected and p.get('closing_valuation_complete')) or (not prior and not before)
 r['daily_baseline_day']=prior[0]['day'] if prior else None;r['daily_baseline_known']=known;r['daily_gross_paise']=None;r['daily_net_accrued_paise']=None
 if known and r['closing_valuation_complete']:
  for key,out in [('gross_total_paise','daily_gross_paise'),('net_total_accrued_paise','daily_net_accrued_paise')]:
   if r[key] is not None and (p is None or p.get(key) is not None):r[out]=r[key]-(p[key] if p else 0)
 r.update(day=str(d),revaluation=True,mark_basis='official_close_revaluation',revalued_at=t.isoformat(),missing_eod_warning=None,
          revaluation_note='Written after the session from the NSE UDiFF CM bhavcopy closing price for every open symbol; positions and accrued charges are the ledger as of that day (no fills or corporate-action changes since). Not a live end-of-day snapshot.')
 try:r['performance']=performance(e,db,r,str(d))
 except Exception as ex:r['performance']={'status':'unavailable','reason':safe_error(ex)}
 if existing:r['previous_snapshot_as_of']=existing[0]['as_of'];r['previous_snapshot_path']=_preserve_prior(e,d,existing[0])
 db.batch([('INSERT INTO paper_eod(namespace,day,as_of,report_json) VALUES(?,?,?,?) ON CONFLICT(namespace,day) DO UPDATE SET as_of=excluded.as_of,report_json=excluded.report_json',(e.NS,str(d),t.isoformat(),e.js(r)))])
 al.emit(e,db,'eod_revalued',day=str(d),source=fetched['source'],closing_valuation_complete=r['closing_valuation_complete'],daily_net_accrued_paise=r['daily_net_accrued_paise'],net_total_accrued_paise=r.get('net_total_accrued_paise'),replaced_snapshot=bool(existing))
 return {'status':'written','day':str(d),'written':True,'closing_valuation_complete':r['closing_valuation_complete'],'daily_gross_paise':r['daily_gross_paise'],'daily_net_accrued_paise':r['daily_net_accrued_paise'],'gross_total_paise':r.get('gross_total_paise'),'net_total_accrued_paise':r.get('net_total_accrued_paise'),'baseline_day':r['daily_baseline_day'],'baseline_known':known,'replaced_snapshot':bool(existing),'previous_snapshot_path':r.get('previous_snapshot_path')}

def _session_days_before(e,day,count):
 out=[];d=day
 while len(out)<count:
  d-=timedelta(days=1);s=e.session(d)
  if s is None:break
  if s and d.weekday()<5:out.append(d)
 return out
def monitor_coverage(e,db,d):
 """Share of the 09:15-close session covered by monitor checks (each check credits up to worker_coverage_credit_seconds)."""
 ss=e.session(d)
 if not ss:return None
 o=datetime.combine(d,ss[0],tzinfo=e.IST);c=datetime.combine(d,ss[1],tzinfo=e.IST)
 ts=[datetime.fromisoformat(x['at']) for x in db.query("SELECT at FROM paper_runs WHERE namespace=? AND kind='monitor' AND substr(at,1,10)=? ORDER BY at",(e.NS,str(d)))]
 pts=[o]+[x for x in ts if o<=x<=c]+[c];credit=float(e.CFG['worker_coverage_credit_seconds'])
 covered=sum(min((b-a).total_seconds(),credit) for a,b in zip(pts,pts[1:])) if ts else 0
 return {'checks':len(pts)-2,'coverage':round(covered/(c-o).total_seconds(),4)}
OUTAGES_SCHEMA='CREATE TABLE IF NOT EXISTS paper_outages(namespace TEXT NOT NULL,session_day TEXT NOT NULL,kind TEXT NOT NULL,detected_at TEXT NOT NULL,detail_json TEXT NOT NULL,PRIMARY KEY(namespace,session_day,kind))'
def record_outages(e,db,lookback=None):
 """Durable day-missing / outage records for recent sessions: no monitoring, low coverage, missing scan, missing EOD.
 Insert-once per (session, kind); each new row also emits an audit event. Never backfills prices or fills."""
 db.batch([(OUTAGES_SCHEMA,())])
 t=e.now();lookback=lookback or int(e.CFG['outage_lookback_sessions']);days=_session_days_before(e,t.date(),lookback)
 ss=e.session(t.date())
 if ss and t.weekday()<5 and t.timetz().replace(tzinfo=None)>=e.COMPLETED_CUTOFF:days=[t.date()]+days
 # Sessions before this namespace's first scan predate go-live: nothing was expected to run, so they are not outages.
 first=db.query('SELECT MIN(substr(scanned_at,1,10)) AS d FROM paper_scan_batches WHERE namespace=?',(e.NS,))
 golive=first[0]['d'] if first and first[0]['d'] else None
 days=[d for d in days if golive and str(d)>=golive]
 new=[]
 for d in days:
  findings=[];cov=monitor_coverage(e,db,d)
  if cov is not None:
   if cov['checks']==0:findings.append(('no_monitoring',cov))
   elif cov['coverage']<float(e.CFG['outage_min_monitor_coverage']):findings.append(('monitor_coverage_low',cov))
  if not db.query('SELECT 1 FROM paper_scan_batches WHERE namespace=? AND substr(scanned_at,1,10)=? LIMIT 1',(e.NS,str(d))):findings.append(('scan_missing',{}))
  if not db.query('SELECT 1 FROM paper_eod WHERE namespace=? AND day=? LIMIT 1',(e.NS,str(d))):findings.append(('eod_missing',{}))
  for kind,detail in findings:
   n=db.query('SELECT COUNT(*) AS n FROM paper_outages WHERE namespace=? AND session_day=? AND kind=?',(e.NS,str(d),kind))[0]['n']
   if n:continue
   db.batch([('INSERT OR IGNORE INTO paper_outages(namespace,session_day,kind,detected_at,detail_json) VALUES(?,?,?,?,?)',(e.NS,str(d),kind,t.isoformat(),e.js(detail)))])
   al.emit(e,db,'session_outage_recorded',outcome='failed',severity='ERROR',missed_session=str(d),reason=kind,**({'coverage':detail.get('coverage'),'checks':detail.get('checks')} if detail else {}))
   new.append({'session_day':str(d),'kind':kind,**detail})
 return {'status':'ok','checked_sessions':[str(d) for d in days],'new_records':new}

def eod(e,db):
 with al.scope(e,db,'eod'):
  al.emit(e,db,'eod_started')
  r=_eod(e,db)
  al.emit(e,db,'eod_completed',severity='INFO' if r.get('ledger_reconciled',False) else 'WARN',**{**r,'snapshot_written':r.get('snapshot_written',True),'charge_policy_version':r.get('policy_version')})
 try:
  r['audit_export']=al.ensure_daily_export(e,db,str(e.now().date()),force=True)
 except Exception as ex:
  r['audit_export']={'status':'unavailable','reason':al.classified_error(ex)}
 return r

def weekly(e,db):
 end=e.now().date();start=end-timedelta(days=end.weekday());rs=db.query('SELECT day,report_json FROM paper_eod WHERE namespace=? AND day>=? AND day<=? ORDER BY day',(e.NS,str(start),str(end)));values=[json.loads(x['report_json']) for x in rs];expected=[];d=start
 while d<=end:
  s=e.session(d)
  if s is None:return {'status':'calendar_unverified','weekly_net_paise':None}
  if d.weekday()<5 and s and (d<end or e.now().timetz().replace(tzinfo=None)>=e.COMPLETED_CUTOFF):expected.append(str(d))
  d+=timedelta(days=1)
 complete=bool(values) and set(expected)=={x['day'] for x in rs} and all(x.get('daily_net_accrued_paise') is not None for x in values)
 return {'week_start':str(start),'through':rs[-1]['day'] if rs else None,'requested_through':str(end),'unfinalized_today_excluded':str(end) not in expected,'expected_sessions':expected,'snapshot_days':[x['day'] for x in rs],'complete':complete,'weekly_gross_paise':sum(x['daily_gross_paise'] for x in values) if complete else None,'weekly_net_paise':sum(x['daily_net_accrued_paise'] for x in values) if complete else None,'definition':'Sum verified daily CHANGES, never sum cumulative snapshots; incomplete week/session coverage unavailable.'}

def boot_id():
 try:return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
 except OSError:return None
def mono_offset():
 """Wall clock minus CLOCK_MONOTONIC. Constant while the host runs; grows by exactly the frozen time when the VM is paused (monotonic stops, wall time does not)."""
 return time.time()-time.monotonic()
def frozen_since(detail):
 """Seconds this host was frozen since a heartbeat carrying mono_offset/boot_id was written. 0 when unknown or from another boot."""
 detail=detail or {}
 try:off=float(detail.get('mono_offset'))
 except (TypeError,ValueError):return 0.0
 if not detail.get('boot_id') or detail.get('boot_id')!=boot_id():return 0.0
 return max(0.0,mono_offset()-off)
def heartbeat(e,db,status,**detail):
 # Turso-move Task 4: the worker's read count, so /health's refresher can pick it up from detail_json.
 # Both keys are the same number by construction: the counter's epoch is process start. The write
 # itself is a batch, so writing the heartbeat never increments the count. Duck-typed DBs without
 # read_count() (tests) simply get no keys.
 read_count=getattr(db,'read_count',None)
 if callable(read_count):
  try:n=read_count()
  except Exception:n=None
  if isinstance(n,int) and not isinstance(n,bool):detail['db_reads_total']=n;detail['db_reads_since_start']=n
 detail['process_start']=(Path('/proc')/str(os.getpid())/'stat').read_text().split()[21]
 detail['mono_offset']=round(mono_offset(),3);detail['boot_id']=boot_id()
 db.batch([('INSERT INTO paper_monitor_health(namespace,pid,heartbeat_at,status,detail_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,status=excluded.status,detail_json=excluded.detail_json',(e.NS,os.getpid(),e.now().isoformat(),status,e.js(detail)))])
def stale_budget(e,status):
 """Seconds a heartbeat may age before the worker counts as stale."""
 if status=='backoff':return e.CFG['backoff_heartbeat_stale_seconds']
 if status=='polling':return e.CFG['worker_polling_stale_seconds']
 return e.CFG['worker_stale_seconds']
def heartbeat_is_stale(e,r,now=None):
 """True when the stored heartbeat is older than the status budget, or a polling cycle has already run past that budget."""
 if not r:return True
 now=now or e.now()
 try:detail=json.loads(r.get('detail_json') or '{}')
 except (TypeError,ValueError,AttributeError):detail={}
 if not isinstance(detail,dict):detail={}
 # Time the whole host was frozen (VM paused) is not worker silence: the worker could not run either.
 # Only same-boot heartbeats are adjusted, so a worker from before a VM restart still turns stale.
 age=(now-datetime.fromisoformat(r['heartbeat_at'])).total_seconds()-frozen_since(detail)
 if age>stale_budget(e,r['status']):return True
 if r.get('status')=='polling':
  elapsed=detail.get('cycle_elapsed_seconds')
  if elapsed is not None and elapsed>e.CFG['worker_polling_stale_seconds']:return True
 return False
class CycleHeartbeat:
 """Refreshes the 'polling' heartbeat every worker_heartbeat_interval_seconds while one cycle runs.
 Stops refreshing after worker_cycle_max_seconds so a genuinely hung cycle still turns stale.
 A tick is always written to engine.jsonl first so a slow Turso heartbeat cannot stall the log_files verifier."""
 def __init__(self,e,db,polls):
  self.e,self.db,self.polls=e,db,polls;self.stop=threading.Event();self.began=time.monotonic();self.beats=0;self.failures=0;self._writing=False
  self.thread=threading.Thread(target=self.run,name='worker-cycle-heartbeat',daemon=True)
 def run(self):
  interval=self.e.CFG['worker_heartbeat_interval_seconds'];limit=self.e.CFG['worker_cycle_max_seconds']
  while not self.stop.wait(interval):
   elapsed=time.monotonic()-self.began
   if elapsed>limit:return
   try:self.e.log('worker_heartbeat',polls=self.polls,cycle_elapsed_seconds=round(elapsed,1))
   except Exception:pass
   if self._writing:continue
   self._writing=True
   threading.Thread(target=self._write,args=(elapsed,),name='worker-cycle-heartbeat-write',daemon=True).start()
 def _write(self,elapsed):
  began=time.monotonic();interval=self.e.CFG['worker_heartbeat_interval_seconds']
  try:heartbeat(self.e,self.db,'polling',polls=self.polls,cycle_elapsed_seconds=round(elapsed,1));self.beats+=1
  except Exception as ex:self.report('worker_heartbeat_failed','failed','WARN',error_code=al.classified_error(ex),duration_ms=round((time.monotonic()-began)*1000))
  else:
   took=time.monotonic()-began
   if took>interval:self.report('worker_heartbeat_slow','ok','WARN',duration_ms=round(took*1000),poll_seconds=interval)
  finally:self._writing=False
 def report(self,kind,outcome,severity,**data):
  # Never silent: a durable audit event (falls back to the fsynced spool when Turso is down) plus the local engine log.
  self.failures+=kind=='worker_heartbeat_failed'
  try:al.emit(self.e,self.db,kind,outcome=outcome,severity=severity,failures=self.failures,**data)
  except Exception:pass
  try:self.e.log(kind,failures=self.failures,**data)
  except Exception:pass
 def __enter__(self):self.thread.start();return self
 def __exit__(self,*exc):self.stop.set();self.thread.join(self.e.CFG['worker_heartbeat_interval_seconds']);return False
class WakeWatch:
 """Detects a host freeze from inside the worker: CLOCK_MONOTONIC stops while the VM is paused, wall time does not.
 On wake it logs the gap (engine.jsonl first, then a durable audit event), resets the Turso helper so a request
 stranded on a dead connection fails at once instead of after its full timeout, and wakes the worker's sleep so
 the next quote check (which tests every open position against the session open/low) runs immediately."""
 def __init__(self,e,db,on_wake=None,offset=None,interval=None,threshold=None):
  self.e,self.db,self.on_wake=e,db,on_wake;self.offset=offset or mono_offset()
  self.interval=float(interval if interval is not None else e.CFG['worker_wake_check_seconds'])
  self.threshold=float(threshold if threshold is not None else e.CFG['worker_wake_threshold_seconds'])
  self.event=threading.Event();self.stop=threading.Event();self.wakes=0;self.last_gap=None;self.frozen_total=0.0
  self.thread=threading.Thread(target=self.run,name='worker-wake-watch',daemon=True)
 def run(self):
  off=self.offset;wall=self.e.now()
  while not self.stop.wait(self.interval):
   cur=mono_offset();now=self.e.now()
   if cur-off>self.threshold:self.woke(wall,now,cur-off)
   off,wall=cur,now
 def woke(self,began,ended,frozen):
  self.wakes+=1;self.frozen_total+=frozen
  self.last_gap={'started':began.isoformat(),'stopped':ended.isoformat(),'frozen_seconds':round(frozen,1),'wakes':self.wakes}
  try:self.e.log('host_freeze_detected',reason='host_vm_paused',**self.last_gap)
  except Exception:pass
  if self.on_wake:
   try:self.on_wake()
   except Exception:pass
  self.event.set()
  gap=dict(self.last_gap);poll=max(1,int(self.e.CFG.get('poll_seconds',30)))
  def emit():
   try:al.emit(self.e,self.db,'host_freeze_detected',outcome='failed',severity='WARN',reason='host_vm_paused',started=gap['started'],stopped=gap['stopped'],age_seconds=gap['frozen_seconds'],missed_checks=int(gap['frozen_seconds']//poll),count=gap['wakes'])
   except Exception:pass
  threading.Thread(target=emit,name='worker-wake-audit',daemon=True).start()
 def nap(self,seconds):
  """Sleep up to seconds; returns early when the host wakes from a freeze."""
  if seconds>0:self.event.wait(seconds)
  self.event.clear()
 def __enter__(self):self.thread.start();return self
 def __exit__(self,*exc):self.stop.set();self.thread.join(max(1.0,self.interval*2));return False
_children=[]
def reap_children():
 """Poll workers this process launched so exited ones never linger as zombies."""
 done=[p for p in _children if p.poll() is not None]
 for p in done:_children.remove(p)
 return len(done)
def _wait_for_lease(lock,seconds,interval=0.5):
 end=time.monotonic()+seconds
 while True:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);return True
  except BlockingIOError:
   if time.monotonic()>=end:return False
   time.sleep(interval)
def _install_report(e):
 try:
  import install_check as ic
  return ic.check(e.ROOT,smoke_imports=False)
 except Exception as ex:
  return {'ok':False,'missing':[],'lock_errors':[safe_error(ex)],'locks':{'ok':False},'error':safe_error(ex)}
def health(e,db):
 rows=db.query('SELECT * FROM paper_monitor_health WHERE namespace=?',(e.NS,));r=rows[0] if rows else None
 deps=rd.check();info=rd.report_for_audit({**deps,'healed':False,'restored':[]})
 if not deps.get('ok'):
  try:al.emit(e,db,info['event'],python_ok=deps.get('python_ok'),node_ok=deps.get('node_ok'),restored=[])
  except Exception:pass
 inst=_install_report(e)
 return {'paper_only':True,'automation_session_open':active(e,e.now()),'calendar_valid_through':e.CFG['calendar_valid_through'],'calendar_verified':e.session(e.now().date()) is not None,'heartbeat':r,'heartbeat_stale':bool(active(e,e.now()) and (not r or heartbeat_is_stale(e,r))),'scheduler_required':True,'poll_seconds':e.CFG['poll_seconds'],'calendar_review_warning':(date.fromisoformat(e.CFG['calendar_valid_through'])-e.now().date()).days<=14,'policy_review_warning':(date.fromisoformat(json.loads((e.ROOT/'charge_policy.json').read_text())['valid_through'])-e.now().date()).days<=14,'runtime_deps':{'ok':deps.get('ok'),'python_ok':deps.get('python_ok'),'node_ok':deps.get('node_ok'),'python_reason':deps.get('python_reason'),'node_reason':deps.get('node_reason'),'extra_ok':deps.get('extra_ok'),'extra_projects':deps.get('extra_projects')},'install':inst,'install_ok':bool(inst.get('ok'))}
def preflight(e,db):
 r=rd.ensure();info=rd.report_for_audit(r)
 al.emit(e,db,info['event'],python_ok=r.get('python_ok'),node_ok=r.get('node_ok'),restored=r.get('restored') or [],outcome='ok' if r.get('ok') else 'failed',severity='INFO' if r.get('ok') else 'ERROR')
 try:audit=al.ensure_daily_export(e,db,str(e.now().date()),force=False)
 except Exception as ex:audit={'status':'unavailable','reason':al.classified_error(ex)}
 try:
  day=e.reference_day(e.now());ready=e.surveillance_ready(db,str(day))
  reference=e.refresh_reference(db,day) if not ready.get('ready') else {'status':'already_loaded','surveillance_status':ready}
  reference={'trade_day':str(day),'surveillance_status':reference.get('surveillance_status'),'sources':{k:(v or {}).get('status') for k,v in (reference.get('sources') or {}).items()}}
 except Exception as ex:reference={'status':'unavailable','reason':safe_error(ex)}
 try:import sessiond as sd;daemon=sd.ensure(e)
 except Exception as ex:daemon={'status':'unavailable','reason':safe_error(ex)}
 inst=_install_report(e)
 status='ok' if r.get('ok') and inst.get('ok') else ('runtime_deps_restore_incomplete' if not r.get('ok') else 'install_incomplete')
 return {'paper_only':True,'status':status,'runtime_deps':r,'audit_export':audit,'reference_feeds':reference,'sessiond':daemon,'calendar_verified':e.session(e.now().date()) is not None,'at_IST':e.now().isoformat(),'install':inst,'install_ok':bool(inst.get('ok'))}

def ensure_monitor(e,db,defer_stale_signal=False):
 if not active(e,e.now()):return {'status':'market_closed_or_calendar_unverified','started':False}
 reap_children()
 with (e.ROOT/'monitor.lock').open('a+') as lock:
  replaced=None
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   h=health(e,db);r=h.get('heartbeat');detail=json.loads(r['detail_json']) if r else {};pid=r['pid'] if r else 0
   if h['heartbeat_stale'] and r and detail.get('process_start'):
    try:
     proc=Path('/proc')/str(pid);cmd=(proc/'cmdline').read_bytes().split(b'\0');start=(proc/'stat').read_text().split()[21]
     owned=start==detail['process_start'] and str(e.ROOT/'engine.py').encode() in cmd and b'worker' in cmd and (proc/'cwd').resolve()==e.ROOT
     if owned and defer_stale_signal:return {'status':'stale_signal_deferred_after_supervisor_gap','started':False,'pid':pid}
     if owned:
      os.kill(pid,signal.SIGTERM)
      if not _wait_for_lease(lock,e.CFG['worker_stop_wait_seconds']):return {'status':'stale_owned_worker_signalled','started':False,'restart':'next supervisor invocation','pid':pid}
      replaced=pid
    except (OSError,ValueError,IndexError):pass
   if replaced is None:return {'status':'unhealthy_worker_needs_attention' if h['heartbeat_stale'] else 'already_running','started':False,**h}
  # Lock is an OS lease; a stale PID alone never causes process termination or prevents restart.
  with (e.ROOT/'logs/monitor-worker.log').open('a') as out:
   p=subprocess.Popen([sys.executable,str(e.ROOT/'engine.py'),'worker'],stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True,cwd=e.ROOT,pass_fds=(lock.fileno(),),env={**os.environ,'PAPER_MONITOR_LOCK_FD':str(lock.fileno())})
  if callable(getattr(p,'poll',None)):_children.append(p)
  if replaced is not None:return {'status':'stale_owned_worker_replaced','started':True,'pid':p.pid,'replaced_pid':replaced,'singleton_enforced_by_child':True}
  return {'status':'starting','started':True,'pid':p.pid,'singleton_enforced_by_child':True}

def worker(e,db,sleep=time.sleep,max_polls=None,api_factory=None):
 if not active(e,e.now()):return {'status':'closed','polls':0}
 fd=os.environ.get('PAPER_MONITOR_LOCK_FD')
 if fd and os.fstat(int(fd)).st_ino!=(e.ROOT/'monitor.lock').stat().st_ino:raise RuntimeError('invalid_inherited_monitor_lease')
 with (os.fdopen(int(fd),'a+') if fd else (e.ROOT/'monitor.lock').open('a+')) as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'status':'already_running','polls':0}
  failures=0;polls=0;last_pnl=-1000;api=None;paused=False;factory=api_factory or e.MarketData
  state={'in_cycle':False,'stop':False};db.enable_helper() if hasattr(db,'enable_helper') else None
  def on_term(signum,frame):
   # Between cycles exit at once; mid-cycle finish the cycle (fills stay atomic) and exit after it.
   # A second SIGTERM falls back to the default action, so a hung cycle can still be terminated.
   state['stop']=True;signal.signal(signal.SIGTERM,signal.SIG_DFL)
   if not state['in_cycle']:raise SystemExit(0)
  previous=signal.signal(signal.SIGTERM,on_term) if threading.current_thread() is threading.main_thread() else None
  watch=WakeWatch(e,db,on_wake=(lambda:db.reset_helper('host_resume')) if hasattr(db,'reset_helper') else None) if e.CFG.get('worker_wake_watch') else None
  nap=(watch.nap if watch is not None and sleep is time.sleep else sleep);reported=0
  if watch is not None:watch.__enter__()
  try:
   while active(e,e.now()) and (max_polls is None or polls<max_polls) and not state['stop']:
    start=time.monotonic();delay=e.CFG['poll_seconds'];full_cooloff=False;gen=watch.wakes if watch is not None else 0
    with (e.ROOT/'engine.lock').open('a+') as lock:
     try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:
      heartbeat(e,db,'waiting_for_engine_lock');al.emit(e,db,'worker_waiting',reason='singleton_busy',poll_seconds=delay);nap(delay);continue
     try:
      state['in_cycle']=True;heartbeat(e,db,'polling',polls=polls)
      with CycleHeartbeat(e,db,polls):
       api=api or factory();r=e.monitor(db,api);polls+=1;failures=0
       if time.monotonic()-last_pnl>=e.CFG['pnl_snapshot_seconds']:e.pnl(db);last_pnl=time.monotonic()
      heartbeat(e,db,'healthy',polls=polls,last_result=r,db_helper=db.helper_stats() if hasattr(db,'helper_stats') else None)
      if watch is not None and watch.wakes>reported:
       # First completed check after a host freeze: every open position was tested against this cycle's quote.
       reported=watch.wakes;gap=watch.last_gap or {}
       try:e.log('post_wake_check',status=r.get('status'),fills=r.get('fills',0),fresh_quotes=r.get('fresh_quotes'),quotes=r.get('quotes'),frozen_seconds=gap.get('frozen_seconds'),gap_started=gap.get('started'),gap_stopped=gap.get('stopped'),seconds_after_wake=round((e.now()-datetime.fromisoformat(gap['stopped'])).total_seconds(),1) if gap.get('stopped') else None)
       except Exception:pass
      al.emit(e,db,'worker_check',duration_ms=round((time.monotonic()-start)*1000),poll_seconds=delay,missed_checks=max(0,int((time.monotonic()-start)//delay)-1),status=r.get('status'))
     except Exception as ex:
      reason=al.classified_error(ex);failures+=1;polls+=1
      try:e.log('monitor_check',status='failed',error_code=reason,failures=failures)
      except Exception:pass
      al.emit(e,db,'worker_failure',outcome='failed',severity='ERROR',error_code=reason,failures=failures)
      try:al.emit(e,db,'monitor_check',outcome='failed',severity='WARN',error_code=reason,failures=failures,poll_seconds=delay)
      except Exception:pass
      heartbeat(e,db,'backoff',error=reason,failures=failures)
      if 'auth' in reason.lower() or 'token_refresh' in reason.lower():paused=True;heartbeat(e,db,'paused_auth',error=reason);return {'status':'paused_auth','polls':polls}
      backoff_max=int(e.CFG.get('worker_backoff_max_seconds',30))
      if 'rate_limit' in reason:
       delay=min(backoff_max,max(int(e.CFG['market_data'].get('rate_limit_cooloff_seconds',180)),min(300,30*2**min(failures,4))));full_cooloff=True
      else:
       delay=min(backoff_max,min(300,30*2**min(failures,4)))
      if watch is not None and watch.wakes!=gen and 'rate_limit' not in reason:
       # The failure was a request stranded by a host freeze (dead connection), not a Turso/Fyers fault: retry at once.
       delay=0;full_cooloff=False
       try:e.log('post_wake_retry',error_code=reason,failures=failures,frozen_seconds=(watch.last_gap or {}).get('frozen_seconds'))
       except Exception:pass
     finally:state['in_cycle']=False
    if state['stop']:break
    nap(delay if full_cooloff else max(0,delay-(time.monotonic()-start)))
  finally:
   if watch is not None:watch.__exit__(None,None,None)
   if previous is not None and not state['stop']:signal.signal(signal.SIGTERM,previous)
   if not paused:heartbeat(e,db,'stopped',polls=polls,terminated=state['stop'],db_helper=db.helper_stats() if hasattr(db,'helper_stats') else None)
   if hasattr(db,'close_helper'):db.close_helper()
  return {'status':'terminated' if state['stop'] else 'session_finished','polls':polls}
