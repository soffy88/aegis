#!/usr/bin/env bash
# deploy-console.sh — 唯一被认可的 aegis-console 构建+部署方式。
#
# 存在的理由(2026-07-16 踩过大坑):从落后于 origin/main 的旧分支构建部署,
# 把 GPU/文件页/最忙容器等一大片功能盖没了、线上回退。本脚本把那次的错做成
# 机械闸门:任何一关不过就 exit 非零、绝不碰线上容器。
#
# 用法: scripts/deploy-console.sh            # 构建+部署当前 checkout
#        DRY_RUN=1 scripts/deploy-console.sh # 只构建+验证,不部署
set -euo pipefail

CONSOLE_DIR="${CONSOLE_DIR:-/data/soffy/projects/aegis-console}"
AEGIS_DIR="${AEGIS_DIR:-/data/soffy/projects/aegis}"
PLATFORM_DIR="${PLATFORM_DIR:-/data/soffy/projects/platform}"
PROXY="${DEPLOY_PROXY:-http://127.0.0.1:7890}"
API_URL="${NEXT_PUBLIC_AEGIS_API:-https://aegis.kanpan.co}"
PUBLIC_URL="${PUBLIC_URL:-https://aegis.kanpan.co}"
COMPOSE="docker compose -f ${AEGIS_DIR}/docker-compose.aegis.yml --env-file ${AEGIS_DIR}/.env.aegis"
DATE="$(date -u +%Y%m%d-%H%M%S)"
TMP_TAG="aegis-console:candidate-${DATE}"

# 构建产物里必须存在的功能路由/标记 —— 缺任何一个都说明这次构建会丢功能。
REQUIRED_ROUTES=(files memory apm slo nodes websites projects containers backups)
REQUIRED_MARKERS='busiest|gpu'

die() { echo "❌ 闸门失败: $*" >&2; exit 1; }
ok()  { echo "✅ $*"; }

cd "$CONSOLE_DIR"

# ── 闸门 1: 分支不得落后 origin/main(这就是那次翻车的根因)────────────────
echo "[1/6] 核对是否落后 origin/main ..."
git fetch origin --quiet
behind="$(git rev-list --count HEAD..origin/main)"
[ "$behind" = "0" ] || die "当前 HEAD 落后 origin/main ${behind} 个提交 —— 从旧地基构建会回退功能。请先 'git checkout -b <x> origin/main' 再叠加改动。"
ok "未落后 origin/main"

# ── 构建到临时 tag(不碰 latest)───────────────────────────────────────────
echo "[2/6] 构建候选镜像 ${TMP_TAG} ..."
DOCKER_BUILDKIT=1 docker build --network host -f Dockerfile.prod \
  --build-context platform="${PLATFORM_DIR}" \
  --build-arg NEXT_PUBLIC_AEGIS_API="${API_URL}" \
  --build-arg HTTP_PROXY="${PROXY}" --build-arg HTTPS_PROXY="${PROXY}" \
  --build-arg http_proxy="${PROXY}" --build-arg https_proxy="${PROXY}" \
  --build-arg NO_PROXY=localhost,127.0.0.1 \
  -t "${TMP_TAG}" . >/dev/null
ok "构建成功"

# ── 闸门 2: 候选镜像必须功能齐全(部署前验，不达标不部署)──────────────────
echo "[3/6] 核验候选镜像功能齐全 ..."
CHK="console-verify-${DATE}"
docker rm -f "$CHK" >/dev/null 2>&1 || true
docker run -d --name "$CHK" "${TMP_TAG}" >/dev/null
trap 'docker rm -f "$CHK" >/dev/null 2>&1 || true' EXIT
sleep 3
routes="$(docker exec "$CHK" sh -c 'find .next/server/app -maxdepth 6 -type d 2>/dev/null | grep -oE "orgs/\[org_slug\]/[a-z-]+$"' | sed -E 's#.*/##' | sort -u)"
for r in "${REQUIRED_ROUTES[@]}"; do
  echo "$routes" | grep -qx "$r" || die "候选镜像缺功能路由: /$r (可能从旧代码构建)"
done
docker exec "$CHK" sh -c "grep -rlE '${REQUIRED_MARKERS}' .next >/dev/null 2>&1" || die "候选镜像缺 GPU/最忙容器 标记"
CAND_BID="$(docker exec "$CHK" cat .next/BUILD_ID)"
docker rm -f "$CHK" >/dev/null 2>&1 || true; trap - EXIT
ok "功能齐全 (routes: $(echo "$routes" | tr '\n' ' ')| BUILD_ID=${CAND_BID})"

if [ "${DRY_RUN:-0}" = "1" ]; then echo "DRY_RUN=1，只构建+验证，不部署。候选镜像: ${TMP_TAG}"; exit 0; fi

# ── 留回滚点 + 切换 latest + 重建 ──────────────────────────────────────────
echo "[4/6] 留回滚点并切换 latest ..."
docker tag aegis-console:latest "aegis-console:rollback-${DATE}" 2>/dev/null || true
docker tag "${TMP_TAG}" aegis-console:latest
ok "回滚点: aegis-console:rollback-${DATE}"

echo "[5/6] 重建容器 ..."
$COMPOSE up -d --force-recreate aegis-console >/dev/null
sleep 6

# ── 闸门 3: 部署后核验(公网 BUILD_ID 命中 + 功能在场，否则自动回滚)────────
echo "[6/6] 部署后核验 ..."
run_bid="$(docker exec aegis-console cat .next/BUILD_ID 2>/dev/null || true)"
served="$(curl -s --noproxy '*' --max-time 15 "${PUBLIC_URL}/en/login" 2>/dev/null | grep -oE "${run_bid}" | head -1 || true)"
if [ "$run_bid" != "$CAND_BID" ] || [ "$served" != "$run_bid" ]; then
  echo "部署后核验失败 (容器BID=${run_bid} 候选=${CAND_BID} 公网命中=${served:-无}) —— 自动回滚" >&2
  docker tag "aegis-console:rollback-${DATE}" aegis-console:latest
  $COMPOSE up -d --force-recreate aegis-console >/dev/null
  die "已自动回滚到部署前状态"
fi
ok "线上 = 候选镜像 (BUILD_ID=${run_bid})，公网校验命中"
echo "🎉 部署完成。回滚: docker tag aegis-console:rollback-${DATE} aegis-console:latest && ${COMPOSE} up -d --force-recreate aegis-console"
