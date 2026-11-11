"""Restore paper-trading and extra lockfile-backed runtime dirs.

A box VM fork rematerializes /workspace without gitignored runtime directories
(.venv, node_modules). This module is stdlib-only so it can run with system
python3 before the venv exists. Install and rollback scripts must never touch
the runtime dirs this module repairs. Extra node projects (e.g. adaptive-live)
are restored from their own package-lock.json; npm ci is skipped while a
configured flock (state/adaptive.lock) is held, and a failure there never
blocks the paper-trading engine.
"""
from __future__ import annotations
import fcntl, json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAMP = ROOT / 'out' / 'runtime_deps_last.json'
RUNTIME_DIR_NAMES = ('.venv', 'node_modules')

def load_cfg(root=None):
    root = Path(root or ROOT)
    cfg = json.loads((root / 'config.json').read_text())
    d = dict(cfg.get('runtime_deps') or {})
    d['_root'] = root
    return d

def venv_python(cfg=None):
    cfg = cfg or load_cfg()
    return Path(cfg.get('venv_path') or '/workspace/sahil-fyers-turso/.venv') / 'bin' / 'python'

def _requirements(cfg):
    p = Path(cfg.get('requirements_path') or 'requirements.txt')
    return p if p.is_absolute() else cfg['_root'] / p

def _lock(cfg):
    p = Path(cfg.get('package_lock_path') or 'package-lock.json')
    return p if p.is_absolute() else cfg['_root'] / p

def _imports(cfg):
    return list(cfg.get('required_python_imports') or ['numpy', 'pandas', 'requests', 'pyotp'])

def _node_packages(cfg):
    return list(cfg.get('required_node_packages') or ['@libsql/client'])

def _python_ok(cfg):
    py = venv_python(cfg)
    if not py.is_file():
        return False, 'venv_python_missing'
    script = 'import importlib,sys;\n[importlib.import_module(m) for m in sys.argv[1].split(",") if m]'
    r = subprocess.run([str(py), '-c', script, ','.join(_imports(cfg))], capture_output=True, text=True)
    if r.returncode != 0:
        return False, 'required_python_import_failed'
    return True, 'ok'

def _node_ok(cfg):
    root = cfg['_root']
    missing = []
    for name in _node_packages(cfg):
        pkg = root / 'node_modules' / Path(*name.split('/')) / 'package.json'
        if not pkg.is_file():
            missing.append(name)
    if missing:
        return False, 'node_modules_missing:' + ','.join(missing)
    return True, 'ok'

def check(cfg=None):
    cfg = cfg or load_cfg()
    py_ok, py_reason = _python_ok(cfg)
    node_ok, node_reason = _node_ok(cfg)
    extras = check_extra_projects(cfg)
    return {
        'python_ok': py_ok, 'python_reason': py_reason,
        'node_ok': node_ok, 'node_reason': node_reason,
        'ok': py_ok and node_ok,
        'venv_python': str(venv_python(cfg)),
        'requirements': str(_requirements(cfg)),
        'package_lock': str(_lock(cfg)),
        'extra_projects': extras,
        'extra_ok': all(x.get('ok') for x in extras) if extras else True,
    }

def _write_stamp(result):
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps(result, separators=(',', ':')))

def restore_python(cfg, run=subprocess.run):
    req = _requirements(cfg)
    if not req.is_file():
        raise RuntimeError('runtime_deps_requirements_missing')
    venv = Path(cfg.get('venv_path') or '/workspace/sahil-fyers-turso/.venv')
    venv.parent.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    r = run([py, '-m', 'venv', str(venv)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('runtime_deps_venv_create_failed')
    pip = venv / 'bin' / 'pip'
    r = run([str(pip), 'install', '--disable-pip-version-check', '-r', str(req)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('runtime_deps_pip_install_failed')
    return 'venv'

def restore_node(cfg, run=subprocess.run):
    lock = _lock(cfg)
    if not lock.is_file():
        raise RuntimeError('runtime_deps_package_lock_missing')
    r = run(['npm', 'ci', '--omit=dev'], cwd=str(cfg['_root']), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('runtime_deps_npm_ci_failed')
    return 'node_modules'


def _lock_held(path):
    """True only when another process holds an exclusive flock on path."""
    if not path or not Path(path).is_file():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)

def _packages_ok(root, packages):
    missing = []
    for name in packages:
        pkg = Path(root) / 'node_modules' / Path(*name.split('/')) / 'package.json'
        if not pkg.is_file():
            missing.append(name)
    return missing

def check_extra_projects(cfg=None):
    cfg = cfg or load_cfg()
    items = []
    for proj in list(cfg.get('extra_node_projects') or []):
        root = Path(proj['path'])
        packages = list(proj.get('required_node_packages') or ['@libsql/client'])
        missing = _packages_ok(root, packages)
        lock = proj.get('lock_path')
        lockp = Path(lock) if lock and Path(lock).is_absolute() else (root / lock if lock else None)
        items.append({
            'path': str(root),
            'ok': not missing,
            'missing': missing,
            'lock_held': bool(lockp and _lock_held(lockp)),
            'lock_path': str(lockp) if lockp else None,
            'package_lock': str(root / (proj.get('package_lock_path') or 'package-lock.json')),
        })
    return items

def restore_extra_node(proj, run=subprocess.run):
    root = Path(proj['path'])
    lockfile = root / (proj.get('package_lock_path') or 'package-lock.json')
    if not lockfile.is_file():
        raise RuntimeError('runtime_deps_package_lock_missing')
    r = run(['npm', 'ci', '--omit=dev'], cwd=str(root), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('runtime_deps_npm_ci_failed')
    return str(root)

def ensure_extra_projects(cfg=None, run=subprocess.run):
    """Restore extra node_modules from their lockfiles. Skip while a project lock is held."""
    cfg = cfg or load_cfg()
    results = []
    restored = []
    for proj in list(cfg.get('extra_node_projects') or []):
        root = Path(proj['path'])
        packages = list(proj.get('required_node_packages') or ['@libsql/client'])
        item = {'path': str(root), 'ok': False, 'skipped': None, 'restored': False, 'error': None}
        missing = _packages_ok(root, packages)
        if not missing:
            item['ok'] = True
            results.append(item)
            continue
        lock = proj.get('lock_path')
        lockp = Path(lock) if lock and Path(lock).is_absolute() else (root / lock if lock else None)
        if lockp and _lock_held(lockp):
            item['skipped'] = proj.get('lock_held_reason') or 'project_lock_held'
            results.append(item)
            continue
        try:
            restore_extra_node(proj, run=run)
        except Exception as ex:
            item['error'] = str(ex) if str(ex).startswith('runtime_deps_') else 'runtime_deps_npm_ci_failed'
            results.append(item)
            continue
        missing = _packages_ok(root, packages)
        item['ok'] = not missing
        item['restored'] = True
        if item['ok']:
            restored.append(str(root))
        else:
            item['error'] = 'runtime_deps_restore_incomplete'
        results.append(item)
    return results, restored

def ensure(cfg=None, run=subprocess.run):
    """Check runtime dirs and restore from lockfiles if missing. Never deletes them."""
    cfg = cfg or load_cfg()
    state = check(cfg)
    restored = []
    if not state['python_ok']:
        restored.append(restore_python(cfg, run=run))
    if not state['node_ok']:
        restored.append(restore_node(cfg, run=run))
    extras, extra_restored = ensure_extra_projects(cfg, run=run)
    restored.extend(extra_restored)
    state = check(cfg)
    result = {**state, 'restored': restored, 'healed': bool(restored), 'extra_projects': extras, 'extra_ok': all(x.get('ok') or x.get('skipped') for x in extras) if extras else True}
    _write_stamp(result)
    if not result['ok']:
        result['error'] = 'runtime_deps_restore_incomplete'
    return result

def report_for_audit(result):
    status = 'runtime_dependency_restored' if result.get('healed') else ('runtime_dependency_ok' if result.get('ok') else 'runtime_dependency_missing')
    return {'event': status, 'restored': result.get('restored') or [], 'python_ok': result.get('python_ok'), 'node_ok': result.get('node_ok'), 'ok': result.get('ok')}

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--check' in argv:
        r = check(); print(json.dumps(r, separators=(',', ':'))); sys.exit(0 if r['ok'] else 1)
    r = ensure()
    # Keep run.sh stdout as the engine JSON. Report heals/failures on stderr (and --json on stdout).
    payload = json.dumps(r, separators=(',', ':'))
    if '--json' in argv:
        print(payload)
    elif r.get('healed') or not r.get('ok'):
        print(payload, file=sys.stderr)
    sys.exit(0 if r['ok'] else 1)

if __name__ == '__main__':
    main()
