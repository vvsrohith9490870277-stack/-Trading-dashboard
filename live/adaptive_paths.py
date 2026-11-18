"""Portable path resolution for the adaptive sidecar (Task 1).

Track A root: env TRACK_A_ROOT first, then adaptive_live_config.json track_a_root.
Report dir: env ADAPTIVE_REPORT_DIR, default '/workspace/reports/adaptive' (box behaviour unchanged).
Nothing here touches the network, Turso, or any Track A file; the namespace is only read
lazily, so importing a module that uses these helpers never crashes when the root is absent.
"""
import json, os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPORT_DIR = '/workspace/reports/adaptive'
DEFAULT_TRACK_A_ROOT = '/workspace/paper-trading'


def report_dir():
    return Path(os.environ.get('ADAPTIVE_REPORT_DIR') or DEFAULT_REPORT_DIR)


def track_a_root(cfg=None):
    if os.environ.get('TRACK_A_ROOT'):
        return Path(os.environ['TRACK_A_ROOT'])
    if cfg is not None:
        return Path(cfg.get('track_a_root') or DEFAULT_TRACK_A_ROOT)
    return Path(DEFAULT_TRACK_A_ROOT)


def track_a_namespace(root=None, cfg=None):
    """Read <track_a_root>/config.json namespace, lazily. Raises a clear
    RuntimeError('track_a_root_not_found:<path>') when the root or its config is missing."""
    root = Path(root) if root is not None else track_a_root(cfg)
    cfg_path = root / 'config.json'
    if not root.is_dir() or not cfg_path.is_file():
        raise RuntimeError(f'track_a_root_not_found:{root}')
    try:
        return json.loads(cfg_path.read_text())['namespace']
    except (KeyError, ValueError, OSError) as e:
        raise RuntimeError(f'track_a_root_not_found:{root}') from e
