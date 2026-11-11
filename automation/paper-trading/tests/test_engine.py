import json,sys,unittest,uuid,threading,tempfile
from pathlib import Path
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import engine as e
T=datetime(2026,11,9,10,tzinfo=e.IST)
class PaperTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.db=e.DB(str(e.ROOT/'tests'/'test_')+uuid.uuid4().hex+'.sqlite');e.NS='test_fixture';e.init(cls.db)
  cls.db.batch([('CREATE TABLE daily_ohlcv(symbol TEXT,trade_date TEXT,close REAL,volume REAL,turnover REAL)',())]);e.now=lambda:T
 def setUp(self):e.NS='test_'+uuid.uuid4().hex;e.now=lambda:T;self.db.batch([('DELETE FROM paper_corporate_actions',()),('DELETE FROM paper_price_bands',())])
 def seed_adv(self,symbol,turnover,sessions=40):
  self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?',(symbol,))])
  if sessions:self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,turnover) VALUES(?,?,?,?)',(symbol,(T.date()-timedelta(days=i)).isoformat(),100,turnover)) for i in range(1,sessions+1)])
 def signal(self,side='BUY',symbol='RELIANCE-EQ',strategy=1,eligible='2026-11-09'):
  self.seed_adv(symbol,200000000)
  s={'id':uuid.uuid4().hex,'strategy':strategy,'symbol':symbol,'side':side,'bar_date':'2026-11-08','eligible_date':eligible,'reason':'fixture'}
  self.db.batch([('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',(e.NS,s['id'],strategy,symbol,s['bar_date'],side,'pending','fixture',T.isoformat(),eligible))])
  if side=='BUY':self.db.batch([('INSERT OR REPLACE INTO paper_entry_input_proofs VALUES(?,?,?,?,?)',(e.NS,s['id'],e.si.POLICY['version'],s['bar_date'],T.isoformat())),('INSERT OR REPLACE INTO paper_prefilter_states VALUES(?,?,?,?,?,?,?)',(e.NS,s['bar_date'],symbol,e.pf.POLICY['version'],'PASS','{}',T.isoformat())),('INSERT OR REPLACE INTO paper_entry_gate_proofs VALUES(?,?,?,?,?)',(e.NS,s['id'],s['bar_date'],symbol,e.pf.POLICY['version'])),('INSERT OR IGNORE INTO paper_qualifier_states VALUES(?,?,?,?,?,?,?,?)',(e.NS,s['bar_date'],strategy,symbol,1,e.js({'input_policy':{'version':e.si.POLICY['version']}}),'{}',T.isoformat()))])
  return s
 def quote(self,p=100,age=0):return {'symbol':'RELIANCE-EQ','price':p,'quote_at':(T-timedelta(seconds=age)).isoformat()}
 def test_buy_duplicate_exit_and_pnl(self):
  s=self.signal();buy=e.fill(self.db,s,self.quote(),T);self.assertTrue(buy['filled']);self.assertEqual(buy['qty'],500);self.assertEqual(buy['price'],100.09);self.assertFalse(e.fill(self.db,s,self.quote(),T)['filled'])
  out=e.fill(self.db,self.signal('SELL'),self.quote(110),T);self.assertLess(out['price'],110);self.assertEqual(out['realized_pnl'],(out['price']-buy['price'])*buy['qty'])
  p=e.pnl(self.db);self.assertEqual(round(p['daily_mark_to_market_pnl_INR'],2),round(out['realized_pnl'],2));self.assertEqual(round(p['latest_known_total_pnl_INR'],2),round(out['realized_pnl'],2));self.assertEqual(len(self.db.positions()),0)
 def test_multistock_and_strategy_separation(self):
  for sym,strategy in [('RELIANCE-EQ',1),('RELIANCE-EQ',2),('TCS-EQ',1)]:self.assertTrue(e.fill(self.db,self.signal(symbol=sym,strategy=strategy),self.quote(),T)['filled'])
  self.assertEqual(len(self.db.positions()),3)
 def test_no_oversell(self):self.assertFalse(e.fill(self.db,self.signal('SELL'),self.quote(),T)['filled'])
 def test_cannot_duplicate_position(self):
  e.fill(self.db,self.signal(),self.quote(),T);self.assertFalse(e.fill(self.db,self.signal(),self.quote(),T)['filled'])
 def test_reject_stale(self):self.assertFalse(e.fill(self.db,self.signal(),self.quote(age=61),T)['filled'])
 def test_reject_future(self):self.assertFalse(e.fill(self.db,self.signal(),self.quote(age=-10),T)['filled'])
 def test_reject_zero(self):self.assertFalse(e.fill(self.db,self.signal(),self.quote(0),T)['filled'])
 def test_target_notional_share_count_and_unaffordable_skip(self):
  priced=e.fill(self.db,self.signal(),self.quote(2500),T);self.assertTrue(priced['filled']);self.assertEqual(priced['qty'],20)
  s=self.signal(strategy=2);r=e.fill(self.db,s,self.quote(60000),T);self.assertFalse(r['filled']);self.assertEqual(r['reason'],'UNAFFORDABLE_ZERO_SHARES');self.assertEqual(r['quantity'],0)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,s['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND strategy=2',(e.NS,)))
  rows=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='paper_order_result'",(e.NS,));self.assertTrue(any(json.loads(x['data_json']).get('reason')=='UNAFFORDABLE_ZERO_SHARES' for x in rows))
  exact=e.fill(self.db,self.signal(strategy=3),self.quote(50000),T);self.assertTrue(exact['filled']);self.assertEqual(exact['qty'],1)
 def test_sleeve_holds_nine_positions_and_keeps_the_next_entry_pending(self):
  symbols=['360ONE-EQ','3MINDIA-EQ','ABB-EQ','ACC-EQ','AGI-EQ','AIAENG-EQ','APLAPOLLO-EQ','ASKAUTOLTD-EQ','AUBANK-EQ','AARTIDRUGS-EQ']
  for sym in symbols[:9]:self.assertTrue(e.fill(self.db,self.signal(symbol=sym,strategy=1),self.quote(),T)['filled'])
  skipped=self.signal(symbol=symbols[9],strategy=1);r=e.fill(self.db,skipped,self.quote(),T)
  self.assertFalse(r['filled']);self.assertEqual(r['reason'],'SLEEVE_SLOT_FULL');self.assertEqual(r['quantity'],0);self.assertEqual(r['open_positions'],9)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,skipped['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND strategy=1 AND symbol=?',(e.NS,symbols[9])))
  blocked=self.db.query("SELECT outcome,data_json FROM paper_events WHERE namespace=? AND event_type='paper_order_blocked'",(e.NS,))
  self.assertTrue(any(x['outcome']=='SLEEVE_SLOT_FULL' and json.loads(x['data_json']).get('open_positions')==9 for x in blocked))
  self.assertTrue(e.fill(self.db,self.signal(symbol=symbols[9],strategy=2),self.quote(),T)['filled'])
  self.assertTrue(e.fill(self.db,self.signal('SELL',symbol=symbols[0],strategy=1),self.quote(110),T)['filled'])
  self.assertTrue(e.fill(self.db,skipped,self.quote(),T)['filled'])
  self.assertEqual(len([p for p in self.db.positions() if p['strategy']==1]),9)
 def test_monitor_frees_a_slot_before_entries_and_hashes_the_last_slot(self):
  symbols=['360ONE-EQ','3MINDIA-EQ','ABB-EQ','ACC-EQ','AGI-EQ','AIAENG-EQ','APLAPOLLO-EQ','ASKAUTOLTD-EQ','AUBANK-EQ']
  for sym in symbols:self.assertTrue(e.fill(self.db,self.signal(symbol=sym,strategy=1),self.quote(),T)['filled'])
  contenders=['AARTIDRUGS-EQ','AARTIIND-EQ'];day=T.date().isoformat()
  winner=min(contenders,key=lambda sym:(e.cap.slot_rank(day,sym),sym));loser=[sym for sym in contenders if sym!=winner][0]
  sell=self.signal('SELL',symbol=symbols[0],strategy=1);buy=self.signal(symbol=winner,strategy=1);other=self.signal(symbol=loser,strategy=1)
  class Feed:
   def quotes(_,requested):return {sym:{'symbol':sym,'price':100,'quote_at':T.isoformat(),'provider_status':'ok'} for sym in requested}
  report=e.monitor(self.db,Feed());self.assertEqual(report['fills'],2)
  self.assertFalse(self.db.query('SELECT * FROM paper_positions WHERE namespace=? AND strategy=1 AND symbol=? AND qty>0',(e.NS,symbols[0])))
  self.assertTrue(self.db.query('SELECT * FROM paper_positions WHERE namespace=? AND strategy=1 AND symbol=? AND qty>0',(e.NS,winner)))
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,other['id']))[0]['status'],'pending')
  self.assertEqual(len([p for p in self.db.positions() if p['strategy']==1]),9)
 def test_fill_is_ltp_legacy_and_decision_chain_detects_tamper(self):
  s=self.signal();r=e.fill(self.db,s,self.quote(),T);self.assertTrue(r['filled']);self.assertEqual(r['execution_path'],'LTP_LEGACY');self.assertEqual(r['correlation_id'],s['id'])
  self.assertEqual(self.db.query('SELECT execution_path FROM paper_fills WHERE namespace=?',(e.NS,))[0]['execution_path'],'LTP_LEGACY')
  decision=self.db.query('SELECT * FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]
  self.assertEqual(decision['decision_type'],'ENTRY');self.assertEqual(decision['cause'],'filled');self.assertEqual(decision['execution_path'],'LTP_LEGACY')
  payload=json.loads(decision['payload_json']);self.assertEqual(payload['quantity'],500);self.assertEqual(payload['price'],100.09);self.assertEqual(payload['reference_price'],100);self.assertEqual(payload['slippage_rule'],'tiered_adv_slippage');self.assertEqual(payload['open_positions_before'],0);self.assertIn('strategy_version',payload)
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
  self.db.batch([('UPDATE paper_decisions SET cause=? WHERE namespace=? AND seq=?',('tampered',e.NS,decision['seq']))])
  self.assertFalse(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_unaffordable_skip_is_on_the_decision_chain(self):
  s=self.signal(strategy=2);r=e.fill(self.db,s,self.quote(60000),T);self.assertEqual(r['reason'],'UNAFFORDABLE_ZERO_SHARES')
  decision=self.db.query('SELECT decision_type,cause,execution_path FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]
  self.assertEqual(decision['decision_type'],'SKIP');self.assertEqual(decision['cause'],'UNAFFORDABLE_ZERO_SHARES');self.assertEqual(decision['execution_path'],'LTP_LEGACY')
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_isin_cap_skips_the_fourth_strategy_on_the_same_symbol(self):
  for strategy in (1,2,3):self.assertTrue(e.fill(self.db,self.signal(symbol='RELIANCE-EQ',strategy=strategy),self.quote(),T)['filled'])
  skipped=self.signal(symbol='RELIANCE-EQ',strategy=4);r=e.fill(self.db,skipped,self.quote(),T)
  self.assertFalse(r['filled']);self.assertEqual(r['reason'],'ISIN_CAP');self.assertEqual(r['quantity'],0)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,skipped['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND strategy=4 AND symbol=?',(e.NS,'RELIANCE-EQ')))
  self.assertEqual(self.db.query("SELECT cause FROM paper_decisions WHERE namespace=? AND correlation_id=?",(e.NS,skipped['id']))[0]['cause'],'ISIN_CAP')
  self.assertTrue(e.fill(self.db,self.signal(symbol='TCS-EQ',strategy=4),self.quote(),T)['filled'])
  self.assertTrue(e.fill(self.db,self.signal('SELL',symbol='RELIANCE-EQ',strategy=1),self.quote(110),T)['filled'])
  self.assertTrue(e.fill(self.db,skipped,self.quote(),T)['filled'])
 def test_monitor_admits_strategies_in_order_until_the_isin_cap(self):
  for strategy in (4,1,3,2):self.signal(symbol='RELIANCE-EQ',strategy=strategy)
  class Feed:
   def quotes(_,requested):return {sym:{'symbol':sym,'price':100,'quote_at':T.isoformat(),'provider_status':'ok'} for sym in requested}
  e.monitor(self.db,Feed())
  self.assertEqual(sorted(p['strategy'] for p in self.db.positions()),[1,2,3])
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND strategy=4',(e.NS,))[0]['status'],'pending')
 def test_shadow_entry_does_not_consume_the_isin_cap(self):
  for strategy in (1,2,3):self.assertTrue(e.fill(self.db,self.signal(symbol='RELIANCE-EQ',strategy=strategy),self.quote(),T)['filled'])
  skipped=self.signal(symbol='RELIANCE-EQ',strategy=4);r=e.fill(self.db,skipped,self.quote(),T)
  self.assertEqual(r['reason'],'ISIN_CAP');self.assertTrue(r['shadow_recorded'])
  self.assertEqual(len(self.db.query('SELECT * FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'RELIANCE-EQ'))),3)
  self.assertEqual(self.db.query('SELECT qty FROM paper_shadow_positions WHERE namespace=? AND strategy=4 AND symbol=?',(e.NS,'RELIANCE-EQ'))[0]['qty'],500)
  e.fill(self.db,skipped,self.quote(),T)
  self.assertEqual(len(self.db.query('SELECT * FROM paper_shadow_costs WHERE namespace=? AND signal_id=?',(e.NS,skipped['id']))),1)
  self.assertTrue(e.fill(self.db,self.signal(symbol='TCS-EQ',strategy=5),self.quote(),T)['filled'])
  self.assertEqual(len(self.db.query('SELECT * FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'RELIANCE-EQ'))),3)
 def test_adv_floor_skips_a_thin_stock_and_still_allows_the_exit(self):
  thin=self.signal();self.seed_adv('RELIANCE-EQ',90000000)
  blocked=e.fill(self.db,thin,self.quote(),T)
  self.assertEqual(blocked['reason'],'ADV_FILTER_SKIP');self.assertEqual(blocked['quantity'],0)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,thin['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND signal_id=?',(e.NS,thin['id'])))
  decision=self.db.query('SELECT cause,rule,payload_json FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,thin['id']))[0]
  self.assertEqual(decision['cause'],'ADV_FILTER_SKIP');self.assertEqual(decision['rule'],'median_20d_traded_value_vs_reviewed_floor')
  self.assertEqual(json.loads(decision['payload_json'])['adv_floor_review'],'reviewed')
  exact=self.signal(strategy=2);self.seed_adv('RELIANCE-EQ',e.cap.POLICY['adv_floor_inr'])
  self.assertTrue(e.fill(self.db,exact,self.quote(),T)['filled'])
  short=self.signal(strategy=3);self.seed_adv('RELIANCE-EQ',500000000,sessions=19)
  missed=e.fill(self.db,short,self.quote(),T)
  self.assertEqual(missed['reason'],'ADV_FILTER_SKIP');self.assertEqual(missed['adv_rule'],'lagged_adv_history_short')
  held=self.signal(symbol='TCS-EQ',strategy=1);self.assertTrue(e.fill(self.db,held,self.quote(),T)['filled'])
  sell=self.signal('SELL',symbol='TCS-EQ',strategy=1);self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?',('TCS-EQ',))])
  self.assertTrue(e.fill(self.db,sell,self.quote(110),T)['filled'])
 def test_adv_uses_close_x_volume_when_turnover_missing_and_skips_muhurat(self):
  # Stored Fyers turnover equals close*volume; NULL turnover rows use the same derivation instead of failing as history-short.
  ok=self.signal();short=self.signal(strategy=2)
  self.db.batch([('DELETE FROM daily_ohlcv WHERE symbol=?',('RELIANCE-EQ',))])
  # close × volume must clear the reviewed ADV floor; Muhurat's tiny turnover must not enter the median.
  self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,volume,turnover) VALUES(?,?,?,?,?)',('RELIANCE-EQ',(T.date()-timedelta(days=i)).isoformat(),100,2500000,None)) for i in range(2,22)])
  self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close,volume,turnover) VALUES(?,?,?,?,?)',('RELIANCE-EQ','2026-11-08',100,10,1000))])
  r=e.fill(self.db,ok,self.quote(),T)
  self.assertTrue(r['filled'])
  payload=json.loads(self.db.query('SELECT payload_json FROM paper_decisions WHERE namespace=? AND correlation_id=? ORDER BY seq DESC LIMIT 1',(e.NS,ok['id']))[0]['payload_json'])
  self.assertEqual(payload['adv_median_inr'],250000000)
  # Zero-volume, NULL-turnover rows are not evidence: still history-short, never a fabricated value.
  self.db.batch([('UPDATE daily_ohlcv SET volume=0 WHERE symbol=? AND trade_date<?',('RELIANCE-EQ','2026-10-25'))])
  m=e.fill(self.db,short,self.quote(),T)
  self.assertEqual(m['reason'],'ADV_FILTER_SKIP');self.assertEqual(m['adv_rule'],'lagged_adv_history_short')
 def test_participation_cap_skips_without_scaling_and_still_exits(self):
  # Participation cap is rate × lagged ADV. Price 100 buys exactly the target notional.
  version=json.loads((e.ROOT/'charge_policy.json').read_text())['version']
  floor=e.cap.POLICY['adv_floor_inr']; rate=e.cap.POLICY['participation_rate']; notional=e.cap.POLICY['target_notional_inr']
  cap_paise=int(floor*rate*100); order_paise=notional*100
  s=self.signal(strategy=2);self.seed_adv('RELIANCE-EQ',floor)
  self.db.batch([('INSERT INTO paper_fill_costs(namespace,fill_id,day,strategy,symbol,isin,side,turnover_paise,policy_version,components_json,total_paise,entry_at,gross_cashflow_paise,net_cashflow_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(e.NS,900002,T.date().isoformat(),1,'RELIANCE-EQ','INE002A01018','BUY',cap_paise-order_paise+10000,version,'{}',0,T.isoformat(),-(cap_paise-order_paise+10000),0))])
  blocked=e.fill(self.db,s,self.quote(),T)
  self.assertEqual(blocked['reason'],'ADV_PARTICIPATION_CAP');self.assertEqual(blocked['quantity'],0)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,s['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND signal_id=?',(e.NS,s['id'])))
  self.assertEqual(self.db.query('SELECT cause FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]['cause'],'ADV_PARTICIPATION_CAP')
  self.db.batch([('UPDATE paper_fill_costs SET turnover_paise=? WHERE namespace=? AND fill_id=?',(cap_paise-order_paise,e.NS,900002))])
  filled=e.fill(self.db,s,self.quote(),T);self.assertTrue(filled['filled']);self.assertEqual(filled['qty'],500)
  held=self.signal(symbol='TCS-EQ');self.assertTrue(e.fill(self.db,held,self.quote(),T)['filled'])
  self.db.batch([('INSERT INTO paper_fill_costs(namespace,fill_id,day,strategy,symbol,isin,side,turnover_paise,policy_version,components_json,total_paise,entry_at,gross_cashflow_paise,net_cashflow_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(e.NS,900003,T.date().isoformat(),2,'TCS-EQ','INE467B01029','BUY',9000000000,version,'{}',0,T.isoformat(),0,0))])
  self.assertTrue(e.fill(self.db,self.signal('SELL',symbol='TCS-EQ'),self.quote(110),T)['filled'])
 def test_tiered_slippage_keeps_the_share_count(self):
  s=self.signal();bought=e.fill(self.db,s,self.quote(),T);self.assertEqual(bought['qty'],500);self.assertEqual(bought['price'],100.09)
  normal=self.db.call({'action':'slippage','adv_paise':20000000000,'order_paise':5000000,'reference_paise':10000,'side':'BUY','stress':False})
  self.assertEqual(normal['base_bps'],8);self.assertEqual(normal['price'],100.09)
  above=self.db.call({'action':'slippage','adv_paise':25000000000,'order_paise':5000000,'reference_paise':10000,'side':'BUY','stress':False})
  self.assertEqual(above['base_bps'],5);self.assertLess(above['price'],normal['price'])
  stress=self.db.call({'action':'slippage','adv_paise':20000000000,'order_paise':5000000,'reference_paise':10000,'side':'BUY','stress':True})
  self.assertGreater(stress['price'],normal['price'])
  rich=self.signal(strategy=2);self.seed_adv('RELIANCE-EQ',250000000);filled=e.fill(self.db,rich,self.quote(),T)
  self.assertEqual(filled['qty'],500);self.assertEqual(filled['price'],above['price'])
 def test_insufficient_sleeve_cash_keeps_the_entry_pending(self):
  # ₹4,99,500 already spent in paise. ₹500 remains, which cannot fund a ₹50,000 entry plus fees.
  self.db.batch([('INSERT INTO paper_fill_costs(namespace,fill_id,day,strategy,symbol,isin,side,turnover_paise,policy_version,components_json,total_paise,entry_at,gross_cashflow_paise,net_cashflow_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(e.NS,900001,'2026-11-06',1,'RELIANCE-EQ','INE002A01018','BUY',49950000,'FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_07_v1','{}',0,'2026-11-06T10:00:00+05:30',-49950000,-49950000))])
  s=self.signal();r=e.fill(self.db,s,self.quote(),T)
  self.assertFalse(r['filled']);self.assertEqual(r['reason'],'INSUFFICIENT_CASH');self.assertEqual(r['quantity'],0);self.assertLess(r['cash_paise'],r['need_paise'])
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,s['id']))[0]['status'],'pending')
  self.assertFalse(self.db.query('SELECT * FROM paper_fills WHERE namespace=? AND signal_id=?',(e.NS,s['id'])))
  self.assertEqual(self.db.query('SELECT cause FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]['cause'],'INSUFFICIENT_CASH')
  self.assertTrue(e.fill(self.db,self.signal(strategy=2),self.quote(),T)['filled'])
 def test_reject_missing_timestamp(self):self.assertFalse(e.fill(self.db,self.signal(),{'price':100},T)['filled'])
 def test_closed_market(self):
  saturday=datetime(2026,11,7,10,tzinfo=e.IST);self.assertFalse(e.fill(self.db,self.signal(),self.quote(),saturday)['filled'])
 def test_expire_old_entry(self):
  s=self.signal(eligible='2026-11-06');self.assertFalse(e.fill(self.db,s,self.quote(),T)['filled']);r=self.db.query('SELECT status FROM paper_signals WHERE namespace=?',(e.NS,));self.assertEqual(r[0]['status'],'expired')
 def test_session_dates(self):
  self.assertFalse(e.is_open(datetime(2026,11,10,10,tzinfo=e.IST)));self.assertTrue(e.is_open(datetime(2026,11,8,14,30,tzinfo=e.IST)));self.assertFalse(e.is_open(datetime(2026,11,8,15,20,tzinfo=e.IST)));self.assertFalse(e.is_open(datetime(2027,1,4,10,tzinfo=e.IST)))
 def test_completed_candle_guard(self):
  self.assertEqual(str(e.completed_day(T)),'2026-11-08');self.assertEqual(str(e.completed_day(T.replace(hour=16))),'2026-11-09')
 def test_atomic_batch_failure(self):
  with self.assertRaises(RuntimeError):self.db.batch([('INSERT INTO paper_meta(namespace,key,value) VALUES(?,?,?)',(e.NS,'rollback','bad')),('INSERT INTO nonexistent VALUES(1)',())])
  self.assertFalse(self.db.query('SELECT * FROM paper_meta WHERE namespace=?',(e.NS,)))
 def test_concurrent_duplicate_fill(self):
  s=self.signal()
  def f(_):
   try:return e.fill(self.db,s,self.quote(),T)['filled']
   except RuntimeError:return False
  with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(f,[1,2]))
  self.assertEqual(sum(r),1);self.assertEqual(len(self.db.query('SELECT * FROM paper_fills WHERE namespace=?',(e.NS,))),1)
 def test_stop_fills_observed_quote_not_threshold(self):
  bought=e.fill(self.db,self.signal(strategy=7),self.quote(),T);self.assertTrue(bought['filled'])
  self.assertEqual(e.RULES['7']['evidence_clock'],'2026-11-09');self.assertIn('version',e.RULES['7'])
  class PolledOnly:
   def quotes(_,symbols):return {'RELIANCE-EQ':self.quote(85)}
  self.assertEqual(e.monitor(self.db,PolledOnly())['fills'],0);self.assertEqual(len(self.db.positions()),1)
  threshold=round(.9*bought['price'],2)
  class TouchFeed:
   def quotes(_,symbols):return {'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':85,'open':95,'low':85,'quote_at':T.isoformat()}}
  r=e.monitor(self.db,TouchFeed());self.assertEqual(r['fills'],1);fills=self.db.query('SELECT * FROM paper_fills WHERE namespace=? ORDER BY id',(e.NS,));self.assertLess(fills[-1]['price'],threshold);self.assertGreater(fills[-1]['price'],85)
  row=self.db.query("SELECT rule,execution_path,payload_json FROM paper_decisions WHERE namespace=? AND decision_type='EXIT'",(e.NS,))[0]
  self.assertEqual(row['rule'],'trail_touch');self.assertEqual(row['execution_path'],'LTP_LEGACY')
  exit_decision=json.loads(row['payload_json']);self.assertEqual(exit_decision['reference_price'],threshold);self.assertEqual(exit_decision['slippage_rule'],'tiered_adv_slippage')
 def test_open_path_sizes_on_the_official_open(self):
  mapped=e.quote_from_fyers({'n':'NSE:RELIANCE-EQ','s':'ok','v':{'lp':100,'open_price':90,'indicative_price':80,'reference_price':79,'tt':'2026-11-09T10:00:00+05:30'}})
  self.assertEqual(mapped['open'],90);self.assertEqual(mapped['price'],100);self.assertNotIn('auction_price',mapped)
  auction_only=e.quote_from_fyers({'n':'NSE:TCS-EQ','s':'ok','v':{'lp':100,'open_price':0,'indicative_price':80,'reference_price':79,'tt':'2026-11-09T10:00:00+05:30'}})
  self.assertIsNone(auction_only['open']);self.assertEqual(auction_only['auction_price'],80)
  repeated=e.quote_from_fyers({'n':'NSE:INFY-EQ','s':'ok','v':{'lp':100,'indicative_price':100,'reference_price':0,'tt':'2026-11-09T10:00:00+05:30'}})
  self.assertNotIn('auction_price',repeated)
  self.assertEqual(e.execution_reference({'price':85,'open':90},stop=True),(85,'LTP_LEGACY'))
  q=self.quote();q['open']=90;q['auction_price']=80
  s=self.signal();fed=e.fill(self.db,s,q,T);self.assertTrue(fed['filled']);self.assertEqual(fed['qty'],555);self.assertEqual(fed['execution_path'],'FEED_OPEN');self.assertGreater(fed['price'],90)
  payload=json.loads(self.db.query('SELECT rule,execution_path,payload_json FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]['payload_json'])
  decision=self.db.query('SELECT rule,execution_path FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,s['id']))[0]
  self.assertEqual(decision['execution_path'],'FEED_OPEN');self.assertEqual(decision['rule'],'feed_official_open');self.assertEqual(payload['reference_price'],90)
  self.assertEqual(self.db.query('SELECT execution_path FROM paper_fills WHERE namespace=? AND signal_id=?',(e.NS,s['id']))[0]['execution_path'],'FEED_OPEN')
  aq={'symbol':'TCS-EQ','price':100,'quote_at':T.isoformat(),'auction_price':80}
  caught=e.fill(self.db,self.signal(symbol='TCS-EQ'),aq,T);self.assertTrue(caught['filled']);self.assertEqual(caught['qty'],625);self.assertEqual(caught['execution_path'],'AUCTION_CAPTURE');self.assertGreater(caught['price'],80)
  fq={'symbol':'INFY-EQ','price':100,'quote_at':T.isoformat(),'first_trade':95}
  first=e.fill(self.db,self.signal(symbol='INFY-EQ'),fq,T);self.assertTrue(first['filled']);self.assertEqual(first['qty'],526);self.assertEqual(first['execution_path'],'FALLBACK_OPEN')
  stop=self.signal('SELL',symbol='HDFCBANK-EQ');stop['stop']=True
  skipped=e.fill(self.db,stop,{'symbol':'HDFCBANK-EQ','price':85,'quote_at':T.isoformat(),'open':90},T)
  self.assertFalse(skipped['filled']);self.assertEqual(skipped['execution_path'],'LTP_LEGACY')
  self.assertEqual(self.db.query('SELECT execution_path FROM paper_decisions WHERE namespace=? AND correlation_id=?',(e.NS,stop['id']))[0]['execution_path'],'LTP_LEGACY')
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_microstructure_blocks_the_locked_side_and_carries_the_exit(self):
  self.assertEqual(e.circuit_haircut(80,1),80);self.assertEqual(round(e.circuit_haircut(80,2),2),79.2)
  self.assertEqual(e.market_guard({'price':100,'open':100,'high':100,'low':100})['lock_unknown'],True)
  self.assertFalse(e.market_guard({'price':100,'open':100,'high':100,'low':100,'fo_eligible':True})['lock_unknown'])
  self.assertEqual(e.market_guard({'ask':0,'bid':1,'price':100})['circuit'],'UPPER')
  self.assertEqual(e.market_guard({'ask':1,'bid':0,'price':100})['circuit'],'LOWER')
  blocked=self.signal();first=e.fill(self.db,blocked,{**self.quote(),'circuit':'UPPER'},T);self.assertFalse(first['filled']);self.assertEqual(first['reason'],'CIRCUIT_LOCKED')
  second=e.fill(self.db,blocked,self.quote(),T);self.assertFalse(second['filled']);self.assertEqual(second['reason'],'CIRCUIT_LOCKED')
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,blocked['id']))[0]['status'],'pending')
  later=datetime(2026,11,11,10,tzinfo=e.IST);e.now=lambda:later
  expired=e.fill(self.db,blocked,{'symbol':'RELIANCE-EQ','price':100,'quote_at':later.isoformat()},later)
  self.assertEqual(expired['reason'],'expired_entry_signal');e.now=lambda:T
  bought=self.signal(symbol='TCS-EQ');self.assertTrue(e.fill(self.db,bought,{**self.quote(),'symbol':'TCS-EQ','circuit':'LOWER'},T)['filled'])
  be=e.fill(self.db,self.signal(symbol='INFY-EQ'),{**self.quote(),'symbol':'INFY-EQ','series':'BE'},T);self.assertEqual(be['reason'],'SERIES_TRANSITION_BE_BZ')
  asm=e.fill(self.db,self.signal(symbol='HDFCBANK-EQ'),{**self.quote(),'symbol':'HDFCBANK-EQ','surveillance':'ASM'},T);self.assertEqual(asm['reason'],'ASM_GSM_RESTRICTION')
  self.db.batch([('INSERT INTO paper_price_bands(symbol,trade_date,upper_price,lower_price,series,surveillance,fo_eligible,source) VALUES(?,?,?,?,?,?,?,?)',('ICICIBANK-EQ',T.date().isoformat(),100,90,'EQ',None,0,'TEST_BAND'))])
  banded=e.fill(self.db,self.signal(symbol='ICICIBANK-EQ'),{**self.quote(),'symbol':'ICICIBANK-EQ'},T);self.assertEqual(banded['reason'],'CIRCUIT_LOCKED')
  flat=e.fill(self.db,self.signal(symbol='WIPRO-EQ'),{'symbol':'WIPRO-EQ','price':100,'open':100,'high':100,'low':100,'quote_at':T.isoformat()},T)
  self.assertTrue(flat['filled']);payload=json.loads(self.db.query("SELECT payload_json FROM paper_decisions WHERE namespace=? AND decision_type='ENTRY' AND symbol='WIPRO-EQ'",(e.NS,))[0]['payload_json']);self.assertTrue(payload['lock_unknown'])
  sbin=e.fill(self.db,self.signal(symbol='SBIN-EQ'),{**self.quote(),'symbol':'SBIN-EQ'},T);axis=e.fill(self.db,self.signal(symbol='AXISBANK-EQ'),{**self.quote(),'symbol':'AXISBANK-EQ'},T)
  self.assertTrue(sbin['filled'] and axis['filled'])
  be_exit=e.fill(self.db,self.signal('SELL',symbol='SBIN-EQ'),{**self.quote(110),'symbol':'SBIN-EQ','series':'BE'},T)
  eq_exit=e.fill(self.db,self.signal('SELL',symbol='AXISBANK-EQ'),{**self.quote(110),'symbol':'AXISBANK-EQ'},T)
  self.assertTrue(be_exit['filled'] and eq_exit['filled']);self.assertLess(be_exit['price'],eq_exit['price'])
  be_payload=json.loads(self.db.query("SELECT payload_json FROM paper_decisions WHERE namespace=? AND decision_type='EXIT' AND symbol='SBIN-EQ'",(e.NS,))[0]['payload_json']);self.assertTrue(be_payload['series_exit_stress'])
  locked=e.fill(self.db,self.signal('SELL',symbol='TCS-EQ'),{**self.quote(80),'symbol':'TCS-EQ','circuit':'LOWER'},T)
  self.assertEqual(locked['reason'],'CIRCUIT_LOCKED');self.assertEqual(self.db.positions()[0]['symbol'] if False else self.db.query('SELECT pending_exit FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'TCS-EQ'))[0]['pending_exit'],1)
  class Feed:
   def quotes(_self,symbols):return {sym:{'symbol':sym,'price':80,'quote_at':T.isoformat(),'circuit':'LOWER'} for sym in symbols}
  e.monitor(self.db,Feed());self.assertEqual(self.db.query('SELECT mark_price FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'TCS-EQ'))[0]['mark_price'],80)
  self.db.batch([('INSERT INTO paper_circuit_locks(namespace,strategy,symbol,trade_date,direction,locked_price,recorded_at) VALUES(?,?,?,?,?,?,?)',(e.NS,1,'TCS-EQ','2026-11-06','LOWER',80,T.isoformat()))])
  e.monitor(self.db,Feed());self.assertEqual(round(self.db.query('SELECT mark_price FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'TCS-EQ'))[0]['mark_price'],2),79.2)
  cleared=e.fill(self.db,self.signal('SELL',symbol='TCS-EQ'),{**self.quote(80),'symbol':'TCS-EQ'},T);self.assertTrue(cleared['filled']);self.assertFalse(self.db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND symbol=? AND qty>0',(e.NS,'TCS-EQ')))
 def test_gap_stop_fills_the_open(self):
  self.assertTrue(e.fill(self.db,self.signal(strategy=7),self.quote(),T)['filled'])
  class Feed:
   def quotes(_,symbols):return {'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':88,'open':80,'quote_at':T.isoformat()}}
  self.assertEqual(e.monitor(self.db,Feed())['fills'],1)
  row=self.db.query("SELECT rule,payload_json FROM paper_decisions WHERE namespace=? AND decision_type='EXIT'",(e.NS,))[0]
  self.assertEqual(row['rule'],'trail_gap')
  exit_row=json.loads(row['payload_json'])
  self.assertEqual(exit_row['reference_price'],80);self.assertEqual(self.db.query("SELECT execution_path FROM paper_fills WHERE namespace=? AND side='SELL'",(e.NS,))[0]['execution_path'],'FEED_OPEN');self.assertLess(exit_row['price'],80)
 def test_strategy_07_chronology_sma_open_before_trail_touch(self):
  self.assertTrue(e.fill(self.db,self.signal(symbol='RELIANCE-EQ',strategy=7),self.quote(),T)['filled'])
  sma=self.signal('SELL',symbol='RELIANCE-EQ',strategy=7)
  self.assertTrue(e.fill(self.db,self.signal(symbol='TCS-EQ',strategy=7),self.quote(),T)['filled'])
  self.signal('SELL',symbol='TCS-EQ',strategy=7)
  class Feed:
   def quotes(_,symbols):return {
    'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':88,'open':95,'low':85,'quote_at':T.isoformat()},
    'TCS-EQ':{'symbol':'TCS-EQ','price':82,'open':80,'low':78,'quote_at':T.isoformat()}}
  self.assertEqual(e.monitor(self.db,Feed())['fills'],2)
  rel_fill=self.db.query("SELECT signal_id,execution_path FROM paper_fills WHERE namespace=? AND symbol='RELIANCE-EQ' AND side='SELL'",(e.NS,))[0]
  self.assertEqual(rel_fill['signal_id'],sma['id']);self.assertEqual(rel_fill['execution_path'],'FEED_OPEN')
  rel_exit=self.db.query("SELECT correlation_id,rule,execution_path,payload_json FROM paper_decisions WHERE namespace=? AND symbol='RELIANCE-EQ' AND decision_type='EXIT'",(e.NS,))[0]
  self.assertEqual(rel_exit['correlation_id'],sma['id']);self.assertEqual(rel_exit['rule'],'feed_official_open');self.assertEqual(rel_exit['execution_path'],'FEED_OPEN');self.assertEqual(json.loads(rel_exit['payload_json'])['reference_price'],95)
  tcs_exit=self.db.query("SELECT rule,execution_path,payload_json FROM paper_decisions WHERE namespace=? AND symbol='TCS-EQ' AND decision_type='EXIT'",(e.NS,))[0]
  self.assertEqual(tcs_exit['rule'],'trail_gap');self.assertEqual(tcs_exit['execution_path'],'FEED_OPEN');self.assertEqual(json.loads(tcs_exit['payload_json'])['reference_price'],80)
 def test_closed_monitor_no_quotes(self):
  e.now=lambda:datetime(2026,11,7,10,tzinfo=e.IST)
  class Feed:
   def quotes(_,s):raise AssertionError('should not fetch')
  self.assertEqual(e.monitor(self.db,Feed())['fills'],0)
 def test_daily_start_mark_not_entry_basis(self):
  # Position opened during prior special session, cost90; previous completed close100; current mark105.
  yesterday=datetime(2026,11,8,14,30,tzinfo=e.IST);s=self.signal(eligible='2026-11-08');q={'price':90,'quote_at':yesterday.isoformat()};self.assertTrue(e.fill(self.db,s,q,yesterday)['filled'])
  self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close) VALUES(?,?,?)',('RELIANCE-EQ','2026-11-08',100)),('UPDATE paper_positions SET mark_price=?,mark_at=? WHERE namespace=?',(105,T.isoformat(),e.NS))]);pos=self.db.positions()[0];p=e.pnl(self.db);self.assertEqual(p['daily_mark_to_market_pnl_INR'],2775);self.assertEqual(p['latest_known_total_pnl_INR'],(105-pos['entry_price'])*pos['qty'])
 def test_pnl_snapshot_idempotent(self):
  e.pnl(self.db);e.pnl(self.db);self.assertEqual(len(self.db.query('SELECT * FROM paper_pnl WHERE namespace=?',(e.NS,))),1)
 def test_stale_marks_labeled(self):
  e.fill(self.db,self.signal(),self.quote(),T);self.db.batch([('UPDATE paper_positions SET mark_at=? WHERE namespace=?',((T-timedelta(hours=1)).isoformat(),e.NS))]);self.assertFalse(e.pnl(self.db)['valuation_is_live'])
 def test_daily_scanner_bootstrap_queue_duplicate_and_fill(self):
  old_u=e.universe;old_read=e.read_history;old_indicator=e.indicator;old_eval=e.pf.evaluate;old_benchmark=e.pf.benchmark
  e.pf.evaluate=lambda *args:{"status":"PASS"};e.pf.benchmark=lambda *args:None
  def history(day):
   import pandas as pd
   dates=pd.bdate_range(end=day,periods=260).strftime('%Y-%m-%d');f=pd.DataFrame({'symbol':'RELIANCE-EQ','trade_date':dates,'open':100.,'high':101.,'low':99.,'close':100.,'volume':1})
   return f
  current=[history('2026-11-06')]
  e.universe=lambda:['RELIANCE-EQ'];e.read_history=lambda *args:current[0]
  def fake(i,g):
   f=g.copy();f['entry_condition']=True;f['exit_condition']=False;return f
  e.indicator=fake
  try:
   e.now=lambda:datetime(2026,11,7,10,tzinfo=e.IST);r=e.scan(self.db,None,bootstrap=True);self.assertEqual(r['signals_queued'],0)
   current[0]=history('2026-11-06');last=current[0].iloc[-1].copy();last['trade_date']='2026-11-08';import pandas as pd;current[0]=pd.concat([current[0],pd.DataFrame([last])],ignore_index=True)
   e.now=lambda:T;r=e.scan(self.db,None);self.assertEqual(r['signals_queued'],7);self.assertEqual(e.scan(self.db,None)['signals_queued'],0)
   self.seed_adv('RELIANCE-EQ',200000000)
   class Feed:
    def quotes(_,symbols):return {'RELIANCE-EQ':self.quote()}
   r=e.monitor(self.db,Feed());self.assertEqual(r['fills'],3);self.assertEqual(e.monitor(self.db,Feed())['fills'],0);self.assertEqual(len(self.db.positions()),3);self.assertEqual(len(self.db.query('SELECT * FROM paper_shadow_positions WHERE namespace=? AND qty>0',(e.NS,))),7)
  finally:e.universe=old_u;e.read_history=old_read;e.indicator=old_indicator;e.pf.evaluate=old_eval;e.pf.benchmark=old_benchmark;e.now=lambda:T
 def test_provider_error_rejected(self):
  q=self.quote();q['provider_status']='error';self.assertFalse(e.fill(self.db,self.signal(),q,T)['filled'])
 def test_quote_must_not_predate_signal_bar(self):
  s=self.signal(eligible='2026-11-11');self.assertFalse(e.fill(self.db,s,self.quote(),T)['filled'])
 def test_signal_tamper_rolls_back(self):
  s=self.signal();s['strategy']=2
  with self.assertRaises(RuntimeError):e.fill(self.db,s,self.quote(),T)
  self.assertEqual(len(self.db.positions()),0);self.assertEqual(len(self.db.query('SELECT * FROM paper_fills WHERE namespace=?',(e.NS,))),0)
 def test_old_position_exit_guard(self):
  e.fill(self.db,self.signal(),self.quote(),T);s=self.signal('SELL');self.db.batch([('UPDATE paper_signals SET reason=? WHERE namespace=? AND id=?',('daily_exit:2026-11-01T10:00:00+05:30',e.NS,s['id']))]);self.assertFalse(e.fill(self.db,s,self.quote(),T)['filled']);self.assertEqual(len(self.db.positions()),1)
 def test_frozen_indicators_match_originals(self):
  import ast,numpy as np,pandas as pd
  n=400;c=100+np.sin(np.arange(n)/9)*15+np.arange(n)*.02;g=pd.DataFrame({'open':c-.3,'high':c+1,'low':c-1,'close':c,'volume':100,'trade_date':pd.date_range('2025-01-01',periods=n).strftime('%Y-%m-%d')})
  for strategy,path in e.STRATEGY_SOURCES.items():
   source=Path('/workspace/strategies')/path;name='supertrend' if strategy==2 else 'indicators';fn=next(x for x in ast.parse(source.read_text()).body if isinstance(x,ast.FunctionDef) and x.name==name);ns={'np':np,'pd':pd};exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),ns);pd.testing.assert_frame_equal(e.indicator(strategy,g),ns[name](g))
  f=e.indicator(1,g);self.assertEqual(bool(f.entry_condition.iloc[-1]),g.close.iloc[-1]>g.high.iloc[-21:-1].max())
 def test_auth_retry_bounded_once(self):
  from unittest.mock import patch
  api=e.MarketData.__new__(e.MarketData);api.env={'FYERS_APP_ID':'fixture','FYERS_ACCESS_TOKEN':'fixture'};api.last=0;api.refreshed=False;count=[0]
  tmp=tempfile.NamedTemporaryFile(suffix='.json',delete=False);tmp.close();self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True));api.budget=e.RequestBudget(tmp.name)
  def refresh():
   if api.refreshed:raise RuntimeError('auth_failed_after_single_refresh')
   api.refreshed=True;count[0]+=1
  api.refresh=refresh
  class Response:
   status_code=401
   def json(self):return {'s':'error','code':-16}
  with patch.object(e.requests,'get',return_value=Response()) as req,patch.object(e.time,'sleep'):
   with self.assertRaises(RuntimeError):api.get('quotes',{'symbols':'NSE:RELIANCE-EQ'})
   self.assertEqual(count[0],1);self.assertEqual(req.call_count,2)
 def test_quote_batch_size(self):
  api=e.MarketData.__new__(e.MarketData);calls=[]
  def get(route,params):
   if route=='quotes':calls.append((route,(params.get('symbols') or '').split(',')));return {'s':'ok','d':[]}
   calls.append((route,params['symbol'].split(',')));return {'s':'ok','d':{}}
  api.get=get;q=api.quotes(['X'+str(i)+'-EQ' for i in range(101)]);self.assertEqual(calls[0][0],'quotes');self.assertEqual(len(calls[0][1]),50)
  self.assertEqual([r for r,_ in calls],['quotes','quotes','quotes']);self.assertEqual(sum(len(x) for _,x in calls),101);self.assertTrue(all(v['quote_at'] is None for v in q.values()))
 def test_rate_limit_fail_fast_does_not_retry_inside_get(self):
  from unittest.mock import patch
  api=e.MarketData.__new__(e.MarketData);api.env={'FYERS_APP_ID':'fixture','FYERS_ACCESS_TOKEN':'fixture'};api.last=0;api.refreshed=False
  tmp=tempfile.NamedTemporaryFile(suffix='.json',delete=False);tmp.close();self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True));api.budget=e.RequestBudget(tmp.name)
  class Response:
   status_code=429
   def json(self):return {'s':'error','code':-360}
  sleeps=[]
  with patch.object(e.requests,'get',return_value=Response()) as req,patch.object(e.time,'sleep',side_effect=lambda s:sleeps.append(s)):
   with self.assertRaisesRegex(RuntimeError,'market_data_rate_limit'):api.get('quotes',{'symbols':'NSE:RELIANCE-EQ'})
  self.assertEqual(req.call_count,1);self.assertFalse(any(s>=e.MD['rate_limit_backoff_seconds'] for s in sleeps))
 def test_real_order_routes_absent(self):
  self.assertEqual(set(e.AUTH_ROUTES),{'quotes','history','depth'});self.assertTrue(all('/data/' in u for u in e.AUTH_ROUTES.values()));self.assertTrue(e.CFG['paper_only']);self.assertEqual(e.cap.POLICY['target_notional_inr'],50000)
 def test_corporate_actions_scale_state_credit_cash_and_leave_rights(self):
  import corporate_actions as ca
  self.assertEqual(ca.scale_paise(10000,3,2),6667);self.assertEqual(ca.scale_paise(20000,2,1),10000)
  at=T.isoformat();day='2026-11-09'
  def pos(strategy,symbol,qty,price):return ('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(e.NS,strategy,symbol,qty,price,at,price,price,at))
  def action(symbol,kind,num,den,cash):return ('INSERT INTO paper_corporate_actions(symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at) VALUES(?,?,?,?,?,?,?,?)',(symbol,day,kind,num,den,cash,'TEST',at))
  self.db.batch([
   pos(1,'RELIANCE-EQ',5,100),pos(1,'TCS-EQ',10,200),pos(1,'INFY-EQ',4,100),pos(1,'HDFCBANK-EQ',4,200),pos(1,'ICICIBANK-EQ',3,50),pos(1,'SBIN-EQ',1,90),pos(1,'WIPRO-EQ',2,80),
   ('INSERT INTO paper_indicator_state(namespace,strategy,symbol,upperband,lowerband,prev_close,updated_at) VALUES(?,?,?,?,?,?,?)',(e.NS,1,'RELIANCE-EQ',90,70,80,at)),
   action('RELIANCE-EQ','SPLIT',3,2,None),action('TCS-EQ','SPLIT',2,1,None),action('INFY-EQ','CASH_DIVIDEND',None,None,250),action('HDFCBANK-EQ','EXTRAORDINARY_DIVIDEND',2,1,100),action('ICICIBANK-EQ','RIGHTS',None,None,None),action('SBIN-EQ','BONUS',1,3,None),action('WIPRO-EQ','BONUS',None,None,None)])
  ca.apply(e,self.db,day)
  rel=self.db.query('SELECT qty,entry_price,high_water,mark_price FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'RELIANCE-EQ'))[0]
  self.assertEqual(rel['qty'],7);self.assertEqual(round(rel['entry_price'],2),66.67);self.assertEqual(round(rel['high_water'],2),66.67);self.assertEqual(round(rel['mark_price'],2),66.67)
  bands=self.db.query('SELECT upperband,lowerband,prev_close FROM paper_indicator_state WHERE namespace=? AND symbol=?',(e.NS,'RELIANCE-EQ'))[0]
  self.assertEqual(round(bands['upperband'],2),60.00);self.assertEqual(round(bands['lowerband'],2),46.67);self.assertEqual(round(bands['prev_close'],2),53.33)
  self.assertFalse(self.db.query("SELECT 1 FROM paper_cash_credits WHERE namespace=? AND kind='FRACTIONAL_OPEN'",(e.NS,)))
  scale=self.db.query('SELECT old_qty,new_qty,fraction_num,fraction_den FROM paper_state_scales WHERE namespace=? AND symbol=? AND kind=?',(e.NS,'RELIANCE-EQ','SPLIT'))[0]
  self.assertEqual((scale['old_qty'],scale['new_qty'],scale['fraction_num'],scale['fraction_den']),(5,7,1,2))
  whole=self.db.query('SELECT qty,entry_price FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'TCS-EQ'))[0]
  self.assertEqual(whole['qty'],20);self.assertEqual(round(whole['entry_price'],2),100.00)
  self.assertEqual(self.db.query('SELECT fraction_num FROM paper_state_scales WHERE namespace=? AND symbol=?',(e.NS,'TCS-EQ'))[0]['fraction_num'],0)
  ca.apply(e,self.db,day,{'RELIANCE-EQ':80});ca.apply(e,self.db,day,{'RELIANCE-EQ':80})
  credits=self.db.query('SELECT kind,amount_paise,symbol FROM paper_cash_credits WHERE namespace=?',(e.NS,))
  by={(c['symbol'],c['kind']):c['amount_paise'] for c in credits}
  self.assertEqual(by[('RELIANCE-EQ','FRACTIONAL_OPEN')],4000)
  self.assertEqual(by[('INFY-EQ','CASH_DIVIDEND')],1000)
  self.assertEqual(by[('HDFCBANK-EQ','EXTRAORDINARY_DIVIDEND')],400)
  self.assertEqual(len([c for c in credits if c['kind']=='FRACTIONAL_OPEN']),1)
  infy=self.db.query('SELECT qty,entry_price FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'INFY-EQ'))[0]
  self.assertEqual((infy['qty'],infy['entry_price']),(4,100))
  extra=self.db.query('SELECT qty,entry_price FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'HDFCBANK-EQ'))[0]
  self.assertEqual(extra['qty'],8);self.assertEqual(round(extra['entry_price'],2),100.00)
  rights=self.db.query('SELECT qty,entry_price FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'ICICIBANK-EQ'))[0]
  self.assertEqual((rights['qty'],rights['entry_price']),(3,50))
  self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'SBIN-EQ'))[0]['qty'],1)
  self.assertEqual(self.db.query('SELECT qty FROM paper_positions WHERE namespace=? AND symbol=?',(e.NS,'WIPRO-EQ'))[0]['qty'],2)
  self.assertEqual(len(self.db.query('SELECT 1 FROM paper_manual_reviews WHERE namespace=? AND symbol IN (?,?,?)',(e.NS,'ICICIBANK-EQ','SBIN-EQ','WIPRO-EQ'))),3)
  causes={row['cause'] for row in self.db.query('SELECT cause FROM paper_decisions WHERE namespace=?',(e.NS,))}
  self.assertEqual(causes,{'STATE_SCALING','CASH_CREDITED','CORP_ACTION_MANUAL_REVIEW'})
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
  self.db.batch([('INSERT INTO paper_fill_costs(namespace,fill_id,day,strategy,symbol,isin,side,turnover_paise,policy_version,components_json,total_paise,entry_at,gross_cashflow_paise,net_cashflow_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(e.NS,900004,'2026-11-06',2,'AXISBANK-EQ','INE238A01034','BUY',49950000,'FYERS_STANDARD_NSE_DELIVERY_ESTIMATE_2026_11_07_v1','{}',0,'2026-11-06T10:00:00+05:30',-49950000,-49950000))])
  blocked=self.signal(symbol='AXISBANK-EQ',strategy=2);missed=e.fill(self.db,blocked,{'symbol':'AXISBANK-EQ','price':100,'quote_at':at},T)
  self.assertEqual(missed['reason'],'INSUFFICIENT_CASH')
  self.db.batch([('INSERT INTO paper_cash_credits(namespace,strategy,symbol,ex_date,kind,amount_paise,recorded_at) VALUES(?,?,?,?,?,?,?)',(e.NS,2,'AXISBANK-EQ',day,'CASH_DIVIDEND',10000000,at))])
  funded=e.fill(self.db,blocked,{'symbol':'AXISBANK-EQ','price':100,'quote_at':at},T)
  self.assertTrue(funded['filled']);self.assertEqual(funded['qty'],500)
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_pause_blocks_entries_keeps_exits_and_reinstates_the_system(self):
  import pauses as pz
  bought=e.fill(self.db,self.signal(),self.quote(),T);self.assertTrue(bought['filled'])
  self.assertEqual(self.db.query('SELECT qty FROM paper_shadow_positions WHERE namespace=? AND strategy=1 AND symbol=?',(e.NS,'RELIANCE-EQ'))[0]['qty'],500)
  at=T.isoformat()
  self.db.batch([('INSERT INTO paper_pause_state(namespace,scope,strategy,paused,cause,peak_paise,pre_pause_peak_paise,baseline_day,paused_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(e.NS,'strategy',1,1,'sleeve_drawdown_exceeds_10pct',50000000,50000000,'',at,at))])
  sold=e.fill(self.db,self.signal('SELL'),self.quote(110),T);self.assertTrue(sold['filled'])
  self.assertFalse(self.db.query('SELECT 1 FROM paper_shadow_positions WHERE namespace=? AND strategy=1 AND symbol=? AND qty>0',(e.NS,'RELIANCE-EQ')))
  self.assertEqual(len(self.db.query("SELECT 1 FROM paper_closed_trades WHERE namespace=? AND book='capped' AND strategy=1",(e.NS,))),1)
  self.assertEqual(len(self.db.query("SELECT 1 FROM paper_closed_trades WHERE namespace=? AND book='shadow' AND strategy=1",(e.NS,))),1)
  skipped=self.signal(symbol='TCS-EQ');blocked=e.fill(self.db,skipped,{'symbol':'TCS-EQ','price':100,'quote_at':at},T)
  self.assertEqual(blocked['reason'],'KILL_PAUSE');self.assertTrue(blocked['shadow_recorded']);self.assertEqual(blocked['pause_cause'],'sleeve_drawdown_exceeds_10pct')
  self.assertFalse(self.db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND strategy=1 AND symbol=? AND qty>0',(e.NS,'TCS-EQ')))
  self.assertEqual(self.db.query('SELECT qty FROM paper_shadow_positions WHERE namespace=? AND strategy=1 AND symbol=?',(e.NS,'TCS-EQ'))[0]['qty'],500)
  self.assertEqual(self.db.query('SELECT status FROM paper_signals WHERE namespace=? AND id=?',(e.NS,skipped['id']))[0]['status'],'pending')
  self.assertTrue(e.fill(self.db,self.signal(symbol='TCS-EQ',strategy=2),{'symbol':'TCS-EQ','price':100,'quote_at':at},T)['filled'])
  self.db.batch([('INSERT INTO paper_shadow_positions(namespace,strategy,symbol,qty,entry_price,entry_at,updated_at) VALUES(?,?,?,?,?,?,?)',(e.NS,2,'INFY-EQ',10,100,at,at))])
  shadow=e.fill(self.db,self.signal('SELL',symbol='INFY-EQ',strategy=2),{'symbol':'INFY-EQ','price':110,'quote_at':at},T)
  self.assertTrue(shadow['shadow_closed']);self.assertFalse(self.db.query('SELECT 1 FROM paper_positions WHERE namespace=? AND strategy=2 AND symbol=? AND qty>0',(e.NS,'INFY-EQ')))
  self.assertFalse(self.db.query('SELECT 1 FROM paper_shadow_positions WHERE namespace=? AND strategy=2 AND symbol=? AND qty>0',(e.NS,'INFY-EQ')))
  self.db.batch([
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'shadow',4,'2026-11-05',50000000)),
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'shadow',4,'2026-11-06',44000000)),
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'shadow',6,'2026-11-05',50000000)),
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'shadow',6,'2026-11-06',45000000)),
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'capped',0,'2026-11-05',100000000)),
   ('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'capped',0,'2026-11-06',90000000))])
  self.assertEqual(pz.evaluate(e,self.db,'2026-11-09',force=True)['status'],'evaluated')
  self.assertEqual(self.db.query("SELECT cause FROM paper_pause_state WHERE namespace=? AND scope='strategy' AND strategy=4",(e.NS,))[0]['cause'],'sleeve_drawdown_exceeds_10pct')
  self.assertFalse(self.db.query("SELECT 1 FROM paper_pause_state WHERE namespace=? AND scope='strategy' AND strategy=6 AND paused=1",(e.NS,)))
  self.assertEqual(self.db.query("SELECT cause FROM paper_pause_state WHERE namespace=? AND scope='system' AND strategy=0",(e.NS,))[0]['cause'],'system_drawdown_exceeds_8pct')
  system=e.fill(self.db,self.signal(symbol='INFY-EQ',strategy=2),{'symbol':'INFY-EQ','price':100,'quote_at':at},T)
  self.assertEqual(system['reason'],'KILL_PAUSE');self.assertEqual(system['pause_cause'],'system_drawdown_exceeds_8pct')
  self.db.batch([('INSERT INTO paper_equity_closes(namespace,book,strategy,day,equity_paise) VALUES(?,?,?,?,?)',(e.NS,'capped',0,'2026-11-12',97000000))])
  pz.evaluate(e,self.db,'2026-11-12',force=True)
  restored=self.db.query("SELECT paused,peak_paise,baseline_day FROM paper_pause_state WHERE namespace=? AND scope='system' AND strategy=0",(e.NS,))[0]
  self.assertEqual(restored['paused'],0);self.assertEqual(restored['peak_paise'],97000000);self.assertEqual(restored['baseline_day'],'2026-11-12')
  self.assertEqual(self.db.query("SELECT paused FROM paper_pause_state WHERE namespace=? AND scope='strategy' AND strategy=4",(e.NS,))[0]['paused'],1)
  self.db.batch([('INSERT INTO paper_pause_overrides(namespace,scope,strategy,action,recorded_at) VALUES(?,?,?,?,?)',(e.NS,'strategy',4,'reinstate','2026-11-12T11:00:00+05:30'))])
  pz.evaluate(e,self.db,'2026-11-12',force=True)
  self.assertEqual(self.db.query("SELECT paused,baseline_day FROM paper_pause_state WHERE namespace=? AND scope='strategy' AND strategy=4",(e.NS,))[0]['paused'],0)
  self.assertTrue(self.db.call({'action':'verify_decisions','namespace':e.NS})['ok'])
 def test_group_i_funnel_overlap_muhurat_and_capped_headline_pnl(self):
  import pandas as pd
  self.assertTrue(e.is_open(datetime(2026,11,8,14,30,tzinfo=e.IST)))
  dates=pd.bdate_range(end='2026-11-06',periods=255).strftime('%Y-%m-%d').tolist()+['2026-11-09']
  base=pd.DataFrame({'symbol':'RELIANCE-EQ','trade_date':dates,'open':100.,'high':102.,'low':99.,'close':101.,'volume':1000.})
  muhurat_row=pd.DataFrame([{'symbol':'RELIANCE-EQ','trade_date':'2026-11-08','open':9999.,'high':9999.,'low':1.,'close':9999.,'volume':999999.}])
  with_muhurat=pd.concat([base.iloc[:-1],muhurat_row,base.iloc[-1:]],ignore_index=True)
  for strategy in range(1,8):
   f_clean=e.indicator(strategy,base).reset_index(drop=True)
   f_muh=e.indicator(strategy,with_muhurat)
   m_row=f_muh[f_muh.trade_date=='2026-11-08'].iloc[0]
   self.assertFalse(bool(m_row.entry_condition));self.assertFalse(bool(m_row.exit_condition))
   f_after=f_muh[f_muh.trade_date!='2026-11-08'].reset_index(drop=True)
   pd.testing.assert_frame_equal(f_clean,f_after)
  pf_clean=e.pf.evaluate(base,base,'2026-11-09');pf_muh=e.pf.evaluate(with_muhurat,with_muhurat,'2026-11-09')
  self.assertEqual(pf_clean,pf_muh)
  bar='2026-11-08';at=T.isoformat()
  self.seed_adv('RELIANCE-EQ',200000000);self.seed_adv('TCS-EQ',50000000)
  stm=[('INSERT OR REPLACE INTO paper_scan_batches(namespace,bar_date,scanned_at,status,report_json) VALUES(?,?,?,?,?)',(e.NS,bar,at,'ok','{}')),
       ('INSERT OR REPLACE INTO paper_prefilter_states(namespace,bar_date,symbol,policy_version,status,metrics_json,observed_at) VALUES(?,?,?,?,?,?,?)',(e.NS,bar,'RELIANCE-EQ',e.pf.POLICY['version'],'PASS','{}',at)),
       ('INSERT OR REPLACE INTO paper_prefilter_states(namespace,bar_date,symbol,policy_version,status,metrics_json,observed_at) VALUES(?,?,?,?,?,?,?)',(e.NS,bar,'TCS-EQ',e.pf.POLICY['version'],'PASS','{}',at))]
  chk=e.js({'input_policy':{'version':e.si.POLICY['version']}})
  for s in (1,2,3,4):stm.append(('INSERT OR REPLACE INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,bar,s,'RELIANCE-EQ',1,chk,'{}',at)))
  for s in (1,2):stm.append(('INSERT OR REPLACE INTO paper_qualifier_states(namespace,bar_date,strategy,symbol,qualified,checklist_json,plan_json,scanned_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,bar,s,'TCS-EQ',1,chk,'{}',at)))
  self.db.batch(stm)
  ov=e.au.overlap(e,self.db,bar)
  self.assertEqual(ov['bucket_counts'],{'single':0,'exactly_2':1,'exactly_3':0,'4_plus':1})
  self.assertEqual(ov['matrix']['1']['2'],2);self.assertEqual(ov['matrix']['1']['4'],1);self.assertEqual(ov['isin_cap_impact_symbols'],['RELIANCE-EQ'])
  fn=e.au.funnel(e,self.db,bar)
  self.assertEqual(fn['adv_floor_review'],'reviewed');self.assertEqual(e.cap.POLICY['adv_floor_review'],'reviewed')
  self.assertEqual(fn['stages']['strategy_qualified'],6);self.assertEqual(fn['stages']['adv_floor_pass'],4);self.assertEqual(fn['stages']['adv_floor_skipped'],2)
  self.assertEqual(fn['stages']['isin_cap_pass'],3);self.assertEqual(fn['stages']['isin_cap_skipped'],1);self.assertEqual(fn['stages']['sleeve_slot_pass'],3)
  self.assertTrue(e.fill(self.db,self.signal(symbol='RELIANCE-EQ',strategy=1),self.quote(100),T)['filled'])
  self.db.batch([('INSERT INTO paper_shadow_positions(namespace,strategy,symbol,qty,entry_price,entry_at,updated_at) VALUES(?,?,?,?,?,?,?)',(e.NS,5,'INFY-EQ',500,100,at,at)),
                 ('INSERT INTO paper_shadow_costs(namespace,signal_id,strategy,symbol,side,qty,price,total_paise,net_cashflow_paise,filled_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(e.NS,'sh1',5,'INFY-EQ','BUY',500,100,1000,-5001000,at))])
  p=e.pnl(self.db)
  self.assertEqual(p['headline_pnl_source'],'capped_master_ledger')
  self.assertIsNotNone(p['costs_INR']);self.assertGreater(p['costs_INR'],0)
  self.assertIsNotNone(p['capital_return_pct'])
  self.assertEqual(p['capped_master_ledger']['master_capital_INR'],3500000)
  self.assertEqual(p['headline_net_pnl_paise'],p['estimated_finance']['net_total_accrued_paise'])
  self.assertFalse(p['capped_master_ledger']['shadow_diagnostic']['headline'])
  self.assertTrue(p['public_labels']['historical_figures_gross']);self.assertTrue(p['public_labels']['survivorship_biased']);self.assertTrue(p['public_labels']['not_jointly_tested'])
if __name__=='__main__':unittest.main(verbosity=2)
