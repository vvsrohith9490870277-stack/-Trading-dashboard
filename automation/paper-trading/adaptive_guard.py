"""Isolation guards for the adaptive shadow runner (H1-H4, C-06, E12). Library only: nothing here is scheduled,
installed or wired into the live runtime, and nothing here places, routes or simulates live orders.

- can_run_heavy_work(): heavy work (simulation, calibration, backfill, full test runs) only in the post-close window
  heavy_work_window_start_ist .. backfill_window_end_ist (IST), never 09:15-15:30.
- kill switch: a file at kill_switch_relative_path stops every adaptive entry point.
- scrubbed_env(): minimal environment with no credentials or tokens, TZ pinned to Asia/Kolkata.
- assert_frozen_files(): frozen Track A policy files must match freeze_lock.json (fail closed).
- systemd_run_command(): hard MemoryMax/CPUQuota wrapper (built, never executed here).
- forbidden_import_violations(): transitive import scan proving no broker/order/live-engine module is reachable.
"""
import ast, json, os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import adaptive_policy as apol
import capital as cap

AP = apol.POLICY
ROOT = Path(__file__).resolve().parent
IST = ZoneInfo('Asia/Kolkata')

# Modules that can reach a broker, live orders, live market data or the live engine loop. Adaptive code must never
# import any of them, directly or transitively.
FORBIDDEN_MODULES = frozenset({
    'engine', 'automation', 'guardian', 'reconciliation', 'audit', 'session_inputs', 'reference_feeds',
    'history_repair', 'support_agent', 'support_install', 'support_verifiers', 'pauses', 'bhavcopy_compare',
    'fyers_apiv3', 'fyers_api', 'kiteconnect', 'requests', 'libsql_client', 'libsql_experimental',
})
ADAPTIVE_ENTRY_MODULES = (
    'adaptive_engine', 'adaptive_policy', 'adaptive_promotion', 'adaptive_logging', 'atomic_gates', 'calibration',
    'residualized_check', 'trial_counting', 'pit_archive', 'adaptive_ci', 'adaptive_guard', 'adaptive_observation',
    'adaptive_placebo',
)
ENV_ALLOWLIST = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'PYTHONPATH', 'PYTHONHASHSEED')
CREDENTIAL_MARKERS = ('TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'API_KEY', 'APIKEY', 'AUTH', 'CREDENTIAL', 'FYERS', 'TURSO', 'GH_', 'GITHUB')


def _hhmm(v):
    h, m = str(v).split(':')
    return time(int(h), int(m))


def ist_now():
    return datetime.now(IST)


def to_ist(now):
    if now is None:
        return ist_now()
    if now.tzinfo is None:
        raise ValueError('naive_datetime_refused_use_aware_ist_or_utc')
    return now.astimezone(IST)


def can_run_heavy_work(now=None):
    """True only inside [heavy_work_window_start_ist, 24:00) or [00:00, backfill_window_end_ist) IST."""
    t = to_ist(now).time()
    start = _hhmm(AP['heavy_work_window_start_ist'])
    end = _hhmm(AP['backfill_window_end_ist'])
    return t >= start or t < end


def can_run_backfill(now=None):
    """Archive backfill window (C-01): [backfill_window_start_ist, backfill_window_end_ist) IST, wrapping midnight."""
    t = to_ist(now).time()
    start = _hhmm(AP['backfill_window_start_ist'])
    end = _hhmm(AP['backfill_window_end_ist'])
    return t >= start or t < end


def kill_switch_path(root=None):
    return (Path(root) if root else ROOT) / AP['kill_switch_relative_path']


def kill_switch_engaged(root=None):
    return kill_switch_path(root).exists()


def assert_runnable(now=None, root=None, heavy=True):
    """Single entry check for any adaptive job: kill switch, post-close window, frozen files. Fails closed."""
    if kill_switch_engaged(root):
        raise RuntimeError('adaptive_kill_switch_engaged')
    if heavy and not can_run_heavy_work(now):
        raise RuntimeError('adaptive_heavy_work_outside_post_close_window')
    assert_frozen_files()
    return True


def scrubbed_env(source=None):
    """Minimal child environment: allowlisted keys only, nothing credential-like, TZ pinned to Asia/Kolkata."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in ENV_ALLOWLIST if k in src and not any(m in k.upper() for m in CREDENTIAL_MARKERS)}
    env['TZ'] = 'Asia/Kolkata'
    return env


def credential_like_keys(env):
    return sorted(k for k in env if any(m in k.upper() for m in CREDENTIAL_MARKERS))


def assert_frozen_files(lock_path=None, root=None):
    """Frozen Track A policy files must match freeze_lock.json digests (H2). Mismatch => refuse to run."""
    base = Path(root) if root else ROOT
    lock = json.loads(Path(lock_path or base / 'freeze_lock.json').read_text(encoding='utf8'))
    files = lock.get('files') or {}
    if set(files) != set(cap.LOCKED_FILES):
        raise RuntimeError('frozen_lock_file_set_mismatch')
    for name, want in files.items():
        got = cap.digest(json.loads((base / name).read_text(encoding='utf8')))
        if got != want:
            raise RuntimeError(f'frozen_file_hash_mismatch:{name}')
    return True


def systemd_run_command(argv, unit_name='adaptive-shadow'):
    """Hard resource limits (C-06): MemoryMax and CPUQuota from policy. Returned, never executed here."""
    return [
        'systemd-run', '--user', '--scope', '--quiet', f'--unit={unit_name}',
        '-p', f"MemoryMax={AP['runner_memory_max_mb']}M",
        '-p', f"CPUQuota={AP['runner_cpu_quota_pct']}%",
        '--setenv=TZ=Asia/Kolkata', '--',
    ] + list(argv)


def _module_imports(path):
    tree = ast.parse(Path(path).read_text(encoding='utf8'))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split('.')[0])
        elif isinstance(node, ast.Call) and getattr(node.func, 'attr', getattr(node.func, 'id', None)) in ('import_module', '__import__'):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value.split('.')[0])  # literal module name: checked like a normal import
            else:
                out.add('<dynamic_import>')
    return out


def forbidden_import_violations(entry_modules=ADAPTIVE_ENTRY_MODULES, root=None):
    """Walk local imports transitively from the adaptive modules; report any forbidden or dynamic import."""
    base = Path(root) if root else ROOT
    seen, stack, violations = set(), list(entry_modules), []
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = base / f'{mod}.py'
        if not path.exists():
            continue
        for imp in _module_imports(path):
            if imp in FORBIDDEN_MODULES or imp == '<dynamic_import>':
                violations.append((mod, imp))
            elif (base / f'{imp}.py').exists():
                stack.append(imp)
    return {'modules_scanned': sorted(seen), 'violations': sorted(violations)}
