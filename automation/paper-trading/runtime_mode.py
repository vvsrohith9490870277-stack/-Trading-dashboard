"""Runtime switch for the Render deployment (Task 1).

Box behaviour is the default: with PAPER_RUNTIME unset, every helper here
returns the box value, so existing code paths are unchanged. In render mode
(PAPER_RUNTIME=render) local runtime state (logs, spool, locks, out files)
lives under state_dir() instead of the code tree, credentials come from the
environment instead of the legacy .env file, and the namespace is resolved
through render_trial.resolve (never mixed live/trial).
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

log = logging.getLogger('render.runtime_mode')

# Default env-file location on the box. Guarded default: only used when
# PAPER_ENV_FILE is unset; render mode prefers process.env instead.
DEFAULT_ENV_FILE = Path('/workspace/sahil-fyers-turso/.env')  # box default

# Env names the render path reads (names only; values are never logged).
FYERS_ENV_NAMES = (
    'FYERS_APP_ID', 'FYERS_CLIENT_ID', 'FYERS_SECRET_ID', 'FYERS_PIN',
    'FYERS_TOTP_SECRET', 'FYERS_REDIRECT_URI', 'FYERS_ACCESS_TOKEN',
    'FYERS_REFRESH_TOKEN',
)


def is_render():
    return os.environ.get('PAPER_RUNTIME') == 'render'


def render_mode():
    """'live' unless RENDER_MODE=trial (only meaningful when render)."""
    return os.environ.get('RENDER_MODE', 'live')


def log_to_stdout():
    return is_render()


def state_dir(root):
    """Where runtime state (logs/out/spool/locks) lives.

    root: the code tree (engine.ROOT). Box: the code tree itself.
    Render: PAPER_STATE_DIR, else /tmp/paper-state (ephemeral, per brief).
    """
    root = Path(root)
    override = os.environ.get('PAPER_STATE_DIR')
    if override:
        return Path(override)
    if is_render():
        return Path('/tmp/paper-state')
    return root


def state_path(root, *parts):
    return state_dir(root).joinpath(*parts)


def env_file_path():
    """Env-file location: PAPER_ENV_FILE, else the box default."""
    return Path(os.environ.get('PAPER_ENV_FILE') or str(DEFAULT_ENV_FILE))


def env_file_exists():
    try:
        return env_file_path().exists()
    except Exception:
        return False


def env_creds():
    """Fyers/Turso credential values present in process.env (names only logged)."""
    return {k: v for k, v in ((k, os.environ.get(k)) for k in FYERS_ENV_NAMES)
            if v is not None}


def resolve_namespace(cfg, env=None):
    """The single namespace resolver (Task 1.7).

    With PAPER_RUNTIME unset it returns CFG['namespace'] unchanged, and an
    override is ignored and logged. In render mode the rules live in
    render_trial.resolve; refusal raises render_trial.NamespaceRefused
    before any DB write (callers exit with code 3).
    """
    env = os.environ if env is None else env
    cfg_ns = cfg['namespace']
    if not is_render():
        override = env.get('PAPER_NAMESPACE_OVERRIDE')
        if override:
            log.warning('PAPER_NAMESPACE_OVERRIDE ignored: PAPER_RUNTIME is not '
                        'render (override=%s, namespace=%s)', override, cfg_ns)
        return cfg_ns
    import render_trial as rt
    return rt.resolve(render_mode(), cfg_ns, env.get('PAPER_NAMESPACE_OVERRIDE'), True)
