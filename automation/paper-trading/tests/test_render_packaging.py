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
        self.assertEqual(svc['env'], 'python')
        self.assertEqual(svc['plan'], 'free')
        self.assertEqual(svc['region'], 'singapore')
        self.assertEqual(svc['healthCheckPath'], '/health')
        self.assertEqual(svc['maxShutdownDelaySeconds'], 30)
        self.assertEqual(svc['dockerfilePath'],
                         'deploy/render/Dockerfile')
        # autoDeploy is deprecated; the spec uses autoDeployTrigger: off
        self.assertNotIn('autoDeploy', svc)
        self.assertEqual(svc['autoDeployTrigger'], 'off')
        env_names = {e['key']: e for e in svc['envVars']}
        for key in ('PAPER_RUNTIME', 'RENDER_MODE', 'TURSO_DATABASE_URL',
                    'TURSO_AUTH_TOKEN', 'TOKEN_ENC_KEY', 'FYERS_AUTO_LOGIN',
                    'PAPER_NAMESPACE_OVERRIDE'):
            self.assertIn(key, env_names, key)
            self.assertFalse(env_names[key].get('sync', True),
                             key + ' must be sync: false')
        self.assertEqual(env_names['PAPER_RUNTIME'].get('value'), 'render')

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
        self.assertIn('deploy/render/DEPLOY.md',
                      (ROOT / 'README.md').read_text())

    def test_deploy_doc_mentions_handoff_and_trial(self):
        text = (DEPLOY / 'DEPLOY.md').read_text()
        self.assertIn('active_runner', text)
        self.assertIn('paper_render_trial_v1', text)
        self.assertIn('handoff', text.lower())
        self.assertIn('/health', text)


if __name__ == '__main__':
    unittest.main()
