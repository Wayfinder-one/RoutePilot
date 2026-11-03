# 容器化部署设计（一次性验证形态）

日期：2026-11-03
状态：设计已确认，待实施

## 1. 背景与决策

RoutePilot v3 目前的部署方式是「rsync 代码 + venv + systemd」（`scripts/deploy-76.sh` → 76 的
`/opt/smart-router-v3/`，`smart-router-v3.service` 监听 8400，litestream 独立单元做 SQLite 复制）。
本设计增加**容器部署方式**，定位为**一次性验证能力**：

- 部署 → 端到端验证 → 卸载，不留常驻运行资源。
- 仓库需要推 GitHub，因此全部部署物放在 `.gitignore` 已覆盖的 `scripts/docker/` 与 `docs/` 下，
  不进入 git 历史（与 `scripts/deploy-76.sh`、`docs/` 的既有约定一致）。
- 现有 systemd 形态全程不动，是容器验证的兜底与对照。

已确认的关键决策（2026-11-03 与用户逐项确认）：

| 决策点 | 结论 |
|---|---|
| 目标环境 | 宿主机 docker/compose（76）+ 76 的 k8s 集群，两者都要 |
| CI/CD | 不接 Jenkins/ArgoCD，镜像手动构建手动推 |
| k8s 集群 | 192.168.95.76 的单节点集群（master01），不是 163 |
| 验证深度 | 真实部署验证：`/healthz`、`/v1` 真实路由、控制台、PVC 持久化 |
| 生命周期 | 验证完即卸载（teardown 脚本），验收无痕 |
| litestream | v1 容器形态不带 sidecar（longhorn 3 副本已覆盖卷级冗余） |
| 方案选型 | 方案 A：单镜像 + compose + 原生 k8s manifests（不用 helm） |

## 2. 目标与非目标

### 2.1 目标

1. 一条命令完成镜像构建并推送 Harbor。
2. compose 形态在 76 宿主机 `8401` 端口跑通完整链路（健康检查、上游配置、`/v1` 真实路由、控制台）。
3. k8s 形态在 76 集群 `NodePort 30400` 跑通同一链路。
4. 验证 PVC 持久化：重启 pod 后路由树、模型、上游配置不丢。
5. `teardown` 一键卸载两种形态，验收无痕；systemd 服务不受任何影响。
6. 操作手册覆盖构建、部署、验证、卸载、回滚全流程。

### 2.2 非目标

- 不做 Jenkins/ArgoCD/helm 集成（未来迁移 chart 的成本已通过原生 manifests 控到最低）。
- 不迁移现有 systemd 部署，不共享其数据库与端口。
- 不改路由引擎、控制台等应用代码；只新增部署物。
- 不做多副本、高可用（SQLite 单写者 + 进程内 affinity/in-flight 状态，`replicas=1` + `Recreate`）。
- v1 不做 litestream sidecar。

## 3. 环境事实（2026-11-03 实测）

| 事实 | 值 | 对设计的影响 |
|---|---|---|
| 76 宿主机 docker | `/usr/bin/docker`，`daemon.json` 已含 `192.168.95.133:30002` insecure registry | 直接在 76 上 build + push |
| 76 containerd | `certs.d/192.168.95.133:30002` 配了 pull/push | k8s 可直接拉 Harbor 镜像 |
| k8s 版本 | v1.28.15，单节点 `master01` | manifests 用原生 YAML |
| StorageClass | `longhorn`（默认，3 副本）、`local-path`、`nfs-storageclass` | PVC 用 longhorn |
| 上游 LLM | svc `sandlake-inference`：ClusterIP `10.233.43.73:8080`（节点与 pod 内均实测可达）；上游模型 `admin-inference-qwen06b`（`/v1/models` 实查）。**NodePort 31808 当前 connection refused，不可用** | k8s 形态走 svc DNS；compose 形态走 ClusterIP |
| JEV 端点 | 宿主机 llama-server `0.0.0.0:30104` | pod/容器走 `http://192.168.95.76:30104` |
| NodePort 占用 | 30080/30090/31808/31809/31992-31998/32549 | **30400 空闲** |
| 宿主端口占用 | 8400（systemd router）、30104（llama-server） | compose 用 **8401** |
| Mac docker daemon | 未运行 | 构建固定在 76 上做 |
| v3 应用依赖 | 仅 fastapi/uvicorn/httpx，无 torch | 镜像用 `python:3.11-slim` |

## 4. 交付物与目录

```
scripts/docker/
├── Dockerfile                  # python:3.11-slim，非 root，自带 HEALTHCHECK
├── .dockerignore               # 构建脚本放入 staging 根目录（不污染仓库根）
├── compose.yml                 # 宿主机 compose 形态（端口 8401）
├── env.compose.example         # compose 环境变量模板
├── k8s/
│   ├── 00-namespace.yaml       # namespace routepilot
│   ├── 01-secret.yaml          # admin 邮箱/密码
│   ├── 02-pvc.yaml             # longhorn 5Gi
│   ├── 03-deployment.yaml      # 单副本 + Recreate + 探针
│   └── 04-service.yaml         # NodePort 30400
├── docker-build.sh             # staging → 76 docker build → push Harbor
├── compose-up.sh               # 部署 compose 形态
├── deploy-k8s.sh               # kubectl apply + rollout + 冒烟
└── teardown.sh                 # 卸载两种形态（--purge 连 Harbor 镜像 tag 一起删）

docs/
└── 容器部署-2026-11-03.md       # 操作手册（全流程 + 踩坑）
```

## 5. 镜像设计

- **基镜像**：`python:3.11-slim`（对齐 `pyproject.toml` 的 `requires-python >= 3.11`）。
- **依赖**：只装 `requirements.txt`（fastapi / uvicorn[standard] / httpx），`--no-cache-dir`。
  v3 的 JEV 信号引擎走远程 `ROUTER_JEV_URL`，进程内无 torch，镜像预计 ~200MB。
- **内容物**：
  - `/app/router/`（含 console 静态文件；运行时只依赖包自身，不读 `configs/`）
  - `/app/configs/`（供 `apply_default_routing_tree.py --tree-json` 初始化用）
  - `/app/scripts/configure_upstream.py`、`/app/scripts/apply_default_routing_tree.py`
    （容器内初始化/运维入口；镜像里只带这两个脚本，其余 `scripts/` 不进镜像）
- **运行身份**：非 root 用户 `routepilot`（uid/gid 10001）；`/data` 为数据目录，属主 10001。
- **默认 ENV**：`ROUTER_HOST=0.0.0.0`、`ROUTER_PORT=8400`、`ROUTER_DB=/data/routepilot.db`。
- **HEALTHCHECK**：slim 无 curl，用 `python -c` + `urllib.request` 探 `http://127.0.0.1:8400/healthz`。
- **入口**：`ENTRYPOINT ["python", "-m", "router"]`，`WORKDIR /app`，`EXPOSE 8400`。
- **镜像名**：`192.168.95.133:30002/sandlake-deploy/routepilot`，
  tag = `amd64-<git 短 sha>`（对齐 sandlake 组件惯例）+ `latest`。
- **.dockerignore**：排除 `.git/ .venv/ node_modules/ docs/ tests/ *.db* __pycache__/ .pytest_cache/
  .workbuddy/ .superpowers/ .kimi-code/ scripts/`（仅重新包含上述两个运维脚本）。

## 6. compose 形态（宿主机，验证用）

- 部署目录：`/opt/routepilot-container/`（compose.yml + `.env` + `data/` bind mount → `/data`）。
- 端口：`8401:8400`（避开 systemd 的 8400）。
- `restart: unless-stopped`；自带 healthcheck。
- `.env` 从 `env.compose.example` 生成（已存在则不覆盖）：
  - `ROUTER_JEV_URL=http://192.168.95.76:30104`、`ROUTER_JEV_MODEL=d1-2B`
  - `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD`（默认值 `admin@test.com` / `1qaz@WSX`，
    与 systemd 形态一致，仅验证用）
- 数据是**全新空库**，不共享 `router-v3.db`；初始化：
  `docker compose exec routepilot python scripts/apply_default_routing_tree.py --tree-json
  configs/production_routing_tree.json --enable`，再 `configure_upstream.py` 挂上游。
  上游 base 用 **`http://10.233.43.73:8080/v1`**（`sandlake-inference` 的 ClusterIP，节点侧实测可达；
  原定的 NodePort 31808 实测 connection refused 弃用），且部署脚本在容器内做一次
  upstream 可达性预检（`docker compose exec` + urllib），再跑 `configure_upstream.py`。

## 7. k8s 形态（76 集群，验证用）

- namespace：`routepilot`。
- Deployment：
  - `replicas: 1`，`strategy.type: Recreate`（RWO PVC 不允许双写挂载）
  - 探针：readiness / liveness 均 `GET /healthz`，`initialDelay 5s`、`period 10s`、
    `failureThreshold 3`（进程秒级启动，无需 startupProbe）
  - resources：requests `250m/256Mi`，limits `1/512Mi`
  - env：
    - `ROUTER_JEV_URL=http://192.168.95.76:30104`、`ROUTER_JEV_MODEL=d1-2B`
    - `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` 来自 Secret `routepilot-admin`
  - volume：PVC `routepilot-data`（longhorn 5Gi，RWO）挂 `/data`
- Service：`NodePort 30400` → targetPort 8400。
- Secret：`routepilot-admin`（`stringData` 明文，验证环境与 systemd 形态同值）。
- 集群内初始化：`kubectl -n routepilot exec deploy/routepilot -- python
  scripts/apply_default_routing_tree.py …`，上游通过 `configure_upstream.py` 挂
  `http://sandlake-inference.sandlake.svc.cluster.local:8080/v1`。
- 不建 Ingress（76 集群无 ingress-nginx），NodePort 直出。

## 8. 脚本行为

| 脚本 | 行为 | 失败处理 |
|---|---|---|
| `docker-build.sh [tag]` | rsync 干净 staging（排除 §5 所列）→ 拷入 `.dockerignore` → `ssh 76 docker build` → 打 `amd64-<sha>` + `latest` 双 tag → `docker push` | push 失败提示 Harbor 登录命令；任一步失败即停，不产半成品部署 |
| `compose-up.sh [tag]` | 同步 compose.yml → 生成 `.env`（不覆盖已有）→ `docker compose up -d` → 轮询 `8401/healthz` 60s | 超时打印 `docker compose logs` 尾部；8401 被占时启动前报错退出 |
| `deploy-k8s.sh [tag]` | `kubectl apply -f` → `rollout status` → 冒烟：30400 `/healthz`、控制台页 200 | rollout 失败打印 pod events；30400 被占（apply 前查svc）时明确报错 |
| `teardown.sh` | compose：`down` + 删 `/opt/routepilot-container/`；k8s：`delete namespace routepilot`（PVC/Secret/Service 随删，longhorn 卷按 Delete 回收）；清本地 staging | 幂等：资源不存在时跳过并提示；`--purge` 追加删 Harbor tag（Harbor API） |

- 镜像 tag 与部署解耦：部署脚本都接受 `tag` 参数（默认 `latest`）。
- `configure_upstream.py` 的上游模型名（`SR_UPSTREAM_MODEL`）**不写死**：作为参数/环境变量
  传入，部署时取 76 集群当前在跑的 ModelServing 名（如 `admin-inference-qwen06b`，
  以 `kubectl -n default get modelserving` 实查为准），避免集群模型漂移导致冒烟 404。
- 全部脚本 `set -euo pipefail`；对 76 的操作统一走 ssh（BatchMode）。

## 9. 验证计划（验收标准）

| # | 操作 | 预期 |
|---|---|---|
| V1 | `docker-build.sh` | build 成功、Harbor 出现 `routepilot:amd64-<sha>` 与 `:latest` |
| V2 | `compose-up.sh` | 60s 内 `8401/healthz` 200 |
| V3 | compose 初始化 + 冒烟 | 默认树 enabled；`configure_upstream` 200；`/v1/chat/completions`（model=auto）返回 200 且带 `x-sr-model`；`/console/index.html` 200 |
| V4 | `deploy-k8s.sh` | rollout 就绪；30400 通过同一冒烟集 |
| V5 | PVC 持久化 | `kubectl delete pod` 后新 pod 就绪，`/admin/trees` 树仍在、revision 不变 |
| V6 | 共存 | 全程 `systemctl is-active smart-router-v3` = active，8400 服务正常 |
| V7 | `teardown.sh` | 见 §10 无痕清单 |
| V8 | 本地基线 | 应用代码零改动，`pytest` 结果与基线一致 |

## 10. 卸载与无痕标准

`teardown.sh` 执行后逐项核对：

1. `docker ps -a`：无 routepilot 容器；`docker compose ls` 无该项目。
2. `/opt/routepilot-container/` 不存在。
3. `kubectl get namespace routepilot` → NotFound；`kubectl get svc -A | grep 30400` → 空。
4. 宿主机 `ss -tlnp` 无 8401 监听；`curl -m2 127.0.0.1:30400` 拒绝。
5. longhorn 无 `pvc-routepilot` 残留卷。
6. `systemctl is-active smart-router-v3 litestream` = active；8400 正常服务。
7. Harbor 镜像默认保留（复验用）；`--purge` 后 tag 删除。

## 11. 错误处理

- **Harbor push 失败**：提示 `docker login 192.168.95.133:30002`（76 已配 insecure，通常只需首次登录）。
- **ImagePullBackOff / 探针失败**：`deploy-k8s.sh` 打印 pod events 与日志尾部再退出。
- **上游/JEV 不可达**：不阻塞容器启动（应用启动仅要求 `ROUTER_JEV_URL` 与 `ROUTER_JEV_MODEL`
  成对出现，见 `router/api/app.py`）；`/v1` 会在请求期报上游错误，冒烟据此可判定连通性。
- **端口冲突**：8401 / 30400 启动前显式检查，冲突即退出并指明占用者。
- **数据目录权限**：PVC / bind mount 均以 uid 10001 运行；脚本在部署前 `chown` bind mount 目录。

## 12. 回滚

- compose：`docker compose down`（`teardown.sh` 已含）。
- k8s：`kubectl delete namespace routepilot`（`teardown.sh` 已含）。
- systemd 形态全程不动，是天然兜底：容器验证的任何失败都不影响线上 8400 服务。
