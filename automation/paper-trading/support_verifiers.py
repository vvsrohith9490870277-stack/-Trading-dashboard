"""Support Agent Verifier Loop, Binding Safety Scripts & Reconciler (Plan v3).

Enforces:
- Two separate gates: Verifiers (V1, V2, V3) + Reconciler -> READY_FOR_HUMAN -> Human approval.
- Binding deterministic scripts outside the LLM for Verifier 2 (hard veto):
  order_route_ast, freeze_lock, locked_files_diff, gitleaks, install_window,
  ntp_sync, change_class_ast, naive_clock_ast, comment_injection_scan.
- Diff-only handoff (Proposer prose and commit messages stripped).
- Heavy work only after 15:35 IST and paused while EOD runs.
"""
import ast, hashlib, json, os, re, stat
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
import support_agent as sa

ROOT = Path(__file__).resolve().parent
LOCKED_TRADING_FILES = frozenset({
    'config.json',
    'capital_policy.json',
    'charge_policy.json',
    'charge_rates.json',
    'freeze_lock.json',
    'strategy_rules.json',
    'prefilter_policy.json',
    'session_input_policy.json',
})
PROTECTED_ENGINE_FUNCTIONS = frozenset({
    'indicator',
    '_fill',
    'fill',
    'entry_priority',
    'integer_shares',
    'assert_freeze',
    'strategy_1',
    'strategy_2',
    'strategy_3',
    'strategy_4',
    'strategy_5',
    'strategy_6',
    'strategy_7',
    # Signal, sizing, price and order-routing functions that exist today (engine.py / capital.py).
    'decide',
    'slot_rank',
    'price_paise',
    'execution_reference',
    'book_price',
    'best_level',
    'circuit_haircut',
    'trail_level',
    'band_for',
    'current_lock',
})
# Plan 5.2: engine_instrumentation_or_locking may touch logging, error taxonomy and lock paths in these files only.
ENGINE_CLASS_FILES = frozenset({'engine.py', 'audit.py', 'automation.py', 'guardian.py'})
# Plan H1: dynamic import / process execution bans in support code (AST, not grep).
BANNED_IMPORT_MODULES = frozenset({'subprocess', 'importlib', 'ctypes', 'fyers_apiv3', 'fyers_api', 'kiteconnect'})
BANNED_CALLS = frozenset({'__import__', 'eval', 'exec', 'compile'})
BANNED_ATTR_CALLS = frozenset({('os', 'system'), ('os', 'popen'), ('os', 'execv'), ('os', 'execve'), ('os', 'spawnv'), ('importlib', 'import_module')})
FORBIDDEN_ORDER_CALLS = frozenset({
    'place_order',
    'place_basket_order',
    'modify_order',
    'cancel_order',
    'exit_positions',
    'orders_post',
})
SECRET_PATTERNS = [
    re.compile(r'(?i)(fyers_access_token|turso_auth_token|api_secret|private_key|client_secret)\s*[:=]\s*["\'][A-Za-z0-9_\-\.=]{8,}["\']'),
    re.compile(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),
    re.compile(r'libsql://[^\s"\']+authToken=[^\s"\']+'),
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
]
COMMENT_INJECTION_PATTERNS = [
    re.compile(r'(?i)\bmark\s+PASS\b'),
    re.compile(r'(?i)\bignore\s+(all\s+)?(previous|prior)\s+instructions\b'),
    re.compile(r'(?i)\boverride\s+verifier\b'),
    re.compile(r'(?i)\balways\s+return\s+PASS\b'),
    re.compile(r'(?i)\bskip\s+safety\s+check\b'),
    re.compile(r'(?i)\bSYSTEM\s*:\s*you\s+are\s+verifier\b'),
]


def compute_batch_id(session_day, commit_shas):
    """Return batch id B-YYYY-MM-DD-<sha256(sorted SHAs)[:8]>."""
    day = sa.ist_session_day(session_day)
    if not commit_shas:
        raise ValueError('batch_requires_commit_shas')
    joined = '\n'.join(sorted(str(s).strip() for s in commit_shas if str(s).strip()))
    digest8 = hashlib.sha256(joined.encode('utf8')).hexdigest()[:8]
    return f'B-{day}-{digest8}'


def batch_branch_name(session_day):
    """Return canonical batch branch name support/batch-YYYY-MM-DD."""
    return f"support/batch-{sa.ist_session_day(session_day)}"


def can_run_heavy_work(now_at, eod_running=False):
    """Heavy Proposer builds, suites, and replays run only after 15:35 IST on trading days (or off-hours) and pause during EOD."""
    if eod_running and sa.SUPPORT_POLICY['heavy_work'].get('pause_during_eod', True):
        return {'allowed': False, 'reason': 'paused_during_eod'}
    t = sa.parse_ist(now_at)
    if not sa.is_trading_day(t.date()):
        return {'allowed': True, 'reason': 'non_trading_day_off_hours'}
    start_after_str = sa.SUPPORT_POLICY['heavy_work']['start_after']
    sh, sm = [int(x) for x in start_after_str.split(':')]
    start_after = dt_time(sh, sm)
    reg_start_h, reg_start_m = [int(x) for x in sa.CFG['regular_session'][0].split(':')]
    reg_start = dt_time(reg_start_h, reg_start_m)
    wall = t.time().replace(tzinfo=None)
    if reg_start <= wall < start_after:
        return {'allowed': False, 'reason': f'market_hours_before_{start_after_str}'}
    return {'allowed': True, 'reason': 'after_heavy_work_cutoff'}


def sanitize_proposer_handoff(raw_bundle):
    """Strip Proposer narrative, commit messages, and other verdicts before verifier handoff."""
    allowed_keys = {'plan_version', 'finding', 'diff', 'changed_files', 'change_class', 'change_id', 'artifacts'}
    sanitized = {k: v for k, v in raw_bundle.items() if k in allowed_keys}
    # Strip any git format-patch / commit header lines before the first 'diff --git' or '--- '
    diff_text = str(sanitized.get('diff') or '')
    lines = diff_text.splitlines()
    clean_lines = []
    in_diff = False
    for line in lines:
        if line.startswith('diff --git ') or line.startswith('--- ') or line.startswith('+++ ') or line.startswith('@@ '):
            in_diff = True
        if in_diff:
            clean_lines.append(line)
    sanitized['diff'] = '\n'.join(clean_lines) if clean_lines else diff_text
    return sanitized


def check_order_route_ast(py_sources_map):
    """H1 AST check: ensure no broker order placement calls or order routes exist."""
    violations = []
    for filename, source in py_sources_map.items():
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            violations.append({'file': filename, 'reason': f'syntax_error:{exc.msg}'})
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
                for mod in mods:
                    if mod.split('.')[0] in BANNED_IMPORT_MODULES:
                        violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'banned_import': mod})
            if isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    if func_name in BANNED_CALLS:
                        violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'banned_call': func_name})
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                    if isinstance(node.func.value, ast.Name) and (node.func.value.id, func_name) in BANNED_ATTR_CALLS:
                        violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'banned_call': f'{node.func.value.id}.{func_name}'})
                if func_name in FORBIDDEN_ORDER_CALLS:
                    violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'call': func_name})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.lower()
                forbidden_Order_paths = ('api-t1.fyers.in' + '/api/v3/orders', 'fyers.in' + '/api/v2/orders')
                if any(p in val for p in forbidden_Order_paths):
                    violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'url': node.value})
    return {'passed': len(violations) == 0, 'violations': violations}


def check_naive_clock_ast(py_sources_map):
    """v3 Time AST check: ban naive clock calls and SQLite current-date functions."""
    violations = []
    for filename, source in py_sources_map.items():
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            violations.append({'file': filename, 'reason': f'syntax_error:{exc.msg}'})
            continue
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstring_nodes.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == 'utcnow':
                        violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'reason': 'banned_utcnow'})
                    elif node.func.attr == 'now':
                        # datetime.now() / datetime.datetime.now() with no tz is naive
                        owner = node.func.value
                        is_dt = (isinstance(owner, ast.Name) and owner.id in ('datetime', 'dt')) or (isinstance(owner, ast.Attribute) and owner.attr == 'datetime')
                        if is_dt:
                            tz_kw = [kw for kw in node.keywords if kw.arg == 'tz']
                            tz_arg = node.args[0] if node.args else (tz_kw[0].value if tz_kw else None)
                            if tz_arg is None or (isinstance(tz_arg, ast.Constant) and tz_arg.value is None):
                                violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'reason': 'naive_datetime_now'})
                    elif node.func.attr == 'today':
                        owner = node.func.value
                        if (isinstance(owner, ast.Name) and owner.id in ('date', 'datetime', 'dt')) or (isinstance(owner, ast.Attribute) and owner.attr in ('date', 'datetime')):
                            violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'reason': 'naive_today'})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_nodes:
                if re.search(r"(?i)\bdate\s*\(\s*'now'\s*\)", node.value) or re.search(r'(?i)\bdatetime\s*\(\s*\'now\'\s*\)', node.value):
                    violations.append({'file': filename, 'line': getattr(node, 'lineno', 0), 'reason': 'sqlite_date_now_banned'})
    return {'passed': len(violations) == 0, 'violations': violations}


def check_freeze_lock(root=None):
    """H3 check: verify freeze_lock.json and support_policy_lock.json match their policy files on disk."""
    base = Path(root) if root is not None else ROOT
    try:
        freeze_lock = json.loads((base / 'freeze_lock.json').read_text(encoding='utf8'))
        for fname, expected_digest in freeze_lock.get('files', {}).items():
            actual = sa.policy_digest(json.loads((base / fname).read_text(encoding='utf8')))
            if actual != expected_digest:
                return {'passed': False, 'reason': f'freeze_lock_mismatch:{fname}'}
        sup_policy = json.loads((base / 'support_policy.json').read_text(encoding='utf8'))
        sup_lock = json.loads((base / 'support_policy_lock.json').read_text(encoding='utf8'))
        sa.assert_support_policy_lock(sup_policy, sup_lock)
        return {'passed': True}
    except Exception as exc:
        return {'passed': False, 'reason': str(exc)}


def check_locked_files_diff(changed_files):
    """H3 check: ensure no locked policy or rule files are in the diff."""
    basenames = {Path(f).name for f in (changed_files or [])}
    touched = sorted(basenames & LOCKED_TRADING_FILES)
    return {'passed': len(touched) == 0, 'touched_locked_files': touched}


def check_gitleaks(diff_text, extra_texts=None):
    """H4 check: scan diff and output texts for credentials or tokens."""
    matches = []
    corpus = [str(diff_text or '')] + [str(x) for x in (extra_texts or [])]
    for text in corpus:
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                matches.append(m.group(0)[:12] + '***REDACTED***')
    return {'passed': len(matches) == 0, 'leaks_found': len(matches), 'samples': matches}


def check_comment_injection(diff_text):
    """Prompt-injection defense: scan diff for 'mark PASS' or instruction override phrases."""
    hits = []
    for line in str(diff_text or '').splitlines():
        if line.startswith('---') or line.startswith('+++'):
            continue
        for pat in COMMENT_INJECTION_PATTERNS:
            m = pat.search(line)
            if m:
                hits.append({'matched': m.group(0), 'line': line.strip()[:120]})
    return {'passed': len(hits) == 0, 'injections': hits}


def check_install_window(planned_install_at, cfg=None):
    """H6 check: install only 15:45-08:30 IST before a trading day; never on holidays/weekends or around the 11 Nov test."""
    c = sa.CFG if cfg is None else cfg
    inst_cfg = sa.SUPPORT_POLICY['install']
    t = sa.parse_ist(planned_install_at)
    d = t.date()
    wall = t.time().replace(tzinfo=None)
    ws_h, ws_m = [int(x) for x in inst_cfg['window_start'].split(':')]
    we_h, we_m = [int(x) for x in inst_cfg['window_end'].split(':')]
    win_start = dt_time(ws_h, ws_m)
    win_end = dt_time(we_h, we_m)

    protected = set(inst_cfg.get('protected_test_dates', []))
    if d.isoformat() in protected or (d + timedelta(days=1)).isoformat() in protected:
        return {'passed': False, 'reason': 'protected_nov11_test_window'}

    if wall >= win_start:
        # Evening install slot: must be before a trading day (or Friday/holiday-eve evening slot per Plan v3 step 6)
        # Never on a weekend or NSE holiday itself
        if not sa.is_trading_day(d, cfg=c):
            return {'passed': False, 'reason': 'evening_install_on_non_trading_day'}
        return {'passed': True, 'slot': 'evening', 'date': d.isoformat()}
    if wall <= win_end:
        # Morning install slot: today itself must be a trading day
        if not sa.is_trading_day(d, cfg=c):
            return {'passed': False, 'reason': 'morning_install_not_on_trading_day'}
        return {'passed': True, 'slot': 'morning', 'date': d.isoformat()}
    return {'passed': False, 'reason': 'outside_1545_0830_ist_window'}


def check_ntp_sync(clock_drift_seconds=0.0):
    """Verify clock drift is within support_policy threshold."""
    max_drift = float(sa.SUPPORT_POLICY['thresholds']['box_clock_drift_max_seconds'])
    ok = abs(float(clock_drift_seconds)) <= max_drift
    return {'passed': ok, 'clock_drift_seconds': float(clock_drift_seconds), 'max_allowed_seconds': max_drift}


def _function_ast_signatures(py_source):
    tree = ast.parse(py_source)
    sigs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs[node.name] = ast.dump(node, annotate_fields=True, include_attributes=False)
    return sigs


def check_change_class_ast(change_class, changed_files, before_sources=None, after_sources=None, track_a_decision_diff=0):
    """Verify change_class constraints:
    - support_only: only touches support* files, tests/*support*, or reports/*
    - engine_instrumentation_or_locking: requires zero Track A diff and AST proof that no signal, sizing, or order function changed.
    """
    valid_classes = set(sa.SUPPORT_POLICY['verifier_rules']['change_classes'])
    if change_class not in valid_classes:
        return {'passed': False, 'reason': f'invalid_change_class:{change_class}'}

    files = [Path(f).name for f in (changed_files or [])]
    if change_class == 'support_only':
        non_support = [
            f for f in files
            if not (f.startswith('support_') or f.startswith('test_support') or f.endswith('.md'))
        ]
        if non_support:
            return {'passed': False, 'reason': 'support_only_touched_non_support_files', 'files': non_support}
        return {'passed': True, 'change_class': change_class}

    # engine_instrumentation_or_locking
    outside = [f for f in files if not (f in ENGINE_CLASS_FILES or f.startswith('support_') or f.startswith('test_') or f.endswith('.md'))]
    if outside:
        return {'passed': False, 'reason': 'engine_class_touched_files_outside_allowlist', 'files': outside}
    if int(track_a_decision_diff) != 0:
        return {'passed': False, 'reason': 'non_zero_track_a_decision_diff', 'track_a_decision_diff': track_a_decision_diff}
    before_sources = before_sources or {}
    after_sources = after_sources or {}
    provided = {Path(k).name for k in set(before_sources) & set(after_sources)}
    unproven = sorted(f for f in files if f in ENGINE_CLASS_FILES and f not in provided)
    if unproven:
        # No before/after source means no AST proof; fail closed instead of passing vacuously.
        return {'passed': False, 'reason': 'engine_class_ast_proof_missing_sources', 'files': unproven}
    modified_protected = []
    for fname in set(before_sources.keys()) | set(after_sources.keys()):
        b_sigs = _function_ast_signatures(before_sources.get(fname, ''))
        a_sigs = _function_ast_signatures(after_sources.get(fname, ''))
        for fn in PROTECTED_ENGINE_FUNCTIONS:
            if b_sigs.get(fn) != a_sigs.get(fn):
                modified_protected.append(f'{fname}:{fn}')
    if modified_protected:
        return {'passed': False, 'reason': 'protected_signal_sizing_or_order_ast_changed', 'functions': sorted(modified_protected)}
    return {'passed': True, 'change_class': change_class}


def run_binding_safety_suite(bundle, planned_install_at, py_sources_map=None, before_sources=None, after_sources=None, track_a_decision_diff=0, clock_drift_seconds=0.0, root=None, out_file=None):
    """Run all 9 binding deterministic checks outside any LLM and optionally write a read-only verdict file."""
    clean = sanitize_proposer_handoff(bundle)
    diff_text = clean.get('diff', '')
    changed_files = clean.get('changed_files', [])
    change_class = clean.get('change_class', 'support_only')
    sources = py_sources_map or {}

    checks = {
        'order_route_ast': check_order_route_ast(sources),
        'freeze_lock': check_freeze_lock(root=root),
        'locked_files_diff': check_locked_files_diff(changed_files),
        'gitleaks': check_gitleaks(diff_text),
        'install_window': check_install_window(planned_install_at),
        'ntp_sync': check_ntp_sync(clock_drift_seconds),
        'change_class_ast': check_change_class_ast(change_class, changed_files, before_sources=before_sources, after_sources=after_sources, track_a_decision_diff=track_a_decision_diff),
        'naive_clock_ast': check_naive_clock_ast(sources),
        'comment_injection_scan': check_comment_injection(diff_text),
    }
    all_passed = all(c['passed'] for c in checks.values())
    failed_checks = sorted(k for k, v in checks.items() if not v['passed'])
    verdict = {
        'binding_suite': 'verifier_2_safety_scripts',
        'overall': 'PASS' if all_passed else 'FAIL',
        'hard_veto': not all_passed,
        'failed_checks': failed_checks,
        'checks': checks,
        'recorded_at': sa.ist_now().isoformat(),
    }
    if out_file is not None:
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        p.write_text(json.dumps(verdict, indent=2, sort_keys=True), encoding='utf8')
        os.chmod(p, stat.S_IREAD)
        verdict['verdict_file'] = str(p)
    return verdict


def run_verifier_1_correctness(handoff_bundle, proposer_agent_id='proposer_1', verifier_agent_id='verifier_1'):
    """Verifier 1 (Correctness): checks root cause fit, reproducing test, and forbidden inputs."""
    if proposer_agent_id == verifier_agent_id:
        raise PermissionError('self_review_forbidden')
    if any(k in handoff_bundle for k in ('other_verdicts', 'commit_message', 'proposer_narrative')):
        raise PermissionError('verifier_context_contamination')
    finding = handoff_bundle.get('finding')
    diff = handoff_bundle.get('diff')
    artifacts = handoff_bundle.get('artifacts')
    if not finding or not diff or artifacts is None:
        return {'role': 'verifier_1_correctness', 'verdict': 'NOT_VERIFIABLE', 'items': [{'tag': 'MUST', 'id': 'inputs_present', 'status': 'NOT_VERIFIABLE'}]}
    has_repro_test = bool(artifacts.get('reproducing_test_passed', False))
    fits_finding = bool(artifacts.get('root_cause_verified', True))
    items = [
        {'tag': 'MUST', 'id': 'reproducing_test', 'status': 'PASS' if has_repro_test else 'FAIL'},
        {'tag': 'MUST', 'id': 'root_cause_fit', 'status': 'PASS' if fits_finding else 'FAIL'},
        {'tag': 'SHOULD', 'id': 'minimal_scope', 'status': 'PASS' if not artifacts.get('over_engineered', False) else 'WARN'},
    ]
    overall = 'PASS' if all(i['status'] == 'PASS' for i in items if i['tag'] == 'MUST') else 'FAIL'
    return {'role': 'verifier_1_correctness', 'verdict': overall, 'items': items}


def run_verifier_2_safety(handoff_bundle, binding_script_verdict, proposer_model_family='family_a', verifier_model_family='family_b', proposer_agent_id='proposer_1', verifier_agent_id='verifier_2'):
    """Verifier 2 (Safety / Hard Veto): script-first, requires distinct model family from Proposer."""
    if proposer_agent_id == verifier_agent_id:
        raise PermissionError('self_review_forbidden')
    if any(k in handoff_bundle for k in ('other_verdicts', 'commit_message', 'proposer_narrative')):
        raise PermissionError('verifier_context_contamination')
    if not binding_script_verdict:
        return {'role': 'verifier_2_safety', 'verdict': 'NOT_VERIFIABLE', 'hard_veto': True, 'items': []}
    family_diverse = (proposer_model_family != verifier_model_family)
    scripts_ok = (binding_script_verdict.get('overall') == 'PASS') and not binding_script_verdict.get('hard_veto', False)
    overall = 'PASS' if (scripts_ok and family_diverse) else 'FAIL'
    return {
        'role': 'verifier_2_safety',
        'verdict': overall,
        'hard_veto': overall != 'PASS',
        'model_family_diverse': family_diverse,
        'failed_scripts': binding_script_verdict.get('failed_checks', []),
        'items': [
            {'tag': 'MUST', 'id': 'binding_safety_scripts', 'status': 'PASS' if scripts_ok else 'FAIL'},
            {'tag': 'MUST', 'id': 'model_family_diversity', 'status': 'PASS' if family_diverse else 'FAIL'},
        ],
    }


def run_verifier_3_regression(handoff_bundle, proposer_agent_id='proposer_1', verifier_agent_id='verifier_3'):
    """Verifier 3 (Regression / Replay): full suites, virtual-clock 9 Nov replay, zero Track A diff, zero duplicate findings."""
    if proposer_agent_id == verifier_agent_id:
        raise PermissionError('self_review_forbidden')
    if any(k in handoff_bundle for k in ('other_verdicts', 'commit_message', 'proposer_narrative')):
        raise PermissionError('verifier_context_contamination')
    artifacts = handoff_bundle.get('artifacts')
    if not artifacts or 'unit_suite_passed' not in artifacts or 'nov9_replay_passed' not in artifacts:
        return {'role': 'verifier_3_regression', 'verdict': 'NOT_VERIFIABLE', 'items': []}
    suite_ok = bool(artifacts.get('unit_suite_passed'))
    replay_ok = bool(artifacts.get('nov9_replay_passed'))
    zero_track_a = int(artifacts.get('track_a_decision_diff', 0)) == 0
    no_dups = int(artifacts.get('duplicate_findings_count', 0)) == 0
    batch_ok = str(artifacts.get('batch_branch', '')).startswith('support/batch-')
    items = [
        {'tag': 'MUST', 'id': 'unit_suite', 'status': 'PASS' if suite_ok else 'FAIL'},
        {'tag': 'MUST', 'id': 'nov9_virtual_clock_replay', 'status': 'PASS' if replay_ok else 'FAIL'},
        {'tag': 'MUST', 'id': 'zero_track_a_diff', 'status': 'PASS' if zero_track_a else 'FAIL'},
        {'tag': 'MUST', 'id': 'no_duplicate_findings', 'status': 'PASS' if no_dups else 'FAIL'},
        {'tag': 'SHOULD', 'id': 'batch_branch_verified', 'status': 'PASS' if batch_ok else 'WARN'},
    ]
    overall = 'PASS' if all(i['status'] == 'PASS' for i in items if i['tag'] == 'MUST') else 'FAIL'
    return {'role': 'verifier_3_regression', 'verdict': overall, 'items': items}


def load_binding_verdict(binding_verdict):
    """Read the binding script verdict directly (dict or the read-only verdict file) with its sha256."""
    if binding_verdict is None:
        return None, None
    if isinstance(binding_verdict, (str, Path)):
        raw = Path(binding_verdict).read_bytes()
        return json.loads(raw.decode('utf8')), hashlib.sha256(raw).hexdigest()
    raw = json.dumps(binding_verdict, sort_keys=True, default=str).encode('utf8')
    return binding_verdict, hashlib.sha256(raw).hexdigest()


def reconcile_verdicts(v1, v2, v3, revision_round=0, binding_verdict=None):
    """Reconciler / Judge: merges V1, V2, V3 after all three are final.

    Rules:
    - V2 FAIL or hard_veto is an unconditional veto.
    - Any NOT_VERIFIABLE blocks PASS.
    - MUST items require unanimity (all PASS).
    - SHOULD items pass by 2 of 3, with dissent recorded.
    - Max 1 revision from scratch; a failure on revision_round >= 1 means REJECT.
    - Best possible outcome is READY_FOR_HUMAN (never installs).
    """
    verdicts = [v1, v2, v3]
    max_revs = int(sa.SUPPORT_POLICY['verifier_rules']['max_revisions'])

    # Plan 5.1(3): the Reconciler reads the binding script verdict itself; no model verdict can overrule it.
    script, script_sha = load_binding_verdict(binding_verdict)
    if script is None or script.get('overall') != 'PASS' or script.get('hard_veto', True) or script.get('failed_checks'):
        outcome = 'REJECT' if revision_round >= max_revs else 'REVISE'
        return {
            'outcome': outcome,
            'vetoed_by_verifier_2': True,
            'reason': 'binding_script_verdict_missing' if script is None else 'binding_script_veto',
            'binding_verdict_sha256': script_sha,
            'revision_round': revision_round,
            'dissent': [],
            'verdicts': verdicts,
        }

    if v2.get('hard_veto') or v2.get('verdict') != 'PASS':
        outcome = 'REJECT' if revision_round >= max_revs else 'REVISE'
        return {
            'outcome': outcome,
            'vetoed_by_verifier_2': True,
            'revision_round': revision_round,
            'dissent': [],
            'verdicts': verdicts,
        }

    if any(v.get('verdict') == 'NOT_VERIFIABLE' for v in verdicts):
        outcome = 'REJECT' if revision_round >= max_revs else 'REVISE'
        return {
            'outcome': outcome,
            'vetoed_by_verifier_2': False,
            'reason': 'not_verifiable',
            'revision_round': revision_round,
            'dissent': [],
            'verdicts': verdicts,
        }

    must_failures = [
        f"{v.get('role', 'verifier')}:verdict:{v.get('verdict')}"
        for v in verdicts if v.get('verdict') not in ('PASS', 'PASS_WITH_WARNINGS')
    ]
    should_Statuses = []
    dissent = []
    for v in verdicts:
        role = v.get('role', 'verifier')
        for item in v.get('items', []):
            if item.get('tag') == 'MUST' and item.get('status') != 'PASS':
                must_failures.append(f"{role}:{item.get('id')}")
            elif item.get('tag') == 'SHOULD':
                should_Statuses.append((role, item.get('id'), item.get('status')))
                if item.get('status') != 'PASS':
                    dissent.append(f"{role}:{item.get('id')}:{item.get('status')}")

    # Check 2-of-3 rule across verifiers
    pass_count = sum(1 for v in verdicts if v.get('verdict') in ('PASS', 'PASS_WITH_WARNINGS'))
    if must_failures or pass_count < int(sa.SUPPORT_POLICY['verifier_rules']['should_min_pass']):
        outcome = 'REJECT' if revision_round >= max_revs else 'REVISE'
        return {
            'outcome': outcome,
            'vetoed_by_verifier_2': False,
            'must_failures': must_failures,
            'revision_round': revision_round,
            'dissent': dissent,
            'verdicts': verdicts,
        }

    return {
        'outcome': 'READY_FOR_HUMAN',
        'vetoed_by_verifier_2': False,
        'binding_verdict_sha256': script_sha,
        'must_failures': [],
        'revision_round': revision_round,
        'dissent': dissent,
        'verdicts': verdicts,
    }
