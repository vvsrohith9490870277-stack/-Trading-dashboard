"""paper_v2 cutover (Task 1): protected namespaces and config-bound parity ledger."""
import ast, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
HERE = Path(__file__).resolve().parent
LIVE = HERE.parent
ROOT = LIVE.parent
sys.path.insert(0, str(HERE))
from track_a_parity import live_ledger


def rowcounts_protected():
    """rowcounts.py runs a live Turso query and writes a file at import time, so
    the PROTECTED constant is read statically instead of imported."""
    tree = ast.parse((LIVE / 'rowcounts.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'PROTECTED' for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('PROTECTED not found in rowcounts.py')


class FakeRO:
    def __init__(self):
        self.calls = []

    def query(self, sql, args=()):
        self.calls.append((sql, tuple(args)))
        return []


class NamespaceCutoverTests(unittest.TestCase):
    def test_protected_namespaces_cover_paper_v1_paper_v2_demo(self):
        for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1'):
            self.assertIn(ns, rowcounts_protected())
        cfg = json.loads((LIVE / 'adaptive_live_config.json').read_text())
        for ns in ('paper_v1', 'paper_v2', 'paper_demo_v1'):
            self.assertIn(ns, cfg['protected_namespaces'])

    def test_protected_includes_live_config_namespace(self):
        cfg_path = ROOT / 'automation' / 'paper-trading' / 'config.json'
        if not cfg_path.is_file():
            self.skipTest('repo config.json absent')
        live_ns = json.loads(cfg_path.read_text())['namespace']
        cfg = json.loads((LIVE / 'adaptive_live_config.json').read_text())
        self.assertIn(live_ns, cfg['protected_namespaces'])

    def test_parity_ledger_binds_config_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'config.json').write_text(json.dumps({'namespace': 'paper_v2'}))
            with mock.patch.dict(os.environ, {'TRACK_A_ROOT': tmp}):
                ro = FakeRO()
                live_ledger(ro)
                bound = {a for _, args in ro.calls for a in args}
                self.assertTrue(ro.calls)
                self.assertEqual(bound, {'paper_v2'})
                ro2 = FakeRO()
                live_ledger(ro2, ns='paper_v1')
                bound2 = {a for _, args in ro2.calls for a in args}
                self.assertEqual(bound2, {'paper_v1'})

    def test_parity_import_needs_no_track_a_root(self):
        env = dict(os.environ, TRACK_A_ROOT='/nonexistent')
        env.pop('ADAPTIVE_REPORT_DIR', None)
        r = subprocess.run(
            [sys.executable, '-c', 'import track_a_parity; print("import ok")'],
            cwd=str(LIVE), env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('import ok', r.stdout)


if __name__ == '__main__':
    unittest.main()
