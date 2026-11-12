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
 GSM_ARCHIVE='\ufeff"SR. NO \n","SYMBOL \n","COMPANY NAME \n","ISIN \n","GSM STAGE \n"\n1,"INFY","Infosys Limited","INE009A01021","GSM - I (1)"\n2,"NOTINUNIV","Other","INE000X00000","GSM - 0 (99)"\n'
 def fetch_with(self,asm_api_ok=True,archive_ok=(True,True),ca=None,gsm_api_ok=False,gsm_archive_ok=True):
  def fetch(url,as_json=True):
   if url==CFG['nse_asm_url']:
    if asm_api_ok:return {'longterm':{'data':[{'symbol':'INFY'}]},'shortterm':{'data':[]}}
    raise RuntimeError('http_403')
   if url in CFG['nse_asm_archive_urls']:
    i=CFG['nse_asm_archive_urls'].index(url)
    if not archive_ok[i]:raise RuntimeError('http_403')
    return [ARCHIVE,SHORT][i]
   if url==CFG['nse_gsm_url']:
    if gsm_api_ok:return {'gsm':{'data':[{'symbol':'INFY'}]}}
    raise RuntimeError('http_403')
   if url in (CFG.get('nse_gsm_archive_urls') or []):
    if not gsm_archive_ok:raise RuntimeError('http_403')
    return self.GSM_ARCHIVE
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
 def test_gsm_archive_parser(self):
  rows=rf.gsm_from_archive([self.GSM_ARCHIVE]);self.assertEqual([r['symbol'] for r in rows],['INFY','NOTINUNIV']);self.assertEqual(rows[0]['stage'],'GSM - I (1)');self.assertEqual(rows[0]['isin'],'INE009A01021')
  # requests often decodes a UTF-8 BOM as Latin-1 (ï»¿), which would otherwise break quoted headers.
  mis=self.GSM_ARCHIVE.encode('utf-8').decode('latin-1')
  rows=rf.gsm_from_archive([mis]);self.assertEqual([r['symbol'] for r in rows],['INFY','NOTINUNIV'])
  with self.assertRaisesRegex(RuntimeError,'gsm_archive_unexpected_format'):rf.gsm_from_archive(['<html></html>'])
 def test_gsm_api_refused_uses_archive(self):
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(asm_api_ok=True,gsm_api_ok=False,gsm_archive_ok=True))
  self.assertEqual(r['sources']['gsm']['status'],'ok');self.assertEqual(r['sources']['gsm']['via'],'archive_csv')
  self.assertEqual(r['sources']['gsm']['flagged_symbols'],['INFY-EQ']);self.assertEqual(r['sources']['gsm']['source'],'NSE_GSM_ARCHIVE')
  self.assertIn('gsm',r['fallbacks']);self.assertEqual(r['fallbacks']['gsm']['api_error'],'http_403')
  row=self.db.query("SELECT surveillance,source FROM paper_price_bands WHERE symbol=? AND trade_date=?",('INFY-EQ','2026-11-09'))[0]
  self.assertEqual(row['surveillance'],'GSM')
 def test_gsm_all_sources_fail_is_unavailable(self):
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(gsm_api_ok=False,gsm_archive_ok=False))
  self.assertEqual(r['sources']['gsm']['status'],'unavailable')
  self.assertNotIn('via',r['sources']['gsm'])
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
 def test_ca_api_403_uses_http2_fallback_with_identical_mapping(self):
  fixture=base.fixture_fetch(CFG['nse_corporate_actions_url'])
  api=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with())
  api_rows=self.db.query('SELECT symbol,ex_date,kind,ratio_num,ratio_den,cash_paise FROM paper_corporate_actions ORDER BY symbol,ex_date,kind')
  self.db.batch([('DELETE FROM paper_corporate_actions',()),('DELETE FROM paper_price_bands',())])
  def ca_fail(url,as_json=True):
   if url.startswith(CFG['nse_corporate_actions_url']):raise RuntimeError('http_403')
   return base.fixture_fetch(url,as_json)
  r=rf.refresh(e,self.db,'2026-11-09',fetch=ca_fail,http2_fetch=lambda u: fixture)
  self.assertEqual(r['sources']['corporate_actions']['status'],'ok')
  self.assertEqual(r['sources']['corporate_actions']['via'],'http2')
  self.assertEqual(r['sources']['corporate_actions']['source'],'NSE_CORPORATE_ACTIONS_HTTP2')
  self.assertIn('corporate_actions',r['fallbacks'])
  self.assertEqual(r['fallbacks']['corporate_actions']['api_error'],'http_403')
  fb_rows=self.db.query('SELECT symbol,ex_date,kind,ratio_num,ratio_den,cash_paise FROM paper_corporate_actions ORDER BY symbol,ex_date,kind')
  self.assertEqual(fb_rows, api_rows)
  self.assertGreater(len(api_rows),0)
 def test_ca_all_sources_fail_keeps_existing_rows(self):
  self.db.batch([('INSERT INTO paper_corporate_actions(symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at) VALUES(?,?,?,?,?,?,?,?)',('RELIANCE-EQ','2026-11-09','CASH_DIVIDEND',None,None,500,'RETAINED','2026-11-11T15:40:50+05:30'))])
  def ca_fail(url,as_json=True):
   if url.startswith(CFG['nse_corporate_actions_url']):raise RuntimeError('http_403')
   return base.fixture_fetch(url,as_json)
  r=rf.refresh(e,self.db,'2026-11-09',fetch=ca_fail,http2_fetch=lambda u: (_ for _ in ()).throw(RuntimeError('http_403')))
  self.assertEqual(r['sources']['corporate_actions']['status'],'unavailable')
  self.assertEqual(r['sources']['corporate_actions']['error'],'http_403')
  self.assertNotIn('via',r['sources']['corporate_actions'])
  kept=self.db.query('SELECT symbol,ex_date,kind,cash_paise,source,recorded_at FROM paper_corporate_actions')
  self.assertEqual(len(kept),1)
  self.assertEqual(kept[0]['source'],'RETAINED')
  self.assertEqual(kept[0]['cash_paise'],500)
 def test_http2_html_is_never_parsed_as_ca_data(self):
  with self.assertRaisesRegex(RuntimeError,'html_instead_of_data'):
   rf.http2_json(CFG,'https://www.nseindia.com/api/corporates-corporateActions',curl_get=lambda *a,**k: '<html>Access Denied</html>')
 def test_reference_feeds_busy_lock_fails_closed_without_writes(self):
  import fcntl
  lock=e.ROOT/'tests'/('feeds_lock_'+e.NS)
  with lock.open('a+') as fh:
   fcntl.flock(fh,fcntl.LOCK_EX)
   r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(),feeds_lock=lock)
   self.assertEqual(r['status'],'unavailable')
   self.assertEqual(r['error'],'reference_feeds_busy')
   self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_corporate_actions')[0]['n'],0)
   self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_price_bands')[0]['n'],0)
  lock.unlink(missing_ok=True)
 def test_dry_run_writes_nothing_and_reports_impact(self):
  self.db.batch([('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,1,'RELIANCE-EQ',10,100,T.isoformat(),100,T.isoformat())),('INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,updated_at) VALUES(?,?,?,?,?,?,?,?)',(e.NS,7,'INFY-EQ',5,100,T.isoformat(),100,T.isoformat()))])
  r=rf.refresh(e,self.db,'2026-11-09',fetch=self.fetch_with(),dry_run=True)
  self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_corporate_actions')[0]['n'],0);self.assertEqual(self.db.query('SELECT COUNT(*) n FROM paper_price_bands')[0]['n'],0)
  self.assertTrue(r['dry_run']);self.assertEqual(r['guard_rows_written'],0);self.assertEqual(r['sources']['corporate_actions']['rows_written'],0)
  self.assertEqual(r['impact']['entry_blocked_symbols_held'],['INFY-EQ'])
  self.assertEqual(r['impact']['corporate_actions_ex_trade_day_on_held_symbols'],[('RELIANCE-EQ','BONUS'),('RELIANCE-EQ','CASH_DIVIDEND')])

 def test_pr_purpose_and_iso_dates_map_like_the_api(self):
  self.assertEqual(rf.nse_day('12-Nov-2026'),'2026-11-12')
  self.assertEqual(rf.nse_day('2026-11-12'),'2026-11-12')
  self.assertEqual(rf.nse_day('12-11-2026'),'2026-11-12')
  self.assertIsNone(rf.nse_day('not-a-date'))
  self.assertEqual(rf.classify(rf.pr_purpose_subject('INTDIV - RS 7 PER SH')),[('CASH_DIVIDEND',None,None,700)])
  self.assertEqual(rf.classify(rf.pr_purpose_subject('INTDIV -RS 90PERSH')),[('CASH_DIVIDEND',None,None,9000)])
  self.assertEqual(rf.classify(rf.pr_purpose_subject('INT DIV - RS 1.90 PER SH')),[('CASH_DIVIDEND',None,None,190)])
  self.assertEqual(rf.classify(rf.pr_purpose_subject('NTDIV - RE 1 PER SH')),[('CASH_DIVIDEND',None,None,100)])
  self.assertEqual(rf.classify(rf.pr_purpose_subject('SPLDIV-1.50/INTDIV-1.50'))[0][0],'EXTRAORDINARY_DIVIDEND_REVIEW')
  self.assertEqual(rf.classify(rf.pr_purpose_subject('BUY BACK')),[])
  self.assertEqual(rf.classify(rf.pr_purpose_subject('RIGHTS 5:12 @ PRM RS855'))[0][0],'RIGHTS')
 def _pr_zip(self,csv_text,name='bc09112026.csv'):
  import io,zipfile
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:z.writestr(name,csv_text)
  return buf.getvalue()
 def test_ca_api_and_http2_403_uses_pr_archive(self):
  csv='\n'.join([
   'SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE',
   'EQ,RELIANCE,Reliance,2026-11-09,,,2026-11-09,,,INTDIV - RS 5 PER SH',
   'BE,RELIANCE,Reliance,2026-11-09,,,2026-11-09,,,INTDIV - RS 5 PER SH',
   'EQ,NAVNETEDUL,Navneet,2026-11-09,,,2026-11-09,,,SPLDIV-1.50/INTDIV-1.50',
   'EQ,TCS,TCS,2026-11-16,,,2026-11-16,,,INTDIV - RE 1 PER SH',
   'EQ,FDC,FDC,2026-11-13,,,2026-11-13,,,BUY BACK',
  ])+'\n'
  def ca_fail(url,as_json=True):
   if url.startswith(CFG['nse_corporate_actions_url']):raise RuntimeError('http_403')
   return base.fixture_fetch(url,as_json)
  r=rf.refresh(e,self.db,'2026-11-09',fetch=ca_fail,http2_fetch=lambda u:(_ for _ in ()).throw(RuntimeError('http_403')),pr_fetch=lambda u:self._pr_zip(csv))
  self.assertEqual(r['sources']['corporate_actions']['status'],'ok')
  self.assertEqual(r['sources']['corporate_actions']['via'],'pr_archive')
  self.assertEqual(r['sources']['corporate_actions']['source'],'NSE_PR_BC_ARCHIVE')
  self.assertEqual(r['fallbacks']['corporate_actions']['api_error'],'http_403')
  self.assertEqual(r['fallbacks']['corporate_actions']['http2_error'],'http_403')
  kinds={(x['symbol'],x['ex_date'],x['kind'],x['cash_paise']) for x in self.db.query('SELECT symbol,ex_date,kind,cash_paise FROM paper_corporate_actions')}
  self.assertIn(('RELIANCE-EQ','2026-11-09','CASH_DIVIDEND',500),kinds)
  self.assertIn(('NAVNETEDUL-EQ','2026-11-09','EXTRAORDINARY_DIVIDEND_REVIEW',None),kinds)
  self.assertIn(('TCS-EQ','2026-11-16','CASH_DIVIDEND',100),kinds)
  self.assertFalse(any(k[0]=='FDC-EQ' for k in kinds))
 def test_pr_archive_bad_payload_is_unavailable_and_keeps_rows(self):
  self.db.batch([('INSERT INTO paper_corporate_actions(symbol,ex_date,kind,ratio_num,ratio_den,cash_paise,source,recorded_at) VALUES(?,?,?,?,?,?,?,?)',('RELIANCE-EQ','2026-11-09','CASH_DIVIDEND',None,None,500,'RETAINED','2026-11-11T15:40:50+05:30'))])
  def ca_fail(url,as_json=True):
   if url.startswith(CFG['nse_corporate_actions_url']):raise RuntimeError('http_403')
   return base.fixture_fetch(url,as_json)
  r=rf.refresh(e,self.db,'2026-11-09',fetch=ca_fail,http2_fetch=lambda u:(_ for _ in ()).throw(RuntimeError('http_403')),pr_fetch=lambda u:b'<html>Access Denied</html>')
  self.assertEqual(r['sources']['corporate_actions']['status'],'unavailable')
  self.assertEqual(r['sources']['corporate_actions']['error'],'html_instead_of_data')
  kept=self.db.query('SELECT symbol,cash_paise,source FROM paper_corporate_actions')
  self.assertEqual(len(kept),1);self.assertEqual(kept[0]['source'],'RETAINED');self.assertEqual(kept[0]['cash_paise'],500)

if __name__=='__main__':unittest.main()
