#!/bin/bash
set -eu

cd "$(dirname "$0")/.."

if curl -fsS --max-time 1 http://127.0.0.1:47821/health >/dev/null 2>&1; then
  echo "共享陪伴服务已经在运行"
  exit 0
fi

mkdir -p .run
PYTHON_BIN="${WHALE_PYTHON:-python3}"

"$PYTHON_BIN" - "$PWD" "$PYTHON_BIN" <<'PYEOF'
import os
import subprocess
import sys

root, python = sys.argv[1:]
run_dir = os.path.join(root, ".run")
log = open(os.path.join(run_dir, "service.log"), "ab")
process = subprocess.Popen(
    [python, os.path.join(root, "scripts", "run-memory-server.py")],
    cwd=root,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
with open(os.path.join(run_dir, "service.pid"), "w", encoding="utf-8") as handle:
    handle.write(str(process.pid))
PYEOF

for _ in 1 2 3 4 5 6 7 8; do
  if curl -fsS --max-time 1 http://127.0.0.1:47821/health >/dev/null 2>&1; then
    echo "共享陪伴服务已常驻启动：http://127.0.0.1:47821"
    echo "回声控制台：http://127.0.0.1:47821/debug/"
    exit 0
  fi
  sleep 0.5
done

echo "共享陪伴服务启动失败，最近日志："
tail -20 .run/service.log
exit 1
