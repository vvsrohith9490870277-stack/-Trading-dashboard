// One source for the exit / stop / target wording and the Strategy 07 trail level, shared by the
// dashboard API and the browser. The trail fraction is read from the engine config (the engine
// refuses to start if it disagrees with strategy_rules.json 7.trailing_update) and the frozen exit
// rule text is read from strategy_rules.json, so nothing here repeats a policy number by hand.
import rules from '../automation/paper-trading/strategy_rules.json' with {type: 'json'};
import engine from '../automation/paper-trading/config.json' with {type: 'json'};

export const TRAIL_FRACTION = Number(engine.strategy_07_trail_fraction);
if (!(TRAIL_FRACTION > 0 && TRAIL_FRACTION < 1)) throw new Error('STRATEGY_07_TRAIL_CONFIG_INVALID');
// Percentage below the high-water, e.g. 0.9 -> "10".
export const TRAIL_PCT_BELOW = String(Number(((1 - TRAIL_FRACTION) * 100).toFixed(6)));
// Strategies whose stop is a stored-state price level (only the 52-week momentum trail).
export const STOP_STRATEGIES = Object.freeze([7]);

// Plain-language exit rules (summaries of strategy_rules.json; Strategy 07 includes the configured trail).
export const EXIT_RULES = Object.freeze({
  1: 'Close below the prior 10-session low',
  2: 'A green-to-red Supertrend flip',
  3: 'RSI14 > 60 OR close > SMA5',
  4: 'EMA20 crosses below EMA50',
  5: 'Close returns to or exceeds the Bollinger middle band',
  6: 'MACD crosses below its signal',
  7: `Close < SMA50 OR the ${TRAIL_PCT_BELOW}% completed-close high-water stop`,
});
export const exitRule = id => EXIT_RULES[Number(id)] ?? null;

// The frozen engine wording of the exit, verbatim from strategy_rules.json.
export function engineExitRule(id) {
  const r = rules[String(Number(id))];
  if (!r) return null;
  if (Number(id) === 7) return [r.SMA_exit, r.trailing_update].filter(Boolean).join(' · ');
  return r.exit ?? r.long_exit ?? null;
}

// Python round(x, 2): correctly rounded, exact binary ties go to even (matches engine.trail_level).
export function pyRound2(x) {
  if (!Number.isFinite(x)) return null;
  const exact = Math.abs(x).toFixed(40);
  const [whole, frac = ''] = exact.split('.');
  const rest = frac.slice(2);
  if (/^50*$/.test(rest)) {
    const cents = BigInt(whole + frac.slice(0, 2));
    const even = cents % 2n === 0n ? cents : cents + 1n;
    const v = Number(even) / 100;
    return x < 0 ? -v : v;
  }
  return Number(x.toFixed(2));
}

// engine.py: trail_level(high_water) = round(TRAIL * high_water, 2).
export function trailLevel(highWater, fraction = TRAIL_FRACTION) {
  const h = Number(highWater);
  if (highWater === null || highWater === undefined || highWater === '' || !Number.isFinite(h) || h <= 0) return null;
  return pyRound2(fraction * h);
}

export const NO_TARGET = 'No fixed target';
export const RR_UNAVAILABLE = 'Risk : reward needs both a stop level and a defined profit target. No strategy defines a fixed target (target_defined = false), so no R:R is computed.';
