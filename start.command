#!/bin/bash
# VibeDot 一键启动 (macOS / Linux)
# 双击运行 (需先 chmod +x start.command)，或: ./start.command [项目路径]
cd "$(dirname "$0")"

# ---------- 查找 Python3 ----------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "[错误] 未找到 Python3，请先安装 (macOS: brew install python)"
  read -r -p "按回车退出..."
  exit 1
fi

# ---------- 可选: 监控的项目路径 ----------
ARGS=""
if [ -n "$1" ]; then ARGS="--project $1"; fi

# ---------- 启动服务器 + 打开控制台 ----------
"$PY" pc/vibedot_server.py $ARGS &
SERVER_PID=$!
sleep 2
open "http://127.0.0.1:8266" 2>/dev/null || xdg-open "http://127.0.0.1:8266" 2>/dev/null || echo "请手动打开: http://127.0.0.1:8266"

echo
echo "VibeDot 已启动: http://127.0.0.1:8266  (Ctrl+C 停止)"
wait $SERVER_PID
