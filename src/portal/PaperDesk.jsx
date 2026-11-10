import {useState} from 'react';
import LedgerBrowser from './LedgerBrowser';
import {Link, stockHref} from './router';
import {paiseToInr} from './data';
import {fmt, inr, istTime, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Loading, Meter, Section, Tag, Tile} from './ui';

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
  const [ledgerView, setLedgerView] = useState('positions');
  const openLedger = v => { setLedgerView(v); document.getElementById('ad-ledger')?.scrollIntoView?.({behavior: 'smooth', block: 'start'}); };
  if (!live) return overview.loading ? <Loading label="Reading the paper ledger…"/> : <Blank title="Live ledger unavailable" text="The paper ledger could not be read. Nothing is shown rather than placeholder numbers."/>;
  const t = live.totals, c = live.cappedLedger;
  const net = paiseToInr(c?.headline_net_pnl_paise);
  return <div className="ad-screen">
    <div className="ad-hero">
      <div><span className="ad-eyebrow">Paper desk · {live.day} IST</span><h1>What the engine is holding right now</h1>
        <p>Orders are placed only by the scheduled paper engine. This desk is read-only — there is no order ticket and no broker connection.</p></div>
      <div className="ad-hero-figure"><span>Capped-ledger net P&amp;L</span><strong className={`ad-${tone(net)}`}>{signedInr(net)}</strong>
        <small>{c?.capital_return_pct !== null && c?.capital_return_pct !== undefined ? `${pct(c.capital_return_pct, Math.abs(c.capital_return_pct) < 0.01 ? 4 : 2, true)} of ${inr(paiseToInr(c.master_capital_paise), 0)} paper capital` : c?.marks_missing ? 'Unavailable: some marks or fee rows are missing' : 'Unavailable'}</small></div>
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
        {live.recentFills?.length > 0 && <div className="ad-pager wide"><span>Latest {live.recentFills.length} of {t.filledOrders} fills</span><div><button onClick={() => openLedger('fills')}>Open all {t.filledOrders} fills</button></div></div>}
      </Section>
      <Section eyebrow="Engine" title="Recent engine runs">
        {live.lastRuns?.length ? <ul className="ad-feed">{live.lastRuns.map((r, i) => <li key={i}>
          <Tag tone={['ok', 'success'].includes(r.status) ? 'good' : r.status === 'failed' ? 'bad' : 'neutral'}>{r.status}</Tag><div><strong>{r.kind}</strong></div><time>{istTime(r.at)}</time></li>)}</ul> : <Blank title="No engine runs recorded"/>}
        <div className="ad-pager wide"><span>Latest {live.lastRuns?.length ?? 0} runs (mostly 5-minute monitor checks)</span><div><button onClick={() => openLedger('runs')}>Browse all runs</button></div></div>
      </Section>
    </div>
    <LedgerBrowser overview={live} refreshKey={refreshKey} view={ledgerView} onView={setLedgerView}/>
    {live.demoTrades?.length > 0 && <Section eyebrow="Isolated test record" title="Manual demo — excluded from every total" className="ad-demo">
      <p className="ad-muted">Stored separately in the {`paper_demo_v1`} namespace. It is not a strategy position and is never counted in P&amp;L or performance.</p>
      <ul className="ad-feed">{live.demoTrades.map(d => <li key={d.id}><Tag tone="warn">DEMO</Tag><div><strong>{ticker(d.symbol)}</strong><small>{d.strategy_label} · {d.side} {d.qty} @ {inr(d.entry_price)} · {d.status}</small></div><time>{istTime(d.entry_at)}</time></li>)}</ul>
    </Section>}
  </div>;
}
