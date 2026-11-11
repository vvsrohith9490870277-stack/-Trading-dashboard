import sys,unittest
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import engine as e
T=datetime(2026,11,9,12,32,tzinfo=e.IST)
MIDNIGHT_TT=1794182400  # 2026-11-09 00:00 UTC == 05:30 IST: what /data/quotes returns as `tt`
class DepthQuoteFreshness(unittest.TestCase):
 def api(self,payloads,quotes_rows=None,quotes_error=None,quotes_first=True):
  api=e.MarketData.__new__(e.MarketData);calls=[]
  prev=e.MD.get('quotes_batch_first');e.MD['quotes_batch_first']=quotes_first
  self.addCleanup(lambda: e.MD.__setitem__('quotes_batch_first',prev))
  def get(route,params):
   calls.append((route,params))
   if route=='quotes':
    if quotes_error:raise quotes_error
    if quotes_rows is not None:return {'s':'ok','d':quotes_rows}
    return {'s':'ok','d':[]}
   item=payloads[params['symbol']]
   if isinstance(item,Exception):raise item
   return {'s':'ok','d':{params['symbol']:item}}
  api.get=get;api.calls=calls;api._restore=lambda: e.MD.__setitem__('quotes_batch_first',prev);return api
 def q(self,ltt,price=1307.7,sym='RELIANCE-EQ'):
  return self.api({'NSE:'+sym:{'ltp':price,'ltt':ltt,'v':100}},quotes_first=False).quotes([sym])[sym]
 def test_midnight_tt_style_stamp_rejected(self):
  q=self.q(MIDNIGHT_TT);self.assertEqual(q['quote_at'],'2026-11-09T05:30:00+05:30');self.assertEqual(e.valid_quote(q,T),(False,'stale_or_future_quote'))
 def test_fresh_ltt_accepted(self):
  q=self.q(int((T-timedelta(seconds=5)).timestamp()));self.assertEqual(q['price'],1307.7);self.assertEqual(q['source'],'FYERS_DEPTH_LTT');self.assertTrue(e.valid_quote(q,T)[0])
 def test_stale_ltt_rejected_60s_rule_unchanged(self):
  self.assertEqual(e.CFG['quote_max_age_seconds'],60)
  self.assertTrue(e.valid_quote(self.q(int((T-timedelta(seconds=60)).timestamp())),T)[0])
  self.assertEqual(e.valid_quote(self.q(int((T-timedelta(seconds=61)).timestamp())),T),(False,'stale_or_future_quote'))
 def test_missing_or_invalid_ltt_fails_closed(self):
  for bad in [None,'',0,-5,'abc',float('nan'),True,'1794207721x']:
   q=self.q(bad);self.assertIsNone(q['quote_at']);self.assertEqual(e.valid_quote(q,T),(False,'missing_exchange_timestamp'))
  q=self.api({'NSE:RELIANCE-EQ':{'ltp':1307.7}},quotes_first=False).quotes(['RELIANCE-EQ'])['RELIANCE-EQ'];self.assertEqual(e.valid_quote(q,T),(False,'missing_exchange_timestamp'))
 def test_future_ltt_rejected(self):
  self.assertEqual(e.valid_quote(self.q(int((T+timedelta(seconds=30)).timestamp())),T),(False,'stale_or_future_quote'))
 def test_missing_symbol_and_provider_error_fail_closed(self):
  api=self.api({'NSE:A-EQ':RuntimeError('market_data_error_-50')},quotes_first=False);q=api.quotes(['A-EQ'])['A-EQ'];self.assertEqual(q['provider_status'],'error');self.assertFalse(e.valid_quote(q,T)[0])
  prev=e.MD.get('quotes_batch_first');e.MD['quotes_batch_first']=False
  api=e.MarketData.__new__(e.MarketData);api.get=lambda r,p:{'s':'ok','d':{}};q=api.quotes(['B-EQ'])['B-EQ'];e.MD['quotes_batch_first']=prev;self.assertEqual(q['provider_status'],'missing');self.assertFalse(e.valid_quote(q,T)[0])
 def test_transport_and_auth_errors_still_raise(self):
  for err in ['market_data_transport_failed','auth_circuit_open_30min','market_data_rate_limit']:
   with self.assertRaisesRegex(RuntimeError,err):self.api({},quotes_error=RuntimeError(err)).quotes(['A-EQ'])
 def test_one_symbol_per_depth_request(self):
  api=self.api({'NSE:A-EQ':{'ltp':1,'ltt':1},'NSE:B-EQ':{'ltp':2,'ltt':2}},quotes_first=False);api.quotes(['A-EQ','B-EQ'])
  self.assertEqual([(r,p.get('symbol') or p.get('symbols')) for r,p in api.calls],[('depth','NSE:A-EQ'),('depth','NSE:B-EQ')])
 def test_exit_path_uses_same_gate(self):
  # Strategy-7 stop and all SELL fills call valid_quote on the same quote dict, so stale exits are blocked too.
  stale=self.q(MIDNIGHT_TT);self.assertFalse(e.valid_quote(stale,T)[0])
 def test_depth_feeds_upgrade_open_and_circuit_paths(self):
  stamp=int((T-timedelta(seconds=3)).timestamp())
  row={'ltp':105.0,'ltt':stamp,'o':100.0,'h':106.0,'l':99.0,'upper_ckt':110.0,'lower_ckt':90.0,'bids':[{'price':104.9,'volume':10,'ord':1}],'ask':[{'price':105.1,'volume':5,'ord':1}]}
  q=self.api({'NSE:A-EQ':row},quotes_first=False).quotes(['A-EQ'])['A-EQ']
  self.assertEqual((q['open'],q['high'],q['low'],q['bid'],q['ask'],q['upper_band'],q['lower_band']),(100.0,106.0,99.0,104.9,105.1,110.0,90.0))
  self.assertTrue(e.valid_quote(q,T)[0]);self.assertEqual(e.execution_reference(q),(100.0,'FEED_OPEN'))
  self.assertEqual(e.market_guard(q)['circuit'],'NONE');self.assertFalse(e.market_guard(q)['lock_unknown'])
 def test_depth_empty_ask_side_reads_upper_lock(self):
  stamp=int((T-timedelta(seconds=3)).timestamp())
  row={'ltp':110.0,'ltt':stamp,'o':101.0,'h':110.0,'l':100.0,'upper_ckt':110.0,'lower_ckt':90.0,'bids':[{'price':110.0,'volume':900,'ord':9}],'ask':[{'price':0,'volume':0,'ord':0}]}
  q=self.api({'NSE:A-EQ':row},quotes_first=False).quotes(['A-EQ'])['A-EQ'];self.assertEqual(e.market_guard(q)['circuit'],'UPPER')
  row.update(bids=[{'price':0,'volume':0,'ord':0}],ask=[{'price':90.0,'volume':900,'ord':9}],ltp=90.0)
  q=self.api({'NSE:A-EQ':row},quotes_first=False).quotes(['A-EQ'])['A-EQ'];self.assertEqual(e.market_guard(q)['circuit'],'LOWER')
 def test_depth_missing_ohlc_and_book_are_none_not_zero_price(self):
  q=self.api({'NSE:A-EQ':{'ltp':50.0,'ltt':int(T.timestamp()),'o':0,'bids':[],'upper_ckt':0}},quotes_first=False).quotes(['A-EQ'])['A-EQ']
  self.assertIsNone(q['open']);self.assertIsNone(q['bid']);self.assertIsNone(q['ask']);self.assertNotIn('upper_band',q)
  self.assertEqual(e.execution_reference(q),(50.0,'LTP_LEGACY'))
 def test_quotes_batch_with_ltt_skips_depth(self):
  stamp=int((T-timedelta(seconds=5)).timestamp())
  rows=[{'n':'NSE:RELIANCE-EQ','s':'ok','v':{'lp':1307.7,'ltt':stamp,'open_price':1300,'high_price':1310,'low_price':1290,'ask':1307.8,'bid':1307.6,'tt':MIDNIGHT_TT}}]
  api=self.api({},quotes_rows=rows);q=api.quotes(['RELIANCE-EQ'])['RELIANCE-EQ']
  self.assertEqual([r for r,_ in api.calls],['quotes']);self.assertEqual(q['source'],'FYERS_QUOTES');self.assertEqual(q['price'],1307.7);self.assertTrue(e.valid_quote(q,T)[0])
 def test_quotes_midnight_tt_is_not_used_as_trade_time(self):
  rows=[{'n':'NSE:RELIANCE-EQ','s':'ok','v':{'lp':1307.7,'tt':MIDNIGHT_TT,'open_price':1300}}]
  self.assertIsNone(e.quote_trade_stamp(rows[0]['v']))
  api=self.api({},quotes_rows=rows)
  q=api.quotes(['RELIANCE-EQ'])['RELIANCE-EQ']
  self.assertEqual([r for r,_ in api.calls],['quotes']);self.assertEqual(q['source'],'FYERS_QUOTES');self.assertNotEqual(q.get('quote_at'),'2026-11-09T05:30:00+05:30')
  self.assertEqual(q['open'],1300)
 def test_depth_only_when_official_open_missing(self):
  stamp=int((T-timedelta(seconds=5)).timestamp())
  rows=[{'n':'NSE:A-EQ','s':'ok','v':{'lp':105.0,'tt':MIDNIGHT_TT}}]
  api=self.api({'NSE:A-EQ':{'ltp':105.0,'ltt':stamp,'o':100.0}},quotes_rows=rows)
  q=api.quotes(['A-EQ'])['A-EQ']
  self.assertEqual([r for r,_ in api.calls],['quotes','depth']);self.assertEqual(q['open'],100.0)
 def test_quotes_observation_stamp_during_open_session(self):
  rows=[{'n':'NSE:RELIANCE-EQ','s':'ok','v':{'lp':1307.7,'open_price':1300,'tt':MIDNIGHT_TT}}]
  api=self.api({},quotes_rows=rows)
  # Freeze 'now' inside quotes() to the fixture session time.
  import engine as eng
  real_now=eng.now;eng.now=lambda:T
  try:q=api.quotes(['RELIANCE-EQ'])['RELIANCE-EQ']
  finally:eng.now=real_now
  self.assertEqual(q['freshness'],'session_observation');self.assertEqual(q['quote_at'],T.isoformat());self.assertTrue(e.valid_quote(q,T)[0]);self.assertEqual([r for r,_ in api.calls],['quotes'])
 def test_quotes_rate_limit_does_not_start_depth_batch(self):
  api=self.api({'NSE:A-EQ':{'ltp':1,'ltt':1}},quotes_error=RuntimeError('market_data_rate_limit'))
  with self.assertRaisesRegex(RuntimeError,'market_data_rate_limit'):api.quotes(['A-EQ'])
  self.assertEqual([r for r,_ in api.calls],['quotes'])
if __name__=='__main__':unittest.main(verbosity=2)
