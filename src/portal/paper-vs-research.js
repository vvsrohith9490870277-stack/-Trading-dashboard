// Research (saved backtest export) vs live paper (paper_v1) statistics, computed separately and
// never blended. Inputs are rows exactly as returned by /api/dashboard and /data/historical.json.
// A figure whose inputs are missing is null (shown as "—"), never zero.
const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const sum = list => list.reduce((a, v) => a + Number(v), 0);
const max = list => (list.length ? Math.max(...list) : null);
const min = list => (list.length ? Math.min(...list) : null);

// The isolated RELIANCE manual demo lives in paper_demo_v1. Ledger views read paper_v1 only, but any
// row that is tagged as demo/test is dropped again here so it can never reach a total.
export const DEMO_NAMESPACE = 'paper_demo_v1';
export function isDemo(r) {
  return Boolean(r) && (r.namespace === DEMO_NAMESPACE || r.is_test === 1 || r.is_test === true || r.demo === true || /^dummy_/.test(String(r.id ?? '')));
}
export const withoutDemo = rows => (rows ?? []).filter(r => !isDemo(r));

// Saved-research figures for one strategy from historical.json: gross % per closed trade, costs excluded.
export function researchStats(s) {
  if (!s) return null;
  const m = s.metrics ?? {};
  const best = s.top?.[0] ?? null, worst = s.bottom?.[0] ?? null;
  return {
    strategy: s.id, closed: finite(m.closed_trades) ? Number(m.closed_trades) : null,
    winRate: Number(m.closed_trades) > 0 && finite(m.win_rate_pct) ? Number(m.win_rate_pct) : null,
    mean: Number(m.closed_trades) > 0 && finite(m.mean_gross_return_pct) ? Number(m.mean_gross_return_pct) : null,
    median: Number(m.closed_trades) > 0 && finite(m.median_gross_return_pct) ? Number(m.median_gross_return_pct) : null,
    best: finite(best?.return_pct) ? Number(best.return_pct) : null, bestTrade: best, bestFlagged: Boolean(best?.dataSensitive),
    worst: finite(worst?.return_pct) ? Number(worst.return_pct) : null, worstTrade: worst, worstFlagged: Boolean(worst?.dataSensitive),
    openAtExport: finite(m.open_positions) ? Number(m.open_positions) : null,
  };
}

// Research figures for one stock under one strategy (perStock row), same basis.
export function researchStockStats(s, symbol) {
  const p = s?.perStock?.find(x => x.symbol === symbol);
  if (!p) return {closed: null, winRate: null, mean: null, best: null, worst: null};
  const n = Number(p.closed) || 0;
  return {closed: n, winRate: n && finite(p.winRate) ? Number(p.winRate) : null, mean: n && finite(p.mean) ? Number(p.mean) : null,
    best: n && finite(p.best) ? Number(p.best) : null, worst: n && finite(p.worst) ? Number(p.worst) : null};
}

// Closed-trade statistics of paper rows (view=trades). Win/mean/best/worst are null when nothing closed:
// zero closed trades is "no evidence", not a 0% win rate. % uses pnl_pct, which the API only sets when
// the exit quantity matches its paired entry; rupee figures use realized_pnl.
export function closedStats(trades) {
  const list = withoutDemo(trades);
  const valued = list.filter(t => finite(t.realized_pnl)).map(t => Number(t.realized_pnl));
  const pcts = list.filter(t => finite(t.pnl_pct)).map(t => Number(t.pnl_pct));
  const nets = list.map(t => t.net_pnl_paise), charges = list.map(t => t.charges_paise);
  const closed = list.length;
  return {
    closed, valued: valued.length, pctCount: pcts.length,
    wins: valued.filter(v => v > 0).length, losses: valued.filter(v => v < 0).length,
    winRate: valued.length ? valued.filter(v => v > 0).length / valued.length * 100 : null,
    mean: pcts.length ? sum(pcts) / pcts.length : null, best: max(pcts), worst: min(pcts),
    realized: valued.length ? sum(valued) : null, bestInr: max(valued), worstInr: min(valued),
    realizedNet: closed && nets.every(finite) ? sum(nets) / 100 : null, netMissing: list.filter(t => !finite(t.net_pnl_paise)).length,
    closedCharges: closed && charges.every(finite) ? sum(charges) / 100 : null,
  };
}

// Open-position money (view=positions). Gross is summed over marked positions only; the count of
// unmarked ones is returned so the UI can say the sum is partial. Net needs a fee row and a mark on
// every position (entry fee recorded + engine exit-fee estimate), otherwise it is null.
export function openStats(positions) {
  const list = withoutDemo(positions);
  const marked = list.filter(p => finite(p.unrealized_pnl));
  const basis = sum(marked.filter(p => finite(p.cost_basis)).map(p => p.cost_basis));
  const gross = marked.length ? sum(marked.map(p => p.unrealized_pnl)) : null;
  const nets = list.map(p => p.net_unrealized_paise), ch = list.map(p => p.charges_paise);
  return {
    open: list.length, qty: sum(list.filter(p => finite(p.qty)).map(p => p.qty)), unmarked: list.length - marked.length,
    costBasis: list.length ? sum(list.filter(p => finite(p.cost_basis)).map(p => p.cost_basis)) : null,
    marketValue: marked.length ? sum(marked.filter(p => finite(p.market_value)).map(p => p.market_value)) : null,
    unrealized: gross, unrealizedPct: gross !== null && basis > 0 ? gross / basis * 100 : null,
    unrealizedNet: list.length && nets.every(finite) ? sum(nets) / 100 : null, netMissing: list.filter(p => !finite(p.net_unrealized_paise)).length,
    openCharges: list.length && ch.every(finite) ? sum(ch) / 100 : null,
  };
}

// Charges actually recorded against fills (paper_fill_costs). Null when no fill has a fee row.
export function fillCharges(fills) {
  const list = withoutDemo(fills);
  const costed = list.filter(f => finite(f.costs?.charges_paise));
  return {fills: list.length, charges: costed.length ? sum(costed.map(f => f.costs.charges_paise)) / 100 : null, missing: list.length - costed.length};
}

const byStrategy = (rows, id) => withoutDemo(rows).filter(r => Number(r.strategy) === Number(id));

// One strategy's paper record, all sources kept separate from research.
export function paperStats(id, {trades = [], positions = [], fills = [], sleeve = null} = {}) {
  const c = closedStats(byStrategy(trades, id)), o = openStats(byStrategy(positions, id)), f = fillCharges(byStrategy(fills, id));
  const net = sleeve && finite(sleeve.net_pnl_paise) ? Number(sleeve.net_pnl_paise) / 100 : null;
  return {strategy: Number(id), ...c, ...o, fills: f.fills, chargesPaid: f.charges, chargesMissing: f.missing, sleeveNet: net, slots: sleeve?.slots_max ?? null};
}

// Symbols in the actual paper book (open now, closed, or filled) that two or more of `ids` touched.
export function paperOverlap(ids, {trades = [], positions = [], fills = []} = {}) {
  const want = new Set(ids.map(Number));
  const m = new Map();
  const add = (kind, r) => {
    const s = Number(r.strategy);
    if (!want.has(s) || !r.symbol) return;
    const bySym = m.get(r.symbol) ?? new Map();
    const a = bySym.get(s) ?? {positions: [], trades: [], fills: []};
    a[kind].push(r); bySym.set(s, a); m.set(r.symbol, bySym);
  };
  withoutDemo(positions).forEach(r => add('positions', r));
  withoutDemo(trades).forEach(r => add('trades', r));
  withoutDemo(fills).forEach(r => add('fills', r));
  const out = [];
  for (const [symbol, bySym] of m) {
    if (bySym.size < 2) continue;
    const legs = [...bySym.entries()].sort((a, b) => a[0] - b[0]).map(([strategy, a]) => {
      const o = openStats(a.positions), c = closedStats(a.trades), f = fillCharges(a.fills);
      const pos = a.positions.filter(p => finite(p.qty) && finite(p.entry_price));
      const mark = a.positions.map(p => p.mark_price).find(finite);
      return {symbol, strategy, ...o, entry: o.qty ? sum(pos.map(p => p.qty * p.entry_price)) / sum(pos.map(p => p.qty)) : null,
        mark: finite(mark) ? Number(mark) : null, closed: c.closed, realized: c.realized, realizedNet: c.realizedNet, closedMean: c.mean,
        fills: f.fills, chargesPaid: f.charges, chargesMissing: f.missing};
    });
    const nn = list => (list.every(finite) ? sum(list) : null);
    const marked = legs.filter(l => finite(l.unrealized));
    const basis = sum(marked.filter(l => finite(l.costBasis)).map(l => l.costBasis));
    const unrealized = marked.length ? sum(marked.map(l => l.unrealized)) : null;
    const anyClosed = legs.some(l => l.closed);
    out.push({symbol, strategies: legs.map(l => l.strategy), legs, combined: {
      open: sum(legs.map(l => l.open)), qty: sum(legs.map(l => l.qty)), closed: sum(legs.map(l => l.closed)),
      unrealized, unrealizedPct: unrealized !== null && basis > 0 ? unrealized / basis * 100 : null, unmarked: sum(legs.map(l => l.unmarked)),
      unrealizedNet: legs.some(l => l.open) ? nn(legs.filter(l => l.open).map(l => l.unrealizedNet)) : null,
      realized: anyClosed ? nn(legs.filter(l => l.closed).map(l => l.realized)) : null,
      realizedNet: anyClosed ? nn(legs.filter(l => l.closed).map(l => l.realizedNet)) : null,
      chargesPaid: legs.some(l => l.chargesPaid !== null) ? sum(legs.filter(l => l.chargesPaid !== null).map(l => l.chargesPaid)) : null,
      chargesMissing: sum(legs.map(l => l.chargesMissing)),
    }});
  }
  return out.sort((a, b) => b.strategies.length - a.strategies.length || a.symbol.localeCompare(b.symbol));
}

// Which symbols the paper book holds now or has traded, per strategy (for flagging research names).
export function paperFootprint({trades = [], positions = [], fills = []} = {}) {
  const held = new Map(), traded = new Map();
  const put = (map, r) => { if (!r.symbol) return; const s = map.get(r.symbol) ?? new Set(); s.add(Number(r.strategy)); map.set(r.symbol, s); };
  withoutDemo(positions).forEach(r => put(held, r));
  [...withoutDemo(trades), ...withoutDemo(fills)].forEach(r => put(traded, r));
  return {held, traded};
}

// Paper book age from fills: first IST fill day and the number of distinct IST days with fills.
export function paperAge(fills) {
  const f = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit'});
  const days = [...new Set(withoutDemo(fills).map(r => { const d = new Date(r.filled_at ?? ''); return Number.isNaN(d.getTime()) ? null : f.format(d); }).filter(Boolean))].sort();
  return {first: days[0] ?? null, last: days.at(-1) ?? null, sessions: days.length};
}
