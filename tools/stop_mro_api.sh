#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${MRO_KB_PID_FILE:-$ROOT_DIR/data_runtime/mro_api_8121.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "pid file not found: $PID_FILE"
  exit 0
fi

pid="$(cat "$PID_FILE" || true)"
if [[ -z "$pid" ]]; then
  rm -f "$PID_FILE"
  echo "empty pid file removed"
  exit 0
fi

if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "mro api stopped: pid=$pid"
else
  echo "mro api was not running: pid=$pid"
fi

rm -f "$PID_FILE"
