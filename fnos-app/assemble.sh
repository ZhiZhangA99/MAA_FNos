#!/usr/bin/env bash
# 组装飞牛 fpk 应用包
# 前置：把 maa-image.tar.gz 解压成 app/maa-image.tar（或直接放入）
# 用法: bash assemble.sh <镜像tar路径> <输出目录>
set -euo pipefail

IMAGE_TAR="${1:?需要镜像 tar 路径}"
OUT_DIR="${2:-/tmp/maafnos-build}"
APP_SRC="${3:-/tmp/maafnos/maafnos}"

echo "=== [1/4] 复制官方骨架 ==="
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -r "$APP_SRC/." "$OUT_DIR/"

echo "=== [2/4] 放入离线镜像 ==="
cp -f "$IMAGE_TAR" "$OUT_DIR/app/maa-image.tar"
ls -la "$OUT_DIR/app/"

echo "=== [3/4] 覆盖我们的定制文件 ==="
# 由调用方在此之前把定制文件同步到 $OUT_DIR
echo "（定制文件需已同步）"

echo "=== [4/4] 打 fpk ==="
cd "$OUT_DIR"
fnpack build
ls -la *.fpk
