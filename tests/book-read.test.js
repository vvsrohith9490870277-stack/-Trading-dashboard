// paper_v2 cutover (Task 2): book allowlist, SELECT-only reads per book, cache separation.
// Isolated local libSQL only. Never connects to Turso or creates production fills.
import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {createClient} from '@libsql/client/node';
import {readFileSync, rmSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {readDashboard, parseOptions, readConfig, NAMESPACE} from '../server/data.mjs';
import {BOOKS, ARCHIVE_BOOK, ACTIVE_BOOK, isBook} from '../server/books.mjs';
import {LEDGER_VIEWS} from '../server/ledger-read.mjs';
import {createHandler} from '../api/dashboard.js';
import cfg from '../automation/paper-trading/config.json' with {type: 'json'};

const now = new Date('2026-11-11T10:00:00+05:30');
const VIEWS = ['overview', 'fills', 'positions', 'signals', 'runs', 'quotes', 'history', 'demo', 'analytics', 'scan', 'qualifiers', 'prefilter', ...LEDGER_VIEWS];

function optionsFor(view, extra = {}) {
  if (view === 'history') return parseOptions({view, symbol: 'RELIANCE-EQ', ...extra});
  if (view === 'qualifiers') return parseOptions({view, strategy: '2', ...extra});
  return parseOptions({view, ...extra});
}

// Empty-DB spy: returns shape-correct empty rows so every view completes.
function executeFixture() {
  const calls = [];
  const execute = async (sql, args = []) => {
    calls.push({sql, args: [...args]});
    if (sql.includes('AS filled_orders')) return [{filled_orders: 0, realized: 0, closed_trades: 0, today_fills: 0}];
    if (sql.includes('AS watermarks')) return [{watermarks: 0, excluded_bars: 0, pending_signals: 0, signals_today: 0}];
    if (sql.includes('AS closed_count')) return [{closed_count: 0, wins: 0, average_hold_days: null, matched_closures: 0}];
    if (sql.includes('FROM paper_fill_costs WHERE namespace=?')) return [{total: 0, cashgross: 0, cashnet: 0, prior: 0, priorbuy: 0, brokerage: 0, exchange: 0, sebi: 0, ipft: 0, clearing: 0, stt: 0, stamp: 0, gst: 0, dp: 0, dp_gst: 0}];
    if (sql.includes('COUNT(*) AS total')) return [{total: 0}];
    if (sql.includes('COALESCE(SUM(CAST(ROUND(realized_pnl')) return [{total: 0}];
    return [];
  };
  return {calls, execute};
}

describe('book allowlist', () => {
  it('active book follows config.json', () => {
    expect(ACTIVE_BOOK).toBe('paper_v2');
    expect(cfg.namespace).toBe('paper_v2');
    expect(ACTIVE_BOOK).toBe(cfg.namespace);
    expect(NAMESPACE).toBe(ACTIVE_BOOK);
    expect(BOOKS).toEqual(['paper_v2', 'paper_v1']);
    expect(ARCHIVE_BOOK).toBe('paper_v1');
    expect(isBook('paper_v2')).toBe(true);
    expect(isBook('paper_v1')).toBe(true);
    expect(isBook('paper_demo_v1')).toBe(false);
    expect(isBook('other')).toBe(false);
    expect(isBook('')).toBe(false);
    expect(isBook(null)).toBe(false);
    expect(isBook(['paper_v1'])).toBe(false);
  });

  it('parseOptions accepts paper_v2/paper_v1 and defaults to paper_v2', () => {
    expect(parseOptions({}).book).toBe('paper_v2');
    expect(parseOptions({book: 'paper_v2'}).book).toBe('paper_v2');
    expect(parseOptions({book: 'paper_v1'}).book).toBe('paper_v1');
    expect(parseOptions({view: 'fills'}).book).toBe('paper_v2');
  });

  it.each([
    {book: 'paper_demo_v1'},
    {book: 'other'},
    {book: 'paper_v1 OR 1=1'},
    {book: ['x']},
    {namespace: 'paper_v1'},
    {book: ''},
    {book: 'PAPER_V1'},
  ])('rejects book input %j', q => expect(() => parseOptions(q)).toThrow('BAD_REQUEST'));

  it('PAPER_NAMESPACE never selects the book', () => {
    const env = {TURSO_DATABASE_URL: 'libsql://fixture', TURSO_AUTH_TOKEN: 'token'};
    expect(() => readConfig({...env, PAPER_NAMESPACE: 'paper_v1'})).not.toThrow();
    expect(() => readConfig({...env, PAPER_NAMESPACE: 'paper_v2'})).not.toThrow();
    expect(() => readConfig({...env, PAPER_NAMESPACE: 'other'})).toThrow('CONFIG_NAMESPACE');
    expect(() => readConfig({...env, PAPER_NAMESPACE: ' paper_v1 '})).not.toThrow();
  });
});

describe('book-bound reads are SELECT-only', () => {
  it.each(VIEWS)('view %s: SELECT-only, default book binds no paper_v1, archive binds no paper_v2', async view => {
    for (const extra of [{}, {book: 'paper_v1'}]) {
      const f = executeFixture();
      await readDashboard(optionsFor(view, extra), f.execute, now);
      expect(f.calls.length).toBeGreaterThan(0);
      for (const c of f.calls) {
        expect(c.sql).toMatch(/^SELECT\s/i);
      }
      if (!extra.book) {
        expect(f.calls.flatMap(c => c.args)).not.toContain('paper_v1');
        // demo ignores book by design (binds only paper_demo_v1); covered below.
        if (view !== 'demo') expect(f.calls.flatMap(c => c.args)).toContain('paper_v2');
      } else {
        expect(f.calls.flatMap(c => c.args)).not.toContain('paper_v2');
        if (view !== 'demo') expect(f.calls.flatMap(c => c.args)).toContain('paper_v1');
      }
    }
  });

  it('view=demo binds only paper_demo_v1 for both books', async () => {
    for (const extra of [{}, {book: 'paper_v1'}]) {
      const f = executeFixture();
      const r = await readDashboard(optionsFor('demo', extra), f.execute, now);
      expect(r.excludedFromPerformance).toBe(true);
      const bound = f.calls.flatMap(c => c.args);
      expect(bound).toEqual(['paper_demo_v1']);
    }
  });
});

describe('real local libSQL fixture: empty paper_v2, seeded paper_v1', () => {
  let db, path, select;
  beforeEach(async () => {
    path = process.cwd() + '/automation/paper-trading/tests/test_book_' + randomUUID().replaceAll('-', '') + '.sqlite';
    db = createClient({url: 'file:' + path});
    await db.batch(readFileSync('automation/paper-trading/schema.sql', 'utf8').split(';').filter(x => x.trim()).map(sql => ({sql, args: []})), 'write');
    await db.batch(readFileSync('server/demo-schema.sql', 'utf8').split(';').filter(x => x.trim()).map(sql => ({sql, args: []})), 'write');
    select = async (sql, args = []) => { expect(sql).toMatch(/^SELECT\s/i); return (await db.execute({sql, args})).rows; };
    const at = '2026-11-09T09:20:00+05:30';
    await db.execute({sql: 'INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', args: ['paper_v1', 'sig-1', 1, 'RELIANCE-EQ', 'BUY', 1, 2500, at, at, 'fixture', 0, -2500]});
    await db.execute({sql: 'INSERT INTO paper_positions(namespace,strategy,symbol,qty,entry_price,entry_at,high_water,mark_price,mark_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', args: ['paper_v1', 1, 'RELIANCE-EQ', 1, 2500, at, 2500, 2600, '2026-11-11T10:00:00+05:30', at]});
    await db.execute({sql: 'INSERT INTO paper_eod(namespace,day,as_of,report_json) VALUES(?,?,?,?)', args: ['paper_v1', '2026-11-09', '2026-11-09T16:20:00+05:30', '{}']});
    await db.execute({sql: 'INSERT INTO paper_pnl(namespace,day,as_of,report_json) VALUES(?,?,?,?)', args: ['paper_v1', '2026-11-09', '2026-11-09T16:20:00+05:30', '{}']});
  });
  afterEach(() => { db.close(); for (const suffix of ['', '-wal', '-shm']) rmSync(path + suffix, {force: true}); });

  it('active book reads nothing from the paper_v1 archive', async () => {
    for (const view of ['fills', 'positions', 'signals', 'runs', 'quotes']) {
      const r = await readDashboard(parseOptions({view}), select, now);
      expect(r.namespace).toBe('paper_v2');
      expect(r.book).toBe('paper_v2');
      expect(r.isArchive).toBe(false);
      expect(r.total).toBe(0);
      expect(r.rows).toEqual([]);
    }
    const o = await readDashboard(parseOptions(), select, now);
    expect(o.namespace).toBe('paper_v2');
    expect(o.totals.openPositions).toBe(0);
    expect(o.dailyHistory).toEqual([]);
    expect(o.positions).toEqual([]);
    expect(JSON.stringify(o)).not.toContain('RELIANCE-EQ');
  });

  it('archive book returns the seeded paper_v1 rows', async () => {
    const fills = await readDashboard(parseOptions({view: 'fills', book: 'paper_v1'}), select, now);
    expect(fills.namespace).toBe('paper_v1');
    expect(fills.book).toBe('paper_v1');
    expect(fills.isArchive).toBe(true);
    expect(fills.total).toBe(1);
    expect(fills.rows[0].symbol).toBe('RELIANCE-EQ');
    const pos = await readDashboard(parseOptions({view: 'positions', book: 'paper_v1'}), select, now);
    expect(pos.total).toBe(1);
    expect(pos.rows[0].symbol).toBe('RELIANCE-EQ');
    const o = await readDashboard(parseOptions({book: 'paper_v1'}), select, now);
    expect(o.namespace).toBe('paper_v1');
    expect(o.totals.openPositions).toBe(1);
    expect(o.dailyHistory).toHaveLength(1);
    const a = await readDashboard(parseOptions({view: 'analytics', book: 'paper_v1'}), select, now);
    expect(a.eods).toHaveLength(1);
    expect(a.eods[0].day).toBe('2026-11-09');
  });
});

describe('dashboard handler cache is per book', () => {
  const request = (query = {}, method = 'GET') => ({query, method, headers: {'x-forwarded-for': randomUUID()}});
  function response() { return {headers: {}, setHeader(k, v) { this.headers[k] = v; }, status(n) { this.code = n; return this; }, json(v) { this.body = v; return this; }}; }

  it('book=paper_v2 then book=paper_v1 returns different namespaces, no cross-book hit', async () => {
    const reader = vi.fn(async options => ({namespace: options.book, book: options.book, readAt: 'fixture'}));
    const h = createHandler(reader);
    const first = response(); await h(request({}), first);
    expect(first.body.namespace).toBe('paper_v2');
    const second = response(); await h(request({book: 'paper_v1'}), second);
    expect(second.body.namespace).toBe('paper_v1');
    expect(reader).toHaveBeenCalledTimes(2);
  });

  it('book=paper_demo_v1 returns HTTP 400 without reading', async () => {
    const reader = vi.fn();
    const h = createHandler(reader);
    const res = response(); await h(request({book: 'paper_demo_v1'}), res);
    expect(res.code).toBe(400);
    expect(reader).not.toHaveBeenCalled();
  });
});

describe('server read modules contain no write SQL', () => {
  it('no INSERT/UPDATE/DELETE/REPLACE/DROP/ALTER/CREATE/TRUNCATE in read modules', () => {
    const files = ['server/data.mjs', 'server/automation-read.mjs', 'server/ledger-read.mjs', 'server/exit-plan.mjs', 'server/capped-ledger.mjs', 'server/books.mjs', 'api/dashboard.js'];
    const write = /\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE)\b/;
    for (const f of files) {
      const src = readFileSync(f, 'utf8');
      for (const line of src.split('\n')) {
        expect(line.replace(/\/\/.*$/, '')).not.toMatch(write);
      }
    }
  });
});
