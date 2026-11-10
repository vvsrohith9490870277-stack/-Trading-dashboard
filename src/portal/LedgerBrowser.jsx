// "Browse the paper ledger": every tab of the reference paper desk (Positions, Orders, History,
// Analytics, Daily, Alerts, Capital, Watchlist) plus the engine audit tabs (Fills, Signals, Engine
// runs, Quote audit, Data audit). Read-only. Every value comes from /api/dashboard; anything the
// ledger does not record is labelled unavailable instead of being estimated.
import {useState} from 'react';
import {Link, stockHref} from './router';
import {paiseToInr, useAllPages, useDashboard} from './data';
import {fmt, inr, istTime, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, KeyValues, Loading, Section, SortTable, Tabs, Tag, Tile} from './ui';
import {Columns} from './charts';
import {exitSource, fillStats, groupBy, inRange, istDate, RANGES, rangeBounds, sessionSlots, signalKind, tradeStats} from './ledger-stats';

export const LEDGER_TABS = [
  ['positions', 'Positions'], ['orders', 'Orders'], ['history', 'History'], ['fills', 'Fills'], ['analytics', 'Analytics'], ['daily', 'Daily'],
  ['alerts', 'Alerts'], ['capital', 'Capital'], ['signals', 'Signals'], ['runs', 'Engine runs'], ['quotes', 'Quote audit'], ['dataaudit', 'Data audit'], ['watchlist', 'Watchlist'],
];
const LIMIT = 25;
export const SIGNAL_STATUSES = ['pending', 'filled', 'expired', 'blocked_prefilter', 'ignored_state', 'ignored_stale_position', 'ignored_same_bar', 'shadow_closed'];
const RUN_KINDS = ['monitor', 'scan', 'bootstrap', 'probe', 'init', 'pnl', 'status'];
const FEE_LABELS = [['brokerage', 'Brokerage'], ['stt', 'STT'], ['exchange', 'Exchange txn'], ['sebi', 'SEBI fee'], ['ipft', 'IPFT'], ['stamp', 'Stamp duty'], ['gst', 'GST'], ['clearing', 'Clearing'], ['dp', 'DP charge'], ['dp_gst', 'DP GST']];

const NA = ({why}) => <span className="ad-muted-cell" title={why}>n/a</span>;
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

const POSITION_COLUMNS = market => [
  {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat}, {key: 'industry', title: 'Sector'},
  {key: 'qty', title: 'Qty', align: 'right'}, {key: 'entry_price', title: 'Avg entry', align: 'right', render: money},
  {key: 'cost_basis', title: 'Cost basis', align: 'right', render: money}, {key: 'mark_price', title: 'Last mark', align: 'right', render: money},
  {key: 'market_value', title: 'Market value', align: 'right', render: money}, {key: 'unrealized_pnl', title: 'Unrealized', align: 'right', render: pnl},
  {key: 'unrealized_pct', title: '% P&L', align: 'right', render: pctCell}, {key: 'high_water', title: 'High-water', align: 'right', render: money},
  {key: 'stop', title: 'Stop', render: () => <NA why="The engine evaluates exits at each check; no stop level is stored on the position."/>},
  {key: 'target', title: 'Target', render: () => <NA why="Strategies exit on rules; no fixed target is defined (target_defined = false)."/>},
  {key: 'mark_state', title: 'Monitoring / mark', render: (v, r) => markCell(v, r, market), sortValue: r => r.mark_age_seconds},
  {key: 'mark_at', title: 'Quote time', render: at}, {key: 'entry_at', title: 'Opened', render: at}, {key: 'updated_at', title: 'Updated', render: at},
];
const ORDER_COLUMNS = [
  {key: 'created_at', title: 'Order time', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
  {key: 'side', title: 'Side', render: side}, {key: 'reason', title: 'Type', render: v => signalKind(v), sortValue: r => signalKind(r.reason)},
  {key: 'qty', title: 'Qty', render: () => <NA why="Sized at fill time from target notional and the verified fill quote."/>},
  {key: 'price', title: 'Requested price', render: () => <NA why="Signals carry no limit price; the fill uses a verified live quote."/>},
  {key: 'bar_date', title: 'Signal bar'}, {key: 'eligible_date', title: 'Expected execution', render: v => (v ? `${v} session` : '—')},
  {key: 'status', title: 'Status', render: status}, {key: 'reason_code', title: 'Reason', render: (v, r) => code(r.reason)},
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
  {key: 'quote_at', title: 'Quote time', render: at}, {key: 'reason', title: 'Reason', render: code},
];
const TRADE_COLUMNS = [
  {key: 'closed_at', title: 'Closed', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy / signal', render: strat},
  {key: 'qty', title: 'Qty', align: 'right'}, {key: 'entry_price', title: 'Entry', align: 'right', render: money}, {key: 'exit_price', title: 'Exit', align: 'right', render: money},
  {key: 'realized_pnl', title: 'P&L (gross)', align: 'right', render: pnl}, {key: 'pnl_pct', title: 'P&L %', align: 'right', render: pctCell},
  {key: 'charges_paise', title: 'Charges', align: 'right', render: v => (v === null || v === undefined ? <NA why="A fee row is missing for the entry or exit fill"/> : paise(v))},
  {key: 'net_pnl_paise', title: 'Net P&L', align: 'right', render: v => (v === null || v === undefined ? <NA why="Needs fee rows for both fills and an equal entry/exit quantity"/> : pnlPaise(v))},
  {key: 'score', title: 'Score', render: () => <NA why="The paper ledger does not store a signal score."/>},
  {key: 'opened_at', title: 'Opened', render: at}, {key: 'hold_hours', title: 'Hold', align: 'right', render: hours},
  {key: 'exit_reason', title: 'Exit reason', render: code},
];
const ALERT_COLUMNS = [
  {key: 'source_at', title: 'Time', render: at}, {key: 'event_type', title: 'Alert', render: v => String(v).replace(/_/g, ' ')},
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
  {key: 'at', title: 'At', render: at}, {key: 'kind', title: 'Kind'}, {key: 'status', title: 'Status', render: status},
  {key: 'summary', title: 'Details', render: v => <span className="ad-muted-cell">{runSummaryText(v)}</span>, sortValue: r => runSummaryText(r.summary)},
];
const QUOTE_COLUMNS = [
  {key: 'observed_at', title: 'Observed', render: at}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'price', title: 'Price', align: 'right', render: money},
  {key: 'accepted', title: 'Accepted', render: v => <Tag tone={Number(v) === 1 ? 'good' : 'warn'}>{Number(v) === 1 ? 'yes' : 'no'}</Tag>},
  {key: 'quote_at', title: 'Exchange time', render: at}, {key: 'lag_seconds', title: 'Lag', align: 'right', render: v => (v === null || v === undefined ? '—' : age(Math.abs(v)) + (v < 0 ? ' ahead' : ''))},
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

function AnalyticsTab({overview, strategy, refreshKey}) {
  const [range, setRange] = useState('all');
  const base = strategy ? {strategy: String(strategy)} : {};
  const trades = useAllPages({view: 'trades', ...base}, refreshKey);
  const fills = useAllPages({view: 'fills', ...base}, refreshKey);
  const pending = useDashboard({view: 'signals', status: 'pending', limit: '1', ...base}, refreshKey);
  const expired = useDashboard({view: 'signals', status: 'expired', limit: '1', ...base}, refreshKey);
  const blocked = useDashboard({view: 'signals', status: 'blocked_prefilter', limit: '1', ...base}, refreshKey);
  if (trades.error || fills.error) return <Callout tone="error">{trades.error || fills.error}</Callout>;
  if (!trades.data || !fills.data) return <Loading label="Reading the complete trade and fill record…"/>;
  const bounds = rangeBounds(range, overview.day);
  const t = trades.data.rows.filter(r => inRange(r.closed_at, bounds));
  const f = fills.data.rows.filter(r => inRange(r.filled_at, bounds));
  const s = tradeStats(t), fs = fillStats(f), c = overview.cappedLedger, tot = overview.totals;
  const daily = (overview.dailyHistory ?? []).filter(d => !bounds[0] || (d.day >= bounds[0] && d.day <= bounds[1]));
  const all = range === 'all' && !strategy;
  const countOf = q => (q.data ? q.data.total : null);
  return <div className="ad-ledger-tab">
    <div className="ad-ranges" role="group" aria-label="Analytics range">{RANGES.map(([id, label]) => <button key={id} className={`ad-chip ${range === id ? 'on' : ''}`} aria-pressed={range === id} onClick={() => setRange(id)}>{label}</button>)}</div>
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
    <Section eyebrow="Charts" title="Cumulative P&L (latest known total per snapshot)">
      <Columns rows={daily.map(d => ({label: d.day, value: d.total}))} unit="" empty={{title: 'No snapshots in range'}}/>
    </Section>
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
        ['Avg risk : reward', 'No stop or target levels are stored per trade.'],
        ['Trade frequency / portfolio allocation charts', 'Shown as tables above (per stock, sector, strategy) instead of pie charts.'],
      ]}/>
    </Section>
  </div>;
}

function DailyTab({overview, strategy, refreshKey}) {
  const base = strategy ? {strategy: String(strategy)} : {};
  const trades = useAllPages({view: 'trades', ...base}, refreshKey);
  const fills = useAllPages({view: 'fills', ...base}, refreshKey);
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
    <Section eyebrow="Session performance journal" title={`${day} snapshot`}>
      {snap ? <KeyValues items={[['Daily MTM P&L', pnl(snap.daily)], ['Latest known total P&L', pnl(snap.total)], ['Realized on closures today', pnl(snap.realizedClosuresToday)],
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
    <Section eyebrow="Not shown" title="Unavailable for a paper session">
      <Unavailable items={[
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
  return <Section eyebrow="Ledger" title="Browse the paper ledger" id="ad-ledger" actions={<div className="ad-ledger-filters">
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
      notes={() => <p className="ad-ledger-note">Marks: <strong>fresh</strong> ≤60s verified quote · <strong>stale</strong> older quote kept (expected outside market hours) · <strong>missing</strong> no valid quote, so P&amp;L is unavailable. Exits are rule-based; no stop or target level is stored. Square-off and exit are not available on this read-only desk.</p>}/>}
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
