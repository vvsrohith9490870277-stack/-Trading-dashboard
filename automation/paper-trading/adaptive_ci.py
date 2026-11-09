"""CI proof for promotion (E6, D8, C-07 partly).

Promotion never reads a stored "CI passed" flag. It calls verify_ci_for_promotion(), which needs BOTH:
1. the public, unauthenticated GitHub check-runs for the exact commit SHA show every required check
   (ci_required_check_names) completed with conclusion 'success' (the repo is public, so no token is used or
   needed on the trading box); and
2. a local re-run of the look-ahead truncation test suite exits 0.
Any error (network, timeout, HTTP status, malformed JSON, missing check, git failure) fails closed.
There is intentionally no function that records or sets a CI pass.
"""
import json, subprocess, sys
from http import HTTPStatus
from pathlib import Path
from urllib.request import Request, urlopen

import adaptive_guard as guard
import adaptive_policy as apol

AP = apol.POLICY
ROOT = Path(__file__).resolve().parent
API = 'https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100'
LOCAL_TRUNCATION_TEST_MODULE = 'test_adaptive_truncation_ci'


def _http_get_json(url, timeout):
    # No Authorization header: public repo, unauthenticated read (no token on the box).
    req = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'adaptive-ci-check'})
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != HTTPStatus.OK:
            raise RuntimeError(f'http_{resp.status}')
        return json.loads(resp.read().decode('utf8'))


def current_commit_sha(root=None):
    out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root or ROOT, capture_output=True, text=True, timeout=AP['ci_api_timeout_seconds'])
    sha = out.stdout.strip()
    if out.returncode != 0 or len(sha) != 40:
        raise RuntimeError('git_head_unavailable')
    return sha


def public_check_runs_status(sha):
    """Required check names must each have a completed/success run on this exact SHA. Fail closed on anything else."""
    required = list(AP['ci_required_check_names'])
    if not isinstance(sha, str) or len(sha) != 40:
        return {'passed': False, 'reason': 'sha_must_be_full_40_hex', 'sha': sha}
    try:
        body = _http_get_json(API.format(repo=AP['ci_github_repo'], sha=sha), AP['ci_api_timeout_seconds'])
        runs = body['check_runs']
    except Exception as exc:  # unreachable API, timeout, HTTP error, bad JSON => not proven
        return {'passed': False, 'reason': f'ci_api_unreachable_or_invalid:{type(exc).__name__}', 'sha': sha}
    checks = {}
    for name in required:
        mine = [r for r in runs if r.get('name') == name and r.get('head_sha') == sha]
        if not mine:
            checks[name] = 'missing'
            continue
        latest = max(mine, key=lambda r: str(r.get('completed_at') or r.get('started_at') or ''))
        checks[name] = 'success' if latest.get('status') == 'completed' and latest.get('conclusion') == 'success' else \
            f"{latest.get('status')}:{latest.get('conclusion')}"
    ok = all(v == 'success' for v in checks.values())
    return {'passed': ok, 'reason': 'all_required_checks_success' if ok else 'required_check_not_success', 'sha': sha, 'checks': checks}


def run_local_truncation_suite(root=None):
    """Re-run the truncation test module locally with a scrubbed environment; exit code 0 required."""
    base = Path(root) if root else ROOT
    try:
        out = subprocess.run(
            [sys.executable, '-m', 'unittest', LOCAL_TRUNCATION_TEST_MODULE],
            cwd=base / 'tests', env={**guard.scrubbed_env(), 'PYTHONPATH': str(base)},
            capture_output=True, text=True, timeout=AP['ci_local_truncation_timeout_seconds'],
        )
    except Exception as exc:
        return {'passed': False, 'reason': f'local_truncation_error:{type(exc).__name__}'}
    return {'passed': out.returncode == 0, 'returncode': out.returncode, 'tail': out.stderr[-400:]}


def verify_ci_for_promotion(sha=None, root=None):
    try:
        sha = sha or current_commit_sha(root)
    except Exception as exc:
        return {'passed': False, 'reason': f'sha_unavailable:{exc}', 'public': None, 'local': None}
    public = public_check_runs_status(sha)
    if not public['passed']:
        return {'passed': False, 'reason': public['reason'], 'sha': sha, 'public': public, 'local': None}
    local = run_local_truncation_suite(root)
    return {'passed': bool(local['passed']), 'reason': 'ci_verified' if local['passed'] else 'local_truncation_failed',
            'sha': sha, 'public': public, 'local': local}
