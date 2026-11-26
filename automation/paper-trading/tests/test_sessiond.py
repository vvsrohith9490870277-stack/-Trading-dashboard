import unittest,sys
from pathlib import Path
from unittest.mock import patch
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import engine as e;import sessiond as sd
import test_engine as fixtures

class _DoneProc:
 """A spawned child that has already exited, so tick() treats it as not running."""
 def poll(self):return 0

class _RunRecorder:
 """Stand-in for sessiond.spawn: records the engine command, starts nothing."""
 def __init__(self):self.calls=[]
 def __call__(self,eng,args):
  self.calls.append(list(args));return _DoneProc()

class SessiondRetryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.PaperTests.setUpClass.__func__(cls)
 @classmethod
 def tearDownClass(cls):fixtures.PaperTests.tearDownClass.__func__(cls)
 setUp=fixtures.PaperTests.setUp

 def _due_and_tick(self,state,run,hour,minute,mono):
  # The guardian is unrelated to the scan retry; keep it out of the way.
  t=fixtures.T.replace(hour=hour,minute=minute)
  with patch.object(sd.gd,'armable',return_value=False):
   return sd.due(e,self.db,t),sd.tick(e,self.db,state,t,mono,run=run)

 def test_failed_scan_retries_past_three_attempts(self):
  # I23: a failed 08:45 scan used to retry at most 3 times (08:45, 09:00,
  # 09:15) and then give up for the day. Retries must continue until EOD.
  # No scan batch exists, so the scan stays due; drive EVERY 15-minute
  # retry slot from 08:45 to 16:15 (31 ticks) and assert a scan starts on
  # each one. Ticks 4 and 5 are past the old limit of 3, and the last
  # ticks prove retries really run until EOD, not just past 3. Wall times
  # past 09:30 also make the unrelated exit-selftest due; it is filtered
  # out of the assertions (only scan starts are counted).
  state=sd.State();run=_RunRecorder()
  from datetime import timedelta as _td
  slots=[(fixtures.T.replace(hour=8,minute=45)+_td(minutes=15*i)) for i in range(31)]
  for i,t in enumerate(slots):
   with patch.object(sd.gd,'armable',return_value=False):
    due=sd.due(e,self.db,t);acted=sd.tick(e,self.db,state,t,i*900.0,run=run)
   self.assertIn('scan',due)
   self.assertIn(('scan','started'),acted)
  self.assertEqual(state.tries['scan'][0],31)
  self.assertEqual([c for c in run.calls if c==['scan','--sync']],[['scan','--sync']]*31)
  # The scan_missed alert is still written once, on the first attempt.
  ev=self.db.query("SELECT data_json FROM paper_events WHERE namespace=? AND event_type='scan_missed'",(e.NS,));self.assertEqual(len(ev),1)

 def test_late_scan_before_eod_is_due_and_started(self):
  # I23: the scan-due window now ends at EOD (16:20), not the session
  # close (15:30). At 15:45 and at 16:05 the scan is due and tick starts
  # it; the old window excluded both times.
  state=sd.State();run=_RunRecorder()
  for i,(h,m) in enumerate([(15,45),(16,5)]):
   due,acted=self._due_and_tick(state,run,h,m,i*900.0)
   self.assertIn('scan',due)
   self.assertIn(('scan','started'),acted)
  self.assertEqual([c for c in run.calls if c==['scan','--sync']],[['scan','--sync']]*2)

 def test_no_scan_after_eod(self):
  # I23 guard: after EOD (16:20) the scan is no longer due and tick must
  # not start one, so no post-EOD scan can create signals. The daemon
  # moves on to the EOD action instead.
  state=sd.State();run=_RunRecorder()
  due,acted=self._due_and_tick(state,run,16,30,0.0)
  self.assertNotIn('scan',due)
  self.assertNotIn(('scan','started'),acted)
  self.assertEqual(acted,[('eod','started')])
  self.assertEqual(run.calls,[['eod']])

 def test_late_scan_after_completed_bar_cutoff_creates_next_session_signals(self):
  # I23: a scan that lands late — after the 16:00 completed-bar cutoff but
  # before EOD 16:20 — scans the just-completed bar and creates signals
  # eligible for the NEXT session (2026-11-11; 2026-11-10 is a holiday),
  # never for the session that just closed.
  for s in range(1,8):self.db.batch([('INSERT INTO paper_watermarks VALUES(?,?,?,?)',(e.NS,s,'RELIANCE-EQ','2026-11-06'))])
  dates=pd.bdate_range(end='2026-11-09',periods=300).strftime('%Y-%m-%d')
  def f(c):return pd.DataFrame({'trade_date':dates,'open':c,'high':c+1,'low':c-1,'close':c,'volume':100.})
  s=f(100.);s['symbol']='RELIANCE-EQ';b=f(100.)
  def indicator(i,g):return g.assign(entry_condition=True,exit_condition=False)
  with patch.object(e,'now',return_value=fixtures.T.replace(hour=16,minute=5)),patch.object(e,'universe',return_value=['RELIANCE-EQ']),patch.object(e,'read_history',return_value=s),patch.object(e.pf,'benchmark',return_value=b),patch.object(e.pf,'evaluate',return_value={'status':'PASS'}),patch.object(e,'indicator',side_effect=indicator):
   r=e.scan(self.db,None)
  self.assertEqual(r['signals_queued'],7)
  q=self.db.query("SELECT side,bar_date,eligible_date FROM paper_signals WHERE namespace=? AND status='pending'",(e.NS,))
  self.assertEqual(len(q),7)
  self.assertTrue(all(x['side']=='BUY' for x in q))
  self.assertTrue(all(x['bar_date']=='2026-11-09' for x in q))
  self.assertTrue(all(x['eligible_date']=='2026-11-11' for x in q))
