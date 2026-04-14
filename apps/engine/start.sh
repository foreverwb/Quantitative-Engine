#!/usr/bin/env bash
# start.sh — 启动脚本：运行 Alembic 迁移后启动 uvicorn
#
# 用法:
#   ./start.sh               # 生产模式 (单进程)
#   ./start.sh --reload      # 开发模式 (hot-reload)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 激活 venv（如存在）
if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
fi

echo "==> Running Alembic migrations..."
alembic upgrade head

echo "==> Starting uvicorn..."
exec uvicorn engine.main:app \
    --host 0.0.0.0 \
    --port 8001 \
    --log-level info \
    "$@"
