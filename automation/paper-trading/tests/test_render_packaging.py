"""Tests for Task 8: packaging (render.yaml, Dockerfile, .dockerignore,
.env.render.example, DEPLOY.md, README link)."""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEPLOY = ROOT / 'deploy' / 'render'


class PackagingTests(unittest.TestCase):
    def test_yaml_parses_and_has_required_keys(self):
        import yaml
        data = yaml.safe_load((ROOT / 'render.yaml').read_text())
        svc = data['services'][0]
        self.assertEqual(svc['type'], 'web')
        # Docker runtime: with env/runtime python Render ignores the Dockerfile
        self.assertEqual(svc.get('runtime'), 'docker')
        self.assertNotIn('env', svc)
        self.assertEqual(svc['plan'], 'free')
        self.assertEqual(svc['region'], 'singapore')
        self.assertEqual(svc['healthCheckPath'], '/health')
        self.assertEqual(svc['maxShutdownDelaySeconds'], 30)
        self.assertEqual(svc['dockerfilePath'].lstrip('./'),
                         'deploy/render/Dockerfile')
        # autoDeploy is deprecated; the spec uses autoDeployTrigger: off
        self.assertNotIn('autoDeploy', svc)
        self.assertEqual(svc['autoDeployTrigger'], 'off')
        env = {e['key']: e for e in svc['envVars']}
        # secrets: prompted (sync: false), never inlined
        for key in ('TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN', 'TOKEN_ENC_KEY',
                    'FYERS_APP_ID', 'FYERS_CLIENT_ID', 'FYERS_SECRET_ID',
                    'FYERS_PIN', 'FYERS_TOTP_SECRET'):
            self.assertIn(key, env, key)
            self.assertIs(env[key].get('sync'), False, key)
            self.assertNotIn('value', env[key], key)
        # first deploy = Friday trial defaults, no login
        self.assertEqual(env['PAPER_RUNTIME']['value'], 'render')
        self.assertEqual(env['RENDER_MODE']['value'], 'trial')
        self.assertRegex(env['PAPER_NAMESPACE_OVERRIDE']['value'],
                         r'^paper_render_trial_[a-z0-9_]{1,24}$')
        self.assertEqual(env['FYERS_AUTO_LOGIN']['value'], '0')
        self.assertEqual(env['RENDER_TRIAL_DAY_CAP']['value'], '1000')
        for key in ('PAPER_RUNTIME', 'RENDER_MODE', 'PAPER_NAMESPACE_OVERRIDE',
                    'FYERS_AUTO_LOGIN', 'PAPER_SESSIOND_DISABLE'):
            self.assertNotIn('sync', env[key], key + ': plain value, no sync')

    def test_dockerfile_has_expected_steps(self):
        text = (DEPLOY / 'Dockerfile').read_text()
        self.assertIn('FROM python:3.13', text)
        self.assertIn('node', text.lower())
        self.assertIn('requirements.txt', text)
        self.assertIn('npm ci', text)
        self.assertIn('render_app.py', text)
        self.assertNotIn('.env', text)
        self.assertNotIn('TOKEN', text)

    def test_dockerignore_excludes_venvs_and_git(self):
        text = (ROOT / '.dockerignore').read_text()
        for pat in ('.git', 'node_modules', '.venv', '__pycache__',
                    '*.sqlite'):
            self.assertIn(pat, text, pat)

    def test_env_example_has_no_real_secrets(self):
        text = (DEPLOY / '.env.render.example').read_text()
        for key in ('PAPER_RUNTIME', 'RENDER_MODE', 'TURSO_DATABASE_URL',
                    'TURSO_AUTH_TOKEN', 'TOKEN_ENC_KEY', 'FYERS_AUTO_LOGIN',
                    'PAPER_NAMESPACE_OVERRIDE', 'PAPER_SESSIOND_DISABLE'):
            self.assertIn(key, text, key)
        # every value must be a placeholder, never a real-looking token
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            value = line.split('=', 1)[1].strip()
            self.assertTrue(
                value == '' or '...' in value or value in
                ('render', 'live', '0', '1'),
                'suspicious value in .env.render.example: %r' % line)

    def test_readme_links_deploy_doc(self):
        # The link line lives in the backend README (brief Task 8).
        self.assertIn('deploy/render/DEPLOY.md',
                      (ROOT / 'automation' / 'paper-trading'
                       / 'README.md').read_text())

    def test_deploy_doc_mentions_handoff_and_trial(self):
        text = (DEPLOY / 'DEPLOY.md').read_text()
        self.assertIn('active_runner', text)
        self.assertIn('paper_render_trial_v1', text)
        self.assertIn('handoff', text.lower())
        self.assertIn('/health', text)

    def test_deploy_md_mentions_required_sections(self):
        # Brief 01 Task 8.4 / brief 02 Task 3: every section is present.
        text = (DEPLOY / 'DEPLOY.md').read_text()
        lower = text.lower()
        for section in (
                'what runs where',
                'free-tier limits',
                'create the service',
                'secrets checklist',
                'keep-awake pinger',
                'cron-job.org',
                'uptimerobot',
                '"ok": true',
                'trial runbook',
                'monday handoff',
                'cutover sequence',
                'rollback to the box',
                'never-double-run',
                'calendar renewal',
                'troubleshooting',
                'combined fyers usage check',
                'adaptive stays on the box',
                'pin', 'totp',
                'login invalidates',
        ):
            self.assertIn(section, lower, 'missing DEPLOY.md section: %r'
                          % section)

    def test_deploy_md_commands_parse(self):
        # Brief 02 Task 3: every render_ctl.py line in DEPLOY.md must
        # parse with the real argparse parser, and any command that
        # needs --yes to act must show --yes wherever the text says it
        # acts (a line that says "dry run" is a preview, not an action).
        import render_ctl
        parser = render_ctl.build_parser()
        text = (DEPLOY / 'DEPLOY.md').read_text()
        # subcommand + zero or more --flag [value] groups; a value never
        # starts with --, and backticks / prose end a command.
        pat = re.compile(
            r'render_ctl\.py\s+([a-z-]+'
            r'(?:\s+--[a-z-]+(?:\s+(?!--)[^\s`]+)?)*)')
        found = []
        for line in text.splitlines():
            for m in pat.finditer(line):
                found.append((line, m.group(1)))
        self.assertTrue(found, 'no render_ctl.py commands in DEPLOY.md')
        for line, raw in found:
            argv = raw.split()
            try:
                parsed = parser.parse_args(argv)
            except SystemExit as ex:
                self.fail('DEPLOY.md command does not parse: %r '
                          '(line: %r, %s)' % (raw, line.strip(), ex))
            if parsed.command in ('handoff', 'trial-purge') \
                    and 'dry' not in line.lower():
                self.assertIn('--yes', argv,
                              'acting %s command without --yes: %r'
                              % (parsed.command, raw))
            if parsed.command == 'trial-init':
                self.assertIn('--bar', argv,
                              'trial-init without --bar: %r' % raw)


if __name__ == '__main__':
    unittest.main()
