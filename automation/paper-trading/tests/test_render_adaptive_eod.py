"""Tests for the adaptive EOD Render job (engine.py adaptive-eod command).

The 17:45 adaptive_eod schedule job runs `engine.py adaptive-eod`, which
runs live/adaptive_runner.py eod as a subprocess (the runner's own lock,
market-hours refusal and memory caps apply). These tests cover the command
wiring and the subprocess invocation; the runner itself is tested in
live/tests/.
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import engine

IST = ZoneInfo('Asia/Kolkata')


class FakeCompleted:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class AdaptiveEodCommandTests(unittest.TestCase):
    def test_parser_accepts_adaptive_eod(self):
        # Black-box: argparse --help exits 0 for a valid command choice
        # and 2 for an invalid one.
        p = subprocess.run([sys.executable, str(HERE.parent / 'engine.py'),
                            'adaptive-eod', '--help'],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        p = subprocess.run([sys.executable, str(HERE.parent / 'engine.py'),
                            'no-such-command', '--help'],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 2)

    def test_command_classification(self):
        # No engine lock (supervision, like 'support') but refused when
        # another runner is active (mutating: it writes the Turso mirror).
        self.assertIn('adaptive-eod', engine.SUPERVISION_COMMANDS)
        self.assertIn('adaptive-eod', engine.MUTATING_COMMANDS)
        self.assertFalse(engine.uses_engine_lock('adaptive-eod'))

    def test_cli_runs_runner_with_session(self):
        report = {'phase': 'eod', 'session_day': '2026-11-25',
                  'status': 'OK', 'shadow': {'status': 'OK'}}
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.return_value = FakeCompleted(
                0, json.dumps(report, indent=1), '')
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 0)
        self.assertEqual(out, report)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1],
                         str(engine.ROOT.parent / 'live' / 'adaptive_runner.py'))
        self.assertEqual(argv[2:], ['eod', '--session', '2026-11-25'])

    def test_cli_defaults_session_to_now_ist(self):
        # The session defaults to the engine clock's current IST date.
        # Patch engine.now (other suites replace it with a fixed fixture
        # clock, so the real wall-clock date is not a stable expectation).
        fixed = datetime(2026, 11, 25, 10, 0, tzinfo=IST)
        with mock.patch.object(engine.subprocess, 'run') as run, \
                mock.patch.object(engine, 'now', return_value=fixed):
            run.return_value = FakeCompleted(
                0, json.dumps({'phase': 'eod', 'status': 'OK'}), '')
            engine.adaptive_eod_cli()
        argv = run.call_args.args[0]
        self.assertEqual(argv[2:], ['eod', '--session', '2026-11-25'])

    def test_cli_failed_report_exits_1(self):
        report = {'phase': 'eod', 'session_day': '2026-11-25',
                  'status': 'FAILED'}
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.return_value = FakeCompleted(
                0, json.dumps(report), '')
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 1)
        self.assertEqual(out, report)

    def test_cli_skipped_report_exits_0(self):
        # Track A EOD not reconciled by the deadline: nothing to do yet,
        # not a failure of this job.
        report = {'phase': 'eod', 'session_day': '2026-11-25',
                  'status': 'SKIPPED_TRACK_A_EOD_NOT_RECONCILED_BY_DEADLINE'}
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.return_value = FakeCompleted(
                0, json.dumps(report), '')
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 0)

    def test_cli_runner_nonzero_exit_exits_1(self):
        # Runner exception path: exit 1 and a FAILED report on stdout.
        report = {'phase': 'eod', 'session_day': '2026-11-25',
                  'status': 'FAILED', 'error': 'boom'}
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.return_value = FakeCompleted(
                1, json.dumps(report), 'traceback')
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'FAILED')

    def test_cli_non_json_stdout_is_failure(self):
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.return_value = FakeCompleted(
                0, 'not json at all', 'some error')
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'FAILED')
        self.assertEqual(out['error'], 'adaptive_runner_stdout_not_json')

    def test_cli_timeout_is_failure(self):
        with mock.patch.object(engine.subprocess, 'run') as run:
            run.side_effect = subprocess.TimeoutExpired('cmd', 1700)
            out, code = engine.adaptive_eod_cli('2026-11-25')
        self.assertEqual(code, 1)
        self.assertEqual(out['status'], 'FAILED')
        self.assertEqual(out['error'], 'adaptive_eod_timeout')


if __name__ == '__main__':
    unittest.main()
