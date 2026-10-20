#!/usr/bin/env bash
# 启动 / 停止 / 状态 / 清理 MAA-Web 测试容器
# 用法: bash ctl.sh start|stop|status|logs|rm
set -euo pipefail

NAME=maa-fnos

case "${1:-status}" in
  start)
    docker run -d --name "$NAME" \
      --restart no \
      -p 18000:8000 \
      -e MAA_ADAPTER=official \
      -e MAA_CORE_DIR=/opt/maa \
      -e MAA_WEB_HOST=0.0.0.0 \
      -e MAA_WEB_PORT=8000 \
      -e TZ=Asia/Shanghai \
      -v maa-data:/app/data \
      maa-fnos:local
    echo "started"
    sleep 5
    docker ps --filter "name=$NAME" --format "{{.Names}} {{.Status}} {{.Ports}}"
    docker logs --tail 40 "$NAME" 2>&1 || true
    ;;
  stop)
    docker stop "$NAME" 2>/dev/null || true
    echo "stopped"
    ;;
  status)
    docker ps -a --filter "name=$NAME" --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;
  logs)
    docker logs --tail 80 "$NAME" 2>&1
    ;;
  rm)
    docker rm -f "$NAME" 2>/dev/null || true
    echo "removed"
    ;;
  *)
    echo "usage: bash ctl.sh start|stop|status|logs|rm"
    ;;
esac
