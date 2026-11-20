"""Tests for A1: support_taxonomy classification of the 23 unclassified 19-Nov events.

Fixture: tests/fixtures/support/events_2026-11-19_unclassified.json — a sanitized
mirror of the real 19-Nov event shapes (brief §4 A1 evidence): 11 x
host_freeze_detected (failed), 11 x sessiond_wake (ok), 1 x session_outage_recorded
(failed), all with reason "unclassified_condition". Only types, outcomes and the
rewritten reason mirror the real events; the times in the fixture are illustrative
(pause windows need a start/stop) and are not claimed to be the real event times.
"""
import json, sqlite3, unittest
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support_agent as sa
import support_taxonomy as st

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'support' / 'events_2026-11-19_unclassified.json'
DAY = '2026-11-19'
NS = 'paper_v2'


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


def _trading_db():
    db = MemDB()
    for sql in (Path(__file__).resolve().parents[1] / 'schema.sql').read_text().split(';'):
        if sql.strip():
            db.batch([(sql.strip(), ())])
    return db


def _insert_event(db, event_id, event_type, outcome, source_at, data, run_id='r1', severity='WARN'):
    db.batch([(
        'INSERT INTO paper_events(namespace,event_id,run_id,session_day,source_at,recorded_at,event_type,severity,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (NS, event_id, run_id, DAY, source_at, source_at, event_type, severity, outcome, json.dumps(data)),
    )])


def _run(db):
    store = sa.SupportStore(':memory:')
    out = sa.run_v3_production_detectors(store, NS, DAY, now_at=f'{DAY}T16:35:00+05:30', trading_db=db, observation_only=True)
    findings = sa.current_findings(store, NS, DAY)
    store.close()
    return out, {f['detector']: f for f in findings}


class TaxonomyTests(unittest.TestCase):
    def test_19nov_events_all_classified(self):
        ev = json.loads(FIXTURE.read_text())
        self.assertEqual(len(ev), 23)
        cls = [st.classify_event(e) for e in ev]
        unc = sum(1 for c in cls if c['class'] in (None, 'unclassified'))
        counts = Counter(c['class'] for c in cls)
        self.assertEqual(unc, 0)
        self.assertEqual(counts['box_host_pause'], 11)
        self.assertEqual(counts['box_host_resume'], 11)
        self.assertEqual(counts['session_outage'], 1)

    def test_unknown_event_still_reported_with_counts(self):
        db = _trading_db()
        _insert_event(db, 'unk-1', 'flux_capacitor_malfunction', 'failed',
                      f'{DAY}T10:00:00+05:30', {'reason': 'unclassified_condition'})
        _insert_event(db, 'frz-1', 'host_freeze_detected', 'failed',
                      f'{DAY}T11:00:00+05:30',
                      {'reason': 'unclassified_condition', 'started': f'{DAY}T11:00:00+05:30',
                       'stopped': f'{DAY}T11:02:00+05:30', 'frozen_seconds': 120})
        _, by_det = _run(db)
        gap = by_det.get('error_taxonomy_gap')
        self.assertIsNotNone(gap, 'truly unknown events must still raise error_taxonomy_gap')
        self.assertEqual(gap['details']['unclassified_count'], 1)
        self.assertIn('flux_capacitor_malfunction|failed', gap['details']['counts_by_type'])
        self.assertEqual(gap['details']['counts_by_type']['flux_capacitor_malfunction|failed'], 1)
        self.assertNotIn('host_freeze_detected|failed', gap['details']['counts_by_type'])
        # The known host freeze is a box_host_pause finding, not unclassified.
        pause = by_det.get('box_host_pause')
        self.assertIsNotNone(pause)
        self.assertEqual(pause['details']['pause_count'], 1)
        self.assertEqual(pause['details']['total_paused_seconds'], 120)
        db.c.close()

    def test_host_pause_explains_overlapping_staleness_gap(self):
        db = _trading_db()
        # One host pause 10:00-10:15 (900 s).
        _insert_event(db, 'frz-1', 'host_freeze_detected', 'failed',
                      f'{DAY}T10:00:00+05:30',
                      {'reason': 'unclassified_condition', 'started': f'{DAY}T10:00:00+05:30',
                       'stopped': f'{DAY}T10:15:00+05:30', 'frozen_seconds': 900})
        # Monitor checks: gap 09:30->10:20 overlaps the pause; gaps after it do not.
        for i, at in enumerate(('09:30:00', '10:20:00', '13:00:00', '13:05:00')):
            _insert_event(db, f'mon-{i}', 'monitor_completed', 'ok',
                          f'{DAY}T{at}+05:30', {'check': 'ok'}, run_id=f'mon-{i}', severity='INFO')
        _, by_det = _run(db)
        ms = by_det.get('monitor_staleness')
        self.assertIsNotNone(ms)
        gaps = ms['details']['gaps']
        self.assertEqual(len(gaps), 3)
        overlapping = [g for g in gaps if g['from_ist'] == '09:30:00']
        self.assertEqual(len(overlapping), 1)
        self.assertTrue(overlapping[0]['explained_by_host_pause'])
        for g in gaps:
            if g['from_ist'] != '09:30:00':
                self.assertFalse(g['explained_by_host_pause'])
        db.c.close()

    def test_session_outage_severity_from_payload(self):
        base = {'event_type': 'session_outage_recorded', 'outcome': 'failed'}
        # Missed trading session (kind/scan or eod missing, or a missed_session field).
        self.assertEqual(st.classify_event({**base, 'data_json': json.dumps({'kind': 'scan_missing'})})['severity'], 'CRITICAL')
        self.assertEqual(st.classify_event({**base, 'data_json': json.dumps({'kind': 'eod_missing'})})['severity'], 'CRITICAL')
        self.assertEqual(st.classify_event({**base, 'data_json': json.dumps({'missed_session': '2026-11-19'})})['severity'], 'CRITICAL')
        # Coverage failure (no monitoring at all, or coverage below full).
        self.assertEqual(st.classify_event({**base, 'data_json': json.dumps({'checks': 0, 'coverage': 0.0})})['severity'], 'CRITICAL')
        self.assertEqual(st.classify_event({**base, 'data_json': json.dumps({'kind': 'monitor_coverage_low', 'coverage': 0.5})})['severity'], 'CRITICAL')
        # No kind/coverage information survives audit clean() — the real 19-Nov
        # outage shape: ERROR, matching the severity recorded for that event.
        plain = st.classify_event({**base, 'data_json': json.dumps({'reason': 'unclassified_condition'})})
        self.assertEqual(plain['class'], 'session_outage')
        self.assertEqual(plain['severity'], 'ERROR')

    def test_no_change_for_sessions_without_host_events(self):
        db = _trading_db()
        sa.build_nov9_replay_fixture(db, 'paper_v1')
        store = sa.SupportStore(':memory:')
        out = sa.run_virtual_clock_replay(store, db, 'paper_v1', '2026-11-09')
        store.close()
        db.c.close()
        # No host events in the 9-Nov fixture: findings identical to before A1
        # except the A2 severity change (scan_missed / monitor_staleness are
        # CRITICAL / SEV1 now).
        self.assertEqual(out['by_severity'], {
            'SEV1': ['fill_sizing_anomaly', 'monitor_staleness', 'scan_missed', 'sleeve_over_limit_new_buy'],
            'SEV2': ['lock_blocked_gap', 'worker_down'],
            'SEV3': ['error_taxonomy_gap', 'quote_batch_failed', 'signal_to_fill_latency'],
            'SEV4': ['exit_path_unexercised'],
        })
        self.assertNotIn('box_host_pause', [f['detector'] for f in out['findings']])


if __name__ == '__main__':
    unittest.main()
