import {useState} from 'react';
import {Link, stockHref} from './router';
import {paiseToInr, useDashboard} from './data';
import {fmt, inr, istTime, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, Loading, Meter, Section, SortTable, Tabs, Tag, Tile} from './ui';

const VIEWS = [['positions', 'Open positions'], ['fills', 'Fills'], ['signals', 'Signals'], ['runs', 'Engine runs'], ['quotes', 'Quote audit']];
const LIMIT = 25;

const sym = v => <Link className="ad-sym" href={stockHref(v)}>{ticker(v)}</Link>;
const strat = v => <span className="ad-strat"><i style={{background: STRATEGY_HUES[Number(v) - 1]}}/>{strategyName(v)}</span>;
const money = v => inr(v);
const pnl = v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>;

const COLUMNS = {
  positions: [
    {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
    {key: 'qty', title: 'Qty', align: 'right'}, {key: 'entry_price', title: 'Entry', align: 'right', render: money},
    {key: 'mark_price', title: 'Last mark', align: 'right', render: money}, {key: 'unrealized_pnl', title: 'Unrealized', align: 'right', render: pnl},
    {key: 'mark_stale', title: 'Mark', render: v => <Tag tone={v ? 'warn' : 'good'}>{v ? 'stale / missing' : '≤60s'}</Tag>},
    {key: 'mark_at', title: 'Quote time', render: v => istTime(v)}, {key: 'entry_at', title: 'Opened', render: v => istTime(v)},
  ],
  fills: [
    {key: 'filled_at', title: 'Filled', render: v => istTime(v)}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
    {key: 'side', title: 'Side', render: v => <Tag tone={v === 'BUY' ? 'good' : 'bad'}>{v}</Tag>}, {key: 'qty', title: 'Qty', align: 'right'},
    {key: 'price', title: 'Fill price', align: 'right', render: money}, {key: 'realized_pnl', title: 'Realized', align: 'right', render: v => (v === null || v === undefined ? '—' : pnl(v))},
    {key: 'quote_at', title: 'Quote time', render: v => istTime(v)}, {key: 'reason', title: 'Reason', render: v => <code className="ad-code">{v}</code>},
  ],
  signals: [
    {key: 'created_at', title: 'Detected', render: v => istTime(v)}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'strategy', title: 'Strategy', render: strat},
    {key: 'side', title: 'Side'}, {key: 'status', title: 'Status', render: v => <Tag tone={v === 'filled' ? 'good' : v === 'pending' ? 'info' : 'neutral'}>{v}</Tag>},
    {key: 'bar_date', title: 'Bar'}, {key: 'eligible_date', title: 'Eligible'}, {key: 'reason', title: 'Reason', render: v => <code className="ad-code">{v}</code>},
  ],
  runs: [{key: 'at', title: 'At', render: v => istTime(v)}, {key: 'kind', title: 'Kind'}, {key: 'status', title: 'Status', render: v => <Tag tone={['ok', 'success'].includes(v) ? 'good' : v === 'failed' ? 'bad' : 'neutral'}>{v}</Tag>}],
  quotes: [
    {key: 'observed_at', title: 'Observed', render: v => istTime(v)}, {key: 'symbol', title: 'Stock', render: sym}, {key: 'price', title: 'Price', align: 'right', render: money},
    {key: 'accepted', title: 'Accepted', render: v => <Tag tone={Number(v) === 1 ? 'good' : 'warn'}>{Number(v) === 1 ? 'yes' : 'no'}</Tag>},
    {key: 'quote_at', title: 'Exchange time', render: v => istTime(v)}, {key: 'reason', title: 'Reason', render: v => <code className="ad-code">{v}</code>},
  ],
};

function LedgerBrowser({refreshKey}) {
  const [view, setView] = useState('positions');
  const [scope, setScope] = useState('all');
  const [strategy, setStrategy] = useState(0);
  const [page, setPage] = useState(1);
  const params = {view, scope, page: String(page), limit: String(LIMIT)};
  if (strategy && ['fills', 'positions', 'signals'].includes(view)) params.strategy = String(strategy);
  const {data, error, loading} = useDashboard(params, refreshKey);
  const total = data?.total ?? 0;
  const reset = fn => v => { fn(v); setPage(1); };
  return <Section eyebrow="Ledger" title="Browse the paper ledger" actions={<>
    <select className="ad-input" aria-label="Date scope" value={scope} onChange={e => reset(setScope)(e.target.value)}><option value="all">All dates</option><option value="today">Today (IST)</option></select>
    {['fills', 'positions', 'signals'].includes(view) && <select className="ad-input" aria-label="Strategy filter" value={strategy} onChange={e => reset(setStrategy)(Number(e.target.value))}>
      <option value={0}>All strategies</option>{[1, 2, 3, 4, 5, 6, 7].map(i => <option key={i} value={i}>{strategyName(i)}</option>)}</select>}
  </>}>
    <Tabs label="Ledger view" items={VIEWS} value={view} onChange={reset(setView)}/>
    {error && <Callout tone="error">{error}</Callout>}
    {loading && !data ? <Loading/> : <SortTable key={view} rows={data?.rows ?? []} columns={COLUMNS[view]} pageSize={LIMIT} caption={view}
      empty={{title: `No ${view} recorded`, text: scope === 'today' ? 'Nothing for today yet (IST). Switch to “All dates” for the full history.' : 'The engine has not recorded any matching rows.'}}/>}
    <div className="ad-pager wide">
      <span>{total} matching · page {page} of {Math.max(1, Math.ceil(total / LIMIT))} · server-paginated</span>
      <div><button disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Previous</button><button disabled={page * LIMIT >= total || loading} onClick={() => setPage(page + 1)}>Next</button></div>
    </div>
  </Section>;
}

function SleeveBoard({live}) {
  const capped = live.cappedLedger;
  const perf = live.performance ?? [];
  return <Section eyebrow="Capital sleeves" title="Seven strategy sleeves">
    <div className="ad-sleeves">{[1, 2, 3, 4, 5, 6, 7].map(id => {
      const s = capped?.sleeves?.[id], p = perf.find(r => r.strategy === id);
      const used = s?.open_positions ?? p?.openPositions ?? 0, max = s?.slots_max ?? capped?.max_positions_per_sleeve ?? 0;
      return <article key={id} className="ad-sleeve" style={{'--ad-accent': STRATEGY_HUES[id - 1]}}>
        <header><span className="ad-sleeve-id">{String(id).padStart(2, '0')}</span><h3>{strategyName(id)}</h3></header>
        <div className="ad-sleeve-row"><span>Slots</span><strong className={max && used > max ? 'ad-down' : ''}>{used} / {max || '—'}</strong></div>
        <Meter value={used} max={max} label={`${strategyName(id)} slots used`}/>
        <div className="ad-sleeve-row"><span>Equity</span><strong>{inr(paiseToInr(s?.equity_paise), 0)}</strong></div>
        <div className="ad-sleeve-row"><span>Net P&amp;L</span><strong className={`ad-${tone(s?.net_pnl_paise)}`}>{signedInr(paiseToInr(s?.net_pnl_paise))}</strong></div>
        <div className="ad-sleeve-row"><span>Closed · win</span><strong>{p?.closedTrades ?? 0} · {pct(p?.winRate)}</strong></div>
        {max > 0 && used > max && <Tag tone="bad">Over slot limit — no new buys</Tag>}
      </article>;
    })}</div>
  </Section>;
}

export default function PaperDesk({overview, refreshKey}) {
  const live = overview.data;
  if (!live) return overview.loading ? <Loading label="Reading the paper ledger…"/> : <Blank title="Live ledger unavailable" text="The paper ledger could not be read. Nothing is shown rather than placeholder numbers."/>;
  const t = live.totals, c = live.cappedLedger;
  const net = paiseToInr(c?.headline_net_pnl_paise);
  return <div className="ad-screen">
    <div className="ad-hero">
      <div><span className="ad-eyebrow">Paper desk · {live.day} IST</span><h1>What the engine is holding right now</h1>
        <p>Orders are placed only by the scheduled paper engine. This desk is read-only — there is no order ticket and no broker connection.</p></div>
      <div className="ad-hero-figure"><span>Capped-ledger net P&amp;L</span><strong className={`ad-${tone(net)}`}>{signedInr(net)}</strong>
        <small>{c?.capital_return_pct !== null && c?.capital_return_pct !== undefined ? `${pct(c.capital_return_pct, 2, true)} of ${inr(paiseToInr(c.master_capital_paise), 0)} paper capital` : c?.marks_missing ? 'Unavailable: some marks or fee rows are missing' : 'Unavailable'}</small></div>
    </div>
    <div className="ad-tiles">
      <Tile label="Equity (capped book)" value={inr(paiseToInr(c?.equity_paise), 0)} hint={`Cash ${inr(paiseToInr(c?.cash_paise), 0)}`}/>
      <Tile label="Open positions" value={fmt(t.openPositions, 0)} hint={`${t.staleMarks} stale · ${t.missingMarks} missing marks`} tone={t.staleMarks ? 'warn' : 'flat'}/>
      <Tile label="Unrealized" value={signedInr(t.unrealized)} tone={tone(t.unrealized)} hint="Gross, from last marks"/>
      <Tile label="Realized" value={signedInr(t.realized)} tone={tone(t.realized)} hint={`${t.closedTrades} closed trades`}/>
      <Tile label="Today’s fills" value={fmt(t.todayFills, 0)} hint={`${t.filledOrders} fills all-time`}/>
      <Tile label="Pending signals" value={fmt(t.pendingSignals, 0)} hint={`${live.signalsToday ?? 0} detected today`}/>
    </div>
    <SleeveBoard live={live}/>
    <div className="ad-split">
      <Section eyebrow="Latest activity" title="Recent fills">
        {live.recentFills?.length ? <ul className="ad-feed">{live.recentFills.map(f => <li key={f.id}>
          <Tag tone={f.side === 'BUY' ? 'good' : 'bad'}>{f.side}</Tag>
          <div><Link className="ad-sym" href={stockHref(f.symbol)}>{ticker(f.symbol)}</Link><small>{strategyName(f.strategy)} · {f.qty} @ {inr(f.price)}</small></div>
          <time>{istTime(f.filled_at)}</time></li>)}</ul> : <Blank title="No fills yet"/>}
      </Section>
      <Section eyebrow="Engine" title="Recent engine runs">
        {live.lastRuns?.length ? <ul className="ad-feed">{live.lastRuns.map((r, i) => <li key={i}>
          <Tag tone={['ok', 'success'].includes(r.status) ? 'good' : r.status === 'failed' ? 'bad' : 'neutral'}>{r.status}</Tag><div><strong>{r.kind}</strong></div><time>{istTime(r.at)}</time></li>)}</ul> : <Blank title="No engine runs recorded"/>}
      </Section>
    </div>
    <LedgerBrowser refreshKey={refreshKey}/>
    {live.demoTrades?.length > 0 && <Section eyebrow="Isolated test record" title="Manual demo — excluded from every total" className="ad-demo">
      <p className="ad-muted">Stored separately in the {`paper_demo_v1`} namespace. It is not a strategy position and is never counted in P&amp;L or performance.</p>
      <ul className="ad-feed">{live.demoTrades.map(d => <li key={d.id}><Tag tone="warn">DEMO</Tag><div><strong>{ticker(d.symbol)}</strong><small>{d.strategy_label} · {d.side} {d.qty} @ {inr(d.entry_price)} · {d.status}</small></div><time>{istTime(d.entry_at)}</time></li>)}</ul>
    </Section>}
  </div>;
}
