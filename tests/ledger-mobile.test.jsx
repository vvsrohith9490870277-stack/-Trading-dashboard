// Narrow-viewport contract for the ledger (~375px phones). jsdom has no layout engine, so this pins the
// structure and the CSS rules that keep tables scrollable with a pinned first column, tabs in one
// scrollable row, two-column tiles and stacked details. A visual 375px check was done in headless Chrome.
import React from 'react';
import {readFileSync} from 'node:fs';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, render, screen} from '@testing-library/react';
import Portal from '../src/portal/Portal';
import {clearStaticCache} from '../src/portal/data';

const css = readFileSync('src/portal/portal.css', 'utf8');
const media = (() => { const out = []; const re = /@media \(max-width:640px\)\{/g; while (re.exec(css)) { let depth = 1, i = re.lastIndex; while (depth && i < css.length) { if (css[i] === '{') depth++; if (css[i] === '}') depth--; i++; } out.push(css.slice(re.lastIndex, i - 1)); } return out.join('\n'); })();
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('phone CSS: pinned first column, scrollable one-row tabs, 2-col tiles, stacked details, no clipped labels', () => {
  expect(css).toMatch(/\.ad-ledger-palette \.ad-table thead th:first-child,\.ad-ledger-palette \.ad-table tbody td:first-child\{position:sticky;left:0/);
  expect(css).toMatch(/\.ad-table-scroll\{overflow-x:auto/);
  for (const rule of [/\.ad-ledger-tabs \.ad-tabs\{flex-wrap:nowrap/, /\.ad-ledger-tabs\{overflow-x:auto/, /\.ad-tiles\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/, /\.ad-detail-grid\{grid-template-columns:1fr\}/, /\.ad-kv div\{grid-template-columns:1fr/, /\.ad-tile-label,[^{]*\{overflow-wrap:anywhere;white-space:normal\}/, /\.ad-input\{[^}]*min-height:36px/])
    expect(media).toMatch(rule);
});

it('every ledger table is rendered inside a horizontal scroller within the palette-scoped ledger section', async () => {
  clearStaticCache(); vi.stubGlobal('scrollTo', vi.fn());
  window.innerWidth = 375; window.dispatchEvent(new Event('resize'));
  vi.stubGlobal('fetch', vi.fn(async url => {
    const view = new URLSearchParams(String(url).split('?')[1]).get('view');
    if (view === 'overview') return {ok: true, json: async () => ({readAt: '2026-11-09T11:00:00Z', day: '2026-11-09', market: {open: false}, totals: {openPositions: 1}, performance: [], positions: [], dailyHistory: [], recentFills: [], recentSignals: [], lastRuns: [], quoteAudit: [], demoTrades: [], cappedLedger: {master_capital_paise: 1, sleeves: {}}})};
    return {ok: true, json: async () => ({total: 1, page: 1, limit: 25, rows: view === 'positions' ? [{strategy: 2, symbol: 'ABB-EQ', qty: 1, entry_price: 1, mark_state: 'missing', exit_plan: null}] : []})};
  }));
  window.history.replaceState(null, '', '/paper'); render(<Portal/>);
  await screen.findByText('Monitoring / mark');
  const section = document.getElementById('ad-ledger');
  expect(section.classList.contains('ad-ledger-palette')).toBe(true);
  const table = screen.getByRole('table', {name: 'positions'});
  expect(table.closest('.ad-table-scroll')).not.toBeNull();
  expect(section.querySelector('.ad-ledger-tabs [role="tablist"], .ad-ledger-tabs')).not.toBeNull();
});
