// Stop / target / exit-rule detail for open paper positions and pending orders. Read-only.
// The paper engine stores no ticket stop or target columns. What it does have:
//  - Strategy 07: a real price stop, trail_level(high_water) = round(TRAIL * high_water, 2), with
//    TRAIL from config.json (the same value engine.py loads) and high_water from paper_positions.
//  - Strategies 01-06 and the Strategy 07 SMA50 leg: completed-bar exit rules. A price level is
//    shown only when it follows exactly from completed daily bars already stored in daily_ohlcv
//    (same validity filter and indicator-excluded sessions as the engine) or from the indicator
//    values the latest scan stored for that position. Otherwise the level is null with a reason.
//  - No strategy defines a profit target, so no target price and no risk:reward is ever produced.
// Fixed SELECTs only, values bound. Raw checklist_json is parsed here; only whitelisted numbers leave.
import engine from '../automation/paper-trading/config.json' with {type: 'json'};
import capital from '../automation/paper-trading/capital_policy.json' with {type: 'json'};
import charges from '../automation/paper-trading/charge_policy.json' with {type: 'json'};
import {estimate} from '../automation/paper-trading/fees.mjs';
import {engineExitRule, exitRule, NO_TARGET, RR_UNAVAILABLE, STOP_STRATEGIES, TRAIL_FRACTION, TRAIL_PCT_BELOW, trailLevel} from '../src/exitRules.js';

const EXCLUDED_SESSIONS = new Set(Object.keys(engine.indicator_excluded_sessions ?? {}));
const num = v => { if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null; const n = Number(v); return Number.isFinite(n) ? n : null; };
const missing = e => /no such (table|column)/i.test(String(e?.message ?? ''));
const isDay = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v);
const day = v => (isDay(v) ? v.slice(0, 10) : null);
const r2 = v => (v === null ? null : Math.round(v * 100) / 100);
const key = (strategy, symbol) => `${Number(strategy)}|${symbol}`;
const parse = raw => { if (typeof raw !== 'string') return {}; try { const v = JSON.parse(raw); return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; } catch { return {}; } };

// Latest-scan indicator values that may leave the server, per strategy (all numeric).
export const INDICATOR_FIELDS = Object.freeze({
  1: [], 2: ['supertrend', 'final_lower', 'final_upper', 'direction', 'atr10'], 3: ['rsi14', 'sma5', 'sma200'], 4: ['ema20', 'ema50', 'sma200'],
  5: ['bb_middle', 'bb_lower', 'bb_upper', 'sma200'], 6: ['macd', 'signal', 'histogram', 'sma50'], 7: ['sma50', 'sma200', 'high252'],
});
export const PRODUCT = Object.freeze({type: 'Delivery', note: `Overnight (delivery) paper holding; charges modelled by ${charges.version}.`});
export const STOP_EXECUTION = 'Checked on verified quotes: an open at or below the stop exits at the open (trail_gap); otherwise a session low touching it exits at the stop (trail_touch).';

// engine.py valid-bar filter: positive finite OHLC, high >= max(open, close, low), low <= min(open, close, high).
export function validBar(b) {
  const o = num(b.open), h = num(b.high), l = num(b.low), c = num(b.close);
  if ([o, h, l, c].some(v => v === null || v <= 0)) return false;
  return h >= Math.max(o, c, l) && l <= Math.min(o, c, h);
}
// Completed bars the engine's indicators use: valid, not an indicator-excluded session, up to the bar the engine last evaluated.
export function indicatorBars(bars, throughBar) {
  return bars.filter(b => isDay(b.trade_date) && (!throughBar || day(b.trade_date) <= throughBar) && !EXCLUDED_SESSIONS.has(day(b.trade_date)) && validBar(b))
    .sort((a, b) => day(a.trade_date).localeCompare(day(b.trade_date)));
}
const closes = (bars, n) => bars.slice(-n).map(b => num(b.close));
const mean = xs => xs.reduce((a, v) => a + v, 0) / xs.length;

// Price the next completed close must cross for a completed-bar rule to exit, when it follows exactly
// from stored bars. SMA rules are solved for the next close: c > (sum of last n-1 + c)/n  <=>  c > mean(last n-1).
export function ruleLevel(strategy, bars, snapshot) {
  const s = Number(strategy), last = bars.at(-1), bar = last ? day(last.trade_date) : null;
  const v = snapshot?.values ?? {};
  const out = (level, label, source, extra = {}) => ({level: level === null ? null : r2(level), label, bar_date: source === 'scan' ? snapshot?.bar_date ?? null : bar, source: source === 'scan' ? 'Indicator values stored by the latest scan for this position' : source === 'bars' ? 'Completed daily bars stored in daily_ohlcv (engine validity filter, indicator-excluded sessions removed)' : null, ...extra});
  const none = (label, reason) => ({level: null, label, bar_date: null, source: null, unavailable_reason: reason});
  const need = n => `Needs ${n} stored completed bars up to the last evaluated bar; have ${bars.length}.`;
  if (s === 1) {
    if (bars.length < 10) return none('Exit if the next close is below the prior 10-session low', need(10));
    return out(Math.min(...bars.slice(-10).map(b => num(b.low))), 'Exit if the next completed close is below this level (lowest low of the last 10 completed sessions)', 'bars', {exact: true});
  }
  if (s === 3) {
    if (bars.length < 4) return none('Exit if the next close is above SMA5 (or RSI14 rises above 60)', need(4));
    return out(mean(closes(bars, 4)), 'Exit if the next completed close is above this level (it would then be above SMA5). RSI14 > 60 can exit sooner.', 'bars', {exact: true, rsi14: num(v.rsi14)});
  }
  if (s === 5) {
    if (bars.length < 19) return none('Exit if the next close reaches the Bollinger middle band', need(19));
    return out(mean(closes(bars, 19)), 'Exit if the next completed close is at or above this level (it would then reach the Bollinger middle band, SMA20)', 'bars', {exact: true});
  }
  if (s === 7) {
    if (bars.length < 49) return none('Exit if the next close is below SMA50', need(49));
    return out(mean(closes(bars, 49)), 'SMA50 leg: exit if the next completed close is below this level (it would then be below SMA50)', 'bars', {exact: true});
  }
  if (s === 2) {
    const st = num(v.supertrend), dir = num(v.direction);
    if (st === null || dir === null) return none('Exit on a green-to-red Supertrend flip', snapshot ? 'The latest scan stored no Supertrend value for this position.' : 'No stored scan indicator values for this position (the scan stores them only when the stock passes the prefilter).');
    if (dir !== -1) return out(null, 'Supertrend is already red at the last evaluated bar; the exit is queued or pending.', 'scan', {exact: false});
    return out(st, 'Current Supertrend line (green). A completed close below the line as updated at that close flips it red; while it stays green the updated line is never below this value.', 'scan', {exact: false});
  }
  if (s === 4) return none('Exit when EMA20 crosses below EMA50', 'A crossover has no single price level; the stored EMA values are shown instead.');
  if (s === 6) return none('Exit when MACD crosses below its signal line', 'A crossover has no single price level; the stored MACD values are shown instead.');
  return none('Exit rule', 'Unknown strategy.');
}

// Full exit plan for one open position. mark/entry/qty/high_water are the already-validated position fields.
export function exitPlan(p, {bars = [], snapshot = null, barsAvailable = true} = {}) {
  const s = Number(p.strategy), rule = exitRule(s);
  const rules = {strategy: s, exit_rule: rule, engine_rule: engineExitRule(s)};
  const target = {defined: false, label: NO_TARGET, exit_rule: rule, why: 'The strategy exits on its rule; target_defined = false in the engine.'};
  const rr = {value: null, reason: RR_UNAVAILABLE};
  const indicators = snapshot ? {bar_date: snapshot.bar_date, scanned_at: snapshot.scanned_at, values: snapshot.values} : null;
  const level = barsAvailable ? ruleLevel(s, bars, snapshot) : {level: null, label: rule, bar_date: null, source: null, unavailable_reason: 'Completed daily bars (daily_ohlcv) are not readable by the dashboard.'};
  let stop;
  if (STOP_STRATEGIES.includes(s)) {
    const hw = num(p.high_water), lvl = trailLevel(hw), mark = num(p.mark_price), entry = num(p.entry_price), qty = num(p.qty);
    stop = lvl === null
      ? {defined: true, kind: 'trail', level: null, fraction: TRAIL_FRACTION, pct_below: TRAIL_PCT_BELOW, high_water: hw, unavailable_reason: 'No completed-close high-water is stored for this position, so the trail level cannot be computed.'}
      : {defined: true, kind: 'trail', level: lvl, fraction: TRAIL_FRACTION, pct_below: TRAIL_PCT_BELOW, high_water: hw,
        label: `Strategy 07 trail: ${TRAIL_FRACTION} × completed-close high-water ${hw}`, basis: 'Highest completed daily close since the actual entry (initialised at the entry fill); updated only after each completed close.',
        execution: STOP_EXECUTION,
        distance_to_mark: mark !== null ? r2(mark - lvl) : null, distance_pct: mark !== null && mark > 0 ? (mark - lvl) / mark * 100 : null, mark_state: p.mark_state ?? null,
        risk_per_share: entry !== null ? r2(entry - lvl) : null, risk_total: entry !== null && qty !== null ? r2((entry - lvl) * qty) : null,
        open_risk_to_stop: mark !== null && qty !== null ? r2((mark - lvl) * qty) : null, locked_in: entry !== null ? lvl >= entry : null};
  } else {
    stop = {defined: false, kind: 'rule', level: level.level, rule, label: level.label, bar_date: level.bar_date, source: level.source, exact: level.exact ?? false, rsi14: level.rsi14 ?? null,
      unavailable_reason: level.level === null ? level.unavailable_reason ?? 'No price level for this rule.' : null,
      why: 'No fixed rupee stop is stored or defined for this strategy; it exits on its completed-bar rule.'};
  }
  return {...rules, stop, rule_level: STOP_STRATEGIES.includes(s) ? level : null, target, risk_reward: rr, indicators, product: PRODUCT};
}

// Entry context for a pending BUY: policy sizing and what the stop/target will be after the fill.
export function entryPlan(signal, snapshotPlan) {
  const s = Number(signal.strategy);
  return {strategy: s, exit_rule: exitRule(s), engine_rule: engineExitRule(s), target: {defined: false, label: NO_TARGET, exit_rule: exitRule(s)},
    target_notional_inr: num(capital.target_notional_inr), sizing: 'Whole shares = floor(target notional ÷ fill price), sized only at the fill from a verified quote; no quantity or limit price exists before the fill.',
    stop_after_entry: STOP_STRATEGIES.includes(s) ? `Trail stop initialised at ${TRAIL_FRACTION} × the actual entry fill price, then ${TRAIL_FRACTION} × the highest completed close.` : `No fixed stop; exits on: ${exitRule(s)}.`,
    signal_close: num(snapshotPlan?.reference_close), signal_close_bar: day(snapshotPlan?.reference_bar) ?? null, product: PRODUCT};
}

// Parse one qualifier-state checklist into the whitelisted indicator snapshot.
export function snapshotOf(row) {
  const s = Number(row.strategy), obs = parse(row.checklist_json).observed_values;
  if (!obs || typeof obs !== 'object') return null;
  const values = {};
  for (const k of INDICATOR_FIELDS[s] ?? []) { const v = num(obs[k]); if (v !== null) values[k] = v; }
  return {bar_date: day(row.bar_date), scanned_at: typeof row.scanned_at === 'string' ? row.scanned_at : null, values};
}

const IN = n => Array.from({length: n}, () => '?').join(',');
async function tryRead(select, sql, args) { try { return {ok: true, rows: await select(sql, args)}; } catch (e) { if (missing(e)) return {ok: false, rows: []}; throw e; } }

// Inputs for exitPlan for a set of positions: watermark bar, stored bars, latest scan snapshot.
export async function loadExitInputs(select, namespace, positions) {
  const symbols = [...new Set(positions.map(p => p.symbol).filter(v => typeof v === 'string'))].slice(0, 200);
  const empty = {barsAvailable: false, bars: new Map(), watermarks: new Map(), snapshots: new Map()};
  if (!symbols.length) return {...empty, barsAvailable: true};
  const [wm, snaps] = await Promise.all([
    tryRead(select, `SELECT strategy,symbol,bar_date FROM paper_watermarks WHERE namespace=? AND symbol IN (${IN(symbols.length)})`, [namespace, ...symbols]),
    tryRead(select, `SELECT q.strategy,q.symbol,q.bar_date,q.scanned_at,q.checklist_json FROM paper_qualifier_states q WHERE q.namespace=? AND q.symbol IN (${IN(symbols.length)}) AND q.bar_date=(SELECT MAX(m.bar_date) FROM paper_qualifier_states m WHERE m.namespace=q.namespace AND m.strategy=q.strategy AND m.symbol=q.symbol)`, [namespace, ...symbols]),
  ]);
  const watermarks = new Map(wm.rows.map(r => [key(r.strategy, r.symbol), day(r.bar_date)]));
  const held = new Set(positions.map(p => key(p.strategy, p.symbol)));
  const snapshots = new Map(snaps.rows.filter(r => held.has(key(r.strategy, r.symbol))).map(r => [key(r.strategy, r.symbol), snapshotOf(r)]));
  const through = [...watermarks.values()].filter(Boolean).sort().at(-1);
  if (!through) return {...empty, barsAvailable: true, watermarks, snapshots};
  const from = new Date(Date.parse(through + 'T00:00:00Z') - 160 * 86400000).toISOString().slice(0, 10);
  const b = await tryRead(select, `SELECT symbol,trade_date,open,high,low,close FROM daily_ohlcv WHERE symbol IN (${IN(symbols.length)}) AND trade_date>=? AND trade_date<=? ORDER BY symbol,trade_date LIMIT 40000`, [...symbols, from, through + 'T23:59:59']);
  const bars = new Map();
  for (const r of b.rows) { const list = bars.get(r.symbol) ?? []; list.push(r); bars.set(r.symbol, list); }
  return {barsAvailable: b.ok, bars, watermarks, snapshots};
}

export function planFor(p, inputs) {
  const k = key(p.strategy, p.symbol), through = inputs.watermarks.get(k) ?? null;
  const bars = through ? indicatorBars(inputs.bars.get(p.symbol) ?? [], through) : [];
  const plan = exitPlan(p, {bars, snapshot: inputs.snapshots.get(k) ?? null, barsAvailable: inputs.barsAvailable});
  if (!through && plan.stop.kind === 'rule' && plan.stop.level === null && !plan.stop.unavailable_reason?.startsWith('A crossover')) plan.stop.unavailable_reason = 'No evaluated bar is recorded for this position yet (no scan watermark), so no rule level is computed.';
  return {...plan, last_evaluated_bar: through};
}

// Strategy entry pauses (kill-pause / drawdown). Only whitelisted fields.
export async function pauseStates(select, namespace) {
  const r = await tryRead(select, 'SELECT scope,strategy,cause,paused_at FROM paper_pause_state WHERE namespace=? AND paused=1 LIMIT 20', [namespace]);
  return {available: r.ok, rows: r.rows.map(x => ({scope: x.scope === 'system' ? 'system' : 'strategy', strategy: Number(x.strategy) || 0, cause: typeof x.cause === 'string' && /^[A-Za-z0-9_.]{1,80}$/.test(x.cause) ? x.cause : 'paused', paused_at: typeof x.paused_at === 'string' ? x.paused_at : null}))};
}
export function pauseFor(pauses, strategy) {
  const hit = pauses.rows.find(p => p.scope === 'system') ?? pauses.rows.find(p => p.strategy === Number(strategy));
  return hit ? {paused: true, scope: hit.scope, cause: hit.cause, paused_at: hit.paused_at, effect: 'New entries are recorded as shadow (not booked) while paused; exits still run.'} : pauses.available ? {paused: false} : null;
}

// ---- Charges, break-even and net unrealized for open positions -------------------------------
// Entry charges: the actual fee row(s) of the BUY fill(s) behind the position (paper_fill_costs,
// matched on strategy + symbol + entry_at). Exit charges: the engine's own fee estimator
// (automation/paper-trading/fees.mjs estimate(), the same one the live finance view uses for the
// "net after estimated exit" figure), applied to this position sold alone at the mark. DP charge is
// included when the position was opened before today (an overnight delivery sell). Missing fee row
// or mark -> null, never zero.
const toPaise = v => { const n = num(v); return n === null || n <= 0 ? null : BigInt(Math.round(n * 100)); };
export function exitChargesPaise(pricePaise, qty, overnight) {
  if (pricePaise === null || !Number.isSafeInteger(qty) || qty <= 0) return null;
  try { return estimate({turnover: pricePaise * BigInt(qty), side: 'SELL', prior: 0n, priorBuy: 0n, dp: overnight}).total; } catch { return null; }
}
// Lowest exit price (in paise) at which (exit - entry) * qty - entry charges - exit charges(exit) >= 0.
export function breakEvenPaise(entryPaise, qty, entryChargesPaise, overnight) {
  if (entryPaise === null || !Number.isSafeInteger(qty) || qty <= 0 || !Number.isSafeInteger(entryChargesPaise)) return null;
  const q = BigInt(qty), need = p => { const x = exitChargesPaise(p, qty, overnight); return x === null ? null : (p - entryPaise) * q - BigInt(entryChargesPaise) - BigInt(x); };
  let p = entryPaise + (BigInt(entryChargesPaise) + q - 1n) / q;
  for (let i = 0; i < 200; i++) { const n = need(p); if (n === null) return null; if (n >= 0n) break; p += (-n + q - 1n) / q; }
  while (p > entryPaise) { const n = need(p - 1n); if (n === null || n < 0n) break; p -= 1n; }
  return Number(p);
}
// Break-even exit price of a closed trade given the recorded charges of both fills.
export function closedBreakEven(entry, qty, chargesPaise) {
  const e = num(entry);
  if (e === null || !Number.isSafeInteger(qty) || qty <= 0 || !Number.isSafeInteger(chargesPaise)) return null;
  return Math.ceil(Math.round(e * 100) + chargesPaise / qty) / 100;
}

export async function entryCosts(select, namespace, positions) {
  const syms = [...new Set(positions.map(p => p.symbol))];
  if (!syms.length) return {available: true, map: new Map()};
  const r = await tryRead(select, `SELECT strategy,symbol,entry_at,fill_id,total_paise FROM paper_fill_costs WHERE namespace=? AND side='BUY' AND symbol IN (${IN(syms.length)})`, [namespace, ...syms]);
  const map = new Map();
  for (const x of r.rows) { const k = `${Number(x.strategy)}|${x.symbol}|${x.entry_at}`; const t = Number(x.total_paise); const cur = map.get(k) ?? {paise: 0, fills: []}; cur.paise += Number.isSafeInteger(t) ? t : NaN; cur.fills.push(Number(x.fill_id)); map.set(k, cur); }
  return {available: r.ok, map};
}
// Entry fill / signal behind each position (exact entry_at match), for the Signal column.
export async function entrySignals(select, namespace, positions) {
  const syms = [...new Set(positions.map(p => p.symbol))];
  if (!syms.length) return new Map();
  const rows = await select(`SELECT f.id,f.strategy,f.symbol,f.filled_at,f.signal_id,f.reason,g.bar_date,g.created_at FROM paper_fills f LEFT JOIN paper_signals g ON g.namespace=f.namespace AND g.id=f.signal_id WHERE f.namespace=? AND f.side='BUY' AND f.symbol IN (${IN(syms.length)})`, [namespace, ...syms]);
  const safe = v => (typeof v === 'string' && /^[A-Za-z0-9_:.+-]{1,120}$/.test(v) ? v : null);
  return new Map(rows.map(r => [`${Number(r.strategy)}|${r.symbol}|${r.filled_at}`, {fill_id: Number(r.id), signal_id: safe(r.signal_id), reason: safe(r.reason), bar_date: day(r.bar_date), detected_at: typeof r.created_at === 'string' ? r.created_at : null}]));
}
const istDay = ms => new Date(ms + 19800000).toISOString().slice(0, 10);
export function positionMoney(p, costs, nowMs) {
  const k = `${Number(p.strategy)}|${p.symbol}|${p.entry_at}`, c = costs.map.get(k);
  const qty = Number(p.qty), entry = toPaise(p.entry_price), mark = p.mark_price === null ? null : toPaise(p.mark_price);
  const opened = Date.parse(p.entry_at), overnight = Number.isFinite(opened) ? istDay(opened) < istDay(nowMs) : true;
  const entryCharges = c && Number.isSafeInteger(c.paise) ? c.paise : null;
  const exitEst = mark !== null ? exitChargesPaise(mark, qty, overnight) : null;
  const gross = p.unrealized_pnl === null || p.unrealized_pnl === undefined ? null : Math.round(p.unrealized_pnl * 100);
  const total = entryCharges !== null && exitEst !== null ? entryCharges + exitEst : null;
  const net = gross !== null && total !== null ? gross - total : null;
  const basis = entry !== null && Number.isSafeInteger(qty) ? Number(entry) * qty : null;
  const be = entryCharges !== null ? breakEvenPaise(entry, qty, entryCharges, overnight) : null;
  const why = !costs.available ? 'The fee ledger is not readable.' : entryCharges === null ? 'No fee row is recorded for the entry fill (legacy fill), so charges are unavailable, not zero.' : mark === null ? 'No valid mark, so the exit-charge estimate and net P&L are unavailable.' : null;
  return {gross_pnl_paise: gross, entry_charges_paise: entryCharges, est_exit_charges_paise: exitEst, charges_paise: total, net_unrealized_paise: net,
    net_return_pct: net !== null && basis ? net / basis * 100 : null, break_even: be === null ? null : be / 100, overnight_exit: overnight,
    entry_fee_fills: c?.fills ?? [], charges_note: why ?? `Entry: recorded fee row. Exit: engine estimate (${charges.version}) if sold alone at the mark${overnight ? ', incl. DP charge' : ''}.`};
}
