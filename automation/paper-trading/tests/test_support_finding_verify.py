import json, sqlite3, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import support_finding_verify as sfv  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30), name='Asia/Kolkata')
NS = 'paper_v1'
DAY = '2026-11-11'


def _snap(events, fills=None, signals=None):
    path = Path(tempfile.mkdtemp()) / 'snap.sqlite'
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE paper_events(namespace TEXT,event_id TEXT,run_id TEXT,session_day TEXT,source_at TEXT,recorded_at TEXT,event_type TEXT,severity TEXT,outcome TEXT,data_json TEXT,signal_id TEXT)')
    c.execute('CREATE TABLE paper_fills(namespace TEXT,id TEXT,symbol TEXT,strategy INTEGER,side TEXT,price REAL,qty INTEGER,filled_at TEXT,signal_id TEXT,session_day TEXT)')
    c.execute('CREATE TABLE paper_signals(namespace TEXT,id TEXT,symbol TEXT,created_at TEXT)')
    for i, ev in enumerate(events):
        c.execute('INSERT INTO paper_events VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
            ev.get('namespace', NS), ev.get('event_id', f'e{i}'), 'run', ev.get('session_day', DAY), ev['source_at'], ev['source_at'],
            ev['event_type'], 'INFO', ev.get('outcome', 'ok'), ev.get('data_json', '{}'), ev.get('signal_id'),
        ))
    for i, f in enumerate(fills or []):
        c.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?)', (
            f.get('namespace', NS), f.get('id', f'f{i}'), f.get('symbol', 'AAA-EQ'), f.get('strategy', 1),
            f.get('side', 'BUY'), f['price'], f['qty'], f['filled_at'], f.get('signal_id'), DAY,
        ))
    for s in signals or []:
        c.execute('INSERT INTO paper_signals VALUES(?,?,?,?)', (NS, s['id'], s.get('symbol', 'AAA-EQ'), s['created_at']))
    c.commit(); c.close()
    return path


def _logs(dirpath, engine_rows=None, audit_rows=None):
    dirpath = Path(dirpath)
    engine = dirpath / 'engine.jsonl'
    audit = dirpath / f'audit-{DAY}.jsonl'
    if engine_rows is not None:
        engine.write_text(''.join(json.dumps(r) + '\n' for r in engine_rows))
    if audit_rows is not None:
        audit.write_text(''.join(json.dumps(r) + '\n' for r in audit_rows))
    return engine, audit


def _finding(detector, finding_id=None, details=None, severity='SEV2'):
    return {
        'finding_id': finding_id or f'{detector}:{DAY}',
        'detector': detector,
        'severity': severity,
        'status': 'OPEN',
        'session_day': DAY,
        'first_event_at': f'{DAY}T09:00:00+05:30',
        'details': details or {},
        'details_json': json.dumps(details or {}),
    }


def _now(hhmm='16:45'):
    h, m = [int(x) for x in hhmm.split(':')]
    return datetime(2026, 11, 11, h, m, tzinfo=IST)


class FindingVerifiers(unittest.TestCase):
    def test_scan_missed_confirmed_by_both_paths(self):
        # Scan finished 09:13 — after 09:00 deadline. Both sources agree.
        events = [
            {'event_type': 'scan_completed', 'source_at': f'{DAY}T09:13:40+05:30'},
        ]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'command_completed', 'command': 'scan', 'outcome': 'ok', 'at_IST': f'{DAY}T09:13:40+05:30', 'namespace': NS},
        ])
        findings = [_finding('scan_missed')]
        report = sfv.verify_findings(findings, snapshot_path=snap, engine_path=engine, audit_path=audit,
                                     namespace=NS, session_day=DAY, now_ist=_now('16:45'))
        rec = report['findings'][0]
        self.assertEqual(rec['status'], 'confirmed')
        self.assertEqual([v['verdict'] for v in rec['verifiers']], ['CONFIRM', 'CONFIRM'])
        self.assertIn('NOT_VERIFIABLE', rec['review_prompt'])
        self.assertIn('scan_missed', rec['review_prompt'])

    def test_scan_missed_rejected_when_on_time(self):
        events = [{'event_type': 'scan_completed', 'source_at': f'{DAY}T08:51:00+05:30'}]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'command_completed', 'command': 'scan', 'outcome': 'ok', 'at_IST': f'{DAY}T08:51:00+05:30', 'namespace': NS},
        ])
        rec = sfv.verify_findings([_finding('scan_missed')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')
        self.assertEqual([v['verdict'] for v in rec['verifiers']], ['REJECT', 'REJECT'])

    def test_403_confirmed_and_disputed(self):
        payload = json.dumps({'sources': {'corporate_actions': {'error': 'http_403'}, 'asm': {'error': 'ok'}}})
        events = [{'event_type': 'reference_feeds', 'source_at': f'{DAY}T08:29:00+05:30', 'data_json': payload}]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'reference_feeds', 'at_IST': f'{DAY}T08:29:00+05:30', 'namespace': NS,
             'sources': {'corporate_actions': {'error': 'http_403'}}},
        ])
        rec = sfv.verify_findings(
            [_finding('feed_http_403', details={'feed': 'corporate_actions'})],
            snapshot_path=snap, engine_path=engine, audit_path=audit,
            namespace=NS, session_day=DAY, now_ist=_now('16:45'),
        )['findings'][0]
        self.assertEqual(rec['status'], 'confirmed')

        # Same snapshot finding, but logs have no 403 -> disputed (events CONFIRM, logs REJECT)
        engine2, audit2 = _logs(Path(tempfile.mkdtemp()), engine_rows=[
            {'kind': 'heartbeat', 'at_IST': f'{DAY}T09:20:00+05:30', 'namespace': NS},
        ])
        rec2 = sfv.verify_findings(
            [_finding('feed_http_403', details={'feed': 'corporate_actions'})],
            snapshot_path=snap, engine_path=engine2, audit_path=audit2,
            namespace=NS, session_day=DAY, now_ist=_now('16:45'),
        )['findings'][0]
        self.assertEqual(rec2['status'], 'disputed')
        vmap = {v['verifier_id']: v['verdict'] for v in rec2['verifiers']}
        self.assertEqual(vmap['events_sql'], 'CONFIRM')
        self.assertEqual(vmap['log_files'], 'REJECT')

    def test_monitor_staleness_confirmed(self):
        events = [
            {'event_type': 'monitor_completed', 'source_at': f'{DAY}T09:16:00+05:30'},
            {'event_type': 'monitor_completed', 'source_at': f'{DAY}T09:18:49+05:30'},
            {'event_type': 'monitor_completed', 'source_at': f'{DAY}T09:22:54+05:30'},  # 245s gap
        ]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'command_completed', 'command': 'monitor', 'at_IST': f'{DAY}T09:16:00+05:30', 'namespace': NS},
            {'kind': 'command_completed', 'command': 'monitor', 'at_IST': f'{DAY}T09:18:49+05:30', 'namespace': NS},
            {'kind': 'command_completed', 'command': 'monitor', 'at_IST': f'{DAY}T09:22:54+05:30', 'namespace': NS},
        ])
        rec = sfv.verify_findings([_finding('monitor_staleness')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'confirmed')
        gaps = rec['verifiers'][0]['evidence']['gaps']
        self.assertTrue(any(abs(g['gap_seconds'] - 245) < 1 for g in gaps))

    def test_position_marks_stale_confirmed_by_both_verifiers(self):
        events = [{'event_type': 'position_marks_stale', 'source_at': f'{DAY}T10:10:00+05:30',
                   'data_json': json.dumps({'error_code': 'position_mark_stale', 'count': 23, 'threshold': 180})}]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'position_marks_stale', 'at_IST': f'{DAY}T10:10:00+05:30', 'namespace': NS,
             'error_code': 'position_mark_stale', 'count': 23, 'threshold': 180},
        ])
        rec = sfv.verify_findings([_finding('position_marks_stale')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'confirmed')
        vmap = {v['verifier_id']: v['verdict'] for v in rec['verifiers']}
        self.assertEqual(vmap['events_sql'], 'CONFIRM')
        self.assertEqual(vmap['log_files'], 'CONFIRM')

    def test_position_marks_stale_rejected_when_absent(self):
        events = [{'event_type': 'monitor_completed', 'source_at': f'{DAY}T10:10:00+05:30'}]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'monitor_completed', 'at_IST': f'{DAY}T10:10:00+05:30', 'namespace': NS, 'fresh_quotes': 19},
        ])
        rec = sfv.verify_findings([_finding('position_marks_stale')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')

    def test_worker_down_rejected_when_first_check_within_grace(self):
        events = [{'event_type': 'monitor_completed', 'source_at': f'{DAY}T09:16:30+05:30'}]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'worker_heartbeat', 'at_IST': f'{DAY}T09:16:30+05:30', 'namespace': NS},
        ])
        rec = sfv.verify_findings([_finding('worker_down')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')

    def test_unconfirmed_when_logs_missing(self):
        events = [{'event_type': 'scan_completed', 'source_at': f'{DAY}T09:13:40+05:30'}]
        snap = _snap(events)
        rec = sfv.verify_findings(
            [_finding('scan_missed')],
            snapshot_path=snap, engine_path=Path('/tmp/no-such-engine.jsonl'),
            audit_path=Path('/tmp/no-such-audit.jsonl'),
            namespace=NS, session_day=DAY, now_ist=_now('16:45'),
        )['findings'][0]
        vmap = {v['verifier_id']: v['verdict'] for v in rec['verifiers']}
        self.assertEqual(vmap['events_sql'], 'CONFIRM')
        self.assertEqual(vmap['log_files'], 'NOT_VERIFIABLE')
        self.assertEqual(rec['status'], 'unconfirmed')

    def test_exit_path_and_sizing_and_prompts_written(self):
        # No sells -> exit_path warranted after close. Buys sized correctly -> sizing finding rejected.
        fills = [
            {'symbol': 'TCS-EQ', 'side': 'BUY', 'price': 3000.0, 'qty': 16, 'filled_at': f'{DAY}T09:16:00+05:30'},
        ]
        # 50000/3000 = 16 shares if integer_shares floors
        snap = _snap([], fills=fills)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'heartbeat', 'at_IST': f'{DAY}T15:31:00+05:30', 'namespace': NS},
        ], audit_rows=[
            {'event_type': 'paper_fill', 'side': 'BUY', 'price': 3000.0, 'qty': 16, 'at_IST': f'{DAY}T09:16:00+05:30', 'namespace': NS},
        ])
        findings = [
            _finding('exit_path_unexercised', severity='SEV4'),
            _finding('fill_sizing_anomaly', severity='SEV1'),
        ]
        store_path = Path(tempfile.mkdtemp()) / 'findings.sqlite'
        report = sfv.verify_findings(
            findings, snapshot_path=snap, engine_path=engine, audit_path=audit,
            namespace=NS, session_day=DAY, now_ist=_now('16:45'), findings_db=str(store_path),
        )
        by = {f['detector']: f for f in report['findings']}
        self.assertEqual(by['exit_path_unexercised']['status'], 'confirmed')
        self.assertEqual(by['fill_sizing_anomaly']['status'], 'rejected')

        out = Path(tempfile.mkdtemp())
        paths = sfv.write_report(report, out)
        self.assertTrue(Path(paths['verification_json']).exists())
        self.assertTrue(Path(paths['review_prompts']).exists())
        text = Path(paths['review_prompts']).read_text()
        self.assertIn('NOT_VERIFIABLE', text)
        self.assertIn('exit_path_unexercised', text)
        # findings status in the store is not used; only verification rows were written
        c = sqlite3.connect(store_path)
        n = c.execute('SELECT COUNT(*) FROM support_finding_verifications').fetchone()[0]
        c.close()
        self.assertEqual(n, 4)  # 2 findings x 2 verifiers

    def test_does_not_import_production_detectors(self):
        src = Path(sfv.__file__).read_text()
        self.assertNotRegex(src, r'(?m)^\s*(import support_agent|from support_agent\b)')
        self.assertNotIn('run_v3_production_detectors(', src)
        self.assertNotIn('record_episode(', src)

    def test_aggregate_status(self):
        self.assertEqual(sfv.aggregate_status(['CONFIRM', 'CONFIRM']), 'confirmed')
        self.assertEqual(sfv.aggregate_status(['CONFIRM', 'REJECT']), 'disputed')
        self.assertEqual(sfv.aggregate_status(['REJECT', 'REJECT']), 'rejected')
        self.assertEqual(sfv.aggregate_status(['CONFIRM', 'NOT_VERIFIABLE']), 'unconfirmed')

    def test_scan_missed_rejected_when_started_on_time_and_finished_before_open(self):
        events = [
            {'event_type': 'scan_started', 'source_at': f'{DAY}T08:50:51+05:30'},
            {'event_type': 'scan_completed', 'source_at': f'{DAY}T09:01:27+05:30'},
        ]
        snap = _snap(events)
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'scan_started', 'at_IST': f'{DAY}T08:50:51+05:30', 'namespace': NS},
            {'kind': 'command_completed', 'command': 'scan', 'outcome': 'ok', 'at_IST': f'{DAY}T09:01:27+05:30', 'namespace': NS},
        ])
        rec = sfv.verify_findings([_finding('scan_missed')], snapshot_path=snap, engine_path=engine, audit_path=audit,
                                  namespace=NS, session_day=DAY, now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')
        self.assertEqual([v['verdict'] for v in rec['verifiers']], ['REJECT', 'REJECT'])

    def test_exit_path_rejected_when_selftest_sell_in_isolated_namespace(self):
        fills = [{'symbol': 'DLF-EQ', 'side': 'BUY', 'price': 100.0, 'qty': 1, 'filled_at': f'{DAY}T09:16:00+05:30'}]
        snap = _snap([], fills=fills + [
            {'namespace': 'paper_exit_test_v1', 'symbol': 'HDFCBANK-EQ', 'side': 'SELL', 'price': 802.6, 'qty': 1,
             'filled_at': f'{DAY}T10:46:28+05:30'},
        ])
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'paper_fill_attempt', 'at_IST': f'{DAY}T10:46:38+05:30', 'namespace': 'paper_exit_test_v1',
             'symbol': 'RELIANCE-EQ', 'result': {'filled': True, 'side': 'SELL', 'rule': 'trail_touch'}},
            {'kind': 'command_completed', 'command': 'exit-selftest', 'status': 'ok',
             'at_IST': f'{DAY}T10:47:29+05:30', 'namespace': 'paper_exit_test_v1'},
        ])
        rec = sfv.verify_findings([_finding('exit_path_unexercised', severity='SEV4')], snapshot_path=snap,
                                  engine_path=engine, audit_path=audit, namespace=NS, session_day=DAY,
                                  now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')
        self.assertEqual([v['verdict'] for v in rec['verifiers']], ['REJECT', 'REJECT'])

    def test_exit_path_rejected_when_selftest_only_in_remapped_payload(self):
        # Cleanup deleted paper_exit_test_* rows. Snapshot only has paper_v1 engine-log
        # merges whose data_json still carries the real namespace and command.
        payload = json.dumps({
            'at_IST': f'{DAY}T10:47:29+05:30',
            'namespace': 'paper_exit_test_v1',
            'kind': 'command_completed',
            'command': 'exit-selftest',
            'status': 'ok',
        })
        snap = _snap([
            {'namespace': NS, 'event_type': 'command_completed', 'source_at': f'{DAY}T10:47:29+05:30',
             'data_json': payload},
        ], fills=[{'symbol': 'ENGINERSIN-EQ', 'side': 'BUY', 'price': 310.16, 'qty': 161,
                   'filled_at': f'{DAY}T09:16:00+05:30'}])
        tmp = Path(tempfile.mkdtemp())
        engine, audit = _logs(tmp, engine_rows=[
            {'kind': 'command_completed', 'command': 'exit-selftest', 'status': 'ok',
             'at_IST': f'{DAY}T10:47:29+05:30', 'namespace': 'paper_exit_test_v1'},
        ])
        rec = sfv.verify_findings([_finding('exit_path_unexercised', severity='SEV4')], snapshot_path=snap,
                                  engine_path=engine, audit_path=audit, namespace=NS, session_day=DAY,
                                  now_ist=_now('16:45'))['findings'][0]
        self.assertEqual(rec['status'], 'rejected')
        self.assertEqual([v['verdict'] for v in rec['verifiers']], ['REJECT', 'REJECT'])

    def test_exit_path_events_sql_rejects_from_durable_record_alone(self):
        import os
        import support_exit_evidence as evd
        snap = _snap([], fills=[{'symbol': 'TANLA-EQ', 'side': 'BUY', 'price': 695.36, 'qty': 71,
                                 'filled_at': f'{DAY}T09:16:11+05:30'}])
        tmp = Path(tempfile.mkdtemp())
        # No useful engine/audit lines — only the durable record.
        engine, audit = _logs(tmp, engine_rows=[{'kind': 'heartbeat', 'at_IST': f'{DAY}T09:20:00+05:30', 'namespace': NS}])
        path = tmp / 'exit-selftest-evidence.json'
        evd.write_durable_evidence(path, {
            'session_day': DAY,
            'exercised': True,
            'sells': 4,
            'stop_or_target_hits': 1,
            'exit_selftest_commands': 2,
            'samples': [{'at': f'{DAY}T10:47:29+05:30', 'namespace': 'paper_exit_test_v1', 'command': 'exit-selftest'}],
        }, session_day=DAY)
        old = os.environ.get('SUPPORT_EXIT_SELFTEST_EVIDENCE')
        os.environ['SUPPORT_EXIT_SELFTEST_EVIDENCE'] = str(path)
        try:
            rec = sfv.verify_findings([_finding('exit_path_unexercised', severity='SEV4')], snapshot_path=snap,
                                      engine_path=engine, audit_path=audit, namespace=NS, session_day=DAY,
                                      now_ist=_now('16:45'))['findings'][0]
        finally:
            if old is None:
                os.environ.pop('SUPPORT_EXIT_SELFTEST_EVIDENCE', None)
            else:
                os.environ['SUPPORT_EXIT_SELFTEST_EVIDENCE'] = old
        vmap = {v['verifier_id']: v['verdict'] for v in rec['verifiers']}
        self.assertEqual(vmap['events_sql'], 'REJECT')
        self.assertEqual(vmap['log_files'], 'REJECT')
        self.assertEqual(rec['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
