const nf = (d) => new Intl.NumberFormat('en-IN', {minimumFractionDigits: d, maximumFractionDigits: d});
export const isNum = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
export function fmt(v, d = 2) { return isNum(v) ? nf(d).format(Number(v)) : '—'; }
export function inr(v, d = 2) { return isNum(v) ? (Number(v) < 0 ? '−₹' : '₹') + nf(d).format(Math.abs(Number(v))) : '—'; }
export function signedInr(v, d = 2) { return isNum(v) ? (Number(v) > 0 ? '+' : '') + inr(v, d) : '—'; }
export function pct(v, d = 1, signed = false) { return isNum(v) ? (signed && Number(v) > 0 ? '+' : '') + nf(d).format(Number(v)) + '%' : '—'; }
export function tone(v) { return !isNum(v) ? 'flat' : Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : 'flat'; }
export function ticker(symbol) { return String(symbol ?? '').replace(/-EQ$/, ''); }
export function istTime(v, withDate = true) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  return new Intl.DateTimeFormat('en-IN', {timeZone: 'Asia/Kolkata', ...(withDate ? {day: '2-digit', month: 'short'} : {}), hour: '2-digit', minute: '2-digit', hour12: false}).format(d) + ' IST';
}
// Same as istTime but with seconds, for audit rows where same-minute events must stay distinguishable.
export function istTimeSec(v, withDate = true) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  return new Intl.DateTimeFormat('en-IN', {timeZone: 'Asia/Kolkata', ...(withDate ? {day: '2-digit', month: 'short'} : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).format(d) + ' IST';
}
export const STRATEGY_NAMES = ['Donchian / Turtle', 'Supertrend', 'RSI Pullback', 'Dual MA', 'Bollinger Reversion', 'MACD Trend', '52W Momentum'];
export const STRATEGY_HUES = ['#6c7cff', '#ff8a4c', '#2bb8a3', '#e35d9c', '#c9a227', '#4aa3df', '#9b6bff'];
export function strategyName(id) { return STRATEGY_NAMES[Number(id) - 1] ?? `Strategy ${id}`; }
