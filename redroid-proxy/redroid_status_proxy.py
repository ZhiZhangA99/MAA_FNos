#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAA redroid 状态查询代理（宿主机侧）
--------------------------------------
最小权限设计：只暴露「查询容器运行状态」一个能力，不提供任何启停/删除操作。

监听 172.17.0.1:18001（仅 docker bridge 网关，外网/局域网不可达）

接口：
  GET /version
  返回 JSON: {"ok":true,"version":"1.1"}
  GET /inspect?name=<容器名>
  返回 JSON: {"ok":true,"status":"running","running":true} 或 {"ok":false,"error":"..."}

仅接受白名单内的容器名（默认允许名字里含 redroid 的容器）。

前端的「检查 redroid 容器」固定用默认名 `redroid` 查询，而实际容器名往往不是它。
默认名查不到时按以下顺序兜底：
  1. MAA_REDROID_CONTAINER 显式指定（设了就优先用它）
  2. 按 MAA_REDROID_PORT 指定的宿主机端口反查（哪个容器映射了这个端口，就是它）
  3. 本机第一个名字含 redroid 的容器

/version 是给应用启动探活用的。只看「端口有没有人应答」分不清代理的新旧 ——
换了脚本但没换进程时，旧进程照样在应答，升级就静默失效了。比对版本号才能发现
"跑的还是上一版"，进而把它换掉。
"""
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# 本脚本的版本号。行为有改动就 +1，并同步 cmd/main 里的 REDROID_PROXY_VERSION，
# 否则应用升级后旧进程不会被替换（本文件就是为修这个毛病而加的版本号）。
PROXY_VERSION = "1.1"

LISTEN_HOST = "172.17.0.1"
LISTEN_PORT = 18001

# 只允许查询这些容器（按名称正则白名单），避免被拿来探测宿主机上其他容器
ALLOW_PATTERNS = [
    re.compile(r"redroid", re.I),
    re.compile(r"^maa-fnos$"),            # 本项目容器（v1.0.7 起）
    re.compile(r"^maa-web(-test)?$"),     # 历史容器名（v1.0.6 及更早），留作过渡兼容
]

# 单次 docker 调用超时
DOCKER_TIMEOUT = 5

# 自动发现时只认这一条：名字里含 redroid 的容器
DISCOVER_PATTERN = re.compile(r"redroid", re.I)

# 上游的「检查 redroid 容器」按钮不带容器名，固定用这个默认值查询。
DEFAULT_QUERY_NAME = "redroid"

# 显式指定优先：设了就优先用它。
OVERRIDE_NAME = (os.environ.get("MAA_REDROID_CONTAINER") or "").strip()

# redroid 容器对外映射的 ADB 端口，由应用启动时注入。
# 端口比容器名可靠：应用能连上设备，就说明这个端口一定是那个容器在监听。
REDROID_PORT = (os.environ.get("MAA_REDROID_PORT") or "").strip()


def name_allowed(name: str) -> bool:
    return any(p.search(name) for p in ALLOW_PATTERNS)


def inspect_container(name: str):
    """查询单个容器状态，返回 (ok, status, running, error)。"""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}|{{.State.Running}}", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", False, "docker inspect 超时"
    except FileNotFoundError:
        return False, "", False, "宿主机未找到 docker 命令"
    except Exception as exc:  # pragma: no cover - 兜底
        return False, "", False, f"inspect 异常: {exc}"

    if result.returncode != 0:
        return False, "", False, (result.stderr or "").strip() or "docker inspect 失败"

    state = (result.stdout or "").strip()
    status, _, running = state.partition("|")
    return True, status or "unknown", running.strip().lower() == "true", ""


def discover_container() -> str:
    """挑一个名字含 redroid 的容器，取 `docker ps -a` 顺序里的第一个。"""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT,
            check=False,
        )
    except Exception:  # pragma: no cover - 兜底
        return ""
    if result.returncode != 0:
        return ""
    for line in (result.stdout or "").splitlines():
        candidate = line.strip()
        if candidate and DISCOVER_PATTERN.search(candidate):
            return candidate
    return ""


def find_by_port(port: str) -> str:
    """按宿主机映射端口反查容器名。

    比「猜名字」可靠：应用连着的那条链路就是这个端口，反查到的容器必然就是它，
    宿主机上就算有几个名字含 redroid 的容器也不会认错。
    结果不再做白名单过滤 —— 端口由应用侧注入（容器内无法影响它），
    查询走 docker 自身，属可信来源。
    """
    port = (port or "").strip()
    if not port.isdigit():
        return ""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT,
            check=False,
        )
    except Exception:  # pragma: no cover - 兜底
        return ""
    if result.returncode != 0:
        return ""
    for line in (result.stdout or "").splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return ""


def resolve_fallback() -> str:
    """默认名查不到时，按 显式指定 → 端口反查 → 猜名字 的顺序找一个。"""
    if OVERRIDE_NAME and name_allowed(OVERRIDE_NAME):
        return OVERRIDE_NAME
    if REDROID_PORT:
        by_port = find_by_port(REDROID_PORT)
        if by_port:
            return by_port
    return discover_container()


class Handler(BaseHTTPRequestHandler):
    server_version = f"redroid-status-proxy/{PROXY_VERSION}"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        # 探活专用：只报版本，不碰 docker，也不受白名单影响
        if parsed.path == "/version":
            self._json(200, {"ok": True, "version": PROXY_VERSION})
            return

        if parsed.path != "/inspect":
            self._json(404, {"ok": False, "error": "not found"})
            return

        qs = parse_qs(parsed.query)
        name = (qs.get("name", [""])[0] or "").strip()
        if not name:
            self._json(400, {"ok": False, "error": "missing name"})
            return

        if not name_allowed(name):
            self._json(403, {"ok": False, "error": "container not allowed"})
            return

        looked_up = name
        ok, status, running, err = inspect_container(name)

        # 默认名查不到时做一次兜底解析
        if not ok and name == DEFAULT_QUERY_NAME:
            fallback = resolve_fallback()
            if fallback and fallback != name:
                ok2, status2, running2, err2 = inspect_container(fallback)
                if ok2:
                    ok, status, running, err = ok2, status2, running2, err2
                    looked_up = fallback

        if not ok:
            self._json(200, {"ok": False, "error": err or "docker inspect 失败"})
            return

        self._json(
            200,
            {
                "ok": True,
                "container": looked_up,
                "status": status,
                "running": running,
            },
        )

    def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷日志
        sys.stderr.write("[redroid-proxy] " + (fmt % args) + "\n")


def main() -> None:
    httpd = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    sys.stderr.write(
        f"[redroid-proxy] v{PROXY_VERSION} listening on {LISTEN_HOST}:{LISTEN_PORT}"
        f" (port-hint={REDROID_PORT or 'none'}, override={OVERRIDE_NAME or 'none'})\n"
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
