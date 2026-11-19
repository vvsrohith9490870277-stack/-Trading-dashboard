// Book switch + archive banner + empty-state contract for the portal (jsdom, fetch stubbed).
// Follows tests/ledger-mobile.test.jsx (CSS extraction + stubbed fetch) and tests/portal.test.jsx.
import React from 'react';
import {readFileSync} from 'node:fs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, within} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {clearStaticCache} from '../src/portal/data';

const css = readFileSync('src/portal/portal.css', 'utf8');
const media = (() => { const out = []; const re = /@media \(max-width:640px\)\{/g; while (re.exec(css)) { let depth = 1, i = re.lastIndex; while (depth && i < css.length) { if (css[i] === '{') depth++; if (css[i] === '}') depth--; i++; } out.push(css.slice(re.lastIndex, i - 1)); } return out.join('\n'); })();
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const sleeve = (open, net) => ({cash_paise: 100000000, market_value_paise: 0, equity_paise: 100000000 + net, net_pnl_paise: net, open_positions: open, slots_max: 9});
const overview = {
  readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v2', book: 'paper_v2', isArchive: false, day: '2026-11-09', market: {open: false, label: 'NSE session closed'},
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
const emptyOverview = {
  readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v2', book: 'paper_v2', isArchive: false, day: '2026-11-09', market: {open: false, label: 'NSE session closed'},
  cappedLedger: {available: true, master_capital_paise: 700000000, cash_paise: 700000000, equity_paise: 700000000, headline_net_pnl_paise: 0, capital_return_pct: 0, max_positions_per_sleeve: 9,
    sleeves: {1: sleeve(0, 0), 2: sleeve(0, 0), 3: sleeve(0, 0), 4: sleeve(0, 0), 5: sleeve(0, 0), 6: sleeve(0, 0), 7: sleeve(0, 0)}},
  totals: {filledOrders: 0, closedTrades: 0, todayFills: 0, openPositions: 0, realized: 0, unrealized: 0, total: 0, daily: null, staleMarks: 0, missingMarks: 0, pendingSignals: 0},
  closedStats: {count: 0, winRate: null, averageHoldDays: null, matchedClosures: 0}, signalsToday: 0,
  performance: [1, 2, 3, 4, 5, 6, 7].map(i => ({strategy: i, closedTrades: 0, winRate: null, realized: 0, openPositions: 0})),
  positions: [], dailyHistory: [], recentFills: [], recentSignals: [], lastRuns: [], quoteAudit: [], demoTrades: [],
};

function mockNetwork({ov = overview} = {}) {
  clearStaticCache();
  const calls = [];
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url); calls.push(u);
    if (u.startsWith('/api/dashboard')) {
      const q = new URLSearchParams(u.split('?')[1]);
      const view = q.get('view');
      if (view === 'overview') return {ok: true, json: async () => ov};
      if (view === 'analytics' || view === 'scan') return {ok: true, json: async () => ({available: false, message: 'No completed-bar scan has been saved yet.'})};
      return {ok: true, json: async () => ({total: 0, page: 1, limit: 25, rows: []})};
    }
    return {ok: true, json: async () => JSON.parse(readFileSync(process.cwd() + '/public' + u.split('?')[0], 'utf8'))};
  }));
  return calls;
}

describe('book switch and archive banner', () => {
  it('live book: no book param on API reads, footer names paper_v2, no banner', async () => {
    const calls = mockNetwork();
    window.history.replaceState(null, '', '/paper');
    render(<Portal/>);
    await screen.findByText(/live figures from the paper_v2 ledger/);
    expect(calls.filter(u => u.startsWith('/api/dashboard'))).not.toHaveLength(0);
    expect(calls.filter(u => u.startsWith('/api/dashboard')).every(u => !new URLSearchParams(u.split('?')[1]).has('book'))).toBe(true);
    expect(document.title).toBe('Paper desk · Atlas Desk');
    expect(screen.queryByText(/Archived book/)).toBeNull();
    const live = screen.getByRole('link', {name: /Live · paper_v2/});
    expect(live.getAttribute('aria-current')).toBe('page');
    expect(live.getAttribute('href')).toBe('/paper?book=paper_v2');
    const archive = screen.getByRole('link', {name: /Archive · paper_v1/});
    expect(archive.getAttribute('aria-current')).toBeNull();
    expect(archive.getAttribute('href')).toBe('/paper?book=paper_v1');
    const tester = screen.getByRole('link', {name: 'Strategy tester'});
    expect(tester.getAttribute('href')).toBe('/strategy-tester');
  });

  it('archive book: API reads carry book=paper_v1, banner + back link shown, title marked', async () => {
    const calls = mockNetwork();
    window.history.replaceState(null, '', '/paper?book=paper_v1');
    render(<Portal/>);
    await screen.findByText('Archived book (paper_v1, 9–18 Nov)');
    expect(screen.getByText('Read-only history. No trades are added here. The live book is paper_v2.')).not.toBeNull();
    expect(screen.getByRole('link', {name: /Back to live book/}).getAttribute('href')).toBe('/paper?book=paper_v2');
    expect(screen.getByText(/live figures from the paper_v1 ledger/)).not.toBeNull();
    expect(document.title).toBe('Paper desk · Atlas Desk (archive)');
    expect(calls.filter(u => u.startsWith('/api/dashboard'))).not.toHaveLength(0);
    expect(calls.filter(u => u.startsWith('/api/dashboard')).every(u => new URLSearchParams(u.split('?')[1]).get('book') === 'paper_v1')).toBe(true);
    const live = screen.getByRole('link', {name: /Live · paper_v2/});
    expect(live.getAttribute('aria-current')).toBeNull();
    const archive = screen.getByRole('link', {name: /Archive · paper_v1/});
    expect(archive.getAttribute('aria-current')).toBe('page');
    const tester = screen.getByRole('link', {name: 'Strategy tester'});
    expect(tester.getAttribute('href')).toBe('/strategy-tester?book=paper_v1');
  });

  it('clicking the Archive switch opens the archive without a reload', async () => {
    mockNetwork();
    window.history.replaceState(null, '', '/paper');
    render(<Portal/>);
    await screen.findByText(/live figures from the paper_v2 ledger/);
    await act(async () => { fireEvent.click(screen.getByRole('link', {name: /Archive · paper_v1/})); });
    expect(window.location.search).toBe('?book=paper_v1');
    await screen.findByText('Archived book (paper_v1, 9–18 Nov)');
  });
});

describe('empty live book renders clear text, never NaN/undefined/Infinity', () => {
  it.each(['/paper', '/strategy-tester', '/strategy-comparison', '/performance', '/stock/RELIANCE'])('empty %s', async (path) => {
    mockNetwork({ov: emptyOverview});
    window.history.replaceState(null, '', path);
    render(<Portal/>);
    await screen.findByText(/live figures from the paper_v2 ledger/);
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/NaN|undefined|Infinity/);
    expect(text).toMatch(/No |Nothing recorded|not (yet|held)|0 open|empty/i);
  });
});

describe('book switch CSS at 640px and below', () => {
  it('switch wraps in one scrollable row, banner text wraps, tap targets are 44px', () => {
    expect(css).toMatch(/\.ad-book-switch\{[^}]*display:flex/);
    expect(css).toMatch(/\.ad-book-banner\{[^}]*overflow-wrap:anywhere/);
    for (const rule of [/\.ad-book-switch\{[^}]*overflow-x:auto/, /\.ad-book-switch a\{[^}]*min-height:44px/, /\.ad-book-banner\{[^}]*white-space:normal/])
      expect(media).toMatch(rule);
  });
});
