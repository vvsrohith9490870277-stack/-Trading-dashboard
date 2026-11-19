"""Render control CLI (Tasks 7 and 10, brief render-deploy-01).

Operator tooling for the Render deployment. It never touches the live box
and never logs a secret. Task 10 ships the trial commands and the combined
budget check; Task 7 adds migrate/status/handoff/keys/token/net-check/
dry-run/footprint.

Usage: python render_ctl.py <command> [args]
  trial-init --namespace paper_render_trial_v1
  trial-compare --namespace paper_render_trial_v1 [--live paper_v2] [--day YYYY-MM-DD]
  trial-purge --namespace paper_render_trial_v1 [--state-only]
  budget-check
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_lease as rl
import render_state as rs
import render_trial as rt
import runtime_mode as rm


def _store():
    return rs.open_store_from_env()


def _trading_db():
    """The paper-trading DB (engine helper: PAPER_TEST_DATABASE only in
    render mode, else the configured Turso DB)."""
    import engine as e
    return e._db()


def _print(obj):
    print(json.dumps(obj), flush=True)


def cmd_trial_init(args):
    out = rt.init_trial(_store(), args.namespace)
    _print(out)
    return 0


def cmd_trial_compare(args):
    day = args.day
    if not day:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    out = rt.compare_trial(_trading_db(), args.namespace,
                           args.live or 'paper_v2', day)
    _print(out)
    return 0


def cmd_trial_purge(args):
    db = None if args.state_only else _trading_db()
    out = rt.purge_trial(_store(), args.namespace, db=db)
    _print(out)
    return 0


def cmd_budget_check(args):
    """Combined budget state: box limits vs trial limits, and the counters
    stored in kv for each (None when a budget has never been used)."""
    import token_store as ts
    store = _store()
    tstore = ts.TokenStore(store)
    box = tstore.get_budget(trial=False)
    trial = tstore.get_budget(trial=True)
    import engine as e
    md = e.MD
    caps = rt.trial_caps()
    out = {
        'box': {'limits': {
            'per_day': int(md.get('budget_requests_per_day', 4000)),
            'per_minute': int(md.get('budget_requests_per_minute', 40)),
            'per_second': int(md.get('budget_requests_per_second', 4)),
            'reserve_for_session_monitor': int(
                md.get('budget_reserve_for_session_monitor', 1500))},
            'state': box},
        'trial': {'limits': {
            'per_day': caps['day'], 'per_minute': caps['minute'],
            'per_second': caps['second'],
            'reserve_for_session_monitor': caps['reserve']},
            'state': trial},
    }
    _print(out)
    return 0


def cmd_status(args):
    """One-line status: mode, namespace, flag, lease, last jobs."""
    store = _store()
    ok, namespace = True, None
    try:
        import render_app as ra
        ok, namespace = ra.check_namespace()
    except Exception:
        namespace = None
    flag = None
    lease = None
    if namespace:
        try:
            flag = rl.get_active_runner(store, namespace)
            lease = rl.get_lease(store, namespace)
        except Exception:
            pass
    _print({'mode': rm.render_mode(), 'namespace': namespace,
            'namespace_ok': ok, 'active_runner': flag,
            'lease': lease})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render control CLI')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('trial-init')
    p.add_argument('--namespace', required=True)
    p.set_defaults(fn=cmd_trial_init)
    p = sub.add_parser('trial-compare')
    p.add_argument('--namespace', required=True)
    p.add_argument('--live', default='paper_v2')
    p.add_argument('--day', default=None)
    p.set_defaults(fn=cmd_trial_compare)
    p = sub.add_parser('trial-purge')
    p.add_argument('--namespace', required=True)
    p.add_argument('--state-only', action='store_true')
    p.set_defaults(fn=cmd_trial_purge)
    p = sub.add_parser('budget-check')
    p.set_defaults(fn=cmd_budget_check)
    p = sub.add_parser('status')
    p.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except rt.NamespaceRefused as ex:
        print('refused: %s' % ex, file=sys.stderr, flush=True)
        return 3
    except Exception as ex:
        print('error: %s: %s' % (type(ex).__name__, ex),
              file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
