#!/usr/bin/env bash
# Paper-only adaptive sidecar. Usage: run_adaptive.sh preflight|eod|backfill|status [--session YYYY-MM-DD] [--no-wait]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export TZ=Asia/Kolkata
PY="${ADAPTIVE_PYTHON:-/workspace/sahil-fyers-turso/.venv/bin/python}"
mkdir -p "$ROOT/logs"
exec "$PY" "$ROOT/live/adaptive_runner.py" "$@" 2>>"$ROOT/logs/adaptive-$(date +%F).err"
