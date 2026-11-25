#!/usr/bin/env bash
# Build the RoutePilot image on 76 (Harbor-insecure docker) and push it.
# Usage: scripts/docker/docker-build.sh [tag] [ssh-target]
set -euo pipefail

TAG="${1:-}"
TARGET="${2:-root@192.168.95.76}"
REGISTRY="192.168.95.133:30002"
REPO="$REGISTRY/sandlake-deploy/routepilot"
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

echo "==> docker build + push ($REPO:$TAG)"
if ! ssh "$TARGET" "cd '$STAGING' && docker build -t '$REPO:$TAG' . \
      && docker push '$REPO:$TAG'"; then
  echo "ERROR: build/push failed. If push was rejected with unauthorized, run:" >&2
  echo "  ssh $TARGET 'docker login $REGISTRY'" >&2
  exit 1
fi

echo "==> image self-test (container on 127.0.0.1:18400, then removed)"
ssh "$TARGET" "docker rm -f routepilot-selftest >/dev/null 2>&1 || true; \
 docker run -d --rm --name routepilot-selftest \
      -p 127.0.0.1:18400:8400 \
      -e ROUTER_JEV_URL=http://192.168.95.76:30104 \
      -e ROUTER_JEV_MODEL=d1-2B \
      '$REPO:$TAG' \
 && for i in \$(seq 1 30); do curl -fsS -m 2 http://127.0.0.1:18400/healthz >/dev/null 2>&1 && break; sleep 1; done \
 && curl -fsS -m 5 http://127.0.0.1:18400/healthz \
 && docker stop routepilot-selftest >/dev/null \
 || { echo 'ERROR: self-test failed — container logs:'; docker logs --tail 20 routepilot-selftest 2>&1; docker rm -f routepilot-selftest >/dev/null 2>&1 || true; exit 1; }"

echo "==> OK: $REPO:$TAG"
