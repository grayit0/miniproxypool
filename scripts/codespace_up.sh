#!/usr/bin/env bash
# 在 GitHub Codespaces（或任何有 Docker 的机器）里一键拉起 manager + 3 个 node。
#
# 这条路不需要 PAT 带 `workflow` scope —— 因为它完全不依赖 GitHub Actions，
# 直接用的是 GitHub 提供的容器（Codespace）本身。
#
#   bash scripts/codespace_up.sh
#
# 环境变量（都有默认值）：
#   MANAGER_PORT=8000   容器内 manager 端口（Codespaces 会自动转发）
#
set -euo pipefail

cd "$(dirname "$0")/.."

MANAGER_PORT="${MANAGER_PORT:-8000}"

say() { printf '\n==> %s\n' "$*"; }

say "检查 docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 docker。" >&2
  echo "如果你在 Codespaces 里，请确认 .devcontainer 的 docker-in-docker feature 已启用。" >&2
  echo "如果在本机，请先装 Docker Desktop / docker engine。" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon 还没就绪，等 10 秒再试一次……" >&2
  sleep 10
fi
docker info >/dev/null 2>&1 || { echo "docker daemon 起不来，放弃。" >&2; exit 1; }

say "构建镜像（manager=Python/FastAPI，node=Go 静态二进制）"
docker compose build

say "启动 manager + node1..node3"
docker compose up -d

say "等 manager 健康"
ready=0
for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:${MANAGER_PORT}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

if [ "$ready" -ne 1 ]; then
  echo "manager 未就绪，输出最近日志：" >&2
  docker compose logs --tail=60 >&2 || true
  exit 1
fi

say "等 3 个 node 通过 manager 的心跳探测"
alive=0
for _ in $(seq 1 30); do
  alive=$(curl -fsS "http://127.0.0.1:${MANAGER_PORT}/api/stats" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("nodes", {}).get("alive", 0))' 2>/dev/null \
    || echo 0)
  if [ "${alive:-0}" -ge 1 ]; then
    break
  fi
  sleep 2
done

say "当前状态"
curl -fsS "http://127.0.0.1:${MANAGER_PORT}/api/stats" || true
echo

cat <<'EOF'

--------------------------------------------------------------------
就绪。

Codespaces 会把 8000 端口转发成一个公开地址，形如：

    https://<codespace-name>-8000.app.github.dev

在「PORTS」面板里把 8000 的 Visibility 设成 Public 就能对外用。
把那个地址当成 PROXYPOOL_MANAGER 交给客户端：

    export PROXYPOOL_MANAGER=https://<codespace-name>-8000.app.github.dev
    python client/proxypool_client.py check
    python client/proxypool_client.py list

注意（诚实说明）：
  * Codespace 里跑的 node1..node3 只能从 Codespace 内部访问
    （127.0.0.1:18801/18802/18803），它们的作用是验证链路是通的。
  * 外部客户端要真正走代理，请在你自己那台有公网入口的机器上跑一个 node，
    让它注册到上面这个 manager（EGRESS_HOST 填那台机器的公网 IP/域名）：

        docker run -d --name proxypool-node --restart unless-stopped \
          -p 18888:8888 \
          -e MANAGER_URL=https://<codespace-name>-8000.app.github.dev \
          -e NODE_HOST=$(hostname) -e NODE_NAME=mine \
          -e EGRESS_HOST=<那台机器的公网IP或域名> -e EGRESS_PORT=18888 \
          ghcr.io/grayit0/miniproxypool-node:latest

    镜像可以先用 scripts/push_ghcr.sh 从这个 Codespace 里构建并推到 GHCR。
  * Codespace 停了之后一切清零，除非你自己把它当作长期主机。
--------------------------------------------------------------------
EOF
