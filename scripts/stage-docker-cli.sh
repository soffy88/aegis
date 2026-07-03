#!/usr/bin/env bash
# Stage the host's docker CLI + compose plugin into .buildbin/ so the backend
# image can COPY them in (Dockerfile.prod / Dockerfile.hotpatch). The backend
# needs `docker compose` to install multi-container apps against the mounted
# docker socket. .buildbin/ is gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="$(command -v docker || echo /usr/bin/docker)"
COMPOSE_BIN=""
for p in /usr/libexec/docker/cli-plugins/docker-compose \
         /usr/local/lib/docker/cli-plugins/docker-compose \
         /usr/lib/docker/cli-plugins/docker-compose \
         "$HOME/.docker/cli-plugins/docker-compose"; do
  [ -f "$p" ] && COMPOSE_BIN="$p" && break
done

[ -x "$DOCKER_BIN" ] || { echo "docker CLI not found on host" >&2; exit 1; }
[ -n "$COMPOSE_BIN" ] || { echo "docker compose plugin not found on host" >&2; exit 1; }

mkdir -p .buildbin
cp "$DOCKER_BIN" .buildbin/docker
cp "$COMPOSE_BIN" .buildbin/docker-compose
chmod +x .buildbin/docker .buildbin/docker-compose
echo "Staged: $DOCKER_BIN -> .buildbin/docker"
echo "Staged: $COMPOSE_BIN -> .buildbin/docker-compose"
