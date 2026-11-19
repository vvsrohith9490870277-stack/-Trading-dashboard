import {useState} from 'react';
import {paiseToInr, useDashboard} from './data';
import {fmt, inr, istTime, pct, signedInr, strategyName, STRATEGY_HUES, tone} from './format';
import {Blank, Callout, Loading, Section, SortTable, Tabs, Tag, Tile} from './ui';
import {Columns, Lines} from './charts';

const money = v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>;
const strat = v => <span className="ad-strat"><i style={{background: STRATEGY_HUES[Number(v) - 1]}}/>{strategyName(v)}</span>;

// Phase 2 EOD metrics. A null value is shown as "not enough data yet", never 0, and no P&L
// number is computed or altered here — every figure is read from the API as stored.
const NOT_YET = 'not enough data yet';
const moneyOrNotYet = v => (v === null || v === undefined) ? NOT_YET : money(paiseToInr(v));
const bpsOrNotYet = v => (v === null || v === undefined) ? NOT_YET : `${fmt(v, 2)} bps`;
const ratioOrNotYet = v => (v === null || v === undefined) ? NOT_YET : pct(v * 100, 1);
const OUTAGE_LABELS = {no_monitoring: 'No monitoring', monitor_coverage_low: 'Low monitor coverage', scan_missing: 'Scan missing', eod_missing: 'EOD missing'};
const outageDetail = o => {
  const parts = [];
  if (o.coverage !== null && o.coverage !== undefined) parts.push(`coverage ${pct(o.coverage * 100, 1)}`);
  if (o.checks !== null && o.checks !== undefined) parts.push(`${o.checks} checks`);
  return parts.join(' · ');
};

function CostBook({refreshKey}) {
  const a = useDashboard({view: 'analytics'}, refreshKey);
  const [tab, setTab] = useState('strategies');
  if (a.loading && !a.data) return <Loading label="Reading net-of-cost analytics…"/>;
  if (a.error) return <Callout tone="warn" title="Net-of-cost analytics unavailable.">{a.error}</Callout>;
  const d = a.data, t = d.totals ?? {};
  const charges = Object.entries(d.charge_breakdown_paise ?? {}).map(([k, v]) => ({charge: k.replaceAll('_', ' '), amount: paiseToInr(v)}));
  return <>
    <div className="ad-tiles">
      <Tile label="Gross total" value={signedInr(paiseToInr(t.gross_total_paise))} tone={tone(t.gross_total_paise)} hint={t.missing_marks ? `${t.missing_marks} missing marks` : 'Realized + marked'}/>
      <Tile label="Net after accrued costs" value={signedInr(paiseToInr(t.net_total_accrued_paise))} tone={tone(t.net_total_accrued_paise)} hint={`Costs ${inr(paiseToInr(t.accrued_cost_paise))}`}/>
      <Tile label="Net after est. exit costs" value={signedInr(paiseToInr(d.net_after_estimated_exit_paise))} tone={tone(d.net_after_estimated_exit_paise)} hint={`Exit est. ${inr(paiseToInr(d.estimated_exit_cost_paise))}`}/>
      <Tile label="Week to date (net)" value={signedInr(paiseToInr(d.weekly?.net_paise))} tone={tone(d.weekly?.net_paise)} hint={d.weekly?.complete ? `Since ${d.weekly.week_start}` : 'Pending verified EODs'}/>
      <Tile label="Ledger reconciled" value={d.ledger_reconciled === true ? 'Yes' : d.ledger_reconciled === false ? 'No' : '—'} tone={d.ledger_reconciled === false ? 'down' : 'flat'} hint={d.health ? `Worker ${d.health.status} · ${istTime(d.health.heartbeat_at)}` : 'No worker heartbeat'}/>
    </div>
    {d.heartbeatStale && <Callout tone="warn">The monitoring worker heartbeat is older than 10 minutes during an expected session.</Callout>}
    {!d.weekly?.complete && d.weekly?.reason && <p className="ad-muted ad-fine">Week: {d.weekly.reason}</p>}
    <Tabs label="Breakdown" value={tab} onChange={setTab} items={[['strategies', 'By strategy'], ['cohorts', 'By overlap cohort'], ['eod', 'End-of-day book', d.eods?.length ?? 0], ['charges', 'Charges']]}/>
    {tab === 'strategies' && <SortTable caption="net by strategy" rows={d.strategies ?? []} columns={[
      {key: 'strategy', title: 'Strategy', render: strat}, {key: 'fills', title: 'Fills', align: 'right'}, {key: 'open_positions', title: 'Open', align: 'right'},
      {key: 'closed_trades', title: 'Closed', align: 'right'}, {key: 'win_rate', title: 'Win', align: 'right', render: v => pct(v)},
      {key: 'gross_total_paise', title: 'Gross', align: 'right', render: v => money(paiseToInr(v))},
      {key: 'accrued_cost_paise', title: 'Costs', align: 'right', render: v => inr(paiseToInr(v))},
      {key: 'net_total_accrued_paise', title: 'Net', align: 'right', render: v => money(paiseToInr(v))},
    ]}/>}
    {tab === 'cohorts' && <SortTable caption="net by cohort" rows={d.cohorts ?? []} columns={[
      {key: 'cohort', title: 'Held by', render: v => v.replaceAll('_', ' ')}, {key: 'open_positions', title: 'Open', align: 'right'}, {key: 'closed_trades', title: 'Closed', align: 'right'},
      {key: 'gross_total_paise', title: 'Gross', align: 'right', render: v => money(paiseToInr(v))}, {key: 'net_total_accrued_paise', title: 'Net', align: 'right', render: v => money(paiseToInr(v))},
    ]}/>}
    {tab === 'eod' && <>
      {(() => {
        const eods = d.eods ?? [];
        const latestPerf = [...eods].reverse().find(e => e.performance);
        const perf = latestPerf?.performance?.status === 'ok' ? latestPerf.performance : null;
        const perfDay = latestPerf?.day;
        const closed = perf?.expectancy?.closed_trades;
        return <Section eyebrow={perfDay ? `Latest EOD · ${perfDay}` : 'Latest EOD'} title="Execution quality">
          <div className="ad-tiles">
            <Tile label="Expectancy (net)" value={moneyOrNotYet(perf?.expectancy?.expectancy_net_paise)} tone={tone(perf?.expectancy?.expectancy_net_paise)} hint={perf ? (closed === null || closed === undefined ? NOT_YET : `${closed} closed trades`) : NOT_YET}/>
            <Tile label="Expectancy (gross)" value={moneyOrNotYet(perf?.expectancy?.expectancy_gross_paise)} tone={tone(perf?.expectancy?.expectancy_gross_paise)} hint={perf ? (closed === null || closed === undefined ? NOT_YET : `${closed} closed trades`) : NOT_YET}/>
            <Tile label="Max drawdown" value={moneyOrNotYet(perf?.drawdown?.max_drawdown_net_paise)} tone={tone(perf?.drawdown?.max_drawdown_net_paise)} hint={perf?.drawdown?.peak_day && perf?.drawdown?.trough_day ? `Peak ${perf.drawdown.peak_day} → trough ${perf.drawdown.trough_day}` : undefined}/>
            <Tile label="Current drawdown" value={moneyOrNotYet(perf?.drawdown?.current_drawdown_net_paise)} tone={tone(perf?.drawdown?.current_drawdown_net_paise)}/>
            <Tile label="Cost / gross" value={ratioOrNotYet(perf?.cost_to_gross)} hint={perf ? 'Accrued costs over gross' : NOT_YET}/>
            <Tile label="Shortfall vs signal close" value={bpsOrNotYet(perf?.implementation_shortfall_bps_vs_signal_close)} hint={perf ? 'Positive = worse than reference' : NOT_YET}/>
            <Tile label="Shortfall vs session open" value={bpsOrNotYet(perf?.implementation_shortfall_bps_vs_session_open)} hint={perf ? 'Positive = worse than reference' : NOT_YET}/>
          </div>
          {latestPerf?.performance?.status === 'unavailable' && <p className="ad-muted ad-fine">Performance unavailable for {perfDay}.</p>}
          {!latestPerf && <p className="ad-muted ad-fine">No EOD has a performance block yet.</p>}
        </Section>;
      })()}
      <Section eyebrow="Sessions" title="Outages">
        {(d.outages ?? []).length === 0
          ? <p className="ad-muted">No outages recorded.</p>
          : <ul className="ad-outages">{(d.outages ?? []).map(o => <li key={`${o.session_day}-${o.kind}`} className="ad-outage-row">
            <span className="ad-outage-day">{o.session_day}</span>
            <span className="ad-outage-kind">{OUTAGE_LABELS[o.kind] ?? o.kind}</span>
            {outageDetail(o) && <span className="ad-muted ad-fine">{outageDetail(o)}</span>}
            <span className="ad-muted ad-fine">{istTime(o.detected_at)}</span>
          </li>)}</ul>}
      </Section>
      <SortTable caption="end-of-day book" rows={[...(d.eods ?? [])].reverse()} rowKey={r => r.day} columns={[
      {key: 'day', title: 'Session'}, {key: 'daily_gross_paise', title: 'Daily gross', align: 'right', render: v => money(paiseToInr(v))},
      {key: 'daily_net_accrued_paise', title: 'Daily net', align: 'right', render: v => money(paiseToInr(v))},
      {key: 'net_total_accrued_paise', title: 'Net total', align: 'right', render: v => money(paiseToInr(v))},
      {key: 'closing_valuation_complete', title: 'Close valued', render: v => <Tag tone={v ? 'good' : 'warn'}>{v ? 'complete' : 'incomplete'}</Tag>},
      {key: 'revaluation', title: 'Revalued', render: (v, r) => (r.revaluation && r.mark_basis === 'official_close_revaluation' ? <Tag tone="good">Revalued from official close</Tag> : '—')},
      {key: 'as_of', title: 'Written', render: v => istTime(v)},
    ]} empty={{title: 'No end-of-day snapshot yet', text: 'Snapshots are written by the 16:10 IST EOD routine.'}}/>
    </>}
    {tab === 'charges' && <SortTable caption="charges" rows={charges} rowKey={r => r.charge} columns={[{key: 'charge', title: 'Charge'}, {key: 'amount', title: 'Accrued', align: 'right', render: v => inr(v)}]}/>}
  </>;
}

export default function Performance({overview, refreshKey}) {
  const live = overview.data;
  if (!live) return overview.loading ? <Loading label="Reading performance…"/> : <Blank title="Live ledger unavailable" text="Performance is only shown from a successful ledger read."/>;
  const t = live.totals, c = live.cappedLedger, cs = live.closedStats;
  const days = (live.dailyHistory ?? []).filter(d => d.daily !== null || d.total !== null);
  return <div className="ad-screen">
    <div className="ad-hero"><div><span className="ad-eyebrow">Performance · live paper</span><h1>The record so far</h1>
      <p>Every figure is read from the engine’s stored snapshots and fills. Days without a snapshot stay blank; nothing is interpolated. The isolated RELIANCE demo trade is excluded.</p></div>
      <div className="ad-hero-figure"><span>Total P&amp;L (gross, marked)</span><strong className={`ad-${tone(t.total)}`}>{signedInr(t.total)}</strong>
        <small>{t.total === null ? 'Unavailable while a mark is missing' : `Realized ${signedInr(t.realized)} · unrealized ${signedInr(t.unrealized)}`}</small></div>
    </div>
    <div className="ad-tiles">
      <Tile label="Today (stored snapshot)" value={signedInr(t.daily)} tone={tone(t.daily)} hint={t.daily === null ? 'No snapshot for today yet' : 'Daily mark-to-market'}/>
      <Tile label="Capital return (capped)" value={pct(c?.capital_return_pct, 3, true)} tone={tone(c?.capital_return_pct)} hint={`Net ${signedInr(paiseToInr(c?.headline_net_pnl_paise))}`}/>
      <Tile label="Closed trades" value={fmt(cs.count, 0)} hint={`${cs.matchedClosures} matched to entries`}/>
      <Tile label="Win rate" value={pct(cs.winRate)} hint={cs.count ? 'Closed trades only' : 'No closed trades yet'}/>
      <Tile label="Avg hold" value={cs.averageHoldDays === null ? '—' : `${fmt(cs.averageHoldDays, 1)} d`}/>
    </div>
    <div className="ad-split">
      <Section eyebrow="Per session" title="Daily mark-to-market">
        <Columns unit="" rows={days.filter(d => d.daily !== null).map(d => ({label: d.day, value: d.daily}))} describe={() => '₹ daily MTM from the stored snapshot'}
          empty={{title: 'No daily snapshots yet', text: 'The first bar appears after an EOD snapshot is written.'}}/>
      </Section>
      <Section eyebrow="Cumulative" title="Latest known total P&L">
        <Lines baseline={0} series={[{name: 'Total P&L (₹)', color: 'var(--ad-brand)', points: days.filter(d => d.total !== null).map(d => ({x: d.day, y: d.total}))}]}
          empty={{title: 'No totals recorded yet'}}/>
      </Section>
    </div>
    <Section eyebrow="Gross ledger" title="By strategy">
      <SortTable caption="performance by strategy" rows={(live.performance ?? []).map(p => ({...p, slots: c?.sleeves?.[p.strategy]?.slots_max ?? null, equity: paiseToInr(c?.sleeves?.[p.strategy]?.equity_paise), net: paiseToInr(c?.sleeves?.[p.strategy]?.net_pnl_paise)}))} rowKey={r => r.strategy} columns={[
        {key: 'strategy', title: 'Strategy', render: strat},
        {key: 'openPositions', title: 'Open / slots', align: 'right', render: (v, r) => <span className={r.slots && v > r.slots ? 'ad-down' : ''}>{v} / {r.slots ?? '—'}</span>},
        {key: 'closedTrades', title: 'Closed', align: 'right'}, {key: 'winRate', title: 'Win', align: 'right', render: v => pct(v)},
        {key: 'realized', title: 'Realized', align: 'right', render: money}, {key: 'equity', title: 'Sleeve equity', align: 'right', render: v => inr(v, 0)},
        {key: 'net', title: 'Sleeve net', align: 'right', render: money},
      ]}/>
    </Section>
    <Section eyebrow="After charges" title="Net-of-cost book"><CostBook refreshKey={refreshKey}/></Section>
    <p className="ad-muted ad-fine">Not shown on purpose: CAGR, Sharpe, drawdown and other risk ratios. The paper record is too short for them to mean anything, so they are not computed.</p>
  </div>;
}
