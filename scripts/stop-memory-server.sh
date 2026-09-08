#!/bin/bash
set -eu

cd "$(dirname "$0")/.."
PID=""
if [ -f .run/service.pid ]; then
  PID="$(sed -n '1p' .run/service.pid)"
fi
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null || true
fi

# PID 文件可能来自一次启动失败；端口监听者才是常驻服务的权威身份。
LISTENER_PID="$(lsof -tiTCP:47821 -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$LISTENER_PID" ] && [ "$LISTENER_PID" != "$PID" ]; then
  kill "$LISTENER_PID" 2>/dev/null || true
fi

for _ in 1 2 3 4 5 6; do
  if ! curl -fsS --max-time 1 http://127.0.0.1:47821/health >/dev/null 2>&1; then
    echo "共享陪伴服务已停止"
    exit 0
  fi
  sleep 0.5
done

echo "共享陪伴服务仍在运行"
exit 1
