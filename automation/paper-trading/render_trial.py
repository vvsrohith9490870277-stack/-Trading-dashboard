"""Trial-mode rules (pure, no I/O): namespace resolution, login gate, trial caps.

Part of Task 10, but created with Task 1 because runtime_mode.resolve_namespace
delegates the rules here. The budget/token/init/compare/purge wiring that needs
render_state / token_store lands in the Task 10 commit; this module stays pure
so the resolver and its tests never touch the network or the database.
"""
from __future__ import annotations
import logging
import os
import re

log = logging.getLogger('render.trial')

TRIAL_RE = re.compile(r'^paper_render_trial_[a-z0-9_]{1,24}$')
PROTECTED = frozenset({
    'paper_v1', 'paper_v2', 'paper_demo_v1', 'paper_exit_test_v1', 'adaptive_v1',
})
# Hard trial Fyers caps (brief section 3). Env may lower, never raise.
TRIAL_DAY_CAP = 1000
TRIAL_MINUTE_CAP = 8
TRIAL_SECOND_CAP = 1


class NamespaceRefused(Exception):
    """Raised when a mode/namespace/override combination breaks section 3."""


def is_trial_namespace(name):
    return bool(name) and bool(TRIAL_RE.match(str(name)))


def resolve(mode, cfg_namespace, override, runtime_is_render):
    """Return the namespace to run under, or raise NamespaceRefused.

    mode: 'live' or 'trial'. cfg_namespace: config.json namespace.
    override: PAPER_NAMESPACE_OVERRIDE or None. runtime_is_render: whether
    PAPER_RUNTIME=render is set. When the runtime is not render, the override
    is ignored and logged and the config namespace is returned unchanged.
    Refusal happens before any DB write; callers exit with code 3.
    """
    if not runtime_is_render:
        if override:
            log.warning('PAPER_NAMESPACE_OVERRIDE ignored: PAPER_RUNTIME is not render '
                        '(override=%s, namespace=%s)', override, cfg_namespace)
        return cfg_namespace
    if mode == 'live':
        if override:
            raise NamespaceRefused(
                'live mode refuses PAPER_NAMESPACE_OVERRIDE (%s)' % override)
        if is_trial_namespace(cfg_namespace):
            raise NamespaceRefused(
                'live mode refuses trial namespace %s' % cfg_namespace)
        return cfg_namespace
    if mode == 'trial':
        if not override:
            raise NamespaceRefused(
                'trial mode requires PAPER_NAMESPACE_OVERRIDE')
        if override in PROTECTED or override == cfg_namespace:
            raise NamespaceRefused(
                'trial mode refuses protected/config namespace %s' % override)
        if not TRIAL_RE.match(override):
            raise NamespaceRefused(
                'trial mode refuses namespace %r (must match %s)'
                % (override, TRIAL_RE.pattern))
        return override
    raise NamespaceRefused('unknown RENDER_MODE %r' % (mode,))


def login_allowed(mode, env=None):
    """True only in live mode with FYERS_AUTO_LOGIN=1 and all login creds present.

    Always False in trial mode, even with credentials in the environment.
    """
    env = os.environ if env is None else env
    if mode != 'live':
        return False
    if str(env.get('FYERS_AUTO_LOGIN', '')) != '1':
        return False
    return all(env.get(k) for k in (
        'FYERS_CLIENT_ID', 'FYERS_SECRET_ID', 'FYERS_PIN', 'FYERS_TOTP_SECRET'))


def trial_caps(env=None):
    """Trial Fyers caps. RENDER_TRIAL_DAY_CAP may lower the day cap, never raise it."""
    env = os.environ if env is None else env
    try:
        day = int(env.get('RENDER_TRIAL_DAY_CAP') or TRIAL_DAY_CAP)
    except (TypeError, ValueError):
        day = TRIAL_DAY_CAP
    return {
        'day': min(TRIAL_DAY_CAP, max(0, day)),
        'minute': TRIAL_MINUTE_CAP,
        'second': TRIAL_SECOND_CAP,
        'reserve': 0,
    }
