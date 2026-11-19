// Phase 2 EOD metrics: revaluation tag, execution-quality panel, outages list.
// Follows the portal.test.jsx / ledger-mobile.test.jsx pattern: stub fetch, render the
// portal at /performance, click the End-of-day tab, assert what the user sees.
import React from 'react';
import {readFileSync} from 'node:fs';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, within} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {clearStaticCache} from '../src/portal/data';

const sleeve = (open, net) => ({cash_paise: 100000000, market_value_paise: 0, equity_paise: 100000000 + net, net_pnl_paise: net, open_positions: open, slots_max: 9});
const overview = {
  readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v1', day: '2026-11-09', market: {open: false, label: 'NSE session closed'},
  cappedLedger: {available: true, master_capital_paise: 700000000, cash_paise: 690000000, equity_paise: 699987655, headline_net_pnl_paise: -12345, capital_return_pct: -0.0018, max_positions_per_sleeve: 9,
    sleeves: {1: sleeve(2, -2000), 2: sleeve(0, 0), 3: sleeve(1, 500), 4: sleeve(0, 0), 5: sleeve(1, -845), 6: sleeve(1, 0), 7: sleeve(12, -10000)}},
  totals: {filledOrders: 17, closedTrades: 0, todayFills: 17, openPositions: 17, realized: 0, unrealized: -98.4, total: -98.4, daily: null, staleMarks: 0, missingMarks: 0, pendingSignals: 3},
  closedStats: {count: 0, winRate: null, averageHoldDays: null, matchedClosures: 0}, signalsToday: 20,
  performance: [1, 2, 3, 4, 5, 6, 7].map(i => ({strategy: i, closedTrades: 0, winRate: null, realized: 0, openPositions: i === 7 ? 12 : 1})),
  positions: [], dailyHistory: [{day: '2026-11-09', daily: -123.45, total: -123.45}],
  recentFills: [{id: 9, strategy: 7, symbol: 'MAHABANK-EQ', side: 'BUY', qty: 1, price: 86.9, filled_at: '2026-11-09T06:00:00Z', reason: 'daily_entry'}],
  recentSignals: [], lastRuns: [{kind: 'scan', at: '2026-11-09T06:40:00Z', status: 'ok'}], quoteAudit: [],
  demoTrades: [{id: 'dummy_reliance_manual_v1', is_test: 1, strategy_label: 'Manual demo', symbol: 'RELIANCE-EQ', side: 'BUY', qty: 1, entry_price: 1490.1, status: 'open', entry_at: '2026-11-09T07:00:00Z'}],
};
const perfOk = (maxDd, peak, trough, currentDd) => ({status: 'ok',
  expectancy: {closed_trades: 0, expectancy_net_paise: null, expectancy_gross_paise: null},
  drawdown: {max_drawdown_net_paise: maxDd, peak_day: peak, trough_day: trough, current_drawdown_net_paise: currentDd},
  cost_to_gross: 0.136, implementation_shortfall_bps_vs_signal_close: 18.59, implementation_shortfall_bps_vs_session_open: 3.2});
const eod = (day, net, extra = {}) => ({day, as_of: day + 'T16:10:00+05:30', gross_total_paise: 1000, net_total_accrued_paise: net, daily_gross_paise: 1000, daily_net_accrued_paise: net, closing_valuation_complete: true, daily_baseline_known: true, revaluation: false, mark_basis: null, performance: null, ...extra});
const analyticsFixture = {
  available: true, totals: {gross_total_paise: 1000, net_total_accrued_paise: 800, accrued_cost_paise: 136, missing_marks: 0},
  charge_breakdown_paise: {}, strategies: [], cohorts: [], weekly: {net_paise: null, complete: false, week_start: '2026-11-16', reason: 'Awaiting complete EODs.'},
  health: null, heartbeatStale: false, ledger_reconciled: true,
  eods: [
    eod('2026-11-09', 800),
    eod('2026-11-16', -177709, {revaluation: true, mark_basis: 'official_close_revaluation', performance: perfOk(-177709, '2026-11-09', '2026-11-16', -177709)}),
    eod('2026-11-17', 603785, {revaluation: true, mark_basis: 'official_close_revaluation', performance: perfOk(-177709, '2026-11-09', '2026-11-16', 0)}),
    eod('2026-11-18', -468449, {revaluation: true, mark_basis: 'official_close_revaluation', performance: perfOk(-468449, '2026-11-17', '2026-11-18', -468449)}),
  ],
  outages: [
    {session_day: '2026-11-18', kind: 'monitor_coverage_low', detected_at: '2026-11-18T16:30:00+05:30', checks: 12, coverage: 0.407},
    {session_day: '2026-11-17', kind: 'scan_missing', detected_at: '2026-11-17T16:30:00+05:30', checks: null, coverage: null},
    {session_day: '2026-11-16', kind: 'no_monitoring', detected_at: '2026-11-16T16:30:00+05:30', checks: 0, coverage: 0},
  ],
};
const analyticsNoPerf = {...analyticsFixture, eods: [eod('2026-11-09', 800), eod('2026-11-10', 900)], outages: []};

function mockNetwork(analytics) {
  clearStaticCache(); vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url);
    if (u.startsWith('/api/dashboard')) {
      const view = new URLSearchParams(u.split('?')[1]).get('view');
      if (view === 'overview') return {ok: true, json: async () => overview};
      if (view === 'analytics') return {ok: true, json: async () => analytics};
      return {ok: true, json: async () => ({total: 0, page: 1, limit: 25, rows: []})};
    }
    return {ok: true, json: async () => JSON.parse(readFileSync(process.cwd() + '/public' + u.split('?')[0], 'utf8'))};
  }));
}
async function openEodTab() {
  window.history.replaceState(null, '', '/performance'); render(<Portal/>);
  await screen.findByText('The record so far');
  await screen.findByText('Net-of-cost book');
  const tab = await screen.findByRole('tab', {name: /End-of-day book/});
  fireEvent.click(tab);
  await screen.findByText('Execution quality');
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); window.history.replaceState(null, '', '/'); });

it('revalued EOD rows show the official-close tag and the panel shows the latest stored performance', async () => {
  mockNetwork(analyticsFixture); await openEodTab();
  expect(screen.getAllByText('Revalued from official close')).toHaveLength(3);
  const panel = screen.getByText('Execution quality').closest('section');
  expect(within(panel).getAllByText('not enough data yet').length).toBeGreaterThan(0);
  expect(within(panel).getAllByText('0 closed trades')).toHaveLength(2);
  expect(within(panel).getAllByText(/4,684\.49/).length).toBeGreaterThan(0);
  expect(within(panel).getByText('13.6%')).toBeInTheDocument();
  expect(within(panel).getByText('18.59 bps')).toBeInTheDocument();
  expect(within(panel).getByText('3.20 bps')).toBeInTheDocument();
  expect(within(panel).queryByText('₹0')).toBeNull();
  const outages = screen.getByText('Outages').closest('section');
  expect(within(outages).getByText('Low monitor coverage')).toBeInTheDocument();
  expect(within(outages).getByText('Scan missing')).toBeInTheDocument();
  expect(within(outages).getByText('No monitoring')).toBeInTheDocument();
  expect(within(outages).getByText(/40\.7%/)).toBeInTheDocument();
  expect(within(outages).getByText('2026-11-18')).toBeInTheDocument();
});

it('no performance block yet shows "not enough data yet" and no outages', async () => {
  mockNetwork(analyticsNoPerf); await openEodTab();
  expect(screen.queryByText('Revalued from official close')).toBeNull();
  const panel = screen.getByText('Execution quality').closest('section');
  expect(within(panel).getByText('No EOD has a performance block yet.')).toBeInTheDocument();
  expect(within(panel).getAllByText('not enough data yet').length).toBeGreaterThan(0);
  const outages = screen.getByText('Outages').closest('section');
  expect(within(outages).getByText('No outages recorded.')).toBeInTheDocument();
});

it('phone CSS: outages list stacks and tiles stay two-column at 640px', () => {
  const css = readFileSync('src/portal/portal.css', 'utf8');
  const media = (() => { const out = []; const re = /@media \(max-width:640px\)\{/g; while (re.exec(css)) { let depth = 1, i = re.lastIndex; while (depth && i < css.length) { if (css[i] === '{') depth++; if (css[i] === '}') depth--; i++; } out.push(css.slice(re.lastIndex, i - 1)); } return out.join('\n'); })();
  expect(css).toMatch(/\.ad-outage-row\{display:flex;[^}]*flex-wrap:wrap/);
  expect(media).toMatch(/\.ad-outage-row\{flex-direction:column/);
  expect(media).toMatch(/\.ad-tiles\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
});
