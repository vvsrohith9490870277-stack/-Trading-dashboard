import React from 'react';
import {readFileSync} from 'node:fs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, within} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {clearStaticCache} from '../src/portal/data';
import {closedStats, fillCharges, isDemo, openStats, paperAge, paperOverlap, paperStats, researchStats, withoutDemo} from '../src/portal/paper-vs-research';

const history = JSON.parse(readFileSync(process.cwd() + '/public/data/historical.json', 'utf8'));
const pos = (strategy, symbol, o = {}) => ({strategy, symbol, qty: 2, entry_price: 100, mark_price: 101, cost_basis: 200, market_value: 202, unrealized_pnl: 2, net_unrealized_paise: 120, charges_paise: 80, entry_at: '2026-11-09T04:00:00Z', ...o});
const trade = (strategy, symbol, pnl, pctv, o = {}) => ({strategy, symbol, qty: 1, entry_price: 100, exit_price: 100 + pctv, realized_pnl: pnl, pnl_pct: pctv, charges_paise: 50, net_pnl_paise: pnl * 100 - 50, closed_at: '2026-11-09T09:00:00Z', ...o});
const fill = (strategy, symbol, charges, o = {}) => ({id: Math.random(), strategy, symbol, side: 'BUY', qty: 1, price: 100, filled_at: '2026-11-07T04:00:00Z', costs: charges === null ? null : {charges_paise: charges}, ...o});
const demo = {namespace: 'paper_demo_v1', is_test: 1, id: 'dummy_reliance_manual_v1', strategy: 1, symbol: 'RELIANCE-EQ', qty: 1, entry_price: 1490.1, mark_price: 1500, cost_basis: 1490.1, unrealized_pnl: 9.9, realized_pnl: 500, pnl_pct: 30, costs: {charges_paise: 999}};

describe('research vs paper statistics', () => {
  it('computes research and paper separately from their own sources', () => {
    const s1 = history.strategies.find(s => s.id === 1);
    const r = researchStats(s1);
    expect(r.closed).toBe(s1.metrics.closed_trades);
    expect(r.winRate).toBeCloseTo(s1.metrics.win_rate_pct);
    expect(r.mean).toBeCloseTo(s1.metrics.mean_gross_return_pct);
    expect(r.best).toBe(Math.max(...s1.perStock.map(p => p.best).filter(Number.isFinite)));
    expect(r.worst).toBe(Math.min(...s1.perStock.map(p => p.worst).filter(Number.isFinite)));
    const p = paperStats(1, {trades: [trade(1, 'A-EQ', 10, 5), trade(1, 'B-EQ', -4, -2), trade(4, 'C-EQ', 99, 50)], positions: [], fills: []});
    expect(p).toMatchObject({closed: 2, wins: 1, winRate: 50, mean: 1.5, best: 5, worst: -2, realized: 6, realizedNet: 5});
    // Paper figures never pick up research values, and vice versa.
    expect(p.mean).not.toBeCloseTo(r.mean);
    expect(researchStats(s1).closed).toBe(r.closed);
  });

  it('omits win / mean / best / worst when paper has no closed trades (not a 0% win rate)', () => {
    const p = paperStats(7, {trades: [], positions: [pos(7, 'X-EQ')], fills: [fill(7, 'X-EQ', 30)]});
    expect(p.closed).toBe(0);
    expect(p.winRate).toBeNull(); expect(p.mean).toBeNull(); expect(p.best).toBeNull(); expect(p.worst).toBeNull(); expect(p.realized).toBeNull();
    // Open unrealized is reported on its own and is never a closed-trade mean.
    expect(p.unrealized).toBe(2); expect(p.unrealizedPct).toBe(1);
  });

  it('a closed trade without a matched entry quantity has no % but still counts for win rate', () => {
    const c = closedStats([trade(1, 'A-EQ', 10, null)]);
    expect(c).toMatchObject({closed: 1, winRate: 100, pctCount: 0, mean: null, best: null});
  });

  it('excludes the RELIANCE demo from every total', () => {
    expect(isDemo(demo)).toBe(true);
    expect(withoutDemo([demo, pos(1, 'A-EQ')])).toHaveLength(1);
    const p = paperStats(1, {trades: [demo], positions: [demo, pos(1, 'A-EQ')], fills: [demo]});
    expect(p.closed).toBe(0); expect(p.open).toBe(1); expect(p.unrealized).toBe(2); expect(p.chargesPaid).toBeNull();
    expect(paperOverlap([1, 7], {positions: [demo, {...demo, strategy: 7}]})).toEqual([]);
  });

  it('reports charges and net only when fee rows exist', () => {
    expect(fillCharges([fill(1, 'A-EQ', 25), fill(1, 'A-EQ', 75)])).toEqual({fills: 2, charges: 1, missing: 0});
    expect(fillCharges([fill(1, 'A-EQ', null)])).toEqual({fills: 1, charges: null, missing: 1});
    expect(fillCharges([fill(1, 'A-EQ', 25), fill(1, 'A-EQ', null)])).toMatchObject({charges: 0.25, missing: 1});
    expect(openStats([pos(1, 'A-EQ')]).unrealizedNet).toBe(1.2);
    expect(openStats([pos(1, 'A-EQ'), pos(1, 'B-EQ', {net_unrealized_paise: null, charges_paise: null})])).toMatchObject({unrealizedNet: null, netMissing: 1, openCharges: null, unrealized: 4});
    expect(closedStats([trade(1, 'A-EQ', 10, 5, {net_pnl_paise: null})]).realizedNet).toBeNull();
    expect(paperStats(1, {sleeve: {net_pnl_paise: -6503}}).sleeveNet).toBe(-65.03);
    expect(paperStats(1, {}).sleeveNet).toBeNull();
  });

  it('overlap only lists a symbol that 2+ selected strategies touched on paper', () => {
    const data = {positions: [pos(1, 'ANGELONE-EQ'), pos(7, 'ANGELONE-EQ', {qty: 1, entry_price: 110, cost_basis: 110, unrealized_pnl: -3, net_unrealized_paise: -350}), pos(7, 'SOLO-EQ'), pos(4, 'MAHABANK-EQ')],
      trades: [trade(4, 'TRADED-EQ', 5, 2)], fills: [fill(1, 'TRADED-EQ', 40), fill(4, 'TRADED-EQ', 60)]};
    const o = paperOverlap([1, 7], data);
    expect(o.map(x => x.symbol)).toEqual(['ANGELONE-EQ']);
    expect(o[0].strategies).toEqual([1, 7]);
    expect(o[0].combined).toMatchObject({qty: 3, unrealized: -1, unrealizedNet: -2.3});
    expect(o[0].combined.unrealizedPct).toBeCloseTo(-1 / 310 * 100);
    // MAHABANK is held by 4 only within the 1/7 selection, so it is not an overlap.
    expect(paperOverlap([1, 7, 4], data).map(x => x.symbol)).toEqual(['ANGELONE-EQ', 'TRADED-EQ']);
    const t = paperOverlap([1, 4], data).find(x => x.symbol === 'TRADED-EQ');
    expect(t.combined).toMatchObject({closed: 1, realized: 5, chargesPaid: 1});
    expect(paperOverlap([2, 3], data)).toEqual([]);
  });

  it('derives the paper book age from fills in IST', () => {
    expect(paperAge([fill(1, 'A', 1, {filled_at: '2026-11-07T04:00:00Z'}), fill(1, 'A', 1, {filled_at: '2026-11-09T19:00:00Z'})])).toEqual({first: '2026-11-07', last: '2026-11-10', sessions: 2});
    expect(paperAge([])).toEqual({first: null, last: null, sessions: 0});
  });
});

const sleeve = (open, net) => ({cash_paise: 100000000, market_value_paise: 0, equity_paise: 100000000 + net, net_pnl_paise: net, open_positions: open, slots_max: 9});
const overview = {readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v1', day: '2026-11-09', market: {open: false, label: 'closed'},
  cappedLedger: {available: true, master_capital_paise: 700000000, sleeves: {1: sleeve(1, -2000), 4: sleeve(0, 0), 7: sleeve(1, -10000)}},
  totals: {openPositions: 2}, performance: [], positions: [], dailyHistory: [], recentFills: [], recentSignals: [], lastRuns: [], quoteAudit: [], demoTrades: []};
const pages = {
  positions: [pos(1, 'ANGELONE-EQ'), pos(7, 'ANGELONE-EQ', {unrealized_pnl: -3, net_unrealized_paise: -350})],
  fills: [fill(1, 'ANGELONE-EQ', 251), fill(7, 'ANGELONE-EQ', 251)], trades: [],
};
function mock() {
  clearStaticCache();
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url);
    if (u.startsWith('/api/dashboard')) {
      const view = new URLSearchParams(u.split('?')[1]).get('view');
      if (view === 'overview') return {ok: true, json: async () => overview};
      if (pages[view]) return {ok: true, json: async () => ({total: pages[view].length, page: 1, limit: 100, rows: pages[view]})};
      return {ok: true, json: async () => ({total: 0, rows: []})};
    }
    return {ok: true, json: async () => JSON.parse(readFileSync(process.cwd() + '/public' + u.split('?')[0], 'utf8'))};
  }));
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('compare screen: research vs paper', () => {
  it('shows research and paper columns side by side, paper overlap, and all-seven view', async () => {
    mock(); window.history.replaceState(null, '', '/strategy-comparison?ids=1,7');
    render(<Portal/>);
    await screen.findByText('Research vs paper, same metrics');
    const table = (await screen.findAllByRole('table')).find(t => within(t).queryByText(/Research · saved backtest/));
    await within(table).findAllByText('1 / 9');
    expect(within(table).getAllByRole('row')).toHaveLength(4); // 2 header rows + strategies 1 and 7
    expect(screen.getByText(/first fill was on 2026-11-07/)).toBeInTheDocument();
    await screen.findByText('ANGELONE combined');
    expect(screen.getByText('Signal agreement (research scan, not paper performance)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Paper views: all seven'}));
    expect(window.location.search).toContain('paper=all');
    const t7 = (await screen.findAllByRole('table')).find(t => within(t).queryByText(/Research · saved backtest/));
    expect(within(t7).getAllByRole('row')).toHaveLength(9);
  });
});
