import React from 'react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, within} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {clearStaticCache} from '../src/portal/data';
import {LEDGER_TABS} from '../src/portal/LedgerBrowser';
import {exitPlan} from '../server/exit-plan.mjs';
import {trailLevel} from '../src/exitRules.js';
import {allocation, monthlyPnl, returnDistribution, stopRisk, tradeFrequency} from '../src/portal/ledger-stats';
import {fillStats, groupBy, rangeBounds, sessionSlots, signalKind, tradeStats} from '../src/portal/ledger-stats';

const sleeve = (open, net) => ({cash_paise: 50000000, market_value_paise: 0, equity_paise: 50000000 + net, net_pnl_paise: net, open_positions: open, slots_max: 9});
const overview = {
  readAt: '2026-11-09T11:00:00.000Z', namespace: 'paper_v1', day: '2026-11-09', market: {open: false, label: 'NSE session closed'},
  cappedLedger: {available: true, policy_version: 'freeze_capital_v1', master_capital_paise: 350000000, sleeve_capital_paise: 50000000, sleeve_count: 7, cash_paise: 349000000, market_value_paise: 987655, equity_paise: 349987655, headline_net_pnl_paise: -12345, capital_return_pct: -0.0035, max_positions_per_sleeve: 9,
    sleeves: {1: sleeve(2, -2000), 2: sleeve(0, 0), 3: sleeve(1, 500), 4: sleeve(0, 0), 5: sleeve(1, -845), 6: sleeve(1, 0), 7: sleeve(12, -10000)}},
  totals: {filledOrders: 17, closedTrades: 2, todayFills: 17, openPositions: 17, realized: 15, unrealized: -98.4, total: -83.4, daily: -123.45, staleMarks: 0, missingMarks: 0, pendingSignals: 3},
  closedStats: {count: 2, winRate: 50, averageHoldDays: 1, matchedClosures: 2}, signalsToday: 20,
  performance: [1, 2, 3, 4, 5, 6, 7].map(i => ({strategy: i, closedTrades: 0, winRate: null, realized: 0, openPositions: i === 7 ? 12 : 1})),
  positions: [], dailyHistory: [{day: '2026-11-09', asOf: '2026-11-09T10:40:00Z', daily: -123.45, total: -123.45, realizedClosuresToday: 15, valuationIsLive: false, missingDailyBasis: []}],
  recentFills: Array.from({length: 12}, (_, i) => ({id: 100 + i, strategy: 7, symbol: 'MAHABANK-EQ', side: 'BUY', qty: 1, price: 86.9, filled_at: '2026-11-09T06:00:00Z', reason: 'daily_entry'})),
  recentSignals: [], lastRuns: [{kind: 'monitor', at: '2026-11-09T10:00:00Z', status: 'ok'}], quoteAudit: [], demoTrades: [],
};
const page = rows => ({total: rows.length, page: 1, limit: 25, rows, market: overview.market});
const ROWS = {
  positions: [{strategy: 7, symbol: 'MAHABANK-EQ', industry: 'Financial Services', qty: 1, entry_price: 86.9, cost_basis: 86.9, mark_price: 87.1, market_value: 87.1, unrealized_pnl: 0.2, unrealized_pct: 0.23, high_water: 87, mark_state: 'stale', mark_age_seconds: 4000, mark_at: '2026-11-09T09:59:00Z', entry_at: '2026-11-09T06:00:00Z', updated_at: '2026-11-09T09:59:00Z',
      exit_plan: exitPlan({strategy: 7, qty: 1, entry_price: 86.9, high_water: 87, mark_price: 87.1, mark_state: 'stale'}), gross_pnl_paise: 20, entry_charges_paise: 1800, est_exit_charges_paise: 1850, charges_paise: 3650, net_unrealized_paise: -3630, net_return_pct: -41.77, break_even: 123.4, charges_note: 'Entry: recorded fee row.', pending_exit: false, entry_pause: {paused: false}, entry_signal: {signal_id: 'sig-7', bar_date: '2026-11-06', reason: 'daily_entry:beta_momentum_v1'}},
    {strategy: 1, symbol: 'ABB-EQ', qty: 1, entry_price: 100, cost_basis: 100, mark_price: null, market_value: null, unrealized_pnl: null, unrealized_pct: null, mark_state: 'missing', mark_age_seconds: null, entry_at: '2026-11-09T06:00:00Z',
      exit_plan: exitPlan({strategy: 1, qty: 1, entry_price: 100, high_water: 100, mark_price: null}, {bars: []}), gross_pnl_paise: null, charges_paise: null, net_unrealized_paise: null, break_even: null, charges_note: 'No fee row is recorded for the entry fill (legacy fill), so charges are unavailable, not zero.'}],
  fills: [{id: 7, strategy: 3, symbol: 'CUPID-EQ', side: 'SELL', qty: 2, price: 120, quote_at: '2026-11-09T05:00:00Z', filled_at: '2026-11-09T05:00:01Z', reason: 'daily_exit:2026-11-06T10:00:00+05:30', realized_pnl: 20, cashflow: 240,
    costs: {turnover_paise: 24000, charges_paise: 45, gross_cashflow_paise: 24000, net_cashflow_paise: 23955, policy_version: 'FYERS_v2', fees: {brokerage: 20, stt: 24, exchange: 1, sebi: 0, ipft: 0, clearing: 0, stamp: 0, gst: 0, dp: 0, dp_gst: 0, total: 45}}},
    {id: 6, strategy: 3, symbol: 'CUPID-EQ', side: 'BUY', qty: 2, price: 110, quote_at: '2026-11-06T05:00:00Z', filled_at: '2026-11-06T05:00:01Z', reason: 'daily_entry', realized_pnl: 0, cashflow: -220, costs: null}],
  trades: [{id: 7, strategy: 3, symbol: 'CUPID-EQ', industry: 'Healthcare', qty: 2, entry_price: 110, exit_price: 120, realized_pnl: 20, pnl_pct: 9.09, charges_paise: null, net_pnl_paise: null, entry_fill_id: 6, entry_matched: true, opened_at: '2026-11-06T05:00:01Z', closed_at: '2026-11-09T05:00:01Z', exit_quote_at: '2026-11-09T05:00:00Z', hold_hours: 72, exit_reason: 'daily_exit:2026-11-06T10:00:00+05:30', entry_costs: null, exit_costs: null},
    {id: 9, strategy: 5, symbol: 'ACC-EQ', industry: 'Construction Materials', qty: 1, entry_price: 50, exit_price: 45, realized_pnl: -5, pnl_pct: -10, charges_paise: 30, net_pnl_paise: -530, entry_fill_id: 8, entry_matched: true, opened_at: '2026-11-09T04:00:00Z', closed_at: '2026-11-09T06:00:00Z', hold_hours: 2, exit_reason: 'observed_LTP_below_close_highwater_stop'}],
  signals: [{id: 'p1', strategy: 2, symbol: 'ABB-EQ', side: 'BUY', status: 'pending', reason: 'daily_entry:beta_momentum_v1', bar_date: '2026-11-09', eligible_date: '2026-11-11', created_at: '2026-11-09T10:40:00Z', fill_qty: null, fill_price: null, filled_at: null}],
  runs: [{kind: 'scan', at: '2026-11-09T06:57:00Z', status: 'partial', summary: {signals_queued: 17, latest_qualifiers: 40, universe_symbols: 750}}],
  quotes: [{symbol: 'ABB-EQ', observed_at: '2026-11-09T09:01:00Z', quote_at: '2026-11-09T09:00:00Z', price: 100, accepted: 0, reason: 'stale_or_future_quote', source: 'FYERS_QUOTES', lag_seconds: 60}],
  events: [{event_type: 'risk_hit', severity: 'INFO', outcome: 'ok', strategy: 7, symbol: 'MAHABANK-EQ', bar_date: '2026-11-06', source_at: '2026-11-09T05:00:00Z', price: 85.5, threshold: 86, rule: 'observed_stop_crossing'}],
  costs: [{fill_id: 7, day: '2026-11-09', strategy: 3, symbol: 'CUPID-EQ', side: 'SELL', qty: 2, price: 120, filled_at: '2026-11-09T05:00:01Z', turnover_paise: 24000, charges_paise: 45, net_cashflow_paise: 23955, sleeve_cash_after_paise: 50001000, policy_version: 'FYERS_v2'}],
  credits: [], dataaudit: [{symbol: 'ABB-EQ', bar_date: '2026-11-06', reason: 'invalid_Fyers_OHLC', observed_at: '2026-11-09T03:00:00Z'}],
};

function mock({hold} = {}) {
  clearStaticCache();
  const calls = [];
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    const u = String(url); calls.push(u);
    const q = new URLSearchParams(u.split('?')[1]);
    const view = q.get('view');
    if (view === 'overview') return {ok: true, json: async () => overview};
    if (hold?.view === view) await hold.promise;
    let rows = ROWS[view] ?? [];
    if (view === 'signals' && q.get('status') && q.get('status') !== 'pending') rows = [];
    if (view === 'runs') return {ok: true, json: async () => ({...page(rows), health: {latestByKind: [{kind: 'scan', at: '2026-11-09T06:57:00Z', status: 'partial', runs: 1}], lastAcceptedQuoteAt: '2026-11-09T09:59:00Z', quotedSymbols: 17}})};
    if (view === 'costs') return {ok: true, json: async () => ({...page(rows), available: true, balanceNote: 'Sleeve cash after each fill = sleeve capital + running net cash flow.'})};
    if (['analytics', 'scan'].includes(view)) return {ok: true, json: async () => ({available: false, message: 'n/a'})};
    return {ok: true, json: async () => page(rows)};
  }));
  return calls;
}
const at = p => window.history.replaceState(null, '', p);
const tab = name => screen.getByRole('tab', {name: new RegExp(`^${name}`)});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); at('/'); });

describe('paper ledger tab parity', () => {
  it('offers every reference tab plus the engine audit tabs', async () => {
    mock(); at('/paper'); render(<Portal/>);
    await screen.findByText('Browse the paper ledger');
    for (const [, label] of LEDGER_TABS) expect(tab(label)).toBeInTheDocument();
    expect(LEDGER_TABS.map(t => t[0])).toEqual(expect.arrayContaining(['positions', 'orders', 'history', 'analytics', 'daily', 'alerts', 'capital', 'watchlist', 'fills', 'signals', 'runs', 'quotes', 'dataaudit']));
  });

  it('shows a loading state on tab switch instead of the previous tab’s rows under the new headings', async () => {
    let release; const promise = new Promise(r => { release = r; });
    mock({hold: {view: 'fills', promise}}); at('/paper'); render(<Portal/>);
    await screen.findByText('Monitoring / mark');
    expect(screen.getByText(/stale · 67m \(market closed\)/)).toBeInTheDocument();
    expect(within(screen.getByRole('table', {name: 'positions'})).getAllByText('missing')[0]).toBeInTheDocument();
    fireEvent.click(tab('Fills'));
    expect(await screen.findByText('Loading fills…')).toBeInTheDocument();
    expect(screen.queryByText('Monitoring / mark')).toBeNull();
    expect(screen.queryByText('Fill price')).toBeNull();
    await act(async () => { release(); });
    await screen.findByText('Fill price');
    const table = screen.getByRole('table', {name: 'fills'});
    expect(within(table).getAllByText('CUPID')).toHaveLength(2);
    expect(within(table).queryByText('MAHABANK')).toBeNull();
    fireEvent.click(within(table).getAllByRole('row')[1]);
    expect(await screen.findByRole('region', {name: 'Fill details'})).toBeInTheDocument();
    expect(screen.getByText('STT')).toBeInTheDocument();
  });

  it('positions show the S7 trail stop, rule text for S1, no target, no R:R, charges and read-only actions', async () => {
    mock(); at('/paper'); render(<Portal/>);
    await screen.findByText('Monitoring / mark');
    const table = screen.getByRole('table', {name: 'positions'});
    for (const h of ['Signal', 'Stop', 'Target', 'R:R', 'Break-even', 'Gross P&L', 'Taxes & charges', 'Net unrealized', '% Net return', 'Status', 'Open time', 'Action']) expect(within(table).getByRole('columnheader', {name: h})).toBeInTheDocument();
    expect(within(table).getByText('₹' + trailLevel(87).toFixed(2))).toBeInTheDocument();
    expect(within(table).getByText(/No fixed stop · Close below the prior 10-session low/)).toBeInTheDocument();
    expect(within(table).getAllByText(/No fixed target · exit:/)).toHaveLength(2);
    expect(within(table).getAllByText('— (no target)')).toHaveLength(2);
    expect(within(table).getByText('₹123.40')).toBeInTheDocument();
    expect(within(table).getAllByText('Exit / square off · read-only')).toHaveLength(2);
    expect(within(table).queryByRole('button', {name: /exit|square/i})).toBeNull();
    expect(screen.getByText('Total capital')).toBeInTheDocument();
    fireEvent.click(within(table).getAllByRole('row')[1]);
    const detail = await screen.findByRole('region', {name: 'Position details'});
    expect(within(detail).getByText(/risk distance, not risk:reward/)).toBeInTheDocument();
    expect(within(detail).getByText(/Strategy 07 trail ·/)).toBeInTheDocument();
  });

  it('recent fills says it is capped and opens the complete Fills tab', async () => {
    mock(); at('/paper'); render(<Portal/>);
    await screen.findByText('Latest 12 of 17 fills');
    fireEvent.click(screen.getByRole('button', {name: 'Open all 17 fills'}));
    expect(tab('Fills')).toHaveAttribute('aria-selected', 'true');
    await screen.findByText('Fill price');
  });

  it('orders, history, alerts, runs, quotes and data audit show complete, correctly labelled detail', async () => {
    const calls = mock(); at('/paper'); render(<Portal/>);
    await screen.findByText('Browse the paper ledger');
    fireEvent.click(tab('Orders'));
    await screen.findByText('Expected execution');
    expect(screen.getAllByText('2026-11-11 session').length).toBeGreaterThan(0);
    for (const h of ['Order ID', 'Signal', 'Strategy', 'Order type', 'Quantity', 'Order price', 'Current price', 'Order status', 'Created time', 'Execution time', 'Remarks', 'Action']) expect(screen.getByRole('columnheader', {name: h})).toBeInTheDocument();
    expect(screen.getByText('Edit / cancel · read-only')).toBeInTheDocument();
    expect(calls.some(c => c.includes('view=signals') && c.includes('status=pending'))).toBe(true);
    fireEvent.click(tab('History'));
    await screen.findByRole('columnheader', {name: 'Net P&L'});
    for (const h of ['Break-even', 'Gross P&L', 'Charges', 'Net %', 'Signal', 'Strategy', 'Score', 'Opened', 'Closed', 'Exit reason', 'Hold']) expect(screen.getByRole('columnheader', {name: h})).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('table', {name: 'history'})).getAllByRole('row')[1]);
    expect(await screen.findByRole('region', {name: 'Trade exit details'})).toBeInTheDocument();
    expect(screen.getAllByText('Completed-bar daily exit rule').length).toBeGreaterThan(0);
    fireEvent.click(tab('Alerts'));
    await screen.findByText('Threshold');
    expect(screen.getByText('Price alerts you set yourself are unavailable.')).toBeInTheDocument();
    fireEvent.click(tab('Engine runs'));
    await screen.findByText(/17 signals queued · 40 qualifiers · 750 symbols scanned/);
    expect(screen.getByText('Last scan')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Run kind'), {target: {value: 'scan'}});
    await screen.findByText(/17 signals queued/);
    expect(calls.some(c => c.includes('view=runs') && c.includes('kind=scan'))).toBe(true);
    fireEvent.click(tab('Quote audit'));
    await screen.findByText('Lag');
    expect(screen.getByText('FYERS_QUOTES')).toBeInTheDocument();
    fireEvent.click(tab('Data audit'));
    await screen.findByText('invalid_Fyers_OHLC');
    fireEvent.click(tab('Watchlist'));
    expect(screen.getByText('Watchlist unavailable')).toBeInTheDocument();
  });

  it('analytics, daily and capital are computed from the complete record and say what is unavailable', async () => {
    const calls = mock(); at('/paper'); render(<Portal/>);
    await screen.findByText('Browse the paper ledger');
    fireEvent.click(tab('Analytics'));
    await screen.findByText('Trade stats');
    expect(calls.some(c => c.includes('view=trades') && c.includes('limit=100'))).toBe(true);
    const tileOf = label => screen.getAllByText(label).map(e => e.closest('.ad-tile')).find(Boolean);
    expect(tileOf('Win rate')).toHaveTextContent('50.0%');
    expect(tileOf('Profit factor')).toHaveTextContent('4.00');
    expect(screen.getByText(/Max drawdown \/ Sharpe \/ Sortino/)).toBeInTheDocument();
    for (const t of ['Monthly P&L (realized, gross, by exit month)', 'Win / loss distribution (gross return % per closed trade)', 'Trade frequency (per IST session day)']) expect(screen.getByText(t)).toBeInTheDocument();
    expect(await screen.findByText('S07 open risk to trail stops')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Custom'}));
    expect(screen.getByLabelText('Custom range from')).toBeInTheDocument();
    fireEvent.click(tab('Daily'));
    await screen.findByText('2026-11-09 snapshot');
    expect(screen.getByText('Daily journal')).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Export session CSV'})).toBeInTheDocument();
    fireEvent.click(tab('Capital'));
    await screen.findByText('Starting capital');
    expect(screen.getByText('Sleeve cash after')).toBeInTheDocument();
    expect(screen.getAllByText(/-0\.0035%/).length).toBeGreaterThan(0);
  });
});

describe('ledger statistics', () => {
  it('monthly, distribution, frequency, allocation and stop risk use only real rows', () => {
    const t = [{closed_at: '2026-10-30T05:00:00Z', realized_pnl: 10, pnl_pct: 6}, {closed_at: '2026-11-03T05:00:00Z', realized_pnl: -4, pnl_pct: -3}, {closed_at: '2026-11-04T05:00:00Z', realized_pnl: null, pnl_pct: null}];
    expect(monthlyPnl(t)).toEqual([{month: '2026-10', trades: 1, pnl: 10}, {month: '2026-11', trades: 1, pnl: -4}]);
    const d = returnDistribution(t); expect(d.reduce((a, b) => a + b.trades, 0)).toBe(2); expect(d.find(b => b.label === '> 5%').trades).toBe(1); expect(d.find(b => b.label === '−5% to −2%').trades).toBe(1);
    expect(tradeFrequency(t.slice(0, 1), [{filled_at: '2026-10-30T04:00:00Z'}])).toEqual([{day: '2026-10-30', closed: 1, fills: 1}]);
    const a = allocation([{industry: 'X', market_value: 300}, {industry: 'Y', market_value: 100}, {industry: 'Y', market_value: null}], p => p.industry);
    expect(a.total).toBe(400); expect(a.missing).toBe(1); expect(a.rows[0]).toMatchObject({key: 'X', share: 75});
    const plan = exitPlan({strategy: 7, qty: 2, entry_price: 100, high_water: 100, mark_price: 95});
    const r = stopRisk([{symbol: 'A', exit_plan: plan}, {symbol: 'B', exit_plan: exitPlan({strategy: 2, qty: 1, entry_price: 1, high_water: 1, mark_price: 1})}]);
    expect(r).toMatchObject({positions: 1, priced: 1, withMark: 1}); expect(r.openRisk).toBeCloseTo((95 - trailLevel(100)) * 2); expect(r.entryRisk).toBeCloseTo((100 - trailLevel(100)) * 2);
  });

  it('session CSV contains only loaded ledger rows', async () => {
    const {sessionCsv} = await import('../src/portal/LedgerBrowser');
    const csv = sessionCsv('2026-11-09', ROWS.fills.slice(0, 1), ROWS.trades.slice(0, 1));
    expect(csv.trim().split('\n')).toHaveLength(3);expect(csv).toContain('fill,2026-11-09T05:00:01Z,CUPID-EQ,3,SELL,2,120');expect(csv).toContain('trade,');
  });
  it('computes trade stats, streaks and groups without inventing missing values', () => {
    const t = [
      {symbol: 'A', strategy: 1, realized_pnl: 10, closed_at: '2026-11-03T05:00:00Z', opened_at: '2026-11-03T04:00:00Z', hold_hours: 1, entry_price: 100, exit_price: 110, qty: 1, entry_matched: true, pnl_pct: 10, net_pnl_paise: 900, charges_paise: 100},
      {symbol: 'A', strategy: 1, realized_pnl: 5, closed_at: '2026-11-04T05:00:00Z', opened_at: '2026-11-02T04:00:00Z', hold_hours: 49, entry_price: 100, exit_price: 105, qty: 1, entry_matched: true, pnl_pct: 5, net_pnl_paise: null, charges_paise: null},
      {symbol: 'B', strategy: 2, realized_pnl: -8, closed_at: '2026-11-05T05:00:00Z', hold_hours: null, exit_price: 92, qty: 1, entry_matched: false},
    ];
    const s = tradeStats(t);
    expect(s).toMatchObject({count: 3, wins: 2, losses: 1, grossPnl: 7, netPnl: null, netMissing: 2, charges: null, longestWin: 2, longestLoss: 1, currentStreak: -1, sameDay: 1, unmatched: 1, largestProfit: 10, largestLoss: -8});
    expect(s.profitFactor).toBeCloseTo(15 / 8);
    expect(s.mostProfitableSymbol).toMatchObject({key: 'A', pnl: 15});
    expect(groupBy(t, r => r.strategy).map(g => [g.key, g.trades, g.pnl])).toEqual([[1, 2, 15], [2, 1, -8]]);
    expect(tradeStats([])).toMatchObject({count: 0, winRate: null, grossPnl: null, netPnl: null, profitFactor: null});
  });
  it('builds IST ranges and session slots', () => {
    expect(rangeBounds('today', '2026-11-11')).toEqual(['2026-11-11', '2026-11-11']);
    expect(rangeBounds('week', '2026-11-11')).toEqual(['2026-11-09', '2026-11-11']);
    expect(rangeBounds('month', '2026-11-11')).toEqual(['2026-11-01', '2026-11-11']);
    expect(rangeBounds('last_month', '2026-11-11')).toEqual(['2026-10-01', '2026-10-31']);
    expect(rangeBounds('all', '2026-11-11')).toEqual([null, null]);
    expect(sessionSlots([{filled_at: '2026-11-09T03:50:00Z', side: 'BUY'}, {filled_at: '2026-11-09T04:10:00Z', side: 'SELL', realized_pnl: 4}]))
      .toEqual([{slot: '09:00–10:00', fills: 2, buys: 1, sells: 1, pnl: 4}]);
    expect(fillStats([{side: 'BUY', price: 10, qty: 2, costs: null}])).toMatchObject({count: 1, turnover: null, charges: null, missingCosts: 1, notional: 20});
    expect(signalKind('daily_entry:x')).toBe('Entry');
    expect(signalKind('observed_LTP_below_close_highwater_stop')).toBe('Stop exit');
  });
});
