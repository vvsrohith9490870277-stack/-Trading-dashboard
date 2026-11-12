#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# Self-heal venv + node_modules from pinned lockfiles before exec. System python3
# is required here because the venv itself may be missing (box VM fork). This
# script never deletes .venv or node_modules.
/usr/bin/env python3 "$ROOT/runtime_deps.py" --ensure
# install-check must stay reachable even when engine.py cannot be imported.
if [ "${1:-}" = install-check ]; then
  exec "${PAPER_PYTHON:-/workspace/sahil-fyers-turso/.venv/bin/python}" "$ROOT/install_check.py" "${@:2}"
fi
exec "${PAPER_PYTHON:-/workspace/sahil-fyers-turso/.venv/bin/python}" "$ROOT/engine.py" "$@"
