import {cappedLedger,PUBLIC_LABELS} from './capped-ledger.mjs';
import {readAutomation} from './automation-read.mjs';
import {DEMO_NAMESPACE,DEMO_SELECT} from './demo-read.mjs';
import {createClient} from '@libsql/client/web';
import {loadExitInputs,planFor,pauseStates,pauseFor,entryPlan,entryCosts,entrySignals,positionMoney} from './exit-plan.mjs';
import {readLedgerView,runHealth,runSummary,enrichFills,industryOf,LEDGER_VIEWS,SIGNAL_STATUSES,RUN_KINDS,STRATEGY_VIEWS} from './ledger-read.mjs';
export const NAMESPACE='paper_v1';
export const IST='Asia/Kolkata';
export function istDay(value=new Date()){return new Intl.DateTimeFormat('en-CA',{timeZone:IST,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));}
export function numeric(value){if(value===null||value===undefined||value==='')return null;const n=Number(value);return Number.isFinite(n)?n:null;}
export function ageSeconds(value,now=Date.now()){const d=Date.parse(value);return Number.isFinite(d)?(now-d)/1000:null;}
export function marketState(now=new Date()){
 const day=istDay(now),time=new Intl.DateTimeFormat('en-GB',{timeZone:IST,hour:'2-digit',minute:'2-digit',hour12:false}).format(now),week=new Intl.DateTimeFormat('en-US',{timeZone:IST,weekday:'short'}).format(now);
 if(day<'2026-11-01'||day>'2026-12-31')return {open:false,label:'Calendar review required',calendarValidThrough:'2026-12-31'};
 const session=day==='2026-11-08'?['14:20','15:15']:['09:15','15:30'];const trading=day==='2026-11-08'||(!['Sat','Sun'].includes(week)&&!['2026-11-10','2026-11-24','2026-12-25'].includes(day));
 return {open:trading&&time>=session[0]&&time<session[1],label:trading&&time>=session[0]&&time<session[1]?'NSE session open':'NSE session closed',calendarValidThrough:'2026-12-31'};
}
export function positionView(row,now=Date.now()){
 const qty=numeric(row.qty),entry=numeric(row.entry_price),mark=numeric(row.mark_price),age=ageSeconds(row.mark_at,now);
 const valid=mark!==null&&mark>0;
 const state=!valid?'missing':age===null?'missing':age< -5?'future':age>60?'stale':'fresh';
 return {...row,qty,entry_price:entry,mark_price:valid?mark:null,mark_age_seconds:age,mark_stale:!valid||age===null||age>60||age< -5,mark_state:state,unrealized_pnl:valid&&qty!==null&&entry!==null?(mark-entry)*qty:null,unrealized_pct:valid&&entry>0?(mark-entry)/entry*100:null,cost_basis:qty!==null&&entry!==null?entry*qty:null,market_value:valid&&qty!==null?mark*qty:null,high_water:numeric(row.high_water)};
}
export function publicRows(rows){return rows.map(row=>{const r={...row};if('reason' in r){const known=['trail_gap','trail_touch','pending_exit_carry','daily_entry:beta_momentum_v1','blocked_prefilter_missing_active_pass','daily_entry','observed_LTP_below_close_highwater_stop','market_closed_or_unverified','provider_quote_error','nonpositive_or_missing_price','missing_exchange_timestamp','stale_or_future_quote','quote_outside_verified_session','fresh','unverified_or_closed_session_candle','invalid_Fyers_OHLC','nonpositive_nonfinite_or_inconsistent_OHLC'];r.reason=known.includes(r.reason)||/^daily_entry:[A-Za-z0-9_.-]{1,60}$/.test(r.reason)?r.reason:/^daily_exit:\d{4}-\d{2}-\d{2}T[0-9:.+Z-]{8,40}$/.test(r.reason)?r.reason:'recorded_event';}if('status' in r&&!['blocked_prefilter','pending','filled','ignored_state','ignored_stale_position','ignored_same_bar','shadow_closed','expired','closed','ok','partial','failed','success','noop'].includes(r.status))r.status='recorded';if('kind' in r&&!['monitor','scan','bootstrap','probe','init','pnl','status'].includes(r.kind))r.kind='engine_run';if('source' in r)r.source=r.source==='FYERS_QUOTES'?'FYERS_QUOTES':'market_data';return r;});}
export function parseOptions(query={}){
 const view=query.view??'overview';if(!['overview','fills','positions','signals','runs','quotes','history','demo','analytics','scan','qualifiers','prefilter',...LEDGER_VIEWS].includes(view))throw new Error('BAD_REQUEST');
 const integer=(v,def,max)=>{const n=v===undefined?def:Number(v);if(!Number.isInteger(n)||n<1||n>max)throw new Error('BAD_REQUEST');return n;};
 const page=integer(query.page,1,10000),limit=integer(query.limit,25,100);
 const strategy=query.strategy===undefined||query.strategy===''?null:integer(query.strategy,1,7);
 const scope=query.scope??'all';if(!['all','today'].includes(scope))throw new Error('BAD_REQUEST');
 const symbol=query.symbol;if(view==='history'||view==='qualifiers'&&symbol!==undefined){if(typeof symbol!=='string'||!/^\b[A-Z0-9&_.-]{1,80}$/.test(symbol)||view==='history'&&strategy!==null)throw new Error('BAD_REQUEST');}else if(symbol!==undefined)throw new Error('BAD_REQUEST');
 const refresh=query.refresh;if(refresh!==undefined&&refresh!=='1')throw new Error('BAD_REQUEST');
 if(['scan','analytics','qualifiers'].includes(view)&&scope!=='all'||['scan','analytics','prefilter'].includes(view)&&strategy!==null)throw new Error('BAD_REQUEST');
 if(view==='qualifiers'&&strategy===null)throw new Error('BAD_REQUEST');
 const q=query.q;if(q!==undefined&&(!['qualifiers','prefilter'].includes(view)||typeof q!=='string'||q.length>60||!/^[A-Za-z0-9&_. -]*$/.test(q)))throw new Error('BAD_REQUEST');
 const allowed=new Set(['q','view','page','limit','strategy','scope','symbol','refresh','status','kind','accepted']);if(Object.keys(query).some(k=>!allowed.has(k)))throw new Error('BAD_REQUEST');
 // Tab filters: each is bound to exactly one view and a fixed enum.
 const status=query.status,kind=query.kind,accepted=query.accepted;
 if(status!==undefined&&(view!=='signals'||!SIGNAL_STATUSES.includes(status)))throw new Error('BAD_REQUEST');
 if(kind!==undefined&&(view!=='runs'||!RUN_KINDS.includes(kind)))throw new Error('BAD_REQUEST');
 if(accepted!==undefined&&(view!=='quotes'||!['0','1'].includes(accepted)))throw new Error('BAD_REQUEST');
 if(LEDGER_VIEWS.includes(view)&&strategy!==null&&!STRATEGY_VIEWS.includes(view))throw new Error('BAD_REQUEST');
 return {view,page,limit,strategy,scope,...(q?{q}:{}),...(symbol?{symbol}:{}),...(status?{status}:{}),...(kind?{kind}:{}),...(accepted?{accepted:Number(accepted)}:{}),...(refresh==='1'?{forceRefresh:true}:{})};
}
let client;
export function readConfig(env=process.env){
 const clean=v=>String(v??'').trim().replace(/^(["'])(.*)\1$/s,'$2').trim();
 const namespace=clean(env.PAPER_NAMESPACE)||NAMESPACE,url=clean(env.TURSO_DATABASE_URL),authToken=clean(env.TURSO_AUTH_TOKEN);
 if(namespace!==NAMESPACE)throw new Error('CONFIG_NAMESPACE');
 if(!url)throw new Error('CONFIG_MISSING_URL');if(!authToken)throw new Error('CONFIG_MISSING_TOKEN');
 if(!/^(libsql|https):\/\/[^\s]+$/.test(url))throw new Error('CONFIG_URL_INVALID');
 return {url,authToken};
}
function getClient(){const config=readConfig();return client??=createClient(config);}
export function publicError(error){
 const messages={CONFIG_MISSING_URL:'Server setup needed: TURSO_DATABASE_URL is missing.',CONFIG_MISSING_TOKEN:'Server setup needed: TURSO_AUTH_TOKEN is missing.',CONFIG_NAMESPACE:'Server setup needed: PAPER_NAMESPACE must be paper_v1.',CONFIG_URL_INVALID:'Server setup needed: TURSO_DATABASE_URL is not a valid libsql:// or https:// address.'};
 if(messages[error.message])return {code:error.message,message:messages[error.message]};
 const code=String(error.code??''),message=String(error.message??'');
 if(/UNAUTHORIZED|AUTH_TOKEN|AUTHENTICATION|TOKEN_EXPIRED|FORBIDDEN/.test(code)||/HTTP_STATUS_(401|403)/.test(code)||/\b(401|403)\b|not authorized|invalid auth token|authentication failed/i.test(message))return {code:'DB_AUTH_REJECTED',message:'Turso rejected the server credential. Replace the server-only read token and redeploy.'};
 if(/no such table/i.test(message))return {code:'DB_SCHEMA_MISSING',message:'The configured database does not contain the expected paper tables. Check the server database selection.'};
 return {code:'DATA_UNAVAILABLE',message:'The paper database could not be reached. Check server connectivity or try refreshing shortly.'};
}
// Only fixed SELECT statements below. No client SQL, namespaces, writes or broker access.
export async function readDashboard(options,execute,now=new Date()){
 const run=execute??(async(sql,args)=>(await getClient().execute({sql,args})).rows);
 const select=(sql,args=[])=>{if(!/^SELECT\s/i.test(sql))throw new Error('READ_ONLY_GUARD');return run(sql,args);};
 const day=istDay(now),ns=[NAMESPACE];
 if(['analytics','scan','qualifiers','prefilter'].includes(options.view)){try{return await readAutomation({...options,day},select,now);}catch(error){const e=publicError(error);return {readAt:now.toISOString(),namespace:NAMESPACE,available:false,error:e};}}
 if(options.view==='demo'){if(options.strategy!==null)throw new Error('BAD_REQUEST');const rows=await select(DEMO_SELECT,[DEMO_NAMESPACE]);return {readAt:now.toISOString(),namespace:NAMESPACE,demoNamespace:DEMO_NAMESPACE,excludedFromPerformance:true,rows};}
 if(options.view==='history'){
  const raw=await select('SELECT quote_at,price FROM paper_quote_audit WHERE namespace=? AND symbol=? AND accepted=1 AND price>0 AND quote_at IS NOT NULL ORDER BY quote_at DESC,id DESC LIMIT 60',[NAMESPACE,options.symbol]);
  const seen=new Set();const rows=raw.filter(r=>{if(seen.has(r.quote_at))return false;seen.add(r.quote_at);return numeric(r.price)>0;}).reverse();
  return {readAt:now.toISOString(),namespace:NAMESPACE,symbol:options.symbol,source:'Accepted recorded Fyers quotes',rows};
 }
 if(LEDGER_VIEWS.includes(options.view)){if(options.strategy!==null&&!STRATEGY_VIEWS.includes(options.view))throw new Error('BAD_REQUEST');return {readAt:now.toISOString(),namespace:NAMESPACE,view:options.view,market:marketState(now),...await readLedgerView({...options,namespace:NAMESPACE},select,day,publicRows)};}
 if(options.view!=='overview'){
  const definitions={
   fills:{table:'paper_fills',fields:'id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow',order:'filled_at DESC,id DESC',date:'filled_at'},
   positions:{table:'paper_positions',fields:'strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at',order:'strategy,symbol',date:'entry_at'},
   signals:{table:'paper_signals',fields:'id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date',order:'created_at DESC,id DESC',date:'created_at'},
   runs:{table:'paper_runs',fields:'kind,at,status,details_json',order:'at DESC,id DESC',date:'at'},
   quotes:{table:'paper_quote_audit',fields:'symbol,observed_at,quote_at,price,accepted,reason,source',order:'observed_at DESC,id DESC',date:'observed_at'}
  };
  const d=definitions[options.view],args=[...ns];let where='namespace=?'+(options.view==='positions'?' AND qty>0':'');
  if(options.strategy!==null){if(!['fills','positions','signals'].includes(options.view))throw new Error('BAD_REQUEST');where+=' AND strategy=?';args.push(options.strategy);}
  if(options.status!==undefined){where+=' AND status=?';args.push(options.status);}
  if(options.kind!==undefined){where+=' AND kind=?';args.push(options.kind);}
  if(options.accepted!==undefined){where+=' AND accepted=?';args.push(options.accepted);}
  if(options.scope==='today'){where+=` AND date(${d.date},'+5 hours','+30 minutes')=?`;args.push(day);}
  const pageArgs=[...args,options.limit,(options.page-1)*options.limit];
  const rowsSql=fields=>`SELECT ${fields} FROM ${d.table} WHERE ${where} ORDER BY ${d.order} LIMIT ? OFFSET ?`;
  // Newer engine columns (pending_exit on positions, execution_path on fills) are read when present; older databases fall back to the base fields.
  const extra={positions:',pending_exit,pending_exit_since',fills:',execution_path'}[options.view];
  const readRows=async()=>{if(!extra)return select(rowsSql(d.fields),pageArgs);try{return await select(rowsSql(d.fields+extra),pageArgs);}catch(e){if(/no such column/i.test(String(e?.message??'')))return select(rowsSql(d.fields),pageArgs);throw e;}};
  const [counts,rows]=await Promise.all([select(`SELECT COUNT(*) AS total FROM ${d.table} WHERE ${where}`,args),readRows()]);
  const base={readAt:now.toISOString(),namespace:NAMESPACE,view:options.view,market:marketState(now),page:options.page,limit:options.limit,total:Number(counts[0].total)};
  if(options.view==='positions'){
   // Stop / target / exit rule per position: S7 trail = trail_level(high_water) from config TRAIL; S1-6 rule levels only from stored completed bars.
   const views=rows.map(r=>positionView(r,now.getTime()));const [inputs,pauses,costs,entries]=await Promise.all([loadExitInputs(select,NAMESPACE,views),pauseStates(select,NAMESPACE),entryCosts(select,NAMESPACE,views),entrySignals(select,NAMESPACE,views)]);
   return {...base,pausesAvailable:pauses.available,rows:views.map(v=>{const {pending_exit,pending_exit_since,...p}=v;return {...p,industry:industryOf(v.symbol),pending_exit:pending_exit===undefined||pending_exit===null?null:Number(pending_exit)===1,pending_exit_since:typeof pending_exit_since==='string'?pending_exit_since:null,exit_plan:planFor(v,inputs),entry_pause:pauseFor(pauses,v.strategy),...positionMoney(v,costs,now.getTime()),entry_signal:entries.get(`${Number(v.strategy)}|${v.symbol}|${v.entry_at}`)??null};})};
  }
  if(options.view==='runs'){const health=await runHealth(select,NAMESPACE);return {...base,health:{...health,latestByKind:publicRows(health.latestByKind)},rows:publicRows(rows.map(({details_json,...r})=>({...r,summary:runSummary(details_json)})))};}
  if(options.view==='fills'){const e=await enrichFills(select,NAMESPACE,publicRows(rows));return {...base,...e};}
  if(options.view==='signals'){
   // Attach the fill (qty/price/time) that executed each signal, when one exists.
   const ids=rows.map(r=>r.id).filter(Boolean);let fills=[];
   if(ids.length)fills=await select(`SELECT signal_id,qty,price,filled_at FROM paper_fills WHERE namespace=? AND signal_id IN (${ids.map(()=>'?').join(',')})`,[NAMESPACE,...ids]);
   const byId=new Map(fills.map(f=>[f.signal_id,f]));
   // Pending orders: exits carry the held position's current stop / rule level; entries carry the policy sizing context and the signal-bar close.
   // Current price for each order's stock: the latest accepted recorded quote (never fetched live here).
   const qs=[...new Set(rows.map(r=>r.symbol))];const lastQuote=new Map();
   if(qs.length)for(const q of await select(`SELECT q.symbol,q.price,q.quote_at FROM paper_quote_audit q WHERE q.namespace=? AND q.accepted=1 AND q.price>0 AND q.symbol IN (${qs.map(()=>'?').join(',')}) AND q.id=(SELECT MAX(m.id) FROM paper_quote_audit m WHERE m.namespace=q.namespace AND m.symbol=q.symbol AND m.accepted=1 AND m.price>0)`,[NAMESPACE,...qs]))lastQuote.set(q.symbol,{price:numeric(q.price),quote_at:q.quote_at??null});
   const pend=rows.filter(r=>r.status==='pending');let held=[],plans=new Map(),closes=new Map(),pauses={available:false,rows:[]};
   if(pend.length){
    const sells=pend.filter(r=>r.side==='SELL'),buys=pend.filter(r=>r.side==='BUY');
    if(sells.length){const syms=[...new Set(sells.map(r=>r.symbol))];held=(await select(`SELECT strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at FROM paper_positions WHERE namespace=? AND qty>0 AND symbol IN (${syms.map(()=>'?').join(',')})`,[NAMESPACE,...syms])).map(r=>positionView(r,now.getTime()));const inputs=await loadExitInputs(select,NAMESPACE,held);plans=new Map(held.map(p=>[`${p.strategy}|${p.symbol}`,planFor(p,inputs)]));}
    if(buys.length){try{const bs=[...new Set(buys.map(r=>r.symbol))],bd=[...new Set(buys.map(r=>r.bar_date))];const q=await select(`SELECT strategy,symbol,bar_date,plan_json FROM paper_qualifier_states WHERE namespace=? AND symbol IN (${bs.map(()=>'?').join(',')}) AND bar_date IN (${bd.map(()=>'?').join(',')})`,[NAMESPACE,...bs,...bd]);for(const r of q){let plan={};try{plan=JSON.parse(r.plan_json);}catch{/* unreadable plan stays unavailable */}closes.set(`${r.strategy}|${r.symbol}|${r.bar_date}`,plan&&typeof plan==='object'?plan:{});}}catch(e){if(!/no such (table|column)/i.test(String(e?.message??'')))throw e;}}
    pauses=await pauseStates(select,NAMESPACE);
   }
   return {...base,rows:publicRows(rows).map(r=>{const f=byId.get(r.id);const lq=lastQuote.get(r.symbol);const out={...r,fill_qty:f?numeric(f.qty):null,fill_price:f?numeric(f.price):null,filled_at:f?.filled_at??null,current_price:lq?.price??null,current_quote_at:lq?.quote_at??null};
    if(r.status!=='pending')return out;const k=`${r.strategy}|${r.symbol}`;
    if(r.side==='SELL'){const plan=plans.get(k)??null;const p=held.find(x=>`${x.strategy}|${x.symbol}`===k);return {...out,exit_plan:plan,position:p?{qty:p.qty,entry_price:p.entry_price,mark_price:p.mark_price,mark_state:p.mark_state,high_water:p.high_water}:null};}
    return {...out,entry_plan:entryPlan(r,closes.get(`${k}|${r.bar_date}`)),entry_pause:pauseFor(pauses,r.strategy)};})};
  }
  if(options.view==='quotes')return {...base,rows:publicRows(rows).map(r=>{const o=Date.parse(r.observed_at),q=Date.parse(r.quote_at);return {...r,price:numeric(r.price),lag_seconds:Number.isFinite(o)&&Number.isFinite(q)?(o-q)/1000:null};})};
  return {...base,rows:publicRows(rows)};
 }
 const [totals,positions,pnl,performance,recentFills,recentSignals,lastRuns,audit,coverage,closedStats,demoTrades]=await Promise.all([
  select("SELECT COUNT(*) AS filled_orders,COALESCE(SUM(realized_pnl),0) AS realized,COALESCE(SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END),0) AS closed_trades,COALESCE(SUM(CASE WHEN date(filled_at,'+5 hours','+30 minutes')=? THEN 1 ELSE 0 END),0) AS today_fills FROM paper_fills WHERE namespace=?",[day,NAMESPACE]),
  select('SELECT strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at FROM paper_positions WHERE namespace=? AND qty>0 ORDER BY strategy,symbol LIMIT 5000',ns),
  select('SELECT day,as_of,report_json FROM paper_pnl WHERE namespace=? ORDER BY day DESC LIMIT 366',ns),
  select("SELECT strategy,SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) AS closed_trades,SUM(CASE WHEN side='SELL' AND realized_pnl>0 THEN 1 ELSE 0 END) AS wins,COALESCE(SUM(realized_pnl),0) AS realized FROM paper_fills WHERE namespace=? GROUP BY strategy ORDER BY strategy",ns),
  select('SELECT id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl FROM paper_fills WHERE namespace=? ORDER BY filled_at DESC,id DESC LIMIT 12',ns),
  select('SELECT strategy,symbol,bar_date,side,status,reason,created_at,eligible_date FROM paper_signals WHERE namespace=? ORDER BY created_at DESC LIMIT 12',ns),
  select('SELECT kind,at,status FROM paper_runs WHERE namespace=? ORDER BY at DESC,id DESC LIMIT 10',ns),
  select('SELECT symbol,observed_at,quote_at,price,accepted,reason,source FROM paper_quote_audit WHERE namespace=? ORDER BY observed_at DESC,id DESC LIMIT 8',ns),
  select("SELECT (SELECT COUNT(*) FROM paper_watermarks WHERE namespace=?) AS watermarks,(SELECT COUNT(*) FROM paper_data_audit WHERE namespace=?) AS excluded_bars,(SELECT COUNT(*) FROM paper_signals WHERE namespace=? AND status='pending') AS pending_signals,(SELECT COUNT(*) FROM paper_signals WHERE namespace=? AND date(created_at,'+5 hours','+30 minutes')=?) AS signals_today",[NAMESPACE,NAMESPACE,NAMESPACE,NAMESPACE,day]),
  select("SELECT COUNT(*) AS closed_count,SUM(CASE WHEN s.realized_pnl>0 THEN 1 ELSE 0 END) AS wins,AVG(julianday(s.filled_at)-julianday((SELECT b.filled_at FROM paper_fills b WHERE b.namespace=s.namespace AND b.strategy=s.strategy AND b.symbol=s.symbol AND b.side='BUY' AND b.id<s.id ORDER BY b.id DESC LIMIT 1))) AS average_hold_days,COUNT((SELECT b.id FROM paper_fills b WHERE b.namespace=s.namespace AND b.strategy=s.strategy AND b.symbol=s.symbol AND b.side='BUY' AND b.id<s.id ORDER BY b.id DESC LIMIT 1)) AS matched_closures FROM paper_fills s WHERE s.namespace=? AND s.side='SELL'",ns),
  select(DEMO_SELECT,[DEMO_NAMESPACE])
 ]);
 const capped=await cappedLedger(select,NAMESPACE,positions);
 const pos=positions.map(r=>positionView(r,now.getTime()));const unrealized=pos.some(r=>r.unrealized_pnl===null)?null:pos.reduce((a,r)=>a+r.unrealized_pnl,0);
 const reports=pnl.map(r=>{let d={};try{d=JSON.parse(r.report_json);}catch{/* Corrupt snapshots stay unavailable. */}return {day:r.day,asOf:r.as_of,daily:numeric(d.daily_mark_to_market_pnl_INR),total:numeric(d.latest_known_total_pnl_INR),realizedClosuresToday:numeric(d.realized_on_closures_today_INR),valuationIsLive:d.valuation_is_live===true,missingDailyBasis:d.missing_daily_basis_symbols??[]};});
 const latest=reports[0]??null,today=latest?.day===day?latest:null,realized=numeric(totals[0].realized);
 return {readAt:now.toISOString(),namespace:NAMESPACE,day,market:marketState(now),cappedLedger:capped,publicLabels:PUBLIC_LABELS,totals:{filledOrders:Number(totals[0].filled_orders),closedTrades:Number(totals[0].closed_trades),todayFills:Number(totals[0].today_fills),openPositions:pos.length,realized,unrealized,total:unrealized===null?null:realized+unrealized,daily:today?.daily??null,staleMarks:pos.filter(r=>r.mark_stale).length,missingMarks:pos.filter(r=>r.unrealized_pnl===null).length,pendingSignals:Number(coverage[0].pending_signals)},closedStats:{count:Number(closedStats[0]?.closed_count??0),winRate:Number(closedStats[0]?.closed_count)>0?Number(closedStats[0].wins)/Number(closedStats[0].closed_count)*100:null,averageHoldDays:numeric(closedStats[0]?.average_hold_days),matchedClosures:Number(closedStats[0]?.matched_closures??0)},signalsToday:Number(coverage[0].signals_today??0),demoTrades,positions:pos.slice(0,20),performance:Array.from({length:7},(_,i)=>{const row=performance.find(r=>Number(r.strategy)===i+1);const closed=Number(row?.closed_trades??0);return {strategy:i+1,closedTrades:closed,winRate:closed?Number(row.wins)/closed*100:null,realized:numeric(row?.realized??0),openPositions:pos.filter(r=>Number(r.strategy)===i+1).length};}),dailyHistory:reports.reverse(),latestSnapshot:latest,recentFills:publicRows(recentFills),recentSignals:publicRows(recentSignals),lastRuns:publicRows(lastRuns),quoteAudit:publicRows(audit),coverage:coverage[0]};
}
