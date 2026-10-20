#!/usr/bin/env bash
# 取回 MAA 官方 Linux 核心包，并按 versions.env 钉死的 SHA256 校验。
#
# CI 和本地构建都调用这一个脚本 —— 这样「CI 构建的」和「本地验证过的」永远是同一个包，
# 不会因为两边各写一套 curl 而悄悄分叉。
#
# 用法：
#   bash deploy/fetch-maa-core.sh --dest /abs/dir            # 下载到 <dir>/maa-linux.tar.gz
#   bash deploy/fetch-maa-core.sh --dest /abs/dir --force    # 已存在也重新下载
#   bash deploy/fetch-maa-core.sh --print-sha256 <tarball>   # 只算摘要，用于填 versions.env
#   bash deploy/fetch-maa-core.sh --check <tarball>          # 只校验已有文件
#
# 环境变量：
#   MAA_VERSION_FILE  versions.env 路径（默认：脚本上一级目录下的 versions.env）
#   网络：本脚本需要外网（GitHub Releases 或镜像）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${MAA_VERSION_FILE:-$REPO_ROOT/versions.env}"

log() { echo "[fetch-maa-core] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

load_versions() {
    [ -f "$VERSION_FILE" ] || die "找不到版本表：$VERSION_FILE"
    # shellcheck disable=SC1090
    . "$VERSION_FILE"
    : "${MAA_CORE_VERSION:?versions.env 缺少 MAA_CORE_VERSION}"
    : "${MAA_CORE_SHA256:?versions.env 缺少 MAA_CORE_SHA256}"
    : "${MAA_CORE_URL_TEMPLATE:?versions.env 缺少 MAA_CORE_URL_TEMPLATE}"
    MAA_CORE_ASSET_TEMPLATE="${MAA_CORE_ASSET_TEMPLATE:-MAA-{version}-linux-x86_64.tar.gz}"
    MAA_TARBALL_NAME="${MAA_TARBALL_NAME:-maa-linux.tar.gz}"
}

sha256_of() {
    local raw
    if command -v sha256sum >/dev/null 2>&1; then
        raw="$(sha256sum "$1" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        raw="$(shasum -a 256 "$1" | awk '{print $1}')"
    else
        die "缺少 sha256sum / shasum，无法校验"
    fi
    # 有些环境（如 Git Bash / MSYS）会在输出前加一个 `\` 转义前导字符，
    # 只取十六进制部分，避免把摘要和一个反斜杠一起拿去比对。
    printf '%s\n' "$raw" | tr -d '\\' | tr 'A-F' 'a-f'
}

verify() {
    local file="$1" actual
    [ -f "$file" ] || die "文件不存在：$file"
    actual="$(sha256_of "$file")"
    if [ "$actual" != "$MAA_CORE_SHA256" ]; then
        log "期望 SHA256: $MAA_CORE_SHA256"
        log "实际 SHA256: $actual"
        die "$file 摘要不符（版本或下载内容不对）"
    fi
    log "SHA256 校验通过：$actual"
}

download() {
    local url="$1" out="$2"
    case "$url" in
        http://*|https://*) ;;
        file://*|/*) ;;
        *) die "不支持的 URL：$url";;
    esac
    if [[ "$url" == http* ]]; then
        command -v curl >/dev/null 2>&1 || die "缺少 curl（需要外网）"
        log "下载（需要外网）：$url"
        curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "$out.part" "$url" \
            || { rm -f "$out.part"; return 1; }
    else
        # 本地路径 / file:// —— 给离线构建和测试用，不走网络。
        log "复制本地文件：$url"
        cp "${url#file://}" "$out.part" || return 1
    fi
    mv -f "$out.part" "$out"
}

main() {
    local mode="fetch" dest="" force=0 arg=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dest) dest="${2:-}"; shift 2 ;;
            --force) force=1; shift ;;
            --check) mode="check"; arg="${2:-}"; shift 2 ;;
            --print-sha256) mode="print"; arg="${2:-}"; shift 2 ;;
            -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
            *) die "未知参数：$1" ;;
        esac
    done

    load_versions

    case "$mode" in
        print)
            [ -f "$arg" ] || die "文件不存在：$arg"
            log "把下面这行填进 $VERSION_FILE 的 MAA_CORE_SHA256："
            sha256_of "$arg"
            exit 0
            ;;
        check)
            verify "$arg"
            exit 0
            ;;
    esac

    [ -n "$dest" ] || die "缺少 --dest"
    mkdir -p "$dest"
    local target="$dest/$MAA_TARBALL_NAME"

    # 摘要还是占位符时直接停下 —— 宁可构建失败，也不要让「未校验的包」进镜像。
    case "$MAA_CORE_SHA256" in
        __FILL_ME__|"")
            die "versions.env 的 MAA_CORE_SHA256 还是占位符。先运行：
      bash deploy/fetch-maa-core.sh --print-sha256 <本地已有 tarball>
   把结果填进 $VERSION_FILE。" ;;
    esac

    if [ -f "$target" ] && [ "$force" -eq 0 ]; then
        log "已存在，校验后复用：$target"
        verify "$target"
        exit 0
    fi

    local asset url fallback
    asset="${MAA_CORE_ASSET_TEMPLATE//\{version\}/$MAA_CORE_VERSION}"
    url="${MAA_CORE_URL_TEMPLATE//\{version\}/$MAA_CORE_VERSION}"
    url="${url//\{asset\}/$asset}"
    fallback=""
    if [ -n "${MAA_CORE_URL_FALLBACK_TEMPLATE:-}" ]; then
        fallback="${MAA_CORE_URL_FALLBACK_TEMPLATE//\{version\}/$MAA_CORE_VERSION}"
        fallback="${fallback//\{asset\}/$asset}"
    fi

    if ! download "$url" "$target"; then
        if [ -n "$fallback" ]; then
            log "主 URL 失败，尝试备选：$fallback"
            download "$fallback" "$target" || die "主 URL 与备选 URL 都下载失败"
        else
            die "下载失败：$url"
        fi
    fi

    if ! verify "$target"; then
        rm -f "$target"
        die "下载内容与 versions.env 不符，已删除"
    fi
    log "就绪：$target（$MAA_CORE_VERSION）"
}

main "$@"
