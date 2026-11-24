// Adaptive Engine screen: the frozen seven vs the shadow candidates 101-108 and 109.
// Read-only. Nothing here promotes, demotes, spawns or trades anything.
// Data provenance, stated on the screen itself:
//   - Main 7: public/data/historical.json (saved research export, as of 2026-11-06).
//     Win rate, mean gross return and closed trades come from each strategy's
//     `metrics`. The export has no average holding period, so that cell is
//     "not available" for the main 7, never estimated from the top/bottom lists.
//   - Candidates 101-108 and 109: public/data/adaptive-candidates.json (registry facts
//     generated from automation/paper-trading/adaptive_candidates.py) for names
//     and sources, and public/data/adaptive-compare.json for numbers.
//     There is NO live compare output for any candidate, so every live candidate
//     metric is "not available". The only candidate numbers that exist are the
//     synthetic fixture compare, shown in their own labelled panel below.
import {useState} from 'react';
import {useStatic} from './data';
import {fmt, pct, strategyName, STRATEGY_HUES} from './format';
import {Blank, Callout, KeyValues, Loading, Section, SortTable, Tag, Tile} from './ui';
import {Columns, Lines, MonthGrid} from './charts';
import {adaptiveHealth, candidateRows, fullMonths, mainAnalyticsRows, mainHealth, MAIN_IDS, CANDIDATE_IDS, NOT_AVAILABLE, promotionLabel, PROPOSED_PROMOTION} from './adaptive';

const na = <span className="ad-muted">{NOT_AVAILABLE}</span>;
const SYNTHETIC_LABEL = 'Synthetic fixture only (4 symbols, 130 days) — not real NSE results';

function HealthList({health}) {
  if (!health?.available) return <Blank title="Not available" text="The data file for this engine did not load, so its filters cannot be checked."/>;
  return <ul className="ad-outages">{health.checks.map(c => <li key={c.label} className="ad-outage-row">
    <span>{c.label}</span>
    <span className="ad-muted ad-fine">{c.value}</span>
    <Tag tone={c.ok ? 'good' : 'warn'}>{c.ok ? 'ok' : 'check'}</Tag>
  </li>)}</ul>;
}

export default function AdaptiveEngine() {
  const history = useStatic('/data/historical.json');
  const compare = useStatic('/data/adaptive-compare.json');
  const catalogue = useStatic('/data/adaptive-candidates.json');
  const [trendId, setTrendId] = useState(1);

  if (history.error) return <Callout tone="error">{history.error}</Callout>;
  if (!history.data) return <Loading label="Loading saved runs…"/>;
  const mains = mainAnalyticsRows(history.data);
  const cands = candidateRows(catalogue.data, compare.data);
  const aHealth = adaptiveHealth(compare.data);
  const mHealth = mainHealth(history.data);
  const synth = compare.data?.historical_replay?.strategies ?? {};
  const synthIds = Object.keys(synth).map(Number).sort((a, b) => a - b);

  return <div className="ad-screen">
    <div className="ad-hero"><div><span className="ad-eyebrow">Adaptive engine · shadow only</span><h1>Adaptive Engine</h1>
      <p>The frozen seven run the live paper book. Candidates 101–108 and 109 only watch from the side (shadow mode) and never trade. This screen compares them side by side and says plainly where the numbers come from — and where there are no numbers yet. No promotion or demotion happens here.</p></div>
      <div className="ad-hero-figure"><span>Last compare run</span><strong>{aHealth.sessionDay ?? '—'}</strong>
        <small>{aHealth.available ? 'Synthetic fixture session day' : 'No compare output loaded'}</small></div>
    </div>
    <div className="ad-tiles">
      <Tile label="Adaptive candidates" value={String(cands.length)} hint="101–108 and 109 · shadow only"/>
      <Tile label="Main strategies" value="7" hint="Frozen · run the paper book"/>
      <Tile label="Forward shadow days" value={aHealth.forwardDays === null || aHealth.forwardDays === undefined ? '—' : String(aHealth.forwardDays)} hint={aHealth.available ? 'Observed live shadow, not replay' : NOT_AVAILABLE}/>
      <Tile label="Candidates with a compare row" value={`${cands.filter(c => c.synthetic).length} / ${cands.length}`} hint={cands.filter(c => c.synthetic).length === cands.length ? 'All have a row' : 'Some have no row yet'}/>
      <Tile label="Research export" value={history.data.asOf ?? '—'} hint={`${history.data.coverage?.current ?? '?'} of ${history.data.coverage?.universe ?? '?'} symbols current`}/>
    </div>

    <Section eyebrow="Are the filters working?" title="Filter health">
      <div className="ad-split">
        <div><h3 className="ad-sub">Adaptive engine</h3>
          {compare.error
            ? <Callout tone="warn" title="Compare output unavailable.">{compare.error} Candidate metrics below are therefore all “not available”.</Callout>
            : <HealthList health={aHealth}/>}
          {aHealth.available && <p className="ad-muted ad-fine">Mode: {aHealth.mode} · {aHealth.statement}. Replay: {aHealth.replayDays} days, {aHealth.replayTrades} trades. Forward shadow: {aHealth.forwardDays} days, {aHealth.forwardTrades} trades — nothing has been observed live yet. Minimum trades for a sufficient sample: {aHealth.minTrades}.</p>}
        </div>
        <div><h3 className="ad-sub">Main engine</h3><HealthList health={mHealth}/>
          {mHealth.available && <p className="ad-muted ad-fine">Coverage: {mHealth.coverage.current} current, {mHealth.coverage.missing} missing, {mHealth.coverage.stale} stale of {mHealth.coverage.universe}. {mHealth.verificationCount} of 7 strategies verified; {mHealth.caveatCount} caveats recorded in the export.</p>}
        </div>
      </div>
    </Section>

    <Section eyebrow="Candidates 101–108 and 109 · live metrics" title="Adaptive candidates">
      <p className="ad-muted">There is no live compare output for any candidate, so every live metric below is “{NOT_AVAILABLE}”. The only candidate numbers that exist are the synthetic fixture numbers in the next panel — they are not mixed in here.</p>
      <SortTable caption="adaptive candidates, live metrics" rows={cands} rowKey={r => r.id} columns={[
        {key: 'id', title: 'Candidate', render: (v, r) => <span><strong>C{v}</strong> · {r.name}</span>},
        {key: 'winRate', title: 'Win rate', align: 'right', render: () => na},
        {key: 'meanReturn', title: 'Mean net / trade', align: 'right', render: () => na},
        {key: 'trades', title: 'Trades', align: 'right', render: () => na},
        {key: 'avgHoldDays', title: 'Avg hold', align: 'right', render: () => na},
        {key: 'missingReason', title: 'Why no live number', render: v => <span className="ad-muted ad-fine">{v}</span>},
        {key: 'promotion', title: 'Meets proposed promotion criteria', render: (v, r) => {
          const p = promotionLabel(compare.data, r.id);
          return <span><Tag tone={p.verdict === 'yes' ? 'good' : p.verdict === 'no' ? 'warn' : 'neutral'}>{p.verdict}</Tag>{' '}<span className="ad-muted ad-fine">{p.reasons[0] ?? ''}</span></span>;
        }},
        {key: 'status', title: 'Status', render: () => <Tag tone="neutral">shadow only</Tag>},
      ]}/>
      <KeyValues items={cands.map(c => [`C${c.id} · ${c.name}`, c.source ?? '—', c.tier ? `tier ${c.tier} · warm-up ${c.warmStartDays ?? '?'} days` : undefined])}/>
    </Section>

    <Section eyebrow={SYNTHETIC_LABEL} title="Synthetic fixture compare — not real results">
      <Callout tone="warn" title={SYNTHETIC_LABEL}>These numbers come from the synthetic fixture (4 made-up symbols, 130 days, session 2025-04-04, mode shadow_simulated_observation_only). They show the compare machinery works; they say nothing about real NSE performance. C106 ran and closed 0 trades, so it has no row. C101, C103, C104, C107 and C108 were not run in this fixture.</Callout>
      {compare.error
        ? <Callout tone="warn" title="Synthetic compare unavailable.">{compare.error}</Callout>
        : synthIds.length === 0
          ? <Blank title="No synthetic rows" text="The fixture compare has no closed candidate trades at all."/>
          : <SortTable caption="synthetic fixture candidate rows" rows={synthIds.map(id => ({id, ...synth[String(id)]}))} rowKey={r => r.id} columns={[
            {key: 'id', title: 'Candidate', render: v => <span><strong>C{v}</strong> · {(cands.find(c => c.id === v) ?? {}).name ?? ''}</span>},
            {key: 'trades', title: 'Trades', align: 'right'},
            {key: 'win_rate', title: 'Win rate', align: 'right', render: v => v === null || v === undefined ? na : pct(v * 100)},
            {key: 'avg_net_return_pct', title: 'Mean net / trade', align: 'right', render: v => v === null || v === undefined ? na : pct(v, 2, true)},
            {key: 'avg_hold_days', title: 'Avg hold', align: 'right', render: v => v === null || v === undefined ? na : `${fmt(v, 1)} d`},
            {key: 'max_drawdown_pct', title: 'Max drawdown', align: 'right', render: v => v === null || v === undefined ? na : pct(v, 2)},
            {key: 'sample_label', title: 'Sample', render: v => <Tag tone={v === 'sufficient' ? 'good' : 'warn'}>{v ?? NOT_AVAILABLE}</Tag>},
          ]}/>}
      <p className="ad-muted ad-fine">Source: public/data/adaptive-compare.json, generated by live/candidate_compare.py + live/candidate_shadow.py on the synthetic market fixture (see the file’s _provenance note). Net of the frozen costs; 1R uses the 2× ATR(14) convention, and ATR at entry was not stored, so avg R is not available.</p>
      <Section eyebrow={SYNTHETIC_LABEL} title="Synthetic mean net return per trade">
        <Columns unit="%" rows={synthIds.map(id => ({label: `C${id}`, value: synth[String(id)].avg_net_return_pct}))} describe={r => `C${r.label.slice(1)} · ${synth[r.label.slice(1)]?.trades ?? 0} trades, synthetic only`}
          empty={{title: 'No synthetic rows to draw'}}/>
      </Section>
    </Section>

    <Section eyebrow={`Saved runs · as of ${history.data.asOf}`} title="Main 7 strategies — the comparison">
      <p className="ad-muted">Gross closed-trade statistics from the saved research export. Costs excluded. Average holding period is “{NOT_AVAILABLE}” for all seven: the export does not record it.</p>
      <SortTable caption="main seven strategies" rows={mains} rowKey={r => r.id} columns={[
        {key: 'id', title: 'Strategy', render: (v, r) => <span className="ad-strat"><i style={{background: STRATEGY_HUES[v - 1]}}/>{r.name}</span>},
        {key: 'trades', title: 'Closed trades', align: 'right', render: v => fmt(v, 0)},
        {key: 'winRate', title: 'Win rate', align: 'right', render: v => pct(v)},
        {key: 'meanReturn', title: 'Mean gross / trade', align: 'right', render: v => pct(v, 2, true)},
        {key: 'medianReturn', title: 'Median gross / trade', align: 'right', render: v => pct(v, 2, true)},
        {key: 'avgHoldDays', title: 'Avg hold', align: 'right', render: () => na},
        {key: 'openPositions', title: 'Open at export', align: 'right', render: v => fmt(v, 0)},
        {key: 'trend', title: 'Recent trend', render: (v, r) => r.trend
          ? <span><Tag tone={r.trend.label === 'Improving' ? 'good' : r.trend.label === 'Worsening' ? 'warn' : 'neutral'}>{r.trend.label}</Tag>{' '}<span className="ad-muted ad-fine">{r.trend.period} mean {pct(r.trend.mean, 2, true)} · {r.trend.trades} trades</span></span>
          : na},
        {key: 'trailing12', title: 'Trailing 12m mean', align: 'right', render: (v, r) => r.trailing12 ? pct(r.trailing12.mean, 2, true) : na},
        {key: 'demotion', title: 'Demotion watch (proposed)', render: (v, r) => <Tag tone={r.demotion.watch ? 'warn' : 'good'}>{r.demotion.evaluable ? (r.demotion.watch ? 'watch' : 'no') : 'not evaluable'}</Tag>},
      ]}/>
      <p className="ad-muted ad-fine">Recent trend definition: recent month = last full month with closed trades ({mains[0]?.trend?.period ?? '—'}; the export month {history.data.asOf?.slice(0, 7)} is partial, so it is excluded). Improving = recent-month mean above both the prior month and the long-run mean of monthly means; Worsening = below both; otherwise Mixed. A month with no closed trades is “not available”, never 0. Trailing 12m = trade-weighted mean over the last 12 full months. Main 7 and candidates are never blended or ranked together: the main figures are gross, over 2004–2026 on 692 real symbols; the candidate figures are net, over 130 synthetic days on 4 made-up symbols. Strategy names: {MAIN_IDS.map(id => `S${id} ${strategyName(id)}`).join(' · ')}.</p>
    </Section>

    <Section eyebrow="Charts · saved runs" title="Performance comparison">
      <div className="ad-split">
        <div><h3 className="ad-sub">Mean gross return per trade — main 7</h3>
          <Columns unit="%" rows={mains.map(r => ({label: `S${r.id}`, value: r.meanReturn}))} describe={r => mains.find(m => `S${m.id}` === r.label)?.name ?? ''}/>
        </div>
        <div><h3 className="ad-sub">Win rate — main 7</h3>
          <Columns unit="%" rows={mains.map(r => ({label: `S${r.id}`, value: r.winRate}))} describe={r => mains.find(m => `S${m.id}` === r.label)?.name ?? ''}/>
        </div>
      </div>
      <p className="ad-muted ad-fine">Candidates are not in these charts: there is no real candidate series to draw, and the synthetic fixture numbers (net, 4 symbols) must not share an axis with the main figures (gross, 692 symbols).</p>
    </Section>

    <Section eyebrow="Trend over time · monthly mean gross return" title="Is it getting better or worse?">
      <div className="ad-chooser" role="group" aria-label="Strategy trend to show">{[...MAIN_IDS, ...CANDIDATE_IDS].map(id => {
        const isMain = id <= 7;
        const label = isMain ? `S${id} ${strategyName(id)}` : `C${id}`;
        return <button key={id} aria-pressed={trendId === id} className={trendId === id ? 'on' : ''} style={isMain ? {'--ad-accent': STRATEGY_HUES[id - 1]} : undefined} onClick={() => setTrendId(id)}><i/>{label}</button>;
      })}</div>
      {(() => {
        const row = mains.find(m => m.id === trendId);
        if (!row) {
          const cand = cands.find(c => c.id === trendId);
          return <Blank title="Trend not available" text={`${cand?.name ?? `Candidate ${trendId}`} has no monthly series: no live compare output exists and the synthetic fixture has no monthly breakdown. Nothing is drawn instead of data.`}/>;
        }
        const months = fullMonths(row.monthly, history.data.asOf).slice(-24);
        return <>
          <p className="ad-muted">Mean gross return per closed trade, by exit month, last 24 full months{row.trend ? ` · recent ${row.trend.period}: ${pct(row.trend.mean, 2, true)} (${row.trend.label.toLowerCase()})` : ''}.</p>
          <Lines unit="%" baseline={0} series={[{name: row.name, color: STRATEGY_HUES[row.id - 1], points: months.map(m => ({x: m.period, y: m.mean}))}]}
            empty={{title: 'No monthly series', text: 'This strategy has no months with closed trades.'}}/>
          <h3 className="ad-sub">Month grid · mean gross return per closed trade</h3>
          <MonthGrid rows={fullMonths(row.monthly, history.data.asOf).map(m => ({period: m.period, mean: m.mean, closed: m.closed}))}/>
        </>;
      })()}
    </Section>

    <Section eyebrow="Proposal only — not a decision" title="Proposed promotion / demotion criteria">
      <Callout tone="info" title="Nothing here moves a strategy.">These criteria are a proposal for Grok’s review and VVS’s approval. There is no promote or demote button, and no label on this screen changes any strategy’s status.</Callout>
      <h3 className="ad-sub">Promotion — candidate → main (all must hold, judged on forward shadow, net of costs)</h3>
      <KeyValues items={[
        ['Sample', `≥ ${PROPOSED_PROMOTION.minForwardTrades} closed trades in forward shadow`, '30 is the compare’s floor; ~100 is where a trend starts to show'],
        ['Duration', `≥ ${PROPOSED_PROMOTION.minForwardSessions} forward sessions (~12 months)`, 'covers more than one regime'],
        ['Edge', `Mean net return per trade > 0% and win rate ≥ ${PROPOSED_PROMOTION.minWinRatePct}%`, 'trend systems in the main 7 win 36–41%'],
        ['Drawdown', `Max drawdown ≤ ${PROPOSED_PROMOTION.maxDrawdownPct}% of allocated capital`, 'backtest max drawdown understates the future worst case'],
        ['Overlap', `< ${PROPOSED_PROMOTION.maxOverlapPct}% of its trades share (symbol, entry date) with the frozen seven`, 'it must add something new'],
        ['Manual review', PROPOSED_PROMOTION.manualReview, ''],
        ['Review cycle', PROPOSED_PROMOTION.reviewCycle, 'like the NIFTY 50 / Next 50: fixed dates, full checklist, six-month data window'],
      ]}/>
      <h3 className="ad-sub">Demotion — main → adaptive (watch flag, never automatic)</h3>
      <KeyValues items={[
        ['Watch if', 'Trailing-12-month mean gross return per trade below 0% AND recent trend “Worsening”', 'both must hold at the same review'],
        ['Review', 'Semi-annually, same cycle as promotion', 'a single bad year never demotes on its own'],
        ['Decision', 'Grok review + VVS approval, as always', ''],
      ]}/>
      <p className="ad-muted ad-fine">Today: every candidate is “not evaluable” for promotion (no forward shadow trades yet — the only candidate numbers are the 130-day synthetic fixture). Among the main 7, the demotion watch flags {mains.filter(m => m.demotion.watch).map(m => `S${m.id} ${m.name}`).join(', ') || 'none'}.</p>
    </Section>
  </div>;
}
