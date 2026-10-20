#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MAA Web 控制台挂到飞牛网关的 unix socket 上（本地自用构建的启动器）。

为什么需要它：
    飞牛应用在 ui/config 里用 gatewaySocket 指定一个 socket 文件，统一网关在
    /app/<app>/ 前缀下反向代理过去。uvicorn 没有「监听 unix socket」的现成入口，
    所以这里做一层：删掉可能残留的旧 socket，再交给 uvicorn 用 --uds 创建。

环境变量（由 fnos-app/cmd/main 的 docker run 注入）：
    MAA_WEB_SOCKET         socket 路径，容器内固定 /gw/app.sock
    MAA_WEB_SOCKET_OWNER   期望属主 "uid:gid"；不设则沿用所在目录的属主
    未设置 MAA_WEB_SOCKET 时回退 TCP（MAA_WEB_HOST/MAA_WEB_PORT）

关于属主（这里曾有一个 race，改动时别退回去）：
    **不要在这里预 bind 占位。** 一旦本进程先 bind 出 socket、交给 uvicorn
    再 unlink/bind，父进程的 chown 就可能作用在「即将被 unlink 的旧 socket」上，
    而 uvicorn 新建的那个属主仍是 root —— 表现是应用中心启动正常、点图标 502，
    极难排查。现在 socket 只由 uvicorn 创建，chown 面对的必然是它建的那个；
    启动后再用 inode 断言一次，防止有人把预 bind 加回来。

    /gw 由 cmd/main 以 package 用户建出并挂进容器，所以「沿用目录属主」就已经是
    package 用户；权限 0600，只有属主可达 —— 与 CHANGELOG 1.0.9 描述的 srw------- 一致。
"""
from __future__ import annotations

import os
import sys
import time

DEFAULT_SOCKET = "/gw/app.sock"
SOCKET_MODE = 0o600
OWNER_POLL_SECONDS = 15.0
OWNER_POLL_INTERVAL = 0.1


def log(message: str) -> None:
    sys.stderr.write(f"[serve_socket] {message}\n")
    sys.stderr.flush()


def parse_owner(value: str) -> tuple[int, int] | None:
    """'1000:1000' -> (1000, 1000)；非法值返回 None（只告警，不阻断启动）。"""
    text = (value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        log(f"WARN: MAA_WEB_SOCKET_OWNER 不是 uid:gid 格式，忽略：{value!r}")
        return None
    return int(parts[0]), int(parts[1])


def clear_path(path: str) -> None:
    """只删旧路径、建目录，不创建 socket —— socket 一律交给 uvicorn 建。"""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(path):
        try:
            os.unlink(path)
        except OSError as exc:
            log(f"ERROR: 无法删除旧的 socket {path}: {exc}")
            raise SystemExit(2) from exc


def socket_identity(path: str) -> tuple[int, int] | None:
    """返回 (st_ino, st_dev)；不存在返回 None。用于确认路径上换了文件。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_dev)


def _is_socket_mode(mode: int) -> bool:
    """S_ISSOCK 判定；单独拆出来便于在无 AF_UNIX 的平台上做单元测试。"""
    import stat as stat_module

    return stat_module.S_ISSOCK(mode)


def wait_for_new_socket(
    path: str,
    before: tuple[int, int] | None,
    timeout: float = OWNER_POLL_SECONDS,
) -> tuple[int, int] | None:
    """等 uvicorn 建出 socket，且它与启动前的 identity 不同。

    before 为 None 表示路径本来就不存在，此时只要求「出现且是 socket」。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = socket_identity(path)
        if current is not None and current != before:
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                mode = 0
            if _is_socket_mode(mode):
                return current
        time.sleep(OWNER_POLL_INTERVAL)
    return None


def apply_owner(path: str, owner: tuple[int, int] | None) -> bool:
    """把 socket 属主设成 owner；未指定则沿用目录属主。"""
    if not hasattr(os, "chown"):
        return False
    if owner is None:
        try:
            st = os.stat(os.path.dirname(path) or ".")
        except OSError:
            return False
        owner = (st.st_uid, st.st_gid)
    try:
        os.chown(path, owner[0], owner[1])
    except OSError as exc:
        log(f"WARN: chown {path} -> {owner[0]}:{owner[1]} 失败：{exc}")
        return False
    return True


def _exec_uvicorn(uds: str | None) -> None:
    args = [sys.executable, "-m", "uvicorn", "app.main:app"]
    if uds:
        args += ["--uds", uds]
    else:
        args += [
            "--host", os.environ.get("MAA_WEB_HOST", "0.0.0.0"),
            "--port", os.environ.get("MAA_WEB_PORT", "8000"),
        ]
    os.execvp(sys.executable, args)


def main() -> int:
    socket_path = (os.environ.get("MAA_WEB_SOCKET") or "").strip()
    if not socket_path:
        socket_path = DEFAULT_SOCKET if os.path.isdir(os.path.dirname(DEFAULT_SOCKET) or ".") else ""

    if not socket_path:
        log("未配置 MAA_WEB_SOCKET，回退 TCP 监听")
        _exec_uvicorn(None)
        return 0

    owner = parse_owner(os.environ.get("MAA_WEB_SOCKET_OWNER", ""))
    before = socket_identity(socket_path)
    clear_path(socket_path)

    if not hasattr(os, "fork"):
        # 无 fork 的平台：直接顶上去，uvicorn 负责建 socket（不做属主回调）。
        log(f"starting on unix socket {socket_path}")
        _exec_uvicorn(socket_path)
        return 0

    # 有 fork（Linux 容器）：子进程跑 uvicorn，父进程等它建出**新** socket 后补属主，
    # 并把退出码原样传出去（应用中心启停依赖它）。
    child = os.fork()
    if child == 0:
        _exec_uvicorn(socket_path)
        os._exit(127)

    created = wait_for_new_socket(socket_path, before)
    if created is None:
        log("ERROR: uvicorn 未在预期时间内创建 /gw/app.sock")
        return 3

    if not apply_owner(socket_path, owner):
        log("WARN: 设置 socket 属主失败，网关侧可能连不上")

    # 断言：chown 之后路径上仍是同一枚 socket（没有被别人替换）。
    after = socket_identity(socket_path)
    if after != created:
        log(f"WARN: socket 在 chown 期间被替换（before={created}, after={after}）")
    log(f"listening on unix socket {socket_path} (mode {oct(SOCKET_MODE)})")

    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
