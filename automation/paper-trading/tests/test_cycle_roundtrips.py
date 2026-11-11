"""Worker cycle round-trips: marks loop skips unneeded band reads, pnl batches prior closes, heartbeat failures are logged."""
import unittest,time,uuid
from datetime import timedelta
from unittest.mock import patch
import test_engine as fixtures
import engine as e
import automation as a
T=fixtures.T
class Feed:
 def __init__(self,quotes):self.q=quotes
 def quotes(self,symbols):return {s:dict(self.q[s]) for s in symbols if s in self.q}
class CycleRoundTripTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp;seed_adv=fixtures.PaperTests.seed_adv;signal=fixtures.PaperTests.signal;quote=fixtures.PaperTests.quote
 def open_position(self,price=100):
  self.assertTrue(e.fill(self.db,self.signal(),self.quote(price),T)['filled'])
 def test_marks_loop_skips_band_read_and_extra_positions_read_when_no_action(self):
  self.open_position()
  q={'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':104.5,'quote_at':T.isoformat(),'bid':104.4,'ask':104.6}}
  real_band,real_pos=e.band_for,e.DB.positions;bands=[];reads=[]
  with patch.object(e,'band_for',side_effect=lambda *x:bands.append(x) or real_band(*x)),patch.object(e.DB,'positions',autospec=True,side_effect=lambda db:reads.append(1) or real_pos(db)):
   r=e.monitor(self.db,Feed(q))
  self.assertEqual(r['status'],'ok');self.assertEqual(r['fills'],0);self.assertEqual(bands,[]);self.assertEqual(len(reads),2)
  p=self.db.positions()[0];self.assertEqual(p['mark_price'],104.5);self.assertEqual(p['mark_at'],T.isoformat())
 def test_carried_exit_still_reads_band_and_requeries_positions(self):
  self.open_position()
  self.db.batch([('UPDATE paper_positions SET pending_exit=1 WHERE namespace=?',(e.NS,))])
  q={'RELIANCE-EQ':{'symbol':'RELIANCE-EQ','price':90.0,'quote_at':T.isoformat(),'bid':0,'ask':90.0}}
  real_band,real_pos=e.band_for,e.DB.positions;bands=[];reads=[]
  with patch.object(e,'band_for',side_effect=lambda *x:bands.append(x[1]) or real_band(*x)),patch.object(e.DB,'positions',autospec=True,side_effect=lambda db:reads.append(1) or real_pos(db)):
   e.monitor(self.db,Feed(q))
  self.assertIn('RELIANCE-EQ',bands);self.assertEqual(len(reads),3)
 def seed_closes(self,rows):
  self.db.batch([('INSERT INTO daily_ohlcv(symbol,trade_date,close) VALUES(?,?,?)',r) for r in rows])
 def test_prior_closes_matches_per_symbol_lookup(self):
  tag=uuid.uuid4().hex[:6].upper();A,B,C,D,E2=[f'{x}{tag}-EQ' for x in 'ABCDE'];day='2026-11-09'
  self.seed_closes([(A,'2026-11-05',10.),(A,'2026-11-06',11.),(A,'2026-11-07',0.),(B,'2026-11-09',5.),(B,'2026-11-10',6.),(C,'2026-11-06',7.),(C,'2026-11-09',8.),(E2,'2026-11-04',3.)])
  syms=[A,B,C,D,E2];got=e.prior_closes(self.db,syms,day,chunk=2)
  for s in syms:
   want=self.db.query('SELECT trade_date,close FROM daily_ohlcv WHERE symbol=? AND trade_date<? AND close>0 ORDER BY trade_date DESC LIMIT 1',(s,day))
   self.assertEqual(got[s],want,s)
  self.assertEqual(got[A],[{'trade_date':'2026-11-06','close':11.}]);self.assertEqual(got[B],[]);self.assertEqual(got[D],[])
 def test_pnl_reads_prior_closes_in_one_query(self):
  self.open_position()
  e.now=lambda:T+timedelta(days=1)
  real=e.DB.query;seen=[]
  with patch.object(e.DB,'query',autospec=True,side_effect=lambda db,sql,args=():seen.append(sql) or real(db,sql,args)):r=e.pnl(self.db)
  self.assertEqual(sum('daily_ohlcv' in s for s in seen if 'close>0' in s),1);self.assertIn('daily_mark_to_market_pnl_INR',r)
 def test_heartbeat_write_failure_is_logged_not_silent(self):
  events=[];logs=[]
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.02,'worker_cycle_max_seconds':60}),patch.object(a,'heartbeat',side_effect=RuntimeError('Turso_operation_failed')),patch.object(a.al,'emit',side_effect=lambda e_,db,kind,**d:events.append((kind,d))),patch.object(e,'log',side_effect=lambda kind,**d:logs.append(kind)):
   with a.CycleHeartbeat(e,self.db,1) as hb:time.sleep(0.15)
  kinds={k for k,_ in events};self.assertIn('worker_heartbeat_failed',kinds);self.assertIn('worker_heartbeat_failed',logs)
  d=next(d for k,d in events if k=='worker_heartbeat_failed');self.assertEqual(d['error_code'],'Turso_operation_failed');self.assertEqual(d['severity'],'WARN');self.assertGreaterEqual(hb.failures,1)
 def test_slow_heartbeat_write_is_logged(self):
  events=[]
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.02,'worker_cycle_max_seconds':60}),patch.object(a,'heartbeat',side_effect=lambda *x,**d:time.sleep(0.05)),patch.object(a.al,'emit',side_effect=lambda e_,db,kind,**d:events.append(kind)),patch.object(e,'log'):
   with a.CycleHeartbeat(e,self.db,1):time.sleep(0.2)
  self.assertIn('worker_heartbeat_slow',events)
 def test_heartbeat_failure_event_lands_in_audit_table(self):
  with patch.dict(e.CFG,{'worker_heartbeat_interval_seconds':0.02,'worker_cycle_max_seconds':60}),patch.object(a,'heartbeat',side_effect=RuntimeError('Turso_operation_failed')),patch.object(e,'log'):
   with a.CycleHeartbeat(e,self.db,1):time.sleep(0.1)
  rows=self.db.query("SELECT severity,outcome FROM paper_events WHERE namespace=? AND event_type='worker_heartbeat_failed'",(e.NS,))
  self.assertTrue(rows);self.assertEqual(rows[0]['severity'],'WARN');self.assertEqual(rows[0]['outcome'],'failed')
if __name__=='__main__':unittest.main()
