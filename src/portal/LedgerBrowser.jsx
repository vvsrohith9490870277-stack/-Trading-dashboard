// "Browse the paper ledger": every tab of the reference paper desk (Positions, Orders, History,
// Analytics, Daily, Alerts, Capital, Watchlist) plus the engine audit tabs (Fills, Signals, Engine
// runs, Quote audit, Data audit). Read-only. Every value comes from /api/dashboard; anything the
// ledger does not record is labelled unavailable instead of being estimated.
import {useState} from 'react';
import {Link, stockHref} from './router';
import {paiseToInr, useAllPages, useDashboard} from './data';
import {fmt, inr, istTime, istTimeSec, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {exitRule, NO_TARGET, RR_UNAVAILABLE, TRAIL_FRACTION, TRAIL_PCT_BELOW} from '../exitRules.js';
import {Blank, Callout, KeyValues, Loading, Section, SortTable, Tabs, Tag, Tile} from './ui';
import {Columns} from './charts';
import {allocation, exitSource, fillStats, groupBy, inRange, istDate, monthlyPnl, RANGES, rangeBounds, returnDistribution, sessionSlots, signalKind, stopRisk, tradeFrequency, tradeStats} from './ledger-stats';

export const LEDGER_TABS = [
  ['positions', 'Positions'], ['orders', 'Orders'], ['history', 'History'], ['fills', 'Fills'], ['analytics', 'Analytics'], ['daily', 'Daily'],
  ['alerts', 'Alerts'], ['capital', 'Capital'], ['signals', 'Signals'], ['runs', 'Engine runs'], ['quotes', 'Quote audit'], ['dataaudit', 'Data audit'], ['watchlist', 'Watchlist'],
];
const LIMIT = 25;
export const SIGNAL_STATUSES = ['pending', 'filled', 'expired', 'blocked_prefilter', 'ignored_state', 'ignored_stale_position', 'ignored_same_bar', 'shadow_closed'];
const RUN_KINDS = ['monitor', 'scan', 'bootstrap', 'probe', 'init', 'pnl', 'status'];
const FEE_LABELS = [['brokerage', 'Brokerage'], ['stt', 'STT'], ['exchange', 'Exchange txn'], ['sebi', 'SEBI fee'], ['ipft', 'IPFT'], ['stamp', 'Stamp duty'], ['gst', 'GST'], ['clearing', 'Clearing'], ['dp', 'DP charge'], ['dp_gst', 'DP GST']];

const NA = ({why, children = 'n/a'}) => <span className="ad-muted-cell" title={why}>{children}</span>;
const atSec = v => istTimeSec(v);
const sym = v => <Link className="ad-sym" href={stockHref(v)}>{ticker(v)}</Link>;
const strat = v => (v ? <span className="ad-strat"><i style={{background: STRATEGY_HUES[Number(v) - 1]}}/>{strategyName(v)}</span> : '—');
const money = v => inr(v);
const paise = v => inr(paiseToInr(v));
const pnl = v => (v === null || v === undefined ? '—' : <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>);
const pnlPaise = v => pnl(paiseToInr(v));
const pctCell = v => (v === null || v === undefined ? '—' : <span className={`ad-${tone(v)}`}>{pct(v, 2, true)}</span>);
const code = v => (v ? <code className="ad-code">{v}</code> : '—');
const side = v => <Tag tone={v === 'BUY' ? 'good' : 'bad'}>{v}</Tag>;
const at = v => istTime(v);
const hours = h => (h === null || h === undefined ? '—' : h < 24 ? `${fmt(h, 1)} h` : `${fmt(h / 24, 1)} d`);
const age = s => (s === null || s === undefined ? '' : s < 120 ? `${Math.round(s)}s` : s < 7200 ? `${Math.round(s / 60)}m` : s < 172800 ? `${fmt(s / 3600, 1)}h` : `${fmt(s / 86400, 1)}d`);
const statusTone = v => (['filled', 'ok', 'success', 'closed', 'shadow_closed'].includes(v) ? 'good' : v === 'pending' ? 'info' : ['failed', 'blocked_prefilter'].includes(v) ? 'bad' : ['expired', 'partial'].includes(v) || String(v).startsWith('ignored') ? 'warn' : 'neutral');
const status = v => <Tag tone={statusTone(v)}>{String(v ?? '—').replace(/_/g, ' ')}</Tag>;

function markCell(state, row, market) {
  const a = age(row.mark_age_seconds);
  if (state === 'fresh') return <Tag tone="good" title="Mark is a verified quote at most 60 seconds old">fresh · {a}</Tag>;
  if (state === 'stale') return <Tag tone="warn" title={market?.open ? 'Last verified quote is older than 60 seconds during the session' : 'Market closed: the last session quote is kept and shown as stale'}>stale · {a}{market && !market.open ? ' (market closed)' : ''}</Tag>;
  if (state === 'future') return <Tag tone="bad" title="Quote time is ahead of the server clock">future-dated</Tag>;
  return <Tag tone="bad" title="No valid quote recorded for this position; unrealized P&L is unavailable, not zero">missing</Tag>;
}

// Stop cell: Strategy 07 shows its real trail level; 01-06 show the exit rule, plus the rule's price
// level only when it follows from stored completed bars. Never a stored-ticket stop, never a guess.
function stopCell(plan) {
  const st = plan?.stop;
  if (!st) return <NA why="No exit plan returned for this row.">unavailable</NA>;
  if (st.kind === 'trail') {
    if (st.level === null) return <NA why={st.unavailable_reason}>trail stop unavailable</NA>;
    return <span className="ad-stop" title={`${st.label}. ${st.basis} ${st.execution ?? ''}`}>
      <strong>{inr(st.level)}</strong><small className="ad-muted"> trail {st.fraction} × H {inr(st.high_water)}{st.distance_pct !== null && st.distance_pct !== undefined ? ` · ${pct(st.distance_pct, 1)} below mark` : ''}</small>
    </span>;
  }
  if (st.level !== null && st.level !== undefined) return <span className="ad-stop" title={`${st.label}. Bar ${st.bar_date ?? '—'}. Source: ${st.source}. ${st.why}`}>
    <small className="ad-muted">rule level </small><strong>{inr(st.level)}</strong><small className="ad-muted"> · {plan.exit_rule}</small></span>;
  return <span className="ad-muted-cell" title={`${st.why} ${st.unavailable_reason ?? ''}`}>No fixed stop · {plan.exit_rule}</span>;
}
function targetCell(plan, strategy) {
  const rule = plan?.target?.exit_rule ?? exitRule(strategy);
  return <span className="ad-muted-cell" title={`${plan?.target?.why ?? 'No strategy defines a profit target.'} Exit: ${rule}.`}>{NO_TARGET} · exit: {rule}</span>;
}
const rrCell = () => <NA why={RR_UNAVAILABLE}>— (no target)</NA>;
function riskCell(plan) {
  const st = plan?.stop;
  if (st?.kind !== 'trail' || st.level === null) return <NA why={st?.kind === 'trail' ? st.unavailable_reason : 'Only Strategy 07 has a price stop; rule-based exits have no fixed risk distance.'}>—</NA>;
  return <span title="Risk distance from the average entry to the trail stop, per share and for the position. Negative means the stop is above entry (gain locked in). This is not a risk:reward ratio.">
    {st.risk_per_share === null ? '—' : inr(st.risk_per_share)}<small className="ad-muted">/sh · {st.risk_total === null ? '—' : inr(st.risk_total)}{st.locked_in ? ' · above entry' : ''}</small></span>;
}
function lifecycleCell(row) {
  const parts = [];
  if (row.pending_exit) parts.push(<Tag key="x" tone="warn" title={`Exit carried since ${row.pending_exit_since ?? '—'} (lower circuit); it fills on the next session that allows it.`}>exit pending</Tag>);
  if (row.entry_pause?.paused) parts.push(<Tag key="p" tone="bad" title={`${row.entry_pause.cause} since ${istTime(row.entry_pause.paused_at)}. ${row.entry_pause.effect}`}>entries paused</Tag>);
  if (!parts.length) parts.push(<Tag key="o" tone="good" title={row.pending_exit === null ? 'Open. Exit-carry state is not recorded by this database version.' : 'Open, no exit pending'}>open</Tag>);
  return <span className="ad-tags">{parts}</span>;
}

const READ_ONLY_ACTION = 'Atlas Desk is a read-only view of the paper ledger. Exit, square off, edit, cancel and SL/TP updates are made only by the engine rules, never from this page.';
const actionCell = label => <NA why={READ_ONLY_ACTION}>{label} · read-only</NA>;
// Signal = our strategy identity plus the real entry signal behind the position (no scanner score / MATCH exists here).
function signalCell(row) {
  const e = row.entry_signal;
  return <span title={e ? `Signal ${e.signal_id ?? '—'} · bar ${e.bar_date ?? '—'} · detected ${istTime(e.detected_at)} · ${e.reason ?? ''}` : 'Entry fill not matched to a signal row (legacy entry).'}>
    {strat(row.strategy)}{e?.bar_date ? <small className="ad-muted"> · bar {e.bar_date}</small> : null}</span>;
}
const chargesWhy = r => r.charges_note ?? 'Charges unavailable.';
const POSITION_COLUMNS = market => [
  {key: 'symbol', title: 'Symbol', render: sym}, {key: 'signal', title: 'Signal', render: (v, r) => signalCell(r), sortValue: r => r.strategy},
  {key: 'qty', title: 'Qty', align: 'right'}, {key: 'entry_price', title: 'Avg entry', align: 'right', render: money},
  {key: 'mark_price', title: 'Current', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why="No valid quote recorded; never shown as zero">no mark</NA> : <span title={`Last verified mark · ${r.mark_state}`}>{inr(v)}</span>)},
  {key: 'stop', title: 'Stop', render: (v, r) => stopCell(r.exit_plan), sortValue: r => r.exit_plan?.stop?.level ?? null},
  {key: 'target', title: 'Target', render: (v, r) => targetCell(r.exit_plan, r.strategy), sortValue: r => r.strategy},
  {key: 'rr', title: 'R:R', render: rrCell, sortValue: () => null},
  {key: 'break_even', title: 'Break-even', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why={chargesWhy(r)}>unavailable</NA> : <span title="Exit price at which net P&L is zero after the recorded entry charges and the engine's estimated exit charges.">{inr(v)}</span>)},
  {key: 'gross_pnl_paise', title: 'Gross P&L', align: 'right', render: v => (v === null || v === undefined ? <NA why="No valid mark">—</NA> : pnlPaise(v))},
  {key: 'charges_paise', title: 'Taxes & charges', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why={chargesWhy(r)}>unavailable</NA> : <span title={`Entry ${inr(paiseToInr(r.entry_charges_paise))} (recorded) + exit ${inr(paiseToInr(r.est_exit_charges_paise))} (estimated). ${r.charges_note}`}>{paise(v)}</span>)},
  {key: 'net_unrealized_paise', title: 'Net unrealized', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why={chargesWhy(r)}>unavailable</NA> : pnlPaise(v))},
  {key: 'net_return_pct', title: '% Net return', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why={chargesWhy(r)}>—</NA> : <span title="Net unrealized ÷ cost basis (avg entry × qty)">{pctCell(v)}</span>)},
  {key: 'lifecycle', title: 'Status', render: (v, r) => lifecycleCell(r), sortValue: r => (r.pending_exit ? 2 : 0) + (r.entry_pause?.paused ? 1 : 0)},
  {key: 'entry_at', title: 'Open time', render: atSec},
  {key: 'action', title: 'Action', render: () => actionCell('Exit / square off'), sortValue: () => null},
  {key: 'risk', title: 'Risk to stop', align: 'right', render: (v, r) => riskCell(r.exit_plan), sortValue: r => r.exit_plan?.stop?.risk_total ?? null},
  {key: 'industry', title: 'Sector'}, {key: 'cost_basis', title: 'Position size', align: 'right', render: money},
  {key: 'market_value', title: 'Market value', align: 'right', render: money}, {key: 'unrealized_pct', title: '% gross', align: 'right', render: pctCell},
  {key: 'high_water', title: 'High-water', align: 'right', render: money},
  {key: 'mark_state', title: 'Monitoring / mark', render: (v, r) => markCell(v, r, market), sortValue: r => r.mark_age_seconds},
  {key: 'mark_at', title: 'Quote time', render: atSec}, {key: 'updated_at', title: 'Updated', render: at},
];
const ORDER_COLUMNS = [
  {key: 'id', title: 'Order ID', render: v => <code className="ad-code" title="Engine signal id (the paper order identifier)">{String(v ?? '—').slice(0, 12)}</code>},
  {key: 'symbol', title: 'Symbol', render: sym}, {key: 'signal', title: 'Signal', render: (v, r) => <span title={`Signal bar ${r.bar_date}`}>{signalKind(r.reason)} · bar {r.bar_date}</span>, sortValue: r => r.bar_date},
  {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'reason', title: 'Order type', render: (v, r) => <span>{side(r.side)} {signalKind(v)} · market at open</span>, sortValue: r => signalKind(r.reason)},
  {key: 'qty', title: 'Quantity', render: (v, r) => (r.side === 'SELL' && r.position ? <span title="An exit sells the whole held quantity.">{fmt(r.position.qty, 0)} (held)</span>
    : <NA why={r.entry_plan?.sizing ?? 'Sized at fill time from target notional and the verified fill quote.'}>at fill{r.entry_plan?.target_notional_inr ? ` · target ${inr(r.entry_plan.target_notional_inr, 0)}` : ''}</NA>)},
  {key: 'price', title: 'Order price', render: (v, r) => <NA why="Signals carry no limit price; the fill uses a verified live quote (official open, else tagged fallback).">none (market){r.entry_plan?.signal_close ? ` · signal close ${inr(r.entry_plan.signal_close)}` : ''}</NA>},
  {key: 'current_price', title: 'Current price', align: 'right', render: (v, r) => (v === null || v === undefined ? <NA why="No accepted recorded quote for this stock">—</NA> : <span title={`Latest accepted recorded quote · ${istTimeSec(r.current_quote_at)}`}>{inr(v)}</span>)},
  {key: 'stop', title: 'Stop', render: (v, r) => (r.side === 'SELL' ? stopCell(r.exit_plan) : <span className="ad-muted-cell" title="What the stop will be once the entry fills.">{r.entry_plan?.stop_after_entry ?? '—'}</span>)},
  {key: 'target', title: 'Target', render: (v, r) => targetCell(r.exit_plan ?? r.entry_plan, r.strategy)},
  {key: 'status', title: 'Order status', render: (v, r) => <>{status(v)}{r.entry_pause?.paused && <Tag tone="bad" title={`${r.entry_pause.cause}. ${r.entry_pause.effect}`}>entries paused</Tag>}</>},
  {key: 'created_at', title: 'Created time', render: atSec},
  {key: 'filled_at', title: 'Execution time', render: (v, r) => (v ? atSec(v) : <span className="ad-muted-cell" title="Not filled yet">— · expected {r.eligible_date ? `${r.eligible_date} session` : '—'}</span>)},
  {key: 'eligible_date', title: 'Expected execution', render: v => (v ? `${v} session` : '—')},
  {key: 'product', title: 'Product', render: (v, r) => { const pr = r.entry_plan?.product ?? r.exit_plan?.product; return pr ? <span title={pr.note}>{pr.type}</span> : 'Delivery'; }},
  {key: 'remarks', title: 'Remarks', render: (v, r) => code(r.reason), sortValue: r => r.reason},
  {key: 'action', title: 'Action', render: () => actionCell('Edit / cancel'), sortValue: () => null},
];
const SIGNAL_COLUMNS = [
  {key: 'created_at', title: 'Detected', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'side', title: 'Side', render: side}, {key: 'kind', title: 'Type', render: (v, r) => signalKind(r.reason), sortValue: r => signalKind(r.reason)},
  {key: 'status', title: 'Status', render: status}, {key: 'bar_date', title: 'Signal bar'}, {key: 'eligible_date', title: 'Eligible from'},
  {key: 'filled_at', title: 'Filled', render: at}, {key: 'fill_qty', title: 'Fill qty', align: 'right', render: v => fmt(v, 0)},
  {key: 'fill_price', title: 'Fill price', align: 'right', render: money}, {key: 'reason', title: 'Reason', render: code},
];
const FILL_COLUMNS = [
  {key: 'filled_at', title: 'Filled', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'side', title: 'Side', render: side}, {key: 'qty', title: 'Qty', align: 'right'}, {key: 'price', title: 'Fill price', align: 'right', render: money},
  {key: 'turnover', title: 'Turnover', align: 'right', render: (v, r) => (r.costs ? paise(r.costs.turnover_paise) : <NA why="No fee/cash-flow row for this fill"/>), sortValue: r => r.costs?.turnover_paise},
  {key: 'charges', title: 'Charges', align: 'right', render: (v, r) => (r.costs ? paise(r.costs.charges_paise) : <NA why="No fee/cash-flow row for this fill"/>), sortValue: r => r.costs?.charges_paise},
  {key: 'net_cash', title: 'Net cash flow', align: 'right', render: (v, r) => (r.costs ? pnlPaise(r.costs.net_cashflow_paise) : <NA why="No fee/cash-flow row for this fill"/>), sortValue: r => r.costs?.net_cashflow_paise},
  {key: 'realized_pnl', title: 'Realized (gross)', align: 'right', render: (v, r) => (r.side === 'BUY' ? '—' : pnl(v))},
  {key: 'execution_path', title: 'Price source', render: v => (v ? <span title={EXECUTION_PATH_HELP[v]}>{v}</span> : <NA why="Execution path not recorded for this fill"/>)},
  {key: 'quote_at', title: 'Quote time', render: atSec}, {key: 'reason', title: 'Reason', render: code},
];
const EXECUTION_PATH_HELP = {FEED_OPEN: 'Official session open from the feed', AUCTION_CAPTURE: 'Captured pre-open auction price', FALLBACK_OPEN: 'First observed 9:15 trade (official open unavailable)', LTP_LEGACY: 'Legacy fill at the observed last traded price'};
function closedStopCell(row) {
  const st = row.exit_plan?.stop;
  if (!st) return '—';
  if (!st.hit) return <span className="ad-muted-cell" title={row.exit_plan.exit_rule}>{st.defined ? 'not hit · rule exit' : 'no fixed stop'}</span>;
  if (st.threshold === null || st.threshold === undefined) return <NA why={st.unavailable_reason}>stop exit · level not recorded</NA>;
  return <span title={`${st.source} at ${istTimeSec(st.recorded_at)}`}><strong>{inr(st.threshold)}</strong><small className="ad-muted"> stop hit{st.rule ? ` (${st.rule})` : ''}</small></span>;
}
const TRADE_COLUMNS = [
  {key: 'symbol', title: 'Symbol', render: sym}, {key: 'qty', title: 'Qty', align: 'right'},
  {key: 'entry_price', title: 'Entry', align: 'right', render: v => (v === null || v === undefined ? <NA why="No earlier BUY fill in the ledger for this exit">unmatched</NA> : money(v))},
  {key: 'exit_price', title: 'Exit', align: 'right', render: money},
  {key: 'break_even', title: 'Break-even', align: 'right', render: v => (v === null || v === undefined ? <NA why="Needs fee rows for both fills and an equal entry/exit quantity">unavailable</NA> : <span title="Exit price at which net P&L would be zero after the recorded charges of both fills">{inr(v)}</span>)},
  {key: 'realized_pnl', title: 'Gross P&L', align: 'right', render: pnl},
  {key: 'charges_paise', title: 'Charges', align: 'right', render: v => (v === null || v === undefined ? <NA why="A fee row is missing for the entry or exit fill">unavailable</NA> : paise(v))},
  {key: 'net_pnl_paise', title: 'Net P&L', align: 'right', render: v => (v === null || v === undefined ? <NA why="Needs fee rows for both fills and an equal entry/exit quantity">unavailable</NA> : pnlPaise(v))},
  {key: 'net_pnl_pct', title: 'Net %', align: 'right', render: v => (v === null || v === undefined ? <NA why="Needs net P&L">—</NA> : <span title="Net P&L ÷ entry notional">{pctCell(v)}</span>)},
  {key: 'signal', title: 'Signal', render: (v, r) => <span title={r.entry_signal_id ? `Entry signal ${r.entry_signal_id}` : 'Entry signal not matched'}>{r.entry_reason ? signalKind(r.entry_reason) : '—'}{r.entry_reason ? <small className="ad-muted"> · {r.entry_reason}</small> : null}</span>, sortValue: r => r.entry_reason},
  {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'score', title: 'Score', render: () => <NA why="The paper ledger does not store a signal score; signals are pass/fail rule checks.">not stored</NA>},
  {key: 'opened_at', title: 'Opened', render: atSec}, {key: 'closed_at', title: 'Closed', render: atSec},
  {key: 'exit_reason', title: 'Exit reason', render: code}, {key: 'hold_hours', title: 'Hold', align: 'right', render: hours},
  {key: 'exit_source', title: 'Exit source', render: (v, r) => exitSource(r.exit_reason), sortValue: r => exitSource(r.exit_reason)},
  {key: 'stop', title: 'Stop used', render: (v, r) => closedStopCell(r), sortValue: r => r.exit_plan?.stop?.threshold ?? null},
  {key: 'target', title: 'Target', render: (v, r) => targetCell(r.exit_plan, r.strategy)},
  {key: 'pnl_pct', title: 'Gross %', align: 'right', render: pctCell},
];
const ALERT_COLUMNS = [
  {key: 'source_at', title: 'Time', render: atSec}, {key: 'event_type', title: 'Alert', render: v => String(v).replace(/_/g, ' ')},
  {key: 'severity', title: 'Severity', render: v => <Tag tone={v === 'INFO' ? 'info' : v === 'WARN' ? 'warn' : 'bad'}>{v}</Tag>},
  {key: 'outcome', title: 'Outcome', render: code}, {key: 'symbol', title: 'Stock', render: v => (v ? sym(v) : '—')}, {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'bar_date', title: 'Bar'}, {key: 'price', title: 'Price', align: 'right', render: money}, {key: 'threshold', title: 'Threshold', align: 'right', render: money},
  {key: 'rule', title: 'Rule / reason', render: (v, r) => code(v ?? r.reason)},
];
function runSummaryText(s = {}) {
  const parts = [];
  if (s.fills !== undefined) parts.push(`${s.fills} fills`);
  if (s.quotes !== undefined) parts.push(`${s.fresh_quotes ?? '—'}/${s.quotes} fresh quotes`);
  if (s.signals_queued !== undefined) parts.push(`${s.signals_queued} signals queued`);
  if (s.latest_qualifiers !== undefined) parts.push(`${s.latest_qualifiers} qualifiers`);
  if (s.universe_symbols !== undefined) parts.push(`${s.universe_symbols} symbols scanned`);
  if (s.unavailable_symbols !== undefined) parts.push(`${s.unavailable_symbols} unavailable`);
  if (s.invalid_rows_excluded !== undefined) parts.push(`${s.invalid_rows_excluded} invalid rows excluded`);
  if (s.prefilter) parts.push(`prefilter ${s.prefilter.pass ?? '—'} pass / ${s.prefilter.fail ?? '—'} fail / ${s.prefilter.unavailable ?? '—'} n/a`);
  if (s.target_bar) parts.push(`bar ${s.target_bar}`);
  if (s.screen_only) parts.push('screen only');
  if (s.detail_status) parts.push(s.detail_status.replace(/_/g, ' '));
  return parts.join(' · ') || '—';
}
const RUN_COLUMNS = [
  {key: 'at', title: 'At', render: atSec}, {key: 'kind', title: 'Kind'}, {key: 'status', title: 'Status', render: status},
  {key: 'summary', title: 'Details', render: v => <span className="ad-muted-cell">{runSummaryText(v)}</span>, sortValue: r => runSummaryText(r.summary)},
];
const QUOTE_COLUMNS = [
  {key: 'observed_at', title: 'Observed', render: atSec}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'price', title: 'Price', align: 'right', render: money},
  {key: 'accepted', title: 'Accepted', render: v => <Tag tone={Number(v) === 1 ? 'good' : 'warn'}>{Number(v) === 1 ? 'yes' : 'no'}</Tag>},
  {key: 'quote_at', title: 'Exchange time', render: atSec}, {key: 'lag_seconds', title: 'Lag', align: 'right', render: v => (v === null || v === undefined ? '—' : age(Math.abs(v)) + (v < 0 ? ' ahead' : ''))},
  {key: 'source', title: 'Source'}, {key: 'reason', title: 'Reason', render: code},
];
const DATA_AUDIT_COLUMNS = [
  {key: 'observed_at', title: 'Observed', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'bar_date', title: 'Bar date'}, {key: 'reason', title: 'Why the bar was excluded', render: code},
];
const COST_COLUMNS = [
  {key: 'filled_at', title: 'Date / time', render: (v, r) => (v ? at(v) : r.day)}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Sleeve', render: strat},
  {key: 'side', title: 'Action', render: side}, {key: 'qty', title: 'Qty', align: 'right'}, {key: 'turnover_paise', title: 'Turnover', align: 'right', render: paise},
  {key: 'charges_paise', title: 'Charges', align: 'right', render: paise}, {key: 'net_cashflow_paise', title: 'Amount (net)', align: 'right', render: pnlPaise},
  {key: 'sleeve_cash_after_paise', title: 'Sleeve cash after', align: 'right', render: paise}, {key: 'policy_version', title: 'Charge policy', render: code},
];
const CREDIT_COLUMNS = [
  {key: 'recorded_at', title: 'Recorded', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Sleeve', render: strat},
  {key: 'kind', title: 'Kind'}, {key: 'ex_date', title: 'Ex-date'}, {key: 'amount_paise', title: 'Amount', align: 'right', render: pnlPaise},
];

function FeeTable({costs}) {
  if (!costs) return <p className="ad-ledger-note">No fee/cash-flow row is recorded for this fill, so its charges are unavailable (not zero).</p>;
  const f = costs.fees;
  return <KeyValues items={[
    ['Turnover', paise(costs.turnover_paise)], ['Gross cash flow', pnlPaise(costs.gross_cashflow_paise)],
    ...(f ? FEE_LABELS.map(([k, label]) => [label, paise(f[k])]) : [['Charge breakdown', 'unavailable (unreadable components)']]),
    ['Total charges', paise(costs.charges_paise)], ['Net cash flow', pnlPaise(costs.net_cashflow_paise)], ['Charge policy', code(costs.policy_version)],
  ]}/>;
}

const IND_LABELS = {supertrend: 'Supertrend', final_lower: 'Lower band', final_upper: 'Upper band', direction: 'Direction (−1 green / +1 red)', atr10: 'ATR10', rsi14: 'RSI14', sma5: 'SMA5', sma50: 'SMA50', sma200: 'SMA200', ema20: 'EMA20', ema50: 'EMA50', bb_middle: 'BB middle', bb_lower: 'BB lower', bb_upper: 'BB upper', macd: 'MACD', signal: 'MACD signal', histogram: 'Histogram', high252: '252-day high'};
function PlanDetail({plan, strategy}) {
  if (!plan) return <p className="ad-ledger-note">No exit plan is available for this row.</p>;
  const st = plan.stop, lv = plan.rule_level;
  const stopItems = st.kind === 'trail'
    ? [['Stop type', `Strategy 07 trail · ${st.fraction} × completed-close high-water (${st.pct_below}% below)`], ['Stop level', st.level === null ? `unavailable · ${st.unavailable_reason}` : <strong>{inr(st.level)}</strong>],
      ['High-water used', inr(st.high_water), st.basis], ['Distance to stop', st.distance_to_mark === null || st.distance_to_mark === undefined ? '—' : `${inr(st.distance_to_mark)} (${pct(st.distance_pct, 2)} of mark, mark ${st.mark_state ?? '—'})`],
      ['Risk distance (entry → stop)', st.risk_per_share === null || st.risk_per_share === undefined ? '—' : `${inr(st.risk_per_share)} / share · ${inr(st.risk_total)} total${st.locked_in ? ' · stop is above entry (gain locked)' : ''}`, 'risk distance, not risk:reward'],
      ['Open risk to stop (mark → stop)', st.open_risk_to_stop === null || st.open_risk_to_stop === undefined ? '—' : inr(st.open_risk_to_stop)], ['How it fills', st.execution],
      ['SMA50 exit leg', lv?.level === null || lv?.level === undefined ? `unavailable · ${lv?.unavailable_reason ?? '—'}` : `${inr(lv.level)} · ${lv.label} (bar ${lv.bar_date})`]]
    : [['Stop type', 'No fixed rupee stop (rule-based exit)', st.why], ['Exit rule', plan.exit_rule],
      ['Rule level', st.level === null || st.level === undefined ? `unavailable · ${st.unavailable_reason}` : <><strong>{inr(st.level)}</strong> · {st.label}</>, st.level !== null && st.level !== undefined ? `bar ${st.bar_date ?? '—'} · ${st.source}` : undefined],
      ...(st.rsi14 !== null && st.rsi14 !== undefined ? [['RSI14 (last scan)', fmt(st.rsi14, 2), 'exit when above 60']] : [])];
  const values = Object.entries(plan.indicators?.values ?? {});
  return <div>
    <p className="ad-subhead">Stop, target and exit ({strategyName(strategy)})</p>
    <KeyValues items={[...stopItems, ['Target', `${NO_TARGET} · exit: ${plan.exit_rule}`, plan.target?.why], ['R:R', '— (no defined target)', RR_UNAVAILABLE],
      ['Exit rule (engine wording)', plan.engine_rule ? <code className="ad-code">{plan.engine_rule}</code> : '—'], ['Last evaluated bar', plan.last_evaluated_bar ?? '—'], ['Product', plan.product?.type, plan.product?.note]]}/>
    <p className="ad-subhead">Indicator values at the last scan{plan.indicators?.bar_date ? ` (bar ${plan.indicators.bar_date})` : ''}</p>
    {values.length ? <KeyValues items={values.map(([k, v]) => [IND_LABELS[k] ?? k, fmt(v, 2)])}/> : <p className="ad-ledger-note">{plan.indicators ? 'No indicator values for this strategy in the stored scan row.' : 'Not stored: the scan keeps indicator values only for stocks that pass the prefilter.'}</p>}
  </div>;
}

function RowDetail({view, row, onClose}) {
  if (!row) return null;
  const title = {fills: 'Fill details', history: 'Trade exit details', positions: 'Position details', orders: 'Order details', signals: 'Signal details', alerts: 'Alert details'}[view] ?? 'Record details';
  let body;
  if (view === 'history') {
    body = <div className="ad-detail-grid">
      <KeyValues items={[['Stock', sym(row.symbol)], ['Strategy / signal', strat(row.strategy)], ['Sector', row.industry], ['Quantity', fmt(row.qty, 0)],
        ['Entry', `${inr(row.entry_price)} · ${at(row.opened_at)}`], ['Exit price', inr(row.exit_price)], ['Exit time', at(row.closed_at)], ['Exit quote time', at(row.exit_quote_at)],
        ['Reason', code(row.exit_reason)], ['Source', exitSource(row.exit_reason)], ['Hold', hours(row.hold_hours)], ['P&L (gross)', pnl(row.realized_pnl)], ['P&L %', pctCell(row.pnl_pct)],
        ['Charges (both fills)', row.charges_paise === null ? 'unavailable' : paise(row.charges_paise)], ['Net P&L', row.net_pnl_paise === null ? 'unavailable' : pnlPaise(row.net_pnl_paise)],
        ['Break-even after charges', row.break_even === null || row.break_even === undefined ? 'unavailable' : inr(row.break_even)], ['Net %', pctCell(row.net_pnl_pct)],
        ['Entry signal', row.entry_signal_id ? code(row.entry_signal_id) : 'not matched'], ['Score', 'not stored in the paper ledger'],
        ['Stop', closedStopCell(row)], ['Target', `${NO_TARGET} · exit: ${row.exit_plan?.exit_rule ?? exitRule(row.strategy)}`], ['R:R', '— (no defined target)', RR_UNAVAILABLE],
        ['Exit rule (engine wording)', row.exit_plan?.engine_rule ? <code className="ad-code">{row.exit_plan.engine_rule}</code> : '—'],
        ['Reconciliation-recovered flag', 'not recorded', 'the engine has no reconciliation exit source'],
        ['Entry fill', row.entry_fill_id ?? 'not matched'], ['Exit fill', row.id]]}/>
      <div><p className="ad-subhead">Entry fill charges</p><FeeTable costs={row.entry_costs}/><p className="ad-subhead">Exit fill charges</p><FeeTable costs={row.exit_costs}/></div>
    </div>;
  } else if (view === 'fills') {
    body = <div className="ad-detail-grid">
      <KeyValues items={[['Fill id', row.id], ['Stock', sym(row.symbol)], ['Strategy', strat(row.strategy)], ['Sector', row.industry], ['Side', side(row.side)], ['Quantity', fmt(row.qty, 0)],
        ['Fill price', inr(row.price)], ['Value', inr(row.price * row.qty)], ['Quote time', at(row.quote_at)], ['Filled', at(row.filled_at)], ['Reason', code(row.reason)],
        ['Realized (gross)', row.side === 'BUY' ? '—' : pnl(row.realized_pnl)], ['Ledger cash flow (gross)', pnl(row.cashflow)]]}/>
      <div><p className="ad-subhead">Charges (paper estimate, not a broker bill)</p><FeeTable costs={row.costs}/></div>
    </div>;
  } else if (view === 'positions') {
    body = <div className="ad-detail-grid">
      <KeyValues items={[['Stock', sym(row.symbol)], ['Signal / strategy', signalCell(row)], ['Entry signal', row.entry_signal ? code(row.entry_signal.signal_id) : 'not matched (legacy entry)'],
        ['Entry', `${inr(row.entry_price)} · ${atSec(row.entry_at)}`], ['Current', row.mark_price === null ? 'missing' : `${inr(row.mark_price)} · ${atSec(row.mark_at)}`],
        ['Quantity', fmt(row.qty, 0)], ['Position size (cost basis)', inr(row.cost_basis)], ['Market value', inr(row.market_value)],
        ['Unrealized (gross)', pnl(row.unrealized_pnl)], ['Entry charges (recorded)', row.entry_charges_paise === null ? 'unavailable' : paise(row.entry_charges_paise)],
        ['Exit charges (estimated)', row.est_exit_charges_paise === null ? 'unavailable' : paise(row.est_exit_charges_paise), row.charges_note],
        ['Net unrealized', row.net_unrealized_paise === null ? 'unavailable' : pnlPaise(row.net_unrealized_paise)], ['% net return', pctCell(row.net_return_pct)],
        ['Break-even after charges', row.break_even === null ? 'unavailable' : inr(row.break_even)], ['Status', lifecycleCell(row)], ['Action', actionCell('Exit / square off / update SL-TP')]]}/>
      <PlanDetail plan={row.exit_plan} strategy={row.strategy}/>
    </div>;
  } else if (view === 'orders') {
    body = <div className="ad-detail-grid">
      <KeyValues items={[['Order ID', code(row.id)], ['Stock', sym(row.symbol)], ['Strategy', strat(row.strategy)], ['Side', side(row.side)], ['Type', `${signalKind(row.reason)} · market at the eligible session open`],
        ['Signal bar', row.bar_date], ['Created', atSec(row.created_at)], ['Expected execution', row.eligible_date ? `${row.eligible_date} session` : '—'], ['Status', status(row.status)],
        ['Current price', row.current_price === null ? 'no accepted quote' : `${inr(row.current_price)} · ${atSec(row.current_quote_at)}`],
        ...(row.entry_plan ? [['Target notional (policy)', inr(row.entry_plan.target_notional_inr, 0), 'context only, not an order quantity'], ['Sizing', row.entry_plan.sizing], ['Signal-bar close', row.entry_plan.signal_close === null ? 'not stored' : `${inr(row.entry_plan.signal_close)} (${row.entry_plan.signal_close_bar ?? '—'})`], ['Stop after entry', row.entry_plan.stop_after_entry], ['Target', `${NO_TARGET} · exit: ${row.entry_plan.exit_rule}`], ['Product', row.entry_plan.product?.type, row.entry_plan.product?.note]] : []),
        ...(row.entry_pause?.paused ? [['Entries paused', `${row.entry_pause.cause} since ${atSec(row.entry_pause.paused_at)}`, row.entry_pause.effect]] : []),
        ['Remarks', code(row.reason)], ['Action', actionCell('Edit / cancel')]]}/>
      {row.exit_plan && <PlanDetail plan={row.exit_plan} strategy={row.strategy}/>}
    </div>;
  } else {
    body = <KeyValues items={Object.entries(row).filter(([, v]) => v === null || typeof v !== 'object').map(([k, v]) => [k.replace(/_/g, ' '), v === null || v === undefined ? '—' : /(_at|observed|time)$/.test(k) ? at(v) : String(v)])}/>;
  }
  return <div className="ad-detail" role="region" aria-label={title}>
    <header><h3>{title}</h3><button className="ad-ghost" onClick={onClose}>Close</button></header>{body}
  </div>;
}

// One server-paginated tab. Rows always belong to the current query: switching tab or filter shows
// a loading state until the new rows arrive, never the previous tab's rows under new headings.
function PagedTab({view, apiView, params = {}, columns, scope, strategy, refreshKey, empty, notes, detail = true, header}) {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(null);
  const query = {view: apiView, scope, page: String(page), limit: String(LIMIT), ...params};
  if (strategy) query.strategy = String(strategy);
  const key = JSON.stringify(query);
  const [lastKey, setLastKey] = useState(key);
  if (lastKey !== key) { setLastKey(key); setSelected(null); }
  const filterKey = JSON.stringify({...query, page: undefined});
  const [lastFilter, setLastFilter] = useState(filterKey);
  if (lastFilter !== filterKey) { setLastFilter(filterKey); if (page !== 1) setPage(1); }
  const {data, error, loading} = useDashboard(query, refreshKey);
  const total = data?.total ?? 0;
  const rows = data?.rows ?? [];
  return <div className="ad-ledger-tab" data-view={view}>
    {header?.(data)}
    {notes?.(data)}
    {error && <Callout tone="error">{error}</Callout>}
    {!data && loading ? <Loading label={`Loading ${view}…`}/> : !data ? null : <>
      <SortTable key={key} rows={rows} columns={columns} pageSize={LIMIT} caption={view} empty={empty}
        onRowClick={detail ? (r, k) => setSelected(selected?.k === k ? null : {k, r}) : undefined} selectedKey={selected?.k}/>
      {selected && <RowDetail view={view} row={selected.r} onClose={() => setSelected(null)}/>}
    </>}
    <div className="ad-pager wide">
      <span>{data ? `${rows.length ? `${(page - 1) * LIMIT + 1}–${(page - 1) * LIMIT + rows.length}` : 0} of ${total} · page ${page} of ${Math.max(1, Math.ceil(total / LIMIT))} · server-paginated; column sort applies to this page` : ' '}</span>
      <div><button disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Previous</button><button disabled={page * LIMIT >= total || loading} onClick={() => setPage(page + 1)}>Next</button></div>
    </div>
  </div>;
}

function Unavailable({items}) {
  return <ul className="ad-unavail">{items.map(([k, why]) => <li key={k}><strong>{k}</strong><span>{why}</span></li>)}</ul>;
}

function Incomplete({set, what}) {
  if (!set || set.complete) return null;
  return <Callout tone="warn" title="Partial record.">Read {set.rows.length} of {set.total} {what}; figures below cover only the rows read.</Callout>;
}

function ExposureSection({positions, title}) {
  if (positions.error) return <Callout tone="error">{positions.error}</Callout>;
  if (!positions.data) return <Loading label="Reading open positions…"/>;
  const rows = positions.data.rows, bySector = allocation(rows, p => p.industry), byStrategy = allocation(rows, p => p.strategy), risk = stopRisk(rows);
  const largest = bySector.total ? [...rows].filter(p => p.market_value !== null && p.market_value !== undefined).sort((a, b) => b.market_value - a.market_value) : [];
  const share = (v, total) => (total ? pct(v / total * 100, 1) : '—');
  const allocCols = first => [{key: 'key', title: first, render: first === 'Strategy' ? strat : undefined}, {key: 'positions', title: 'Positions', align: 'right'}, {key: 'value', title: 'Market value', align: 'right', render: money}, {key: 'share', title: 'Share', align: 'right', render: v => <span className="ad-bar-cell"><i style={{width: `${v ?? 0}%`}}/>{pct(v, 1)}</span>}];
  return <Section eyebrow="Exposure" title={title}>
    <div className="ad-tiles">
      <Tile label="Open positions" value={fmt(rows.length, 0)} hint={bySector.missing ? `${bySector.missing} without a mark (excluded from allocation)` : 'All marked'}/>
      <Tile label="Capital deployed" value={inr(bySector.total)} hint="Market value at last marks"/>
      <Tile label="Largest position" value={largest[0] ? <>{sym(largest[0].symbol)} {share(largest[0].market_value, bySector.total)}</> : '—'} hint={largest[0] ? inr(largest[0].market_value) : undefined}/>
      <Tile label="Smallest position" value={largest.length ? <>{sym(largest.at(-1).symbol)} {share(largest.at(-1).market_value, bySector.total)}</> : '—'} hint={largest.length ? inr(largest.at(-1).market_value) : undefined}/>
      <Tile label="S07 open risk to trail stops" value={signedInr(risk.openRisk === null ? null : -risk.openRisk)} tone={risk.openRisk ? 'down' : 'flat'} hint={`${risk.withMark} of ${risk.positions} Strategy 07 positions with a mark · Σ (mark − stop) × qty`}/>
      <Tile label="S07 risk from entry to stop" value={risk.entryRisk === null ? '—' : inr(risk.entryRisk)} hint="Σ (avg entry − stop) × qty; negative = stops above entry"/>
      <Tile label="Nearest to stop" value={risk.nearest ? <>{sym(risk.nearest.symbol)} {pct(risk.nearest.exit_plan.stop.distance_pct, 1)}</> : '—'} hint={risk.nearest ? `stop ${inr(risk.nearest.exit_plan.stop.level)}` : 'No priced Strategy 07 stop'}/>
      <Tile label="Strategies 01–06 stops" value="Rule-based" hint="No fixed rupee stop; see each position’s exit rule"/>
    </div>
    <div className="ad-split">
      <SortTable rows={bySector.rows} rowKey={r => r.key} pageSize={10} caption="Allocation by sector" empty={{title: 'No marked positions'}} columns={allocCols('Sector (NSE industry)')}/>
      <SortTable rows={byStrategy.rows} rowKey={r => r.key} pageSize={7} caption="Allocation by strategy" empty={{title: 'No marked positions'}} columns={allocCols('Strategy')}/>
    </div>
  </Section>;
}

function AnalyticsTab({overview, strategy, refreshKey}) {
  const [range, setRange] = useState('all');
  const [custom, setCustom] = useState([overview.day, overview.day]);
  const base = strategy ? {strategy: String(strategy)} : {};
  const trades = useAllPages({view: 'trades', ...base}, refreshKey);
  const fills = useAllPages({view: 'fills', ...base}, refreshKey);
  const positions = useAllPages({view: 'positions', ...base}, refreshKey);
  const pending = useDashboard({view: 'signals', status: 'pending', limit: '1', ...base}, refreshKey);
  const expired = useDashboard({view: 'signals', status: 'expired', limit: '1', ...base}, refreshKey);
  const blocked = useDashboard({view: 'signals', status: 'blocked_prefilter', limit: '1', ...base}, refreshKey);
  if (trades.error || fills.error) return <Callout tone="error">{trades.error || fills.error}</Callout>;
  if (!trades.data || !fills.data) return <Loading label="Reading the complete trade and fill record…"/>;
  const bounds = rangeBounds(range, overview.day, custom);
  const t = trades.data.rows.filter(r => inRange(r.closed_at, bounds));
  const f = fills.data.rows.filter(r => inRange(r.filled_at, bounds));
  const s = tradeStats(t), fs = fillStats(f), c = overview.cappedLedger, tot = overview.totals;
  const daily = (overview.dailyHistory ?? []).filter(d => !bounds[0] || (d.day >= bounds[0] && d.day <= bounds[1]));
  const all = range === 'all' && !strategy;
  const countOf = q => (q.data ? q.data.total : null);
  return <div className="ad-ledger-tab">
    <div className="ad-ranges" role="group" aria-label="Analytics range">{RANGES.map(([id, label]) => <button key={id} className={`ad-chip ${range === id ? 'on' : ''}`} aria-pressed={range === id} onClick={() => setRange(id)}>{label}</button>)}
      {range === 'custom' && <span className="ad-ledger-filters"><input type="date" className="ad-input" aria-label="Custom range from" value={custom[0]} onChange={e => setCustom([e.target.value, custom[1]])}/><input type="date" className="ad-input" aria-label="Custom range to" value={custom[1]} onChange={e => setCustom([custom[0], e.target.value])}/></span>}</div>
    <Incomplete set={trades.data} what="closed trades"/><Incomplete set={fills.data} what="fills"/>
    <p className="ad-ledger-note">Computed in the browser from all {trades.data.total} closed trades and {fills.data.total} fills{strategy ? ` of ${strategyName(strategy)}` : ''}. Wins and losses use gross realized P&amp;L; net figures subtract recorded charges.</p>
    <div className="ad-tiles">
      <Tile label="Total trades" value={fmt(s.count, 0)} hint="Closed round trips"/>
      <Tile label="Winning trades" value={fmt(s.wins, 0)} tone={s.wins ? 'up' : 'flat'}/>
      <Tile label="Losing trades" value={fmt(s.losses, 0)} tone={s.losses ? 'down' : 'flat'}/>
      <Tile label="Win rate" value={pct(s.winRate)} hint={s.count ? `${s.flats} flat` : 'No closed trades in range'}/>
      <Tile label="Realized P&L (gross)" value={signedInr(s.grossPnl)} tone={tone(s.grossPnl)}/>
      <Tile label="Realized P&L (net)" value={signedInr(s.netPnl)} tone={tone(s.netPnl)} hint={s.netMissing ? `${s.netMissing} trades lack fee rows` : 'After recorded charges'}/>
      <Tile label="Today’s P&L (MTM)" value={signedInr(tot.daily)} tone={tone(tot.daily)} hint={tot.daily === null ? 'No snapshot for today yet' : 'Latest daily snapshot'}/>
      <Tile label="Unrealized P&L" value={signedInr(tot.unrealized)} tone={tone(tot.unrealized)} hint="Now, gross, from last marks"/>
      <Tile label="Portfolio value" value={inr(paiseToInr(c?.equity_paise), 0)} hint="Capped book equity, now"/>
      <Tile label="Available cash" value={inr(paiseToInr(c?.cash_paise), 0)} hint="Now"/>
      <Tile label="Capital utilized" value={inr(paiseToInr(c?.market_value_paise), 0)} hint="Market value of holdings, now"/>
      <Tile label="ROI %" value={all ? pct(c?.capital_return_pct, Math.abs(c?.capital_return_pct ?? 1) < 0.01 ? 4 : 2, true) : '—'} hint={all ? 'Capped net P&L ÷ paper capital' : 'All time, all strategies only'}/>
      <Tile label="Average profit" value={signedInr(s.avgProfit)} tone={tone(s.avgProfit)}/>
      <Tile label="Average loss" value={signedInr(s.avgLoss)} tone={tone(s.avgLoss)}/>
      <Tile label="Profit factor" value={fmt(s.profitFactor)} hint={s.profitFactor === null ? 'Needs wins and losses' : 'Gross profit ÷ gross loss'}/>
      <Tile label="Expectancy" value={signedInr(s.expectancy)} hint="Mean gross P&L per trade"/>
      <Tile label="Largest profit" value={signedInr(s.largestProfit)} tone={tone(s.largestProfit)}/>
      <Tile label="Largest loss" value={signedInr(s.largestLoss)} tone={tone(s.largestLoss)}/>
      <Tile label="Open positions" value={fmt(tot.openPositions, 0)} hint="Now"/>
      <Tile label="Current streak" value={s.currentStreak ? `${Math.abs(s.currentStreak)} ${s.currentStreak > 0 ? 'wins' : 'losses'}` : '—'}/>
    </div>
    <div className="ad-split">
      <Section eyebrow="Trade analytics" title="Trade stats"><KeyValues items={[
        ['Avg holding period', hours(s.avgHoldHours)], ['Avg entry price', inr(s.avgEntry)], ['Avg exit price', inr(s.avgExit)], ['Avg return %', pct(s.avgReturnPct, 2, true)],
        ['Best trade', s.best ? <>{sym(s.best.symbol)} {pnl(s.best.realized_pnl)}</> : '—'], ['Worst trade', s.worst ? <>{sym(s.worst.symbol)} {pnl(s.worst.realized_pnl)}</> : '—'],
        ['Most profitable stock', s.mostProfitableSymbol ? <>{sym(s.mostProfitableSymbol.key)} {pnl(s.mostProfitableSymbol.pnl)}</> : '—'],
        ['Most losing stock', s.mostLosingSymbol ? <>{sym(s.mostLosingSymbol.key)} {pnl(s.mostLosingSymbol.pnl)}</> : '—'],
        ['Longest win streak', fmt(s.longestWin, 0)], ['Longest loss streak', fmt(s.longestLoss, 0)],
        ['Unmatched exits', fmt(s.unmatched, 0), 'exits with no earlier BUY fill in the ledger'],
      ]}/></Section>
      <Section eyebrow="Order flow" title="Orders"><KeyValues items={[
        ['Executed orders (fills)', fmt(fs.count, 0)], ['Buy fills', fmt(fs.buys, 0)], ['Sell fills', fmt(fs.sells, 0)],
        ['Pending orders (signals)', fmt(countOf(pending), 0), 'now'], ['Expired signals', fmt(countOf(expired), 0), 'all time'], ['Blocked by prefilter', fmt(countOf(blocked), 0), 'all time'],
        ['Same-day round trips', fmt(s.sameDay, 0)], ['Overnight (delivery) round trips', fmt(s.overnight, 0)],
        ['Turnover', inr(fs.turnover), fs.missingCosts ? `${fs.missingCosts} fills lack fee rows` : undefined], ['Charges', inr(fs.charges)], ['Avg order value', inr(fs.avgSize)],
      ]}/></Section>
    </div>
    <Section eyebrow="Charts" title="Daily P&L (mark-to-market snapshots)">
      <Columns rows={daily.map(d => ({label: d.day, value: d.daily}))} unit="" empty={{title: 'No daily snapshots in range', text: 'Daily P&L comes from the 16:10 EOD snapshot. A day without a snapshot is not a zero.'}}/>
    </Section>
    <Section eyebrow="Charts" title="Equity curve / capital growth (latest known total P&L per EOD snapshot)">
      <Columns rows={daily.map(d => ({label: d.day, value: d.total}))} unit="" empty={{title: 'No snapshots in range'}}/>
      <p className="ad-ledger-note">Capital growth = paper capital {inr(paiseToInr(c?.master_capital_paise), 0)} + the total shown for each snapshot day. Current capped equity: {inr(paiseToInr(c?.equity_paise), 2)}.</p>
    </Section>
    <div className="ad-split">
      <Section eyebrow="Charts" title="Monthly P&L (realized, gross, by exit month)">
        <Columns rows={monthlyPnl(t).map(m => ({label: m.month, value: m.pnl, trades: m.trades}))} unit="" describe={r => `${r.trades} trades`} empty={{title: 'No closed trades in range'}}/>
      </Section>
      <Section eyebrow="Charts" title="Win / loss distribution (gross return % per closed trade)">
        <SortTable rows={returnDistribution(t)} rowKey={r => r.label} pageSize={7} caption="Win/loss distribution" columns={[{key: 'label', title: 'Return bucket'}, {key: 'trades', title: 'Trades', align: 'right', render: v => <span className="ad-bar-cell"><i style={{width: `${t.length ? v / t.length * 100 : 0}%`}}/>{v}</span>}]}/>
      </Section>
    </div>
    <Section eyebrow="Charts" title="Trade frequency (per IST session day)">
      <SortTable rows={tradeFrequency(t, f)} rowKey={r => r.day} pageSize={10} caption="Trade frequency" empty={{title: 'No activity in range'}} columns={[{key: 'day', title: 'Session'}, {key: 'fills', title: 'Fills', align: 'right'}, {key: 'closed', title: 'Closed trades', align: 'right'}]}/>
    </Section>
    <ExposureSection positions={positions} title="Portfolio allocation & stop risk (current open positions, now)"/>
    <div className="ad-split">
      <Section eyebrow="Per stock" title="Per-symbol stats"><SortTable rows={groupBy(t, r => r.symbol)} rowKey={r => r.key} pageSize={12} caption="Per-symbol stats" filter empty={{title: 'No closed trades in range'}}
        columns={[{key: 'key', title: 'Stock', render: sym}, {key: 'trades', title: 'Trades', align: 'right'}, {key: 'winRate', title: 'Win rate', align: 'right', render: v => pct(v)}, {key: 'avgHoldHours', title: 'Avg hold', align: 'right', render: hours}, {key: 'pnl', title: 'P&L (gross)', align: 'right', render: pnl}]}/></Section>
      <Section eyebrow="Per sector" title="Sector-wise performance"><SortTable rows={groupBy(t, r => r.industry)} rowKey={r => r.key} pageSize={12} caption="Sector-wise performance" empty={{title: 'No closed trades in range'}}
        columns={[{key: 'key', title: 'Sector (NSE industry)'}, {key: 'trades', title: 'Trades', align: 'right'}, {key: 'allocation', title: 'Allocation', align: 'right', render: money}, {key: 'profit', title: 'Profit', align: 'right', render: pnl}, {key: 'loss', title: 'Loss', align: 'right', render: pnl}]}/></Section>
    </div>
    <Section eyebrow="Per strategy" title="Strategy breakdown"><SortTable rows={groupBy(t, r => r.strategy)} rowKey={r => r.key} pageSize={7} caption="Strategy breakdown" empty={{title: 'No closed trades in range'}}
      columns={[{key: 'key', title: 'Strategy', render: strat}, {key: 'trades', title: 'Trades', align: 'right'}, {key: 'wins', title: 'Wins', align: 'right'}, {key: 'losses', title: 'Losses', align: 'right'}, {key: 'winRate', title: 'Win rate', align: 'right', render: v => pct(v)}, {key: 'pnl', title: 'P&L (gross)', align: 'right', render: pnl}]}/></Section>
    <Section eyebrow="Not shown" title="Unavailable in the paper record">
      <Unavailable items={[
        ['Max drawdown / Sharpe / Sortino', `Not computed: needs a longer daily-snapshot series (have ${(overview.dailyHistory ?? []).length}).`],
        ['Avg risk : reward', RR_UNAVAILABLE],
      ]}/>
    </Section>
  </div>;
}

const csvCell = v => { const x = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(x) ? `"${x.replace(/"/g, '""')}"` : x; };
export function sessionCsv(day, fills, trades) {
  const lines = [['record', 'time', 'symbol', 'strategy', 'side', 'qty', 'price', 'entry', 'exit', 'gross_pnl', 'charges_inr', 'net_pnl_inr', 'reason'].join(',')];
  for (const f of fills) lines.push(['fill', f.filled_at, f.symbol, f.strategy, f.side, f.qty, f.price, '', '', f.side === 'SELL' ? f.realized_pnl : '', f.costs ? paiseToInr(f.costs.charges_paise) : '', '', f.reason].map(csvCell).join(','));
  for (const t of trades) lines.push(['trade', t.closed_at, t.symbol, t.strategy, 'SELL', t.qty, '', t.entry_price, t.exit_price, t.realized_pnl, t.charges_paise === null || t.charges_paise === undefined ? '' : paiseToInr(t.charges_paise), t.net_pnl_paise === null || t.net_pnl_paise === undefined ? '' : paiseToInr(t.net_pnl_paise), t.exit_reason].map(csvCell).join(','));
  return lines.join('\n') + '\n';
}
function downloadCsv(name, text) {
  const url = URL.createObjectURL(new Blob([text], {type: 'text/csv'}));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function DailyTab({overview, strategy, refreshKey}) {
  const base = strategy ? {strategy: String(strategy)} : {};
  const trades = useAllPages({view: 'trades', ...base}, refreshKey);
  const fills = useAllPages({view: 'fills', ...base}, refreshKey);
  const positions = useAllPages({view: 'positions', ...base}, refreshKey);
  const snapshots = overview.dailyHistory ?? [];
  const days = [...new Set([overview.day, ...snapshots.map(d => d.day), ...(fills.data?.rows ?? []).map(r => istDate(r.filled_at))].filter(Boolean))].sort().reverse();
  const [day, setDay] = useState(overview.day);
  if (trades.error || fills.error) return <Callout tone="error">{trades.error || fills.error}</Callout>;
  if (!trades.data || !fills.data) return <Loading label="Reading the session record…"/>;
  const t = trades.data.rows.filter(r => istDate(r.closed_at) === day), f = fills.data.rows.filter(r => istDate(r.filled_at) === day);
  const s = tradeStats(t), fs = fillStats(f), snap = snapshots.find(d => d.day === day);
  return <div className="ad-ledger-tab">
    <div className="ad-ledger-filters"><label className="ad-ledger-note" htmlFor="ad-daily-day">Session (IST)</label>
      <select id="ad-daily-day" className="ad-input" value={day} onChange={e => setDay(e.target.value)}>{days.map(d => <option key={d} value={d}>{d}{d === overview.day ? ' (today)' : ''}</option>)}</select></div>
    <Incomplete set={trades.data} what="closed trades"/><Incomplete set={fills.data} what="fills"/>
    <div className="ad-ledger-filters"><button className="ad-ghost" onClick={() => downloadCsv(`atlas-paper-${day}.csv`, sessionCsv(day, f, t))} disabled={!f.length && !t.length}>Export session CSV</button>
      <span className="ad-ledger-note">CSV of this session’s fills and closed trades as loaded from the ledger. PDF export is not available.</span></div>
    <Section eyebrow="Session performance journal" title={`${day} snapshot`}>
      {snap ? <KeyValues items={[['Daily MTM P&L', pnl(snap.daily)], ['Day return %', snap.daily === null ? '—' : pct(snap.daily / paiseToInr(overview.cappedLedger?.master_capital_paise) * 100, 4, true), 'daily MTM ÷ paper capital'], ['Latest known total P&L', pnl(snap.total)], ['Realized on closures today', pnl(snap.realizedClosuresToday)],
        ['Valuation live', snap.valuationIsLive ? 'yes' : 'no (last session marks)'], ['Missing daily basis', snap.missingDailyBasis?.length ? snap.missingDailyBasis.map(ticker).join(', ') : 'none'], ['Snapshot as of', at(snap.asOf)]]}/>
        : <Blank title="No EOD snapshot for this session" text="The 16:10 EOD snapshot has not been written for this day. Activity below is read directly from fills."/>}
    </Section>
    <div className="ad-tiles">
      <Tile label="Fills" value={fmt(fs.count, 0)} hint={`${fs.buys} buy · ${fs.sells} sell`}/>
      <Tile label="Closed trades" value={fmt(s.count, 0)} hint={`${s.wins} wins · ${s.losses} losses`}/>
      <Tile label="Today’s profit" value={signedInr(s.grossProfit)} tone={tone(s.grossProfit)}/>
      <Tile label="Today’s loss" value={signedInr(s.grossLoss)} tone={tone(s.grossLoss)}/>
      <Tile label="Realized (gross)" value={signedInr(s.grossPnl)} tone={tone(s.grossPnl)}/>
      <Tile label="Charges" value={inr(fs.charges)} hint={fs.missingCosts ? `${fs.missingCosts} fills lack fee rows` : 'Paper estimate'}/>
      <Tile label="Turnover" value={inr(fs.turnover)}/>
      <Tile label="Avg order value" value={inr(fs.avgSize)}/>
      <Tile label="Capital used (buys)" value={inr(fs.buyValue)} hint="Buy turnover this session"/>
      <Tile label="Avg hold (closed)" value={hours(s.avgHoldHours)}/>
      {day === overview.day && <>
        <Tile label="Unrealized P&L (now)" value={signedInr(overview.totals?.unrealized)} tone={tone(overview.totals?.unrealized)} hint="Gross, last marks"/>
        <Tile label="Cash (now)" value={inr(paiseToInr(overview.cappedLedger?.cash_paise), 0)} hint="Across sleeves"/>
        <Tile label="Open positions (now)" value={fmt(overview.totals?.openPositions, 0)}/>
      </>}
    </div>
    <div className="ad-split">
      <Section eyebrow="Edge metrics" title="Session edge"><KeyValues items={[['Win rate', pct(s.winRate)], ['Loss rate', pct(s.lossRate)], ['Profit factor', fmt(s.profitFactor)], ['Expectancy', signedInr(s.expectancy)],
        ['Largest win', signedInr(s.largestProfit)], ['Largest loss', signedInr(s.largestLoss)], ['Avg win', signedInr(s.avgProfit)], ['Avg loss', signedInr(s.avgLoss)], ['Net P&L', signedInr(s.netPnl)]]}/></Section>
      <Section eyebrow="Best / worst" title="Trades of the session"><KeyValues items={[
        ['Best trade', s.best ? <>{sym(s.best.symbol)} {inr(s.best.entry_price)} → {inr(s.best.exit_price)} · {pnl(s.best.realized_pnl)} · {hours(s.best.hold_hours)}</> : 'No closed trades'],
        ['Best trade reason', s.best ? code(s.best.exit_reason) : '—'],
        ['Worst trade', s.worst ? <>{sym(s.worst.symbol)} {pnl(s.worst.realized_pnl)} · {hours(s.worst.hold_hours)}</> : 'No closed trades'],
        ['Worst trade reason', s.worst ? code(s.worst.exit_reason) : '—']]}/></Section>
    </div>
    <Section eyebrow="Time analysis" title="Fills by session slot (IST)"><SortTable rows={sessionSlots(f)} rowKey={r => r.slot} pageSize={12} caption="Fills by session slot" empty={{title: 'No fills this session'}}
      columns={[{key: 'slot', title: 'Slot'}, {key: 'fills', title: 'Fills', align: 'right'}, {key: 'buys', title: 'Buys', align: 'right'}, {key: 'sells', title: 'Sells', align: 'right'}, {key: 'pnl', title: 'Realized (gross)', align: 'right', render: pnl}]}/></Section>
    <div className="ad-split">
      <Section eyebrow="Symbol performance" title="Per-name results"><SortTable rows={t} pageSize={12} caption="Per-name results" empty={{title: 'No closed trades this session'}}
        columns={[{key: 'symbol', title: 'Stock', render: sym}, {key: 'entry_price', title: 'Entry', align: 'right', render: money}, {key: 'exit_price', title: 'Exit', align: 'right', render: money}, {key: 'qty', title: 'Qty', align: 'right'}, {key: 'pnl_pct', title: 'Return %', align: 'right', render: pctCell}, {key: 'hold_hours', title: 'Hold', align: 'right', render: hours}, {key: 'realized_pnl', title: 'P&L', align: 'right', render: pnl}]}/></Section>
      <Section eyebrow="Sector analysis" title="Allocation & P&L"><SortTable rows={groupBy(f.filter(r => r.side === 'BUY').map(r => ({...r, entry_price: r.price})), r => r.industry)} rowKey={r => r.key} pageSize={12} caption="Sector allocation" empty={{title: 'No buys this session'}}
        columns={[{key: 'key', title: 'Sector (NSE industry)'}, {key: 'trades', title: 'Buys', align: 'right'}, {key: 'allocation', title: 'Allocation', align: 'right', render: money}]}/></Section>
    </div>
    {day === overview.day ? <ExposureSection positions={positions} title="Exposure & allocation (current open positions, now)"/>
      : <p className="ad-ledger-note">Exposure and allocation are shown for today only: the ledger does not keep a per-session position snapshot.</p>}
    <Section eyebrow="Not shown" title="Unavailable for a paper session">
      <Unavailable items={[
        ['Edge: risk : reward', RR_UNAVAILABLE],
        ['Edge: max drawdown / Sharpe / Sortino / recovery', `Need a longer EOD equity series; ${snapshots.length} daily snapshot(s) recorded.`],
        ['Session backdrop', 'No market-regime or session-backdrop record is stored by the engine.'],
        ['Hourly P&L curve', 'No intraday P&L series is stored; only fills and the EOD snapshot.'],
        ['AI coach notes / process scores', 'Not produced by the paper engine; nothing is generated here.'],
        ['Daily journal', 'Atlas Desk is read-only; there is no journal store.'],
        ['Market context (Nifty, Bank Nifty, VIX, breadth)', 'Not recorded in the paper ledger.'],
      ]}/>
    </Section>
  </div>;
}

function CapitalTab({overview, strategy, scope, refreshKey}) {
  const c = overview.cappedLedger, t = overview.totals;
  const sleeves = [1, 2, 3, 4, 5, 6, 7].map(id => ({id, ...(c?.sleeves?.[id] ?? {})}));
  const net = paiseToInr(c?.headline_net_pnl_paise);
  return <div className="ad-ledger-tab">
    <Section eyebrow="Account summary" title="Summary">
      <div className="ad-tiles">
        <Tile label="Starting capital" value={inr(paiseToInr(c?.master_capital_paise), 0)} hint={`${c?.sleeve_count ?? 7} sleeves × ${inr(paiseToInr(c?.sleeve_capital_paise), 0)}`}/>
        <Tile label="Current total capital" value={inr(paiseToInr(c?.equity_paise), 2)} hint="Capped book equity"/>
        <Tile label="Available funds" value={inr(paiseToInr(c?.cash_paise), 2)} hint="Cash across sleeves"/>
        <Tile label="Invested (market value)" value={inr(paiseToInr(c?.market_value_paise), 2)} hint={c?.marks_missing ? 'Unavailable: marks or fee rows missing' : 'At last marks'}/>
        <Tile label="Net P&L" value={signedInr(net)} tone={tone(net)} hint={c?.capital_return_pct !== null && c?.capital_return_pct !== undefined ? pct(c.capital_return_pct, Math.abs(c.capital_return_pct) < 0.01 ? 4 : 2, true) : 'Unavailable'}/>
        <Tile label="Total realized P&L" value={signedInr(t.realized)} tone={tone(t.realized)} hint="Gross"/>
        <Tile label="Total unrealized P&L" value={signedInr(t.unrealized)} tone={tone(t.unrealized)} hint="Gross"/>
        <Tile label="Open positions" value={fmt(t.openPositions, 0)}/>
        <Tile label="Open orders" value={fmt(t.pendingSignals, 0)} hint="Pending signals"/>
      </div>
    </Section>
    <Section eyebrow="Configuration" title="Account settings (policy, read-only)">
      <KeyValues items={[['Capital policy', code(c?.policy_version)], ['Paper capital', inr(paiseToInr(c?.master_capital_paise), 0)], ['Per-sleeve capital', inr(paiseToInr(c?.sleeve_capital_paise), 0)],
        ['Target notional per entry', inr(paiseToInr(c?.target_notional_paise), 0)], ['Max positions per sleeve', fmt(c?.max_positions_per_sleeve, 0)], ['Max strategies per ISIN', fmt(c?.max_strategies_per_isin, 0)],
        ['ADV floor', inr(c?.adv_floor_inr, 0)], ['Uncovered legacy fills', fmt(c?.uncovered_fills, 0), 'fills without a fee row are never valued as zero-cost'],
        ['Set starting capital / reset account', 'Not available here: capital is fixed by the policy file and Atlas Desk never writes to the ledger.']]}/>
    </Section>
    <Section eyebrow="Sleeves" title="Cash by strategy sleeve">
      <SortTable rows={sleeves} rowKey={r => r.id} pageSize={7} caption="Cash by sleeve" columns={[
        {key: 'id', title: 'Sleeve', render: strat}, {key: 'cash_paise', title: 'Cash', align: 'right', render: paise}, {key: 'market_value_paise', title: 'Market value', align: 'right', render: paise},
        {key: 'equity_paise', title: 'Equity', align: 'right', render: paise}, {key: 'net_pnl_paise', title: 'Net P&L', align: 'right', render: pnlPaise},
        {key: 'open_positions', title: 'Open', align: 'right'}, {key: 'slots_max', title: 'Slots', align: 'right'}]}/>
    </Section>
    <Section eyebrow="Transaction log" title="Transactions (every fill’s cash movement)">
      <PagedTab view="transactions" apiView="costs" columns={COST_COLUMNS} scope={scope} strategy={strategy} refreshKey={refreshKey} detail={false}
        notes={d => d?.balanceNote && <p className="ad-ledger-note">{d.balanceNote}</p>}
        empty={{title: 'No transactions recorded', text: 'No fee/cash-flow rows match these filters.'}}/>
    </Section>
    <Section eyebrow="Cash credits" title="Dividends and other credits">
      <PagedTab view="credits" apiView="credits" columns={CREDIT_COLUMNS} scope={scope} strategy={strategy} refreshKey={refreshKey} detail={false}
        empty={{title: 'No cash credits recorded', text: 'No dividend or corporate-action credit has been booked.'}}/>
    </Section>
  </div>;
}

function AccountStrip({overview}) {
  const c = overview?.cappedLedger, t = overview?.totals ?? {};
  if (!c) return null;
  const net = paiseToInr(c.headline_net_pnl_paise);
  return <div className="ad-tiles ad-account-strip">
    <Tile label="Total capital" value={inr(paiseToInr(c.master_capital_paise), 0)}/>
    <Tile label="Equity / balance" value={inr(paiseToInr(c.equity_paise), 2)} hint="Capped book"/>
    <Tile label="Available funds" value={inr(paiseToInr(c.cash_paise), 2)}/>
    <Tile label="Invested value" value={inr(paiseToInr(c.market_value_paise), 2)} hint={c.marks_missing ? 'Unavailable: a mark is missing' : 'At last marks'}/>
    <Tile label="Realized P/L" value={signedInr(t.realized)} tone={tone(t.realized)} hint="Gross"/>
    <Tile label="Unrealized P/L" value={signedInr(t.unrealized)} tone={tone(t.unrealized)} hint="Gross"/>
    <Tile label="Total P/L (net)" value={signedInr(net)} tone={tone(net)} hint="After recorded charges"/>
    <Tile label="Daily P/L" value={signedInr(t.daily)} tone={tone(t.daily)} hint={t.daily === null ? 'No snapshot today yet' : 'EOD snapshot'}/>
    <Tile label="Open positions" value={fmt(t.openPositions, 0)}/>
    <Tile label="Open orders" value={fmt(t.pendingSignals, 0)} hint="Pending signals"/>
  </div>;
}

function RunHealth({data}) {
  const h = data?.health;
  if (!h) return null;
  return <div className="ad-tiles">
    {h.latestByKind.map(r => <Tile key={r.kind} label={`Last ${r.kind}`} value={<Tag tone={statusTone(r.status)}>{r.status}</Tag>} hint={`${at(r.at)} · ${r.runs} runs`}/>)}
    <Tile label="Last accepted quote" value={at(h.lastAcceptedQuoteAt)} hint={`${h.quotedSymbols} symbols ever quoted`}/>
  </div>;
}

export default function LedgerBrowser({overview, refreshKey, view, onView}) {
  const [scope, setScope] = useState('all');
  const [strategy, setStrategy] = useState(0);
  const [signalStatus, setSignalStatus] = useState('');
  const [runKind, setRunKind] = useState('');
  const [accepted, setAccepted] = useState('');
  const t = overview?.totals ?? {};
  const strategyTabs = ['positions', 'orders', 'history', 'fills', 'analytics', 'daily', 'alerts', 'capital', 'signals'];
  const scopeTabs = ['positions', 'orders', 'history', 'fills', 'alerts', 'capital', 'signals', 'runs', 'quotes', 'dataaudit'];
  const common = {scope, strategy: strategyTabs.includes(view) ? strategy : 0, refreshKey};
  const empty = what => ({title: `No ${what} recorded`, text: scope === 'today' ? 'Nothing for today yet (IST). Switch to “All dates” for the full history.' : 'The engine has not recorded any matching rows.'});
  const counts = {positions: t.openPositions, orders: t.pendingSignals, fills: t.filledOrders, history: t.closedTrades};
  return <Section eyebrow="Ledger" title="Browse the paper ledger" id="ad-ledger" className="ad-ledger-palette" actions={<div className="ad-ledger-filters">
    {scopeTabs.includes(view) && <select className="ad-input" aria-label="Date scope" value={scope} onChange={e => setScope(e.target.value)}><option value="all">All dates</option><option value="today">Today (IST)</option></select>}
    {strategyTabs.includes(view) && <select className="ad-input" aria-label="Strategy filter" value={strategy} onChange={e => setStrategy(Number(e.target.value))}>
      <option value={0}>All strategies</option>{[1, 2, 3, 4, 5, 6, 7].map(i => <option key={i} value={i}>{strategyName(i)}</option>)}</select>}
    {view === 'signals' && <select className="ad-input" aria-label="Signal status" value={signalStatus} onChange={e => setSignalStatus(e.target.value)}>
      <option value="">All statuses</option>{SIGNAL_STATUSES.map(s => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}</select>}
    {view === 'runs' && <select className="ad-input" aria-label="Run kind" value={runKind} onChange={e => setRunKind(e.target.value)}>
      <option value="">All kinds</option>{RUN_KINDS.map(s => <option key={s} value={s}>{s}</option>)}</select>}
    {view === 'quotes' && <select className="ad-input" aria-label="Quote acceptance" value={accepted} onChange={e => setAccepted(e.target.value)}>
      <option value="">Accepted and rejected</option><option value="1">Accepted only</option><option value="0">Rejected only</option></select>}
  </div>}>
    <div className="ad-ledger-tabs"><Tabs label="Ledger view" items={LEDGER_TABS.map(([id, text]) => [id, text, counts[id]])} value={view} onChange={onView}/></div>
    {view === 'positions' && <PagedTab view="positions" apiView="positions" columns={POSITION_COLUMNS(overview?.market)} {...common} empty={{...empty('open positions'), title: 'No open positions'}}
      notes={() => <p className="ad-ledger-note">Marks: <strong>fresh</strong> ≤60s verified quote · <strong>stale</strong> older quote kept (expected outside market hours) · <strong>missing</strong> no valid quote, so P&amp;L is unavailable. Stop: Strategy 07 shows its live trail ({TRAIL_FRACTION} × completed-close high-water, {TRAIL_PCT_BELOW}% below); Strategies 01–06 have no fixed rupee stop and show their exit rule, with a rule level only when it follows from stored completed bars. No strategy has a fixed target, so no R:R. Charges = recorded entry fee + engine-estimated exit fee. Exit / square off are not available on this read-only desk.</p>}
      header={() => <AccountStrip overview={overview}/>}/>}
    {view === 'orders' && <PagedTab view="orders" apiView="signals" params={{status: 'pending'}} columns={ORDER_COLUMNS} {...common} empty={{title: 'No pending orders', text: 'The scan queues entries and exits as pending signals for the next eligible session; none are waiting now.'}}
      notes={() => <p className="ad-ledger-note">Open orders are the scan’s pending signals. They carry no limit price or quantity: the engine sizes the order from target notional and fills it at a verified live quote in the eligible session. Edit and cancel are not available (read-only).</p>}/>}
    {view === 'history' && <PagedTab view="history" apiView="trades" columns={TRADE_COLUMNS} {...common} empty={{...empty('closed trades'), title: 'No trade history', text: 'Closed round trips appear here after the engine exits a position.'}}
      notes={d => <p className="ad-ledger-note">{d?.pairing ?? 'Each exit is paired with its entry fill.'} Select a row for the exit details and the charges of both fills.</p>}/>}
    {view === 'fills' && <PagedTab view="fills" apiView="fills" columns={FILL_COLUMNS} {...common} empty={empty('fills')}
      notes={d => <p className="ad-ledger-note">Every engine fill, newest first{d?.costsAvailable === false ? '. The fee ledger is unavailable, so charges cannot be shown.' : '. Select a row for its charge breakdown (paper estimate, not a broker bill).'}</p>}/>}
    {view === 'analytics' && <AnalyticsTab overview={overview} strategy={strategy} refreshKey={refreshKey}/>}
    {view === 'daily' && <DailyTab overview={overview} strategy={strategy} refreshKey={refreshKey}/>}
    {view === 'alerts' && <>
      <PagedTab view="alerts" apiView="events" columns={ALERT_COLUMNS} {...common} empty={{title: 'No engine alerts recorded', text: 'No stop hits, blocked orders or warnings match these filters.'}}
        notes={() => <p className="ad-ledger-note">Engine alerts from the audit log: stop/risk hits, blocked paper orders and WARN/ERROR/CRITICAL events.</p>}/>
      <Callout tone="info" title="Price alerts you set yourself are unavailable.">Atlas Desk is read-only and the paper engine has no user alert store, so there is no create/delete alert form.</Callout>
    </>}
    {view === 'capital' && <CapitalTab overview={overview} strategy={common.strategy} scope={scope} refreshKey={refreshKey}/>}
    {view === 'signals' && <PagedTab view="signals" apiView="signals" params={signalStatus ? {status: signalStatus} : {}} columns={SIGNAL_COLUMNS} {...common} empty={empty('signals')}/>}
    {view === 'runs' && <PagedTab view="runs" apiView="runs" params={runKind ? {kind: runKind} : {}} columns={RUN_COLUMNS} {...common} header={d => <RunHealth data={d}/>} empty={empty('engine runs')}
      notes={() => <p className="ad-ledger-note">Filter by kind to find the daily scan among the 5-minute monitor checks. Details are a whitelisted summary of each run report.</p>}/>}
    {view === 'quotes' && <PagedTab view="quotes" apiView="quotes" params={accepted ? {accepted} : {}} columns={QUOTE_COLUMNS} {...common} empty={empty('quote checks')}/>}
    {view === 'dataaudit' && <PagedTab view="data audit" apiView="dataaudit" columns={DATA_AUDIT_COLUMNS} {...common} empty={{title: 'No excluded bars', text: 'No candle has been excluded by the input checks.'}}
      notes={() => <p className="ad-ledger-note">Daily bars the engine refused to use (unverified session, invalid OHLC). Excluded bars never generate signals.</p>}/>}
    {view === 'watchlist' && <Blank title="Watchlist unavailable" text="The paper engine has no watchlist store and this desk cannot save favourites. The latest qualifier list for each strategy is on the Strategy tester screen."/>}
  </Section>;
}
