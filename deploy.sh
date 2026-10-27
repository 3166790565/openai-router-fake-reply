#!/usr/bin/env bash
# 智能部署：检测源码更新才重建，否则只 ensure 运行
# 用法：./deploy.sh   （在服务器项目根目录执行）
# 依赖：git, docker compose
set -euo pipefail

cd "$(dirname "$0")"

BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"

echo "==> 拉取远端信息..."
git fetch "$REMOTE" "$BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "$REMOTE/$BRANCH")

echo "    本地:  ${LOCAL:0:8}"
echo "    远端:  ${REMOTE_HEAD:0:8}"

if [ "$LOCAL" = "$REMOTE_HEAD" ]; then
  echo "==> 源码无更新，仅确保容器运行（不重建）"
  docker compose up -d --no-build
  echo "==> 完成：无变更"
  exit 0
fi

echo "==> 检测到源码更新，开始拉取并重建..."
git pull "$REMOTE" "$BRANCH" --ff-only

echo "==> 重建镜像并启动（注入 git 版本号）..."
GIT_COMMIT=$(git rev-parse HEAD)
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
export GIT_COMMIT GIT_BRANCH
docker compose up -d --build

echo "==> 清理悬空旧镜像..."
docker image prune -f 2>/dev/null || true

echo "==> 完成：已更新到 ${REMOTE_HEAD:0:8}"
docker compose ps
