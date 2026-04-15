#!/usr/bin/env bash
# start.sh — 启动脚本：运行 Alembic 迁移后启动 uvicorn
#
# 用法:
#   ./start.sh               # 生产模式 (单进程)
#   ./start.sh --reload      # 开发模式 (hot-reload)
#
# 日志: .run/engine.log
# 停止: Ctrl+C

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 确保日志目录存在
LOG_DIR="$SCRIPT_DIR/.run"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engine.log"

# 激活 venv（如存在）
if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
fi

# 优雅停止处理
_cleanup() {
    echo ""
    echo "==> Shutting down..."
    kill "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
    echo "==> Stopped."
    exit 0
}
trap _cleanup INT TERM

echo "==> Running Alembic migrations..."
alembic upgrade head 2>&1 | tee -a "$LOG_FILE"

echo "==> Starting uvicorn on port 18001 (logs: $LOG_FILE)..."
uvicorn engine.main:app \
    --host 0.0.0.0 \
    --port 18001 \
    --log-level info \
    "$@" 2>&1 | tee -a "$LOG_FILE" &
UVICORN_PID=$!

echo "==> Engine running (PID=$UVICORN_PID). Press Ctrl+C to stop."
wait "$UVICORN_PID"
