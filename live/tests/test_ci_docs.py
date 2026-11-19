"""Task 9 tests: adaptive CI workflow parses and has exactly the three jobs;
README commands exist in the runner's argument parser."""
import sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / 'automation' / 'paper-trading'))
import adaptive_runner as ar


class CiWorkflowTests(unittest.TestCase):
    def test_workflow_parses_with_exactly_three_jobs_and_read_only_permissions(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML not installed')
        p = ROOT / '.github' / 'workflows' / 'adaptive-ci.yml'
        self.assertTrue(p.exists())
        doc = yaml.safe_load(p.read_text())
        self.assertEqual(set(doc['jobs']), {'adaptive-python', 'adaptive-dashboard', 'adaptive-truncation'})
        self.assertEqual(doc.get('permissions'), {'contents': 'read'})
        text = p.read_text()
        self.assertNotIn('GITHUB_TOKEN', text)
        self.assertNotIn('secrets.', text)
        # FYERS/TURSO may only appear in the header comment stating there is
        # no network access to them (Appendix B wording), never as a credential.
        for line in text.splitlines():
            if 'FYERS' in line or 'TURSO' in line:
                self.assertTrue(line.lstrip().startswith('#'), line)
        # The adaptive-python job also runs the live/ unit tests (command A).
        steps = doc['jobs']['adaptive-python']['steps']
        runs = [s.get('run', '') for s in steps]
        self.assertTrue(any('unittest discover' in r for r in runs))
        live_steps = [s for s in steps if 'live' in str(s.get('working-directory', ''))]
        self.assertTrue(any('unittest discover' in s.get('run', '') for s in live_steps),
                        'adaptive-python must run the live/ unit tests')

    def test_readme_commands_exist_in_runner_parser(self):
        readme = (HERE / 'README.md').read_text()
        help_text = ar.build_parser().format_help()
        for flag in ('--session', '--no-wait', '--catch-up', '--force', '--from', '--to', '--retry-missing'):
            self.assertIn(flag, help_text, flag)
            self.assertIn(flag, readme, flag)
        for cmd in ('run_adaptive.sh preflight', 'run_adaptive.sh eod', 'run_adaptive.sh backfill'):
            self.assertIn(cmd, readme, cmd)
        for var in ('TRACK_A_ROOT', 'ADAPTIVE_REPORT_DIR', 'ADAPTIVE_PYTHON'):
            self.assertIn(var, readme, var)
        for status in ('SIMULATED', 'BLOCKED_PIT_SERIES_ARCHIVE_P1', 'BLOCKED_STALE_INPUT',
                       'WARM_START_ONLY_NO_FORWARD_SESSION_YET'):
            self.assertIn(status, readme, status)
        self.assertIn('runner-<phase>-FAILED.json', readme)
        self.assertIn('observation-only', readme)
        self.assertIn('SELECT', readme)


if __name__ == '__main__':
    unittest.main()
