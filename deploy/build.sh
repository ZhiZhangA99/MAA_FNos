#!/usr/bin/env bash
# 构建 MAA-FnOS（纯套壳）镜像 —— 单仓布局，CI 与本地共用
#
# 用法：
#   MAA_BUILD_WORK=/绝对路径/构建目录 bash deploy/build.sh
#
# 可选项：
#   MAA_WEB_SOURCE   Web 源码目录；默认 $REPO_ROOT/MAA-WEB-CONTROL-master
#   MAA_TARBALL      MAA 官方 Linux 包路径；不设则按下面的顺序解析
#   MAA_IMAGE_TAG    输出镜像 tag；默认 maa-fnos:local
#
# MAA 官方包的解析顺序（用第一个命中的）：
#   1. $MAA_TARBALL                     显式指定
#   2. $MAA_BUILD_WORK/maa-linux.tar.gz 已放在构建目录里
#   3. 调 deploy/fetch-maa-core.sh 按 versions.env 下载并校验 SHA256（需要外网）
# 无论走哪条，最终都会按 versions.env 的 MAA_CORE_SHA256 校验一次。
#
# 流程：copy 源码 → docker build。工作区改动直接随源码进镜像，不做任何 patch 步骤。
set -euo pipefail

# 调用方必须传一个独立的绝对构建目录（放官方包与构建上下文）。
WORK="${MAA_BUILD_WORK:-}"
if [ -z "$WORK" ]; then
    echo "ERROR: 需要设置 MAA_BUILD_WORK（独立的绝对构建目录）。" >&2
    echo "       例：MAA_BUILD_WORK=/data/maa-work bash deploy/build.sh" >&2
    exit 1
fi
case "$WORK" in
    /*) ;;
    *) echo "ERROR: MAA_BUILD_WORK 必须是绝对路径：$WORK" >&2; exit 1 ;;
esac

# 仓库根 = 脚本所在目录的上一层，Dockerfile 与 redroid-proxy 都从这里取。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Web 源码就在**仓库内**（单仓后 MAA-WEB-CONTROL-master/ 与 deploy/ 平级）。
# 旧写法取的是仓库的上一级目录，那依赖「各子项目是同级独立目录」的旧布局：
# 仓库一 clone 到别处，$REPO_ROOT/.. 就指向仓库外，路径直接断掉。
# 需要改用仓库外的副本时才显式传 MAA_WEB_SOURCE。
WEB_SRC="${MAA_WEB_SOURCE:-$REPO_ROOT/MAA-WEB-CONTROL-master}"

# 镜像 tag 可覆盖（cmd/main 认的默认名是 maa-fnos:local）。
IMAGE_TAG="${MAA_IMAGE_TAG:-maa-fnos:local}"

# ── 解析 MAA 官方核心包 ────────────────────────────────────────────
resolve_tarball() {
    local candidate
    if [ -n "${MAA_TARBALL:-}" ]; then
        [ -f "$MAA_TARBALL" ] || { echo "ERROR: MAA_TARBALL 指向的文件不存在：$MAA_TARBALL" >&2; return 1; }
        echo "$MAA_TARBALL"
        return 0
    fi
    candidate="$WORK/maa-linux.tar.gz"
    if [ -f "$candidate" ]; then
        echo "$candidate"
        return 0
    fi
    echo "构建目录里没有 maa-linux.tar.gz，按 versions.env 下载 ..." >&2
    bash "$REPO_ROOT/deploy/fetch-maa-core.sh" --dest "$WORK" >&2 || return 1
    [ -f "$candidate" ] || { echo "ERROR: 下载后仍找不到 $candidate" >&2; return 1; }
    echo "$candidate"
}

echo "=== [1/4] 校验输入 ==="
TARBALL="$(resolve_tarball)" || exit 1
gzip -t "$TARBALL" && echo "tarball OK：$TARBALL"
# 与 CI 走同一份校验，防止「本地手放了一个不匹配版本的包」而 CI 却校验通过，
# 导致两边构建的不是同一个东西。
bash "$REPO_ROOT/deploy/fetch-maa-core.sh" --check "$TARBALL"
if [ ! -d "$WEB_SRC" ]; then
    echo "ERROR: Web 源码目录不存在：$WEB_SRC" >&2
    echo "       用 MAA_WEB_SOURCE=/path/to/MAA-WEB-CONTROL-master 指定。" >&2
    exit 1
fi
# 网关版才需要 serve_socket.py；没有它容器就不会 listen /gw/app.sock，
# 装上后应用中心一直显示异常。本地自用没有「先出个 TCP 版」的需求，缺了直接失败。
if [ ! -f "$WEB_SRC/serve_socket.py" ]; then
    echo "ERROR: $WEB_SRC 缺少 serve_socket.py —— 构建出来的镜像不会 listen 网关 socket。" >&2
    echo "       把它补进 Web 源码目录（见 docs/构建指南.md 1.2）。" >&2
    exit 1
fi
echo "Web 源码：$WEB_SRC"

echo "=== [2/4] 准备构建上下文 ==="
# 每次构建新开一个上下文目录，不动构建目录里已有的任何内容。
CTX="$(mktemp -d "$WORK/build-ctx.XXXXXX")"
# 落进上下文时统一改名：Dockerfile 的 ARG 与 COPY 都按这个名字取。
cp -f "$TARBALL" "$CTX/maa-linux.tar.gz"
# Dockerfile COPY 的是 MAA-WEB-CONTROL-master/，这里就按这个名字落进上下文，
# 不管 MAA_WEB_SOURCE 指向的目录本来叫什么。
cp -r "$WEB_SRC" "$CTX/MAA-WEB-CONTROL-master"
cp -f "$REPO_ROOT/deploy/Dockerfile" "$CTX/"
cp -r "$REPO_ROOT/redroid-proxy" "$CTX/"
ls -la "$CTX"

echo "=== [3/4] docker build ==="
cd "$CTX"
docker build -t "$IMAGE_TAG" --build-arg MAA_TARBALL=maa-linux.tar.gz .
echo "=== [4/4] BUILD OK ==="
docker images "$IMAGE_TAG" --format "{{.Repository}}:{{.Tag}} {{.Size}}"
