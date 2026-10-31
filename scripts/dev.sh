#!/usr/bin/env bash
# 启动开发服务器
# 用法：bash scripts/dev.sh   （可用 PORT=xxxx 覆盖端口）
set -euo pipefail

VENV_DIR="${VENV_DIR:-.venv}"
PORT="${PORT:-8765}"

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
  VP="$VENV_DIR/Scripts/python.exe"
else
  VP="$VENV_DIR/bin/python"
fi

cd "$(dirname "$0")/.."
exec "$VP" -m uvicorn backend.app:app --host 127.0.0.1 --port "$PORT"
