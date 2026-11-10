// @vitest-environment node
// Stop / target / R:R / break-even for the paper ledger. Isolated temp SQLite fixture, no credentials.
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {createClient} from '@libsql/client';import {afterEach,describe,it,expect} from 'vitest';
import {readDashboard,parseOptions} from '../server/data.mjs';
import {breakEvenPaise,closedBreakEven,exitChargesPaise,exitPlan,indicatorBars,ruleLevel,validBar} from '../server/exit-plan.mjs';
import {EXIT_RULES,NO_TARGET,pyRound2,TRAIL_FRACTION,TRAIL_PCT_BELOW,trailLevel} from '../src/exitRules.js';
import {tradePlan} from '../src/backtestMath.js';

const CFG=JSON.parse(readFileSync('automation/paper-trading/config.json','utf8'));
const RULES=JSON.parse(readFileSync('automation/paper-trading/strategy_rules.json','utf8'));
const clients=[];afterEach(()=>clients.splice(0).forEach(({db,dir})=>{db.close();rmSync(dir,{recursive:true,force:true});}));
const engineSchema=readFileSync('automation/paper-trading/schema.sql','utf8').split('\n').filter(l=>/CREATE TABLE IF NOT EXISTS paper_(fill_costs|qualifier_states|pause_state|events)\(/.test(l)).map(l=>l.split(';')[0]+';').join('\n');
async function fixture(){
 const dir=mkdtempSync(join(tmpdir(),'atlas-exit-test-'));const db=createClient({url:'file:'+join(dir,'isolated.db')});clients.push({db,dir});
 await db.executeMultiple(readFileSync('tests/paper-schema.sql','utf8'));await db.executeMultiple(readFileSync('server/demo-schema.sql','utf8'));await db.executeMultiple(engineSchema);
 await db.execute('CREATE TABLE daily_ohlcv(trade_date TEXT,symbol TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL,source TEXT,loaded_at TEXT,PRIMARY KEY(trade_date,symbol))');
 return db;
}
const now=new Date('2026-11-09T10:00:00Z');const run=db=>async(sql,args)=>(await db.execute({sql,args})).rows;
const read=(db,q)=>readDashboard(parseOptions(q),run(db),now);
const pos=(db,strategy,symbol,qty,entry,hw,mark,entryAt='2026-11-06T09:15:00+05:30')=>db.execute({sql:'INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',strategy,symbol,qty,entry,entryAt,hw,mark,mark===null?null:'2026-11-09T15:29:59+05:30','2026-11-09T15:30:00+05:30']});
const days=n=>{const out=[];const d=new Date('2026-07-01T00:00:00Z');while(out.length<n){const w=d.getUTCDay();if(w>0&&w<6)out.push(d.toISOString().slice(0,10));d.setUTCDate(d.getUTCDate()+1);}return out;};
async function bars(db,symbol,list){for(const b of list)await db.execute({sql:'INSERT INTO daily_ohlcv(trade_date,symbol,open,high,low,close) VALUES(?,?,?,?,?,?)',args:[b.d,symbol,b.o,b.h,b.l,b.c]});}

describe('Strategy 07 trail stop = trail_level(high_water) from config',()=>{
 it('reads the fraction from config.json (no hardcoded 0.90) and it agrees with the frozen rule text',()=>{
  expect(TRAIL_FRACTION).toBe(Number(CFG.strategy_07_trail_fraction));
  expect(RULES['7'].trailing_update).toContain(String(TRAIL_FRACTION).replace(/^0/,'')+'*H');
  expect(TRAIL_PCT_BELOW).toBe(String(Number(((1-TRAIL_FRACTION)*100).toFixed(6))));
  expect(EXIT_RULES[7]).toContain(`${TRAIL_PCT_BELOW}%`);
  const src=readFileSync('src/exitRules.js','utf8')+readFileSync('server/exit-plan.mjs','utf8');expect(src).not.toMatch(/[^\d.]0?\.9[0-9]*\s*\*/);
 });
 it('rounds exactly like Python round(x, 2), including binary ties',()=>{
  // Vectors from CPython: round(0.9*h, 2).
  for(const [h,want] of [[87,78.3],[100,90],[123.45,111.11],[318.38,286.54],[1.39,1.25],[2.5,2.25],[1234.56,1111.1]])expect(trailLevel(h,0.9)).toBe(want);
  expect(pyRound2(0.125)).toBe(0.12);expect(pyRound2(2.675)).toBe(2.67);expect(pyRound2(1.005)).toBe(1);expect(pyRound2(0.375)).toBe(0.38);
  expect(trailLevel(null)).toBeNull();expect(trailLevel(0)).toBeNull();expect(trailLevel('x')).toBeNull();
 });
 it('exit plan: real trail level, distance to mark, risk distance (not R:R), no target',()=>{
  const p=exitPlan({strategy:7,qty:10,entry_price:100,high_water:120,mark_price:115,mark_state:'stale'});
  const level=trailLevel(120);expect(p.stop).toMatchObject({defined:true,kind:'trail',level,high_water:120,fraction:TRAIL_FRACTION,risk_per_share:Math.round((100-level)*100)/100,locked_in:level>=100});
  expect(p.stop.distance_to_mark).toBeCloseTo(115-level,2);expect(p.stop.open_risk_to_stop).toBeCloseTo((115-level)*10,2);
  expect(p.target).toMatchObject({defined:false,label:NO_TARGET});expect(p.risk_reward.value).toBeNull();
  const none=exitPlan({strategy:7,qty:1,entry_price:100,high_water:null,mark_price:null});expect(none.stop.level).toBeNull();expect(none.stop.unavailable_reason).toMatch(/high-water/);
 });
 it('the backtest trade plan uses the same configured fraction',()=>{expect(tradePlan(7,{close:100}).illustration.value).toBeCloseTo(TRAIL_FRACTION*100);expect(tradePlan(7,{}).stop).toContain(`${TRAIL_PCT_BELOW}% below`);});
});

describe('Strategies 01-06: rule text, a level only from stored completed bars',()=>{
 const mk=(closes,low=c=>c-1)=>closes.map((c,i)=>({trade_date:days(closes.length)[i],open:c,high:c+1,low:low(c),close:c}));
 it('never shows a fake rupee stop; rule text always present; crossovers have no level',()=>{
  for(const s of [1,2,3,4,5,6]){const p=exitPlan({strategy:s,qty:1,entry_price:100,high_water:100,mark_price:101},{bars:[],snapshot:null});
   expect(p.stop.defined).toBe(false);expect(p.stop.kind).toBe('rule');expect(p.stop.level).toBeNull();expect(p.stop.rule).toBe(EXIT_RULES[s]);expect(p.stop.unavailable_reason).toBeTruthy();expect(p.target.defined).toBe(false);expect(p.risk_reward.value).toBeNull();}
  expect(ruleLevel(4,mk(Array(60).fill(100)),null).level).toBeNull();expect(ruleLevel(6,mk(Array(60).fill(100)),null).level).toBeNull();
 });
 it('S1 = lowest low of the last 10 bars; S3/S5/S7-SMA50 are solved for the next close exactly',()=>{
  const closes=Array.from({length:60},(_,i)=>100+i);const b=mk(closes,c=>c-2);
  expect(ruleLevel(1,b).level).toBe(Math.min(...closes.slice(-10))-2);
  const s3=ruleLevel(3,b).level,last4=closes.slice(-4);expect(s3).toBeCloseTo(last4.reduce((a,v)=>a+v)/4,2);
  // A next close exactly at the level puts close == SMA5 (no exit, strict >); a cent above exits.
  const sma5=c=>(last4.reduce((a,v)=>a+v)+c)/5;expect(s3>sma5(s3)).toBe(false);expect(s3+0.01>sma5(s3+0.01)).toBe(true);
  expect(ruleLevel(5,b).level).toBeCloseTo(closes.slice(-19).reduce((a,v)=>a+v)/19,2);
  expect(ruleLevel(7,b).level).toBeCloseTo(closes.slice(-49).reduce((a,v)=>a+v)/49,2);
  expect(ruleLevel(1,b.slice(0,9)).level).toBeNull();
 });
 it('uses the engine bar filter: invalid OHLC and indicator-excluded sessions are dropped, later bars ignored',()=>{
  expect(validBar({open:1,high:2,low:0.5,close:1.5})).toBe(true);expect(validBar({open:1,high:0.9,low:0.5,close:1})).toBe(false);expect(validBar({open:0,high:1,low:0,close:1})).toBe(false);
  const ex=Object.keys(CFG.indicator_excluded_sessions)[0];const list=[{trade_date:'2026-11-06',open:1,high:2,low:1,close:1},{trade_date:ex,open:1,high:2,low:1,close:1},{trade_date:'2026-11-07',open:1,high:0.5,low:1,close:1},{trade_date:'2026-11-12',open:1,high:2,low:1,close:1}];
  expect(indicatorBars(list,'2026-11-09').map(b=>b.trade_date)).toEqual(['2026-11-06']);
 });
});

describe('break-even after charges',()=>{
 it('open position: lowest exit price where gross - entry charges - estimated exit charges >= 0',()=>{
  for(const [entry,qty,fee,overnight] of [[39000n,10,2500,true],[8690n,1,1800,false],[125050n,3,9000,true],[10000n,57,4300,true]]){
   const be=breakEvenPaise(entry,qty,fee,overnight);const net=p=>(BigInt(p)-entry)*BigInt(qty)-BigInt(fee)-BigInt(exitChargesPaise(BigInt(p),qty,overnight));
   expect(net(be)>=0n).toBe(true);expect(net(be-1)<0n).toBe(true);expect(be).toBeGreaterThan(Number(entry));
  }
  expect(breakEvenPaise(39000n,10,null,true)).toBeNull();
 });
 it('closed trade: entry + recorded charges of both fills per share, rounded up to a paisa',()=>{expect(closedBreakEven(390,10,4000)).toBe(394);expect(closedBreakEven(100,3,100)).toBe(100.34);expect(closedBreakEven(null,1,1)).toBeNull();expect(closedBreakEven(100,1,null)).toBeNull();});
});

describe('API (isolated fixture)',()=>{
 it('positions carry the S7 trail stop, S1 rule level from stored bars, charges, break-even and no target',async()=>{
  const db=await fixture();
  await pos(db,7,'ABB-EQ',10,100,120,115);await pos(db,1,'ACC-EQ',2,50,50,52);await pos(db,3,'CUPID-EQ',1,10,10,null);
  for(const [s,sym] of [[7,'ABB-EQ'],[1,'ACC-EQ'],[3,'CUPID-EQ']])await db.execute({sql:'INSERT INTO paper_watermarks VALUES(?,?,?,?)',args:['paper_v1',s,sym,'2026-11-06']});
  const closes=Array.from({length:12},(_,i)=>50+i);const d=['2026-10-21','2026-10-22','2026-10-23','2026-10-26','2026-10-27','2026-10-28','2026-10-29','2026-10-30','2026-11-02','2026-11-03','2026-11-04','2026-11-06'];
  await bars(db,'ACC-EQ',closes.map((c,i)=>({d:d[i],o:c,h:c+1,l:c-1,c})));await bars(db,'ACC-EQ',[{d:'2026-11-09',o:1,h:1,l:0.1,c:1}]);
  await db.execute({sql:'INSERT INTO paper_fill_costs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',1,'2026-11-06',7,'ABB-EQ','INE117A01022','BUY',100000,'P','{}',2500,'2026-11-06T09:15:00+05:30',-100000,-102500]});
  const r=await read(db,{view:'positions'});const abb=r.rows.find(x=>x.symbol==='ABB-EQ'),acc=r.rows.find(x=>x.symbol==='ACC-EQ'),cup=r.rows.find(x=>x.symbol==='CUPID-EQ');
  expect(abb.exit_plan.stop).toMatchObject({kind:'trail',level:trailLevel(120),high_water:120});expect(abb.exit_plan.target.label).toBe(NO_TARGET);expect(abb.exit_plan.risk_reward.value).toBeNull();
  expect(abb.gross_pnl_paise).toBe(15000);expect(abb.entry_charges_paise).toBe(2500);expect(abb.est_exit_charges_paise).toBe(exitChargesPaise(11500n,10,true));
  expect(abb.net_unrealized_paise).toBe(15000-2500-abb.est_exit_charges_paise);expect(abb.net_return_pct).toBeCloseTo(abb.net_unrealized_paise/100000*100);expect(abb.break_even).toBe(breakEvenPaise(10000n,10,2500,true)/100);
  // The 9 Nov bar is after the evaluated bar and invalid: excluded. Level = lowest low of the 10 bars to 6 Nov.
  expect(acc.exit_plan.stop).toMatchObject({kind:'rule',defined:false,level:Math.min(...closes.slice(-10))-1,bar_date:'2026-11-06'});
  expect(acc.entry_charges_paise).toBeNull();expect(acc.charges_paise).toBeNull();expect(acc.break_even).toBeNull();expect(acc.charges_note).toMatch(/No fee row/);
  expect(cup.exit_plan.stop.level).toBeNull();expect(cup.exit_plan.stop.rule).toBe(EXIT_RULES[3]);expect(cup.gross_pnl_paise).toBeNull();expect(cup.net_unrealized_paise).toBeNull();
  expect(JSON.stringify(r)).not.toMatch(/checklist_json|components_json|data_json|details_json/);
 });
 it('pending orders: exits carry the S7 stop and held qty; entries carry policy notional context, never a fake qty/limit',async()=>{
  const db=await fixture();await pos(db,7,'ABB-EQ',4,100,110,105);
  await db.execute({sql:'INSERT INTO paper_signals VALUES(?,?,?,?,?,?,?,?,?,?)',args:['paper_v1','x1',7,'ABB-EQ','2026-11-06','SELL','pending','daily_exit:2026-11-06T09:15:00+05:30','2026-11-06T16:00:00+05:30','2026-11-09']});
  await db.execute({sql:'INSERT INTO paper_signals VALUES(?,?,?,?,?,?,?,?,?,?)',args:['paper_v1','b1',2,'ACC-EQ','2026-11-06','BUY','pending','daily_entry:beta_momentum_v1','2026-11-06T16:00:00+05:30','2026-11-09']});
  await db.execute({sql:'INSERT INTO paper_qualifier_states VALUES(?,?,?,?,?,?,?,?)',args:['paper_v1','2026-11-06',2,'ACC-EQ',1,'{}','{"reference_close":2510.5,"reference_bar":"2026-11-06"}','2026-11-06T16:00:00+05:30']});
  await db.execute({sql:'INSERT INTO paper_quote_audit(namespace,symbol,observed_at,quote_at,price,accepted,reason,source) VALUES(?,?,?,?,?,?,?,?)',args:['paper_v1','ACC-EQ','2026-11-09T09:20:00+05:30','2026-11-09T09:19:59+05:30',2520,1,'fresh','FYERS_QUOTES']});
  const r=await read(db,{view:'signals',status:'pending'});const x=r.rows.find(v=>v.id==='x1'),b=r.rows.find(v=>v.id==='b1');
  expect(x.exit_plan.stop.level).toBe(trailLevel(110));expect(x.position.qty).toBe(4);
  expect(b.entry_plan).toMatchObject({signal_close:2510.5,target:{defined:false}});expect(b.entry_plan.target_notional_inr).toBeGreaterThan(0);expect(b).not.toHaveProperty('qty');expect(b.current_price).toBe(2520);
 });
 it('closed stop exits show the threshold from the risk_hit event; others show none and no invented target',async()=>{
  const db=await fixture();
  const fill=(id,sig,side,price,at,reason)=>db.execute({sql:'INSERT INTO paper_fills(id,namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',args:[id,'paper_v1',sig,7,'ABB-EQ',side,2,price,at,at,reason,side==='SELL'?(price-100)*2:0,0]});
  await fill(1,'b1','BUY',100,'2026-11-06T09:15:00+05:30','daily_entry:beta_momentum_v1');await fill(2,'s1','SELL',97.2,'2026-11-09T10:00:00+05:30','trail_touch');
  await db.execute({sql:'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,strategy,symbol,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1','e1','r1','2026-11-09','2026-11-09T09:59:59+05:30','2026-11-09T09:59:59+05:30','risk_hit','INFO','ok',7,'ABB-EQ','{"price":97.2,"threshold":97.2,"reason":"trail_touch","secret":"x"}']});
  await db.execute({sql:'INSERT INTO paper_fill_costs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',1,'2026-11-06',7,'ABB-EQ','INE117A01022','BUY',20000,'P','{}',30,'2026-11-06T09:15:00+05:30',-20000,-20030]});
  await db.execute({sql:'INSERT INTO paper_fill_costs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',args:['paper_v1',2,'2026-11-09',7,'ABB-EQ','INE117A01022','SELL',19440,'P','{}',40,'2026-11-06T09:15:00+05:30',19440,19400]});
  const r=await read(db,{view:'trades'});const t=r.rows[0];
  expect(t.exit_reason).toBe('trail_touch');expect(t.exit_plan.stop).toMatchObject({hit:true,threshold:97.2,rule:'trail_touch'});expect(t.exit_plan.target.defined).toBe(false);expect(t.exit_plan.risk_reward.value).toBeNull();
  expect(t.break_even).toBe(closedBreakEven(100,2,70));expect(t.net_pnl_pct).toBeCloseTo(-630/20000*100);expect(t.entry_reason).toBe('daily_entry:beta_momentum_v1');expect(JSON.stringify(t)).not.toContain('secret');
 });
});
