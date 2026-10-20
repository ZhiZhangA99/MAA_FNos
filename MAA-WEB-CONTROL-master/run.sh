#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${MAA_WEB_VENV:-$ROOT_DIR/.venv}"
VENV_PY="$VENV_DIR/bin/python"
STAMP_FILE="$VENV_DIR/.maa-web-control-deps"

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "$PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return
  fi
  echo "Python 3.11+ is required but was not found." >&2
  exit 1
}

install_dependencies() {
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -e .
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP_FILE"
}

if [[ ! -x "$VENV_PY" ]]; then
  "$(find_python)" -m venv "$VENV_DIR"
fi

if [[ ! -f "$STAMP_FILE" || pyproject.toml -nt "$STAMP_FILE" ]]; then
  install_dependencies
fi

# 网关版：cmd/main 注入 MAA_WEB_SOCKET=/gw/app.sock 时走启动器（socket 由它交给 uvicorn）。
# 只认这个变量，不用 `-d /gw` 之类兜底 —— 目录存在与否不代表要监 socket，
# 误挂一个空目录就会把想走 TCP 的场景拽进 socket 分支。
if [[ -n "${MAA_WEB_SOCKET:-}" ]]; then
  exec "$VENV_PY" "$ROOT_DIR/serve_socket.py"
fi

exec "$VENV_PY" -m uvicorn app.main:app \
  --host "${MAA_WEB_HOST:-0.0.0.0}" \
  --port "${MAA_WEB_PORT:-8000}"
