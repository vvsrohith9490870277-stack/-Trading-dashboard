"""Tests for A4: compact machine-readable summary (support_summary.py).

Schema (schema_version 1, stable key order, at most 4 KB): schema_version,
generated_at_ist, session_day, namespace, runner{expected, health_ok, mode},
status (OK|DEGRADED|CRITICAL), counts{critical,error,warn,info,not_evaluable},
critical[{detector,severity,action,since_ist,finding_id}],
warnings[{detector,severity,action}], not_evaluable[{detector,reason}],
read_only true, actions_taken "none".
"""
import json, unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_summary as ss

RUNNER = {'expected': 'render', 'health_ok': True, 'mode': 'live'}


def _finding(detector, severity, action='Check the finding and verify the cause.'):
    return {'detector': detector, 'severity': severity, 'finding_id': f'id-{detector}',
            'first_event_at': '2026-11-23T10:00:00+05:30',
            'details': {'action': action}}


class SummaryTests(unittest.TestCase):
    def test_compact_schema_keys_and_order(self):
        d = ss.build_compact([], '2026-11-23', 'paper_v2', RUNNER, [], None)
        need = ['schema_version', 'generated_at_ist', 'session_day', 'namespace',
                'runner', 'status', 'counts', 'critical', 'warnings',
                'not_evaluable', 'read_only', 'actions_taken']
        self.assertEqual(list(d.keys())[:len(need)], need)
        self.assertEqual(d['schema_version'], 1)
        self.assertEqual(d['status'], 'OK')
        self.assertTrue(d['read_only'])
        self.assertEqual(d['actions_taken'], 'none')
        self.assertEqual(d['runner'], RUNNER)
        self.assertLessEqual(len(json.dumps(d)), 4096)

    def test_status_levels(self):
        ok = ss.build_compact([], '2026-11-23', 'paper_v2', RUNNER, [], None)
        self.assertEqual(ok['status'], 'OK')
        degraded = ss.build_compact([_finding('feed_http_403', 'SEV3')], '2026-11-23',
                                    'paper_v2', RUNNER, [], None)
        self.assertEqual(degraded['status'], 'DEGRADED')
        self.assertEqual(degraded['counts']['warn'], 1)
        self.assertEqual(degraded['warnings'][0]['detector'], 'feed_http_403')
        ne = ss.build_compact([], '2026-11-23', 'paper_v2', RUNNER,
                              [{'detector': 'render_calendar_days_left', 'reason': 'policy_value_missing'}], None)
        self.assertEqual(ne['status'], 'DEGRADED')
        self.assertEqual(ne['counts']['not_evaluable'], 1)
        crit = ss.build_compact([_finding('eod_missed', 'SEV1', 'Check why EOD did not complete.')],
                                '2026-11-23', 'paper_v2', RUNNER, [], None)
        self.assertEqual(crit['status'], 'CRITICAL')
        self.assertEqual(crit['counts']['critical'], 1)
        self.assertEqual(crit['critical'][0]['detector'], 'eod_missed')
        self.assertEqual(crit['critical'][0]['severity'], 'CRITICAL')
        self.assertEqual(crit['critical'][0]['finding_id'], 'id-eod_missed')

    def test_size_under_4kb_with_many_findings(self):
        many = [_finding(f'feed_http_403-{i}', 'SEV3',
                         f'Check the reference-feed 403 number {i} and verify the feed recovered.')
                for i in range(200)]
        many.append(_finding('eod_missed', 'SEV1'))
        d = ss.build_compact(many, '2026-11-23', 'paper_v2', RUNNER, [], None)
        self.assertLessEqual(len(json.dumps(d)), 4096)
        self.assertEqual(d['status'], 'CRITICAL')
        self.assertGreater(d.get('truncated', 0), 0)
        # The critical finding survives truncation (warnings are cut first).
        self.assertEqual([c['detector'] for c in d['critical']], ['eod_missed'])

    def test_no_secret_like_strings(self):
        sneaky = _finding('feed_http_403', 'SEV3')
        sneaky['details'] = {'action': 'Check the feed.',
                             'token': 'sekrit-value-123',
                             'url': 'libsql://user:sekrit@example.turso.io/db',
                             'password': 'hunter2'}
        d = ss.build_compact([sneaky], '2026-11-23', 'paper_v2', RUNNER, [], None)
        blob = json.dumps(d)
        self.assertNotIn('sekrit-value-123', blob)
        self.assertNotIn('libsql://', blob)
        self.assertNotIn('hunter2', blob)
        self.assertNotIn('password', blob)


if __name__ == '__main__':
    unittest.main()
