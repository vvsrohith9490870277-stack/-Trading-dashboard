// Compare screen: saved research vs the live paper_v1 book, side by side and never blended,
// plus stocks that more than one strategy actually holds or traded on paper.
import {Fragment} from 'react';
import {Link, stockHref} from './router';
import {fmt, inr, pct, signedInr, strategyName, STRATEGY_HUES, ticker, tone} from './format';
import {Blank, Callout, Loading, Section, Tag} from './ui';
import {paperAge, paperOverlap, paperStats, researchStats} from './paper-vs-research';

const Strat = ({id}) => <span className="ad-strat"><i style={{background: STRATEGY_HUES[id - 1]}}/>{strategyName(id)}</span>;
const ret = (v, flagged) => <span className={`ad-${tone(v)}`} title={flagged ? 'Flagged in the export as data-sensitive (possible split / bad print). Treat with care.' : undefined}>{pct(v, 2, true)}{flagged ? ' *' : ''}</span>;
const money = v => <span className={`ad-${tone(v)}`}>{signedInr(v)}</span>;
const na = why => <span className="ad-muted-cell" title={why}>—</span>;

export function Incomplete({set, what}) {
  if (!set || set.complete) return null;
  return <Callout tone="warn" title="Partial record.">Read {set.rows.length} of {set.total} {what} (5,000-row cap); paper figures below cover only the rows read.</Callout>;
}

export function PaperAgeNote({fills}) {
  const a = paperAge(fills);
  return <Callout tone="info" title="Paper is very young.">
    {a.first ? `The paper_v1 book’s first fill was on ${a.first} (IST) and it has fills on ${a.sessions} session day${a.sessions === 1 ? '' : 's'} so far. ` : 'No paper fills are recorded yet. '}
    Research covers years of closed trades; a handful of paper trades does not prove or disprove a strategy. The two sides are shown next to each other and are never averaged, blended or ranked together. The RELIANCE manual demo (paper_demo_v1) is excluded.
  </Callout>;
}

export function ResearchVsPaper({ids, history, overview, ledger}) {
  const {trades, positions, fills} = ledger;
  const rows = ids.map(id => ({id, r: researchStats(history.strategies.find(s => s.id === id)),
    p: trades.data && positions.data && fills.data ? paperStats(id, {trades: trades.data.rows, positions: positions.data.rows, fills: fills.data.rows, sleeve: overview?.cappedLedger?.sleeves?.[id]}) : null}));
  const err = trades.error || positions.error || fills.error;
  const loading = !err && rows.some(x => !x.p);
  const noClose = 'No closed paper trades yet: win rate, mean, best and worst need at least one closed trade.';
  return <Section eyebrow={`Research ${history.asOf} | paper_v1 now`} title="Research vs paper, same metrics" className="ad-ledger-palette ad-pvr">
    {err && <Callout tone="error">{err}</Callout>}
    {loading && <Loading label="Reading every paper trade, position and fill…"/>}
    <Incomplete set={trades.data} what="closed trades"/><Incomplete set={positions.data} what="open positions"/><Incomplete set={fills.data} what="fills"/>
    <div className="ad-table-scroll"><table className="ad-table ad-pvr-table">
      <caption className="ad-sr">Research versus paper by strategy</caption>
      <thead>
        <tr className="ad-pvr-groups"><th scope="col" rowSpan={2}>Strategy</th>
          <th scope="colgroup" colSpan={5} className="ad-pvr-g research">Research · saved backtest (gross % / closed trade)</th>
          <th scope="colgroup" colSpan={6} className="ad-pvr-g paper">Paper · closed trades (paper_v1)</th>
          <th scope="colgroup" colSpan={5} className="ad-pvr-g now">Paper · open book now</th></tr>
        <tr>{['Closed', 'Win', 'Mean', 'Best', 'Worst'].map(h => <th key={'r' + h} scope="col" className="r research">{h}</th>)}
          {['Closed', 'Win', 'Mean %', 'Best %', 'Worst %', 'Realized ₹'].map(h => <th key={'p' + h} scope="col" className="r paper">{h}</th>)}
          {['Open / slots', 'Unrealized ₹', 'Net of charges ₹', 'Charges paid ₹', 'Sleeve net ₹'].map(h => <th key={'n' + h} scope="col" className="r now">{h}</th>)}</tr>
      </thead>
      <tbody>{rows.map(({id, r, p}) => <tr key={id}>
        <td><Strat id={id}/></td>
        <td className="r num research">{fmt(r?.closed, 0)}</td><td className="r num research">{pct(r?.winRate)}</td>
        <td className="r num research">{ret(r?.mean)}</td><td className="r num research">{ret(r?.best, r?.bestFlagged)}</td><td className="r num research">{ret(r?.worst, r?.worstFlagged)}</td>
        {!p ? <td colSpan={11} className="ad-muted-cell">{err ? 'Paper read failed' : 'Reading…'}</td> : <>
          <td className="r num paper">{fmt(p.closed, 0)}</td>
          <td className="r num paper">{p.closed ? pct(p.winRate) : na(noClose)}</td>
          <td className="r num paper">{p.closed ? (p.pctCount ? ret(p.mean) : na('No closed trade with a matched entry quantity, so a % return is undefined.')) : na(noClose)}</td>
          <td className="r num paper">{p.closed && p.pctCount ? ret(p.best) : na(noClose)}</td>
          <td className="r num paper">{p.closed && p.pctCount ? ret(p.worst) : na(noClose)}</td>
          <td className="r num paper">{p.closed ? money(p.realized) : na(noClose)}{p.closed && p.realizedNet !== null ? <small className="ad-muted"> net {signedInr(p.realizedNet)}</small> : null}</td>
          <td className="r num now"><span className={p.slots && p.open > p.slots ? "ad-down" : ""} title={p.slots && p.open > p.slots ? "More open positions than the sleeve slot cap" : undefined}>{p.open} / {p.slots ?? "—"}</span></td>
          <td className="r num now">{p.open ? <>{money(p.unrealized)}{p.unrealizedPct !== null && <small className="ad-muted"> {pct(p.unrealizedPct, 2, true)}</small>}{p.unmarked ? <small className="ad-muted"> ({p.unmarked} unmarked)</small> : null}</> : na('No open positions')}</td>
          <td className="r num now">{p.open ? (p.unrealizedNet !== null ? money(p.unrealizedNet) : na(`${p.netMissing} position(s) lack an entry fee row or a mark, so net is unavailable, not zero.`)) : na('No open positions')}</td>
          <td className="r num now">{p.chargesPaid !== null ? <>{inr(p.chargesPaid)}{p.chargesMissing ? <small className="ad-muted"> ({p.chargesMissing} fills lack fee rows)</small> : null}</> : na(p.fills ? 'No fee rows recorded for these fills.' : 'No fills')}</td>
          <td className="r num now">{money(p.sleeveNet)}</td>
        </>}
      </tr>)}</tbody>
    </table></div>
    <ul className="ad-pvr-legend ad-ledger-note">
      <li><b className="research">Research</b>: {history.asOf} export, every closed backtest trade, gross % per trade, costs excluded. Best/worst are the single largest and smallest closed trades; <b>*</b> = flagged data-sensitive in the export.</li>
      <li><b className="paper">Paper closed</b>: each SELL fill paired with the latest earlier BUY of the same strategy and stock. Win = share with positive gross realized ₹. Mean/Best/Worst % = (exit − entry) ÷ entry, only where the exit quantity matches its entry. Realized ₹ is gross; “net” subtracts recorded entry and exit charges.</li>
      <li><b className="now">Open book</b>: unrealized ₹ = (last mark − entry) × qty, gross; net of charges = gross − recorded entry fees − engine exit-fee estimate. Charges paid = sum of recorded fee rows on all fills. Sleeve net = the capped ledger’s sleeve equity − sleeve capital. Open unrealized is never counted as a closed-trade mean.</li>
    </ul>
  </Section>;
}

export function PaperOverlap({ids, ledger, researchShared = [], asOf}) {
  const {trades, positions, fills} = ledger;
  const err = trades.error || positions.error || fills.error;
  const ready = trades.data && positions.data && fills.data;
  const rows = ready ? paperOverlap(ids, {trades: trades.data.rows, positions: positions.data.rows, fills: fills.data.rows}) : [];
  const onPaper = new Set(ready ? [...positions.data.rows, ...trades.data.rows, ...fills.data.rows].filter(r => ids.includes(Number(r.strategy))).map(r => r.symbol) : []);
  return <Section eyebrow="paper_v1 · actual book" title="Stocks more than one strategy holds or traded on paper" className="ad-ledger-palette ad-pvr">
    {err ? <Callout tone="error">{err}</Callout> : !ready ? <Loading label="Reading the paper book…"/> : !rows.length
      ? <Blank title="No paper overlap among these strategies" text="No stock is held, closed or filled on paper by two or more of the selected strategies. Nothing is inferred from research scans."/>
      : <>
        <p className="ad-ledger-note">{rows.length} stock{rows.length === 1 ? '' : 's'} appear in the paper activity of 2+ selected strategies. One row per strategy leg, then the combined line. All figures are real paper_v1 records; nothing is simulated.</p>
        <div className="ad-table-scroll"><table className="ad-table ad-pvr-table">
          <caption className="ad-sr">Paper overlap stocks</caption>
          <thead><tr>{['Stock / strategy', 'Open qty', 'Avg entry', 'Mark', 'Unrealized ₹', 'Unrealized %', 'Net of charges ₹', 'Charges paid ₹', 'Closed', 'Realized ₹', 'Realized net ₹'].map((h, i) => <th key={h} scope="col" className={i ? 'r' : ''}>{h}</th>)}</tr></thead>
          <tbody>{rows.map(o => <Fragment key={o.symbol}>
            {o.legs.map(l => <tr key={o.symbol + l.strategy} className="ad-pvr-leg">
              <td><Link className="ad-sym" href={stockHref(o.symbol)}>{ticker(o.symbol)}</Link> <Strat id={l.strategy}/></td>
              <td className="r num">{l.open ? fmt(l.qty, 0) : '—'}</td><td className="r num">{l.open ? inr(l.entry) : '—'}</td><td className="r num">{l.open ? inr(l.mark) : '—'}</td>
              <td className="r num">{l.open ? money(l.unrealized) : '—'}</td><td className="r num">{l.open ? ret(l.unrealizedPct) : '—'}</td>
              <td className="r num">{l.open ? (l.unrealizedNet !== null ? money(l.unrealizedNet) : na('No entry fee row or no mark: net unavailable.')) : '—'}</td>
              <td className="r num">{l.chargesPaid !== null ? inr(l.chargesPaid) : na('No fee rows recorded')}</td>
              <td className="r num">{fmt(l.closed, 0)}</td>
              <td className="r num">{l.closed ? money(l.realized) : '—'}</td><td className="r num">{l.closed ? (l.realizedNet !== null ? money(l.realizedNet) : na('Fee rows missing for a leg')) : '—'}</td>
            </tr>)}
            <tr className="ad-pvr-combined">
              <td><strong>{ticker(o.symbol)} combined</strong> <Tag tone="info">{o.strategies.length} strategies</Tag></td>
              <td className="r num">{o.combined.open ? fmt(o.combined.qty, 0) : '—'}</td><td className="r num">—</td><td className="r num">—</td>
              <td className="r num">{o.combined.open ? money(o.combined.unrealized) : '—'}</td><td className="r num">{o.combined.open ? ret(o.combined.unrealizedPct) : '—'}</td>
              <td className="r num">{o.combined.open ? (o.combined.unrealizedNet !== null ? money(o.combined.unrealizedNet) : na('A leg lacks a fee row or mark')) : '—'}</td>
              <td className="r num">{o.combined.chargesPaid !== null ? inr(o.combined.chargesPaid) : '—'}</td>
              <td className="r num">{fmt(o.combined.closed, 0)}</td>
              <td className="r num">{o.combined.closed ? money(o.combined.realized) : '—'}</td><td className="r num">{o.combined.closed ? money(o.combined.realizedNet) : '—'}</td>
            </tr>
          </Fragment>)}</tbody>
        </table></div>
        <p className="ad-ledger-note">Combined unrealized % = summed gross unrealized ÷ summed cost basis of the marked legs. Avg entry and mark are per leg only; the legs are separate sleeve positions, not one netted holding.</p>
      </>}
    {ready && researchShared.length > 0 && <>
      <h3 className="ad-sub">Research-scan shared names on paper</h3>
      <p className="ad-ledger-note">Names every selected strategy matched in the {asOf} research scan (scan agreement, not performance), and whether any of them is actually held or traded on paper.</p>
      <ul className="ad-chips">{researchShared.map(s => <li key={s}><Link className="ad-sym" href={stockHref(s)}>{ticker(s)}</Link> {onPaper.has(s) ? <Tag tone="good">on paper</Tag> : <Tag tone="neutral">not on paper</Tag>}</li>)}</ul>
    </>}
  </Section>;
}
