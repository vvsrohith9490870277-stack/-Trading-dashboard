"""Install completeness guard for the live paper-trading tree.

Computes the import closure of engine.py and runtime_deps.py (the run.sh
entry points), plus JSON/CSV files those modules load at import time, and
refuses an install that would leave any of them missing. Adaptive modules
are never required and must not be copied into the live runtime.

`./run.sh install-check` runs this file directly (stdlib + venv) so the
check still works when engine.py cannot be imported.
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRY_MODULES = ('engine', 'runtime_deps')
DATA_SUFFIXES = ('.json', '.csv')
LOCK_FILES = ('freeze_lock.json', 'support_policy_lock.json')
ADAPTIVE_PREFIX = 'adaptive_'
# Lock companions: if a locked policy is in the import closure, the lock and
# every file the lock digests must also be present. These are not always read
# in the same statement as json.loads (path aliases, or validate-on-call).
POLICY_LOCK_COMPANIONS = {
    'capital_policy.json': ('freeze_lock.json', 'charge_policy.json', 'charge_rates.json'),
    'freeze_lock.json': ('capital_policy.json', 'charge_policy.json', 'charge_rates.json'),
    'support_policy.json': ('support_policy_lock.json',),
    'support_policy_lock.json': ('support_policy.json',),
}
# First-party import targets that are missing from disk must still be required.
# Stdlib and these third-party names are never treated as missing local modules.
THIRD_PARTY = frozenset({
    'numpy', 'pandas', 'pytz', 'requests', 'urllib3', 'dateutil', 'dotenv',
    'fyers_apiv3', 'nacl', 'cryptography', 'pytest',
})


class InstallIncomplete(Exception):
    """An install was refused or rolled back because the dest tree is incomplete."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


def _is_adaptive(rel):
    name = Path(str(rel)).name
    return name.startswith(ADAPTIVE_PREFIX) or str(rel).replace('\\', '/').startswith(ADAPTIVE_PREFIX)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()


def _module_name(node):
    if isinstance(node, ast.Import):
        return [alias.name.split('.')[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module.split('.')[0]]
    return []


def _is_present_local(name, root):
    top = name.split('.')[0]
    return bool(top) and not _is_adaptive(top) and top != 'tests' and (Path(root) / f'{top}.py').is_file()


def _is_missing_first_party(name, root):
    """A top-level import of a missing sibling module must still be required."""
    top = name.split('.')[0]
    if not top or _is_adaptive(top) or top == 'tests':
        return False
    if (Path(root) / f'{top}.py').is_file():
        return False
    if top in getattr(sys, 'stdlib_module_names', set()) or top in THIRD_PARTY:
        return False
    return top.isidentifier() and top[0].islower() and '.' not in name


def _local_imports(tree, root):
    names = set()
    # Existing same-directory modules, including function-level imports (e.g. strategy_indicators).
    for node in ast.walk(tree):
        for name in _module_name(node):
            if _is_present_local(name, root):
                names.add(name)
    # Missing siblings only count when imported at module top level (not fyers_auth after a sys.path insert).
    for node in tree.body:
        for name in _module_name(node):
            if _is_missing_first_party(name, root):
                names.add(name)
    return names


def _is_read_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in {'open', 'loads'}:
        return True
    if isinstance(func, ast.Attribute) and func.attr in {'read_text', 'read_bytes', 'read', 'open', 'loads', 'DictReader'}:
        return True
    return False


def _runtime_state_file(name):
    text = str(name).replace('\\', '/')
    base = Path(text).name
    if 'out/' in text or text.startswith('out/') or '/out/' in text:
        return True
    return base in {
        'runtime_deps_last.json', 'auth_circuit.json', 'fyers_request_budget.json',
        'heartbeat.json', 'history-repair-progress.json',
    }


def _data_filenames(node, aliases):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.endswith(DATA_SUFFIXES):
            if not _runtime_state_file(child.value) and not _is_adaptive(child.value):
                names.add(Path(child.value).name)
        elif isinstance(child, ast.Name) and child.id in aliases:
            names.add(aliases[child.id])
    return names


def _path_aliases(tree):
    aliases = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        files = []
        for child in ast.walk(stmt.value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.endswith(DATA_SUFFIXES):
                if not _runtime_state_file(child.value) and not _is_adaptive(child.value):
                    files.append(Path(child.value).name)
        if len(files) == 1:
            aliases[stmt.targets[0].id] = files[0]
    return aliases


def _functions_called_at_import(tree):
    called = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
    return called


def _import_time_loaded_files(tree):
    aliases = _path_aliases(tree)
    names = set()
    called = _functions_called_at_import(tree)
    for stmt in tree.body:
        follow = False
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            follow = stmt.name in called
        elif isinstance(stmt, ast.ClassDef):
            follow = False
        else:
            follow = True
        if not follow:
            continue
        if not any(_is_read_call(n) for n in ast.walk(stmt)):
            continue
        names.update(_data_filenames(stmt, aliases))
    return names


def required_files(root):
    """Relative paths that must exist for engine.py / run.sh to import cleanly."""
    root = Path(root)
    pending = list(ENTRY_MODULES)
    seen = set()
    required = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = root / f'{name}.py'
        required.add(path.name)
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        required.update(_import_time_loaded_files(tree))
        for dep in _local_imports(tree, root):
            if dep not in seen:
                pending.append(dep)
    extras = set()
    for name in list(required):
        extras.update(POLICY_LOCK_COMPANIONS.get(name, ()))
    required.update(extras)
    return tuple(sorted(required))


def _lock_errors(root, required):
    errors = []
    root = Path(root)
    for name in LOCK_FILES:
        path = root / name
        if name not in required and not path.is_file():
            continue
        if not path.is_file():
            errors.append(f'missing_lock:{name}')
            continue
        try:
            lock = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as ex:
            errors.append(f'unreadable_lock:{name}:{type(ex).__name__}')
            continue
        files = lock.get('files') if isinstance(lock, dict) else None
        if not isinstance(files, dict) or not files:
            errors.append(f'empty_lock:{name}')
            continue
        for fname, expected in files.items():
            target = root / fname
            if not target.is_file():
                errors.append(f'missing_locked_file:{fname}')
                continue
            try:
                actual = digest(json.loads(target.read_text(encoding='utf-8')))
            except (OSError, json.JSONDecodeError) as ex:
                errors.append(f'unreadable_locked_file:{fname}:{type(ex).__name__}')
                continue
            if actual != expected:
                errors.append(f'lock_mismatch:{fname}')
    return errors


def _smoke_imports(root, modules):
    """Import each required module in a clean subprocess so this process stays untouched."""
    root = Path(root)
    names = [m[:-3] for m in modules if m.endswith('.py')]
    if not names:
        return {'ok': True, 'failed': []}
    script = (
        'import importlib, json, sys, traceback\n'
        f'sys.path.insert(0, {str(root)!r})\n'
        f'names = {names!r}\n'
        'failed = []\n'
        'for name in names:\n'
        '    try:\n'
        '        importlib.import_module(name)\n'
        '    except Exception as ex:\n'
        '        failed.append({"module": name, "error": type(ex).__name__ + ": " + str(ex)})\n'
        'print(json.dumps(failed))\n'
        'sys.exit(1 if failed else 0)\n'
    )
    python = sys.executable
    try:
        proc = subprocess.run(
            [python, '-c', script],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {'ok': False, 'failed': [{'module': '*', 'error': type(ex).__name__ + ': ' + str(ex)}]}
    try:
        failed = json.loads(proc.stdout.strip() or '[]')
    except json.JSONDecodeError:
        failed = [{'module': '*', 'error': (proc.stderr or proc.stdout or 'smoke_import_failed')[:400]}]
    return {'ok': not failed and proc.returncode == 0, 'failed': failed}


def check(root, smoke_imports=False):
    root = Path(root)
    required = list(required_files(root))
    missing = [name for name in required if not (root / name).is_file()]
    lock_errors = _lock_errors(root, required)
    smoke = {'ok': True, 'failed': []}
    if smoke_imports and not missing:
        smoke = _smoke_imports(root, required)
    ok = not missing and not lock_errors and smoke['ok']
    return {
        'ok': ok,
        'missing': missing,
        'required': required,
        'lock_errors': lock_errors,
        'locks': {'ok': not lock_errors, 'errors': lock_errors},
        'smoke': smoke,
        'paper_only': True,
    }


def _normalize(src, paths):
    src = Path(src)
    out = []
    for raw in paths:
        path = Path(raw)
        rel = path.relative_to(src) if path.is_absolute() else path
        rel = Path(str(rel).replace('\\', '/'))
        if rel.is_absolute() or '..' in rel.parts:
            raise InstallIncomplete(f'illegal_install_path:{rel}')
        out.append(str(rel))
    return out


def install_files(src, dest, paths, auto_include_closure=True, smoke_imports=True):
    """Copy `paths` from src to dest, auto-include any missing import-closure files, then verify.

    If the dest tree would be missing a required module or import-time JSON, or a
    policy lock digest would not validate, every copied file is rolled back and
    InstallIncomplete is raised. Adaptive modules are refused.
    """
    src = Path(src)
    dest = Path(dest)
    requested = _normalize(src, paths)
    for rel in requested:
        if _is_adaptive(rel):
            raise InstallIncomplete('adaptive_code_not_installed_into_live')
    required = list(required_files(src))
    auto = []
    if auto_include_closure:
        for name in required:
            if name not in requested and not (dest / name).is_file():
                auto.append(name)
    to_copy = list(dict.fromkeys(requested + auto))
    missing_src = [name for name in to_copy if not (src / name).is_file()]
    if auto_include_closure:
        missing_src.extend(name for name in required if not (src / name).is_file() and name not in missing_src)
    if missing_src:
        report = {'ok': False, 'missing_src': missing_src, 'auto_included': auto}
        raise InstallIncomplete('install_source_incomplete:' + ','.join(missing_src), report)

    dest.mkdir(parents=True, exist_ok=True)
    backup_root = Path(tempfile.mkdtemp(prefix='paper-install-backup-'))
    overwritten = []
    added = []
    copied = []
    try:
        for rel in to_copy:
            source = src / rel
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                overwritten.append(rel)
            else:
                added.append(rel)
            shutil.copy2(source, target)
            copied.append(rel)
        report = check(dest, smoke_imports=smoke_imports)
        report['copied'] = copied
        report['auto_included'] = auto
        if not report['ok']:
            raise InstallIncomplete('install_incomplete_rolled_back', report)
        return report
    except Exception:
        for rel in overwritten:
            shutil.copy2(backup_root / rel, dest / rel)
        for rel in added:
            path = dest / rel
            if path.is_file():
                path.unlink()
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def main_for(root, smoke_imports=True):
    report = check(root, smoke_imports=smoke_imports)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['ok'] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    smoke = True
    if '--no-smoke' in argv:
        smoke = False
        argv.remove('--no-smoke')
    root = Path(argv[0]) if argv else Path(__file__).resolve().parent
    return main_for(root, smoke_imports=smoke)


if __name__ == '__main__':
    sys.exit(main())
