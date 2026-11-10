import {useMemo, useState} from 'react';
import {Link, runIdFor, stockHref} from './router';
import {useAllPages, useStatic} from './data';
import {fmt, pct, strategyName, STRATEGY_HUES, ticker} from './format';
import {Blank, Callout, Loading, Section} from './ui';
import {Lines} from './charts';
import {PaperAgeNote, PaperOverlap, ResearchVsPaper} from './PaperVsResearch';

const ALL = [1, 2, 3, 4, 5, 6, 7];
function readScope() { return new URLSearchParams(window.location.search).get('paper') === 'all' ? 'all' : 'selected'; }

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

export default function StrategyComparison({overview, refreshKey = 0}) {
  const [ids, setIds] = useState(readIds);
  const [scope, setScopeState] = useState(readScope);
  const history = useStatic('/data/historical.json');
  // Complete paper record (paged, 5,000-row cap): never only the latest 100 fills.
  const ledger = {trades: useAllPages({view: 'trades'}, refreshKey), positions: useAllPages({view: 'positions'}, refreshKey), fills: useAllPages({view: 'fills'}, refreshKey)};
  const setScope = v => { setScopeState(v); const p = new URLSearchParams(window.location.search); if (v === 'all') p.set('paper', 'all'); else p.delete('paper'); window.history.replaceState(null, '', `${window.location.pathname}?${p}`); };
  const toggle = id => setIds(cur => {
    const next = cur.includes(id) ? (cur.length > 2 ? cur.filter(x => x !== id) : cur) : cur.length < 4 ? [...cur, id].sort() : cur;
    const p = new URLSearchParams(window.location.search); p.set('ids', next.join(','));
    window.history.replaceState(null, '', `${window.location.pathname}?${p}`);
    return next;
  });
  const chosen = useMemo(() => (history.data ? ids.map(id => history.data.strategies.find(s => s.id === id)).filter(Boolean) : []), [history.data, ids]);
  const sets = chosen.map(s => s.qualifiers.map(q => q.symbol));
  const all = overlapStats(sets);

  if (history.error) return <Callout tone="error">{history.error}</Callout>;
  if (!history.data) return <Loading label="Loading saved runs…"/>;
  const asOf = history.data.asOf;
  const paperIds = scope === 'all' ? ALL : ids;
  const years = [...new Set(chosen.flatMap(s => s.yearly.map(y => y.period)))].sort().slice(-12);
  return <div className="ad-screen">
    <div className="ad-hero compact"><div><span className="ad-eyebrow">Strategy comparison</span><h1>Side by side</h1>
      <p>Choose two to four strategies. Saved-run figures are gross closed-trade statistics from the {asOf} export; live paper figures come from the paper_v1 ledger. They sit side by side and are never blended or ranked together.</p></div></div>
    <div className="ad-chooser" role="group" aria-label="Strategies to compare">{[1, 2, 3, 4, 5, 6, 7].map(id => <button key={id} aria-pressed={ids.includes(id)} className={ids.includes(id) ? 'on' : ''} style={{'--ad-accent': STRATEGY_HUES[id - 1]}} onClick={() => toggle(id)}>
      <i/>{strategyName(id)}</button>)}</div>
    <div className="ad-ranges" role="group" aria-label="Strategies in the paper comparison">
      <button className={`ad-chip ${scope === 'selected' ? 'on' : ''}`} aria-pressed={scope === 'selected'} onClick={() => setScope('selected')}>Paper views: selected ({ids.length})</button>
      <button className={`ad-chip ${scope === 'all' ? 'on' : ''}`} aria-pressed={scope === 'all'} onClick={() => setScope('all')}>Paper views: all seven</button>
    </div>
    <PaperAgeNote fills={ledger.fills.data?.rows ?? []}/>
    <ResearchVsPaper ids={paperIds} history={history.data} overview={overview.data} ledger={ledger}/>
    <PaperOverlap ids={paperIds} ledger={ledger} researchShared={scope === 'all' ? [] : all.intersection} asOf={asOf}/>
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
      <Section eyebrow={`Research scan · matched on ${asOf}`} title="Signal agreement (research scan, not paper performance)">
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
      <Section eyebrow={`Research scan · matched on ${asOf}`} title="Shared names (research scan)">
        {all.intersection.length ? <ul className="ad-chips">{all.intersection.map(s => <li key={s}><Link className="ad-sym" href={stockHref(s, runIdFor(asOf, chosen[0].id))}>{ticker(s)}</Link></li>)}</ul>
          : <Blank title="No name matched every selected strategy" text="Exclusive names per strategy are listed below."/>}
        <h3 className="ad-sub">Exclusive</h3>
        <ul className="ad-excl">{chosen.map((s, i) => { const others = new Set(sets.filter((_, j) => j !== i).flat()); const own = sets[i].filter(x => !others.has(x));
          return <li key={s.id}><span className="ad-strat"><i style={{background: STRATEGY_HUES[s.id - 1]}}/>{s.name}</span><strong>{own.length}</strong></li>; })}</ul>
      </Section>
    </div>
  </div>;
}
