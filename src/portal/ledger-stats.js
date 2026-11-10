// Pure, testable summaries of the complete paper record. Inputs are rows exactly as returned by
// /api/dashboard (trades, fills). Nothing is estimated: a figure whose inputs are missing is null.
const IST_DAY = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit'});
const IST_HOUR = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Kolkata', hour: '2-digit', hour12: false});
const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));

export function istDate(ts) { const d = new Date(ts ?? ''); return Number.isNaN(d.getTime()) ? null : IST_DAY.format(d); }
export function istHour(ts) { const d = new Date(ts ?? ''); return Number.isNaN(d.getTime()) ? null : Number(IST_HOUR.format(d)) % 24; }

export const RANGES = [['today', 'Today'], ['week', 'This week'], ['month', 'This month'], ['last_month', 'Last month'], ['last_3_months', 'Last 3 months'], ['last_6_months', 'Last 6 months'], ['last_year', 'Last year'], ['all', 'All time']];

const iso = d => d.toISOString().slice(0, 10);
// Inclusive [from, to] IST calendar-day bounds for a range, relative to the IST day `day` (YYYY-MM-DD).
export function rangeBounds(range, day) {
  if (range === 'all' || !/^\d{4}-\d{2}-\d{2}$/.test(day ?? '')) return [null, null];
  const d = new Date(day + 'T00:00:00Z');
  const shift = (months) => { const x = new Date(d); x.setUTCDate(1); x.setUTCMonth(x.getUTCMonth() + months); return x; };
  if (range === 'today') return [day, day];
  if (range === 'week') { const x = new Date(d); x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7)); return [iso(x), day]; }
  if (range === 'month') return [iso(shift(0)), day];
  if (range === 'last_month') { const end = shift(0); end.setUTCDate(0); return [iso(shift(-1)), iso(end)]; }
  if (range === 'last_3_months') return [iso(shift(-3)), day];
  if (range === 'last_6_months') return [iso(shift(-6)), day];
  if (range === 'last_year') { const x = new Date(d); x.setUTCFullYear(x.getUTCFullYear() - 1); return [iso(x), day]; }
  return [null, null];
}
export function inRange(ts, [from, to]) { if (!from) return true; const d = istDate(ts); return d !== null && d >= from && d <= to; }

const sum = list => list.reduce((a, v) => a + Number(v), 0);
const pnlOf = t => (finite(t.realized_pnl) ? Number(t.realized_pnl) : null);

export function tradeStats(trades) {
  const list = [...trades].sort((a, b) => String(a.closed_at).localeCompare(String(b.closed_at)));
  const valued = list.filter(t => pnlOf(t) !== null);
  const wins = valued.filter(t => pnlOf(t) > 0), losses = valued.filter(t => pnlOf(t) < 0);
  const grossProfit = sum(wins.map(pnlOf)), grossLoss = sum(losses.map(pnlOf));
  const nets = list.map(t => t.net_pnl_paise);
  const netComplete = list.length > 0 && nets.every(finite);
  const charges = list.map(t => t.charges_paise);
  const holds = list.map(t => t.hold_hours).filter(finite).map(Number);
  const rets = list.map(t => t.pnl_pct).filter(finite).map(Number);
  const matched = list.filter(t => t.entry_matched && finite(t.entry_price));
  let longestWin = 0, longestLoss = 0, run = 0, current = null;
  for (const t of valued) {
    const sign = pnlOf(t) > 0 ? 1 : pnlOf(t) < 0 ? -1 : 0;
    run = sign !== 0 && current && Math.sign(current) === sign ? current + sign : sign;
    current = run;
    if (run > 0) longestWin = Math.max(longestWin, run);
    if (run < 0) longestLoss = Math.max(longestLoss, -run);
  }
  const bySymbol = groupBy(valued, t => t.symbol);
  const best = valued.length ? valued.reduce((a, t) => (pnlOf(t) > pnlOf(a) ? t : a)) : null;
  const worst = valued.length ? valued.reduce((a, t) => (pnlOf(t) < pnlOf(a) ? t : a)) : null;
  const sameDay = list.filter(t => t.opened_at && istDate(t.opened_at) === istDate(t.closed_at)).length;
  return {
    count: list.length, valued: valued.length, wins: wins.length, losses: losses.length, flats: valued.length - wins.length - losses.length,
    winRate: valued.length ? wins.length / valued.length * 100 : null, lossRate: valued.length ? losses.length / valued.length * 100 : null,
    grossPnl: valued.length ? sum(valued.map(pnlOf)) : null, grossProfit: wins.length ? grossProfit : null, grossLoss: losses.length ? grossLoss : null,
    netPnl: netComplete ? sum(nets) / 100 : null, netMissing: list.filter(t => !finite(t.net_pnl_paise)).length,
    charges: list.length && charges.every(finite) ? sum(charges) / 100 : null,
    avgProfit: wins.length ? grossProfit / wins.length : null, avgLoss: losses.length ? grossLoss / losses.length : null,
    profitFactor: wins.length && losses.length ? grossProfit / Math.abs(grossLoss) : null,
    expectancy: valued.length ? sum(valued.map(pnlOf)) / valued.length : null,
    largestProfit: wins.length ? Math.max(...wins.map(pnlOf)) : null, largestLoss: losses.length ? Math.min(...losses.map(pnlOf)) : null,
    best, worst, avgHoldHours: holds.length ? sum(holds) / holds.length : null, avgReturnPct: rets.length ? sum(rets) / rets.length : null,
    avgEntry: matched.length ? sum(matched.map(t => t.entry_price)) / matched.length : null,
    avgExit: list.length ? sum(list.map(t => t.exit_price).filter(finite)) / Math.max(1, list.filter(t => finite(t.exit_price)).length) : null,
    mostProfitableSymbol: bySymbol.length ? bySymbol.reduce((a, g) => (g.pnl > a.pnl ? g : a)) : null,
    mostLosingSymbol: bySymbol.length ? bySymbol.reduce((a, g) => (g.pnl < a.pnl ? g : a)) : null,
    longestWin, longestLoss, currentStreak: current, sameDay, overnight: list.length - sameDay, unmatched: list.filter(t => !t.entry_matched).length,
  };
}

// Groups with trades/wins/win rate/P&L/avg hold. `key` returns the group label (e.g. symbol, strategy, industry).
export function groupBy(trades, key) {
  const m = new Map();
  for (const t of trades) {
    const k = key(t) ?? 'Unclassified';
    const g = m.get(k) ?? {key: k, trades: 0, wins: 0, losses: 0, pnl: 0, valued: 0, holds: [], allocation: 0, profit: 0, loss: 0};
    g.trades++;
    const p = pnlOf(t);
    if (p !== null) { g.valued++; g.pnl += p; if (p > 0) { g.wins++; g.profit += p; } if (p < 0) { g.losses++; g.loss += p; } }
    if (finite(t.hold_hours)) g.holds.push(Number(t.hold_hours));
    if (finite(t.entry_price) && finite(t.qty)) g.allocation += Number(t.entry_price) * Number(t.qty);
    m.set(k, g);
  }
  return [...m.values()].map(({holds, ...g}) => ({...g, winRate: g.valued ? g.wins / g.valued * 100 : null, avgHoldHours: holds.length ? sum(holds) / holds.length : null}))
    .sort((a, b) => b.trades - a.trades || String(a.key).localeCompare(String(b.key)));
}

export function fillStats(fills) {
  const buys = fills.filter(f => f.side === 'BUY'), sells = fills.filter(f => f.side === 'SELL');
  const turnover = fills.every(f => finite(f.costs?.turnover_paise)) && fills.length ? sum(fills.map(f => f.costs.turnover_paise)) / 100 : null;
  const charges = fills.every(f => finite(f.costs?.charges_paise)) && fills.length ? sum(fills.map(f => f.costs.charges_paise)) / 100 : null;
  const notional = sum(fills.filter(f => finite(f.price) && finite(f.qty)).map(f => Number(f.price) * Number(f.qty)));
  return {count: fills.length, buys: buys.length, sells: sells.length, turnover, charges, notional, missingCosts: fills.filter(f => !f.costs).length,
    avgSize: fills.length ? notional / fills.length : null};
}

// Fills bucketed by IST clock hour ("09:00–10:00"), with realized gross P&L of sells in that hour.
export function sessionSlots(fills) {
  const m = new Map();
  for (const f of fills) {
    const h = istHour(f.filled_at);
    if (h === null) continue;
    const slot = `${String(h).padStart(2, '0')}:00–${String((h + 1) % 24).padStart(2, '0')}:00`;
    const g = m.get(slot) ?? {slot, fills: 0, buys: 0, sells: 0, pnl: 0};
    g.fills++; if (f.side === 'BUY') g.buys++; else { g.sells++; if (finite(f.realized_pnl)) g.pnl += Number(f.realized_pnl); }
    m.set(slot, g);
  }
  return [...m.values()].sort((a, b) => a.slot.localeCompare(b.slot));
}

export function signalKind(reason) {
  const r = String(reason ?? '');
  if (r.startsWith('daily_entry')) return 'Entry';
  if (r.startsWith('daily_exit')) return 'Daily exit';
  if (r === 'observed_LTP_below_close_highwater_stop') return 'Stop exit';
  return '—';
}
export function exitSource(reason) {
  const r = String(reason ?? '');
  if (r.startsWith('daily_exit')) return 'Completed-bar daily exit rule';
  if (r === 'observed_LTP_below_close_highwater_stop') return 'Intraday stop check (LTP below close high-water stop)';
  return 'Recorded by engine';
}
