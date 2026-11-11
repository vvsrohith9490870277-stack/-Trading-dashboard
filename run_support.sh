#!/usr/bin/env bash
# Read-only Support Agent findings pass. Usage: run_support.sh --session YYYY-MM-DD [--now ISO]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export TZ=Asia/Kolkata
PY="${ADAPTIVE_PYTHON:-/workspace/sahil-fyers-turso/.venv/bin/python}"
mkdir -p "$ROOT/logs"
exec "$PY" "$ROOT/live/support_runner.py" "$@" 2>>"$ROOT/logs/support-$(date +%F).err"
