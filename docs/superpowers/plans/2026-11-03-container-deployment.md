# RoutePilot 容器化部署（一次性验证形态）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 RoutePilot 增加容器部署方式（Dockerfile + compose + 原生 k8s manifests + 构建/部署/卸载脚本），在 76 上完成端到端验证后全部卸载。

**Architecture:** 单一 `python:3.11-slim` 镜像（应用无 torch，JEV 走远程端点）服务两种验证形态：宿主机 compose（端口 8401）和 76 集群原生 manifests（NodePort 30400）。脚本从 Mac 通过 ssh/rsync 驱动 76（BatchMode 免密已配）。验证完成后 `teardown.sh` 卸载一切，systemd 现网服务（8400）全程不动。

**Tech Stack:** Docker / Docker Compose v5 / Kubernetes v1.28 原生 YAML / Harbor (192.168.95.133:30002) / bash。

**Spec:** `docs/superpowers/specs/2026-11-03-container-deployment-design.md`

## Global Constraints

- **不进 git**：本计划新增的所有文件都在 `scripts/docker/` 与 `docs/` 下（`.gitignore` 覆盖）。用户明确要求仓库要推 GitHub，这些部署物**一律不做 git add / commit**。全计划唯一允许的提交是本计划文件自身（`git add -f`，对齐仓库既有 plan/spec 强制入库惯例）。
- **零应用代码改动**：不碰 `router/`、`configs/`、`tests/`、`README*`。
- **现网零影响**：76 的 systemd `smart-router-v3`（8400）与 `litestream` 全程 active；容器形态用独立端口（compose 8401、NodePort 30400）和独立空库。
- **镜像**：`python:3.11-slim`，非 root `routepilot`（uid/gid 10001），数据目录 `/data`，监听 8400；名称 `192.168.95.133:30002/sandlake-deploy/routepilot`，tag `amd64-<git短sha>` + `latest`。
- **环境事实（2026-11-03 实测，可直接依赖）**：
  - 76 上 docker 已配 Harbor insecure registry，且 `/root/.docker/config.json` 已有 `192.168.95.133:30002` 登录凭据（push 无需再 login）。
  - 76 上 `docker compose` = v5.1.1，`rsync` 可用。
  - 上游 LLM：svc `sandlake-inference`（ClusterIP `10.233.43.73:8080`），从节点和 pod 内都可达；上游模型名 **`admin-inference-qwen06b`**（`/v1/models` 实查）。NodePort 31808 当前 **connection refused，不可用，不要用**。
  - JEV 端点：宿主机 llama-server `http://192.168.95.76:30104`，模型 `d1-2B`（现网 systemd 同款配置）。
  - NodePort **30400 空闲**；宿主机 8401 空闲（8400 被 systemd 占用）。
  - admin 登录：`admin@test.com` / `1qaz@WSX`（空库首启种子；运维脚本读 `SR_ADMIN_EMAIL`/`SR_ADMIN_PASSWORD`）。
  - 应用约束：`ROUTER_JEV_URL` 与 `ROUTER_JEV_MODEL` 必须成对出现（`router/api/app.py:94`），否则启动失败。
  - 运维脚本接口：`apply_default_routing_tree.py --tree-json configs/production_routing_tree.json --enable`（读 `SR_ADMIN_EMAIL/PASSWORD`，BASE 默认 `http://127.0.0.1:8400`）；`configure_upstream.py`（读 `SR_BASE`、`SR_UPSTREAM_BASE`、`SR_UPSTREAM_MODEL`、`SR_ADMIN_EMAIL/PASSWORD`）。
- **脚本约定**：全部 `set -euo pipefail`；对 76 的操作走 `ssh root@192.168.95.76`（BatchMode）；脚本可从 Mac 工作目录直接执行。
- **上游模型名不写死**：`SR_UPSTREAM_MODEL` 可被环境变量覆盖，默认 `admin-inference-qwen06b`；若冒烟 404，用 `ssh root@192.168.95.76 'kubectl -n default get modelserving'` 实查当前在跑的名字。

---

### Task 1: 构建链（Dockerfile + .dockerignore + docker-build.sh + 镜像自测）

**Files:**
- Create: `scripts/docker/Dockerfile`
- Create: `scripts/docker/.dockerignore`
- Create: `scripts/docker/docker-build.sh`

**Interfaces:**
- Consumes: 仓库根的 `requirements.txt`、`router/`、`configs/`、`scripts/configure_upstream.py`、`scripts/apply_default_routing_tree.py`；76 的 docker + Harbor 凭据。
- Produces: Harbor 镜像 `192.168.95.133:30002/sandlake-deploy/routepilot:<tag>`（Task 2/3 部署时按 tag 引用）；构建脚本 `docker-build.sh [tag] [ssh-target]`。

- [ ] **Step 1: 写 Dockerfile**

创建 `scripts/docker/Dockerfile`，内容如下（完整照抄）：

```dockerfile
# RoutePilot runtime image.
# No torch: graph v3 calls the JEV signal engine over HTTP (ROUTER_JEV_URL).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROUTER_HOST=0.0.0.0 \
    ROUTER_PORT=8400 \
    ROUTER_DB=/data/routepilot.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY router/ ./router/
COPY configs/ ./configs/
COPY scripts/configure_upstream.py scripts/apply_default_routing_tree.py ./scripts/

RUN groupadd -g 10001 routepilot \
 && useradd -u 10001 -g 10001 -s /usr/sbin/nologin -m routepilot \
 && mkdir -p /data \
 && chown -R routepilot:routepilot /app /data

USER 10001:10001

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=2).status == 200 else 1)"

EXPOSE 8400
ENTRYPOINT ["python", "-m", "router"]
```

- [ ] **Step 2: 写 .dockerignore**

创建 `scripts/docker/.dockerignore`，内容如下：

```
.git
.gitignore
.venv
venv
node_modules
docs
tests
.pytest_cache
__pycache__
**/__pycache__
*.py[cod]
*.db
*.db-wal
*.db-shm
.workbuddy
.superpowers
.kimi-code
.worktrees
.DS_Store
assets
scripts
!scripts/configure_upstream.py
!scripts/apply_default_routing_tree.py
```

- [ ] **Step 3: 写 docker-build.sh**

创建 `scripts/docker/docker-build.sh`，内容如下：

```bash
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

echo "==> docker build + push ($REPO:$TAG)"
if ! ssh "$TARGET" "cd '$STAGING' && docker build -t '$REPO:$TAG' -t '$REPO:latest' . \
      && docker push '$REPO:$TAG' && docker push '$REPO:latest'"; then
  echo "ERROR: build/push failed. If push was rejected with unauthorized, run:" >&2
  echo "  ssh $TARGET 'docker login $REGISTRY'" >&2
  exit 1
fi

echo "==> image self-test (container on 127.0.0.1:18400, then removed)"
ssh "$TARGET" "docker run -d --rm --name routepilot-selftest \
      -p 127.0.0.1:18400:8400 \
      -e ROUTER_JEV_URL=http://192.168.95.76:30104 \
      -e ROUTER_JEV_MODEL=d1-2B \
      '$REPO:$TAG' \
 && for i in \$(seq 1 30); do curl -fsS -m 2 http://127.0.0.1:18400/healthz >/dev/null 2>&1 && break; sleep 1; done \
 && curl -fsS -m 5 http://127.0.0.1:18400/healthz \
 && docker stop routepilot-selftest >/dev/null"

echo "==> OK: $REPO:$TAG"
```

然后 `chmod +x scripts/docker/docker-build.sh`。

- [ ] **Step 4: 语法检查**

Run: `bash -n scripts/docker/docker-build.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`

- [ ] **Step 5: 真实构建 + 推送 + 自测**

Run: `scripts/docker/docker-build.sh`
Expected（全部出现即通过）：
- staging rsync 无报错；
- `docker build` 结束输出镜像 id（`writing image sha256:...` → `naming to 192.168.95.133:30002/sandlake-deploy/routepilot:amd64-xxxx`）；
- 两次 `docker push` 完成（`digest: sha256:...`）；
- 自测段打印 `{"ok":true}` 且容器被移除；
- 末行 `==> OK: 192.168.95.133:30002/sandlake-deploy/routepilot:amd64-<sha>`。

失败排查：push unauthorized → `ssh root@192.168.95.76 'docker login 192.168.95.133:30002'`；`scripts/` 下运维脚本缺文件 → 检查 `.dockerignore` 的 `!` 白名单两行在 `scripts` 排除行**之后**。

---

### Task 2: 共享校验库 + compose 形态（compose.yml + env 模板 + compose-up.sh）

**Files:**
- Create: `scripts/docker/lib.sh`
- Create: `scripts/docker/compose.yml`
- Create: `scripts/docker/env.compose.example`
- Create: `scripts/docker/compose-up.sh`

**Interfaces:**
- Consumes: Task 1 的 Harbor 镜像（tag 参数，默认 `latest`）；76 上 `docker compose` v5。
- Produces: `lib.sh` 的 `log` / `die` / `wait_healthz <base> <tries>` / `smoke <base>`（Task 3、4 复用，签名原样）；compose 形态运行在 `http://192.168.95.76:8401`；部署目录 `/opt/routepilot-container/`。

- [ ] **Step 1: 写 lib.sh（共享函数）**

创建 `scripts/docker/lib.sh`：

```bash
#!/usr/bin/env bash
# Shared helpers for RoutePilot container verification scripts.
# shellcheck shell=bash

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

healthz_ok() {  # $1 = base url
  curl -fsS -m 3 "$1/healthz" >/dev/null 2>&1
}

wait_healthz() {  # $1 = base url, $2 = tries
  local base="$1" tries="${2:-60}"
  for _ in $(seq 1 "$tries"); do
    if healthz_ok "$base"; then return 0; fi
    sleep 1
  done
  return 1
}

smoke() {  # $1 = base url — healthz + console page + real /v1 routing
  local base="$1" code hdr="/tmp/sr-smoke-headers.$$"
  log "smoke: /healthz"
  curl -fsS -m 5 "$base/healthz" | grep -q '"ok":true' || die "/healthz did not report ok"
  log "smoke: console page"
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$base/console/index.html")
  [ "$code" = "200" ] || die "/console/index.html -> $code"
  log "smoke: /v1 chat completion (model=auto)"
  curl -fsS -m 90 -D "$hdr" "$base/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"auto","messages":[{"role":"user","content":"用一句话回答：1+1=?"}]}' \
    | python3 -c 'import sys,json; json.load(sys.stdin)["choices"][0]["message"]["content"]' \
    || { rm -f "$hdr"; die "/v1 response missing choices[0].message.content"; }
  grep -qi '^x-sr-model:' "$hdr" || { rm -f "$hdr"; die "no x-sr-model header in /v1 response"; }
  log "smoke: OK ($(grep -i '^x-sr-model:' "$hdr" | head -1 | tr -d '\r'))"
  rm -f "$hdr"
}
```

- [ ] **Step 2: 写 compose.yml 与 env 模板**

创建 `scripts/docker/compose.yml`：

```yaml
name: routepilot

services:
  routepilot:
    image: ${IMAGE:-192.168.95.133:30002/sandlake-deploy/routepilot:latest}
    container_name: routepilot
    ports:
      - "8401:8400"
    env_file: .env
    volumes:
      - ./data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=2).status == 200 else 1)"]
      interval: 15s
      timeout: 3s
      start_period: 10s
      retries: 3
```

创建 `scripts/docker/env.compose.example`：

```
IMAGE=192.168.95.133:30002/sandlake-deploy/routepilot:latest
ROUTER_HOST=0.0.0.0
ROUTER_PORT=8400
ROUTER_DB=/data/routepilot.db
ROUTER_JEV_URL=http://192.168.95.76:30104
ROUTER_JEV_MODEL=d1-2B
ROUTER_JEV_TIMEOUT=10
ROUTER_SESSION_MAX_ENTRIES=10000
ROUTER_ADMIN_EMAIL=admin@test.com
ROUTER_ADMIN_PASSWORD=1qaz@WSX
```

- [ ] **Step 3: 写 compose-up.sh**

创建 `scripts/docker/compose-up.sh`：

```bash
#!/usr/bin/env bash
# Deploy the RoutePilot compose form on 76 (host port 8401), then init + smoke.
# Usage: scripts/docker/compose-up.sh [tag] [ssh-target]
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TAG="${1:-latest}"
TARGET="${2:-root@192.168.95.76}"
BASE="http://192.168.95.76:8401"
DIR="/opt/routepilot-container"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
UPSTREAM_BASE="http://10.233.43.73:8080/v1"   # sandlake-inference ClusterIP
UPSTREAM_MODEL="${SR_UPSTREAM_MODEL:-admin-inference-qwen06b}"
ADMIN_EMAIL="${SR_ADMIN_EMAIL:-admin@test.com}"
ADMIN_PASSWORD="${SR_ADMIN_PASSWORD:-1qaz@WSX}"

log "preflight: host port 8401"
RUNNING=$(ssh "$TARGET" "docker ps --filter name=^routepilot$ -q")
if [ -z "$RUNNING" ] && ssh "$TARGET" "ss -tln | grep -q ':8401 '"; then
  die "host port 8401 already in use by another process"
fi

log "sync compose files to $TARGET:$DIR"
ssh "$TARGET" "mkdir -p '$DIR'"
rsync -az "$SRC/scripts/docker/compose.yml" "$TARGET:$DIR/compose.yml"
if ssh "$TARGET" "test -f '$DIR/.env'"; then
  log ".env already on target — keeping it"
else
  log "generate .env (tag=$TAG)"
  sed "s#^IMAGE=.*#IMAGE=192.168.95.133:30002/sandlake-deploy/routepilot:$TAG#" \
    "$SRC/scripts/docker/env.compose.example" | ssh "$TARGET" "cat > '$DIR/.env'"
fi
ssh "$TARGET" "mkdir -p '$DIR/data' && chown 10001:10001 '$DIR/data'"

log "docker compose up"
ssh "$TARGET" "cd '$DIR' && docker compose pull && docker compose up -d"

log "wait for /healthz on $BASE"
wait_healthz "$BASE" 60 || { ssh "$TARGET" "cd '$DIR' && docker compose logs --tail 50"; \
  die "container did not become healthy"; }

log "preflight: upstream reachable from inside the container"
ssh "$TARGET" "cd '$DIR' && docker compose exec -T routepilot python -c \
  \"import sys,urllib.request; urllib.request.urlopen('$UPSTREAM_BASE/models', timeout=10)\" " \
  || die "container cannot reach $UPSTREAM_BASE — check docker bridge to ClusterIP routing"

log "init: apply production routing tree"
ssh "$TARGET" "cd '$DIR' && docker compose exec -T \
  -e SR_ADMIN_EMAIL='$ADMIN_EMAIL' -e SR_ADMIN_PASSWORD='$ADMIN_PASSWORD' \
  routepilot python scripts/apply_default_routing_tree.py \
  --tree-json configs/production_routing_tree.json --enable"

log "init: configure upstream ($UPSTREAM_MODEL)"
ssh "$TARGET" "cd '$DIR' && docker compose exec -T \
  -e SR_ADMIN_EMAIL='$ADMIN_EMAIL' -e SR_ADMIN_PASSWORD='$ADMIN_PASSWORD' \
  -e SR_UPSTREAM_BASE='$UPSTREAM_BASE' -e SR_UPSTREAM_MODEL='$UPSTREAM_MODEL' \
  routepilot python scripts/configure_upstream.py"

smoke "$BASE"
log "OK: compose form live at $BASE (teardown: scripts/docker/teardown.sh)"
```

`chmod +x scripts/docker/lib.sh scripts/docker/compose-up.sh`。

- [ ] **Step 4: 语法检查**

Run: `bash -n scripts/docker/lib.sh && bash -n scripts/docker/compose-up.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`

- [ ] **Step 5: 真实部署 + 冒烟（对应 spec V2/V3）**

Run: `scripts/docker/compose-up.sh`
Expected（全部出现即通过）：
- `wait for /healthz` 后无报错；
- `apply production routing tree` 输出 `{"tree_id": "default-jev", ..., "enabled": true}`；
- `configure upstream` 正常结束（无 SystemExit 报错）；
- `smoke: OK (x-sr-model: ...)`；
- 末行 `OK: compose form live at http://192.168.95.76:8401`。

失败排查：
- `/healthz` 起不来 → `ssh root@192.168.95.76 'cd /opt/routepilot-container && docker compose logs'`，常见为 `.env` 缺 `ROUTER_JEV_MODEL`（app.py:94 成对校验）或 `data/` 属主不是 10001；
- `apply tree` 401 → admin 账号非种子值，用 `SR_ADMIN_EMAIL`/`SR_ADMIN_PASSWORD` 重跑；
- `/v1` 冒烟失败但 healthz 正常 → 依次查 JEV（`curl -m5 http://192.168.95.76:30104/v1/models`）与上游（`curl -m5 http://10.233.43.73:8080/v1/models`），上游模型漂移时实查 `kubectl -n default get modelserving` 后用 `SR_UPSTREAM_MODEL=<新名>` 重跑脚本（`.env` 已存在不会覆盖，需先 `ssh root@192.168.95.76 'rm /opt/routepilot-container/.env'` 或手动 `docker compose exec` 重跑 configure_upstream）。

---

### Task 3: k8s 形态（5 个 manifests + deploy-k8s.sh）

**Files:**
- Create: `scripts/docker/k8s/00-namespace.yaml`
- Create: `scripts/docker/k8s/01-secret.yaml`
- Create: `scripts/docker/k8s/02-pvc.yaml`
- Create: `scripts/docker/k8s/03-deployment.yaml`
- Create: `scripts/docker/k8s/04-service.yaml`
- Create: `scripts/docker/deploy-k8s.sh`

**Interfaces:**
- Consumes: Task 1 镜像；Task 2 的 `lib.sh`（`log`/`die`/`wait_healthz`/`smoke`，签名不变）。
- Produces: 76 集群 namespace `routepilot`（Deployment/Service NodePort 30400/PVC `routepilot-data`/Secret `routepilot-admin`）；服务地址 `http://192.168.95.76:30400`。

- [ ] **Step 1: 写 manifests**

创建 `scripts/docker/k8s/00-namespace.yaml`：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: routepilot
```

创建 `scripts/docker/k8s/01-secret.yaml`：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: routepilot-admin
  namespace: routepilot
type: Opaque
stringData:
  ROUTER_ADMIN_EMAIL: admin@test.com
  ROUTER_ADMIN_PASSWORD: 1qaz@WSX
```

创建 `scripts/docker/k8s/02-pvc.yaml`：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: routepilot-data
  namespace: routepilot
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: longhorn
  resources:
    requests:
      storage: 5Gi
```

创建 `scripts/docker/k8s/03-deployment.yaml`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: routepilot
  namespace: routepilot
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: routepilot
  template:
    metadata:
      labels:
        app: routepilot
    spec:
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: routepilot
          image: 192.168.95.133:30002/sandlake-deploy/routepilot:latest
          ports:
            - containerPort: 8400
          env:
            - name: ROUTER_DB
              value: /data/routepilot.db
            - name: ROUTER_JEV_URL
              value: http://192.168.95.76:30104
            - name: ROUTER_JEV_MODEL
              value: d1-2B
            - name: ROUTER_JEV_TIMEOUT
              value: "10"
            - name: ROUTER_ADMIN_EMAIL
              valueFrom:
                secretKeyRef: { name: routepilot-admin, key: ROUTER_ADMIN_EMAIL }
            - name: ROUTER_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef: { name: routepilot-admin, key: ROUTER_ADMIN_PASSWORD }
          resources:
            requests: { cpu: 250m, memory: 256Mi }
            limits: { cpu: "1", memory: 512Mi }
          readinessProbe:
            httpGet: { path: /healthz, port: 8400 }
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: /healthz, port: 8400 }
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 3
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: routepilot-data
```

创建 `scripts/docker/k8s/04-service.yaml`：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: routepilot
  namespace: routepilot
spec:
  type: NodePort
  selector:
    app: routepilot
  ports:
    - port: 8400
      targetPort: 8400
      nodePort: 30400
```

- [ ] **Step 2: 写 deploy-k8s.sh**

创建 `scripts/docker/deploy-k8s.sh`：

```bash
#!/usr/bin/env bash
# Render + apply the RoutePilot k8s form on 76 (NodePort 30400), then init + smoke.
# Usage: scripts/docker/deploy-k8s.sh [tag] [ssh-target]
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TAG="${1:-latest}"
TARGET="${2:-root@192.168.95.76}"
IMAGE_TAG="sandlake-deploy/routepilot:$TAG"
BASE="http://192.168.95.76:30400"
REMOTE="/opt/routepilot-k8s"
K8S_DIR="$(cd "$(dirname "$0")/k8s" && pwd)"
UPSTREAM_BASE="http://sandlake-inference.sandlake.svc.cluster.local:8080/v1"
UPSTREAM_MODEL="${SR_UPSTREAM_MODEL:-admin-inference-qwen06b}"
ADMIN_EMAIL="${SR_ADMIN_EMAIL:-admin@test.com}"
ADMIN_PASSWORD="${SR_ADMIN_PASSWORD:-1qaz@WSX}"

log "preflight: NodePort 30400 vs existing deployment"
if ssh "$TARGET" "kubectl get namespace routepilot >/dev/null 2>&1"; then
  log "namespace routepilot exists — updating in place"
elif ssh "$TARGET" "ss -tln | grep -q ':30400 '"; then
  die "port 30400 already listening on $TARGET but no routepilot namespace exists"
fi

log "render manifests (image tag: $TAG)"
RENDER="$(mktemp -d)"
for f in "$K8S_DIR"/*.yaml; do
  sed "s#sandlake-deploy/routepilot:latest#$IMAGE_TAG#" "$f" > "$RENDER/$(basename "$f")"
done
ssh "$TARGET" "mkdir -p '$REMOTE'"
rsync -az "$RENDER/" "$TARGET:$REMOTE/"
rm -rf "$RENDER"

log "apply manifests"
ssh "$TARGET" "kubectl apply -f '$REMOTE/'"

log "wait for rollout"
if ! ssh "$TARGET" "kubectl -n routepilot rollout status deploy/routepilot --timeout=180s"; then
  ssh "$TARGET" "kubectl -n routepilot describe pod -l app=routepilot | tail -30; \
                 kubectl -n routepilot logs deploy/routepilot --tail=50" || true
  die "rollout failed"
fi

log "wait for /healthz on $BASE"
wait_healthz "$BASE" 60 || { ssh "$TARGET" "kubectl -n routepilot logs deploy/routepilot --tail=50"; \
  die "pod did not become healthy"; }

log "init: apply production routing tree"
ssh "$TARGET" "kubectl -n routepilot exec deploy/routepilot -- \
  env SR_ADMIN_EMAIL='$ADMIN_EMAIL' SR_ADMIN_PASSWORD='$ADMIN_PASSWORD' \
  python scripts/apply_default_routing_tree.py \
  --tree-json configs/production_routing_tree.json --enable"

log "init: configure upstream ($UPSTREAM_MODEL)"
ssh "$TARGET" "kubectl -n routepilot exec deploy/routepilot -- \
  env SR_ADMIN_EMAIL='$ADMIN_EMAIL' SR_ADMIN_PASSWORD='$ADMIN_PASSWORD' \
  SR_UPSTREAM_BASE='$UPSTREAM_BASE' SR_UPSTREAM_MODEL='$UPSTREAM_MODEL' \
  python scripts/configure_upstream.py"

smoke "$BASE"
log "OK: k8s form live at $BASE (teardown: scripts/docker/teardown.sh)"
```

`chmod +x scripts/docker/deploy-k8s.sh`。

- [ ] **Step 3: 语法 + manifest 校验**

Run: `bash -n scripts/docker/deploy-k8s.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`

Run（server 端 dry-run，提前暴露 YAML/字段错误；与脚本同逻辑渲染。注意 namespace 要先真实创建，否则其余四个 namespaced 资源的 server dry-run 会报 not found）:
```bash
ssh root@192.168.95.76 'rm -rf /tmp/rp-render && mkdir -p /tmp/rp-render'
for f in scripts/docker/k8s/*.yaml; do
  sed "s#sandlake-deploy/routepilot:latest#sandlake-deploy/routepilot:latest#" "$f" | \
  ssh root@192.168.95.76 "cat > /tmp/rp-render/$(basename "$f")"
done
ssh root@192.168.95.76 'kubectl apply -f /tmp/rp-render/00-namespace.yaml && \
  kubectl apply --dry-run=server -f /tmp/rp-render/ 2>&1 | tail -5 && rm -rf /tmp/rp-render'
```
Expected: namespace `routepilot` created；随后 4 行 `configured (server dry run)` 或 `created (server dry run)`（secret/pvc/deployment/service）。报字段错误时先修 manifest 再继续。

- [ ] **Step 4: 真实部署 + 冒烟（对应 spec V4）**

Run: `scripts/docker/deploy-k8s.sh`
Expected（全部出现即通过）：
- `kubectl apply` 输出 5 个资源 created（namespace/secret/pvc/deployment/service）；
- `rollout status` 完成（`deployment "routepilot" successfully rolled out`）；
- init 两步输出与 Task 2 相同模式（tree enabled + configure_upstream 无报错）；
- `smoke: OK (x-sr-model: ...)`；
- 末行 `OK: k8s form live at http://192.168.95.76:30400`。

失败排查：ImagePullBackOff → `ssh root@192.168.95.76 'kubectl -n routepilot describe pod -l app=routepilot'`（镜像 tag 是否已 push；Harbor 凭据在 containerd certs.d 已配，通常无需处理）；PVC Pending → `kubectl -n routepilot get pvc`（longhorn 为默认 SC，一般即时 Bound）。

- [ ] **Step 5: PVC 持久化验证（对应 spec V5）**

Run:
```bash
REV_BEFORE=$(curl -s -X POST http://192.168.95.76:30400/admin/login -H 'Content-Type: application/json' -d '{"email":"admin@test.com","password":"1qaz@WSX"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' | xargs -I{} curl -s -H "Authorization: Bearer {}" http://192.168.95.76:30400/admin/trees | python3 -c 'import sys,json;t=json.load(sys.stdin)["trees"][0];print(t["id"],t["tree_revision"],t["enabled"])')
echo "before: $REV_BEFORE"
ssh root@192.168.95.76 'kubectl -n routepilot delete pod -l app=routepilot --wait=true && kubectl -n routepilot rollout status deploy/routepilot --timeout=120s'
REV_AFTER=$(curl -s -X POST http://192.168.95.76:30400/admin/login -H 'Content-Type: application/json' -d '{"email":"admin@test.com","password":"1qaz@WSX"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' | xargs -I{} curl -s -H "Authorization: Bearer {}" http://192.168.95.76:30400/admin/trees | python3 -c 'import sys,json;t=json.load(sys.stdin)["trees"][0];print(t["id"],t["tree_revision"],t["enabled"])')
echo "after: $REV_AFTER"
[ "$REV_BEFORE" = "$REV_AFTER" ] && echo PERSIST-OK
```
Expected: `PERSIST-OK`，且 `enabled=True`、revision 前后一致。

---

### Task 4: 卸载（teardown.sh + 无痕验收）

**Files:**
- Create: `scripts/docker/teardown.sh`

**Interfaces:**
- Consumes: Task 2/3 已知的部署痕迹位置（`/opt/routepilot-container/`、`/opt/routepilot-k8s/`、`/opt/routepilot-docker-context/`、namespace `routepilot`、镜像 repo `sandlake-deploy/routepilot`）。
- Produces: `teardown.sh [--purge]`；`--purge` 时连 Harbor 仓库一并删除（凭据取 76 上 `/root/.docker/config.json` 的 base64 auth）。

- [ ] **Step 1: 写 teardown.sh**

创建 `scripts/docker/teardown.sh`：

```bash
#!/usr/bin/env bash
# Remove the RoutePilot container verification deployments (compose + k8s).
# Usage: scripts/docker/teardown.sh [--purge]
#   --purge  also delete the Harbor repository sandlake-deploy/routepilot
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

PURGE=false
[ "${1:-}" = "--purge" ] && PURGE=true
TARGET="${TARGET_SSH:-root@192.168.95.76}"

log "teardown: compose form"
ssh "$TARGET" "if [ -d /opt/routepilot-container ]; then \
  cd /opt/routepilot-container && docker compose down --remove-orphans || true; fi"
ssh "$TARGET" "rm -rf /opt/routepilot-container"
ssh "$TARGET" "docker ps -a --filter name=routepilot-selftest -q | xargs -r docker rm -f" || true

log "teardown: k8s form"
ssh "$TARGET" "kubectl delete namespace routepilot --ignore-not-found --wait=true" || true
ssh "$TARGET" "rm -rf /opt/routepilot-k8s /opt/routepilot-docker-context"

if $PURGE; then
  log "teardown: purge Harbor repository (auth from target docker config)"
  AUTH_B64=$(ssh "$TARGET" "python3 -c \"import json;print(json.load(open('/root/.docker/config.json'))['auths']['192.168.95.133:30002']['auth'])\"") \
    || die "no Harbor auth found in $TARGET:/root/.docker/config.json"
  # URL-encode the repo path the way Harbor API expects (plain path works here).
  ssh "$TARGET" "curl -fsS -u \"\$(printf '%s' '$AUTH_B64' | base64 -d)\" \
    -X DELETE 'http://192.168.95.133:30002/api/v2.0/projects/sandlake-deploy/repositories/routepilot'" \
    || die "Harbor delete failed — delete manually in Harbor UI: project sandlake-deploy -> routepilot"
fi

log "teardown: done (systemd smart-router-v3 untouched)"
```

`chmod +x scripts/docker/teardown.sh`。

- [ ] **Step 2: 语法检查**

Run: `bash -n scripts/docker/teardown.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`

- [ ] **Step 3: 执行卸载（对应 spec V7）**

Run: `scripts/docker/teardown.sh`
Expected: 各段日志正常，无报错；末行 `teardown: done`。

- [ ] **Step 4: 无痕验收（spec §10 七项逐条核对）**

逐条执行并核对预期（任何一条不符都要修复后重跑 teardown）：

```bash
# 1. 无容器残留
ssh root@192.168.95.76 'docker ps -a --filter name=routepilot -q | wc -l'
#    Expected: 0
# 2. 部署目录已删
ssh root@192.168.95.76 'ls -d /opt/routepilot-container /opt/routepilot-k8s /opt/routepilot-docker-context 2>&1'
#    Expected: 三行都是 "No such file or directory"
# 3. namespace 与 NodePort 回收
ssh root@192.168.95.76 'kubectl get namespace routepilot 2>&1 | tail -1; kubectl get svc -A -o json | python3 -c "import json,sys; print([s[\"metadata\"][\"name\"] for s in json.load(sys.stdin)[\"items\"] if 30400 in [p.get(\"nodePort\") for p in s[\"spec\"].get(\"ports\",[])]])"'
#    Expected: NotFound + []
# 4. 宿主端口已释放
ssh root@192.168.95.76 'ss -tln | grep -E ":(8401|30400) " | wc -l'
#    Expected: 0
# 5. longhorn 无残留卷
ssh root@192.168.95.76 'kubectl -n longhorn-system get volumes.longhorn.io 2>/dev/null | grep -i routepilot | wc -l'
#    Expected: 0
# 6. systemd 现网无损
ssh root@192.168.95.76 'systemctl is-active smart-router-v3 litestream; curl -fsS -m 5 http://127.0.0.1:8400/healthz'
#    Expected: 两行 active + {"ok":true}
# 7. Harbor 镜像默认保留（本步不带 --purge，只确认能拉到 tag）
ssh root@192.168.95.76 'docker images 192.168.95.133:30002/sandlake-deploy/routepilot --format "{{.Tag}}" | sort -u | head -5'
#    Expected: 列出 amd64-<sha> 与 latest
```

---

### Task 5: 操作手册 + 现网基线收尾（V6/V8）

**Files:**
- Create: `docs/容器部署-2026-11-03.md`

**Interfaces:**
- Consumes: Task 1–4 的脚本与验收结果；spec §9 验收表。
- Produces: 完整操作手册（下次验证可直接照做）。

- [ ] **Step 1: 写操作手册**

创建 `docs/容器部署-2026-11-03.md`，结构如下（每节都要写实内容，引用实际脚本路径与实测结果）：

```markdown
# RoutePilot 容器部署手册（一次性验证形态）

## 定位
- 部署 → 验证 → 卸载；交付物在 scripts/docker/（.gitignore 覆盖，不进 GitHub）。
- 现网 systemd smart-router-v3（8400）全程不动，是兜底。

## 前置条件
- ssh 免密 root@192.168.95.76；Mac 可达 192.168.95.76 的 8401/30400（VPN）。
- 76：docker + Harbor 凭据就绪（/root/.docker/config.json）、docker compose v5、kubectl。
- 上游实查：ssh root@192.168.95.76 'kubectl -n default get modelserving'；
  kthena 路由可达性：curl -m10 http://10.233.43.73:8080/v1/models。

## 构建镜像
bash 语法：scripts/docker/docker-build.sh [tag]
（说明默认 tag 规则 amd64-<sha> + latest；push 失败的 login 提示）

## compose 形态（宿主机 8401）
scripts/docker/compose-up.sh [tag]
（部署目录 /opt/routepilot-container、.env 不覆盖已有、初始化两步、冒烟内容、
 上游固定走 ClusterIP 10.233.43.73:8080、SR_UPSTREAM_MODEL 覆盖方法）

## k8s 形态（NodePort 30400）
scripts/docker/deploy-k8s.sh [tag]
（manifests 5 件、渲染逻辑、namespace routepilot、longhorn PVC 5Gi、
 上游走 sandlake-inference.sandlake.svc、PVC 持久化验证方法）

## 冒烟内容
/healthz、/console/index.html、POST /v1/chat/completions（model=auto，断言
choices[0].message.content 与 x-sr-model 头）。

## 卸载
scripts/docker/teardown.sh（--purge 连 Harbor 仓库删除）
无痕清单 7 条（照 spec §10 列出）。

## 踩坑与边界（按实际遇到补）
- ROUTER_JEV_URL/ROUTER_JEV_MODEL 必须成对（app.py:94）。
- NodePort 31808 当前 connection refused，上游不要走它。
- .env 已存在则不覆盖；改 tag 需先删 /opt/routepilot-container/.env。
- 运维脚本读 SR_ADMIN_* 而非 ROUTER_ADMIN_*（Secret 里的名字）。
- 集群模型漂移时用 kubectl 实查后以 SR_UPSTREAM_MODEL 重跑。
```

- [ ] **Step 2: 现网基线复核（spec V6）**

Run:
```bash
ssh root@192.168.95.76 'systemctl is-active smart-router-v3 litestream && curl -fsS -m 5 http://127.0.0.1:8400/healthz && curl -s -m 60 -o /dev/null -w "v1:%{http_code}\n" http://127.0.0.1:8400/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话回答：1+1=?\"}]}"'
```
Expected: `active`、`active`、`{"ok":true}`、`v1:200`（若上游漂移导致非 200，记录现状即可——属既有上游问题，不是本计划引入；在手册「踩坑」节补一行）。

- [ ] **Step 3: 本地测试基线（spec V8）**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -3`
Expected: 与 main 基线一致（2026-11-03 为 `580 passed` 量级），**0 failed**。本计划未改应用代码，任何 FAIL 都说明误改了文件，需要回查。

- [ ] **Step 4: 确认 git 状态干净（部署物零入库）**

Run: `git status --porcelain | grep -E "scripts/docker|容器部署" ; git log --oneline -3`
Expected: grep 无输出（scripts/docker/ 与 docs/容器部署-2026-11-03.md 被 .gitignore 忽略）；最近提交只有 spec 与 plan 两个 docs 提交。

- [ ] **Step 5: 手册补记实测结果**

把 Task 2–4 的关键实测输出（tree revision、x-sr-model 值、V5 持久化前后一致、卸载无痕清单结果）补进 `docs/容器部署-2026-11-03.md` 对应小节，形成可复核的验收记录。

---

## 验收映射（spec §9 → 任务）

| spec | 任务 |
|---|---|
| V1 构建+推送 | Task 1 Step 5 |
| V2/V3 compose 链路 | Task 2 Step 5 |
| V4 k8s 链路 | Task 3 Step 4 |
| V5 PVC 持久化 | Task 3 Step 5 |
| V6 共存 | Task 5 Step 2（Task 2/3 执行期间也应顺带确认） |
| V7 卸载无痕 | Task 4 Step 3–4 |
| V8 测试基线 | Task 5 Step 3 |
