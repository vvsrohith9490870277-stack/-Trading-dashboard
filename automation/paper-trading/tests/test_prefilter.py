import unittest,sys,uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import prefilter as p;import engine as e
import test_engine as fixtures

def frames(n=253,beta=1.5):
 dates=pd.bdate_range(end='2026-11-06',periods=n).strftime('%Y-%m-%d');r=.001+.003*np.sin(np.arange(n-1));b=100*np.r_[1,np.cumprod(1+r)];s=100*np.r_[1,np.cumprod(1+beta*r+.002)]
 def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
 return f(s),f(b)
class CalculationTests(unittest.TestCase):
 def test_analytic_beta_and_all_momentum_pass(self):
  s,b=frames();r=p.evaluate(s,b,'2026-11-06');self.assertEqual(r['status'],'PASS');self.assertAlmostEqual(r['beta'],1.5,10);self.assertEqual(r['beta_pairs'],252)
 def test_beta_threshold_inclusive(self):
  for beta,expected in [(1.2,True),(1.199,False),(-1,False)]:
   s,b=frames(beta=beta);r=p.evaluate(s,b,'2026-11-06');self.assertEqual(r['checks']['beta_at_least_1_2'],expected)
 def test_minimum_199_vs_200(self):
  for n,ok in [(200,False),(201,True)]:
   s,b=frames(n);r=p.evaluate(s,b,'2026-11-06');self.assertEqual(r['status']!='UNAVAILABLE',ok)
 def test_no_pairing_different_intervals(self):
  s,b=frames();s=s.drop(index=10);r=p.evaluate(s,b,'2026-11-06');self.assertEqual(r['beta_pairs'],250);self.assertAlmostEqual(r['beta'],1.5,10)
 def test_latest_200_sessions_must_be_complete(self):
  s,b=frames();self.assertIn('incomplete_latest_200_session_stock_history',p.evaluate(s.drop(index=200),b,'2026-11-06')['reasons'])
 def test_zero_benchmark_variance(self):
  s,b=frames();b[['open','close']]=100;b.high=101;b.low=99;self.assertEqual(p.evaluate(s,b,'2026-11-06')['status'],'UNAVAILABLE')
 def test_benchmark_missing_or_stale(self):
  s,b=frames()
  for bad in [pd.DataFrame(),b.iloc[:-1]]:self.assertEqual(p.evaluate(s,bad,'2026-11-06')['status'],'UNAVAILABLE')
 def test_future_data_never_used(self):
  s,b=frames();r=p.evaluate(s,b,'2026-11-06');future=s.iloc[-1].copy();future.trade_date='2026-11-09';future.close=100000;self.assertEqual(r,p.evaluate(pd.concat([s,pd.DataFrame([future])]),b,'2026-11-06'))
 def test_invalid_ohlc_and_corporate_action_not_clipped(self):
  s,b=frames();s.loc[230,'close']=0;self.assertEqual(p.evaluate(s,b,'2026-11-06')['status'],'UNAVAILABLE');s,b=frames();s.loc[230,['open','high','low','close']]=s.loc[230,['open','high','low','close']]*2;self.assertIn('raw_return_above_50pct_requires_corporate_action_review',p.evaluate(s,b,'2026-11-06')['reasons'])
 def test_volume_optional_not_gate(self):
  s,b=frames();s.volume=0;r=p.evaluate(s,b,'2026-11-06');self.assertEqual(r['status'],'PASS');self.assertIsNone(r['volume_ratio']);s.volume=100;s.loc[s.index[-20:],'volume']=120;r=p.evaluate(s,b,'2026-11-06');self.assertAlmostEqual(r['volume_ratio'],1.2);self.assertTrue(r['volume_expansion'])
 def test_exact_63_session_10pct_and_relative_5pp_boundaries(self):
  s,b=frames();i=np.arange(253);stock=100*1.1**(i/63);index=100*1.05**(i/63)*(1+.005*np.sin(2*np.pi*i/7))
  for frame,c in [(s,stock),(b,index)]:frame['open']=c;frame['close']=c;frame['high']=c+1;frame['low']=c-1
  r=p.evaluate(s,b,'2026-11-06');self.assertTrue(r['checks']['return63_at_least_10pct']);self.assertTrue(r['checks']['relative63_at_least_5pp']);self.assertAlmostEqual(r['return63'],.10,10);self.assertAlmostEqual(r['relative63'],.05,10)
 def test_verified_benchmark_session_gap_fails_closed(self):
  class DB:
   def query(self,sql,args):
    if 'open,high' in sql:return [{'trade_date':'2026-11-05','open':100,'high':101,'low':99,'close':100,'volume':0},{'trade_date':'2026-11-06','open':101,'high':102,'low':100,'close':101,'volume':0}]
    return [{'trade_date':'2026-11-04'},{'trade_date':'2026-11-05'},{'trade_date':'2026-11-06'}]
  from datetime import date
  # Missing the explicitly observed earlier date is outside the returned range, not a gap.
  class GapDB(DB):
   def query(self,sql,args):
    rows=super().query(sql,args)
    if 'open,high' in sql:rows[0]['trade_date']='2026-11-04'
    return rows
  with self.assertRaisesRegex(RuntimeError,'missing_verified_exchange_session'):p.benchmark(e,GapDB(),date(2026,11,6))
 def test_benchmark_session_check_uses_policy_symbol(self):
  # The expected-session query must read the policy benchmark symbol
  # (NIFTY50-INDEX), not a hardcoded 'NIFTY500'. A row stored under the
  # wrong symbol must not fail the check: with the hardcoded value the
  # stray Saturday row below raises spuriously; with the policy value the
  # same check passes.
  class SymbolDB:
   def query(self,sql,args):
    if 'open,high' in sql:return [{'trade_date':'2026-11-05','open':100,'high':101,'low':99,'close':100,'volume':0},{'trade_date':'2026-11-06','open':101,'high':102,'low':100,'close':101,'volume':0}]
    if args[0]=='NIFTY500':return [{'trade_date':'2026-11-07'}]
    return [{'trade_date':'2026-11-05'},{'trade_date':'2026-11-06'}]
  from datetime import date
  out=p.benchmark(e,SymbolDB(),date(2026,11,6));self.assertEqual(len(out),2)
 def test_strict_trend_not_equal(self):
  s,b=frames();s[['open','close']]=100;s.high=101;s.low=99;r=p.evaluate(s,b,'2026-11-06');self.assertFalse(r['checks']['close_above_sma50']);self.assertFalse(r['checks']['sma50_above_sma200']);self.assertFalse(r['checks']['return20_positive'])

 def test_stage1_skips_indicator_excluded_target_bar(self):
  excluded=sorted(p.MUHURAT_INDICATOR_EXCLUDED)
  self.assertTrue(excluded)
  target=excluded[0]
  s,b=frames()
  # A later regular session still evaluates after the excluded bar is present in the frame.
  later='2026-11-09'
  last=s.iloc[-1].copy(); last.trade_date=later; last.close=float(last.close)*1.01; last.open=last.close; last.high=last.close+1; last.low=last.close-1
  excluded_row=s.iloc[-1].copy(); excluded_row.trade_date=target; excluded_row.close=float(excluded_row.close)*2; excluded_row.open=excluded_row.close; excluded_row.high=excluded_row.close+1; excluded_row.low=excluded_row.close-1
  # Target on the excluded session is skipped even if that row would otherwise be usable.
  with_bar=pd.concat([s, pd.DataFrame([excluded_row])], ignore_index=True)
  skipped=p.evaluate(with_bar, pd.concat([b, pd.DataFrame([excluded_row])], ignore_index=True), target)
  self.assertEqual(skipped['status'],'UNAVAILABLE')
  self.assertIn('indicator_excluded_session', skipped['reasons'])
  # Same frames, later regular target: lookback drops the excluded bar, so a doubled Muhurat close cannot leak into returns.
  s2=pd.concat([s, pd.DataFrame([excluded_row, last])], ignore_index=True)
  b2=pd.concat([b, pd.DataFrame([excluded_row, last])], ignore_index=True)
  s3=pd.concat([s, pd.DataFrame([last])], ignore_index=True)
  b3=pd.concat([b, pd.DataFrame([last])], ignore_index=True)
  self.assertEqual(p.evaluate(s2,b2,later)['status'], p.evaluate(s3,b3,later)['status'])
  self.assertEqual(p.evaluate(s2,b2,later).get('close'), p.evaluate(s3,b3,later).get('close'))

class EntryGuardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 @classmethod
 def tearDownClass(cls):fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp;seed_adv=fixtures.PaperTests.seed_adv;signal=fixtures.PaperTests.signal;quote=fixtures.PaperTests.quote
 def test_all_seven_entries_fail_closed_without_proofs(self):
  for strategy in range(1,8):
   signal=self.signal(strategy=strategy);self.db.batch([('DELETE FROM paper_entry_gate_proofs WHERE namespace=?',(e.NS,))]);r=e.fill(self.db,signal,self.quote(),fixtures.T);self.assertFalse(r['filled']);self.assertEqual(r['reason'],'missing_or_failed_current_prefilter_proof')
  self.assertEqual(len(self.db.positions()),0)
 def test_fail_unavailable_and_old_policy_cannot_fill(self):
  for status in ['FAIL','UNAVAILABLE']:
   signal=self.signal();self.db.batch([('UPDATE paper_prefilter_states SET status=? WHERE namespace=?',(status,e.NS))]);self.assertFalse(e.fill(self.db,signal,self.quote(),fixtures.T)['filled'])
  signal=self.signal();self.db.batch([('UPDATE paper_entry_gate_proofs SET policy_version=? WHERE namespace=?',('obsolete',e.NS))]);self.assertFalse(e.fill(self.db,signal,self.quote(),fixtures.T)['filled'])
 def test_exit_not_blocked_by_failed_prefilter(self):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled']);self.db.batch([('DELETE FROM paper_prefilter_states WHERE namespace=?',(e.NS,))]);self.assertTrue(e.fill(self.db,self.signal('SELL'),self.quote(105),fixtures.T)['filled'])
 def test_immutable_policy_and_no_half_migration(self):
  self.db.batch([('UPDATE paper_prefilter_policies SET policy_json=? WHERE version=?',('{}',p.POLICY['version']))])
  try:
   with self.assertRaises(RuntimeError):p.init(e,self.db)
  finally:self.db.batch([('UPDATE paper_prefilter_policies SET policy_json=? WHERE version=?',(e.js(p.POLICY),p.POLICY['version']))])
 def test_held_exit_scan_survives_prefilter_failure(self):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled']);s,b=frames();s['symbol']='RELIANCE-EQ';last=s.iloc[-1].copy();last.trade_date='2026-11-09';s=pd.concat([s,pd.DataFrame([last])],ignore_index=True)
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-08'))])
  def indicator(i,g):return g.assign(entry_condition=True,exit_condition=True)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'FAIL'}),patch.object(e,'indicator',side_effect=indicator):
   r=e.scan(self.db,None);self.assertEqual(r['signals_queued'],1);q=self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND status='pending'",(e.NS,));self.assertEqual([x['side'] for x in q],['SELL']);self.assertEqual(r['latest_qualifiers'],0)
 def test_stale_held_position_exits_and_alerts_after_252_sessions(self):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=1 AND symbol=?',('2025-09-01T09:15:00+05:30',e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side,reason FROM paper_signals WHERE namespace=? AND strategy=1 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(len(q),1);self.assertTrue(q[0]['reason'].startswith('stale_held_exit'))
  import json as _json;ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='stale_held_position'",(e.NS,));self.assertEqual(len(ev),1);self.assertGreaterEqual(_json.loads(ev[0]['data_json'])['held_sessions'],252)
 def test_stale_held_boundary_exactly_252_sessions_exits(self):
  # F18 boundary: held exactly 252 sessions MUST exit. The frame has 300
  # business days ending 2026-11-09, but session_inputs.filter_history
  # removes 15 NSE holidays, leaving 285 rows the engine counts. Entry on
  # 2025-10-31 (the 34th filtered session) puts exactly 252 bars in
  # [entry_day, bar]. held_sessions is asserted == 252, not >=, so this
  # test pins the exact threshold: it fails if the threshold moves to 253.
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=1 AND symbol=?',('2025-10-31T09:15:00+05:30',e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side,reason FROM paper_signals WHERE namespace=? AND strategy=1 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(len(q),1);self.assertTrue(q[0]['reason'].startswith('stale_held_exit'))
  import json as _json;ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='stale_held_position'",(e.NS,));self.assertEqual(len(ev),1);self.assertEqual(_json.loads(ev[0]['data_json'])['held_sessions'],252)
 def test_stale_held_boundary_exactly_251_sessions_does_not_exit(self):
  # F18 boundary: held exactly 251 sessions must NOT exit. Entry on
  # 2025-11-03 (the 35th filtered session, one session after the 252
  # entry) puts exactly 251 bars in [entry_day, bar]. This test pins the
  # exact threshold: it fails if the threshold moves to 251 (the 251-held
  # position would then exit).
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=1 AND symbol=?',('2025-11-03T09:15:00+05:30',e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND strategy=1 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(q,[])
  ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='stale_held_position'",(e.NS,));self.assertEqual(ev,[])
 def test_recently_held_position_does_not_exit_as_stale(self):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  self.db.batch([('UPDATE paper_positions SET entry_at=? WHERE namespace=? AND strategy=1 AND symbol=?',('2026-11-06T09:15:00+05:30',e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND strategy=1 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(q,[])
  ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='stale_held_position'",(e.NS,));self.assertEqual(ev,[])
 def test_missed_s7_trail_touch_queues_sell_and_keeps_alert(self):
  # I19: a completed bar touched the S7 trail (low 89.0 below the 90.0
  # trail on high_water 100.0) while the position stayed open, i.e. the
  # monitor missed it. The scan must queue a SELL for the next session
  # AND still write the old missed_stop_touch alert. The indicator is
  # pinned to no-exit so the only exit source is the missed touch.
  self.assertTrue(e.fill(self.db,self.signal(strategy=7),self.quote(),fixtures.T)['filled'])
  self.db.batch([('UPDATE paper_positions SET entry_at=?,high_water=? WHERE namespace=? AND strategy=7 AND symbol=?',('2026-11-06T09:15:00+05:30',100.0,e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,7,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';s.loc[s.index[-1],'low']=89.0
  b=f(100.)
  def indicator(i,g):return g.assign(entry_condition=False,exit_condition=False)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}),patch.object(e,'indicator',side_effect=indicator):
   r=e.scan(self.db,None)
  self.assertEqual(r['signals_queued'],1)
  q=self.db.query("SELECT side,reason,bar_date,eligible_date FROM paper_signals WHERE namespace=? AND strategy=7 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(len(q),1);self.assertEqual(q[0]['reason'],'missed_trail_touch');self.assertEqual(q[0]['bar_date'],'2026-11-09');self.assertEqual(q[0]['eligible_date'],'2026-11-11')
  import json as _json;ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='missed_stop_touch'",(e.NS,));self.assertEqual(len(ev),1);d=_json.loads(ev[0]['data_json']);self.assertEqual(d['bar_date'],'2026-11-09');self.assertEqual(d['threshold'],90.0);self.assertEqual(d['price'],89.0)
 def test_missed_s7_trail_touch_not_queued_when_untouched(self):
  # I19 guard: with the trail untouched (last-bar low 99.0, above the
  # 90.0 trail), the same scan must queue NO SELL and write NO alert.
  self.assertTrue(e.fill(self.db,self.signal(strategy=7),self.quote(),fixtures.T)['filled'])
  self.db.batch([('UPDATE paper_positions SET entry_at=?,high_water=? WHERE namespace=? AND strategy=7 AND symbol=?',('2026-11-06T09:15:00+05:30',100.0,e.NS,'RELIANCE-EQ'))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,7,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ'
  b=f(100.)
  def indicator(i,g):return g.assign(entry_condition=False,exit_condition=False)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}),patch.object(e,'indicator',side_effect=indicator):
   r=e.scan(self.db,None)
  self.assertEqual(r['signals_queued'],0)
  q=self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND strategy=7 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(q,[])
  ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='missed_stop_touch'",(e.NS,));self.assertEqual(ev,[])
 def test_split_ex_date_does_not_cause_false_exit(self):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  self.db.batch([('INSERT INTO paper_corporate_actions(symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at) VALUES(?,?,?,?,?,?,?,?)',('RELIANCE-EQ','2026-11-09','SPLIT',2,1,None,'TEST',fixtures.T.isoformat()))])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=253).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s.loc[s.index[-1],['open','high','low','close']]=[50.,51.,49.,50.];s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side FROM paper_signals WHERE namespace=? AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(q,[])
 def test_split_exit_fires_without_corporate_action_record(self):
  # Control for the test above: with no corporate-action record the engine
  # cannot know about the split, so the raw-price exit fires. This pins the
  # pre-fix behaviour the back-adjustment corrects.
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(),fixtures.T)['filled'])
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=253).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s.loc[s.index[-1],['open','high','low','close']]=[50.,51.,49.,50.];s['symbol']='RELIANCE-EQ';b=f(100.)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}):
   r=e.scan(self.db,None)
  q=self.db.query("SELECT side,reason FROM paper_signals WHERE namespace=? AND strategy=1 AND symbol='RELIANCE-EQ' AND side='SELL' AND status='pending'",(e.NS,));self.assertEqual(len(q),1);self.assertTrue(q[0]['reason'].startswith('daily_exit'))
 def test_unfiltered_legacy_input_proof_cannot_fill(self):
  for sql,args in [('DELETE FROM paper_entry_input_proofs WHERE namespace=?',(e.NS,)),('UPDATE paper_entry_input_proofs SET policy_version=? WHERE namespace=?',('old_inputs',e.NS)),('UPDATE paper_entry_input_proofs SET observed_at=? WHERE namespace=?',('2000-01-01',e.NS)),('UPDATE paper_qualifier_states SET checklist_json=? WHERE namespace=?',('{}',e.NS))]:
   signal=self.signal();self.db.batch([(sql,args)]);self.assertFalse(e.fill(self.db,signal,self.quote(),fixtures.T)['filled'])
  self.assertEqual(self.db.positions(),[])
 def test_live_input_filter_shared_by_all_seven_full_warmups(self):
  dates=pd.date_range('2024-01-01','2026-11-09',freq='B').strftime('%Y-%m-%d').tolist()+['2026-02-01'];s=pd.DataFrame({'trade_date':dates,'symbol':'RELIANCE-EQ','open':100.,'high':102.,'low':99.,'close':101.,'volume':1000.}).sort_values('trade_date');seen=[]
  def indicator(i,g):
   self.assertNotIn('2026-01-15',g.trade_date.tolist());self.assertIn('2026-02-01',g.trade_date.tolist());self.assertGreater(len(g),500);seen.append(i);return g.assign(entry_condition=False,exit_condition=False)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=s),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}),patch.object(e,'indicator',side_effect=indicator):
   report=e.scan(self.db,None,snapshot_only=True)
  self.assertEqual(seen,list(range(1,8)));self.assertEqual(report['signals_queued'],0);self.assertFalse(report['input_validation']['raw_rows_mutated'])
 def _rerun_scan_frames(self):
  # Bar D is 2026-11-06 (Friday); the signal is eligible 2026-11-09 (Monday), so a fill at
  # fixtures.T (2026-11-09 10:00) is eligible. The two scans run at 16:00 and 17:00 on 2026-11-06.
  s,b=frames();s['symbol']='RELIANCE-EQ'
  return s,b
 def _scan_at(self,s,b,when):
  def indicator(i,g):return g.assign(entry_condition=True,exit_condition=False)
  with patch.object(e,'now',return_value=when),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}),patch.object(e,'indicator',side_effect=indicator):
   return e.scan(self.db,None)
 def test_scan_rerun_same_bar_keeps_pending_buy_fillable(self):
  s,b=self._rerun_scan_frames()
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-05'))])
  t1=(fixtures.T-timedelta(days=3)).replace(hour=16);t2=(fixtures.T-timedelta(days=3)).replace(hour=17)
  r1=self._scan_at(s,b,t1);self.assertEqual(r1['signals_queued'],1)
  rows=self.db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(e.NS,));self.assertEqual(len(rows),1);sid=rows[0]['id']
  self.assertEqual(self.db.query("SELECT observed_at FROM paper_entry_input_proofs WHERE namespace=? AND signal_id=?",(e.NS,sid))[0]['observed_at'],t1.isoformat())
  r2=self._scan_at(s,b,t2);self.assertEqual(r2['signals_queued'],0)
  self.seed_adv('RELIANCE-EQ',200000000)
  signal=self.db.query("SELECT * FROM paper_signals WHERE namespace=? AND id=?",(e.NS,sid))[0]
  # The signal is eligible on 2026-11-08 (the Muhurat special session 14:20-15:15, the next
  # session after bar 2026-11-06), so the fill runs then, with a fresh quote inside that session.
  tf=(fixtures.T-timedelta(days=1)).replace(hour=14,minute=30)
  r=e.fill(self.db,signal,{'symbol':'RELIANCE-EQ','price':100,'quote_at':tf.isoformat()},tf);self.assertTrue(r['filled'],r)
  self.assertEqual(self.db.query("SELECT observed_at FROM paper_entry_input_proofs WHERE namespace=? AND signal_id=?",(e.NS,sid))[0]['observed_at'],t2.isoformat())
  self.assertEqual(self.db.query("SELECT scanned_at FROM paper_qualifier_states WHERE namespace=? AND symbol=? AND strategy=1 AND bar_date='2026-11-06'",(e.NS,'RELIANCE-EQ'))[0]['scanned_at'],t2.isoformat())
  self.assertEqual(self.db.query("SELECT observed_at FROM paper_prefilter_states WHERE namespace=? AND symbol=? AND bar_date='2026-11-06'",(e.NS,'RELIANCE-EQ'))[0]['observed_at'],t2.isoformat())
 def test_scan_rerun_does_not_rewrite_proof_of_non_pending_signal(self):
  s,b=self._rerun_scan_frames()
  self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,1,'RELIANCE-EQ','2026-11-05'))])
  t1=(fixtures.T-timedelta(days=3)).replace(hour=16);t2=(fixtures.T-timedelta(days=3)).replace(hour=17)
  r1=self._scan_at(s,b,t1);self.assertEqual(r1['signals_queued'],1)
  rows=self.db.query("SELECT * FROM paper_signals WHERE namespace=? AND status='pending'",(e.NS,));self.assertEqual(len(rows),1);sid=rows[0]['id']
  self.db.batch([("UPDATE paper_signals SET status='blocked_prefilter' WHERE namespace=? AND id=?",(e.NS,sid))])
  r2=self._scan_at(s,b,t2);self.assertEqual(r2['signals_queued'],0)
  self.assertEqual(self.db.query("SELECT observed_at FROM paper_entry_input_proofs WHERE namespace=? AND signal_id=?",(e.NS,sid))[0]['observed_at'],t1.isoformat())
  # the re-run itself happened: qualifier and prefilter advanced to T2, only the proof was held back
  self.assertEqual(self.db.query("SELECT scanned_at FROM paper_qualifier_states WHERE namespace=? AND symbol=? AND strategy=1 AND bar_date='2026-11-06'",(e.NS,'RELIANCE-EQ'))[0]['scanned_at'],t2.isoformat())
  self.assertEqual(self.db.query("SELECT observed_at FROM paper_prefilter_states WHERE namespace=? AND symbol=? AND bar_date='2026-11-06'",(e.NS,'RELIANCE-EQ'))[0]['observed_at'],t2.isoformat())
  self.assertEqual(self.db.query("SELECT status FROM paper_signals WHERE namespace=? AND id=?",(e.NS,sid))[0]['status'],'blocked_prefilter')
