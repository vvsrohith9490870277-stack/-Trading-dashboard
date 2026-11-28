"""Tests for render-deploy-03 Task 1: scan memory (golden output + caps).

The golden test pins the scan's byte-identical output on a small
deterministic fixture (30 symbols, 300 sessions, one crafted qualifying
symbol, one held strategy-7 position, one unavailable symbol, one
invalid-OHLC row). The golden file `tests/fixtures/render_scan_golden.json`
was recorded from the base code BEFORE any engine change; the test fails
on any difference.

Determinism notes (why the golden can be byte-identical at all):
- engine.now is frozen (T = 2026-11-20 10:00 IST -> completed day
  2026-11-19) and the namespace is fixed, so every timestamp the scan
  writes comes from the frozen clock.
- audit events carry random event_id/run_id, a wall-clock recorded_at
  and a duration_ms, so they are normalised before comparison:
  event_id -> E<i> by position, run_id -> R<j> by first sight,
  recorded_at -> null, duration_ms dropped, batch_id mapped.
- The scope/emit events (run_started, scan_started, scan_completed,
  run_completed) are NOT in the golden's ordered list: the audit spool
  replays them in uuid-filename order, so their DB insertion order is
  random by construction, and their recorded_at/duration_ms are wall
  clock. Their *content* (normalised) is still checked in the
  all-events multiset.
- The golden holds the scan's own audit events in db.batch statement
  order (captured by a spy), the report dict, and the sorted rows of
  paper_watermarks, paper_signals, paper_qualifier_states,
  paper_prefilter_states, paper_scan_batches and paper_data_audit.

Record the golden (base code only, never after an engine change):
  RENDER_RECORD_GOLDEN=1 python -m unittest \
      tests.test_render_scan_memory.ScanGolden
"""
import json
import os
import resource
import sqlite3
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import engine as e
import render_mem_fixture as rmf

T = datetime(2026, 11, 20, 10, tzinfo=e.IST)
TARGET = '2026-11-19'
NS = 'test_rendermem_golden'
GOLDEN = HERE / 'fixtures' / 'render_scan_golden.json'
GOLDEN_TABLES = ('paper_watermarks', 'paper_signals',
                 'paper_qualifier_states', 'paper_prefilter_states',
                 'paper_scan_batches', 'paper_data_audit')
# audit.paper_events columns, in order (see audit.COLUMNS).
EVENT_COLS = ('namespace', 'event_id', 'run_id', 'session_day', 'source_at',
              'recorded_at', 'event_type', 'severity', 'outcome', 'strategy',
              'symbol', 'bar_date', 'signal_id', 'policy_version',
              'input_policy_version', 'data_json')

CHUNK_SYMBOLS = int(os.environ.get('RENDER_SCAN_CHUNK_SYMBOLS', '10'))
BATCH_MAX = int(os.environ.get('RENDER_SCAN_BATCH_MAX', '200'))
RSS_CAP_MB = int(os.environ.get('RENDER_SCAN_RSS_CAP_MB', '300'))


class DummyApi:
    """Scan with do_sync=False never touches the market-data API."""


def _norm_event(row, run_map, pos):
    """Normalise one paper_events row (tuple in EVENT_COLS order) for the
    golden: random ids replaced by position/first-sight ordinals,
    wall-clock fields dropped, data_json canonicalised."""
    d = dict(zip(EVENT_COLS, row))
    d['event_id'] = 'E%d' % pos
    rid = d['run_id']
    if rid not in run_map:
        run_map[rid] = 'R%d' % (len(run_map) + 1)
    d['run_id'] = run_map[rid]
    d['recorded_at'] = None
    payload = json.loads(d['data_json'])
    if 'batch_id' in payload:
        payload['batch_id'] = run_map.get(payload['batch_id'],
                                          payload['batch_id'])
    payload.pop('duration_ms', None)
    d['data_json'] = json.dumps(payload, sort_keys=True,
                                separators=(',', ':'))
    return [d[c] for c in EVENT_COLS]


def _dump_tables(db, ns):
    out = {}
    for table in GOLDEN_TABLES:
        rows = db.query('SELECT * FROM %s WHERE namespace=?' % table, (ns,))
        out[table] = sorted([list(r.values()) for r in rows],
                            key=lambda r: json.dumps(r, default=str,
                                                     sort_keys=True))
    return out


def _dump_audit_multiset(db, ns, session_day):
    """All paper_events rows for the namespace+session, normalised with
    ids dropped (content check only; order is covered by the stm list)."""
    rows = db.query(
        'SELECT * FROM paper_events WHERE namespace=? AND session_day=?',
        (ns, session_day))
    out = []
    for r in rows:
        d = dict(zip(EVENT_COLS, [r[c] for c in EVENT_COLS]))
        d['event_id'] = None
        d['run_id'] = None
        d['recorded_at'] = None
        payload = json.loads(d['data_json'])
        payload.pop('batch_id', None)
        payload.pop('duration_ms', None)
        d['data_json'] = json.dumps(payload, sort_keys=True,
                                    separators=(',', ':'))
        out.append(json.dumps([d[c] for c in EVENT_COLS], default=str))
    return sorted(out)


class _BatchSpy:
    """Wrap db.batch: record every call's statements (for the golden's
    audit-event order and the batches-bounded test)."""

    def __init__(self, db):
        self.db = db
        self.calls = []
        self._orig = db.batch

    def __enter__(self):
        def spy(stm):
            self.calls.append(list(stm))
            return self._orig(stm)
        self.db.batch = spy
        return self

    def __exit__(self, *exc):
        self.db.batch = self._orig
        return False

    def audit_sequence(self):
        """The scan's own audit events in statement order: paper_events
        INSERTs from all batches up to and including the batch that
        carries the paper_scan_batches row (the final scan batch).
        Replay batches (scope events, uuid order) come after it and are
        excluded."""
        seq = []
        for call in self.calls:
            for sql, args in call:
                if 'INTO paper_events' in sql:
                    seq.append(tuple(args))
            if any('INTO paper_scan_batches' in sql for sql, _ in call):
                break
        run_map = {}
        return [_norm_event(row, run_map, i)
                for i, row in enumerate(seq)]


def build_golden_db(path):
    """Small deterministic fixture: 30 universe symbols x 300 sessions,
    one crafted qualifying symbol, one held strategy-7 position, one
    unavailable symbol (the other 662 universe symbols have no bars),
    one invalid-OHLC row. Watermarks are seeded at the session before
    TARGET for the 30 symbols so the full signal logic runs (otherwise
    the scan only bootstraps watermarks and queues no signals)."""
    syms = rmf.universe_symbols(30)
    rmf.build_db(path, syms, 300, TARGET, rmf.SEED, NS)
    con = sqlite3.connect(str(path))
    try:
        sessions = rmf.session_list(TARGET, 300)
        prev = sessions[-2]
        # Crafted qualifying symbol: returns = 1.5x benchmark + drift,
        # so beta ~1.5 and the momentum checks pass.
        qual = syms[0]
        bench = [r[0] for r in con.execute(
            'SELECT close FROM index_ohlcv ORDER BY trade_date')]
        assert len(bench) == len(sessions)
        import random as _random
        rng = _random.Random(rmf.SEED + 4242)
        price = 100.0
        rows = []
        for j, day in enumerate(sessions):
            bret = bench[j] / bench[j - 1] - 1 if j else 0.0
            if j >= len(sessions) - 25:
                # Last 25 sessions: a stronger drift so the 20-day
                # return is robustly positive (the benchmark's own
                # 20-day return is negative on this seed; a plain
                # 1.5x + 0.2%/day drift leaves return20 at -0.07%).
                r = 1.5 * bret + 0.008 + rng.gauss(0, 0.003)
            else:
                r = 1.5 * bret + 0.002 + rng.gauss(0, 0.006)
            o = price * (1 + rng.gauss(0, 0.003))
            c = max(0.01, o * (1 + r))
            h = max(o, c) * 1.003
            l = min(o, c) * 0.997
            rows.append((qual, day, round(o, 2), round(h, 2), round(l, 2),
                         round(c, 2), 200000, round(c * 200000, 2)))
            price = c
        con.execute('DELETE FROM daily_ohlcv WHERE symbol=?', (qual,))
        con.executemany(
            'INSERT INTO daily_ohlcv(symbol,trade_date,open,high,low,'
            'close,volume,turnover,source,loaded_at) '
            'VALUES(?,?,?,?,?,?,?,?,\'RENDER_MEM_FIXTURE\','
            '\'2026-11-20T00:00:00+05:30\')', rows)
        # Invalid-OHLC row: high below low on a mid-history date.
        bad_day = sessions[100]
        con.execute(
            'INSERT OR REPLACE INTO daily_ohlcv(symbol,trade_date,open,'
            'high,low,close,volume,turnover,source,loaded_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (syms[1], bad_day, 100.0, 50.0, 90.0, 95.0, 1000, 95000.0,
             'RENDER_MEM_FIXTURE', '2026-11-20T00:00:00+05:30'))
        # Held strategy-7 position (trail): entered on the session
        # before TARGET, high_water = the max close over its two bars,
        # so the trail (0.9 x high_water) is never touched by either
        # bar's low and no missed_stop_touch fires. The trail update
        # still runs (mark_price is None), so the position row is
        # written deterministically.
        held = syms[2]
        last2 = [r[0] for r in con.execute(
            'SELECT close FROM daily_ohlcv WHERE symbol=? '
            'ORDER BY trade_date DESC LIMIT 2', (held,))]
        assert len(last2) == 2
        con.execute(
            'INSERT INTO paper_positions(namespace,strategy,symbol,qty,'
            'entry_price,entry_at,high_water,mark_price,mark_at,'
            'updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (NS, 7, held, 500, last2[1], prev, max(last2), None, None,
             T.isoformat()))
        # Watermarks at the previous session for the 30 symbols.
        con.executemany(
            'INSERT INTO paper_watermarks(namespace,strategy,symbol,'
            'bar_date) VALUES(?,?,?,?)',
            [(NS, s, sym, prev) for sym in syms for s in range(1, 8)])
        con.commit()
    finally:
        con.close()
    return path


def run_scan_golden(db):
    """Run the scan in-process with the frozen clock and a batch spy.
    Returns (report, tables, audit_stm_order, audit_multiset). Note the
    audit multiset is keyed by the session day of engine.now() (the day
    the scan runs, T.date()), not the target bar: paper_events rows
    carry session_day = now's date and bar_date = the target."""
    with _BatchSpy(db) as spy:
        report = e.scan(db, DummyApi())
    tables = _dump_tables(db, NS)
    audit_seq = spy.audit_sequence()
    multi = _dump_audit_multiset(db, NS, T.date().isoformat())
    return report, tables, audit_seq, multi


class ChunkFrameTests(unittest.TestCase):
    """_chunk_frame (Task 1 fix): a chunk's history restricted to the
    chunk's symbols. The DB reader already filters, so this is a
    no-op on the real path (byte-identical); it makes the scan
    correct when the reader returns extra rows (a mock, or a future
    reader change) — without it, pass B processes every symbol in
    the frame for every chunk and strategy_audit[sym] accumulates
    past the audit list bound (test_full751)."""

    def test_keeps_only_chunk_symbols(self):
        import pandas as pd
        raw = pd.DataFrame([
            {'symbol': 'A-EQ', 'trade_date': '2026-11-09', 'open': 100.,
             'high': 102., 'low': 99., 'close': 101., 'volume': 1000.},
            {'symbol': 'B-EQ', 'trade_date': '2026-11-09', 'open': 100.,
             'high': 102., 'low': 99., 'close': 101., 'volume': 1000.},
            {'symbol': 'C-EQ', 'trade_date': '2026-11-09', 'open': 100.,
             'high': 102., 'low': 99., 'close': 101., 'volume': 1000.}])
        out = e._chunk_frame(raw, ['A-EQ', 'C-EQ'])
        self.assertEqual(sorted(out['symbol'].unique()), ['A-EQ', 'C-EQ'])
        self.assertEqual(len(out), 2)

    def test_empty_frame_passes_through(self):
        import pandas as pd
        raw = pd.DataFrame(columns=['symbol', 'trade_date', 'open',
                                    'high', 'low', 'close', 'volume'])
        out = e._chunk_frame(raw, ['A-EQ'])
        self.assertEqual(len(out), 0)


class ScanGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_ns, cls._old_now = e.NS, e.now
        e.NS = NS
        e.now = lambda: T
        cls.path = HERE / ('test_rendermem_golden_%s.sqlite' % uuid.uuid4().hex)
        build_golden_db(cls.path)
        cls.db = e.DB(str(cls.path))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close_helper()
        except Exception:
            pass
        e.NS, e.now = cls._old_ns, cls._old_now
        cls.path.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(cls.path) + suffix).unlink(missing_ok=True)

    def test_scan_golden_output_identical(self):
        report, tables, audit_seq, multi = run_scan_golden(self.db)
        got = {'report': report, 'tables': tables,
               'audit_events_stm_order': audit_seq,
               'audit_events_all': multi}
        if os.environ.get('RENDER_RECORD_GOLDEN') == '1':
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(got, sort_keys=True,
                                         separators=(',', ':')))
            print('golden recorded: %s (%d bytes, %d audit events, '
                  'signals=%d)' % (GOLDEN, GOLDEN.stat().st_size,
                                    len(audit_seq),
                                    report['signals_queued']))
            return
        self.assertTrue(GOLDEN.exists(), 'golden file missing: %s' % GOLDEN)
        want = json.loads(GOLDEN.read_text())
        self.assertEqual(got['report'], want['report'], 'report differs')
        self.assertEqual(got['tables'], want['tables'], 'tables differ')
        self.assertEqual(got['audit_events_stm_order'],
                         want['audit_events_stm_order'],
                         'audit event order differs')
        self.assertEqual(got['audit_events_all'], want['audit_events_all'],
                         'audit event contents differ')


class ScanMemory(unittest.TestCase):
    """Full-size scan as a subprocess: peak tree RSS under the cap."""

    def test_scan_peak_memory_under_cap(self):
        if not os.path.exists('/proc/self/status'):
            self.skipTest('no /proc on this machine')
        import shutil
        if shutil.which('node') is None:
            self.skipTest('node missing')
        db_path = HERE / ('test_rendermem_mem_%s.sqlite' % uuid.uuid4().hex)
        try:
            rmf.build_db(str(db_path))
            res = rmf.measure_scan(str(db_path))
        finally:
            db_path.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
        print('scan peak: tree=%d MiB python=%d MiB node=%d MiB '
              'wall=%.1fs exit=%d (cap %d MiB)' % (
                  res['peak_tree_mb'], res['peak_python_mb'],
                  res['peak_node_mb'], res['wall_s'], res['exit_code'],
                  RSS_CAP_MB))
        self.assertEqual(res['exit_code'], 0, res['stdout_tail'][-500:])
        self.assertLessEqual(res['peak_tree_mb'], RSS_CAP_MB,
                             'scan tree peak %d MiB exceeds cap %d MiB'
                             % (res['peak_tree_mb'], RSS_CAP_MB))


class ScanBatches(unittest.TestCase):
    """The largest single db.batch call during a full-size scan holds at
    most BATCH_MAX statements (spy on db.batch)."""

    def test_scan_batches_bounded(self):
        old_ns, old_now = e.NS, e.now
        e.NS, e.now = NS, lambda: T
        db_path = HERE / ('test_rendermem_batch_%s.sqlite' % uuid.uuid4().hex)
        try:
            rmf.build_db(str(db_path))
            db = e.DB(str(db_path))
            try:
                with _BatchSpy(db) as spy:
                    e.scan(db, DummyApi())
            finally:
                db.close_helper()
        finally:
            e.NS, e.now = old_ns, old_now
            db_path.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
        sizes = [len(c) for c in spy.calls if c]
        biggest = max(sizes) if sizes else 0
        print('batch calls: %d, biggest single batch: %d statements '
              '(max %d)' % (len(sizes), biggest, BATCH_MAX))
        self.assertLessEqual(biggest, BATCH_MAX,
                             'largest batch %d exceeds %d'
                             % (biggest, BATCH_MAX))


class JobOutput(unittest.TestCase):
    """A fake job child printing 50 MB of stdout must not grow the
    supervisor's RSS by 20 MB, and the recorded tail is the last 2000
    characters."""

    def test_job_output_not_buffered(self):
        import render_app as ra
        before = ra.rh.memory_rss_mb()
        code = ('import sys; sys.stdout.write("x" * (50 * 1024 * 1024)); '
                'sys.stdout.flush()')
        res = ra.run_job_streaming([sys.executable, '-c', code], None)
        after = ra.rh.memory_rss_mb()
        growth = after - before
        print('supervisor RSS growth: %d MiB (limit 20), tail len %d'
              % (growth, len(res['stdout_tail'])))
        self.assertEqual(res['exit_code'], 0)
        self.assertLess(growth, 20,
                        'streaming job grew supervisor RSS by %d MiB'
                        % growth)
        self.assertEqual(len(res['stdout_tail']), 2000)
        self.assertEqual(res['stdout_tail'], 'x' * 2000)


class RerunIdempotent(unittest.TestCase):
    """Abort after the 3rd batch, re-run, final state tables and the
    audit-event sequence equal the golden. (paper_events as a multiset
    is excluded: the aborted run's own scope events legitimately remain
    in the append-only audit log.)"""

    def test_scan_rerun_after_partial_batch_idempotent(self):
        old_ns, old_now = e.NS, e.now
        e.NS, e.now = NS, lambda: T
        path = HERE / ('test_rendermem_rerun_%s.sqlite' % uuid.uuid4().hex)
        try:
            build_golden_db(path)
            db = e.DB(str(path))
            try:
                # Abort on the 3rd batch that writes a scan table
                # (prefilter/qualifier/watermark/signal/scan_batches).
                # Pure-audit replay batches are not counted: on the
                # base code the scan writes everything in ONE batch,
                # so the abort never fires there and this test fails
                # (the G6 check). On the streamed code the 3rd such
                # batch is a prefilter flush, so the abort lands
                # mid-scan, before any watermark or signal is written.
                scan_tables = ('paper_prefilter_states',
                               'paper_qualifier_states', 'paper_watermarks',
                               'paper_signals', 'paper_scan_batches')
                calls = {'n': 0}
                orig = db.batch

                def aborting(stm):
                    if any('INTO %s' % t in sql for sql, _ in stm
                           for t in scan_tables):
                        calls['n'] += 1
                        if calls['n'] == 3:
                            raise RuntimeError(
                                'simulated_kill_after_3_batches')
                    return orig(stm)

                db.batch = aborting
                with self.assertRaises(RuntimeError):
                    e.scan(db, DummyApi())
                db.batch = orig
                report, tables, audit_seq, _multi = run_scan_golden(db)
            finally:
                db.close_helper()
        finally:
            e.NS, e.now = old_ns, old_now
            path.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(path) + suffix).unlink(missing_ok=True)
        want = json.loads(GOLDEN.read_text())
        self.assertEqual(report, want['report'], 'report differs')
        self.assertEqual(tables, want['tables'], 'tables differ')
        self.assertEqual(audit_seq, want['audit_events_stm_order'],
                         'audit event order differs')

    def test_scan_rerun_after_signals_written_idempotent(self):
        # I23 on Render: abort on the FIRST batch that writes a
        # signal (the crash lands mid-scan, after signals exist),
        # re-run, and the day still has exactly one
        # paper_scan_batches row and the same signal ids — no
        # duplicate batch, no duplicate signals. The re-run report
        # is NOT asserted: signals_queued is legitimately lower on
        # the re-run (already-queued signals are skipped via the
        # pending set), a pre-existing reporting quirk out of scope.
        old_ns, old_now = e.NS, e.now
        e.NS, e.now = NS, lambda: T
        path = HERE / ('test_rendermem_rerun2_%s.sqlite' % uuid.uuid4().hex)
        try:
            build_golden_db(path)
            db = e.DB(str(path))
            try:
                orig = db.batch

                def aborting(stm):
                    if any('INTO paper_signals' in sql for sql, _ in stm):
                        raise RuntimeError(
                            'simulated_kill_after_signals')
                    return orig(stm)

                db.batch = aborting
                with self.assertRaises(RuntimeError):
                    e.scan(db, DummyApi())
                db.batch = orig
                _report, tables, audit_seq, _multi = run_scan_golden(db)
            finally:
                db.close_helper()
        finally:
            e.NS, e.now = old_ns, old_now
            path.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(path) + suffix).unlink(missing_ok=True)
        want = json.loads(GOLDEN.read_text())
        batches = tables['paper_scan_batches']
        self.assertEqual(len(batches), 1,
                         're-run wrote a second scan batch row')
        self.assertEqual(tables['paper_signals'],
                         want['tables']['paper_signals'],
                         'signals differ after re-run (duplicates?)')
        self.assertEqual(tables, want['tables'], 'tables differ')
        self.assertEqual(audit_seq, want['audit_events_stm_order'],
                         'audit event order differs')


class SingleNode(unittest.TestCase):
    """At most one node helper runs at a time during the scan, and the
    persistent helper is closed after the scan."""

    def test_scan_single_node_child_at_a_time(self):
        old_ns, old_now = e.NS, e.now
        e.NS, e.now = NS, lambda: T
        path = HERE / ('test_rendermem_node_%s.sqlite' % uuid.uuid4().hex)
        try:
            build_golden_db(path)
            db = e.DB(str(path))
            live = {'n': 0, 'max': 0}
            orig = db._call_per_process

            def counting(a):
                live['n'] += 1
                live['max'] = max(live['max'], live['n'])
                try:
                    return orig(a)
                finally:
                    live['n'] -= 1

            db._call_per_process = counting
            try:
                e.scan(db, DummyApi())
            finally:
                db._call_per_process = orig
                db.close_helper()
        finally:
            e.NS, e.now = old_ns, old_now
            path.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(path) + suffix).unlink(missing_ok=True)
        print('max concurrent node calls: %d' % live['max'])
        self.assertEqual(live['max'], 1,
                         'more than one node child at a time')
        self.assertIsNone(db.helper, 'helper left open after the scan')


if __name__ == '__main__':
    unittest.main()
