// @vitest-environment jsdom
// Adaptive Engine screen: renders from the real data files, shows
// "not available" where no real source exists, and never invents a number.
import {readFileSync} from 'node:fs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import AdaptiveEngine from '../src/portal/AdaptiveEngine';
import {clearStaticCache} from '../src/portal/data';
import {
  adaptiveHealth, candidateRows, demotionWatch, fullMonths, mainAnalyticsRows,
  mainHealth, mainRows, promotionLabel, recentTrend, trailing12,
} from '../src/portal/adaptive';

const realCompare = JSON.parse(readFileSync(process.cwd() + '/public/data/adaptive-compare.json', 'utf8'));
const realCatalogue = JSON.parse(readFileSync(process.cwd() + '/public/data/adaptive-candidates.json', 'utf8'));

const overview = {
  readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v2', day: '2026-11-09',
  market: {open: false, label: 'NSE session closed'},
  totals: {filledOrders: 0, closedTrades: 0, todayFills: 0, openPositions: 0, realized: 0, unrealized: 0, total: 0, daily: null, staleMarks: 0, missingMarks: 0, pendingSignals: 0},
  cappedLedger: {available: true, master_capital_paise: 700000000, cash_paise: 700000000, equity_paise: 700000000, headline_net_pnl_paise: 0, capital_return_pct: 0, max_positions_per_sleeve: 9, sleeves: {}},
  closedStats: {count: 0, winRate: null, averageHoldDays: null, matchedClosures: 0},
  performance: [], positions: [], dailyHistory: [], recentFills: [], recentSignals: [], lastRuns: [], quoteAudit: [], demoTrades: [],
};

function mockNetwork({compareFails = false} = {}) {
  clearStaticCache();
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url);
    if (u.startsWith('/api/dashboard')) {
      const view = new URLSearchParams(u.split('?')[1]).get('view');
      if (view === 'overview') return {ok: true, json: async () => overview};
      return {ok: true, json: async () => ({total: 0, page: 1, limit: 25, rows: []})};
    }
    if (compareFails && u === '/data/adaptive-compare.json') return {ok: false, json: async () => ({})};
    return {ok: true, json: async () => JSON.parse(readFileSync(process.cwd() + '/public' + u.split('?')[0], 'utf8'))};
  }));
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); window.history.replaceState(null, '', '/'); });

const monthly = (rows) => rows.map(([period, closed, winRate, mean]) => ({period, closed, winRate, mean, median: mean}));

describe('adaptive logic', () => {
  it('candidate rows have no live metrics: only the synthetic rows exist', () => {
    const rows = candidateRows(realCatalogue, realCompare);
    expect(rows.map(r => r.id)).toEqual([101, 102, 103, 104, 105, 106]);
    for (const r of rows) {
      expect(r.winRate).toBeNull();
      expect(r.meanReturn).toBeNull();
      expect(r.trades).toBeNull();
      expect(r.avgHoldDays).toBeNull();
    }
    expect(rows.find(r => r.id === 102).synthetic).toMatchObject({trades: 27, win_rate: 0.037, avg_net_return_pct: -4.8622, avg_hold_days: 9.78, sample_label: 'insufficient_sample'});
    expect(rows.find(r => r.id === 105).synthetic).toMatchObject({trades: 1, win_rate: 1.0, avg_net_return_pct: 9.4596});
    expect(rows.find(r => r.id === 106).synthetic).toBeNull();
    expect(rows.find(r => r.id === 106).runState).toBe('ran');
    expect(rows.find(r => r.id === 106).missingReason).toMatch(/0 closed trades/);
    expect(rows.find(r => r.id === 101).runState).toBe('not_run');
    expect(rows.find(r => r.id === 103).missingReason).toMatch(/not run/);
  });

  it('main rows read metrics from historical.json and have no average hold', () => {
    const history = JSON.parse(readFileSync(process.cwd() + '/public/data/historical.json', 'utf8'));
    const rows = mainRows(history);
    expect(rows).toHaveLength(7);
    expect(rows.find(r => r.id === 4)).toMatchObject({trades: 10820, winRate: 36.081330868761555, meanReturn: 10.082743485152076, avgHoldDays: null});
    expect(rows.find(r => r.id === 5)).toMatchObject({winRate: 69.4211319130622, meanReturn: 1.604649342263887});
    for (const r of rows) expect(r.holdMissingReason).toMatch(/no average holding period/);
  });

  it('recent trend uses the last full month, never the partial export month', () => {
    const m = monthly([
      ['2026-08', 100, 40, 1.0], ['2026-09', 100, 40, 2.0],
      ['2026-10', 100, 40, 3.0], ['2026-11', 5, 40, 99.0],
    ]);
    const t = recentTrend(m, '2026-11-06');
    expect(t).toMatchObject({period: '2026-10', priorPeriod: '2026-09', priorMean: 2.0, mean: 3.0, label: 'Improving'});
    expect(t.longRunMean).toBeCloseTo(2.0);
    const down = recentTrend(monthly([
      ['2026-08', 100, 40, 5.0], ['2026-09', 100, 40, 2.0], ['2026-10', 100, 40, 1.0],
    ]), '2026-11-06');
    expect(down.label).toBe('Worsening');
    const mixed = recentTrend(monthly([
      ['2026-08', 100, 40, 0.0], ['2026-09', 100, 40, 2.0], ['2026-10', 100, 40, 1.0],
    ]), '2026-11-06');
    expect(mixed.label).toBe('Mixed');
    expect(recentTrend(monthly([['2026-10', 100, 40, 1.0]]), '2026-11-06')).toBeNull();
    expect(recentTrend([], '2026-11-06')).toBeNull();
  });

  it('trailing 12m is trade-weighted over the last 12 full months', () => {
    const periods = [];
    for (let y = 2025; y <= 2026; y++) for (let mo = 1; mo <= 12; mo++) periods.push(`${y}-${String(mo).padStart(2, '0')}`);
    const rows = periods.slice(0, 14).map((p, i) => [p, 10, 50, i]);
    // months 2025-01..2026-02, means 0..13: last 12 are 2025-03..2026-02, means 2..13
    const t = trailing12(monthly(rows), '2026-11-06');
    expect(t.trades).toBe(120);
    expect(t.mean).toBeCloseTo((2 + 3 + 4 + 5 + 6 + 7 + 8 + 9 + 10 + 11 + 12 + 13) / 12);
    expect(t.from).toBe('2025-03');
    expect(t.to).toBe('2026-02');
  });

  it('promotion is not evaluable with no forward shadow, yes/no only on a forward row', () => {
    expect(promotionLabel(realCompare, 102).verdict).toBe('not evaluable');
    expect(promotionLabel(realCompare, 106).verdict).toBe('not evaluable');
    expect(promotionLabel(null, 102).verdict).toBe('not evaluable');
    const good = {forward_shadow: {days: 300, strategies: {'102': {trades: 150, avg_net_return_pct: 1.5, win_rate: 0.45, max_drawdown_pct: 10, overlap_with_seven: {count: 10, pct: 0.07}}}}};
    expect(promotionLabel(good, 102).verdict).toBe('yes');
    const badDd = {forward_shadow: {days: 300, strategies: {'102': {trades: 150, avg_net_return_pct: 1.5, win_rate: 0.45, max_drawdown_pct: 20, overlap_with_seven: {count: 10, pct: 0.07}}}}};
    const bad = promotionLabel(badDd, 102);
    expect(bad.verdict).toBe('no');
    expect(bad.reasons.join(' ')).toMatch(/drawdown/);
    const small = {forward_shadow: {days: 300, strategies: {'102': {trades: 40, avg_net_return_pct: 1.5, win_rate: 0.45, max_drawdown_pct: 10, overlap_with_seven: {count: 1, pct: 0.02}}}}};
    expect(promotionLabel(small, 102).verdict).toBe('no');
  });

  it('demotion watch needs a negative trailing 12m AND a worsening trend', () => {
    const watch = demotionWatch({trailing12: {mean: -0.02, from: '2025-11', to: '2026-10', trades: 100}, trend: {label: 'Worsening', period: '2026-10', mean: -1.21, priorMean: 0.88, longRunMean: 0.82}});
    expect(watch.watch).toBe(true);
    const calm = demotionWatch({trailing12: {mean: 0.08, from: '2025-11', to: '2026-10', trades: 100}, trend: {label: 'Worsening', period: '2026-10', mean: -1.72, priorMean: 1.7, longRunMean: 3.44}});
    expect(calm.watch).toBe(false);
    const mixed = demotionWatch({trailing12: {mean: -0.5, from: '2025-11', to: '2026-10', trades: 100}, trend: {label: 'Mixed', period: '2026-10', mean: 2.93, priorMean: 2.47, longRunMean: 7.11}});
    expect(mixed.watch).toBe(false);
    expect(demotionWatch({trailing12: null, trend: null}).evaluable).toBe(false);
  });

  it('filter health reports the missing candidates and zero forward shadow honestly', () => {
    const a = adaptiveHealth(realCompare);
    expect(a.forwardDays).toBe(0);
    expect(a.forwardTrades).toBe(0);
    expect(a.replayDays).toBe(130);
    expect(a.missingCandidates).toEqual([101, 103, 104, 106]);
    expect(a.insufficientCandidates).toEqual([102, 105]);
    expect(a.checks.find(c => c.label === 'Forward shadow days observed').ok).toBe(false);
    const history = JSON.parse(readFileSync(process.cwd() + '/public/data/historical.json', 'utf8'));
    const m = mainHealth(history);
    expect(m.asOf).toBe('2026-11-06');
    expect(m.coverage).toMatchObject({universe: 751, current: 692, missing: 52, stale: 7});
    expect(m.checks.find(c => c.label === 'Universe coverage').ok).toBe(false);
    expect(adaptiveHealth(null).available).toBe(false);
    expect(mainHealth(null).available).toBe(false);
  });

  it('main analytics: S7 is the only demotion watch, S1/S3/S5/S6/S7 worsening, S2/S4 mixed', () => {
    const history = JSON.parse(readFileSync(process.cwd() + '/public/data/historical.json', 'utf8'));
    const rows = mainAnalyticsRows(history);
    expect(rows.map(r => r.trend.label)).toEqual(['Worsening', 'Mixed', 'Worsening', 'Mixed', 'Worsening', 'Worsening', 'Worsening']);
    expect(rows.map(r => r.trend.period)).toEqual(Array(7).fill('2026-10'));
    expect(rows.filter(r => r.demotion.watch).map(r => r.id)).toEqual([7]);
    expect(rows.find(r => r.id === 7).trailing12.mean).toBeCloseTo(-0.02, 1);
    expect(rows.find(r => r.id === 1).trailing12.mean).toBeCloseTo(0.08, 1);
    expect(fullMonths(rows[0].monthly, '2026-11-06').at(-1).period).toBe('2026-10');
  });
});

describe('adaptive engine screen', () => {
  it('renders the route, candidates as not available, and the synthetic panel separately', async () => {
    mockNetwork();
    window.history.replaceState(null, '', '/adaptive-engine');
    render(<Portal/>);
    await screen.findByText('Adaptive Engine', {selector: 'h1'});
    expect(screen.getByRole('link', {name: 'Adaptive Engine'})).toHaveAttribute('href', '/adaptive-engine');
    for (const name of ['Minervini Trend Template', 'Connors RSI(2)', 'Bollinger squeeze', 'Wilder ADX', '12-1 cross-sectional', 'Sector-adjusted 12-1']) {
      expect(screen.getAllByText(name, {exact: false}).length).toBeGreaterThanOrEqual(1);
    }
    expect(screen.getAllByText('not available').length).toBeGreaterThan(10);
    expect(screen.getAllByText('Synthetic fixture only (4 symbols, 130 days) — not real NSE results').length).toBe(3);
    expect(screen.getByText('-4.86%', {exact: false})).toBeInTheDocument();
    expect(screen.getByText('3.7%', {exact: false})).toBeInTheDocument();
    expect(screen.getAllByText('not evaluable').length).toBe(6);
    expect(screen.queryByRole('button', {name: /promote|demote/i})).toBeNull();
  });

  it('shows the main-7 comparison with real figures, trend and demotion watch', async () => {
    mockNetwork();
    render(<AdaptiveEngine/>);
    await screen.findByText('Main 7 strategies — the comparison');
    expect(screen.getByText('10.08%', {exact: false})).toBeInTheDocument();
    expect(screen.getByText('69.4%', {exact: false})).toBeInTheDocument();
    expect(screen.getByText('27,669', {exact: false})).toBeInTheDocument();
    expect(screen.getAllByText('Worsening').length).toBeGreaterThanOrEqual(5);
    expect(screen.getAllByText('Mixed').length).toBe(2);
    expect(screen.getByText('Demotion watch (proposed)')).toBeInTheDocument();
    expect(screen.getAllByText('watch').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Proposed promotion / demotion criteria')).toBeInTheDocument();
    expect(screen.getByText(/Nothing here moves a strategy/)).toBeInTheDocument();
    expect(screen.getByText(/≥ 100 closed trades in forward shadow/)).toBeInTheDocument();
    expect(screen.getAllByRole('img').length).toBeGreaterThanOrEqual(3);
  });

  it('trend chart shows a line for a main strategy and not available for a candidate', async () => {
    mockNetwork();
    render(<AdaptiveEngine/>);
    await screen.findByText('Is it getting better or worse?');
    expect(screen.getByRole('button', {name: 'S1 Donchian / Turtle'})).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole('button', {name: 'C102'})); });
    await screen.findByText('Trend not available');
    expect(screen.getByText(/has no monthly series/)).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole('button', {name: 'S4 Dual MA'})); });
    await screen.findByText(/Mean gross return per closed trade, by exit month/);
  });

  it('shows not available everywhere when the compare file fails to load', async () => {
    mockNetwork({compareFails: true});
    render(<AdaptiveEngine/>);
    await screen.findByText('Compare output unavailable.');
    expect(screen.getByText(/Candidate metrics below are therefore all/)).toBeInTheDocument();
    expect(screen.getAllByText('not available').length).toBeGreaterThan(10);
    expect(screen.getByText('Main 7 strategies — the comparison')).toBeInTheDocument();
  });
});
