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
