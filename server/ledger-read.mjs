// Read-only ledger tab adapters for the Atlas Desk "Browse the paper ledger" section.
// Fixed SELECT statements only (the caller's select() refuses anything else). Values are
// bound, never interpolated. Raw JSON payloads (details_json, data_json, components_json)
// are parsed server-side and only whitelisted numeric/enum fields are returned.
import capital from '../automation/paper-trading/capital_policy.json' with {type:'json'};
import instruments from './instruments.json' with {type:'json'};

export const SIGNAL_STATUSES=['pending','filled','expired','blocked_prefilter','ignored_state','ignored_stale_position','ignored_same_bar','shadow_closed'];
export const RUN_KINDS=['monitor','scan','bootstrap','probe','init','pnl','status'];
export const FEE_KEYS=['brokerage','exchange','sebi','ipft','clearing','stt','stamp','gst','dp','dp_gst'];
export const LEDGER_VIEWS=['trades','costs','credits','dataaudit','events'];
export const STRATEGY_VIEWS=['fills','positions','signals','trades','costs','credits','events'];

const num=v=>{if(v===null||v===undefined||v==='')return null;const n=Number(v);return Number.isFinite(n)?n:null;};
const int=v=>{const n=num(v);return n!==null&&Number.isSafeInteger(n)?n:null;};
const word=(v,re=/^[A-Za-z0-9_:.-]{1,80}$/)=>typeof v==='string'&&re.test(v)?v:null;
const iso=v=>typeof v==='string'&&/^\d{4}-\d{2}-\d{2}T[0-9:.]{8,15}(Z|[+-]\d{2}:\d{2})?$/.test(v)?v:null;
const missingTable=e=>/no such (table|column)/i.test(String(e?.message??''));
const todayClause=col=>` AND date(${col},'+5 hours','+30 minutes')=?`;
export const industryOf=symbol=>instruments[symbol]?.industry??null;

export function parseJson(raw){if(typeof raw!=='string')return {};try{const v=JSON.parse(raw);return v&&typeof v==='object'&&!Array.isArray(v)?v:{};}catch{return {};}}

// Fee components in integer paise. Unknown keys are dropped; a corrupt row is "unavailable", never zero.
export function feeBreakdown(raw){
 const d=parseJson(raw);if(!Object.keys(d).length)return null;
 const out={};for(const k of FEE_KEYS)out[k]=int(d[k]);out.total=int(d.total);return out;
}

// Whitelisted run summary. Monitor runs: quotes/fresh_quotes/fills. Scan runs: funnel counts.
export function runSummary(raw){
 const d=parseJson(raw),s={};
 for(const k of ['fresh_quotes','quotes','fills','signals_queued','latest_qualifiers','universe_symbols','frozen_universe_symbols','invalid_rows_excluded','excluded_audit_symbols'])if(int(d[k])!==null)s[k]=int(d[k]);
 if(Array.isArray(d.unavailable_current_symbols))s.unavailable_symbols=d.unavailable_current_symbols.length;
 if(/^\d{4}-\d{2}-\d{2}$/.test(String(d.target_completed_bar??'')))s.target_bar=d.target_completed_bar;
 if(d.prefilter_counts&&typeof d.prefilter_counts==='object')s.prefilter={pass:int(d.prefilter_counts.PASS),fail:int(d.prefilter_counts.FAIL),unavailable:int(d.prefilter_counts.UNAVAILABLE)};
 if(typeof d.screen_only==='boolean')s.screen_only=d.screen_only;
 const st=word(d.status,/^[a-z_]{1,60}$/);if(st)s.detail_status=st;
 return s;
}

// Alert (paper_events) payload whitelist: price/threshold numbers, rule word, quote timestamp.
export function eventSummary(raw){
 const d=parseJson(raw),s={};
 for(const k of ['price','threshold','quantity','fills'])if(num(d[k])!==null)s[k]=num(d[k]);
 for(const k of ['reason','rule'])if(word(d[k],/^[A-Za-z0-9_:]{1,64}$/))s[k]=d[k];
 if(iso(d.quote_at))s.quote_at=d.quote_at;
 if(typeof d.target_defined==='boolean')s.target_defined=d.target_defined;
 return s;
}

async function costsFor(select,namespace,ids){
 const list=[...new Set(ids.filter(i=>Number.isSafeInteger(Number(i))).map(Number))];
 if(!list.length)return {available:true,map:new Map()};
 try{
  const rows=await select(`SELECT fill_id,turnover_paise,total_paise,gross_cashflow_paise,net_cashflow_paise,policy_version,components_json FROM paper_fill_costs WHERE namespace=? AND fill_id IN (${list.map(()=>'?').join(',')})`,[namespace,...list]);
  return {available:true,map:new Map(rows.map(r=>[Number(r.fill_id),{turnover_paise:int(r.turnover_paise),charges_paise:int(r.total_paise),gross_cashflow_paise:int(r.gross_cashflow_paise),net_cashflow_paise:int(r.net_cashflow_paise),policy_version:word(r.policy_version),fees:feeBreakdown(r.components_json)}]))};
 }catch(e){if(missingTable(e))return {available:false,map:new Map()};throw e;}
}

const page=(options,count,rows,extra={})=>({page:options.page,limit:options.limit,total:Number(count?.[0]?.total??0),rows,...extra});

// Fills, enriched with the fee/cash-flow row of each fill when one exists.
export async function enrichFills(select,namespace,rows){
 const c=await costsFor(select,namespace,rows.map(r=>r.id));
 return {costsAvailable:c.available,rows:rows.map(r=>{const k=c.map.get(Number(r.id));return {...r,qty:int(r.qty),price:num(r.price),realized_pnl:num(r.realized_pnl),cashflow:num(r.cashflow),costs:k??null,industry:industryOf(r.symbol)};})};
}

export async function readLedgerView(options,select,day,publicRows){
 const ns=options.namespace;
 const strat=(col,args)=>{if(options.strategy===null)return '';args.push(options.strategy);return ` AND ${col}=?`;};
 const today=(col,args)=>{if(options.scope!=='today')return '';args.push(day);return todayClause(col);};
 const lim=[options.limit,(options.page-1)*options.limit];
 if(options.view==='trades'){
  // Closed round trips: each SELL fill paired with the latest earlier BUY fill of the same strategy+symbol.
  const args=[ns];const where='s.namespace=? AND s.side=\'SELL\''+strat('s.strategy',args)+today('s.filled_at',args);
  const [count,raw]=await Promise.all([
   select(`SELECT COUNT(*) AS total FROM paper_fills s WHERE ${where}`,args),
   select(`SELECT s.id,s.strategy,s.symbol,s.qty,s.price AS exit_price,s.filled_at AS closed_at,s.quote_at AS exit_quote_at,s.reason AS exit_reason,s.realized_pnl,b.id AS entry_fill_id,b.qty AS entry_qty,b.price AS entry_price,b.filled_at AS opened_at FROM paper_fills s LEFT JOIN paper_fills b ON b.namespace=s.namespace AND b.id=(SELECT b2.id FROM paper_fills b2 WHERE b2.namespace=s.namespace AND b2.strategy=s.strategy AND b2.symbol=s.symbol AND b2.side='BUY' AND b2.id<s.id ORDER BY b2.id DESC LIMIT 1) WHERE ${where} ORDER BY s.filled_at DESC,s.id DESC LIMIT ? OFFSET ?`,[...args,...lim])]);
  const c=await costsFor(select,ns,raw.flatMap(r=>[r.id,r.entry_fill_id]).filter(v=>v!==null&&v!==undefined));
  const rows=publicRows(raw.map(r=>({...r,reason:r.exit_reason}))).map(r=>{
   const {reason,...rest}=r;const exitCost=c.map.get(Number(r.id))??null,entryCost=r.entry_fill_id===null||r.entry_fill_id===undefined?null:c.map.get(Number(r.entry_fill_id))??null;
   const entry=num(r.entry_price),exit=num(r.exit_price),qty=int(r.qty),opened=Date.parse(r.opened_at),closed=Date.parse(r.closed_at);
   const matched=entry!==null&&int(r.entry_qty)===qty;
   const charges=exitCost?.charges_paise!=null&&entryCost?.charges_paise!=null?exitCost.charges_paise+entryCost.charges_paise:null;
   const net=matched&&exitCost?.net_cashflow_paise!=null&&entryCost?.net_cashflow_paise!=null?exitCost.net_cashflow_paise+entryCost.net_cashflow_paise:null;
   return {...rest,exit_reason:reason,qty,entry_price:entry,exit_price:exit,realized_pnl:num(r.realized_pnl),entry_matched:entry!==null,
    pnl_pct:matched&&entry>0&&exit!==null?(exit-entry)/entry*100:null,hold_hours:Number.isFinite(opened)&&Number.isFinite(closed)?(closed-opened)/3600000:null,
    charges_paise:charges,net_pnl_paise:net,entry_costs:entryCost,exit_costs:exitCost,industry:industryOf(r.symbol)};});
  return page(options,count,rows,{costsAvailable:c.available,pairing:'Each exit is paired with the latest earlier BUY fill of the same strategy and stock. Unmatched exits show no entry.'});
 }
 if(options.view==='costs'){
  // Transaction log with the running per-sleeve cash: sleeve capital + cumulative net cash flow.
  const args=[ns];const inner="SELECT c.fill_id,c.day,c.strategy,c.symbol,c.side,c.turnover_paise,c.total_paise,c.gross_cashflow_paise,c.net_cashflow_paise,c.policy_version,SUM(c.net_cashflow_paise) OVER (PARTITION BY c.strategy ORDER BY c.fill_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_net_paise,f.filled_at,f.qty,f.price FROM paper_fill_costs c LEFT JOIN paper_fills f ON f.namespace=c.namespace AND f.id=c.fill_id WHERE c.namespace=?";
  let where='1=1';if(options.strategy!==null){where+=' AND t.strategy=?';args.push(options.strategy);}if(options.scope==='today'){where+=' AND t.day=?';args.push(day);}
  try{
   const [count,raw]=await Promise.all([select(`SELECT COUNT(*) AS total FROM (${inner}) t WHERE ${where}`,args),select(`SELECT * FROM (${inner}) t WHERE ${where} ORDER BY t.fill_id DESC LIMIT ? OFFSET ?`,[...args,...lim])]);
   const sleeve=Math.round(capital.sleeve_capital_inr*100);
   const rows=raw.map(r=>({fill_id:int(r.fill_id),day:r.day,strategy:int(r.strategy),symbol:r.symbol,side:r.side==='SELL'?'SELL':'BUY',qty:int(r.qty),price:num(r.price),filled_at:r.filled_at??null,turnover_paise:int(r.turnover_paise),charges_paise:int(r.total_paise),gross_cashflow_paise:int(r.gross_cashflow_paise),net_cashflow_paise:int(r.net_cashflow_paise),policy_version:word(r.policy_version),sleeve_cash_after_paise:int(r.running_net_paise)===null?null:sleeve+int(r.running_net_paise)}));
   return page(options,count,rows,{available:true,sleeveCapitalPaise:sleeve,balanceNote:'Sleeve cash after each fill = sleeve capital + running net cash flow of that sleeve. Dividend or other cash credits are listed separately and are not folded into this running figure.'});
  }catch(e){if(missingTable(e))return page(options,[{total:0}],[],{available:false,reason:'fee_ledger_unavailable'});throw e;}
 }
 if(options.view==='credits'){
  const args=[ns];const where='namespace=?'+strat('strategy',args)+today('recorded_at',args);
  try{
   const [count,raw]=await Promise.all([select(`SELECT COUNT(*) AS total FROM paper_cash_credits WHERE ${where}`,args),select(`SELECT strategy,symbol,ex_date,kind,amount_paise,recorded_at FROM paper_cash_credits WHERE ${where} ORDER BY recorded_at DESC,symbol LIMIT ? OFFSET ?`,[...args,...lim])]);
   return page(options,count,raw.map(r=>({strategy:int(r.strategy),symbol:r.symbol,ex_date:r.ex_date,kind:word(r.kind,/^[a-z_]{1,40}$/)??'credit',amount_paise:int(r.amount_paise),recorded_at:r.recorded_at})),{available:true});
  }catch(e){if(missingTable(e))return page(options,[{total:0}],[],{available:false,reason:'cash_credits_unavailable'});throw e;}
 }
 if(options.view==='dataaudit'){
  const args=[ns];const where='namespace=?'+today('observed_at',args);
  const [count,raw]=await Promise.all([select(`SELECT COUNT(*) AS total FROM paper_data_audit WHERE ${where}`,args),select(`SELECT symbol,bar_date,reason,observed_at FROM paper_data_audit WHERE ${where} ORDER BY observed_at DESC,symbol,bar_date LIMIT ? OFFSET ?`,[...args,...lim])]);
  return page(options,count,publicRows(raw),{available:true});
 }
 if(options.view==='events'){
  // Engine alerts: risk hits, blocked paper orders and WARN/ERROR/CRITICAL audit events.
  const args=[ns];const where="namespace=? AND (event_type IN ('risk_hit','paper_order_blocked') OR (event_type='paper_order_result' AND outcome='blocked') OR severity IN ('WARN','ERROR','CRITICAL'))"+strat('strategy',args)+today('source_at',args);
  try{
   const [count,raw]=await Promise.all([select(`SELECT COUNT(*) AS total FROM paper_events WHERE ${where}`,args),select(`SELECT event_type,severity,outcome,strategy,symbol,bar_date,source_at,data_json FROM paper_events WHERE ${where} ORDER BY source_at DESC LIMIT ? OFFSET ?`,[...args,...lim])]);
   return page(options,count,raw.map(r=>({event_type:word(r.event_type,/^[a-z_]{1,48}$/)??'event',severity:['INFO','WARN','ERROR','CRITICAL'].includes(r.severity)?r.severity:'INFO',outcome:word(r.outcome,/^[A-Za-z0-9_:]{1,80}$/)??'unclassified',strategy:int(r.strategy),symbol:r.symbol??null,bar_date:r.bar_date??null,source_at:r.source_at,...eventSummary(r.data_json)})),{available:true});
  }catch(e){if(missingTable(e))return page(options,[{total:0}],[],{available:false,reason:'audit_events_unavailable'});throw e;}
 }
 throw new Error('BAD_REQUEST');
}

// Engine-health strip for the Engine runs tab: latest run of each kind and the newest accepted quote.
export async function runHealth(select,namespace){
 const [latest,quote]=await Promise.all([
  select('SELECT r.kind,r.at,r.status,(SELECT COUNT(*) FROM paper_runs c WHERE c.namespace=r.namespace AND c.kind=r.kind) AS runs FROM paper_runs r WHERE r.namespace=? AND r.id IN (SELECT MAX(id) FROM paper_runs WHERE namespace=? GROUP BY kind) ORDER BY r.at DESC',[namespace,namespace]),
  select('SELECT MAX(quote_at) AS last_accepted_quote_at,COUNT(DISTINCT symbol) AS quoted_symbols FROM paper_quote_audit WHERE namespace=? AND accepted=1',[namespace])
 ]);
 return {latestByKind:latest,lastAcceptedQuoteAt:quote[0]?.last_accepted_quote_at??null,quotedSymbols:Number(quote[0]?.quoted_symbols??0)};
}
