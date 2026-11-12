"""Install completeness guard: import closure, lock digests, rollback on missing files."""
import hashlib, json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import install_check as ic  # noqa: E402
import automation as au  # noqa: E402


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _minimal_tree(root):
    """A complete, importable stand-in for the live engine closure."""
    capital_policy = {'version': 'test_v1', 'n': 1}
    charge_policy = {'valid_through': '2026-12-31'}
    charge_rates = {'x': 1}
    support_policy = {'version': 'support_policy_v3', 'k': 1}
    freeze_lock = {
        'capital_policy_version': 'test_v1',
        'files': {
            'capital_policy.json': _digest(capital_policy),
            'charge_policy.json': _digest(charge_policy),
            'charge_rates.json': _digest(charge_rates),
        },
    }
    support_lock = {
        'support_policy_version': 'support_policy_v3',
        'files': {'support_policy.json': _digest(support_policy)},
    }
    _write(root, 'capital_policy.json', json.dumps(capital_policy))
    _write(root, 'charge_policy.json', json.dumps(charge_policy))
    _write(root, 'charge_rates.json', json.dumps(charge_rates))
    _write(root, 'freeze_lock.json', json.dumps(freeze_lock))
    _write(root, 'support_policy.json', json.dumps(support_policy))
    _write(root, 'support_policy_lock.json', json.dumps(support_lock))
    _write(root, 'runtime_deps.py', 'STAMP = "out/runtime_deps_last.json"\n')
    _write(root, 'support_exit_evidence.py', 'ok = True\n')
    _write(root, 'capital.py', (
        "import json\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "POLICY = json.loads((ROOT / 'capital_policy.json').read_text())\n"
        "LOCK = json.loads((ROOT / 'freeze_lock.json').read_text())\n"
    ))
    _write(root, 'support_agent.py', (
        "import json\nfrom pathlib import Path\n"
        "from support_exit_evidence import ok  # noqa: F401\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "POLICY = json.loads((ROOT / 'support_policy.json').read_text())\n"
        "LOCK = json.loads((ROOT / 'support_policy_lock.json').read_text())\n"
    ))
    _write(root, 'engine.py', 'import capital\nimport support_agent\n')
    # Adaptive code must never be required, even if a comment or unused file exists.
    _write(root, 'adaptive_engine.py', 'raise RuntimeError("adaptive_must_not_be_imported")\n')
    return root


class InstallCheckTests(unittest.TestCase):
    def test_real_tree_closure_includes_the_12_22_incident_files_not_adaptive(self):
        required = set(ic.required_files(ROOT))
        for name in (
            'engine.py', 'runtime_deps.py', 'support_agent.py', 'support_exit_evidence.py',
            'support_policy.json', 'support_policy_lock.json', 'capital_policy.json',
            'freeze_lock.json', 'charge_policy.json', 'charge_rates.json', 'config.json',
            'prefilter_policy.json', 'session_input_policy.json', 'session_input_evidence.json',
            'strategy_rules.json', 'universe.csv', 'official_close.py', 'reconciliation.py',
            'bhavcopy_compare.py', 'strategy_indicators.py', 'install_check.py',
        ):
            self.assertIn(name, required, name)
        self.assertNotIn('adaptive_engine.py', required)
        self.assertNotIn('adaptive_logging.py', required)
        self.assertNotIn('adaptive_promotion.py', required)
        # support_verifiers is not a load-time import of engine/run.sh
        self.assertNotIn('support_verifiers.py', required)
        self.assertNotIn('runtime_deps_last.json', required)
        self.assertNotIn('auth_circuit.json', required)
        self.assertNotIn('fyers_request_budget.json', required)

    def test_real_tree_check_passes(self):
        report = ic.check(ROOT, smoke_imports=False)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['missing'], [])
        self.assertTrue(report['locks']['ok'], report)

    def test_minimal_tree_requires_imported_modules_and_import_time_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _minimal_tree(Path(tmp))
            required = set(ic.required_files(root))
            self.assertTrue({'engine.py', 'support_agent.py', 'support_exit_evidence.py',
                             'support_policy.json', 'support_policy_lock.json',
                             'capital.py', 'capital_policy.json', 'freeze_lock.json'} <= required)
            self.assertNotIn('adaptive_engine.py', required)
            report = ic.check(root, smoke_imports=True)
            self.assertTrue(report['ok'], report)

    def test_missing_support_policy_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _minimal_tree(Path(tmp))
            (root / 'support_policy.json').unlink()
            report = ic.check(root, smoke_imports=False)
            self.assertFalse(report['ok'])
            self.assertIn('support_policy.json', report['missing'])

    def test_support_policy_lock_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _minimal_tree(Path(tmp))
            (root / 'support_policy.json').write_text(json.dumps({'version': 'support_policy_v3', 'k': 99}))
            report = ic.check(root, smoke_imports=False)
            self.assertFalse(report['ok'])
            self.assertTrue(any('support_policy.json' in e for e in report['lock_errors']))

    def test_partial_install_rolls_back_and_leaves_dest_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _minimal_tree(tmp / 'src')
            dest = tmp / 'dest'
            dest.mkdir()
            # dest already has a previous support_agent that does not need the new evidence file
            old = 'old_support_agent = True\n'
            (dest / 'support_agent.py').write_text(old)
            (dest / 'keep_me.txt').write_text('untouched')
            with self.assertRaises(ic.InstallIncomplete):
                # copy only support_agent.py, and disable auto-include so this matches
                # the 12:22 incident (a partial copy that left dest unimportable)
                ic.install_files(src, dest, ['support_agent.py'], auto_include_closure=False, smoke_imports=False)
            self.assertEqual((dest / 'support_agent.py').read_text(), old)
            self.assertEqual((dest / 'keep_me.txt').read_text(), 'untouched')
            self.assertFalse((dest / 'support_exit_evidence.py').exists())
            self.assertFalse((dest / 'support_policy.json').exists())

    def test_auto_include_copies_missing_closure_files_and_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _minimal_tree(tmp / 'src')
            dest = tmp / 'dest'
            dest.mkdir()
            report = ic.install_files(src, dest, ['support_agent.py'], auto_include_closure=True, smoke_imports=True)
            self.assertTrue(report['ok'])
            self.assertIn('support_policy.json', report['auto_included'])
            self.assertIn('support_exit_evidence.py', report['auto_included'])
            self.assertTrue((dest / 'support_policy_lock.json').is_file())
            self.assertTrue(ic.check(dest, smoke_imports=True)['ok'])

    def test_complete_install_succeeds_without_copying_adaptive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _minimal_tree(tmp / 'src')
            dest = tmp / 'dest'
            dest.mkdir()
            report = ic.install_files(src, dest, list(ic.required_files(src)), auto_include_closure=False, smoke_imports=True)
            self.assertTrue(report['ok'])
            self.assertFalse((dest / 'adaptive_engine.py').exists())
            self.assertTrue(ic.check(dest, smoke_imports=True)['ok'])

    def test_refuses_to_install_adaptive_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _minimal_tree(tmp / 'src')
            dest = tmp / 'dest'
            dest.mkdir()
            with self.assertRaises(ic.InstallIncomplete) as ctx:
                ic.install_files(src, dest, ['adaptive_engine.py'])
            self.assertIn('adaptive', str(ctx.exception).lower())
            self.assertFalse((dest / 'adaptive_engine.py').exists())

    def test_install_check_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _minimal_tree(Path(tmp))
            ok = ic.main_for(root, smoke_imports=True)
            self.assertEqual(ok, 0)
            (root / 'support_exit_evidence.py').unlink()
            self.assertEqual(ic.main_for(root, smoke_imports=False), 1)

    def test_health_and_preflight_include_install_report(self):
        self.assertIn('_install_report', au.health.__code__.co_names)
        self.assertIn('_install_report', au.preflight.__code__.co_names)
        # Bind a failing report and confirm preflight status flips.
        failing = {'ok': False, 'missing': ['support_policy.json'], 'lock_errors': [], 'locks': {'ok': False}}
        passing_deps = {'ok': True, 'python_ok': True, 'node_ok': True, 'restored': [], 'healed': False}

        class DummyDb:
            def query(self, *a, **k):
                return []

        class DummyE:
            NS = 'paper_v1'
            ROOT = ROOT
            CFG = {'calendar_valid_through': '2099-12-31', 'poll_seconds': 30}

            def now(self):
                from datetime import datetime
                from zoneinfo import ZoneInfo
                return datetime(2026, 11, 12, 13, 0, tzinfo=ZoneInfo('Asia/Kolkata'))

            def session(self, day):
                return {'session_date': str(day)}

            def is_open(self, t):
                return True

        e = DummyE()
        db = DummyDb()
        with mock.patch.object(au, '_install_report', return_value=failing), \
             mock.patch.object(au.rd, 'check', return_value=passing_deps), \
             mock.patch.object(au.rd, 'ensure', return_value=passing_deps), \
             mock.patch.object(au.rd, 'report_for_audit', return_value={'event': 'runtime_deps_ok'}), \
             mock.patch.object(au.al, 'emit'), \
             mock.patch.object(au.al, 'ensure_daily_export', return_value={'status': 'ok'}):
            h = au.health(e, db)
            self.assertFalse(h['install_ok'])
            self.assertEqual(h['install']['missing'], ['support_policy.json'])
            pf = au.preflight(e, db)
            self.assertEqual(pf['status'], 'install_incomplete')
            self.assertEqual(pf['install']['missing'], ['support_policy.json'])


if __name__ == '__main__':
    unittest.main()
