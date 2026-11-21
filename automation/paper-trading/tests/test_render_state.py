"""Task 2 render state tests: shared store contract over SQLite and Turso HTTP.

Tests-first: written before render_state.py existed.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render_state as rs

SCHEMA_TABLES = {'paper_runner_lease', 'paper_job_runs', 'paper_runtime_kv'}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeTursoSession:
    """Emulates the Turso /v2/pipeline protocol over a local sqlite3 DB.

    Lets the TursoHttpStore run the same contract tests as SqliteStore
    without any network.
    """

    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({'url': url, 'headers': dict(headers or {}), 'json': json})
        results = []
        for req in (json or {}).get('requests', []):
            if req.get('type') == 'close':
                results.append({'type': 'ok', 'response': {'type': 'close'}})
                continue
            stmt = req.get('stmt') or {}
            sql = stmt.get('sql', '')
            # real Hrana protocol: args are typed values, not raw Python
            args = []
            for a in stmt.get('args', []):
                assert isinstance(a, dict) and 'type' in a, 'untyped arg %r' % (a,)
                args.append(None if a['type'] == 'null' else
                            int(a['value']) if a['type'] == 'integer' else
                            float(a['value']) if a['type'] == 'float' else a.get('value'))
            try:
                cur = self.conn.execute(sql, args)
                if cur.description is not None:
                    cols = [d[0] for d in cur.description]
                    def tv(v):
                        if v is None:
                            return {'type': 'null'}
                        if isinstance(v, int):
                            return {'type': 'integer', 'value': str(v)}
                        if isinstance(v, float):
                            return {'type': 'float', 'value': v}
                        return {'type': 'text', 'value': str(v)}
                    rows = [[tv(v) for v in r] for r in cur.fetchall()]
                    results.append({'type': 'ok', 'response': {'type': 'execute', 'result': {
                        'cols': [{'name': c} for c in cols],
                        'rows': rows, 'affected_row_count': 0,
                        'last_insert_rowid': None, 'rows_read': len(rows),
                        'rows_written': 0, 'query_duration_ms': 0}}})
                else:
                    self.conn.commit()
                    results.append({'type': 'ok', 'response': {'type': 'execute', 'result': {
                        'cols': [], 'rows': [],
                        'affected_row_count': cur.rowcount,
                        'last_insert_rowid': str(cur.lastrowid or 0),
                        'rows_read': 0, 'rows_written': cur.rowcount,
                        'query_duration_ms': 0}}})
            except Exception as ex:
                results.append({'type': 'error', 'error': {'message': str(ex), 'code': 'SQLITE_ERROR'}})
        return FakeResponse({'baton': None, 'base_url': url, 'results': results})


def contract_cases(store):
    """Shared behaviour both backends must satisfy. Returns (name, fn) list."""
    def _migrate():
        rs.migrate(store)

    def _kv_roundtrip():
        store.execute("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
                      ('_global', 'k1', 'v1', '2026-11-19T00:00:00+05:30'))
        rows = store.execute("SELECT value FROM paper_runtime_kv WHERE scope=? AND key=?",
                             ('_global', 'k1'))
        assert rows == [{'value': 'v1'}], rows

    def _affected_rows():
        n = store.affected_rows(
            "UPDATE paper_runtime_kv SET value=? WHERE scope=? AND key=?",
            ('v2', '_global', 'k1'))
        assert n == 1, n
        n = store.affected_rows(
            "UPDATE paper_runtime_kv SET value=? WHERE scope=? AND key=?",
            ('v3', '_global', 'missing'))
        assert n == 0, n

    def _server_time_is_int():
        t = store.server_time()
        assert isinstance(t, int) and t > 1700000000, t

    def _execute_many():
        counts = store.execute_many([
            ("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
             ('_global', 'a', '1', '2026-11-19T00:00:00+05:30')),
            ("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
             ('_global', 'b', '2', '2026-11-19T00:00:00+05:30')),
        ])
        assert counts == [1, 1], counts
        rows = store.execute("SELECT key FROM paper_runtime_kv WHERE scope=? ORDER BY key",
                             ('_global',))
        assert [r['key'] for r in rows] == ['a', 'b', 'k1'], rows

    return [('migrate', _migrate), ('kv_roundtrip', _kv_roundtrip),
            ('affected_rows', _affected_rows), ('server_time_is_int', _server_time_is_int),
            ('execute_many', _execute_many)]


class MigrateTests(unittest.TestCase):
    def test_migrate_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp()) / 's.sqlite'
        store = rs.SqliteStore(str(tmp))
        rs.migrate(store)
        rs.migrate(store)
        rows = store.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_%'")
        names = {r['name'] for r in rows}
        self.assertTrue(SCHEMA_TABLES <= names)

    def test_migrate_never_touches_existing_tables(self):
        tmp = Path(tempfile.mkdtemp()) / 's.sqlite'
        store = rs.SqliteStore(str(tmp))
        store.execute("CREATE TABLE paper_positions(namespace TEXT, qty INTEGER)", ())
        store.execute("INSERT INTO paper_positions VALUES('paper_v2', 5)", ())
        before = {(r['name'], r['sql']) for r in store.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'")}
        rs.migrate(store)
        after = {(r['name'], r['sql']) for r in store.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'")}
        # Every pre-existing table definition is byte-identical after migrate.
        self.assertTrue(before <= after)
        rows = store.execute("SELECT * FROM paper_positions")
        self.assertEqual(rows, [{'namespace': 'paper_v2', 'qty': 5}])


class TursoHttpTests(unittest.TestCase):
    def test_turso_url_conversion(self):
        self.assertEqual(rs.http_base_url('libsql://mydb-myorg.turso.io'),
                         'https://mydb-myorg.turso.io')
        self.assertEqual(rs.http_base_url('turso://mydb-myorg.turso.io'),
                         'https://mydb-myorg.turso.io')
        self.assertEqual(rs.http_base_url('https://mydb-myorg.turso.io'),
                         'https://mydb-myorg.turso.io')
        with self.assertRaises(ValueError):
            rs.http_base_url('http://mydb-myorg.turso.io')

    def test_http_store_request_shape(self):
        sess = FakeTursoSession()
        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'TOKENXYZ', session=sess)
        rs.migrate(store)
        store.execute("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
                      ('_global', 'k', 'v', '2026-11-19T00:00:00+05:30'))
        call = sess.calls[-1]
        self.assertTrue(call['url'].endswith('/v2/pipeline'))
        self.assertEqual(call['headers'].get('Authorization'), 'Bearer TOKENXYZ')
        reqs = call['json']['requests']
        self.assertEqual(reqs[0]['type'], 'execute')
        self.assertEqual(reqs[0]['stmt']['args'], [{'type': 'text', 'value': v} for v in
                         ('_global', 'k', 'v', '2026-11-19T00:00:00+05:30')])
        self.assertEqual(reqs[-1]['type'], 'close')
        self.assertNotIn('TOKENXYZ', json.dumps(call['json']))

    def test_token_never_in_exception_text(self):
        sess = FakeTursoSession()
        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'SECRETTOKEN123', session=sess)
        with self.assertRaises(rs.TursoHttpError) as cm:
            store.execute("SELECT * FROM no_such_table_xyz")
        self.assertNotIn('SECRETTOKEN123', str(cm.exception))

    def test_read_counter_counts_reads_only(self):
        sess = FakeTursoSession()
        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'T', session=sess)
        rs.migrate(store)
        base = store.read_count()
        store.execute('SELECT 1 AS n')
        self.assertEqual(store.read_count(), base + 1)
        store.execute("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
                      ('_global', 'k', 'v', '2026-11-19T00:00:00+05:30'))
        store.execute_many([("INSERT INTO paper_runtime_kv(scope,key,value,updated_at) VALUES(?,?,?,?)",
                             ('_global', 'k2', 'v', '2026-11-19T00:00:00+05:30'))])
        self.assertEqual(store.read_count(), base + 1, 'writes never count')
        store.server_time()
        self.assertEqual(store.read_count(), base + 2, 'server_time is a SELECT')

    def test_server_time_used_not_local(self):
        # The store must ask the server for time (strftime('%s','now') in SQL),
        # never compute it from the container clock.
        sess = FakeTursoSession()
        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'T', session=sess)
        t = store.server_time()
        sqls = [r['stmt']['sql'] for c in sess.calls for r in c['json']['requests']
                if r.get('stmt')]
        self.assertTrue(any("strftime('%s','now')" in s for s in sqls), sqls)
        self.assertIsInstance(t, int)


class ContractTests(unittest.TestCase):
    def test_sqlite_store_contract(self):
        tmp = Path(tempfile.mkdtemp()) / 's.sqlite'
        store = rs.SqliteStore(str(tmp))
        for name, fn in contract_cases(store):
            with self.subTest(case=name):
                fn()

    def test_turso_http_store_contract(self):
        sess = FakeTursoSession()
        store = rs.TursoHttpStore('libsql://mydb-myorg.turso.io', 'T', session=sess)
        for name, fn in contract_cases(store):
            with self.subTest(case=name):
                fn()


if __name__ == '__main__':
    unittest.main()
