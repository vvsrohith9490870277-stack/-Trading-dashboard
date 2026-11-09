"""Synthetic, isolated fixtures for adaptive simulator tests (in-memory SQLite only; no Turso, no broker)."""
import sqlite3, numpy as np, pandas as pd
from datetime import date, timedelta
from pathlib import Path
import adaptive_logging as alog, pit_archive as pit, prefilter as pf
class MemDB:
    def __init__(self):
        self.c=sqlite3.connect(':memory:'); self.c.row_factory=sqlite3.Row
    def batch(self,stm):
        with self.c:
            for sql,args in stm: self.c.execute(sql,args)
    def query(self,sql,args=()): return [dict(r) for r in self.c.execute(sql,args).fetchall()]
def fresh_db(root):
    db=MemDB()
    for sql in (Path(root)/'schema.sql').read_text().split(';'):
        if sql.strip(): db.batch([(sql.strip(),())])
    pit.init(db); alog.init(db); return db
def trading_days(n, start=date(2024,1,1)):
    out=[];cur=start
    while len(out)<n:
        if cur.weekday()<5: out.append(cur.isoformat())
        cur+=timedelta(days=1)
    return out
def make_market(n_days=330, symbols=('AAA-EQ','BBB-EQ','CCC-EQ','DDD-EQ'), seed=7, volume=300_000):
    rng=np.random.default_rng(seed); days=trading_days(n_days); rows=[]
    for k,sym in enumerate(symbols):
        px=1000.0+50*k
        for i,d in enumerate(days):
            r=0.0015+0.018*np.sin(i/3.0+k)+rng.normal(0,0.004)
            o=round(px,2); px=max(50.0,px*(1+r)); c=round(px,2)
            rows.append({'symbol':sym,'trade_date':d,'open':o,'high':round(max(o,c)*1.005,2),'low':round(min(o,c)*0.995,2),'close':c,'volume':volume})
    idx=pd.DataFrame([{'symbol':'NIFTY500','trade_date':d,'open':1000+i,'high':1001+i,'low':999+i,'close':1000+i,'volume':1} for i,d in enumerate(days)])
    return days, pd.DataFrame(rows), idx
def seed_pit(db, ns, days, symbols, series='EQ', eligible=True):
    ver=pf.POLICY['version']
    for d in days:
        pit.ingest_bhavcopy_series(db,[{'symbol':s,'series':series,'close':1000.0} for s in symbols],trade_date=d) if series else None
        db.batch([('INSERT INTO daily_prefilter_universe(namespace,trade_date,symbol,policy_version,status,eligible,pit_series,metrics_json,data_hash,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (ns,d,s,ver,'PASS' if eligible else 'FAIL',1 if eligible else 0,series,'{}','h','t')) for s in symbols])
def calibration_partitions(n_oldest, n_mid=20, n_holdout=45, start=date(2023,1,2)):
    """Explicit dated partitions (oldest -> newest) and the trading dates in each, for date-enforced calibration."""
    days=trading_days(n_oldest+n_mid+n_holdout, start=start)
    old,mid,hold=days[:n_oldest],days[n_oldest:n_oldest+n_mid],days[n_oldest+n_mid:]
    parts=[{'name':'OLDEST_CALIBRATION_PARTITION','start':old[0],'end':old[-1]},
           {'name':'VALIDATION_PARTITION','start':mid[0],'end':mid[-1]},
           {'name':'HOLDOUT_VAULT','start':hold[0],'end':hold[-1]}]
    return parts, old, mid, hold
def seed_observation_evidence(db, ns, strategy, dates, bhavcopy=True, reconciled=True, parity=True, log=True):
    """Evidence the observation counter derives sessions from: shadow log, official Bhavcopy, reconciled EOD, parity."""
    import adaptive_observation as obs
    for d in dates:
        if bhavcopy:
            pit.ingest_bhavcopy_series(db,[{'symbol':'AAA-EQ','series':'EQ','close':100.0}],trade_date=d)
        obs.record_eod_marker(db,ns,d,reconciled,'out/eod/'+d+'.json','hash_'+d)
        if log:
            obs.log_shadow_session_completed(db,ns,strategy,d)
        obs.record_parity(db,ns,strategy,d,parity,True,True,0)
def ci_success_payload(sha):
    import adaptive_policy as apol
    return {'check_runs':[{'name':n,'head_sha':sha,'status':'completed','conclusion':'success','completed_at':'2026-11-10T00:00:00Z'} for n in apol.POLICY['ci_required_check_names']]}
def gate8_cache_from_series(r_var, r_base, weight=0.1, extra_members=2, seed=99):
    """Trade-level Gate 8 cache whose candidate reproduces d_t = r_var - r_base exactly: one 1-session trade on a
    synthetic EDGE symbol per discordant day. Extra pool members are weaker random grid members (spawn-time pool)."""
    import adaptive_placebo as aplc
    d=np.asarray(r_var,dtype=float)-np.asarray(r_base,dtype=float); n=len(d)
    rets={'EDGE':d/weight}
    days=[i for i in range(n) if abs(d[i])>1e-15]
    cand=aplc.build_member_cache([{'symbol':'EDGE','entry_idx':i,'hold':1,'weight':weight} for i in days],[],rets,n)
    rng=np.random.default_rng(seed); pool=[cand]
    for g in range(extra_members):
        sym=f'NOISE{g}'; rets[sym]=rng.normal(0,0.01,n)
        tr=[{'symbol':sym,'entry_idx':int(rng.integers(0,n-3)),'hold':int(rng.integers(1,6)),'weight':weight} for _ in range(12)]
        pool.append(aplc.build_member_cache(tr,[],rets,n))
    return {'candidate_index':0,'pool':pool,'sym_returns':rets}
