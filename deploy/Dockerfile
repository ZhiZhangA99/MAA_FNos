# MAA on Linux —— 纯套壳
# 组成：官方 MAA Linux 发布包（核心 + Python 绑定 + 资源） + 本项目维护的 Web 前端 fork（原样装入）
# 基础镜像：Ubuntu 24.04（glibc 2.39）—— 满足官方 maa CLI 的 glibc>=2.38 要求
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# 运行期依赖：python3.12(24.04 自带) + venv + adb + OCR/MaaCore 所需动态库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        adb ca-certificates curl tzdata \
        libgl1 libglib2.0-0 \
        libatomic1 libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# ---------- MAA 官方核心包 ----------
# 构建期通过 ARG 指定压缩包路径（由 build.sh 把下载好的包拷进 context）
ARG MAA_TARBALL=maa-linux.tar.gz
WORKDIR /opt
COPY ${MAA_TARBALL} /tmp/maa.tar.gz
RUN mkdir -p /opt/maa \
    && tar -xzf /tmp/maa.tar.gz -C /opt/maa --strip-components=1 \
    && rm -f /tmp/maa.tar.gz \
    && chmod +x /opt/maa/maa 2>/dev/null || true

# ---------- redroid 状态查询代理（容器侧脚本） ----------
# 上游的「检查 redroid 容器」会执行 `docker inspect`。
# 容器内没有 docker CLI，这里放一个最小权限代理脚本：
# 只把 `docker inspect --format '{{.State.Status}}|{{.State.Running}}' <name>`
# 转成对宿主机侧代理（172.17.0.1:18001）的查询，其余子命令一律拒绝。
COPY redroid-proxy/docker /usr/local/bin/docker
RUN chmod +x /usr/local/bin/docker

# ---------- Web 前端（本项目维护的 fork，原样装入，构建期不打本地补丁） ----------
WORKDIR /app
COPY MAA-WEB-CONTROL-master/ /app/

# 建立 venv 并安装 4 个纯 Python 依赖（run.sh 逻辑等价，这里显式化便于镜像层缓存）
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -e .

# ---------- 纯套壳的三根接线 ----------
# 1) 走真实 MaaCore（否则 DryRun 空跑）
# 2) 指向官方核心包根目录（含 lib/ resource/ Python/）
# 3) Python 绑定目录自动探测 core_dir/Python，无需显式指定
ENV MAA_ADAPTER=official \
    MAA_CORE_DIR=/opt/maa \
    MAA_WEB_HOST=0.0.0.0 \
    MAA_WEB_PORT=8000

EXPOSE 8000

# run.sh 是官方启动脚本，直接用它（保持零改动原则）
CMD ["bash", "/app/run.sh"]
