#!/usr/bin/env bash
# Read-only Support Agent findings pass. Usage: run_support.sh --session YYYY-MM-DD [--now ISO]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export TZ=Asia/Kolkata
# Resolve @libsql/client from the Track A install (read-only). Do not write there.
export NODE_PATH="${TRACK_A_ROOT:-/workspace/paper-trading}/node_modules${NODE_PATH:+:$NODE_PATH}"
PY="${ADAPTIVE_PYTHON:-/workspace/sahil-fyers-turso/.venv/bin/python}"
mkdir -p "$ROOT/logs"
exec "$PY" "$ROOT/live/support_runner.py" "$@" 2>>"$ROOT/logs/support-$(date +%F).err"
