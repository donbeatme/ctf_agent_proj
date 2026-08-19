#!/bin/sh
# 在 Alpine VM 上运行(幂等):检测并安装 docker,构建 ctf-sandbox Debian 沙箱镜像。
# 用法:sh provision_alpine.sh [workdir]
#   workdir 默认 /root/ctf —— 沙箱镜像构建目录与执行工作目录。
set -e

WORKDIR="${1:-/root/ctf}"
IMAGE="ctf-sandbox:latest"

echo "[1/3] docker 检测/安装"
if command -v docker >/dev/null 2>&1; then
    echo "  docker 已存在: $(docker --version)"
else
    # docker 在 Alpine community 仓库;默认 repositories 里 community 被注释,先启用
    if ! grep -q '^http.*/community' /etc/apk/repositories; then
        echo "  启用 community 仓库"
        sed -i 's|^#\(.*/community\)|\1|' /etc/apk/repositories
    fi
    echo "  未安装,apk 安装 docker..."
    apk update
    apk add --no-cache docker
    rc-update add docker default
fi

echo "[2/3] 启动 docker daemon"
rc-service docker start >/dev/null 2>&1 || true
i=0
until docker info >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then
        echo "  docker daemon 30s 内未就绪,退出。请检查:rc-service docker status"
        exit 1
    fi
    sleep 1
done
echo "  docker daemon 就绪"

echo "[2.5/3] 配置 registry 镜像(直连 Docker Hub 不通时走镜像)"
MIRROR="${CTF_DOCKER_MIRROR:-https://docker.m.daocloud.io}"
if [ -n "$MIRROR" ]; then
    CFG=/etc/docker/daemon.json
    if [ -f "$CFG" ] && grep -q "$MIRROR" "$CFG"; then
        echo "  mirror 已配置:$MIRROR"
    else
        echo "  写入 mirror:$MIRROR"
        mkdir -p /etc/docker
        echo "{\"registry-mirrors\": [\"$MIRROR\"]}" > "$CFG"
        rc-service docker restart >/dev/null 2>&1
        until docker info >/dev/null 2>&1; do sleep 1; done
    fi
fi

echo "[3/3] 沙箱镜像 $IMAGE"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "  $IMAGE 已存在,跳过构建"
    exit 0
fi

DF="$WORKDIR/scripts/Dockerfile.ctf-sandbox"
SCRIPT="$WORKDIR/skills/ctf-skills/scripts/install_ctf_tools.sh"
if [ ! -f "$DF" ]; then
    echo "  缺少 $DF,请先同步项目 scripts/ 目录到本机再执行 provision"
    exit 1
fi
if [ ! -f "$SCRIPT" ]; then
    echo "  缺少 $SCRIPT(ctf-skill 官方自动检查+安装脚本),请先同步 skills/ 目录"
    exit 1
fi

echo "  docker build -t $IMAGE ...(上下文=项目根;首次构建装 70 个工具,可能耗时 10-30 分钟)"
MIRROR="${CTF_APT_MIRROR:-mirrors.aliyun.com}"
# 上下文必须是项目根:Dockerfile 里 COPY skills/ctf-skills/scripts/install_ctf_tools.sh
docker build -t "$IMAGE" --build-arg APT_MIRROR="$MIRROR" -f "$DF" "$WORKDIR"
echo "  完成:$IMAGE 已就绪"
