"""Candidate-strategy registry (B3): shadow challengers 101-108 and 109.

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
- C106 is the SECTOR-ADJUSTED variant of the same momentum family (Ceta
  Research, sector-adjusted momentum, India 2000-2025): rank by the 12-1
  return MINUS the stock's sector average 12-1 return, buy the top 30.
  It is a different ranking from C105's whole-market rank, so it is a new
  candidate, not a duplicate. It needs the full universe panel AND a
  symbol->sector map (explicit sector_map argument, or a 'sector' /
  'industry' column on the universe frame); with no usable map it enters
  nothing. Monthly rebalance; exit at the next rebalance when the stock
  drops out of the top 30. No 63-session expiry: the published
  sector-adjusted rule has none.
- C107 is the first candidate with a fixed stop: a protective stop at entry
  price − 2 x ATR(14) at entry (risk plan MUSE-PLAN-adaptive-risk-fixes.md
  §1), on top of S4's indicator exit. The other candidates have no fixed
  stop (exits are indicator-based, like the frozen seven); their
  initial_stop is therefore None with a note. The B4 compare report uses
  the 2x ATR(14)-based 1R reporting convention (plan §4.4, Grok Q3:
  1R = qty x 2 x ATR(14)) — for C107 that 1R IS the stop distance.
- C107 mirrors frozen S4 (strategy_4: EMA20/50 cross, entry only above
  SMA200, exit on the cross-down) and adds only the protective stop.
  Entry, exit and stop are computed by a state-aware walk: the stop level
  is set at the entry bar from that bar's ATR(14) and frozen for the trade
  (not trailing), and is evaluated on the daily close, the same convention
  as the frozen exits.
- C108 is the TIME-STOP variant of S4 (risk plan §2): it mirrors S4
  (EMA20/50 cross, entry only above SMA200) and exits after 20 sessions
  if the trade is not profitable (close at or below the entry price),
  or on the S4 cross-down, whichever comes first. The 20-session value
  is the plan's suggested value, not a policy value: it is written down
  here and in the registry params as time_stop_sessions = 20. There is
  NO protective price stop in C108 (a later candidate may combine the
  stop and the time stop). "Not profitable" is judged on the daily close
  only (close <= entry price); charges/slippage are applied by the
  simulator at fill, not in this signal-level check.
- C109 (risk plan §3) is the first candidate with ATR-based sizing: it
  mirrors S4 exactly for entry/exit and sizes each trade from the ATR(14)
  at entry, qty = risk_budget / (2 x ATR(14)), capped so the position
  value never exceeds the Rs 50,000 per-trade maximum. The risk budget is
  Rs 5,000 = 1% of the Rs 5,00,000 per-strategy sleeve capital: a choice,
  not a policy value, flagged for Senior/Grok to confirm (change the one
  registry param risk_budget_inr to change it, no code change). With the
  Rs 50,000 value cap binding, a very stable stock's risk per trade is
  below the budget; when the ATR formula binds, risk per trade is the
  budget (1R = Rs 5,000 at entry)."""
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
    the B4 reporting 1R: 1R = qty x 2 x ATR(14) at entry."""
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
# C106 — Sector-adjusted 12-1 momentum, top 30 (Ceta Research, India)
# ---------------------------------------------------------------------------

def _sector_map_from_universe(universe, sector_map=None):
    """Symbol -> sector map for C106. An explicit sector_map wins; else a
    'sector' (or 'industry') column on the universe frame is used (first
    value per symbol); else None (C106 then enters nothing)."""
    if sector_map:
        return {str(k): str(v) for k, v in dict(sector_map).items()}
    if universe is None:
        return None
    u = universe
    col = None
    for cand in ('sector', 'industry'):
        if cand in u.columns:
            col = cand
            break
    if col is None:
        return None
    out = {}
    for sym, sdf in u.groupby('symbol'):
        vals = sdf[col].dropna()
        if len(vals):
            out[str(sym)] = str(vals.iloc[0])
    return out or None


def _sector_adjusted_frame(closes, sector_map):
    """12-1 return minus the stock's sector average 12-1 return, per day.
    Same 12-1 window as C105 (sessions -252..-21). A sector's average is
    the mean over its members with a computable return that day; a stock
    with no sector, or no computable return, gets NaN."""
    if closes is None or len(closes) < 253 or not sector_map:
        return None
    ret = closes.shift(21) / closes.shift(252) - 1.0
    sectors = {}
    for sym in closes.columns:
        sec = sector_map.get(str(sym))
        if sec is not None:
            sectors.setdefault(sec, []).append(sym)
    adj = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    for sec, syms in sectors.items():
        mean = ret[syms].mean(axis=1)
        for sym in syms:
            adj[sym] = ret[sym] - mean
    return adj


def _c106_walk(df, universe=None, sector_map=None):
    """State-aware monthly-rebalance walk for C106 (same rebalance
    convention as C105: first session of a calendar month).

    At a rebalance: if not holding and the stock's sector-adjusted 12-1
    return ranks in the top 30 of the universe that day -> enter; if
    holding and it ranks outside the top 30 -> exit at that rebalance.
    Exits are applied before entries on the same bar. No hold expiry:
    the published sector-adjusted rule exits only on dropping out of the
    top 30. Rank uses method='min' (ties share the best rank), like C105.
    """
    entry = pd.Series(False, index=df.index)
    exit_cond = pd.Series(False, index=df.index)
    dates = df['trade_date'].astype(str).tolist()
    n = len(df)
    sym = str(df['symbol'].iloc[0]) if 'symbol' in df.columns and len(df) else None
    if universe is None or sym is None:
        return entry, exit_cond
    smap = _sector_map_from_universe(universe, sector_map)
    if not smap or sym not in smap:
        return entry, exit_cond
    closes = _universe_closes(universe)
    adj = _sector_adjusted_frame(closes, smap)
    if adj is None or sym not in adj.columns:
        return entry, exit_cond
    rank = adj.rank(axis=1, ascending=False, method='min')
    r = rank[sym].reindex(dates)
    top = (r <= 30)
    holding = False
    for i in range(n):
        # Monthly rebalance: first session of a calendar month.
        if i == 0 or dates[i][:7] != dates[i - 1][:7]:
            if holding and (r.isna().iloc[i] or not bool(top.iloc[i])):
                exit_cond.iloc[i] = True
                holding = False
            if not holding and bool(top.iloc[i]):
                entry.iloc[i] = True
                holding = True
    return entry, exit_cond


def _c106_entries(df, universe=None, sector_map=None):
    g = df.copy()
    entry, _ = _c106_walk(df, universe, sector_map)
    g['entry_condition'] = entry
    return g


def _c106_exits(df, universe=None, sector_map=None):
    g = df.copy()
    _, exit_cond = _c106_walk(df, universe, sector_map)
    g['exit_condition'] = exit_cond
    return g


# C108 — Dual MA crossover (S4) + 20-session time stop (risk plan §2)
# ---------------------------------------------------------------------------



def _c108_walk(df):
    """State-aware walk for C108: S4 entries; exit on the time stop OR the
    S4 cross-down, whichever comes first.

    Time stop: once a trade has been held 20 sessions (sessions_held =
    current bar index - entry bar index >= 20), exit at the close if the
    trade is not profitable, i.e. close <= entry price. A profitable trade
    (close > entry price) is NOT time-stopped and holds until the S4
    cross-down. There is no protective price stop here. The exit is
    evaluated on the daily close, the same convention as the frozen exits
    (signals at the close, fills at the next open). Returns (entry,
    exit_cond, sessions_held, entry_price) Series; sessions_held /
    entry_price hold the values in force on each bar while holding (0 /
    NaN otherwise). No lookahead: every value at a bar uses only bars up
    to that bar. Exits are applied before entries on the same bar, so a
    time-stop / cross-down exit and a fresh entry never coincide on one
    bar."""
    g = _s4_signals(df)
    n = len(g)
    entry = pd.Series(False, index=df.index)
    exit_cond = pd.Series(False, index=df.index)
    sessions_held = pd.Series(0, index=df.index, dtype=int)
    entry_price = pd.Series(np.nan, index=df.index, dtype=float)
    entry_cond = (g['cross_up'] & (g['close'].astype(float) > g['sma200'])).fillna(False)
    cross_down = g['cross_down'].fillna(False)
    close = g['close'].astype(float)
    holding = False
    entry_i = None
    entry_px = None
    for i in range(n):
        if holding:
            held = i - entry_i
            sessions_held.iloc[i] = held
            entry_price.iloc[i] = entry_px
            if bool(cross_down.iloc[i]) or (held >= 20 and close.iloc[i] <= entry_px):
                exit_cond.iloc[i] = True
                holding = False
                entry_i = None
                entry_px = None
        if not holding and bool(entry_cond.iloc[i]):
            entry.iloc[i] = True
            holding = True
            entry_i = i
            entry_px = float(close.iloc[i])
            sessions_held.iloc[i] = 0
            entry_price.iloc[i] = entry_px
    return entry, exit_cond, sessions_held, entry_price


def _c108_entries(df):
    g = _s4_signals(df)
    entry, _, _, _ = _c108_walk(df)
    g['entry_condition'] = entry
    return g


def _c108_exits(df):
    g = _s4_signals(df)
    _, exit_cond, sessions_held, entry_price = _c108_walk(df)
    g['exit_condition'] = exit_cond
    g['sessions_held'] = sessions_held
    g['entry_price'] = entry_price
    return g


# ---------------------------------------------------------------------------
# C107 — Dual MA crossover (S4) + 2x ATR(14) protective stop (risk plan §1)
# ---------------------------------------------------------------------------

def _c107_walk(df):
    """State-aware walk for C107: S4 entries; exit on the protective stop
    OR the S4 cross-down, whichever comes first.

    Stop = entry close − 2 × ATR(14) at the entry bar (the 1R convention:
    1R = qty × 2 × ATR(14) at entry). The ATR is taken at entry and frozen
    for the trade (not trailing). The stop is evaluated on the daily close,
    the same convention as the frozen exits (signals at the close, fills at
    the next open). Returns (entry, exit_cond, stop_level, entry_price,
    atr_entry) Series; stop_level / entry_price / atr_entry hold the stop
    in force on each bar while holding (NaN otherwise). No lookahead: every
    value at a bar uses only bars up to that bar. Exits are applied before
    entries on the same bar, so a stop/cross-down exit and a fresh entry
    never coincide on one bar."""
    g = _s4_signals(df)
    n = len(g)
    entry = pd.Series(False, index=df.index)
    exit_cond = pd.Series(False, index=df.index)
    stop_level = pd.Series(np.nan, index=df.index, dtype=float)
    entry_price = pd.Series(np.nan, index=df.index, dtype=float)
    atr_entry = pd.Series(np.nan, index=df.index, dtype=float)
    atr = _atr_wilder(g['high'], g['low'], g['close'], 14)
    entry_cond = (g['cross_up'] & (g['close'].astype(float) > g['sma200'])).fillna(False)
    cross_down = g['cross_down'].fillna(False)
    close = g['close'].astype(float)
    holding = False
    stop = None
    entry_px = None
    atr_e = None
    for i in range(n):
        if holding:
            stop_level.iloc[i] = stop
            entry_price.iloc[i] = entry_px
            atr_entry.iloc[i] = atr_e
            if bool(cross_down.iloc[i]) or close.iloc[i] <= stop:
                exit_cond.iloc[i] = True
                holding = False
                stop = None
        if not holding and bool(entry_cond.iloc[i]):
            entry.iloc[i] = True
            holding = True
            entry_px = float(close.iloc[i])
            atr_e = float(atr.iloc[i])
            stop = entry_px - 2.0 * atr_e
            stop_level.iloc[i] = stop
            entry_price.iloc[i] = entry_px
            atr_entry.iloc[i] = atr_e
    return entry, exit_cond, stop_level, entry_price, atr_entry


def _c107_entries(df):
    g = _s4_signals(df)
    entry, _, _, _, _ = _c107_walk(df)
    g['entry_condition'] = entry
    return g


def _c107_exits(df):
    g = _s4_signals(df)
    _, exit_cond, stop_level, entry_price, atr_entry = _c107_walk(df)
    g['exit_condition'] = exit_cond
    g['stop_level'] = stop_level
    g['entry_price'] = entry_price
    g['atr14_at_entry'] = atr_entry
    return g

# ---------------------------------------------------------------------------
# C109 — Dual MA crossover (S4) + ATR-based position sizing (risk plan §3)
# ---------------------------------------------------------------------------

# Risk budget per trade (INR): 1% of the Rs 5,00,000 per-strategy sleeve
# capital. A CHOICE, not a policy value — flagged for Senior/Grok to
# confirm. Change only this number (registry param risk_budget_inr) to
# change it; no code change. The Rs 50,000 per-trade maximum is the
# existing TARGET_NOTIONAL_INR (capital policy), reused as the value cap.
RISK_BUDGET_INR = 5000
MAX_NOTIONAL_INR = 50000


def _s4_signals(df):
    """S4 (frozen strategy_4) indicators, mirrored exactly: EMA20/EMA50
    cross, entry only above SMA200, exit on the cross-down. The frozen file
    is never edited; the formulas below are the same ones."""
    g = df.copy()
    c = g['close'].astype(float)
    g['ema20'] = c.ewm(span=20, adjust=False).mean()
    g['ema50'] = c.ewm(span=50, adjust=False).mean()
    g['prior_ema20'] = g['ema20'].shift()
    g['prior_ema50'] = g['ema50'].shift()
    g['sma200'] = c.rolling(200).mean()
    g['cross_up'] = (g['ema20'] > g['ema50']) & (g['prior_ema20'] <= g['prior_ema50'])
    g['cross_down'] = (g['ema20'] < g['ema50']) & (g['prior_ema20'] >= g['prior_ema50'])
    return g


def _c109_qty(entry_close, atr14, risk_budget_inr=RISK_BUDGET_INR,
              max_notional_inr=MAX_NOTIONAL_INR):
    """ATR-based shares for one entry: qty = risk_budget / (2 x ATR(14)),
    capped so qty x entry_close never exceeds the per-trade maximum.
    Returns 0 (no entry) when ATR or price is missing, zero, or negative,
    or when even one share would breach the maximum. Whole shares only
    (floor); risk per trade is then qty x 2 x ATR(14) — the budget when
    the ATR formula binds, less when the value cap binds."""
    try:
        atr = float(atr14)
        px = float(entry_close)
    except (TypeError, ValueError):
        return 0
    if not (atr > 0) or not (px > 0):
        return 0
    import math
    qty_atr = math.floor(float(risk_budget_inr) / (2.0 * atr))
    qty_cap = math.floor(float(max_notional_inr) / px)
    qty = min(qty_atr, qty_cap)
    return int(qty) if qty >= 1 else 0


def _c109_walk(df):
    """State-aware walk for C109: S4 entries/exits, ATR-based sizing.

    Entry: S4 cross-up above SMA200, exactly as frozen strategy_4.
    Exit: S4 cross-down, exactly as frozen strategy_4 (no stop added).
    Sizing: at each entry bar, qty = _c109_qty(entry close, ATR(14) at
    that bar) — the ATR is taken at entry and frozen for the trade.
    The risk budget and per-trade maximum come from candidate 109's
    registry params (single source of truth): the walk passes them to
    _c109_qty explicitly, so changing the registry changes the sizing.
    Exits are applied before entries on the same bar, so an exit and a
    fresh entry never coincide on one bar. No lookahead: every value at
    a bar uses only bars up to that bar. Returns (entry, exit_cond, qty,
    atr_entry) Series; qty / atr_entry hold the sized shares and ATR in
    force on each bar while holding (0 / NaN otherwise)."""
    p109 = CANDIDATE_STRATEGIES[109]['params']
    g = _s4_signals(df)
    n = len(g)
    entry = pd.Series(False, index=df.index)
    exit_cond = pd.Series(False, index=df.index)
    qty = pd.Series(0, index=df.index, dtype=int)
    atr_entry = pd.Series(np.nan, index=df.index, dtype=float)
    atr = _atr_wilder(g['high'], g['low'], g['close'], 14)
    entry_cond = (g['cross_up'] & (g['close'].astype(float) > g['sma200'])).fillna(False)
    cross_down = g['cross_down'].fillna(False)
    close = g['close'].astype(float)
    holding = False
    qty_h = 0
    atr_e = None
    for i in range(n):
        if holding:
            qty.iloc[i] = qty_h
            atr_entry.iloc[i] = atr_e
            if bool(cross_down.iloc[i]):
                exit_cond.iloc[i] = True
                holding = False
                qty_h = 0
        if not holding and bool(entry_cond.iloc[i]):
            q = _c109_qty(float(close.iloc[i]), float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else float('nan'),
                          risk_budget_inr=p109['risk_budget_inr'], max_notional_inr=p109['max_notional_inr'])
            if q >= 1:
                entry.iloc[i] = True
                holding = True
                qty_h = q
                atr_e = float(atr.iloc[i])
                qty.iloc[i] = q
                atr_entry.iloc[i] = atr_e
    return entry, exit_cond, qty, atr_entry


def _c109_entries(df):
    g = _s4_signals(df)
    entry, _, qty, atr_e = _c109_walk(df)
    g['entry_condition'] = entry
    g['qty'] = qty
    g['atr14_at_entry'] = atr_e
    return g


def _c109_exits(df):
    g = _s4_signals(df)
    _, exit_cond, _, _ = _c109_walk(df)
    g['exit_condition'] = exit_cond
    return g


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_NO_STOP = {'type': 'none_indicator_exit',
            'definition': 'No fixed stop; exits are indicator-based (see exits). '
                          'Reporting R uses the 2x ATR(14)-based 1R convention (plan §4.4, Grok Q3).'}

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
    106: {
        'id': 106, 'name': 'Sector-adjusted 12-1 momentum (top 30)',
        'source': 'Ceta Research, "Sector-adjusted momentum, India (BSE/NSE, 2000-2025)"; '
                  'Jegadeesh & Titman, "Returns to Buying Winners and Selling Losers", '
                  'Journal of Finance 48(1), 65-91 (1993)',
        'tier': 'trend', 'warm_start_days': 252,
        'params': {'formation_lookback': 252, 'skip_sessions': 21, 'top_n': 30,
                   'rebalance': 'monthly'},
        'entries': _c106_entries, 'exits': _c106_exits, 'initial_stop': _NO_STOP,
        'notes': 'Sector-adjusted variant of 12-1 momentum: rank by the 12-1 return '
                 'MINUS the stock\'s sector average 12-1 return; buy the top 30 at '
                 'each monthly rebalance, exit at the next rebalance when the stock '
                 'drops out of the top 30. No 63-session expiry (the published '
                 'sector-adjusted rule has none). Different ranking from C105 '
                 '(whole-market rank), so not a duplicate of C105. Needs the full '
                 'universe panel AND a symbol->sector map (explicit sector_map, or '
                 'a sector/industry column on the universe frame); with no usable '
                 'map it enters nothing.',
    },
    107: {
        'id': 107, 'name': 'Dual MA crossover (S4) + 2x ATR(14) protective stop',
        'source': 'MUSE-PLAN-adaptive-risk-fixes.md §1 (risk plan, 2026-11-22); '
                  'S4 = frozen strategy_4 (EMA20/50 cross with SMA200)',
        'tier': 'trend', 'warm_start_days': 200,
        'params': {'ema_fast': 20, 'ema_slow': 50, 'sma_trend': 200,
                   'atr_period': 14, 'stop_atr_mult': 2.0},
        'entries': _c107_entries, 'exits': _c107_exits,
        'initial_stop': {'type': 'protective_stop',
                         'definition': 'Exit at entry price − 2 × ATR(14) at entry, '
                                       'or on the S4 cross-down, whichever comes first. '
                                       '1R = qty × 2 × ATR(14) at entry.'},
        'notes': 'Mirrors S4 exactly (EMA20/50 cross-up above SMA200 for entry, '
                 'cross-down for exit) plus a protective stop: entry close − 2 × '
                 'ATR(14) at the entry bar. The ATR is taken at entry and frozen '
                 'for the trade (not trailing); the stop is evaluated on the daily '
                 'close, the same convention as the frozen exits. First candidate '
                 'with a fixed stop; the other candidates have no fixed stop.',
    },
    108: {
        'id': 108, 'name': 'Dual MA crossover (S4) + 20-session time stop (not profitable)',
        'source': 'MUSE-PLAN-adaptive-risk-fixes.md §2 (risk plan, 2026-11-22); '
                  'S4 = frozen strategy_4 (EMA20/50 cross with SMA200)',
        'tier': 'trend', 'warm_start_days': 200,
        'params': {'ema_fast': 20, 'ema_slow': 50, 'sma_trend': 200,
                   'time_stop_sessions': 20},
        'entries': _c108_entries, 'exits': _c108_exits,
        'initial_stop': _NO_STOP,
        'notes': 'Mirrors S4 exactly (EMA20/50 cross-up above SMA200 for entry, '
                 'cross-down for exit) plus a time stop: exit after 20 sessions '
                 'if the trade is not profitable (close <= entry price), or on '
                 'the S4 cross-down, whichever comes first. The 20-session value '
                 'is the plan\'s suggested value, not a policy value. No '
                 'protective price stop in C108 (a later candidate may combine '
                 'the stop and the time stop). "Not profitable" is judged on the '
                 'daily close only; charges/slippage are applied by the '
                 'simulator at fill, not in this signal-level check.',
    },
    109: {
        'id': 109, 'name': 'Dual MA crossover (S4) + ATR-based position sizing',
        'source': 'MUSE-PLAN-adaptive-risk-fixes.md §3 (risk plan, 2026-11-22); '
                  'S4 = frozen strategy_4 (EMA20/50 cross with SMA200)',
        'tier': 'trend', 'warm_start_days': 200,
        'params': {'ema_fast': 20, 'ema_slow': 50, 'sma_trend': 200,
                   'atr_period': 14, 'risk_budget_inr': 5000,
                   'max_notional_inr': 50000},
        'entries': _c109_entries, 'exits': _c109_exits, 'initial_stop': _NO_STOP,
        'notes': 'Mirrors S4 exactly (EMA20/50 cross-up above SMA200 for entry, '
                 'cross-down for exit) plus ATR-based sizing: qty = risk budget '
                 '/ (2 x ATR(14) at entry), capped so qty x entry price never '
                 'exceeds Rs 50,000 per trade. Risk budget Rs 5,000 = 1% of the '
                 'Rs 5,00,000 sleeve capital — a CHOICE, not a policy value, '
                 'flagged for Senior/Grok to confirm. The Rs 50,000 per-trade '
                 'maximum and the Rs 5 lakh per-strategy cap do not change. '
                 'Risk per trade is the budget when the ATR formula binds, '
                 'less when the value cap binds (very stable stocks). First '
                 'candidate with ATR-based sizing; the other candidates use '
                 'the frozen Rs 50,000 fixed sizing.',    },
}


def candidate_signals(strategy, df, universe=None, entry_dates=None, sector_map=None):
    """Entry + exit conditions for one candidate on one symbol's bars.

    Returns df with entry_condition / exit_condition (same convention as
    evaluate_parameterized_strategy). universe (multi-symbol bars frame) is
    required for 101 (derived_rs_rank), 105 (cross-sectional rank) and 106
    (sector-adjusted rank); entry_dates (entry fill dates for this symbol)
    enables the 105 hold-expiry exit; sector_map (symbol -> sector) is
    required for 106, or a 'sector'/'industry' column on the universe
    frame. 107 is single-symbol: its exit walk tracks the entry price and
    the ATR(14) at entry from the symbol's own bars. Signals use only
    completed bars (no lookahead).
    """
    s = int(strategy)
    if s not in CANDIDATE_STRATEGIES:
        raise ValueError(f'unknown_candidate:{strategy}')
    c = CANDIDATE_STRATEGIES[s]
    if s == 105:
        en = c['entries'](df, universe)
        ex = c['exits'](df, universe, entry_dates)
    elif s == 106:
        en = c['entries'](df, universe, sector_map)
        ex = c['exits'](df, universe, sector_map)
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
    if 'qty' in en.columns:
        # C109 ATR-based sizing: shares for the entry bar (0 = no entry).
        # The shadow simulator reads this column for strategy 109 only.
        out['qty'] = en['qty'].fillna(0).astype(int)
    if 'atr14_at_entry' in en.columns:
        out['atr14_at_entry'] = en['atr14_at_entry']
    return out


def registry_summary():
    """JSON-safe summary of the registry (no callables)."""
    return {s: {k: v for k, v in c.items() if k not in ('entries', 'exits')}
            for s, c in sorted(CANDIDATE_STRATEGIES.items())}
