import React from 'react';
import {readFileSync} from 'node:fs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, within} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {isPortalPath, parseRoute, parseRunId, runIdFor, stockHref} from '../src/portal/router';
import {overlapStats} from '../src/portal/StrategyComparison';
import {clearStaticCache, symbolCandidates} from '../src/portal/data';
import {THEME_KEY} from '../src/portal/theme';

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
const positionsPage = {total: 1, page: 1, limit: 25, rows: [{strategy: 7, symbol: 'MAHABANK-EQ', qty: 1, entry_price: 86.9, mark_price: 87.1, mark_stale: false, unrealized_pnl: 0.2, mark_at: '2026-11-09T09:59:00Z', entry_at: '2026-11-09T06:00:00Z'}]};

function mockNetwork({overviewFails = false} = {}) {
  clearStaticCache();
  const calls = [];
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url); calls.push(u);
    if (u.startsWith('/api/dashboard')) {
      const view = new URLSearchParams(u.split('?')[1]).get('view');
      if (view === 'overview') return overviewFails ? {ok: false, json: async () => ({error: {message: 'The paper database could not be reached.'}})} : {ok: true, json: async () => overview};
      if (view === 'positions') return {ok: true, json: async () => positionsPage};
      if (view === 'history') return {ok: true, json: async () => ({rows: [{quote_at: '2026-11-09T09:58:00Z', price: 86.5}, {quote_at: '2026-11-09T09:59:00Z', price: 87.1}]})};
      if (view === 'analytics' || view === 'scan') return {ok: true, json: async () => ({available: false, message: 'No completed-bar scan has been saved yet.'})};
      return {ok: true, json: async () => ({total: 0, page: 1, limit: 25, rows: []})};
    }
    return {ok: true, json: async () => JSON.parse(readFileSync(process.cwd() + '/public' + u.split('?')[0], 'utf8'))};
  }));
  return calls;
}
function at(path) { window.history.replaceState(null, '', path); }

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); at('/'); });

describe('portal routing helpers', () => {
  it('recognises only the portal paths', () => {
    for (const p of ['/paper', '/strategy-tester', '/strategy-comparison', '/performance/', '/stock/CUPID']) expect(isPortalPath(p)).toBe(true);
    for (const p of ['/', '/index.html', '/api/dashboard', '/stock', '/stock/a/b']) expect(isPortalPath(p)).toBe(false);
  });
  it('parses stock routes and run ids strictly', () => {
    const r = parseRoute('/stock/cupid', '?runId=hist-2026-11-06-s3');
    expect(r).toMatchObject({key: 'stock', symbol: 'CUPID', runId: {kind: 'hist', asOf: '2026-11-06', strategyId: 3}});
    expect(parseRunId('scan-2026-11-09-s7')).toMatchObject({kind: 'scan', strategyId: 7});
    expect(parseRunId('hist-2026-11-06-s9')).toBeNull();
    expect(parseRunId('1;DROP')).toBeNull();
    expect(parseRoute('/stock/%3Cscript%3E', '').symbol).toBeNull();
    expect(runIdFor('2026-11-06', 2)).toBe('hist-2026-11-06-s2');
    expect(stockHref('APARINDS-EQ', 'hist-2026-11-06-s3')).toBe('/stock/APARINDS?runId=hist-2026-11-06-s3');
    expect(symbolCandidates('CUPID')).toEqual(['CUPID-EQ', 'CUPID-BE', 'CUPID']);
  });
  it('computes overlap sets exactly', () => {
    expect(overlapStats([['A', 'B', 'C'], ['B', 'C', 'D']])).toEqual({union: ['A', 'B', 'C', 'D'], intersection: ['B', 'C'], jaccard: 0.5});
    expect(overlapStats([]).jaccard).toBeNull();
  });
});

describe('portal screens', () => {
  it('paper desk shows ledger figures, the over-limit sleeve and keeps the demo trade excluded', async () => {
    mockNetwork(); at('/paper');
    render(<Portal/>);
    await screen.findByText('What the engine is holding right now');
    expect(screen.getAllByText('−₹123.45').length).toBeGreaterThan(0);
    expect(screen.getByText('12 / 9')).toBeInTheDocument();
    expect(screen.getByText('Over slot limit — no new buys')).toBeInTheDocument();
    expect(screen.getByText('Manual demo — excluded from every total')).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: /place|buy|sell order/i})).toBeNull();
    await screen.findByText('87.10', {exact: false});
  });

  it('theme toggle switches and persists dark/light', async () => {
    mockNetwork(); at('/performance');
    const {container} = render(<Portal/>);
    const root = container.querySelector('.ad-root');
    const start = root.dataset.theme;
    fireEvent.click(screen.getByRole('button', {name: /Switch to (light|dark) theme/}));
    expect(root.dataset.theme).not.toBe(start);
    expect(window.localStorage.getItem(THEME_KEY)).toBe(root.dataset.theme);
  });

  it('performance reads snapshots and reports missing analytics honestly', async () => {
    mockNetwork(); at('/performance');
    render(<Portal/>);
    await screen.findByText('The record so far');
    expect(screen.getByText('No snapshot for today yet')).toBeInTheDocument();
    await screen.findByText('Net-of-cost analytics unavailable.');
    expect(screen.queryByText(/Sharpe ratio:/i)).toBeNull();
  });

  it('strategy tester opens a saved run and links matched stocks to stock detail with the run id', async () => {
    mockNetwork(); at('/strategy-tester?strategy=3&source=hist');
    render(<Portal/>);
    await screen.findByText('RSI Pullback · saved backtest');
    const link = await screen.findByRole('link', {name: 'APARINDS'});
    expect(link.getAttribute('href')).toBe('/stock/APARINDS?runId=hist-2026-11-06-s3');
    expect(screen.getByText('Custom rule builder')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', {name: 'Live scan'}));
    await screen.findByText('No live scan available.');
    expect(window.location.search).toContain('source=scan');
  });

  it('comparison shows a metric matrix and exact overlap for the chosen strategies', async () => {
    mockNetwork(); at('/strategy-comparison?ids=1,4');
    render(<Portal/>);
    await screen.findByText('Metric matrix');
    const venn = document.querySelector('.ad-venn');
    expect(within(venn).getByText('55')).toBeInTheDocument();
    expect(within(venn).getByText('1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: /52W Momentum/}));
    expect(window.location.search).toContain('ids=1%2C4%2C7');
  });

  it('stock detail loads the saved per-trade backtest for a run', async () => {
    mockNetwork(); at('/stock/APARINDS?runId=hist-2026-11-06-s3');
    render(<Portal/>);
    await screen.findByText('Trades behind the numbers');
    expect(screen.getByText(/run hist-2026-11-06-s3/)).toBeInTheDocument();
    expect(screen.getByText('Fundamentals, corporate events and news')).toBeInTheDocument();
  });

  it('stock detail for a symbol outside the research set still shows live data without inventing a backtest', async () => {
    mockNetwork(); at('/stock/CUPID?runId=hist-2026-11-06-s3');
    render(<Portal/>);
    await screen.findByText(/was not matched by RSI Pullback/);
    expect(screen.getByText('No closed trades for this stock in this run')).toBeInTheDocument();
  });

  it('navigates between screens without a reload', async () => {
    mockNetwork(); at('/paper');
    render(<Portal/>);
    await screen.findByText('What the engine is holding right now');
    await act(async () => { fireEvent.click(screen.getByRole('link', {name: 'Performance'})); });
    await screen.findByText('The record so far');
    expect(window.location.pathname).toBe('/performance');
  });

  it('shows no figures when the live ledger cannot be read', async () => {
    mockNetwork({overviewFails: true}); at('/paper');
    render(<Portal/>);
    await screen.findByText('Live ledger unavailable');
    expect(screen.getByText(/could not be reached/)).toBeInTheDocument();
    expect(screen.queryByText('−₹123.45')).toBeNull();
  });
});
