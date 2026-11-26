#!/usr/bin/env bash
# Build the two RoutePilot service images from ONE Dockerfile on 76
# (Harbor-insecure docker) and push them.
# Usage: scripts/docker/docker-build.sh [tag] [ssh-target]
#
# Both tags point at the same image bytes; only the container argument differs:
#   routepilot-core:<tag>    `python -m router core`
#   routepilot-console:<tag> `python -m router peripheral`
set -euo pipefail

TAG="${1:-}"
TARGET="${2:-root@192.168.95.76}"
REGISTRY="192.168.95.133:30002"
CORE_REPO="$REGISTRY/sandlake-deploy/routepilot-core"
CONSOLE_REPO="$REGISTRY/sandlake-deploy/routepilot-console"
STAGING="/opt/routepilot-docker-context"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
DOCKERIGNORE="$(cd "$(dirname "$0")" && pwd)/.dockerignore"
DOCKERFILE="$(cd "$(dirname "$0")" && pwd)/Dockerfile"

if [ -z "$TAG" ]; then
  TAG="amd64-$(git -C "$SRC" rev-parse --short HEAD)"
fi

echo "==> staging clean context to $TARGET:$STAGING (tag: $TAG)"
rsync -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'node_modules/' \
  --exclude 'docs/' --exclude 'tests/' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.workbuddy/' --exclude '.superpowers/' --exclude '.kimi-code/' \
  --exclude '.worktrees/' --exclude 'assets/' --exclude '.DS_Store' \
  "$SRC/" "$TARGET:$STAGING/"
# .dockerignore must sit at the context root.
scp -q "$DOCKERIGNORE" "$TARGET:$STAGING/.dockerignore"
# The Dockerfile must also sit at the context root for `docker build .`.
scp -q "$DOCKERFILE" "$TARGET:$STAGING/Dockerfile"

echo "==> docker build one image, tag both services ($CORE_REPO:$TAG, $CONSOLE_REPO:$TAG)"
if ! ssh "$TARGET" "cd '$STAGING' && docker build -t '$CORE_REPO:$TAG' -t '$CONSOLE_REPO:$TAG' . \
      && docker push '$CORE_REPO:$TAG' \
      && docker push '$CONSOLE_REPO:$TAG'"; then
  echo "ERROR: build/push failed. If push was rejected with unauthorized, run:" >&2
  echo "  ssh $TARGET 'docker login $REGISTRY'" >&2
  exit 1
fi

echo "==> image self-test (kernel core on 18400, console peripheral on 18401, then removed)"
# I3: throwaway, container-local admin credentials for the console self-test.
# The image binds ROUTER_HOST=0.0.0.0, so create_app() refuses to start without
# an explicit non-default admin password of at least 8 characters; the container
# is removed right after the health probe and these credentials are never used
# by a real deployment.
ssh "$TARGET" "docker rm -f routepilot-selftest-core routepilot-selftest-console >/dev/null 2>&1 || true; \
 docker run -d --rm --name routepilot-selftest-core \
      -p 127.0.0.1:18400:8400 \
      -e ROUTER_CONFIG=/app/configs/core.example.json \
      '$CORE_REPO:$TAG' core \
 && docker run -d --rm --name routepilot-selftest-console \
      -p 127.0.0.1:18401:8400 \
      -e ROUTER_DB=/data/routepilot.db \
      -e ROUTER_ADMIN_EMAIL=selftest@routepilot.invalid \
      -e ROUTER_ADMIN_PASSWORD=routepilot-selftest-pass \
      '$CONSOLE_REPO:$TAG' peripheral \
 && for i in \$(seq 1 30); do \
      curl -fsS -m 2 http://127.0.0.1:18400/healthz >/dev/null 2>&1 \
      && curl -fsS -m 2 http://127.0.0.1:18401/healthz >/dev/null 2>&1 && break; sleep 1; done \
 && curl -fsS -m 5 http://127.0.0.1:18400/healthz \
 && curl -fsS -m 5 http://127.0.0.1:18401/healthz \
 && docker stop routepilot-selftest-core routepilot-selftest-console >/dev/null \
 || { echo 'ERROR: self-test failed — container logs:'; \
      docker logs --tail 20 routepilot-selftest-core 2>&1; \
      docker logs --tail 20 routepilot-selftest-console 2>&1; \
      docker rm -f routepilot-selftest-core routepilot-selftest-console >/dev/null 2>&1 || true; exit 1; }"

echo "==> OK: $CORE_REPO:$TAG / $CONSOLE_REPO:$TAG"
