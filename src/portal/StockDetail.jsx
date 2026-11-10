import {useMemo, useState} from 'react';
import {ArrowLeft, Check, X} from 'lucide-react';
import {Link, runIdFor, stockHref} from './router';
import {symbolCandidates, useDashboard, useStatic} from './data';
import {fmt, inr, istTime, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, Loading, Section, SortTable, Tabs, Tag, Tile} from './ui';
import {Columns, Spark} from './charts';
import {analyzeTrades, descriptions, tradePlan} from '../backtestMath';

const ret = v => <span className={`ad-${tone(v)}`}>{pct(v, 2, true)}</span>;

function KeyValues({value}) {
  if (value === null || value === undefined) return <span className="ad-muted">—</span>;
  if (typeof value !== 'object') return <span>{typeof value === 'boolean' ? (value ? 'yes' : 'no') : typeof value === 'number' ? fmt(value, 4) : String(value)}</span>;
  return <dl className="ad-kv">{Object.entries(value).map(([k, v]) => <div key={k}><dt>{k.replaceAll('_', ' ')}</dt><dd>{typeof v === 'object' && v !== null ? <KeyValues value={v}/> : <KeyValues value={v}/>}</dd></div>)}</dl>;
}

function Checks({checks, date}) {
  const entries = Object.entries(checks ?? {});
  if (!entries.length) return <Blank title="No filter checklist recorded for this run"/>;
  return <ul className="ad-checks">{entries.map(([k, ok]) => <li key={k} className={ok ? 'ok' : 'no'}>{ok ? <Check size={14}/> : <X size={14}/>}<span>{k}</span><small>{ok ? `passed${date ? ' · ' + date : ''}` : 'not satisfied'}</small></li>)}</ul>;
}

function SavedBacktest({file, strategyId, sym, runId}) {
  const [period, setPeriod] = useState('1y');
  const a = useMemo(() => (file ? analyzeTrades(file.closedTrades, file.asOf, period) : null), [file, period]);
  const allYears = useMemo(() => {
    if (!file) return [];
    const m = new Map();
    for (const t of file.closedTrades) if (Number.isFinite(t.return_pct)) { const y = t.exit_date.slice(0, 4); const g = m.get(y) ?? []; g.push(t.return_pct); m.set(y, g); }
    return [...m].sort().map(([label, rs]) => ({label, value: rs.reduce((x, y) => x + y, 0) / rs.length, n: rs.length}));
  }, [file]);
  if (!file) return null;
  const q = file.stock;
  return <>
    {file.issues?.length > 0 && <Callout tone="warn" title="Data-quality flags.">{file.issues.length} extreme or invalid historical trade(s) for this stock are kept as recorded, not corrected.</Callout>}
    <div className="ad-window"><Tabs label="Backtest window" value={period} onChange={setPeriod} items={[['1w', '1 week'], ['1m', '1 month'], ['1y', '1 year']]}/><span className="ad-muted">Closed trades exiting after {a.start} through {a.asOf}</span></div>
    <div className="ad-tiles">
      <Tile label="Closed in window" value={fmt(a.count, 0)} hint={`${file.closedTrades.length} all-time`}/>
      <Tile label="Win rate" value={pct(a.winRate)}/>
      <Tile label="Mean / trade" value={pct(a.mean, 2, true)} tone={tone(a.mean)}/>
      <Tile label="Closed-index drawdown" value={a.maxDrawdown === null ? '—' : pct(a.maxDrawdown)} hint="Of the baseline-100 closed-trade index"/>
    </div>
    <Section eyebrow="All years" title="Mean gross return per closed trade, by exit year"><Columns rows={allYears} describe={r => `${r.n} closed`}/></Section>
    <Section eyebrow={`${period} window`} title="Trades behind the numbers">
      <SortTable caption="saved trades" rows={a.trades} rowKey={(r, i) => r.entry_date + i} columns={[
        {key: 'entry_date', title: 'Entry'}, {key: 'entry_price', title: 'Entry ₹', align: 'right', render: v => inr(v)},
        {key: 'exit_date', title: 'Exit'}, {key: 'exit_price', title: 'Exit ₹', align: 'right', render: v => inr(v)},
        {key: 'return_pct', title: 'Return', align: 'right', render: ret}, {key: 'calendar_days', title: 'Days', align: 'right'},
        {key: 'dataSensitive', title: 'Review', render: v => (v ? <Tag tone="warn">flagged</Tag> : '—')},
      ]} empty={{title: 'No closed trades in this window', text: 'Try the 1-year window. A window without trades is not a zero return.'}}/>
    </Section>
    <div className="ad-split">
      <Section eyebrow={`Saved bar ${q.date}`} title="Entry filters"><Checks checks={q.checks} date={q.date}/></Section>
      <Section eyebrow="Rule-based" title="Trade plan">{(() => { const p = tradePlan(strategyId, q); return <dl className="ad-plan">
        <div><dt>Entry</dt><dd>{p.entry}</dd></div><div><dt>Exit / target</dt><dd>{p.target}</dd></div><div><dt>Stop</dt><dd>{p.stop}</dd></div>
        <div><dt>Execution</dt><dd>{p.execution}</dd></div></dl>; })()}
        <p className="ad-muted ad-fine">{descriptions[strategyId - 1]} Run {runId} · {sym}.</p></Section>
    </div>
  </>;
}

export default function StockDetail({route, refreshKey}) {
  const {symbol, runId} = route;
  const history = useStatic('/data/historical.json');
  const catalog = useStatic('/data/strategy-catalog.json');
  const candidates = symbolCandidates(symbol);
  const resolved = useMemo(() => {
    const universe = history.data?.strategies[0]?.screens?.map(s => s.symbol) ?? [];
    return candidates.find(c => universe.includes(c)) ?? candidates[0] ?? null;
  }, [history.data, candidates]);
  const strategyId = runId?.strategyId ?? null;
  const backtest = useStatic(runId?.kind === 'hist' && resolved && history.data?.strategies.find(s => s.id === strategyId)?.qualifiers.some(q => q.symbol === resolved) ? `/data/stock-backtests/${strategyId}/${encodeURIComponent(resolved)}.json` : null);
  const quotes = useDashboard({view: 'history', symbol: resolved ?? 'X'}, refreshKey, Boolean(resolved));
  const positions = useDashboard({view: 'positions', scope: 'all', limit: '100'}, refreshKey, Boolean(resolved));
  const fills = useDashboard({view: 'fills', scope: 'all', limit: '100'}, refreshKey, Boolean(resolved));
  const scanParams = runId?.kind === 'scan' && resolved ? {view: 'qualifiers', strategy: String(strategyId), symbol: resolved} : null;
  const scan = useDashboard(scanParams ?? {view: 'qualifiers', strategy: '1'}, refreshKey, Boolean(scanParams));
  const [tab, setTab] = useState(runId ? 'run' : 'live');

  if (!symbol) return <Callout tone="error">That stock symbol is not valid.</Callout>;
  const footprint = history.data ? history.data.strategies.map(s => {
    const ps = s.perStock.find(p => p.symbol === resolved), qual = s.qualifiers.find(x => x.symbol === resolved);
    return {strategy: s.id, closed: ps?.closed ?? null, winRate: ps?.winRate ?? null, mean: ps?.mean ?? null, best: ps?.best ?? null, worst: ps?.worst ?? null, matched: Boolean(qual), held: qual?.held ?? false};
  }) : [];
  const meta = backtest.data?.stock ?? catalog.data?.strategies.flatMap(s => s.qualifiers).find(q => q.symbol === resolved);
  const myPositions = (positions.data?.rows ?? []).filter(p => p.symbol === resolved);
  const myFills = (fills.data?.rows ?? []).filter(f => f.symbol === resolved);
  const q = quotes.data?.rows ?? [];
  const last = q.at(-1);
  const scanRow = scan.data?.rows?.[0];

  return <div className="ad-screen">
    <Link href={runId?.kind === 'hist' ? `/strategy-tester?strategy=${strategyId}&source=hist` : runId?.kind === 'scan' ? `/strategy-tester?strategy=${strategyId}&source=scan` : '/paper'} className="ad-back"><ArrowLeft size={14}/>{runId ? 'Back to the test run' : 'Back to the paper desk'}</Link>
    <div className="ad-hero">
      <div><span className="ad-eyebrow">{resolved ?? symbol}{meta?.industry ? ` · ${meta.industry}` : ''}</span><h1>{ticker(resolved ?? symbol)}</h1>
        <p>{meta?.name ?? scanRow?.name ?? 'Company name not in the saved research set'}{runId ? ` · run ${runId.raw} (${strategyName(strategyId)})` : ''}</p></div>
      <div className="ad-hero-figure"><span>Last accepted Fyers quote</span><strong>{last ? inr(last.price) : '—'}</strong><small>{last ? istTime(last.quote_at) : quotes.loading ? 'Reading…' : 'No accepted quote recorded'}</small>
        {q.length > 1 && <Spark points={q.map(r => ({y: Number(r.price)}))}/>}</div>
    </div>
    {route.params.get('runId') && !runId && <Callout tone="warn">The runId in the link is not recognised. Showing live data and the saved footprint only.</Callout>}
    <Tabs label="Stock sections" value={tab} onChange={setTab} items={[...(runId ? [['run', runId.kind === 'hist' ? 'Backtest run' : 'Live scan run']] : []), ['live', 'Paper ledger', myPositions.length + myFills.length], ['footprint', 'Across strategies']]}/>
    {tab === 'run' && runId?.kind === 'hist' && (backtest.loading || history.loading ? <Loading label="Loading saved backtest…"/> : backtest.data ? <SavedBacktest file={backtest.data} strategyId={strategyId} sym={resolved} runId={runId.raw}/>
      : (() => { const f = footprint.find(r => r.strategy === strategyId); return <>
        <Callout tone="info">{ticker(resolved)} was not matched by {strategyName(strategyId)} on {runId.asOf}, so no per-trade file was exported. Summary statistics from the same run are shown instead.</Callout>
        {f?.closed ? <div className="ad-tiles"><Tile label="Closed trades" value={fmt(f.closed, 0)}/><Tile label="Win rate" value={pct(f.winRate)}/><Tile label="Mean / trade" value={pct(f.mean, 2, true)} tone={tone(f.mean)}/><Tile label="Best / worst" value={`${pct(f.best, 1, true)} / ${pct(f.worst, 1, true)}`}/></div> : <Blank title="No closed trades for this stock in this run"/>}</>; })())}
    {tab === 'run' && runId?.kind === 'scan' && (scan.loading && !scan.data ? <Loading/> : scan.error ? <Callout tone="warn">{scan.error}</Callout> : !scanRow ? <Blank title={`Not a ${strategyName(strategyId)} qualifier on the latest bar`} text="The live scan has moved on or this stock did not pass every rule and the pre-filter."/> : <div className="ad-split">
      <Section eyebrow={`Bar ${scanRow.bar_date}`} title="Live checklist"><KeyValues value={scanRow.checklist}/></Section>
      <Section eyebrow="Engine plan" title="Trade plan"><KeyValues value={scanRow.plan}/></Section></div>)}
    {tab === 'live' && <>
      <Section eyebrow="paper_v1" title="Open positions in this stock">
        {positions.error ? <Callout tone="error">{positions.error}</Callout> : <SortTable caption="positions" rows={myPositions} columns={[
          {key: 'strategy', title: 'Strategy', render: v => <span className="ad-strat"><i style={{background: STRATEGY_HUES[v - 1]}}/>{strategyName(v)}</span>},
          {key: 'qty', title: 'Qty', align: 'right'}, {key: 'entry_price', title: 'Entry', align: 'right', render: v => inr(v)},
          {key: 'mark_price', title: 'Mark', align: 'right', render: v => inr(v)}, {key: 'unrealized_pnl', title: 'Unrealized', align: 'right', render: v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>},
          {key: 'entry_at', title: 'Opened', render: v => istTime(v)},
        ]} empty={{title: 'Not held on paper', text: 'No strategy currently holds this stock.'}}/>}
      </Section>
      <Section eyebrow="paper_v1" title="Fills">
        {fills.error ? <Callout tone="error">{fills.error}</Callout> : <SortTable caption="fills" rows={myFills} columns={[
          {key: 'filled_at', title: 'Filled', render: v => istTime(v)}, {key: 'strategy', title: 'Strategy', render: v => strategyName(v)},
          {key: 'side', title: 'Side', render: v => <Tag tone={v === 'BUY' ? 'good' : 'bad'}>{v}</Tag>}, {key: 'qty', title: 'Qty', align: 'right'},
          {key: 'price', title: 'Price', align: 'right', render: v => inr(v)}, {key: 'realized_pnl', title: 'Realized', align: 'right', render: v => (v === null || v === undefined ? '—' : signedInr(v))},
        ]} empty={{title: 'No fills in the latest 100', text: 'Searched the 100 most recent paper fills.'}}/>}
        <p className="ad-muted ad-fine">Searched the {fills.data?.rows?.length ?? 0} most recent fills of {fills.data?.total ?? 0}.</p>
      </Section>
    </>}
    {tab === 'footprint' && <Section eyebrow={history.data ? `Saved runs · ${history.data.asOf}` : 'Saved runs'} title="How each strategy traded this stock">
      {history.error ? <Callout tone="error">{history.error}</Callout> : !history.data ? <Loading/> : <SortTable caption="footprint" rows={footprint} rowKey={r => r.strategy} columns={[
        {key: 'strategy', title: 'Strategy', render: v => <Link className="ad-link" href={stockHref(resolved, runIdFor(history.data.asOf, v))}><span className="ad-strat"><i style={{background: STRATEGY_HUES[v - 1]}}/>{strategyName(v)}</span></Link>},
        {key: 'closed', title: 'Closed', align: 'right', render: v => fmt(v, 0)}, {key: 'winRate', title: 'Win', align: 'right', render: v => pct(v)},
        {key: 'mean', title: 'Mean', align: 'right', render: ret}, {key: 'best', title: 'Best', align: 'right', render: ret}, {key: 'worst', title: 'Worst', align: 'right', render: ret},
        {key: 'matched', title: 'Matched', render: (v, r) => (v ? <Tag tone="good">{r.held ? 'held' : 'fresh'}</Tag> : '—')},
      ]}/>}
    </Section>}
    <Section className="ad-stubbed" eyebrow="Not connected" title="Fundamentals, corporate events and news">
      <p className="ad-muted">Market cap, earnings and ex-dividend dates, and news are not available: the dashboard has no data source for them, so nothing is shown rather than made up.</p>
    </Section>
  </div>;
}
