#!/usr/bin/env bash
# 在 GitHub 的容器里（Codespaces）构建 proxypool 镜像并推送到 GHCR。
#
# 这条路只需要 PAT 带 `write:packages`（你手上那个 classic token 就有），
# 不需要 `workflow` scope，也不依赖 GitHub Actions。
#
#   export GHCR_TOKEN=<你的 PAT>
#   bash scripts/push_ghcr.sh
#
# 环境变量：
#   GHCR_TOKEN    必填，带 write:packages 的 PAT
#   GHCR_OWNER    默认 grayit0
#   GHCR_REPO     默认 miniproxypool
#   IMAGE_TAG     默认 latest，也可以给 v1.0.0
#
set -euo pipefail

cd "$(dirname "$0")/.."

GHCR_OWNER="${GHCR_OWNER:-grayit0}"
GHCR_REPO="${GHCR_REPO:-miniproxypool}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PREFIX="ghcr.io/${GHCR_OWNER}/${GHCR_REPO}"

if [ -z "${GHCR_TOKEN:-}" ]; then
  cat >&2 <<'EOF'
缺少 GHCR_TOKEN。

先建一个带 write:packages 的 classic PAT，然后：
    export GHCR_TOKEN=ghp_xxxxxxxx
    bash scripts/push_ghcr.sh
EOF
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "找不到 docker。" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon 没起来。" >&2; exit 1; }

echo "==> 登录 GHCR（用户 ${GHCR_OWNER}）"
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_OWNER" --password-stdin

echo "==> 构建 manager / node 镜像"
docker compose build

echo "==> 打标签"
docker tag proxypool-manager "${PREFIX}-manager:${IMAGE_TAG}"
docker tag proxypool-node    "${PREFIX}-node:${IMAGE_TAG}"

echo "==> 推送"
docker push "${PREFIX}-manager:${IMAGE_TAG}"
docker push "${PREFIX}-node:${IMAGE_TAG}"

cat <<EOF

==> 完成。已发布：

    ${PREFIX}-manager:${IMAGE_TAG}
    ${PREFIX}-node:${IMAGE_TAG}

首次推送的包默认是私有的。想让别人（或一台没登录的机器）能直接 pull，
去 GitHub → 你的头像 → Packages → 对应包 → Package settings 里
把可见性改成 Public，或者在目标机器上 docker login ghcr.io。

任何有 Docker 的机器上都能直接跑：

    docker compose -f docker-compose.ghcr.yml up -d

EOF
