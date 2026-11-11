"""Versioned PRE-entry screen; seven strategy indicators remain untouched."""
import json,math
from pathlib import Path
import numpy as np
import pandas as pd
POLICY=json.loads((Path(__file__).parent/'prefilter_policy.json').read_text())
# Explicit calendar exclusions (freeze plan A9) are config, shared with engine.py and db.mjs.
MUHURAT_INDICATOR_EXCLUDED=frozenset(json.loads((Path(__file__).parent/'config.json').read_text())['indicator_excluded_sessions'])
SCHEMA=["CREATE TABLE IF NOT EXISTS paper_prefilter_policies(version TEXT PRIMARY KEY,policy_json TEXT NOT NULL)","CREATE TABLE IF NOT EXISTS paper_prefilter_states(namespace TEXT NOT NULL,bar_date TEXT NOT NULL,symbol TEXT NOT NULL,policy_version TEXT NOT NULL,status TEXT NOT NULL,metrics_json TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(namespace,bar_date,symbol,policy_version))","CREATE TABLE IF NOT EXISTS paper_entry_gate_proofs(namespace TEXT NOT NULL,signal_id TEXT NOT NULL,bar_date TEXT NOT NULL,symbol TEXT NOT NULL,policy_version TEXT NOT NULL,PRIMARY KEY(namespace,signal_id))"]
def init(e,db):
 db.batch([(s,()) for s in SCHEMA]+[('INSERT OR IGNORE INTO paper_prefilter_policies VALUES(?,?)',(POLICY['version'],e.js(POLICY)))])
 if db.query('SELECT policy_json FROM paper_prefilter_policies WHERE version=?',(POLICY['version'],))[0]['policy_json']!=e.js(POLICY):raise RuntimeError('immutable_prefilter_policy_changed')
def evaluate(stock,index,target):
 r={'policy_version':POLICY['version'],'benchmark':POLICY['benchmark'],'bar_date':str(target),'status':'UNAVAILABLE','reasons':[],'checks':{},'volume_expansion':None,'volume_ratio':None,'beta':None,'beta_pairs':0,'raw_prices_unadjusted':True}
 def unavailable(reason):r['reasons'].append(reason);return r
 if stock.empty or index.empty:return unavailable('missing_stock_or_benchmark')
 if str(target) in MUHURAT_INDICATOR_EXCLUDED:return unavailable('indicator_excluded_session')
 if 'trade_date' in stock.columns:stock=stock[~stock.trade_date.astype(str).isin(MUHURAT_INDICATOR_EXCLUDED)]
 if 'trade_date' in index.columns:index=index[~index.trade_date.astype(str).isin(MUHURAT_INDICATOR_EXCLUDED)]
 if stock.empty or index.empty:return unavailable('missing_stock_or_benchmark')
 # Benchmark sessions define every return interval. Never forward-fill or pair unlike intervals.
 b=index[index.trade_date<=str(target)].sort_values('trade_date').drop_duplicates('trade_date',keep=False).tail(POLICY['beta_intervals']+1).copy()
 s=stock[stock.trade_date<=str(target)].sort_values('trade_date').drop_duplicates('trade_date',keep=False)
 if b.empty or s.empty or str(b.iloc[-1].trade_date)!=str(target) or str(s.iloc[-1].trade_date)!=str(target):return unavailable('stale_stock_or_benchmark')
 for frame in [b,s.tail(POLICY['beta_intervals']+1)]:
  if not np.isfinite(frame[['open','high','low','close']].to_numpy(dtype=float)).all() or (frame[['open','high','low','close']]<=0).any().any() or (frame.high<frame[['open','close','low']].max(axis=1)).any() or (frame.low>frame[['open','close','high']].min(axis=1)).any():return unavailable('invalid_raw_OHLC')
 aligned=s.set_index('trade_date').reindex(b.trade_date);sc=aligned.close.astype(float);bc=b.set_index('trade_date').close.astype(float)
 r['missing_recent_sessions']=[str(day) for day,value in sc.tail(200).items() if pd.isna(value)]
 sr=sc.pct_change(fill_method=None);br=bc.pct_change(fill_method=None)
 if (sr.abs()>POLICY['extreme_return_limit']).any() or (br.abs()>POLICY['extreme_return_limit']).any():return unavailable('raw_return_above_50pct_requires_corporate_action_review')
 pairs=pd.concat([sr.rename('stock'),br.rename('benchmark')],axis=1).replace([np.inf,-np.inf],np.nan).dropna().tail(POLICY['beta_intervals']);r['beta_pairs']=len(pairs)
 if len(pairs)<POLICY['beta_min_pairs']:return unavailable('fewer_than_200_aligned_returns')
 variance=float(pairs.benchmark.var(ddof=1))
 if not math.isfinite(variance) or variance<=0:return unavailable('nonpositive_benchmark_variance')
 beta=float(pairs.stock.cov(pairs.benchmark)/variance);r['beta']=beta
 if not math.isfinite(beta):return unavailable('nonfinite_beta')
 # SMA200 requires all latest 200 benchmark sessions, not 200 irregular stock observations.
 if len(sc)<200 or sc.tail(200).isna().any():return unavailable('incomplete_latest_200_session_stock_history')
 close=float(sc.iloc[-1]);ma50=float(sc.tail(50).mean());ma200=float(sc.tail(200).mean());m63=float(sc.iloc[-1]/sc.iloc[-64]-1);m20=float(sc.iloc[-1]/sc.iloc[-21]-1);rel=m63-float(bc.iloc[-1]/bc.iloc[-64]-1)
 r.update(close=close,sma50=ma50,sma200=ma200,return63=m63,return20=m20,relative63=rel)
 r['checks']={'beta_at_least_1_2':beta>=POLICY['beta_min']-1e-12,'close_above_sma50':close>ma50,'sma50_above_sma200':ma50>ma200,'return63_at_least_10pct':m63>=POLICY['momentum_min']-1e-12,'relative63_at_least_5pp':rel>=POLICY['relative_momentum_min']-1e-12,'return20_positive':m20>0}
 volumes=pd.to_numeric(aligned.volume.tail(40),errors='coerce') if 'volume' in aligned else pd.Series(dtype=float)
 if len(volumes)==40 and np.isfinite(volumes).all() and (volumes>0).all():r['volume_ratio']=float(volumes.tail(20).mean()/volumes.head(20).mean());r['volume_expansion']=r['volume_ratio']>=POLICY['volume_expansion_min']
 r['status']='PASS' if all(r['checks'].values()) else 'FAIL';r['reasons']=[k for k,v in r['checks'].items() if not v];return r
def benchmark(e,db,target,api=None,sync=False):
 symbol=POLICY['benchmark_db_symbol'];rows=db.query('SELECT trade_date,open,high,low,close,volume FROM index_ohlcv WHERE symbol=? AND trade_date<=? ORDER BY trade_date',(symbol,str(target)))
 if sync and (not rows or rows[-1]['trade_date']<str(target)):
  start=e.date.fromisoformat(rows[-1]['trade_date'])+e.timedelta(days=1) if rows else target-e.timedelta(days=465)
  statements=[]
  while start<=target:
   end=min(target,start+e.timedelta(days=364))
   for c in api.history(symbol,start,end):
    stamp,o,h,l,cl,v=c[:6];day=e.datetime.fromtimestamp(stamp,e.IST).date()
    if day>end or day<start:continue
    if all(math.isfinite(float(x)) and float(x)>0 for x in [o,h,l,cl]) and h>=max(o,l,cl) and l<=min(o,h,cl):statements.append(('INSERT OR IGNORE INTO index_ohlcv(trade_date,symbol,open,high,low,close,volume,source,loaded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(day),symbol,o,h,l,cl,v,'FYERS_PREFILTER_SYNC',e.now().isoformat())))
   start=end+e.timedelta(days=1)
  db.batch(statements);rows=db.query('SELECT trade_date,open,high,low,close,volume FROM index_ohlcv WHERE symbol=? AND trade_date<=? ORDER BY trade_date',(symbol,str(target)))
 if rows:
  first=rows[-253:][0]['trade_date'];expected={r['trade_date'] for r in db.query('SELECT trade_date FROM index_ohlcv WHERE symbol=? AND trade_date>=? AND trade_date<=?',('NIFTY500',first,str(target)))}
  day=e.date.fromisoformat(first)
  while day<=target:
   if e.session(day) and str(day) not in MUHURAT_INDICATOR_EXCLUDED:expected.add(str(day))
   day+=e.timedelta(days=1)
  if expected-{r['trade_date'] for r in rows}:raise RuntimeError('benchmark_missing_verified_exchange_session')
 return pd.DataFrame(rows)
