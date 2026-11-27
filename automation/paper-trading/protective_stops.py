"""Protective stop for S1-S6 (C1, Grok Decision 1, approved by VVS).

Shadow only: the stop is evaluated in engine._scan and recorded in
paper_shadow_signals; it never creates a live signal, fill or exit.
Live rules change only after VVS approves and Grok reviews shadow results.

The rule set lives in strategy_rules_stops_v1.json (NOT in the frozen
strategy_rules.json): stop = max(entry - 2*ATR(14) at entry, entry * 0.90),
ATR taken at the entry bar and frozen for the trade (not trailing),
evaluated on the daily close. S7 is excluded: it already has its 10% trail.
The ATR convention is C107's Wilder ATR (adaptive_candidates._atr_wilder).
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
_RULES = json.loads((ROOT / 'strategy_rules_stops_v1.json').read_text())
VERSION = str(_RULES['version'])
STOP_STRATEGIES = frozenset(int(s) for s in _RULES['applies_to_strategies'])
ATR_PERIOD = int(_RULES['stop_atr_period'])
ATR_MULT = float(_RULES['stop_atr_mult'])
MAX_LOSS_FRACTION = float(_RULES['stop_max_loss_fraction'])
# Fail closed on a malformed rule set: S7 must never get this stop, the
# multiplier must be positive, and the cap must be a fraction.
if not STOP_STRATEGIES or 7 in STOP_STRATEGIES or ATR_PERIOD < 1 or ATR_MULT <= 0 or not 0 < MAX_LOSS_FRACTION < 1 or not _RULES.get('shadow'):
    raise SystemExit('PROTECTIVE_STOP_RULE_SET_INVALID')


def atr_wilder(high, low, close, period=14):
    """Wilder ATR — the same convention as adaptive_candidates._atr_wilder:
    TR[0] = high-low; seed = mean(TR[1:period+1]) at index `period`; then
    ATR[i] = ((period-1)*ATR[i-1] + TR[i]) / period. First valid at index
    `period`; earlier values are NaN."""
    h, l, c = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    n = len(c)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    if n > 0:
        tr[0] = h[0] - l[0]
    atr = np.full(n, np.nan)
    if n > period:
        atr[period] = tr[1:period + 1].mean()
        for i in range(period + 1, n):
            atr[i] = ((period - 1) * atr[i - 1] + tr[i]) / period
    return pd.Series(atr, index=close.index)


def atr14_at_entry(frame, entry_date):
    """ATR(14) at the entry bar, from a frame with trade_date/high/low/close
    columns. Returns None when not evaluable (entry bar missing, fewer than
    14 bars before it, or a non-finite / non-positive value) — the caller
    then skips the stop (fail closed: no shadow SELL without a known ATR)."""
    if frame is None or len(frame) == 0:
        return None
    try:
        pos = frame['trade_date'].astype(str).tolist().index(str(entry_date))
    except (ValueError, KeyError, AttributeError):
        return None
    atr = atr_wilder(frame['high'], frame['low'], frame['close'], ATR_PERIOD)
    if pos >= len(atr):
        return None
    try:
        v = float(atr.iloc[pos])
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def stop_level(entry_price, atr14):
    """stop = max(entry - ATR_MULT*ATR(14) at entry, entry*(1-MAX_LOSS_FRACTION)),
    rounded to paise. The cap is the tighter bound when 2*ATR exceeds 10% of
    entry. Returns None when inputs are missing or non-positive."""
    if entry_price is None or atr14 is None:
        return None
    try:
        entry = float(entry_price)
        atr = float(atr14)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry) and math.isfinite(atr)) or entry <= 0 or atr <= 0:
        return None
    return round(max(entry - ATR_MULT * atr, entry * (1.0 - MAX_LOSS_FRACTION)), 2)
