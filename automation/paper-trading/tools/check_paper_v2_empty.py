#!/usr/bin/env python3
"""Check that paper_v2 has no positions, no fills and no realised P&L.

Turso move (brief Tasks 3 + 5). paper_v2 started 19 Nov 2026 and every BUY
on 20 Nov was blocked (reason missing_or_failed_current_prefilter_proof), so
paper_v2 should be EMPTY. This script proves that from the box's LOCAL logs
before the cutover, so paper_v2 can start FRESH in the new Turso DB with
nothing carried over.

Runs on the box (Grok runs it there — this machine cannot reach the box):
    python3 tools/check_paper_v2_empty.py
    python3 tools/check_paper_v2_empty.py --days 2026-11-19,2026-11-20 --turso

Inputs (all read-only; the script never writes to the DB or the logs):
  --logs-dir  directory holding logs/engine.jsonl
              (default /workspace/paper-trading/logs/)
  --out-dir   directory holding out/audit-<day>.jsonl
              (default /workspace/paper-trading/out/)
  --days      session days to check, CSV and/or repeatable
              (default 2026-11-19,2026-11-20)
  --db PATH   optional local SQLite file: also run the Turso-side counts
              (paper_positions / paper_fills WHERE namespace='paper_v2')
              against it. Without it the check is log-only (stated, not failed).
  --turso     optional: run the same two counts against live Turso through
              the repo's own db.mjs (node), which reads PAPER_ENV_FILE or
              /workspace/sahil-fyers-turso/.env itself. No credentials on the
              command line, none printed. Costs 2 Turso reads.
  --report PATH  optional: write the JSON summary to PATH (the only write).

What counts as evidence of a fill (either file, namespace paper_v2 only):
  engine.jsonl  kind=paper_fill_attempt with result.filled=true
                (engine.py log(); result is the db.mjs fill reply)
  audit-*.jsonl event_type=paper_order_executed, outcome=filled
                (db.mjs orderAudit; data_json carries fill_id)
                event_type=paper_order_result, outcome=filled / fills>0
                (engine.py fill(); same fill seen from the engine side)
  A fill seen in both files is one fill (deduped by fill_id, else signal_id).
Positions: an open_positions>0 field on a paper_v2 row (engine order_skipped
/ audit eod_completed totals), and — authoritatively — the DB check, which
lists every open paper_positions row. Realised P&L: a nonzero realized_pnl
on a fill, or a nonzero EOD cumulative_realized_pnl_INR / gross_realized_paise.
Blocked signals (paper_order_blocked rows, e.g. the 17 on 20 Nov) are counted
and reported for context; they are NOT fills and do not affect the verdict.

Exit codes: 0 EMPTY (no fills, no positions, no realised P&L in paper_v2),
2 NOT EMPTY (exactly what was found is listed), 1 error (missing/unreadable
log, bad arguments, failed DB check). Missing evidence fails closed: a
missing logs dir, engine.jsonl, or audit-<day>.jsonl for any requested day,
or a malformed JSON line, is exit 1 naming the file (and line). Run the
audit export first (sessiond exports at 20:00 IST) or narrow --days.
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

DEFAULT_DAYS = ['2026-11-19', '2026-11-20']
DEFAULT_LOGS_DIR = '/workspace/paper-trading/logs/'
DEFAULT_OUT_DIR = '/workspace/paper-trading/out/'
NAMESPACE = 'paper_v2'

# Fill evidence: (engine kind, audit event_type) pairs.
ENGINE_FILL_KIND = 'paper_fill_attempt'
AUDIT_FILL_EVENTS = ('paper_order_executed', 'paper_order_result')
AUDIT_BLOCKED_EVENT = 'paper_order_blocked'
AUDIT_EOD_EVENT = 'eod_completed'

# The two Turso-side counts (also used verbatim for the sqlite --db check).
POSITIONS_SQL = ('SELECT strategy, symbol, qty, entry_price, entry_at'
                 ' FROM paper_positions WHERE namespace=? AND qty>0')
FILLS_SQL = ('SELECT id, strategy, symbol, side, qty, price, realized_pnl, filled_at'
             ' FROM paper_fills WHERE namespace=?')


class CheckError(Exception):
    """A problem that makes the verdict unprovable: exit 1."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 by default; 2 must mean NOT EMPTY only.
        self.print_usage(sys.stderr)
        print('error: %s' % message, file=sys.stderr)
        raise SystemExit(1)


def parse_days(values):
    """['2026-11-19,2026-11-20'] / ['2026-11-20', '2026-11-19'] -> sorted list."""
    days = set()
    for value in values or []:
        for part in str(value).split(','):
            part = part.strip()
            if not part:
                continue
            try:
                date.fromisoformat(part)
            except ValueError:
                raise ValueError('invalid day: %r (use YYYY-MM-DD)' % part)
            days.add(part)
    if not days:
        raise ValueError('no days given')
    return sorted(days)


def _num(value):
    """A JSON number as float/int, else None (never raise on log data)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _row_day(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                continue
    return None


def _data_json(row):
    """The audit data_json payload as a dict ({} when absent/unparseable)."""
    raw = row.get('data_json')
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _merge_fill(fills, key, patch, source):
    """Add or merge one fill finding; the same fill seen twice is one fill.

    Two sightings of one fill are matched by fill_id (when known) or by
    signal_id (paper_fills has UNIQUE(namespace, signal_id), so either
    identifies the fill). The key is only a fallback for sightings that
    carry neither.
    """
    fill_id = patch.get('fill_id')
    signal_id = patch.get('signal_id')
    for fill in fills:
        same = False
        if fill_id is not None and fill.get('fill_id') == fill_id:
            same = True
        elif signal_id is not None and fill.get('signal_id') == signal_id:
            same = True
        elif fill.get('_key') == key:
            same = True
        if same:
            for field, value in patch.items():
                if fill.get(field) in (None, '', 0) and value not in (None, '', 0):
                    fill[field] = value
            if source not in fill['sources']:
                fill['sources'].append(source)
                fill['sources'].sort()
            return fill
    fill = {'fill_id': None, 'signal_id': None, 'strategy': None, 'symbol': None,
            'side': None, 'qty': None, 'price': None, 'realized_pnl': None,
            'day': None, 'sources': [source]}
    fill.update({k: v for k, v in patch.items() if v is not None})
    fill['_key'] = key
    fills.append(fill)
    return fill


def _merge_position(positions, strategy, symbol, patch, source):
    for pos in positions:
        if pos['strategy'] == strategy and pos['symbol'] == symbol:
            for field, value in patch.items():
                if pos.get(field) is None and value is not None:
                    pos[field] = value
            if source not in pos['sources']:
                pos['sources'].append(source)
                pos['sources'].sort()
            return pos
    pos = {'strategy': strategy, 'symbol': symbol, 'qty': None,
           'entry_price': None, 'day': None, 'open_positions': None,
           'detail': None, 'sources': [source]}
    pos.update({k: v for k, v in patch.items() if v is not None})
    positions.append(pos)
    return pos


def _realized_entry(entries, amount, unit, day, source, detail):
    if _num(amount) in (None, 0):
        return
    entry = {'amount': amount, 'unit': unit, 'day': day, 'source': source,
             'detail': detail}
    if entry not in entries:
        entries.append(entry)


def _scan_engine(path, days, namespace, out):
    """Scan logs/engine.jsonl. Returns (rows_read, ignored_other_namespace)."""
    rows = ignored = 0
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise CheckError('%s line %d: malformed JSON (unreadable log)'
                                 % (path, lineno))
            if not isinstance(row, dict):
                raise CheckError('%s line %d: expected a JSON object' % (path, lineno))
            if not isinstance(row.get('namespace'), str):
                raise CheckError('%s line %d: row has no namespace' % (path, lineno))
            rows += 1
            if row['namespace'] != namespace:
                ignored += 1
                continue
            day = _row_day(row, ('at_IST',))
            if day is not None and day not in days:
                continue
            kind = row.get('kind')
            if kind == ENGINE_FILL_KIND:
                result = row.get('result')
                if isinstance(result, dict) and result.get('filled') is True:
                    signal = result.get('correlation_id') or row.get('signal_id')
                    _merge_fill(out['fills'], ('signal', str(signal)) if signal else
                                ('engine', day, row.get('strategy'), row.get('symbol'),
                                 result.get('side')),
                                {'signal_id': str(signal) if signal else None,
                                 'strategy': row.get('strategy'),
                                 'symbol': row.get('symbol'),
                                 'side': result.get('side'),
                                 'qty': result.get('qty') or result.get('quantity'),
                                 'price': result.get('price'),
                                 'realized_pnl': _num(result.get('realized_pnl')),
                                 'day': day},
                                'engine.jsonl')
                    _realized_entry(out['realized_pnl'], result.get('realized_pnl'),
                                    'INR', day, 'engine.jsonl',
                                    'realized_pnl on fill %s' % (signal or 'unknown'))
            open_positions = _num(row.get('open_positions'))
            if open_positions and open_positions > 0 and row.get('strategy') is not None \
                    and row.get('symbol'):
                _merge_position(out['positions'], row.get('strategy'), row.get('symbol'),
                                {'open_positions': open_positions, 'day': day},
                                'engine.jsonl')
    return rows, ignored


def _scan_audit(path, days, namespace, out):
    """Scan one out/audit-<day>.jsonl (paper_events rows)."""
    rows = ignored = 0
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise CheckError('%s line %d: malformed JSON (unreadable log)'
                                 % (path, lineno))
            if not isinstance(row, dict):
                raise CheckError('%s line %d: expected a JSON object' % (path, lineno))
            if not isinstance(row.get('namespace'), str):
                raise CheckError('%s line %d: row has no namespace' % (path, lineno))
            rows += 1
            if row['namespace'] != namespace:
                ignored += 1
                continue
            day = _row_day(row, ('session_day', 'source_at', 'recorded_at'))
            if day is not None and day not in days:
                continue
            event = row.get('event_type')
            payload = _data_json(row)
            if event == AUDIT_BLOCKED_EVENT:
                out['blocked_signals'] += 1
                reason = str(row.get('outcome') or 'unknown')
                out['blocked_by_reason'][reason] = out['blocked_by_reason'].get(reason, 0) + 1
            if event in AUDIT_FILL_EVENTS and (
                    row.get('outcome') == 'filled' or _num(payload.get('fills'))):
                fill_id = payload.get('fill_id')
                signal = row.get('signal_id') or payload.get('correlation_id')
                key = ('fill', fill_id) if fill_id is not None else \
                    ('signal', str(signal)) if signal else \
                    ('audit', day, row.get('strategy'), row.get('symbol'))
                fill = _merge_fill(out['fills'], key,
                                   {'fill_id': fill_id if isinstance(fill_id, int) else None,
                                    'signal_id': str(signal) if signal else None,
                                    'strategy': row.get('strategy'),
                                    'symbol': row.get('symbol'),
                                    'side': payload.get('side'),
                                    'qty': payload.get('quantity'),
                                    'price': payload.get('price'),
                                    'day': day},
                                   path.name)
                if fill.get('realized_pnl') is None:
                    fill['realized_pnl'] = _num(payload.get('realized_pnl'))
                _realized_entry(out['realized_pnl'], payload.get('realized_pnl'),
                                'INR', day, path.name,
                                'realized_pnl on fill %s' % (fill_id or signal or 'unknown'))
            if event == AUDIT_EOD_EVENT:
                open_positions = _num(payload.get('open_positions'))
                if open_positions and open_positions > 0:
                    # EOD total, not one position row: unattributed, labelled so.
                    _merge_position(out['positions'], None, None,
                                    {'open_positions': open_positions, 'day': day,
                                     'detail': 'EOD open-position total for the day'},
                                    path.name)
                _realized_entry(out['realized_pnl'],
                                payload.get('cumulative_realized_pnl_INR'),
                                'INR', day, path.name,
                                'eod cumulative_realized_pnl_INR')
                _realized_entry(out['realized_pnl'],
                                payload.get('gross_realized_paise'),
                                'paise', day, path.name,
                                'eod gross_realized_paise')
    return rows, ignored


def _check_sqlite(db_path, namespace, out):
    if not db_path.is_file():
        raise CheckError('db file not found: %s' % db_path)
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
        try:
            positions = con.execute(POSITIONS_SQL, (namespace,)).fetchall()
            fills = con.execute(FILLS_SQL, (namespace,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as ex:
        raise CheckError('db check failed on %s: %s' % (db_path, ex))
    return positions, fills


def _query_via_db_mjs(db_mjs, sql, namespace):
    """One read-only query through the repo's db.mjs (node). Returns rows."""
    if shutil.which('node') is None:
        raise CheckError('--turso needs node, which is not on PATH')
    if not db_mjs.is_file():
        raise CheckError('db.mjs not found: %s (pass --db-mjs)' % db_mjs)
    req = json.dumps({'action': 'query', 'sql': sql, 'args': [namespace]})
    try:
        proc = subprocess.run(['node', str(db_mjs)], input=req, text=True,
                              capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise CheckError('db.mjs query timed out after 60s')
    if proc.returncode != 0:
        code = 'db_mjs_failed'
        try:
            code = json.loads(proc.stderr).get('code') or code
        except ValueError:
            pass
        raise CheckError('db.mjs query failed: %s' % code)
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        raise CheckError('db.mjs returned unparseable output')
    if not isinstance(rows, list):
        raise CheckError('db.mjs returned a non-list result')
    return rows


def _apply_db_rows(positions, fills, source, out):
    """Fold DB rows (sqlite tuples or db.mjs dicts) into the findings."""
    for row in positions:
        if isinstance(row, dict):
            strategy, symbol, qty, entry_price = (
                row['strategy'], row['symbol'], row['qty'], row['entry_price'])
        else:
            strategy, symbol, qty, entry_price = row[0], row[1], row[2], row[3]
        _merge_position(out['positions'], strategy, symbol,
                        {'qty': qty, 'entry_price': entry_price}, source)
    for row in fills:
        if isinstance(row, dict):
            fill_id, strategy, symbol = row.get('id'), row.get('strategy'), row.get('symbol')
            side, qty, price = row.get('side'), row.get('qty'), row.get('price')
            realized, day = _num(row.get('realized_pnl')), _row_day(row, ('filled_at',))
        else:
            fill_id, strategy, symbol = row[0], row[1], row[2]
            side, qty, price = row[3], row[4], row[5]
            realized, day = _num(row[6]), _row_day({'filled_at': row[7]}, ('filled_at',))
        _merge_fill(out['fills'], ('fill', fill_id),
                    {'fill_id': fill_id if isinstance(fill_id, int) else None,
                     'strategy': strategy, 'symbol': symbol, 'side': side,
                     'qty': qty, 'price': price, 'realized_pnl': realized,
                     'day': day},
                    source)
        _realized_entry(out['realized_pnl'], realized, 'INR', day, source,
                        'realized_pnl on fill %s' % fill_id)
    return len(positions), len(fills)


def check(logs_dir, out_dir, days, namespace, db_path=None, turso=False, db_mjs=None):
    """Run the log (+ optional DB) check. Returns (summary, errors)."""
    out = {'fills': [], 'positions': [], 'realized_pnl': [],
           'blocked_signals': 0, 'blocked_by_reason': {}}
    errors = []
    engine_rows = audit_rows = ignored = 0
    audit_files = []
    logs_dir, out_dir = Path(logs_dir), Path(out_dir)
    try:
        if not logs_dir.is_dir():
            raise CheckError('logs directory not found: %s' % logs_dir)
        if not out_dir.is_dir():
            raise CheckError('out directory not found: %s' % out_dir)
        engine_path = logs_dir / 'engine.jsonl'
        if not engine_path.is_file():
            raise CheckError('engine log not found: %s' % engine_path)
        for day in days:
            audit_path = out_dir / ('audit-%s.jsonl' % day)
            if not audit_path.is_file():
                raise CheckError('audit log not found for %s: %s'
                                 % (day, audit_path))
            audit_files.append(str(audit_path))
        engine_rows, ignored = _scan_engine(engine_path, days, namespace, out)
        for audit_path in audit_files:
            rows, ign = _scan_audit(Path(audit_path), days, namespace, out)
            audit_rows += rows
            ignored += ign
    except CheckError as ex:
        errors.append(str(ex))
    db_desc, db_positions, db_fills, db_realized = 'none (log-only check)', None, None, None
    if not errors:
        try:
            if db_path is not None:
                positions, fills = _check_sqlite(Path(db_path), namespace, out)
                db_positions, db_fills = _apply_db_rows(positions, fills, 'db', out)
                db_realized = sum(_num(f[6] if isinstance(f, tuple) else f.get('realized_pnl')) or 0
                                  for f in fills)
                db_desc = 'sqlite:%s' % db_path
            if turso:
                mjs = Path(db_mjs) if db_mjs else (
                    Path(__file__).resolve().parent.parent / 'db.mjs')
                positions = _query_via_db_mjs(mjs, POSITIONS_SQL, namespace)
                fills = _query_via_db_mjs(mjs, FILLS_SQL, namespace)
                t_positions, t_fills = _apply_db_rows(positions, fills, 'turso', out)
                t_realized = sum(_num(f.get('realized_pnl')) or 0 for f in fills)
                if db_positions is None:
                    db_positions, db_fills, db_realized = t_positions, t_fills, t_realized
                else:
                    db_positions += t_positions
                    db_fills += t_fills
                    db_realized += t_realized
                db_desc = (db_desc + ' + turso (db.mjs)') if db_path is not None \
                    else 'turso (db.mjs)'
        except CheckError as ex:
            errors.append(str(ex))
    for fill in out['fills']:
        fill.pop('_key', None)
    verdict = 'NOT EMPTY' if (out['fills'] or out['positions'] or out['realized_pnl']) \
        else 'EMPTY'
    summary = {
        'verdict': verdict if not errors else 'ERROR',
        'namespace': namespace,
        'days': days,
        'checked': {
            'logs_dir': str(logs_dir),
            'out_dir': str(out_dir),
            'engine_jsonl': str(logs_dir / 'engine.jsonl'),
            'audit_files': audit_files,
            'db': db_desc,
            'db_positions': db_positions,
            'db_fills': db_fills,
            'db_realized_pnl': db_realized,
        },
        'scanned': {
            'engine_rows': engine_rows,
            'audit_rows': audit_rows,
            'ignored_other_namespace': ignored,
        },
        'fills': out['fills'],
        'positions': out['positions'],
        'realized_pnl': out['realized_pnl'],
        'blocked_signals': out['blocked_signals'],
        'blocked_by_reason': out['blocked_by_reason'],
        'errors': errors,
    }
    return summary, errors


def render_text(summary):
    """Human-readable report (no secrets: only symbols, ids and numbers)."""
    lines = []
    if summary['errors']:
        lines.append('VERDICT: ERROR — the check could not be completed')
        for error in summary['errors']:
            lines.append('  error: %s' % error)
        return '\n'.join(lines)
    verdict = summary['verdict']
    lines.append('VERDICT: %s — paper_v2 %s' % (
        verdict, 'has no fills, no positions and no realised P&L'
        if verdict == 'EMPTY' else 'is NOT empty — do not cut over fresh'))
    lines.append('namespace: %s; days checked: %s' % (
        summary['namespace'], ', '.join(summary['days'])))
    lines.append('logs: %d engine.jsonl rows, %d audit rows scanned '
                 '(%d rows from other namespaces ignored)' % (
                     summary['scanned']['engine_rows'],
                     summary['scanned']['audit_rows'],
                     summary['scanned']['ignored_other_namespace']))
    lines.append('blocked signals (not fills): %d %s' % (
        summary['blocked_signals'],
        dict(summary['blocked_by_reason']) if summary['blocked_by_reason'] else ''))
    checked = summary['checked']
    if checked['db_positions'] is None:
        lines.append('db: %s' % checked['db'])
    else:
        lines.append('db (%s): %d open positions, %d fills, realised P&L %s' % (
            checked['db'], checked['db_positions'], checked['db_fills'],
            checked['db_realized_pnl']))
    for fill in summary['fills']:
        lines.append('  fill: id=%s signal=%s strategy=%s symbol=%s side=%s '
                     'qty=%s price=%s realized_pnl=%s day=%s sources=%s' % (
                         fill['fill_id'], fill['signal_id'], fill['strategy'],
                         fill['symbol'], fill['side'], fill['qty'], fill['price'],
                         fill['realized_pnl'], fill['day'], ','.join(fill['sources'])))
    for pos in summary['positions']:
        lines.append('  position: strategy=%s symbol=%s qty=%s entry_price=%s '
                     'open_positions=%s day=%s sources=%s%s' % (
                         pos['strategy'], pos['symbol'], pos['qty'],
                         pos['entry_price'], pos['open_positions'], pos['day'],
                         ','.join(pos['sources']),
                         ' (%s)' % pos['detail'] if pos.get('detail') else ''))
    for pnl in summary['realized_pnl']:
        lines.append('  realised P&L: %s %s day=%s source=%s (%s)' % (
            pnl['amount'], pnl['unit'], pnl['day'], pnl['source'], pnl['detail']))
    if verdict == 'EMPTY':
        lines.append('paper_v2 can start FRESH in the new DB: nothing to carry over.')
    return '\n'.join(lines)


def main(argv=None):
    parser = Parser(
        description='Check paper_v2 is empty (no fills, positions or realised P&L).')
    parser.add_argument('--logs-dir', default=DEFAULT_LOGS_DIR)
    parser.add_argument('--out-dir', default=DEFAULT_OUT_DIR)
    parser.add_argument('--days', action='append', default=None,
                        help='session days, CSV and/or repeatable '
                             '(default: 2026-11-19,2026-11-20)')
    parser.add_argument('--namespace', default=NAMESPACE)
    parser.add_argument('--db', default=None,
                        help='local SQLite file to run the Turso-side counts against')
    parser.add_argument('--turso', action='store_true',
                        help='run the Turso-side counts live via db.mjs (2 reads)')
    parser.add_argument('--db-mjs', default=None,
                        help='db.mjs path for --turso (default: sibling db.mjs; tests only)')
    parser.add_argument('--report', default=None,
                        help='write the JSON summary to this path')
    parser.add_argument('--json', action='store_true',
                        help='print only the JSON summary')
    args = parser.parse_args(argv)
    try:
        days = parse_days(args.days) if args.days else list(DEFAULT_DAYS)
    except ValueError as ex:
        print('error: %s' % ex, file=sys.stderr)
        return 1
    summary, errors = check(args.logs_dir, args.out_dir, days, args.namespace,
                            db_path=args.db, turso=args.turso, db_mjs=args.db_mjs)
    if args.report:
        try:
            Path(args.report).write_text(json.dumps(summary, indent=2) + '\n')
        except OSError as ex:
            print('error: cannot write report %s: %s' % (args.report, ex),
                  file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_text(summary))
    return 1 if errors else (0 if summary['verdict'] == 'EMPTY' else 2)


if __name__ == '__main__':
    raise SystemExit(main())
