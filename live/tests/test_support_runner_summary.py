"""Tests for A4 runner wiring (live/support_runner.py): compact summary file,
SUPPORT_STATUS line, render flags, and the read-only write-path proof.

The pure build_compact tests live in automation/paper-trading/tests/
test_support_summary.py; these tests cover the runner side (the file the
§7 gate's G6 mutation step runs for live/support_runner.py).
"""
import json, os, sys, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'automation' / 'paper-trading'))

import support_runner as sr
import support_render as srender

IST = timezone(timedelta(minutes=330))
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
DAY = '2026-11-20'
NS = 'paper_v2'


def _clean_health():
    return {
        'ok': True, 'status': 'ok', 'mode': 'live', 'namespace': NS,
        'session_open': True, 'calendar_days_left': 41,
        'worker': {'state': 'running', 'pid': 1, 'rss_mb': 110, 'restarts': 0,
                   'restarts_10m': 0, 'last_heartbeat_age_s': 10},
        'lease': {'held': True, 'owner_id': 'render-1', 'epoch': 7},
        'fyers_budget': {'day_used': 100, 'per_day_limit': 10000},
        'auth': {'circuit_open': False},
        'box_conflict_check': {'paper_v2_heartbeat_present': False,
                               'paper_v2_heartbeat_age_s': None},
    }


class FakeTursoRO:
    """Read-only stub: raises on anything but SELECT, like live/ro_client.py."""

    def __init__(self, *args, **kwargs):
        pass

    def query(self, sql, args=()):
        if not sql.strip().upper().startswith('SELECT'):
            raise PermissionError('write attempted')
        if 'paper_runtime_kv' in sql:
            return [{'key': 'active_runner', 'value': 'render'}]
        if 'paper_runner_lease' in sql:
            return [{'namespace': NS, 'owner_id': 'render-1', 'owner_kind': 'render',
                     'epoch': 7, 'acquired_unix': int(NOW.timestamp()) - 3600,
                     'expires_unix': int(NOW.timestamp()) + 3600,
                     'heartbeat_at': '2026-11-20T10:00:00+05:30', 'detail_json': '{}'}]
        if 'paper_job_runs' in sql:
            return [{'namespace': NS, 'job': 'scan', 'session_day': DAY, 'attempt': 1,
                     'status': 'ok', 'owner_id': 'render-1',
                     'claimed_unix': int(NOW.timestamp()) - 600,
                     'lease_until_unix': int(NOW.timestamp()) + 300,
                     'started_at': '2026-11-20T08:45:00+05:30',
                     'finished_at': '2026-11-20T08:50:00+05:30', 'exit_code': 0,
                     'result_json': '{}'}]
        return []


class RunnerSummaryTests(unittest.TestCase):
    def test_parser_render_flags(self):
        a = sr.build_parser().parse_args(['--session', '2026-11-20'])
        self.assertIsNone(a.render_health_url)
        self.assertFalse(a.no_render)
        b = sr.build_parser().parse_args(['--session', '2026-11-20',
                                          '--render-health-url', 'https://example.invalid/health',
                                          '--no-render'])
        self.assertEqual(b.render_health_url, 'https://example.invalid/health')
        self.assertTrue(b.no_render)

    def test_run_render_check_no_render(self):
        res = sr.run_render_check(NS, DAY, NOW, no_render=True)
        self.assertEqual(res['findings'], [])
        self.assertEqual(res['not_evaluable'],
                         [{'detector': 'render_check', 'reason': 'render_check_skipped'}])

    def test_run_render_check_clean_health_no_findings(self):
        with mock.patch.object(srender, 'fetch_health', return_value=(200, _clean_health())), \
             mock.patch('ro_client.TursoRO', FakeTursoRO):
            res = sr.run_render_check(NS, DAY, NOW)
        self.assertEqual(res['findings'], [])
        self.assertEqual(res['info']['expected'], 'render')
        self.assertTrue(res['info']['health_ok'])
        self.assertTrue(res['scan_completed'])
        self.assertFalse(res['eod_completed'])

    def test_run_render_check_unreachable_is_critical_not_a_crash(self):
        with mock.patch.object(srender, 'fetch_health', return_value=(None, None)), \
             mock.patch('ro_client.TursoRO', FakeTursoRO):
            res = sr.run_render_check(NS, DAY, NOW)
        crit = [f for f in res['findings'] if f['detector'] == 'render_health_unreachable']
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0]['severity'], 'SEV1')

    def test_runner_prints_status_line(self):
        import tempfile
        findings = [{'detector': 'eod_missed', 'severity': 'SEV1', 'finding_id': 'id-1',
                     'first_event_at': '2026-11-20T16:35:00+05:30',
                     'details': {'action': 'Check why EOD did not complete.'}}]
        with tempfile.TemporaryDirectory() as tmp:
            path, compact = sr.write_compact_summary(
                tmp, findings, DAY, NS,
                {'expected': 'render', 'health_ok': True, 'mode': 'live'}, [], NOW)
            self.assertEqual(path.name, 'summary.compact.json')
            self.assertEqual(json.loads(path.read_text())['status'], 'CRITICAL')
            line = sr.support_status_line(compact, path)
            self.assertEqual(line, f'SUPPORT_STATUS=CRITICAL critical=1 file={path}')

    def test_support_runner_has_no_write_paths(self):
        # Static: no Turso / trading-table writes anywhere in the runner.
        src = (HERE / 'support_runner.py').read_text()
        for bad in ('INSERT INTO paper_', 'UPDATE paper_', 'DELETE FROM paper_',
                    'DELETE FROM support_', 'DROP TABLE', 'TRUNCATE'):
            self.assertNotIn(bad, src, bad)
        # The only local write targets are the findings DB and the report folder.
        self.assertIn('findings.sqlite', src)
        self.assertIn('SUPPORT_REPORT_DIR', src)
        # Write-spy: writing a compact summary touches exactly one file.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sr.write_compact_summary(tmp, [], DAY, NS,
                                     {'expected': 'render', 'health_ok': True, 'mode': 'live'},
                                     [], NOW)
            self.assertEqual(sorted(os.listdir(tmp)), ['summary.compact.json'])


if __name__ == '__main__':
    unittest.main()
