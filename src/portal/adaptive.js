// Pure helpers for the Adaptive Engine screen (src/portal/AdaptiveEngine.jsx).
// Nothing here invents a number: every metric is read from the data files the
// screen loads (/data/historical.json for the main 7, /data/adaptive-compare.json
// for the candidates, /data/adaptive-candidates.json for the registry facts).
// A missing value is null and the screen shows "not available", never 0.
export const NOT_AVAILABLE = 'not available';
export const MAIN_IDS = [1, 2, 3, 4, 5, 6, 7];
export const CANDIDATE_IDS = [101, 102, 103, 104, 105, 106];
// The compare's own floor: below this a sample is labelled insufficient_sample.
export const MIN_TRADES_FOR_SAMPLE = 30;

// One row of the compare.json `strategies` (or historical_replay.strategies) map.
export function compareRow(compare, id) {
  if (!compare) return null;
  const merged = compare.strategies?.[String(id)] ?? null;
  const replay = compare.historical_replay?.strategies?.[String(id)] ?? null;
  return merged ?? replay ?? null;
}

// Was this candidate run in the synthetic fixture that produced compare.json?
// The provenance lists the strategies the shadow run actually ran; a candidate
// not in that list was never run, which is different from running with 0 trades.
export function candidateRunState(compare, id) {
  const ran = compare?._provenance?.strategies_run ?? null;
  if (ran === null) return 'unknown';
  return ran.map(String).includes(String(id)) ? 'ran' : 'not_run';
}

// Candidate table rows for the MAIN table (live metrics only). There is no live
// compare output for 101-106 in the repo, so every live metric is null here.
// The synthetic fixture numbers live in a separate panel, never in this table.
export function candidateRows(catalogue, compare) {
  const list = catalogue?.candidates ?? [];
  return CANDIDATE_IDS.map(id => {
    const meta = list.find(c => Number(c.id) === id) ?? null;
    const row = compareRow(compare, id);
    const runState = candidateRunState(compare, id);
    return {
      id, kind: 'candidate',
      name: meta?.name ?? `Candidate ${id}`,
      source: meta?.source ?? null, tier: meta?.tier ?? null,
      warmStartDays: meta?.warm_start_days ?? null,
      status: meta?.status ?? 'shadow_only',
      // Live metrics: no real source exists.
      winRate: null, meanReturn: null, trades: null, avgHoldDays: null,
      sampleLabel: row?.sample_label ?? null,
      // Why the live metrics are missing.
      missingReason: !compare
        ? 'no compare output loaded'
        : runState === 'not_run'
          ? 'not run in the synthetic fixture; no live compare output exists'
          : row
            ? 'only synthetic fixture numbers exist (see separate panel)'
            : 'ran in the synthetic fixture with 0 closed trades; no live compare output exists',
      synthetic: row ?? null, runState,
    };
  });
}

// Main-7 rows from historical.json. Holding period is NOT in the export:
// metrics carry closed trades, win rate and mean gross return only, so the
// average hold is null ("not available") rather than derived from the 10
// top/bottom trades, which would not be an average over all trades.
export function mainRows(history) {
  const list = history?.strategies ?? [];
  return MAIN_IDS.map(id => {
    const s = list.find(x => Number(x.id) === id) ?? null;
    const m = s?.metrics ?? null;
    return {
      id, kind: 'main',
      name: s?.name ?? `Strategy ${id}`,
      trades: m?.closed_trades ?? null,
      winRate: m?.win_rate_pct ?? null,
      meanReturn: m?.mean_gross_return_pct ?? null,
      medianReturn: m?.median_gross_return_pct ?? null,
      openPositions: m?.open_positions ?? null,
      avgHoldDays: null,
      holdMissingReason: 'historical.json has no average holding period',
      monthly: s?.monthly ?? null,
    };
  });
}

// Filter health: is each engine's data present and complete enough to judge?
// Adaptive side reads the compare file; main side reads the research export.
export function adaptiveHealth(compare) {
  if (!compare) return {available: false, checks: [{label: 'Compare output', value: 'not available', ok: false}]};
  const fwd = compare.forward_shadow ?? {};
  const hist = compare.historical_replay ?? {};
  const missing = CANDIDATE_IDS.filter(id => !compareRow(compare, id));
  const insufficient = CANDIDATE_IDS
    .map(id => [id, compareRow(compare, id)])
    .filter(([, r]) => r && r.sample_label === 'insufficient_sample')
    .map(([id]) => id);
  return {
    available: true,
    mode: compare.mode ?? null,
    sessionDay: compare.session_day ?? null,
    generatedAt: compare.generated_at_ist ?? null,
    statement: compare.statement ?? null,
    replayDays: hist.days ?? null, replayTrades: hist.trades ?? null,
    forwardDays: fwd.days ?? null, forwardTrades: fwd.trades ?? null,
    minTrades: compare.min_trades_for_sample ?? MIN_TRADES_FOR_SAMPLE,
    missingCandidates: missing, insufficientCandidates: insufficient,
    checks: [
      {label: 'Compare file', value: 'loaded', ok: true},
      {label: 'Mode', value: compare.mode ?? 'not available', ok: compare.mode === 'shadow_simulated_observation_only'},
      {label: 'Last compare run (session day)', value: compare.session_day ?? 'not available', ok: Boolean(compare.session_day)},
      {label: 'Forward shadow days observed', value: String(fwd.days ?? 'not available'), ok: Number(fwd.days) > 0},
      {label: 'Candidates with any compare row', value: `${CANDIDATE_IDS.length - missing.length} of ${CANDIDATE_IDS.length}`, ok: missing.length === 0},
      {label: 'Candidates with a sufficient sample', value: `${CANDIDATE_IDS.length - missing.length - insufficient.length} of ${CANDIDATE_IDS.length}`, ok: insufficient.length === 0},
    ],
  };
}

export function mainHealth(history) {
  if (!history) return {available: false, checks: [{label: 'Research export', value: 'not available', ok: false}]};
  const c = history.coverage ?? {};
  const verification = history.verification ?? [];
  return {
    available: true,
    asOf: history.asOf ?? null,
    coverage: c,
    verificationCount: verification.length,
    caveatCount: (history.caveats ?? []).length,
    checks: [
      {label: 'Research export', value: history.asOf ? `as of ${history.asOf}` : 'loaded', ok: Boolean(history.asOf)},
      {label: 'Universe coverage', value: `${c.current ?? '?'} current of ${c.universe ?? '?'} (missing ${c.missing ?? '?'}, stale ${c.stale ?? '?'})`, ok: Number(c.missing) === 0 && Number(c.stale) === 0},
      {label: 'Strategies verified', value: `${verification.length} of 7`, ok: verification.length === 7},
      {label: 'Caveats recorded', value: String((history.caveats ?? []).length), ok: true},
    ],
  };
}

// --- Task 3: recent trend, trailing window, promotion / demotion (proposed) ---

// Full calendar months in the export: the export month itself is partial
// (asOf is a day inside it), so it is excluded. A month with no closed trades
// is absent from the breakdown and counts as unavailable, never zero.
export function fullMonths(monthly, asOf) {
  const month = String(asOf ?? '').slice(0, 7);
  return (monthly ?? []).filter(m => m.period < month && Number(m.closed) > 0)
    .sort((a, b) => a.period.localeCompare(b.period));
}

// Recent trend, exact definition (stated on the screen too):
//   recent month = last full month with closed trades;
//   prior month  = the full month before it;
//   long-run mean = unweighted mean of the monthly mean returns over all full
//   months with closed trades.
// Label: Improving if the recent mean is above BOTH the prior month and the
// long-run mean; Worsening if below BOTH; otherwise Mixed.
export function recentTrend(monthly, asOf) {
  const full = fullMonths(monthly, asOf);
  if (full.length < 2) return null;
  const recent = full.at(-1), prior = full.at(-2);
  const longRunMean = full.reduce((s, m) => s + Number(m.mean), 0) / full.length;
  const label = recent.mean > prior.mean && recent.mean > longRunMean
    ? 'Improving'
    : recent.mean < prior.mean && recent.mean < longRunMean ? 'Worsening' : 'Mixed';
  return {period: recent.period, trades: recent.closed, winRate: recent.winRate, mean: recent.mean,
    priorPeriod: prior.period, priorMean: prior.mean, longRunMean, label};
}

// Trailing 12 full months ending at the recent month, trade-weighted:
// mean = sum(closed * mean) / sum(closed); win rate the same way.
export function trailing12(monthly, asOf) {
  const full = fullMonths(monthly, asOf);
  if (!full.length) return null;
  const win = full.slice(-12);
  const trades = win.reduce((s, m) => s + Number(m.closed), 0);
  if (!trades) return null;
  return {from: win[0].period, to: win.at(-1).period, trades,
    mean: win.reduce((s, m) => s + Number(m.closed) * Number(m.mean), 0) / trades,
    winRate: win.reduce((s, m) => s + Number(m.closed) * Number(m.winRate), 0) / trades};
}

// PROPOSED promotion criteria (candidate -> main). A proposal only: nothing
// here promotes anything, and every promotion still needs Grok's review and
// VVS's approval. Judged on FORWARD shadow (observed, net of costs), never on
// the synthetic replay. Sources: NIFTY 50 / Next 50 review practice (fixed
// semi-annual review, full checklist, six-month data window) and the
// backtest sample-size arithmetic (30 trades is a floor, not a target;
// ~100 trades is where a trend starts to show).
export const PROPOSED_PROMOTION = {
  minForwardTrades: 100,
  minForwardSessions: 252, // ~12 months of sessions
  minWinRatePct: 30,
  maxDrawdownPct: 15,
  maxOverlapPct: 50,
  reviewCycle: 'semi-annual, NIFTY-style (data cutoffs 31 Jan / 31 Jul)',
  manualReview: 'Rules frozen for the whole shadow window; window covers at least one down month; recent trend not worsening; out-of-sample confirmation. Grok review + VVS approval required in all cases.',
};

// Read-only promotion label for one candidate. Verdicts:
//   'not evaluable' — no forward-shadow row at all (nothing observed live yet);
//   'no'  — a forward row exists but at least one computed check fails;
//   'yes' — every computed check passes (still only a proposal: see manualReview).
export function promotionLabel(compare, id) {
  const fwd = compare?.forward_shadow?.strategies?.[String(id)] ?? null;
  if (!fwd) return {verdict: 'not evaluable', reasons: ['no forward shadow trades yet — nothing observed live']};
  const checks = [
    {key: 'sample', pass: Number(fwd.trades) >= PROPOSED_PROMOTION.minForwardTrades,
      text: `${fwd.trades} forward trades (need ≥ ${PROPOSED_PROMOTION.minForwardTrades})`},
    {key: 'duration', pass: Number(compare.forward_shadow.days) >= PROPOSED_PROMOTION.minForwardSessions,
      text: `${compare.forward_shadow.days ?? 0} forward sessions (need ≥ ${PROPOSED_PROMOTION.minForwardSessions}, ~12 months)`},
    {key: 'edge', pass: Number(fwd.avg_net_return_pct) > 0 && Number(fwd.win_rate) * 100 >= PROPOSED_PROMOTION.minWinRatePct,
      text: `mean net ${Number(fwd.avg_net_return_pct).toFixed(2)}% / trade, win ${(Number(fwd.win_rate) * 100).toFixed(1)}% (need mean > 0% and win ≥ ${PROPOSED_PROMOTION.minWinRatePct}%)`},
    {key: 'drawdown', pass: Number(fwd.max_drawdown_pct) <= PROPOSED_PROMOTION.maxDrawdownPct,
      text: `max drawdown ${Number(fwd.max_drawdown_pct).toFixed(2)}% (need ≤ ${PROPOSED_PROMOTION.maxDrawdownPct}%)`},
    {key: 'overlap', pass: fwd.overlap_with_seven?.pct === null || Number(fwd.overlap_with_seven.pct) * 100 < PROPOSED_PROMOTION.maxOverlapPct,
      text: `overlap with the seven ${fwd.overlap_with_seven?.pct === null ? 'not available' : (Number(fwd.overlap_with_seven.pct) * 100).toFixed(1) + '%'} (need < ${PROPOSED_PROMOTION.maxOverlapPct}%)`},
  ];
  const failed = checks.filter(c => !c.pass);
  return {verdict: failed.length ? 'no' : 'yes', reasons: checks.map(c => `${c.pass ? '✓' : '✗'} ${c.text}`), checks};
}

// PROPOSED demotion watch (main -> adaptive). A watch flag only, never an
// automatic move: trailing-12-month mean gross return below 0% AND the recent
// trend worsening. Reviewed semi-annually; a single bad year never demotes
// on its own. Grok review + VVS approval required.
export function demotionWatch(mainRow) {
  const t12 = mainRow.trailing12, trend = mainRow.trend;
  if (!t12 || !trend) return {watch: false, evaluable: false, reasons: ['not evaluable — monthly breakdown missing']};
  const watch = Number(t12.mean) < 0 && trend.label === 'Worsening';
  return {watch, evaluable: true, reasons: [
    `trailing 12m mean ${Number(t12.mean).toFixed(2)}% / trade (${t12.from} to ${t12.to}, ${t12.trades} trades)`,
    `recent trend ${trend.label} (${trend.period} mean ${Number(trend.mean).toFixed(2)}% vs prior ${Number(trend.priorMean).toFixed(2)}% and long-run ${Number(trend.longRunMean).toFixed(2)}%)`,
  ]};
}

// Main-7 rows enriched with trend + trailing-12m analytics (Task 3).
export function mainAnalyticsRows(history) {
  return mainRows(history).map(r => {
    const trend = recentTrend(r.monthly, history?.asOf);
    const t12 = trailing12(r.monthly, history?.asOf);
    const row = {...r, trend, trailing12: t12};
    row.demotion = demotionWatch(row);
    return row;
  });
}
