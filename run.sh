#!/bin/bash
# 啟動 podscript 本機服務
set -e

cd "$(dirname "$0")"
PORT="${PORT:-8420}"

echo "podscript → http://127.0.0.1:$PORT"
# --reload 只監看 src/，避免 audio/ 的中間產物觸發重載而中斷處理中的轉錄。
PYTHONPATH=src ./venv/bin/python -m uvicorn podscript.server:app \
  --host 127.0.0.1 --port "$PORT" \
  --reload --reload-dir src "$@"
