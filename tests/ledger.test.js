// @vitest-environment node
// Ledger tab adapters against an isolated temporary SQLite fixture. No credentials, no production database.
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {createClient} from '@libsql/client';import {afterEach,describe,it,expect} from 'vitest';
import {readDashboard,parseOptions} from '../server/data.mjs';
import {feeBreakdown,runSummary,eventSummary} from '../server/ledger-read.mjs';

const clients=[];afterEach(()=>clients.splice(0).forEach(({db,dir})=>{db.close();rmSync(dir,{recursive:true,force:true});}));
const engineSchema=readFileSync(process.cwd()+'/automation/paper-trading/schema.sql','utf8').split('\n').filter(l=>/CREATE TABLE IF NOT EXISTS paper_(fill_costs|cash_credits)\(/.test(l)).join('\n');
const eventsSchema="CREATE TABLE IF NOT EXISTS paper_events(namespace TEXT NOT NULL,event_id TEXT NOT NULL,run_id TEXT NOT NULL,session_day TEXT NOT NULL,source_at TEXT NOT NULL,recorded_at TEXT NOT NULL,event_type TEXT NOT NULL,severity TEXT NOT NULL,outcome TEXT NOT NULL,strategy INTEGER,symbol TEXT,bar_date TEXT,signal_id TEXT,policy_version TEXT,input_policy_version TEXT,data_json TEXT NOT NULL,PRIMARY KEY(namespace,event_id));";
async function fixture({costs=true,events=true}={}){
 const dir=mkdtempSync(join(tmpdir(),'atlas-ledger-test-'));const db=createClient({url:'file:'+join(dir,'isolated.db')});clients.push({db,dir});
 await db.executeMultiple(readFileSync(process.cwd()+'/tests/paper-schema.sql','utf8'));await db.executeMultiple(readFileSync(process.cwd()+'/server/demo-schema.sql','utf8'));
 if(costs)await db.executeMultiple(engineSchema);if(events)await db.executeMultiple(eventsSchema);return db;
}
const now=new Date('2026-11-09T10:00:00Z');const run=db=>async(sql,args)=>(await db.execute({sql,args})).rows;
const read=(db,q)=>readDashboard(parseOptions(q),run(db),now);
async function fill(db,id,signal,strategy,symbol,side,qty,price,at,reason,pnl){await db.execute({sql:'INSERT INTO paper_fills(id,namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',args:[id,'paper_v1',signal,strategy,symbol,side,qty,price,at,at,reason,pnl,side==='BUY'?-price*qty:price*qty]});}
async function cost(db,fillId,strategy,symbol,side,turnover,total,day='2026-11-09'){const fees={brokerage:Math.floor(total/2),stt:total-Math.floor(total/2),exchange:0,sebi:0,ipft:0,clearing:0,stamp:0,gst:0,dp:0,dp_gst:0,total};await db.execute({sql:'INSERT INTO paper_fill_costs(namespace,fill_id,day,strategy,symbol,isin,side,turnover_paise,policy_version,components_json,total_paise,entry_at,gross_cashflow_paise,net_cashflow_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',fillId,day,strategy,symbol,'INE000000000',side,turnover,'FIXTURE_POLICY_v1',JSON.stringify(fees),total,'2026-11-06T10:00:00+05:30',side==='BUY'?-turnover:turnover,(side==='BUY'?-turnover:turnover)-total]});}

describe('ledger tab adapters (isolated fixture)',()=>{
 it('history pairs each exit with its latest earlier entry and nets recorded charges of both fills',async()=>{
  const db=await fixture();
  await fill(db,1,'b1',1,'ABB-EQ','BUY',2,100,'2026-11-06T10:00:00+05:30','daily_entry:beta_momentum_v1',0);
  await fill(db,2,'s1',1,'ABB-EQ','SELL',2,110,'2026-11-09T10:00:00+05:30','observed_LTP_below_close_highwater_stop',20);
  await fill(db,3,'s2',2,'ACC-EQ','SELL',1,50,'2026-11-09T11:00:00+05:30','daily_exit:2026-11-01T10:00:00+05:30',-5);
  await cost(db,1,1,'ABB-EQ','BUY',20000,30);await cost(db,2,1,'ABB-EQ','SELL',22000,45);
  const r=await read(db,{view:'trades'});expect(r.total).toBe(2);
  const abb=r.rows.find(x=>x.symbol==='ABB-EQ'),acc=r.rows.find(x=>x.symbol==='ACC-EQ');
  expect(abb).toMatchObject({entry_fill_id:1,entry_price:100,exit_price:110,realized_pnl:20,charges_paise:75,net_pnl_paise:2000-75,entry_matched:true,industry:'Capital Goods',exit_reason:'observed_LTP_below_close_highwater_stop'});
  expect(abb.pnl_pct).toBeCloseTo(10);expect(abb.hold_hours).toBeCloseTo(72);expect(abb.exit_costs.fees).toMatchObject({brokerage:22,stt:23,total:45});
  // Unmatched exit: no invented entry, no net P&L, no charges without both fee rows.
  expect(acc).toMatchObject({entry_price:null,entry_matched:false,pnl_pct:null,hold_hours:null,charges_paise:null,net_pnl_paise:null});
  expect((await read(db,{view:'trades',strategy:'2'})).total).toBe(1);
 });
 it('fills carry their fee row; fills without one show null costs, never zero',async()=>{
  const db=await fixture();await fill(db,1,'b1',1,'ABB-EQ','BUY',1,100,'2026-11-09T10:00:00+05:30','daily_entry',0);await fill(db,2,'b2',1,'ACC-EQ','BUY',1,50,'2026-11-09T10:01:00+05:30','daily_entry',0);await cost(db,1,1,'ABB-EQ','BUY',10000,20);
  const r=await read(db,{view:'fills'});expect(r.costsAvailable).toBe(true);
  expect(r.rows.find(x=>x.id===1).costs).toMatchObject({turnover_paise:10000,charges_paise:20,net_cashflow_paise:-10020,policy_version:'FIXTURE_POLICY_v1'});
  expect(r.rows.find(x=>x.id===2).costs).toBeNull();
  const legacy=await fixture({costs:false});await fill(legacy,1,'b1',1,'ABB-EQ','BUY',1,100,'2026-11-09T10:00:00+05:30','daily_entry',0);
  const l=await read(legacy,{view:'fills'});expect(l.costsAvailable).toBe(false);expect(l.rows[0].costs).toBeNull();
 });
 it('transaction log keeps a running per-sleeve cash balance computed over the whole ledger, even when filtered',async()=>{
  const db=await fixture();await cost(db,1,1,'ABB-EQ','BUY',10000,20,'2026-11-06');await cost(db,2,1,'ABB-EQ','SELL',12000,30,'2026-11-09');await cost(db,3,2,'ACC-EQ','BUY',5000,10,'2026-11-09');
  const r=await read(db,{view:'costs'});expect(r.total).toBe(3);const sleeve=500000*100;
  expect(r.rows.map(x=>[x.fill_id,x.sleeve_cash_after_paise])).toEqual([[3,sleeve-5010],[2,sleeve-10020+11970],[1,sleeve-10020]]);
  const today=await read(db,{view:'costs',scope:'today',strategy:'1'});expect(today.rows.map(x=>[x.fill_id,x.sleeve_cash_after_paise])).toEqual([[2,sleeve-10020+11970]]);
  const missing=await fixture({costs:false});expect(await read(missing,{view:'costs'})).toMatchObject({available:false,rows:[],total:0});
 });
 it('orders/signals filter by status and attach the executing fill',async()=>{
  const db=await fixture();
  for(const[id,status]of [['p1','pending'],['f1','filled'],['x1','expired']])await db.execute({sql:'INSERT INTO paper_signals VALUES(?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',id,3,'ABB-EQ','2026-11-06','BUY',status,'daily_entry:beta_momentum_v1','2026-11-06T16:00:00+05:30','2026-11-09']});
  await fill(db,1,'f1',3,'ABB-EQ','BUY',4,99.5,'2026-11-09T09:20:00+05:30','daily_entry',0);
  const pending=await read(db,{view:'signals',status:'pending'});expect(pending.total).toBe(1);expect(pending.rows[0]).toMatchObject({id:'p1',status:'pending',fill_qty:null,reason:'daily_entry:beta_momentum_v1'});
  const filled=await read(db,{view:'signals',status:'filled'});expect(filled.rows[0]).toMatchObject({fill_qty:4,fill_price:99.5,filled_at:'2026-11-09T09:20:00+05:30'});
  expect((await read(db,{view:'signals'})).total).toBe(3);
 });
 it('engine runs filter by kind, expose a whitelisted summary and a latest-per-kind health strip',async()=>{
  const db=await fixture();
  await db.execute({sql:'INSERT INTO paper_runs(namespace,kind,at,status,details_json) VALUES(?,?,?,?,?)',args:['paper_v1','scan','2026-11-09T06:57:00Z','partial',JSON.stringify({signals_queued:17,latest_qualifiers:40,universe_symbols:750,unavailable_current_symbols:['A','B'],target_completed_bar:'2026-11-06',prefilter_counts:{PASS:300,FAIL:440,UNAVAILABLE:10},token:'secret-token'})]});
  for(let i=0;i<3;i++)await db.execute({sql:'INSERT INTO paper_runs(namespace,kind,at,status,details_json) VALUES(?,?,?,?,?)',args:['paper_v1','monitor',`2026-11-09T0${7+i}:00:00Z`,'ok',JSON.stringify({fills:0,quotes:17,fresh_quotes:17})]});
  await db.execute("INSERT INTO paper_quote_audit(namespace,symbol,observed_at,quote_at,price,accepted,reason) VALUES('paper_v1','ABB-EQ','2026-11-09T09:00:05Z','2026-11-09T09:00:00Z',100,1,'fresh')");
  const r=await read(db,{view:'runs',kind:'scan'});expect(r.total).toBe(1);
  expect(r.rows[0]).toMatchObject({kind:'scan',status:'partial',summary:{signals_queued:17,latest_qualifiers:40,universe_symbols:750,unavailable_symbols:2,target_bar:'2026-11-06',prefilter:{pass:300,fail:440,unavailable:10}}});
  expect(JSON.stringify(r)).not.toContain('secret');expect(r.rows[0]).not.toHaveProperty('details_json');
  expect(r.health.latestByKind.map(x=>[x.kind,Number(x.runs)])).toEqual([['monitor',3],['scan',1]]);expect(r.health.lastAcceptedQuoteAt).toBe('2026-11-09T09:00:00Z');
 });
 it('quote audit filters accepted/rejected and reports exchange lag',async()=>{
  const db=await fixture();for(const[acc,reason]of [[1,'fresh'],[0,'stale_or_future_quote']])await db.execute({sql:'INSERT INTO paper_quote_audit(namespace,symbol,observed_at,quote_at,price,accepted,reason) VALUES(?,?,?,?,?,?,?)',args:['paper_v1','ABB-EQ','2026-11-09T09:01:00Z','2026-11-09T09:00:00Z',100,acc,reason]});
  const r=await read(db,{view:'quotes',accepted:'0'});expect(r.total).toBe(1);expect(r.rows[0]).toMatchObject({accepted:0,reason:'stale_or_future_quote',lag_seconds:60});
 });
 it('alerts read risk hits and blocked orders from the audit log without exposing raw payloads; a missing table is unavailable',async()=>{
  const db=await fixture();
  const ev=(id,type,sev,outcome,data)=>db.execute({sql:'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,strategy,symbol,bar_date,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',id,'r','2026-11-09','2026-11-09T05:00:0'+id+'Z','2026-11-09T05:00:00Z',type,sev,outcome,7,'ABB-EQ','2026-11-06',JSON.stringify(data)]});
  await ev('1','risk_hit','INFO','ok',{price:95.5,threshold:96,rule:'observed_stop_crossing',quote_at:'2026-11-09T05:00:00Z',note:'free text /home/x'});
  await ev('2','paper_order_result','INFO','blocked',{reason:'SLEEVE_SLOT_FULL'});await ev('3','paper_order_result','INFO','filled',{});await ev('4','monitor_tick','INFO','ok',{});
  const r=await read(db,{view:'events'});expect(r.total).toBe(2);
  expect(r.rows.find(x=>x.event_type==='risk_hit')).toEqual({event_type:'risk_hit',severity:'INFO',outcome:'ok',strategy:7,symbol:'ABB-EQ',bar_date:'2026-11-06',source_at:'2026-11-09T05:00:01Z',price:95.5,threshold:96,rule:'observed_stop_crossing',quote_at:'2026-11-09T05:00:00Z'});
  expect(r.rows.find(x=>x.outcome==='blocked').reason).toBe('SLEEVE_SLOT_FULL');expect(JSON.stringify(r)).not.toContain('/home');
  expect(await read(await fixture({events:false}),{view:'events'})).toMatchObject({available:false,total:0,rows:[]});
 });
 it('data audit lists excluded bars and positions report fresh / stale / missing marks distinctly',async()=>{
  const db=await fixture();await db.execute("INSERT INTO paper_data_audit VALUES('paper_v1','ABB-EQ','2026-11-06','invalid_Fyers_OHLC','2026-11-09T03:00:00Z')");
  const a=await read(db,{view:'dataaudit'});expect(a.rows).toEqual([{symbol:'ABB-EQ',bar_date:'2026-11-06',reason:'invalid_Fyers_OHLC',observed_at:'2026-11-09T03:00:00Z'}]);
  for(const[s,mark,markAt]of [['ABB-EQ',110,'2026-11-09T09:59:40Z'],['ACC-EQ',55,'2026-11-09T08:00:00Z'],['AGI-EQ',null,null]])await db.execute({sql:'INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',1,s,2,100,'2026-11-09T04:00:00Z',100,mark,markAt,'2026-11-09T09:59:40Z']});
  const p=await read(db,{view:'positions'});const by=Object.fromEntries(p.rows.map(r=>[r.symbol,r]));
  expect(by['ABB-EQ']).toMatchObject({mark_state:'fresh',unrealized_pnl:20,unrealized_pct:10,cost_basis:200,market_value:220,high_water:100});
  expect(by['ACC-EQ'].mark_state).toBe('stale');expect(by['AGI-EQ']).toMatchObject({mark_state:'missing',unrealized_pnl:null,market_value:null});
  expect(p.market).toHaveProperty('open');
 });
});

describe('ledger parameter boundary',()=>{
 it.each([{view:'fills',status:'pending'},{view:'signals',status:'DROP'},{view:'signals',kind:'scan'},{view:'runs',kind:'x'},{view:'quotes',accepted:'yes'},{view:'fills',accepted:'1'},{view:'signals',status:['pending','filled']}])('rejects %j',q=>expect(()=>parseOptions(q)).toThrow('BAD_REQUEST'));
 it.each([{view:'dataaudit',strategy:'1'},{view:'runs',strategy:'2'},{view:'quotes',strategy:'3'}])('rejects a strategy filter the table cannot honour %j',async q=>{await expect((async()=>readDashboard(parseOptions(q),async()=>[{total:0}],now))()).rejects.toThrow('BAD_REQUEST');});
 it('accepts the new enums',()=>{expect(parseOptions({view:'signals',status:'pending'}).status).toBe('pending');expect(parseOptions({view:'runs',kind:'scan'}).kind).toBe('scan');expect(parseOptions({view:'quotes',accepted:'0'}).accepted).toBe(0);for(const view of ['trades','costs','credits','dataaudit','events'])expect(parseOptions({view}).view).toBe(view);});
 it('whitelists payload fields',()=>{
  expect(feeBreakdown('{"brokerage":5,"stt":1,"total":6,"note":"x"}')).toEqual({brokerage:5,exchange:null,sebi:null,ipft:null,clearing:null,stt:1,stamp:null,gst:null,dp:null,dp_gst:null,total:6});expect(feeBreakdown('not json')).toBeNull();
  expect(runSummary('{"fills":1,"error":"Bearer abc","status":"market_closed_or_calendar_unverified"}')).toEqual({fills:1,detail_status:'market_closed_or_calendar_unverified'});
  expect(eventSummary('{"price":"1","reason":"has space","rule":"trail_gap"}')).toEqual({price:1,rule:'trail_gap'});
 });
});
