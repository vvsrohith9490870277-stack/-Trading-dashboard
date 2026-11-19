"""Task 8 tests: monotonic phase timings exist, are non-negative floats, and
the timing code does not change simulation outputs (bit-identical fixture run).
"""
import json, sys, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
LIB = HERE.parent / 'automation' / 'paper-trading'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(LIB)); sys.path.insert(0, str(LIB / 'tests'))
import adaptive_daily as ad
import adaptive_engine as ae
import adaptive_runner as ar
from adaptive_fixtures import fresh_db, make_market, seed_pit


class TimingHelperTests(unittest.TestCase):
    def test_phase_timer_keys_are_non_negative_floats(self):
        t = ad.PhaseTimer()
        t.start('spawn'); t.stop('spawn')
        t.start('persist'); t.stop('persist')
        for k, v in t.timings.items():
            self.assertIsInstance(v, float)
            self.assertGreaterEqual(v, 0.0)
        self.assertIn('spawn', t.timings)
        self.assertIn('persist', t.timings)

    def test_runner_timed_context_manager(self):
        timings = {}
        with ar._timed(timings, 'snapshot'):
            pass
        self.assertIn('snapshot', timings)
        self.assertIsInstance(timings['snapshot'], float)
        self.assertGreaterEqual(timings['snapshot'], 0.0)

    def test_simulation_outputs_bit_identical_with_timing_code(self):
        db, ns = fresh_db(LIB), 'eq'
        days, df, idx = make_market()
        seed_pit(db, ns, days, sorted(df.symbol.unique()))
        t = ad.PhaseTimer()
        t.start('simulate_3')
        r1 = ae.simulate_track_window(db, ns, 3, {}, df, idx, days[-105:-45], days[-45:])
        t.stop('simulate_3')
        r2 = ae.simulate_track_window(db, ns, 3, {}, df, idx, days[-105:-45], days[-45:])
        self.assertEqual(json.dumps(r1['daily_records'], sort_keys=True, default=str),
                         json.dumps(r2['daily_records'], sort_keys=True, default=str))
        self.assertEqual(json.dumps(r1['closed_trades'], sort_keys=True, default=str),
                         json.dumps(r2['closed_trades'], sort_keys=True, default=str))
        self.assertIn('simulate_3', t.timings)
        self.assertGreaterEqual(t.timings['simulate_3'], 0.0)


class RunnerEodTimingsTests(unittest.TestCase):
    def test_eod_report_carries_phase_timings(self):
        class FakeRO:
            def query(self, sql, args=()):
                if 'paper_eod' in sql:
                    return [{'report_json': json.dumps({'ledger_reconciled': True, 'capped_master_ledger': {'sleeves': {}}})}]
                if 'paper_fills' in sql:
                    return []
                return []
            def many(self, queries):
                return [[{'d': 1}]] * len(queries)

        fake_ms = mock.MagicMock()
        fake_ms.refresh.return_value = {}
        day = '2026-11-11'
        state = HERE.parent / 'state'
        for name in (f'track_a_eod-{day}.json', f'track_a_live_fills-{day}.json'):
            try:
                (state / name).unlink()
            except FileNotFoundError:
                pass
        import subprocess as sp
        fake_proc = sp.CompletedProcess([], 0, stdout=json.dumps({'session_day': day}) + '\n', stderr='')
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            Path(t, 'config.json').write_text(json.dumps({'namespace': 'paper_v1'}))
            old_track_a = ar.TRACK_A
            ar.TRACK_A = Path(t)
            try:
                with mock.patch.object(ar.ag, 'assert_runnable', return_value=True), \
                     mock.patch.dict(sys.modules, {'market_snapshot': fake_ms}), \
                     mock.patch.object(ar, 'protected_counts', return_value={}), \
                     mock.patch.object(sp, 'run', return_value=fake_proc):
                    out = ar.eod(day, no_wait=True, ro=FakeRO())
            finally:
                ar.TRACK_A = old_track_a
        timings = out.get('timings_seconds') or {}
        for k in ('snapshot', 'protected_counts_before', 'daily', 'protected_counts_after'):
            self.assertIn(k, timings, timings)
            self.assertIsInstance(timings[k], float)
            self.assertGreaterEqual(timings[k], 0.0)
        self.assertEqual(out['status'], 'OK')
        for name in (f'track_a_eod-{day}.json', f'track_a_live_fills-{day}.json'):
            try:
                (state / name).unlink()
            except FileNotFoundError:
                pass


if __name__ == '__main__':
    unittest.main()
