import {useMemo, useState} from 'react';
import {Link, runIdFor, stockHref} from './router';
import {paiseToInr, useStatic} from './data';
import {fmt, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, Loading, Section, SortTable, Tag} from './ui';
import {Lines} from './charts';

function readIds() {
  const raw = new URLSearchParams(window.location.search).get('ids') ?? '1,4,7';
  const ids = [...new Set(raw.split(',').map(Number).filter(n => Number.isInteger(n) && n >= 1 && n <= 7))];
  return ids.length >= 2 ? ids.slice(0, 4) : [1, 4, 7];
}

// Rows: [label, accessor, format, higherIsBetter]
const METRICS = [
  ['Closed trades', m => m.closed_trades, v => fmt(v, 0), null],
  ['Win rate', m => m.win_rate_pct, v => pct(v), true],
  ['Mean gross / trade', m => m.mean_gross_return_pct, v => pct(v, 2, true), true],
  ['Median gross / trade', m => m.median_gross_return_pct, v => pct(v, 2, true), true],
  ['Open at export', m => m.open_positions, v => fmt(v, 0), null],
  ['Matched on export day', m => m.latest_condition_qualifiers, v => fmt(v, 0), null],
];

export function overlapStats(sets) {
  if (!sets.length) return {union: [], intersection: [], jaccard: null};
  const union = [...new Set(sets.flat())].sort();
  const intersection = union.filter(s => sets.every(set => set.includes(s)));
  return {union, intersection, jaccard: union.length ? intersection.length / union.length : null};
}

export default function StrategyComparison({overview}) {
  const [ids, setIds] = useState(readIds);
  const history = useStatic('/data/historical.json');
  const toggle = id => setIds(cur => {
    const next = cur.includes(id) ? (cur.length > 2 ? cur.filter(x => x !== id) : cur) : cur.length < 4 ? [...cur, id].sort() : cur;
    const p = new URLSearchParams(window.location.search); p.set('ids', next.join(','));
    window.history.replaceState(null, '', `${window.location.pathname}?${p}`);
    return next;
  });
  const chosen = useMemo(() => (history.data ? ids.map(id => history.data.strategies.find(s => s.id === id)).filter(Boolean) : []), [history.data, ids]);
  const sets = chosen.map(s => s.qualifiers.map(q => q.symbol));
  const all = overlapStats(sets);
  const live = overview.data;

  if (history.error) return <Callout tone="error">{history.error}</Callout>;
  if (!history.data) return <Loading label="Loading saved runs…"/>;
  const asOf = history.data.asOf;
  const years = [...new Set(chosen.flatMap(s => s.yearly.map(y => y.period)))].sort().slice(-12);
  return <div className="ad-screen">
    <div className="ad-hero compact"><div><span className="ad-eyebrow">Strategy comparison</span><h1>Side by side</h1>
      <p>Choose two to four strategies. Saved-run figures are gross closed-trade statistics from the {asOf} export; live paper figures come from the paper_v1 ledger. They are never blended.</p></div></div>
    <div className="ad-chooser" role="group" aria-label="Strategies to compare">{[1, 2, 3, 4, 5, 6, 7].map(id => <button key={id} aria-pressed={ids.includes(id)} className={ids.includes(id) ? 'on' : ''} style={{'--ad-accent': STRATEGY_HUES[id - 1]}} onClick={() => toggle(id)}>
      <i/>{strategyName(id)}</button>)}</div>
    <Section eyebrow="Saved runs" title="Metric matrix">
      <div className="ad-table-scroll"><table className="ad-table ad-matrix">
        <thead><tr><th scope="col">Metric</th>{chosen.map(s => <th scope="col" key={s.id} className="r"><span className="ad-strat"><i style={{background: STRATEGY_HUES[s.id - 1]}}/>{s.name}</span></th>)}</tr></thead>
        <tbody>{METRICS.map(([label, get, format, better]) => {
          const vals = chosen.map(s => get(s.metrics));
          const best = better === null ? null : Math.max(...vals.filter(Number.isFinite));
          return <tr key={label}><th scope="row">{label}</th>{vals.map((v, i) => <td key={chosen[i].id} className={`r num ${best !== null && v === best ? 'ad-best' : ''}`}>{format(v)}</td>)}</tr>;
        })}
          <tr><th scope="row">Run</th>{chosen.map(s => <td key={s.id} className="r"><Link className="ad-link" href={`/strategy-tester?strategy=${s.id}&source=hist`}>{runIdFor(asOf, s.id)}</Link></td>)}</tr>
        </tbody>
      </table></div>
      <p className="ad-muted ad-fine">Highlighted cells are the highest value in that row. No ranking is implied: these runs were not jointly tested and costs are excluded.</p>
    </Section>
    <Section eyebrow="Saved runs" title="Mean gross return per closed trade, by exit year">
      <Lines unit="%" baseline={0} series={chosen.map(s => ({name: s.name, color: STRATEGY_HUES[s.id - 1], points: s.yearly.filter(y => years.includes(y.period)).map(y => ({x: y.period, y: y.mean}))}))}/>
    </Section>
    <div className="ad-split">
      <Section eyebrow={`Matched on ${asOf}`} title="Signal agreement">
        <div className="ad-venn">
          <div><span>Union</span><strong>{all.union.length}</strong></div>
          <div><span>All agree</span><strong>{all.intersection.length}</strong></div>
          <div><span>Jaccard</span><strong>{all.jaccard === null ? '—' : fmt(all.jaccard, 3)}</strong></div>
        </div>
        <table className="ad-table ad-matrix"><thead><tr><th/>{chosen.map(s => <th key={s.id} scope="col" className="r">{String(s.id).padStart(2, '0')}</th>)}</tr></thead>
          <tbody>{chosen.map((a, i) => <tr key={a.id}><th scope="row"><span className="ad-strat"><i style={{background: STRATEGY_HUES[a.id - 1]}}/>{a.name}</span></th>
            {chosen.map((b, j) => { const o = overlapStats([sets[i], sets[j]]); return <td key={b.id} className="r num" title={`${o.intersection.length} shared of ${o.union.length}`}>{i === j ? sets[i].length : o.intersection.length}</td>; })}</tr>)}</tbody></table>
        <p className="ad-muted ad-fine">Diagonal: names matched by that strategy. Off-diagonal: names matched by both.</p>
      </Section>
      <Section eyebrow={`Matched on ${asOf}`} title="Shared names">
        {all.intersection.length ? <ul className="ad-chips">{all.intersection.map(s => <li key={s}><Link className="ad-sym" href={stockHref(s, runIdFor(asOf, chosen[0].id))}>{ticker(s)}</Link></li>)}</ul>
          : <Blank title="No name matched every selected strategy" text="Exclusive names per strategy are listed below."/>}
        <h3 className="ad-sub">Exclusive</h3>
        <ul className="ad-excl">{chosen.map((s, i) => { const others = new Set(sets.filter((_, j) => j !== i).flat()); const own = sets[i].filter(x => !others.has(x));
          return <li key={s.id}><span className="ad-strat"><i style={{background: STRATEGY_HUES[s.id - 1]}}/>{s.name}</span><strong>{own.length}</strong></li>; })}</ul>
      </Section>
    </div>
    <Section eyebrow="Live paper ledger" title="How these strategies are doing on paper">
      {!live ? <Blank title="Live ledger unavailable"/> : <SortTable caption="live paper by strategy" rows={chosen.map(s => {
        const p = live.performance?.find(r => r.strategy === s.id), sl = live.cappedLedger?.sleeves?.[s.id];
        return {strategy: s.id, closed: p?.closedTrades ?? 0, win: p?.winRate ?? null, realized: p?.realized ?? null, open: p?.openPositions ?? 0, slots: sl?.slots_max ?? null, net: paiseToInr(sl?.net_pnl_paise)};
      })} columns={[
        {key: 'strategy', title: 'Strategy', render: v => <span className="ad-strat"><i style={{background: STRATEGY_HUES[v - 1]}}/>{strategyName(v)}</span>},
        {key: 'open', title: 'Open / slots', align: 'right', render: (v, r) => <span className={r.slots && v > r.slots ? 'ad-down' : ''}>{v} / {r.slots ?? '—'}</span>},
        {key: 'closed', title: 'Closed', align: 'right'}, {key: 'win', title: 'Win', align: 'right', render: v => pct(v)},
        {key: 'realized', title: 'Realized (gross)', align: 'right', render: v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>},
        {key: 'net', title: 'Sleeve net P&L', align: 'right', render: v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>},
      ]}/>}
      <p className="ad-muted ad-fine">Live paper is a few sessions old; a handful of trades says little about a strategy. <Tag tone="neutral">demo excluded</Tag></p>
    </Section>
  </div>;
}
