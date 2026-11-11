"""NSE fetcher: browser headers, refused-session retry/backoff, HTML rejection, ASM archive fallback, dry-run impact."""
import json,sys,unittest,uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import engine as e
import reference_feeds as rf
import test_reference_feeds as base
T=base.T;CFG=e.CFG['reference_feeds']
class Resp:
 def __init__(self,status,body=None,kind='application/json'):self.status_code=status;self.body=body;self.headers={'content-type':kind}
 def json(self):
  if isinstance(self.body,str):raise ValueError('not json')
  return self.body
 @property
 def text(self):return self.body if isinstance(self.body,str) else json.dumps(self.body)
class Session:
 """Each session replays the next scripted list of responses (warm-up first)."""
 def __init__(self,script,log):self.script=script;self.log=log
 def get(self,url,headers=None,timeout=None):self.log.append((url,headers));return self.script.pop(0)
 def close(self):pass
def factory(scripts,log):
 it=iter(scripts)
 return lambda:Session(list(next(it)),log)
API='https://www.nseindia.com/api/reportASM'
class FetcherTests(unittest.TestCase):
 def test_refused_session_is_replaced_with_backoff(self):
  log=[];sleeps=[]
  f=rf.NseFetcher(CFG,factory([[Resp(403)],[Resp(200,{}),Resp(200,{'x':{'data':[]}})]],log),sleeps.append)
  self.assertEqual(f(API),{'x':{'data':[]}});self.assertEqual(f.sessions,2);self.assertEqual(sleeps,[CFG['backoff_seconds']])
  url,h=log[-1];self.assertEqual(h['User-Agent'],CFG['user_agent']);self.assertEqual(h['Referer'],CFG['nse_referers']['reportASM'])
  self.assertNotIn('paper-only',h['User-Agent'])
 def test_session_reused_across_sources(self):
  log=[];f=rf.NseFetcher(CFG,factory([[Resp(200,{}),Resp(200,[1]),Resp(200,[2])]],log),lambda s:None)
  self.assertEqual(f(API),[1]);self.assertEqual(f('https://www.nseindia.com/api/reportGSM'),[2]);self.assertEqual(f.sessions,1)
 def test_html_page_is_never_parsed_as_data(self):
  scripts=[[Resp(200,{}),Resp(200,'<html>Access Denied</html>','text/html')] for _ in range(CFG['max_attempts'])]
  f=rf.NseFetcher(CFG,factory(scripts,[]),lambda s:None)
  with self.assertRaisesRegex(RuntimeError,'html_instead_of_data'):f(API)
 def test_attempts_and_backoff_are_bounded(self):
  sleeps=[];f=rf.NseFetcher(CFG,factory([[Resp(403)] for _ in range(CFG['max_attempts'])],[]),sleeps.append)
  with self.assertRaisesRegex(RuntimeError,'http_403'):f(API)
  self.assertEqual(f.attempts,CFG['max_attempts']);self.assertEqual(len(sleeps),CFG['max_attempts']-1);self.assertLessEqual(max(sleeps),CFG['backoff_max_seconds'])
 def test_non_retryable_status_raises_at_once(self):
  f=rf.NseFetcher(CFG,factory([[Resp(200,{}),Resp(404)]],[]),lambda s:None)
  with self.assertRaisesRegex(RuntimeError,'http_404'):f(API)
  self.assertEqual(f.attempts,1)
 def test_non_nse_hosts_skip_warmup_session(self):
  f=rf.NseFetcher(CFG,lambda:(_ for _ in ()).throw(AssertionError('no session for fyers')),lambda s:None)
  with patch.object(rf.requests,'get',return_value=Resp(200,'a,b','text/csv')) as get:self.assertEqual(f(CFG['fyers_symbol_master_url'],as_json=False),'a,b')
  get.assert_called_once()
ARCHIVE='''
SYMBOL              ,COMPANY             ,ISIN          ,ASM Stage,TIMESTAMP
--------------------,--------------------,--------------,---------,--------------------
INFY                ,Infosys Limited     ,INE009A01021  ,Stage I  ,11-Nov-2026
NOTINUNIV           ,Other               ,INE000X00000  ,Stage II ,11-Nov-2026
'''
SHORT='''
SYMBOL              ,COMPANY             ,ISIN          ,ASM Stag,TIMESTAMP
--------------------,--------------------,--------------,--------,--------------------
TCS                 ,TCS Limited         ,INE467B01029  ,Stage I ,11-Nov-2026
'''
class RefreshTests(base.ReferenceFeedTests):
 def fetch_with(self,asm_api_ok=True,archive_ok=(True,True),ca=None):
  def fetch(url,as_json=True):
   if url==CFG['nse_asm_url']:
    if asm_api_ok:return {'longterm':{'data':[{'symbol':'INFY'}]},'shortterm':{'data':[]}}
    raise RuntimeError('http_403')
   if url in CFG['nse_asm_archive_urls']:
    i=CFG['nse_asm_archive_urls'].index(url)
    if not archive_ok[i]:raise RuntimeError('http_403')
    return [ARCHIVE,SHORT][i]
   if url.startswith(CFG['nse_corporate_actions_url']) and ca is not None:return ca
   return base.fixture_fetch(url,as_json)
  return fetch
 def setUp(self):
  e.NS='test_'+uuid.uuid4().hex;self.db.batch([('DELETE FROM paper_corporate_actions',()),('DELETE FROM paper_price_bands',())])
 def test_asm_archive_parser(self):
  rows=rf.asm_from_archive([ARCHIVE,SHORT]);self.assertEqual([r['symbol'] for r in rows],['INFY','NOTINUNIV','TCS']);self.assertEqual(rows[0]['stage'],'Stage I')
  with self.assertRaisesRegex(RuntimeError,'asm_archive_unexpected_format'):rf.asm_from_archive(['<html></html>'])
 def test_asm_api_refused_uses_archive(self):
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(asm_api_ok=False))
  self.assertEqual(r['sources']['asm']['via'],'archive_csv');self.assertEqual(r['sources']['asm']['flagged_symbols'],['INFY-EQ','TCS-EQ']);self.assertIn('asm',r['fallbacks'])
 def test_partial_archive_is_unavailable_not_partial(self):
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(asm_api_ok=False,archive_ok=(True,False)))
  self.assertEqual(r['sources']['asm']['status'],'unavailable')
 def test_corporate_actions_use_lookahead_range_url(self):
  seen=[]
  def fetch(url,as_json=True):seen.append(url);return base.fixture_fetch(url,as_json)
  rf.refresh(e,self.db,'2026-11-09',fetch=fetch)
  self.assertIn('from_date=09-11-2026&to_date=16-11-2026',seen[0])
 def test_unexpected_ca_payload_is_unavailable(self):
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(ca={'error':'x'}))
  self.assertEqual(r['sources']['corporate_actions']['status'],'unavailable')
 def test_dry_run_writes_nothing_and_reports_impact(self):
  self.db.batch([('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,1,'RELIANCE-EQ',10,100,T.isoformat(),100,T.isoformat())),('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,7,'INFY-EQ',5,100,T.isoformat(),100,T.isoformat()))])
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(),dry_run=True)
  self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_corporate_actions')[0]['n'],0);self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_price_bands')[0]['n'],0)
  self.assertTrue(r['dry_run']);self.assertEqual(r['guard_rows_written'],0);self.assertEqual(r['sources']['corporate_actions']['rows_written'],0)
  self.assertEqual(r['impact']['entry_blocked_symbols_held'],['INFY-EQ'])
  self.assertEqual(r['impact']['corporate_actions_ex_trade_day_on_held_symbols'],[('RELIANCE-EQ','BONUS'),('RELIANCE-EQ','CASH_DIVIDEND')])
if __name__=='__main__':unittest.main()
