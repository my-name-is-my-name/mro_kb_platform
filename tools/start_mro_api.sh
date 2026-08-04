#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST="${MRO_KB_HOST:-0.0.0.0}"
PORT="${MRO_KB_PORT:-8121}"
PID_FILE="${MRO_KB_PID_FILE:-$ROOT_DIR/data_runtime/mro_api_8121.pid}"
LOG_FILE="${MRO_KB_LOG_FILE:-$ROOT_DIR/data_runtime/mro_api_8121.log}"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "mro api is already running: pid=$old_pid"
    exit 0
  fi
fi

export MRO_KB_PUBLIC_BASE_URL="${MRO_KB_PUBLIC_BASE_URL:-http://10.100.112.51:8121}"
export MRO_KB_LLM_ENABLED="${MRO_KB_LLM_ENABLED:-0}"
export MRO_KB_RERANKER_URL="${MRO_KB_RERANKER_URL:-http://10.251.10.5:9101}"
export MRO_KB_RERANKER_MODEL="${MRO_KB_RERANKER_MODEL:-BAAI/bge-reranker-v2-m3}"
export MRO_KB_QDRANT_ENABLED="${MRO_KB_QDRANT_ENABLED:-1}"
export MRO_KB_QDRANT_URL="${MRO_KB_QDRANT_URL:-http://127.0.0.1:6333}"
export MRO_KB_QDRANT_COLLECTION="${MRO_KB_QDRANT_COLLECTION:-mro_kb_chunks}"
export MRO_KB_QDRANT_TARGET_TOTAL="${MRO_KB_QDRANT_TARGET_TOTAL:-35566}"
export MRO_KB_OLLAMA_URL="${MRO_KB_OLLAMA_URL:-http://127.0.0.1:11434}"
export MRO_KB_EMBEDDING_MODEL="${MRO_KB_EMBEDDING_MODEL:-bge-m3}"

nohup python3 -m apps.api.server --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" > "$PID_FILE"

echo "mro api started: pid=$pid"
echo "log: $LOG_FILE"
echo "health: http://127.0.0.1:$PORT/api/health"
