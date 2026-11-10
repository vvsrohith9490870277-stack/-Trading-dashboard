"""Independent audit of the Support Agent v3 (10 Nov 2026).

Each test reproduces a defect found by replaying the real 9 Nov audit export against the v3 plan.
Fixtures use the engine's real event shapes (worker_waiting/singleton_busy, worker_failure with
error_code, quote_batch_response) instead of event types the engine never emits.
"""
import copy, fcntl, json, sqlite3, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_agent as sa
import support_verifiers as sv
import support_install as si

DAY = '2026-11-09'
NS = 'paper_v1'


class MemDB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row

    def batch(self, stm):
        with self.c:
            for sql, args in stm:
                self.c.execute(sql, args)

    def query(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]


def utc(hms):
    """IST wall time on 9 Nov -> UTC ISO string, the way the engine writes source_at."""
    t = datetime.fromisoformat(f'{DAY}T{hms}+05:30').astimezone(sa.timezone.utc)
    return t.isoformat()


class RealShapeReplayTests(unittest.TestCase):
    def setUp(self):
        self.tdb = MemDB()
        for sql in (sa.ROOT / 'schema.sql').read_text(encoding='utf8').split(';'):
            if sql.strip():
                self.tdb.batch([(sql.strip(), ())])
        self.store = sa.SupportStore(':memory:')
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.tdb.c.close()

    def ev(self, hms, event_type, outcome='ok', data=None, run_id='r'):
        self.n += 1
        self.tdb.batch([(
            'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (NS, f'e{self.n:05d}', run_id, DAY, utc(hms), utc(hms), event_type, 'INFO', outcome, json.dumps(data or {})),
        )])

    def fill(self, idx, hms, strategy, symbol, qty, price):
        sid = f'sig-{idx}'
        self.tdb.batch([
            ('INSERT INTO paper_signals(namespace,id,strategy,symbol,bar_date,side,status,reason,created_at,eligible_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
             (NS, sid, strategy, symbol, '2026-11-08', 'BUY', 'filled', 'qualified', utc('12:57:37'), DAY)),
            ('INSERT INTO paper_fills(namespace,signal_id,strategy,symbol,side,qty,price,quote_at,filled_at,reason,realized_pnl,cashflow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
             (NS, sid, strategy, symbol, 'BUY', qty, price, utc(hms), utc(hms), 'fixture', 0.0, -price)),
        ])

    def build_real_shape_session(self):
        # worker failures (engine shape: worker_failure + error_code=unclassified_condition), spread so early ticks see only some
        for i, hms in enumerate(('12:10:42', '12:11:42', '12:13:42', '12:16:42', '12:20:42', '12:22:42')):
            self.ev(hms, 'worker_failure', 'failed', {'error_code': 'unclassified_condition', 'failures': i + 1})
        self.ev('12:27:44', 'monitor_completed')
        self.ev('12:32:05', 'worker_check')
        t = datetime.fromisoformat(f'{DAY}T12:32:30+05:30')
        while t < datetime.fromisoformat(f'{DAY}T12:40:00+05:30'):
            self.ev(t.strftime('%H:%M:%S'), 'worker_waiting', data={'reason': 'singleton_busy'})
            t += timedelta(seconds=31)
        self.ev('12:40:24', 'monitor_completed')
        self.ev('12:41:57', 'worker_check')
        t = datetime.fromisoformat(f'{DAY}T12:42:22+05:30')
        while t < datetime.fromisoformat(f'{DAY}T12:57:40+05:30'):
            self.ev(t.strftime('%H:%M:%S'), 'worker_waiting', data={'reason': 'singleton_busy'})
            t += timedelta(seconds=31)
        self.ev('12:57:47', 'scan_completed')
        self.ev('12:58:12', 'monitor_completed')
        self.ev('14:09:44', 'quote_batch_failed', 'failed', {'error_code': 'RuntimeError', 'duration_ms': 30066})
        self.ev('14:10:20', 'quote_batch_response', 'ok', {'duration_ms': 2400})
        t = datetime.fromisoformat(f'{DAY}T14:12:00+05:30')
        while t < datetime.fromisoformat(f'{DAY}T15:30:00+05:30'):
            self.ev(t.strftime('%H:%M:%S'), 'monitor_completed')
            t += timedelta(seconds=60)
        self.ev('16:17:06', 'eod_completed')
        for i in range(17):
            strategy = 7 if i < 12 else 1
            self.fill(i, f'13:{17 + i // 10:02d}:{(i * 3) % 60:02d}', strategy, 'MAHABANK-EQ' if i == 0 else f'S{i:02d}-EQ', 1, 86.49 if i == 0 else 250.0)

    def ticks(self):
        step = int(sa.CFG['worker_stale_seconds'])
        t = datetime.fromisoformat(f'{DAY}T08:30:00+05:30')
        out = []
        while t <= datetime.fromisoformat(f'{DAY}T16:45:00+05:30'):
            out.append(t.isoformat())
            t += timedelta(seconds=step)
        return out

    def test_policy_cadence_replay_matches_v3_golden_with_full_evidence_and_zero_duplicates(self):
        self.build_real_shape_session()
        ticks = self.ticks()
        out = sa.run_virtual_clock_replay(self.store, self.tdb, NS, DAY, ticks=ticks)
        rows_after_first = len(self.store.query('SELECT * FROM support_findings'))
        again = sa.run_virtual_clock_replay(self.store, self.tdb, NS, DAY, ticks=ticks)
        self.assertEqual(rows_after_first, len(self.store.query('SELECT * FROM support_findings')))
        self.assertEqual({f['finding_id'] for f in out['findings']}, {f['finding_id'] for f in again['findings']})
        by_sev = {k: [d for d in v if d != 'policy_value_missing'] for k, v in again['by_severity'].items()}
        self.assertEqual(by_sev, {
            'SEV1': ['fill_sizing_anomaly', 'sleeve_over_limit_new_buy'],
            'SEV2': ['lock_blocked_gap', 'monitor_staleness', 'scan_missed', 'worker_down'],
            'SEV3': ['error_taxonomy_gap', 'quote_batch_failed', 'signal_to_fill_latency'],
            'SEV4': ['exit_path_unexercised'],
        })
        self.assertEqual(again['chain_failures'], [])
        det = {f['detector']: f for f in again['findings'] if f['detector'] != 'policy_value_missing'}
        # Evidence reflects the whole session, not the first tick that saw the problem.
        self.assertEqual(det['error_taxonomy_gap']['details']['unclassified_count'], 6)
        self.assertEqual(det['fill_sizing_anomaly']['details']['anomaly_count'], 17)
        self.assertEqual(det['signal_to_fill_latency']['details']['count'], 17)
        self.assertEqual(det['monitor_staleness']['details']['last_check'], '15:29:00')
        self.assertEqual(det['worker_down']['details']['first_check'], '12:27:44')
        self.assertEqual(det['worker_down']['details']['blind_window'], '09:15-12:27')
        self.assertEqual(det['worker_down']['status'], 'RESOLVED')
        lg = det['lock_blocked_gap']['details']
        self.assertEqual(lg['occurrence_count'], 2)
        self.assertTrue(lg['escalated'])
        self.assertEqual(det['quote_batch_failed']['status'], 'RESOLVED')
        self.assertTrue(det['quote_batch_failed']['details']['recovered'])

    def test_fill_sizing_flags_any_quantity_other_than_floor_notional_over_price(self):
        self.fill(1, '13:17:58', 1, 'MAHABANK-EQ', 2, 86.49)   # 2 shares where 578 are implied
        self.fill(2, '13:18:00', 1, 'OKSIZE-EQ', 200, 250.0)    # exactly floor(50000/250)
        sa.run_v3_production_detectors(self.store, NS, DAY, now_at=f'{DAY}T16:35:00+05:30', trading_db=self.tdb)
        f = next(x for x in sa.current_findings(self.store, NS, DAY) if x['detector'] == 'fill_sizing_anomaly')
        self.assertEqual(f['details']['anomaly_count'], 1)
        self.assertEqual(f['details']['samples'][0]['expected_qty'], 578)
        self.assertEqual(f['details']['samples'][0]['filled_qty'], 2)

    def test_holiday_is_silent_even_at_0830_policy_hygiene(self):
        res = sa.run_v3_production_detectors(self.store, NS, '2026-11-10', now_at='2026-11-10T08:30:00+05:30', trading_db=self.tdb)
        self.assertFalse(res['trading_day'])
        self.assertEqual(sa.current_findings(self.store, NS, '2026-11-10'), [])

    def test_support_findings_cannot_be_updated_or_replaced(self):
        sa.append_finding(self.store, NS, 'x', 'c', f'{DAY}T10:00:00+05:30', DAY, severity='SEV3')
        with self.assertRaises(PermissionError):
            sa._write_support_only(self.store, [("UPDATE support_findings SET severity='SEV4'", ())])
        with self.assertRaises(PermissionError):
            sa._write_support_only(self.store, [("INSERT OR REPLACE INTO support_findings(namespace) VALUES('x')", ())])

    def test_setting_incomplete_lifecycle_policy_value_enables_timeout_instead_of_crashing(self):
        db = MemDB()
        for sql in (sa.ROOT / 'schema.sql').read_text(encoding='utf8').split(';'):
            if sql.strip():
                db.batch([(sql.strip(), ())])
        sa.init(db)
        db.batch([('INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,signal_id,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('t', 'a1', 'm', DAY, f'{DAY}T10:00:00+05:30', f'{DAY}T10:00:00+05:30', 'paper_order_attempt', 'INFO', 'ok', 's', '{"correlation_id":"s"}'))])
        with mock.patch.object(sa, 'INCOMPLETE_LIFECYCLE_MINUTES', 30):
            young = sa.run_detectors(db, 't', DAY, recorded_at=f'{DAY}T10:10:00+05:30')
            self.assertNotIn('incomplete_lifecycle', {r['detector'] for r in young['results']})
            old = sa.run_detectors(db, 't', DAY, recorded_at=f'{DAY}T10:45:00+05:30')
            il = [r for r in old['results'] if r['detector'] == 'incomplete_lifecycle']
            self.assertEqual(len(il), 1)

    def test_scheduled_pass_refuses_to_run_on_policy_lock_mismatch(self):
        tampered = copy.deepcopy(sa.SUPPORT_POLICY)
        tampered['thresholds']['lock_blocked_gap_seconds'] = 1
        with mock.patch.object(sa, 'SUPPORT_POLICY', tampered):
            with self.assertRaises(ValueError):
                sa.run_scheduled_pass(self.tdb, NS, DAY)


class VerifierAndInstallGateTests(unittest.TestCase):
    def setUp(self):
        self.store = sa.SupportStore(':memory:')

    def tearDown(self):
        self.store.close()

    def _verdicts(self):
        v1 = {'role': 'verifier_1_correctness', 'verdict': 'PASS', 'items': [{'tag': 'MUST', 'id': 'x', 'status': 'PASS'}]}
        v2 = {'role': 'verifier_2_safety', 'verdict': 'PASS', 'hard_veto': False, 'items': [{'tag': 'MUST', 'id': 'binding_safety_scripts', 'status': 'PASS'}]}
        v3 = {'role': 'verifier_3_regression', 'verdict': 'PASS', 'items': [{'tag': 'MUST', 'id': 'y', 'status': 'PASS'}]}
        return v1, v2, v3

    def test_reconciler_reads_binding_scripts_itself_so_a_forged_v2_pass_cannot_overrule_them(self):
        v1, v2, v3 = self._verdicts()
        failing = {'overall': 'FAIL', 'hard_veto': True, 'failed_checks': ['order_route_ast']}
        passing = {'overall': 'PASS', 'hard_veto': False, 'failed_checks': []}
        self.assertEqual(sv.reconcile_verdicts(v1, v2, v3, binding_verdict=failing)['outcome'], 'REVISE')
        self.assertEqual(sv.reconcile_verdicts(v1, v2, v3)['outcome'], 'REVISE')  # missing verdict file fails closed
        self.assertEqual(sv.reconcile_verdicts(v1, v2, v3, binding_verdict=passing)['outcome'], 'READY_FOR_HUMAN')

    def test_reconciler_requires_unanimous_verifier_pass_even_without_items(self):
        v1, v2, v3 = self._verdicts()
        passing = {'overall': 'PASS', 'hard_veto': False, 'failed_checks': []}
        v1_fail = {'role': 'verifier_1_correctness', 'verdict': 'FAIL', 'items': []}
        out = sv.reconcile_verdicts(v1_fail, v2, v3, binding_verdict=passing)
        self.assertNotEqual(out['outcome'], 'READY_FOR_HUMAN')
        v3_warn = {**v3, 'verdict': 'PASS_WITH_WARNINGS'}
        self.assertEqual(sv.reconcile_verdicts(v1, v2, v3_warn, binding_verdict=passing)['outcome'], 'READY_FOR_HUMAN')

    def test_config_json_is_a_locked_file_for_h3(self):
        self.assertFalse(sv.check_locked_files_diff(['automation/paper-trading/config.json'])['passed'])

    def test_engine_class_needs_allowlisted_files_and_real_ast_proof(self):
        no_src = sv.check_change_class_ast('engine_instrumentation_or_locking', ['engine.py'])
        self.assertFalse(no_src['passed'])
        self.assertEqual(no_src['reason'], 'engine_class_ast_proof_missing_sources')
        outside = sv.check_change_class_ast('engine_instrumentation_or_locking', ['strategy_indicators.py'], {'strategy_indicators.py': ''}, {'strategy_indicators.py': ''})
        self.assertFalse(outside['passed'])
        before = 'def decide(x):\n    return x > 1\n\ndef log(m):\n    return m\n'
        sizing_change = 'def decide(x):\n    return x > 2\n\ndef log(m):\n    return m\n'
        log_change = 'def decide(x):\n    return x > 1\n\ndef log(m):\n    return str(m)\n'
        self.assertFalse(sv.check_change_class_ast('engine_instrumentation_or_locking', ['engine.py'], {'engine.py': before}, {'engine.py': sizing_change})['passed'])
        self.assertTrue(sv.check_change_class_ast('engine_instrumentation_or_locking', ['engine.py'], {'engine.py': before}, {'engine.py': log_change})['passed'])

    def test_h1_ast_bans_dynamic_import_and_process_execution_and_support_code_is_clean(self):
        bad = sv.check_order_route_ast({'x.py': 'import subprocess\nimport importlib\neval("1")\n__import__("os")\nimport os\nos.system("ls")\nfrom fyers_apiv3 import fyersModel\n'})
        self.assertFalse(bad['passed'])
        self.assertEqual(len(bad['violations']), 6)
        own = {n: (sa.ROOT / n).read_text(encoding='utf8') for n in ('support_agent.py', 'support_verifiers.py', 'support_install.py')}
        self.assertTrue(sv.check_order_route_ast(own)['passed'], sv.check_order_route_ast(own)['violations'])

    def test_naive_clock_ast_catches_module_qualified_now_and_today(self):
        res = sv.check_naive_clock_ast({'x.py': 'import datetime\na = datetime.datetime.now()\nb = datetime.date.today()\nfrom datetime import date\nc = date.today()\nd = datetime.datetime.now(tz=None)\n'})
        self.assertEqual(len(res['violations']), 4)
        ok = sv.check_naive_clock_ast({'y.py': 'from datetime import datetime\nimport support_agent as sa\nx = datetime.now(sa.IST)\n'})
        self.assertTrue(ok['passed'])

    def test_quiescence_gate_detects_a_held_empty_flock_lock_and_ignores_an_idle_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in si.QUIESCENCE_LOCKS:
                (Path(tmp) / name).touch()   # the engine's lock files are always present and empty
            self.assertTrue(si.check_quiescence_gate(tmp)['quiescent'])
            holder = subprocess.Popen([sys.executable, '-c', (
                'import fcntl,sys,time\n'
                f'f=open({str(Path(tmp) / "monitor.lock")!r},"a+")\n'
                'fcntl.flock(f,fcntl.LOCK_EX)\nprint("held",flush=True)\ntime.sleep(30)\n'
            )], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                gate = si.check_quiescence_gate(tmp)
                self.assertFalse(gate['quiescent'])
                self.assertEqual(gate['active_locks'], ['monitor.lock'])
            finally:
                holder.kill()
                holder.wait()
            with mock.patch.object(si, '_held_lock_inodes', return_value=None):
                self.assertFalse(si.check_quiescence_gate(tmp)['quiescent'])   # unreadable lock state fails closed

    def _approved_batch(self):
        changes = [{'change_id': 'C1', 'change_class': 'support_only', 'commit_sha': 'a' * 40, 'detector': 'd', 'reconciler_outcome': 'READY_FOR_HUMAN'}]
        report = si.format_evening_report('2026-11-12', [], ready_changes=changes)
        si.parse_and_record_approval(self.store, f"APPROVE {report['batch_id']}", 'user_chat', report)
        return report

    def test_install_aborts_on_held_runtime_lock_without_caller_reporting_it(self):
        report = self._approved_batch()
        with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as pt:
            lock = Path(rt) / 'engine.lock'
            lock.touch()
            with open(lock, 'a+') as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                out = si.execute_install(self.store, NS, report['batch_id'], report, '2026-11-12T21:45:00+05:30',
                                         'a' * 40, pt, {'overall': 'PASS', 'hard_veto': False}, runtime_root=rt)
            self.assertEqual(out['status'], 'ABORTED')
            self.assertFalse((Path(pt) / 'current_release.txt').exists())

    def test_install_refuses_a_release_sha_that_was_not_approved(self):
        report = self._approved_batch()
        with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as pt:
            with self.assertRaises(PermissionError):
                si.execute_install(self.store, NS, report['batch_id'], report, '2026-11-12T21:45:00+05:30',
                                   'b' * 40, pt, {'overall': 'PASS', 'hard_veto': False}, runtime_root=rt)

    def test_late_evening_and_weekend_approvals_take_the_next_trading_morning_slot(self):
        self.assertEqual(si.select_install_slot('2026-11-12T20:00:00+05:30')['scheduled_at'], '2026-11-12T21:45:00+05:30')
        self.assertEqual(si.select_install_slot('2026-11-12T22:30:00+05:30')['scheduled_at'], '2026-11-13T07:50:00+05:30')
        self.assertEqual(si.select_install_slot('2026-11-13T23:00:00+05:30')['scheduled_at'], '2026-11-16T07:50:00+05:30')
        self.assertEqual(si.select_install_slot('2026-11-14T11:00:00+05:30')['scheduled_at'], '2026-11-16T07:50:00+05:30')
        self.assertEqual(si.select_install_slot('2026-11-13T07:40:00+05:30')['scheduled_at'], '2026-11-13T07:50:00+05:30')
        self.assertFalse(si.select_install_slot('2026-11-13T08:00:00+05:30')['eligible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
