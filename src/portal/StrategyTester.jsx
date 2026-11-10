import {useState} from 'react';
import {FlaskConical, Lock} from 'lucide-react';
import {Link, runIdFor, stockHref} from './router';
import {useDashboard, useStatic} from './data';
import {fmt, inr, pct, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, Loading, Section, SortTable, Tabs, Tag, Tile} from './ui';
import {Columns, Funnel, MonthGrid} from './charts';

function readParams() {
  const p = new URLSearchParams(window.location.search);
  const id = Number(p.get('strategy'));
  return {id: Number.isInteger(id) && id >= 1 && id <= 7 ? id : 1, source: p.get('source') === 'scan' ? 'scan' : 'hist'};
}

function writeParams(next) {
  const p = new URLSearchParams(window.location.search);
  p.set('strategy', String(next.id));
  p.set('source', next.source);
  window.history.replaceState(null, '', `${window.location.pathname}?${p}`);
}

const ret = v => <span className={`ad-${tone(v)}`}>{pct(v, 2, true)}</span>;

function SavedRun({strategy, asOf, catalog}) {
  const names = new Map((catalog?.strategies.find(c => Number(c.id) === strategy.id)?.qualifiers ?? []).map(q => [q.symbol, q]));
  const matched = strategy.qualifiers.map(q => ({...q, name: q.name ?? names.get(q.symbol)?.name ?? null, industry: q.industry ?? names.get(q.symbol)?.industry ?? null, held: q.held ?? names.get(q.symbol)?.held, fresh: q.fresh ?? names.get(q.symbol)?.fresh}));
  const m = strategy.metrics, runId = runIdFor(asOf, strategy.id);
  const [tab, setTab] = useState('matched');
  return <>
    <div className="ad-run-head"><div><span className="ad-eyebrow">Run {runId}</span><h2>{strategy.name} · saved backtest</h2>
      <p className="ad-muted">Exported {asOf} from <code className="ad-code">{strategy.source}</code>. Gross of costs, survivorship-biased, closed trades only.</p></div>
      <Tag tone="info">Historical</Tag></div>
    <div className="ad-tiles">
      <Tile label="Closed trades" value={fmt(m.closed_trades, 0)} hint={`${fmt(m.filled_entries, 0)} entries · ${m.open_positions} open`}/>
      <Tile label="Win rate" value={pct(m.win_rate_pct)} hint="Strictly positive closed trades"/>
      <Tile label="Mean / trade" value={pct(m.mean_gross_return_pct, 2, true)} tone={tone(m.mean_gross_return_pct)} hint="Arithmetic, gross"/>
      <Tile label="Median / trade" value={pct(m.median_gross_return_pct, 2, true)} tone={tone(m.median_gross_return_pct)}/>
      <Tile label="Matched today" value={fmt(m.latest_condition_qualifiers, 0)} hint={`${m.fresh_pending_entries} fresh · ${m.pending_exit_orders} pending exit`}/>
    </div>
    <div className="ad-split">
      <Section eyebrow="Sequential" title="Filter funnel"><Funnel stages={strategy.funnel}/></Section>
      <Section eyebrow="By exit year" title="Mean gross return per closed trade">
        <Columns rows={strategy.yearly.map(y => ({label: y.period, value: y.mean, closed: y.closed, win: y.winRate}))} describe={r => `${r.closed} closed · ${pct(r.win)} win`}/>
      </Section>
    </div>
    <Section eyebrow="Seasonality" title="Exit-month heatmap"><MonthGrid rows={strategy.monthly}/></Section>
    <Section title="Run details">
      <Tabs label="Run detail" value={tab} onChange={setTab} items={[['matched', 'Matched stocks', strategy.qualifiers.length], ['ranking', 'Per-stock ranking', strategy.perStock.length], ['extremes', 'Best & worst trades']]}/>
      {tab === 'matched' && <SortTable filter caption="matched stocks" rows={matched} columns={[
        {key: 'symbol', title: 'Stock', render: v => <Link className="ad-sym" href={stockHref(v, runId)}>{ticker(v)}</Link>},
        {key: 'name', title: 'Company'}, {key: 'industry', title: 'Industry'},
        {key: 'close', title: 'Saved close', align: 'right', render: v => inr(v)},
        {key: 'fresh', title: 'Signal', render: (v, r) => <Tag tone={v ? 'good' : 'neutral'}>{v ? 'fresh entry' : r.held ? 'already held' : 'condition true'}</Tag>},
      ]}/>}
      {tab === 'ranking' && <SortTable filter caption="per-stock ranking" rows={strategy.perStock} columns={[
        {key: 'symbol', title: 'Stock', render: v => <Link className="ad-sym" href={stockHref(v, runId)}>{ticker(v)}</Link>},
        {key: 'closed', title: 'Closed', align: 'right'}, {key: 'winRate', title: 'Win', align: 'right', render: v => pct(v)},
        {key: 'mean', title: 'Mean', align: 'right', render: ret}, {key: 'median', title: 'Median', align: 'right', render: ret},
        {key: 'best', title: 'Best', align: 'right', render: ret}, {key: 'worst', title: 'Worst', align: 'right', render: ret},
        {key: 'qualifies', title: 'Matched', render: v => (v ? <Tag tone="good">yes</Tag> : '—')},
      ]}/>}
      {tab === 'extremes' && <div className="ad-split">{[['Best five', strategy.top], ['Worst five', strategy.bottom]].map(([title, rows]) => <div key={title}>
        <h3 className="ad-sub">{title}</h3>
        <SortTable caption={title} rows={rows} columns={[
          {key: 'symbol', title: 'Stock', render: v => <Link className="ad-sym" href={stockHref(v, runId)}>{ticker(v)}</Link>},
          {key: 'entry_date', title: 'Entry'}, {key: 'exit_date', title: 'Exit'},
          {key: 'return_pct', title: 'Return', align: 'right', render: ret},
          {key: 'dataSensitive', title: 'Review', render: v => (v ? <Tag tone="warn">data-sensitive</Tag> : '—')},
        ]}/></div>)}</div>}
    </Section>
  </>;
}

function LiveScan({id, refreshKey}) {
  const scan = useDashboard({view: 'scan'}, refreshKey);
  const [page, setPage] = useState(1);
  const quals = useDashboard({view: 'qualifiers', strategy: String(id), page: String(page), limit: '25'}, refreshKey, Boolean(scan.data));
  if (scan.loading && !scan.data) return <Loading label="Reading the latest completed-bar scan…"/>;
  if (scan.error) return <Callout tone="warn" title="No live scan available.">{scan.error}</Callout>;
  const s = scan.data, count = s.counts?.find(r => Number(r.strategy) === id);
  const runId = runIdFor(s.bar_date, id, 'scan');
  return <>
    <div className="ad-run-head"><div><span className="ad-eyebrow">Run {runId}</span><h2>{strategyName(id)} · latest live scan</h2>
      <p className="ad-muted">Completed bar {s.bar_date}, scanned {s.scanned_at ? new Date(s.scanned_at).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : '—'} · status {s.status}.</p></div>
      <Tag tone={s.prefilter?.ready ? 'good' : 'warn'}>{s.prefilter?.ready ? 'Pre-filter complete' : 'Pre-filter incomplete'}</Tag></div>
    {!s.prefilter?.ready && <Callout tone="warn">The market pre-filter for this bar is not complete, so qualified counts are held at zero rather than guessed.</Callout>}
    <div className="ad-tiles">
      <Tile label="Screened" value={fmt(count?.screened, 0)}/>
      <Tile label="Qualified" value={fmt(count?.qualified, 0)} tone={Number(count?.qualified) > 0 ? 'up' : 'flat'}/>
      <Tile label="Pre-filter pass" value={fmt(s.prefilter?.passed, 0)} hint={`of ${fmt(s.prefilter?.total, 0)} · ${fmt(s.prefilter?.unavailable, 0)} unavailable`}/>
      <Tile label="Multi-strategy names" value={fmt(s.stocks?.filter(x => x.strategies.length > 1).length, 0)} hint="Qualified in 2+ strategies"/>
    </div>
    <Section eyebrow="All strategies" title="Qualified per strategy on this bar">
      <Columns unit="" rows={(s.counts ?? []).map(r => ({label: strategyName(r.strategy), value: Number(r.qualified), screened: r.screened}))} describe={r => `${r.screened} screened`}/>
    </Section>
    <Section eyebrow="Matched" title={`${strategyName(id)} qualifiers`}>
      {quals.error && <Callout tone="error">{quals.error}</Callout>}
      {quals.loading && !quals.data ? <Loading/> : <SortTable caption="live qualifiers" pageSize={25} rows={quals.data?.rows ?? []} columns={[
        {key: 'symbol', title: 'Stock', render: v => <Link className="ad-sym" href={stockHref(v, runId)}>{ticker(v)}</Link>},
        {key: 'name', title: 'Company'}, {key: 'industry', title: 'Industry'}, {key: 'bar_date', title: 'Bar'},
      ]} empty={{title: 'No qualifiers on this bar', text: 'No stock passed every rule and the pre-filter for this strategy.'}}/>}
      {(quals.data?.total ?? 0) > 25 && <div className="ad-pager wide"><span>{quals.data.total} qualifiers · page {page}</span><div>
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><button disabled={page * 25 >= quals.data.total} onClick={() => setPage(page + 1)}>Next</button></div></div>}
    </Section>
  </>;
}

export default function StrategyTester({refreshKey}) {
  const [cfg, setCfg] = useState(readParams);
  const history = useStatic('/data/historical.json');
  const catalog = useStatic('/data/strategy-catalog.json');
  const update = patch => setCfg(c => { const next = {...c, ...patch}; writeParams(next); return next; });
  const strategy = history.data?.strategies.find(s => s.id === cfg.id);
  return <div className="ad-screen">
    <div className="ad-hero compact"><div><span className="ad-eyebrow">Strategy tester</span><h1>Test bench</h1>
      <p>Pick a strategy and a run source. Saved runs come from the verified backtest export; live runs come from the engine’s latest completed-bar scan.</p></div></div>
    <div className="ad-bench">
      <aside className="ad-bench-config" aria-label="Test configuration">
        <h3 className="ad-sub">Strategy</h3>
        <div className="ad-pick">{[1, 2, 3, 4, 5, 6, 7].map(id => <button key={id} className={cfg.id === id ? 'on' : ''} aria-pressed={cfg.id === id} style={{'--ad-accent': STRATEGY_HUES[id - 1]}} onClick={() => update({id})}>
          <span>{String(id).padStart(2, '0')}</span>{strategyName(id)}</button>)}</div>
        <h3 className="ad-sub">Run source</h3>
        <Tabs label="Run source" value={cfg.source} onChange={source => update({source})} items={[['hist', 'Saved backtest'], ['scan', 'Live scan']]}/>
        {history.data && <dl className="ad-facts">
          <div><dt>Universe</dt><dd>{history.data.coverage.universe} symbols</dd></div>
          <div><dt>Current data</dt><dd>{history.data.coverage.current}</dd></div>
          <div><dt>Missing / stale</dt><dd>{history.data.coverage.missing} / {history.data.coverage.stale}</dd></div>
          <div><dt>Execution</dt><dd>Next-session open (backtest)</dd></div>
        </dl>}
        {strategy && <details className="ad-rules"><summary>Exact rules</summary>{Object.entries(strategy.rules).map(([k, v]) => <div key={k}><strong>{k.replaceAll('_', ' ')}</strong><p>{Array.isArray(v) ? v.join(' · ') : String(v)}</p></div>)}</details>}
        <div className="ad-stub" aria-disabled="true"><Lock size={14}/><div><strong>Custom rule builder</strong><p>Not available yet: the dashboard has no backtest compute endpoint, so it can only show saved runs and live scans. Rules stay locked to the frozen seven strategies.</p></div></div>
      </aside>
      <div className="ad-bench-results">
        {cfg.source === 'scan' ? <LiveScan id={cfg.id} refreshKey={refreshKey}/> : history.error ? <Callout tone="error">{history.error}</Callout> : !strategy ? <Loading label="Loading saved backtest…"/> : <SavedRun strategy={strategy} asOf={history.data.asOf} catalog={catalog.data}/>}
        {cfg.source === 'hist' && history.data && <p className="ad-muted ad-fine"><FlaskConical size={12}/> {history.data.caveats[0]}</p>}
      </div>
    </div>
  </div>;
}

