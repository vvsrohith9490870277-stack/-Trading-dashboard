"""Candidate-strategy registry (B3): shadow challengers 101-105.

Shadow / observation mode ONLY. Candidates never enter BASELINE_PARAMS,
adaptive_policy.json tiers, strategy_rules.json, or the adaptive_daily.py
spawn loop (which iterates ae.BASELINE_PARAMS) — so they can never spawn as
Track A/B arms, never place or queue orders, and never appear in Track A's
decision path. They are evaluated with live/candidate_shadow.py through the
same simulate_track_window (same frozen sizing and costs) via the optional
signal_evaluator hook, and their results are written only to the sidecar
adaptive.sqlite tables adaptive_candidate_*.

Rule text: docs/research/adaptive-support-plan.md §4 (exact rule text with
citations). All five are long-only, daily-close, rule-based swing strategies,
different from the frozen seven (01 Donchian/Turtle, 02 Supertrend, 03 RSI14
pullback in uptrend, 04 EMA20/50 cross with SMA200, 05 Bollinger reversion,
06 MACD trend, 07 52-week-high momentum). All rule numbers are strategy
parameters of this registry, not frozen policy.

Indicator constructions mirror the frozen strategy_indicators.py functions
(RSI Wilder smoothing as in strategy_3, Bollinger 20/2 standard deviation
with ddof=0 as in strategy_5); the frozen file itself is never edited.
Signals use only completed bars (no lookahead): every rolling window is
computed from bars up to and including the current close, exactly like the
frozen strategies.

Per-candidate notes / fallbacks (brief §6):
- C101 criterion 8 (RS rank >= 70) is derived_rs_rank (in-universe),
  NOT IBD's rating (Grok Q2 answer, plan §9): percentile (0-100) of each
  stock's 252-session return within the eligible universe on the same bar,
  computed from the bars already held. Missing history -> criterion 8 is
  not_evaluable (rs_rank_unavailable) for that stock only. Full 8-criterion
  template; no partial_template flag.
- C104 entry is +DI crossing above -DI AND ADX(14) > 25 (plan exact text).
  Wilder's extreme-point rule (enter only when price passes the crossover
  bar's high) is NOT applied — the daily-close harness enters at the close.
- C105 is an ADAPTATION of Jegadeesh & Titman (1993), whose rule is
  cross-sectional (rank all stocks, buy the top decile). The single-name
  version ranks the Track A universe by 12-1 return; it is flagged
  "adapted, not the paper's exact rule" everywhere. Rank is evaluated at
  monthly rebalances (first session of each month); the 63-session hold
  expiry and the top-30% rank exit are applied as specified, from the
  actual entry only (state-aware walk, no ghost expiries).
- No candidate has a fixed stop (exits are indicator-based, like the frozen
  seven). initial_stop is therefore None with a note; the B4 compare report
  uses the ATR(14)-based 1R reporting convention (plan §4.4).
"""
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Indicator helpers (mirror the frozen constructions, never edit them)
# ---------------------------------------------------------------------------

def _sma(close, n):
    return close.rolling(n).mean()


def _rsi_wilder(close, period):
    """RSI with Wilder smoothing, exactly as frozen strategy_3 builds RSI14
    (strategy_indicators.py:45-70), with the given period. Seed = mean of the
    first `period` bars' gains/losses."""
    c = close.astype(float)
    d = c.diff().to_numpy()
    up = np.maximum(d, 0.0)
    down = np.maximum(-d, 0.0)
    n = len(c)
    ag = np.full(n, np.nan)
    al = ag.copy()
    if n > period:
        ag[period] = up[1:period + 1].mean()
        al[period] = down[1:period + 1].mean()
        for i in range(period + 1, n):
            ag[i] = ((period - 1) * ag[i - 1] + up[i]) / period
            al[i] = ((period - 1) * al[i - 1] + down[i]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        rsi = 100.0 - 100.0 / (1.0 + ag / al)
    rsi = np.where(al == 0, 100.0, rsi)
    rsi = np.where((ag == 0) & (al > 0), 0.0, rsi)
    return pd.Series(rsi, index=close.index)


def _atr_wilder(high, low, close, period=14):
    """ATR with Wilder smoothing (same (n-1)/n recursion as _rsi_wilder)."""
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


def _wilder_smooth(values, period):
    """Wilder recursive average: seed = mean of the first `period` values,
    then prev*(n-1)/n + x/n (same construction as _rsi_wilder)."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    out = np.full(n, np.nan)
    if n > period:
        out[period] = np.nanmean(x[1:period + 1])
        for i in range(period + 1, n):
            out[i] = ((period - 1) * out[i - 1] + x[i]) / period
    return out


def _dmi_adx(high, low, close, period=14):
    """Wilder Directional Movement: +DI, -DI, ADX (New Concepts in Technical
    Trading Systems, 1978). UpMove = high - prior high, DownMove = prior low
    - low; only the larger counts, and only if positive."""
    h, l, c = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    n = len(c)
    up_move = np.r_[0.0, h[1:] - h[:-1]]
    down_move = np.r_[0.0, l[:-1] - l[1:]]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    if n > 0:
        tr[0] = h[0] - l[0]
    s_plus = _wilder_smooth(np.r_[np.nan, plus_dm[1:]], period) if n > 1 else plus_dm
    s_minus = _wilder_smooth(np.r_[np.nan, minus_dm[1:]], period) if n > 1 else minus_dm
    s_tr = _wilder_smooth(np.r_[np.nan, tr[1:]], period) if n > 1 else tr
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * s_plus / s_tr
        minus_di = 100.0 * s_minus / s_tr
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = _wilder_smooth(np.r_[np.nan, dx[1:]] if n > 1 else dx, period)
    return (pd.Series(plus_di, index=close.index), pd.Series(minus_di, index=close.index),
            pd.Series(adx, index=close.index))


def atr14_at_entry(df, entry_idx):
    """ATR(14) at the entry bar (paise are applied by the caller). Used for
    the B4 reporting 1R: 1R = qty x ATR(14) at entry."""
    atr = _atr_wilder(df['high'], df['low'], df['close'], 14)
    return float(atr.iloc[entry_idx]) if entry_idx < len(atr) else float('nan')


def _derived_rs_rank(universe):
    """derived_rs_rank (in-universe): percentile (0-100) of each stock's
    252-session return within the universe on the same bar (Grok Q2 answer,
    plan §9). NOT IBD's rating: it is computed from the bars already held
    (close / close.shift(252) - 1, ranked across the universe that day).
    Returns a frame indexed by trade_date with one column per symbol, or
    None when the universe is missing or shorter than 253 sessions. A stock
    with missing history gets NaN (criterion 8 not_evaluable,
    rs_rank_unavailable, for that stock only). Point-in-time safe: the rank
    at a date uses only closes up to that date."""
    if universe is None:
        return None
    closes = _universe_closes(universe)
    if closes is None or len(closes) < 253:
        return None
    ret252 = closes / closes.shift(252) - 1.0
    return ret252.rank(axis=1, ascending=True, pct=True, method='min') * 100.0


# ---------------------------------------------------------------------------
# C101 — Minervini Trend Template + breakout (Minervini 2013)
# ---------------------------------------------------------------------------

def _c101_entries(df, universe=None):
    g = df.copy()
    c = g['close'].astype(float)
    h = g['high'].astype(float)
    l = g['low'].astype(float)
    g['sma50'] = _sma(c, 50)
    g['sma150'] = _sma(c, 150)
    g['sma200'] = _sma(c, 200)
    g['high252'] = h.rolling(252).max()
    g['low252'] = l.rolling(252).min()
    # Criterion 8: derived_rs_rank (in-universe), NOT IBD's rating.
    # NaN when the stock's history is missing -> criterion 8 not_evaluable
    # (rs_rank_unavailable) for that stock only; the comparison below is
    # False for NaN, so no entry fires without a computable rank.
    rank_frame = _derived_rs_rank(universe)
    sym = str(df['symbol'].iloc[0]) if 'symbol' in df.columns and len(df) else None
    if rank_frame is not None and sym in rank_frame.columns:
        g['derived_rs_rank'] = rank_frame[sym].reindex(
            g['trade_date'].astype(str)).to_numpy()
    else:
        g['derived_rs_rank'] = np.nan
    # Criteria 1-8 (all must hold at the close).
    g['entry_condition'] = (
        (c > g['sma150']) & (c > g['sma200'])                      # 1
        & (g['sma150'] > g['sma200'])                              # 2
        & (g['sma200'] > g['sma200'].shift(20))                    # 3
        & (g['sma50'] > g['sma150']) & (g['sma50'] > g['sma200'])  # 4
        & (c > g['sma50'])                                         # 5
        & (c >= 1.30 * g['low252'])                                # 6
        & (c >= 0.75 * g['high252'])                               # 7
        & (g['derived_rs_rank'] >= 70)                             # 8
    ).fillna(False)
    return g


def _c101_exits(df):
    g = df.copy()
    c = g['close'].astype(float)
    g['sma50'] = _sma(c, 50)
    g['exit_condition'] = (c < g['sma50']).fillna(False)
    return g


# ---------------------------------------------------------------------------
# C102 — Connors RSI(2) in an uptrend (Connors & Alvarez 2008)
# ---------------------------------------------------------------------------

def _c102_entries(df):
    g = df.copy()
    c = g['close'].astype(float)
    g['sma200'] = _sma(c, 200)
    g['rsi2'] = _rsi_wilder(c, 2)
    g['entry_condition'] = ((c > g['sma200']) & (g['rsi2'] < 10)).fillna(False)
    return g


def _c102_exits(df):
    g = df.copy()
    c = g['close'].astype(float)
    g['sma5'] = _sma(c, 5)
    g['exit_condition'] = (c > g['sma5']).fillna(False)
    return g


# ---------------------------------------------------------------------------
# C103 — Bollinger squeeze breakout, Method I (Bollinger 2001)
# ---------------------------------------------------------------------------

def _bollinger(close, period=20, std=2.0):
    mid = _sma(close, period)
    sd = close.rolling(period).std(ddof=0)  # same convention as frozen strategy_5
    return mid, mid + std * sd, mid - std * sd


def _c103_entries(df):
    g = df.copy()
    c = g['close'].astype(float)
    mid, upper, lower = _bollinger(c, 20, 2.0)
    g['bb_middle'] = mid
    g['bb_upper'] = upper
    g['bb_lower'] = lower
    bw = (upper - lower) / mid
    g['bandwidth'] = bw
    bw_min_126 = bw.rolling(126, min_periods=1).min()  # lowest in the last 126 sessions (available history if shorter)
    # Squeeze: BandWidth at its lowest in 126 sessions, within the last 5 sessions.
    is_low = (bw == bw_min_126).to_numpy()
    n = len(g)
    since_low = np.full(n, 999)
    last = -999
    for i in range(n):
        if is_low[i]:
            last = i
        since_low[i] = i - last
    g['squeeze_flag'] = pd.Series(since_low <= 4, index=g.index).fillna(False)
    g['entry_condition'] = (g['squeeze_flag'] & (c > upper)).fillna(False)
    return g


def _c103_exits(df):
    g = df.copy()
    c = g['close'].astype(float)
    mid, _, _ = _bollinger(c, 20, 2.0)
    g['bb_middle'] = mid
    g['exit_condition'] = (c < mid).fillna(False)
    return g


# ---------------------------------------------------------------------------
# C104 — Wilder DMI / ADX (Wilder 1978)
# ---------------------------------------------------------------------------

def _c104_entries(df):
    g = df.copy()
    plus_di, minus_di, adx = _dmi_adx(g['high'], g['low'], g['close'], 14)
    g['plus_di'] = plus_di
    g['minus_di'] = minus_di
    g['adx'] = adx
    cross_up = (plus_di > minus_di) & (plus_di.shift() <= minus_di.shift())
    g['entry_condition'] = (cross_up & (adx > 25)).fillna(False)
    return g


def _c104_exits(df):
    g = df.copy()
    plus_di, minus_di, _ = _dmi_adx(g['high'], g['low'], g['close'], 14)
    g['plus_di'] = plus_di
    g['minus_di'] = minus_di
    cross_down = (minus_di > plus_di) & (minus_di.shift() <= plus_di.shift())
    g['exit_condition'] = cross_down.fillna(False)
    return g


# ---------------------------------------------------------------------------
# C105 — Jegadeesh & Titman 12-1 momentum, ADAPTED (Jegadeesh & Titman 1993)
# ---------------------------------------------------------------------------

def _universe_closes(universe):
    """Pivot a multi-symbol bars frame (symbol/trade_date/close) to
    close[trade_date, symbol]. The frame must be sorted by trade_date."""
    if universe is None:
        return None
    u = universe.copy()
    return u.pivot(index='trade_date', columns='symbol', values='close').sort_index()


def _rank_frame(closes):
    """12-1 return rank per day: return over sessions -252..-21 (skip the
    most recent 21 sessions). Returns (rank, n) frames; rank 1 = best."""
    if closes is None or len(closes) < 253:
        return None, None
    ret = closes.shift(21) / closes.shift(252) - 1.0
    rank = ret.rank(axis=1, ascending=False, method='min')
    n = ret.notna().sum(axis=1)
    return rank, n


def _c105_walk(df, universe=None):
    """State-aware monthly-rebalance walk for C105 (Grok Q4 answer, plan §9).

    Walks sessions in order; at a rebalance (first session of a calendar
    month): if not holding and in the top decile -> enter; if holding and
    out of the top 30% -> exit at that rebalance. If holding and the
    session is entry+63 -> exit (exact expiry, from the ACTUAL entry only).
    Expiries are therefore never generated from entry edges that never
    opened a position (no ghost expiries). Exits are applied before entries
    on the same bar, so a same-bar expiry + re-entry at one rebalance
    closes then re-opens (the sim processes exits before entries on the
    same bar). Returns (entry_condition, exit_condition) boolean Series.
    """
    entry = pd.Series(False, index=df.index)
    exit_cond = pd.Series(False, index=df.index)
    dates = df['trade_date'].astype(str).tolist()
    n = len(df)
    sym = str(df['symbol'].iloc[0]) if 'symbol' in df.columns and len(df) else None
    if universe is None or sym is None:
        return entry, exit_cond
    closes = _universe_closes(universe)
    rank, cnt = _rank_frame(closes)
    if rank is None or sym not in rank.columns:
        return entry, exit_cond
    r = rank[sym].reindex(dates)
    nn = cnt.reindex(dates)
    top = (r <= np.maximum(1, np.ceil(0.10 * nn)))
    top30 = (r <= np.maximum(1, np.ceil(0.30 * nn)))
    holding = False
    entry_idx = None
    for i in range(n):
        # Exact 63-session hold expiry, from the actual entry only.
        if holding and entry_idx is not None and i == entry_idx + 63:
            exit_cond.iloc[i] = True
            holding = False
            entry_idx = None
        # Monthly rebalance: first session of a calendar month.
        if i == 0 or dates[i][:7] != dates[i - 1][:7]:
            if holding and r.notna().iloc[i] and not bool(top30.iloc[i]):
                exit_cond.iloc[i] = True
                holding = False
                entry_idx = None
            if not holding and bool(top.iloc[i]):
                entry.iloc[i] = True
                holding = True
                entry_idx = i
    return entry, exit_cond


def _c105_entries(df, universe=None):
    g = df.copy()
    entry, _ = _c105_walk(df, universe)
    g['entry_condition'] = entry
    return g


def _c105_exits(df, universe=None, entry_dates=None):
    # entry_dates is accepted for signature compatibility but ignored: the
    # walk derives expiries from its own actual entries, so no expiry can
    # come from an edge that never opened a position (ghost expiry).
    g = df.copy()
    _, exit_cond = _c105_walk(df, universe)
    g['exit_condition'] = exit_cond
    return g


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_NO_STOP = {'type': 'none_indicator_exit',
            'definition': 'No fixed stop; exits are indicator-based (see exits). '
                          'Reporting R uses the ATR(14)-based 1R convention (plan §4.4).'}

CANDIDATE_STRATEGIES = {
    101: {
        'id': 101, 'name': 'Minervini Trend Template + breakout',
        'source': 'Minervini, Trade Like a Stock Market Wizard (McGraw-Hill, 2013), Trend Template (8 criteria)',
        'tier': 'trend', 'warm_start_days': 252,
        'params': {'sma_fast': 50, 'sma_mid': 150, 'sma_slow': 200, 'sma200_trend_lookback': 20,
                   'above_52w_low_pct': 30, 'within_52w_high_pct': 25, 'breakout_lookback': 20,
                   'rs_rank_min': 70},
        'entries': _c101_entries, 'exits': _c101_exits, 'initial_stop': _NO_STOP,
        'rs_rank_label': 'derived_rs_rank (in-universe)',
        'notes': 'Full 8-criterion template. Criterion 8 uses derived_rs_rank '
                 '(in-universe), NOT IBD\'s rating: percentile (0-100) of the '
                 '252-session return within the eligible universe on the same '
                 'bar; missing history -> criterion 8 not_evaluable '
                 '(rs_rank_unavailable) for that stock only.',
    },
    102: {
        'id': 102, 'name': 'Connors RSI(2) in uptrend',
        'source': 'Connors & Alvarez, Short Term Trading Strategies That Work (2008), 2-period RSI',
        'tier': 'fast_swing', 'warm_start_days': 200,
        'params': {'rsi_period': 2, 'rsi_entry': 10.0, 'sma_trend': 200, 'sma_exit': 5},
        'entries': _c102_entries, 'exits': _c102_exits, 'initial_stop': _NO_STOP,
        'notes': 'No stop in the published rule; risk is carried by position size and the fast SMA5 exit.',
    },
    103: {
        'id': 103, 'name': 'Bollinger squeeze breakout (Method I)',
        'source': 'Bollinger, Bollinger on Bollinger Bands (McGraw-Hill, 2001), %b / BandWidth / Squeeze',
        'tier': 'fast_swing', 'warm_start_days': 126,
        'params': {'bb_period': 20, 'bb_std': 2.0, 'squeeze_lookback': 126, 'squeeze_recent': 5},
        'entries': _c103_entries, 'exits': _c103_exits, 'initial_stop': _NO_STOP,
        'notes': 'Squeeze = BandWidth lowest in 126 sessions within the last 5 sessions; breakout gives direction.',
    },
    104: {
        'id': 104, 'name': 'Wilder ADX/DMI trend',
        'source': "Wilder, New Concepts in Technical Trading Systems (1978), Directional Movement System",
        'tier': 'trend', 'warm_start_days': 200,
        'params': {'adx_period': 14, 'adx_entry': 25.0},
        'entries': _c104_entries, 'exits': _c104_exits, 'initial_stop': _NO_STOP,
        'notes': 'Entry is +DI crossing above -DI with ADX(14) > 25 (plan text). '
                 "Wilder's extreme-point rule is NOT applied (daily-close harness).",
    },
    105: {
        'id': 105, 'name': '12-1 cross-sectional momentum (adapted)',
        'source': 'Jegadeesh & Titman, "Returns to Buying Winners and Selling Losers", Journal of Finance 48(1), 65-91 (1993)',
        'tier': 'trend', 'warm_start_days': 252,
        'params': {'formation_lookback': 252, 'skip_sessions': 21, 'top_decile': 0.10,
                   'exit_top_share': 0.30, 'hold_sessions': 63},
        'entries': _c105_entries, 'exits': _c105_exits, 'initial_stop': _NO_STOP,
        'adapted': True,
        'notes': "ADAPTED, not the paper's exact rule: the paper is cross-sectional "
                 '(rank all stocks, buy the top decile); this single-name version ranks '
                 'the Track A universe by 12-1 return. Rank is evaluated at monthly '
                 'rebalances (first session of each month) in a state-aware walk; '
                 'the 63-session hold expiry comes from the actual entry only. '
                 'Needs the full universe panel.',
    },
}


def candidate_signals(strategy, df, universe=None, entry_dates=None):
    """Entry + exit conditions for one candidate on one symbol's bars.

    Returns df with entry_condition / exit_condition (same convention as
    evaluate_parameterized_strategy). universe (multi-symbol bars frame) is
    required for 101 (derived_rs_rank) and 105 (cross-sectional rank);
    entry_dates (entry fill dates for this symbol) enables the 105
    hold-expiry exit. Signals use only completed bars (no lookahead).
    """
    s = int(strategy)
    if s not in CANDIDATE_STRATEGIES:
        raise ValueError(f'unknown_candidate:{strategy}')
    c = CANDIDATE_STRATEGIES[s]
    if s == 105:
        en = c['entries'](df, universe)
        ex = c['exits'](df, universe, entry_dates)
    elif s == 101:
        en = c['entries'](df, universe)
        ex = c['exits'](df)
    else:
        en = c['entries'](df)
        ex = c['exits'](df)
    out = df.copy()
    out['entry_condition'] = en['entry_condition'].fillna(False).astype(bool)
    out['exit_condition'] = ex['exit_condition'].fillna(False).astype(bool)
    if 'derived_rs_rank' in en.columns:
        out['derived_rs_rank'] = en['derived_rs_rank']
    return out


def registry_summary():
    """JSON-safe summary of the registry (no callables)."""
    return {s: {k: v for k, v in c.items() if k not in ('entries', 'exits')}
            for s, c in sorted(CANDIDATE_STRATEGIES.items())}
