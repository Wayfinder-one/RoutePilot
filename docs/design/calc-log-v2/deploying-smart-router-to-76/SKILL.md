---
name: deploying-smart-router-to-76
description: Use when deploying smart-router changes to 192.168.95.76 (production systemd host `smart-router-v3`, port 8400) — incremental console/backend/config updates, checking whether a deploy is even needed, post-deploy verification, upstream-404 triage, or rollback after a bad deploy. 触发词：部署到 76、上线到 76、smart-router-v3、8400、回滚 76、部署验证。
---

# 部署 smart-router 到 76

## 概览

流程：**算改动清单 → 核对远端是否已最新（幂等）→ 备份 → rsync（不带 `--delete`）→ 按变更类型决定是否重启 → 四道闸验证 → 记台账 / 回滚**。

76 上的 `/opt/smart-router-v3` **不是 git 仓库**，回滚只能靠 76 上的 `.deploy-backup-*` 目录。

## 现网形态（2026-11 实机核验）

| 项 | 值 |
|---|---|
| 主机 | `root@192.168.95.76`（节点 `master01`，单节点 k8s v1.28） |
| 服务 | `/etc/systemd/system/smart-router-v3.service`（active + enabled，`Restart=on-failure`） |
| 代码 | `/opt/smart-router-v3`（**非 git 仓库**） |
| 解释器 | `/opt/smart-router-v3/.venv/bin/python`（3.11.16），入口 `python -m router` |
| 端口 | `8400`（unit 里 `ROUTER_HOST=0.0.0.0`，**Mac 可直连**） |
| 控制台 | `http://192.168.95.76:8400/console/index.html` |
| 数据库 | `/opt/smart-router-v3/router-v3.db`（SQLite WAL）+ `litestream` sidecar（副本 `.router-v3.db-litestream/`） |
| 图存储 | `/opt/smart-router-v3/routing-graph/`（durable，**绝不 rsync**） |
| JEV 后端 | llama.cpp d1-2B `http://127.0.0.1:30104`（独立进程，部署不影响它） |
| 模型上游 | kthena 网关 `http://10.233.106.203:8080/v1`（模型会被回收，见闸 3） |
| 登录 | 控制台 `admin@test.com / 1qaz@WSX`；`/admin/*` 要 Bearer session token（静态 admin token 已于 2026-10-26 移除） |
| 容器验证形态 | compose `8401` / k8s NodePort `30400`，见 `docs/容器部署-2026-11-03.md`，与本文无关 |

## 什么时候用 / 不用

- **用**：仓库代码（`router/`、`configs/`、`tests/`）或控制台静态文件要上 76；上线后验证；上线坏了回滚。
- **不用**：从零起新机 → `scripts/deploy-76.sh`（bootstrap，见「常见错误」）；容器/k8s 验证 → 容器部署手册；只改线上配置/决策树 → 走 `/admin/*` API，不需要部署。

## 第 0 步 · 改动清单与分类（决定要不要部署、要不要重启）

先列出相对「上次部署的 commit」到底改了哪些文件——不要凭记忆：

```bash
cd /Users/yuansu/work/smart-router
PATHS="router/ configs/ tests/ assets/ conftest.py requirements.txt pyproject.toml README.md README.zh-CN.md package.json package-lock.json"
git log --oneline -5                       # 从 docs/部署到76.md 找上次部署的 sha
git diff --name-status --diff-filter=ACMR <上次部署 sha>..HEAD -- $PATHS
```

- `--diff-filter=ACMR` 排除「删除/改名」；删除的文件不会进 md5 校验，按第 3 步显式 `rm` 远端。
- `scripts/`（含部署/QA 脚本、`package.json` 引用的 `console_qa.mjs`）被 `.gitignore` 排除、未入库，它的改动**不出现在 `git diff` 里但会被 rsync 同步**，需要人工核对并写进台账。

**幂等核对（先做，避免白重启清日志环）**：把清单里的文件逐个 md5 比远端（命令见闸 1）。**全部一致 = 远端已是最新**：跳过第 2–4 步的备份/同步/重启，但仍要跑闸 2（只读）和闸 3 的一条真实请求确认服务活着，再记台账。

| 变更 | 重启？ | 额外动作 |
|---|---|---|
| `router/console/*.html/js/css`（纯静态，含 `i18n.js`/`shared.css`） | 否 | rsync 即生效，浏览器强刷 |
| `router/**` 下任意 `.py`（`api/`、`routing/` 含 `engine.py`、`requestlog.py`、`db/`） | **是** | 先本地 pytest；备份 DB |
| DB schema / 迁移 | 是 | 备份 DB；上线后核对 `schema_migrations` |
| `requirements.txt` / `pyproject.toml` | 是 | 远端 `./.venv/bin/pip install -r requirements.txt` |
| systemd 单元 env | 是 + `daemon-reload` | 直接改远端单元，先备份单元文件 |
| `configs/*.json` | 多数否 | 磁盘上的 configs 不自动生效，线上树在 DB 里；要应用走脚本/API。同步与否见第 3 步 |
| `routing-graph/`、`*.db*`、`data/`、`models/`、`wheels/`、`backups/` | — | **永远不 rsync** |

混合变更按「需要重启的那一类」处理。

## 第 1 步 · 前置检查

```bash
ssh -o BatchMode=yes -o ConnectTimeout=5 root@192.168.95.76 \
  'hostname; systemctl is-active smart-router-v3 litestream; curl -s -o /dev/null -w "healthz:%{http_code} " http://127.0.0.1:8400/healthz; curl -s -o /dev/null -w "readyz:%{http_code}\n" http://127.0.0.1:8400/readyz'
git -C /Users/yuansu/work/smart-router rev-parse --short HEAD
```

期望：`master01` / `active` 两行 / `healthz:200 readyz:200`。

- 连不上 = VPN（aTrust）掉了 → 用 `macos-atrust-vpn-login` 技能恢复。76 只能从本机 ssh（163 上没配 .76 的 key），跨集群 pod 也打不通 76 的 pod IP。
- 本地健康闸（不通过不部署）：

```bash
cd /Users/yuansu/work/smart-router
.venv/bin/python -m pytest tests/ -q
npm run check:console     # node --test + console_qa.mjs；自起本地 router+mock，不打 76
```

> `package.json` 和 `scripts/` 在 `.gitignore` 里，新 clone / 别的机器上这两条闸不可用；降级跑 `.venv/bin/python -m compileall -q router`，改过前端时对每个改动 JS 跑 `node --check`，并在台账写明「本地闸降级」。本地 `.venv` 解释器版本与远端 3.11 不同，pytest 绿不等于远端可跑。

## 第 2 步 · 备份（先备份，后同步；目录名固定）

```bash
cd /Users/yuansu/work/smart-router
TS=$(date +%Y%m%d-%H%M); SLUG=change-slug; BK=/opt/smart-router-v3/.deploy-backup-$TS-$SLUG
ssh root@192.168.95.76 "BK=$BK bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$BK" && cd /opt/smart-router-v3
cp /etc/systemd/system/smart-router-v3.service "$BK/"
tar czf "$BK/router-before.tgz" router configs scripts tests README.md requirements.txt pyproject.toml
tar tzf "$BK/router-before.tgz" >/dev/null    # 归档不可读就失败退出，不带着坏备份往下走
./.venv/bin/python - "$BK/router-v3.db.bak" <<'PY'
import sqlite3, sys
s = sqlite3.connect("/opt/smart-router-v3/router-v3.db")
d = sqlite3.connect(sys.argv[1])
s.backup(d); d.close(); s.close()
PY
ls -lh "$BK"
REMOTE
```

- `<<'REMOTE'` 用引号：远端脚本原样传过去，本地不做 `$` 展开（以后加变量不会被静默吞掉）；备份路径通过 `BK=$BK` 作为远端环境变量传入。
- 76 上**没有 `sqlite3` CLI**，必须用 venv python 的 `Connection.backup()` 在线热备；`cp` 运行中的 WAL 库不安全。
- 纯前端变更可省 DB 备份，但备份很便宜，照做更省心。

## 第 3 步 · 同步（禁止 `--delete`）

```bash
cd /Users/yuansu/work/smart-router
rsync -az --no-owner --no-group \
  --exclude '.git/' --exclude '.venv/' --exclude 'node_modules/' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.deploy-backup-*/' --exclude '.router-v3.db-litestream/' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  --exclude 'routing-graph/' --exclude 'data/' --exclude 'models/' \
  --exclude 'backups/' --exclude 'wheels/' \
  --exclude 'docs/' --exclude 'configs/cases/' \
  ./ root@192.168.95.76:/opt/smart-router-v3/
```

- 远端有 DB、图、模型、备份等运行时资产，**`--delete` 会删掉它们**。
- **configs 二选一**（命令默认会同步）：
  - (a) 本次确实改了 `configs/` 且要上盘：保留同步，闸 1 核 md5，并在台账写明「只上盘、未应用」；
  - (b) 本次不涉及 `configs/`：命令加 `--exclude 'configs/'`，同时把 `configs/` 从第 0 步 `PATHS` 和闸 1 清单里去掉，台账写明「configs 未同步」。
- 仓库里**删掉**的文件要显式删远端（`ssh ... 'rm /opt/smart-router-v3/router/xxx'`）。
- `--no-owner --no-group`：避免把 Mac 的 uid 501 带到远端（不带也能跑，属主会乱）。
- 同步后必须做闸 1 的 md5 核对。

## 第 4 步 · 重启（仅当第 0 步判定需要）

```bash
ssh root@192.168.95.76 'systemctl restart smart-router-v3'
ok=0
for i in $(seq 1 30); do
  ssh root@192.168.95.76 'curl -fsS -m 2 http://127.0.0.1:8400/healthz >/dev/null && curl -fsS -m 2 http://127.0.0.1:8400/readyz >/dev/null' && { ok=1; break; }
  sleep 1
done
[ "$ok" = 1 ] || { echo "服务未就绪："; ssh root@192.168.95.76 'journalctl -u smart-router-v3 -n 50 --no-pager'; exit 1; }
ssh root@192.168.95.76 'systemctl is-active smart-router-v3; systemctl show smart-router-v3 -p NRestarts -p ActiveEnterTimestamp'
```

- 等的是 **healthz + readyz 都 200**：进程活着但快照还没建好时 `readyz` 会 503，这是正常过渡，所以别只探 healthz 就往下走。
- 通过：`active`、`NRestarts=0`、`ActiveEnterTimestamp` 是刚才。
- 重启会清空内存日志环（最近 20 条会从 DB 回填，但没有 trace/text），日志页短暂「无路径」是**预期**，不是 bug。
- 纯静态变更**不要**重启：无意义，还白清日志环。

## 第 5 步 · 四道闸验证

**闸 1 · 文件一致**（用第 0 步的清单，别只挑一两个文件）

```bash
cd /Users/yuansu/work/smart-router
PATHS="router/ configs/ tests/ assets/ conftest.py requirements.txt pyproject.toml README.md README.zh-CN.md package.json package-lock.json"
FILES=$(git diff --name-only --diff-filter=ACMR <上次部署 sha>..HEAD -- $PATHS | tr '\n' ' ')
if [ -z "$FILES" ]; then echo "清单为空：跳过闸 1"; else
  md5 -r $FILES | awk '{print $1, $2}' | sort -k2
  ssh root@192.168.95.76 "cd /opt/smart-router-v3 && md5sum $FILES" | awk '{print $1, $2}' | sort -k2
fi
```

两边输出的「哈希 + 路径」应逐行一致（`md5 -r` 与 `md5sum` 同格式，按第二列排序后可直接肉眼/`diff` 对）。清单为空时跳过本闸并说明原因。

**闸 2 · 服务面**

```bash
ssh root@192.168.95.76 'curl -s -o /dev/null -w "healthz:%{http_code} readyz:" http://127.0.0.1:8400/healthz; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8400/readyz; \
  for p in index routing logs models jev users; do printf "%s:" $p; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8400/console/$p.html; done; \
  journalctl -u smart-router-v3 --since "-5 min" -p warning -q | tail -5'
```

期望：healthz/readyz 200、6 个页面全 200、journal 无输出。刚重启后 `readyz` 若 503，等 2s 重试一次仍 503 才算失败。

**闸 3 · 管理面 + 真实请求**（Mac 直连；若连不上，把同一段 `curl` 放进 `ssh root@… '<…>'` 在机上跑）

```bash
TOKEN=$(curl -s -X POST http://192.168.95.76:8400/admin/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@test.com","password":"1qaz@WSX"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -D /tmp/h.txt -o /tmp/b.json -X POST http://192.168.95.76:8400/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"用 Python 写快排"}]}'
head -1 /tmp/h.txt; grep -i '^x-sr-model' /tmp/h.txt; head -c 300 /tmp/b.json
curl -s -H "Authorization: Bearer $TOKEN" 'http://192.168.95.76:8400/admin/requests/recent?limit=5' | head -c 500
```

通过：`HTTP/1.1 200`、响应头有 `x-sr-model`、body 有真实补全、`requests/recent` 首条是刚产生的记录（含 trace/waves）。

`/v1` 返回 404 `route not found` 或 502 时：**先怀疑上游模型被回收（kthena 漂移），别当成本次部署坏了**：

```bash
ssh root@192.168.95.76 'kubectl -n default get modelserving; curl -s http://10.233.106.203:8080/v1/models'
# 用当前在跑的 chat 模型重挂（脚本默认模型名是旧的，必须显式传 SR_UPSTREAM_MODEL）
ssh root@192.168.95.76 'cd /opt/smart-router-v3 && SR_BASE=http://127.0.0.1:8400 \
  SR_UPSTREAM_MODEL=<当前模型名> ./.venv/bin/python scripts/configure_upstream.py'
```

**闸 4 · 变更相关面**

- 树没打算改时，`revision`/`snapshot` 应不变（复用闸 3 的 `$TOKEN`）：

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://192.168.95.76:8400/admin/trees | head -c 300
```

- 前端目检（本机有 playwright-core 与缓存的 Chrome；`?token=` 会一次性写入会话、随后从 URL 清掉）：

```bash
cd /Users/yuansu/work/smart-router
CHROME=${CHROME:-$(ls "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac-*/"Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" 2>/dev/null | tail -1)}
if [ ! -x "$CHROME" ]; then echo "未找到本机 Chrome，跳过目检（可 export CHROME=... 后重跑）"; else
CHROME="$CHROME" TOKEN="$TOKEN" node --input-type=module <<'JS'
import { chromium } from 'playwright-core';
const b = await chromium.launch({ executablePath: process.env.CHROME });
const p = await b.newPage();
const errs = [];
p.on('console', m => m.type() === 'error' && errs.push(m.text()));
p.on('pageerror', e => errs.push(e.message));
await p.goto(`http://192.168.95.76:8400/console/logs.html?token=${process.env.TOKEN}`);
await p.waitForTimeout(2000);
console.log('rows=', await p.locator('.lg-item').count(), 'errors=', errs);
await b.close();
JS
fi
```

通过：`rows=` > 0、`errors=[]`；再手点一行确认展开区块（改日志页时看 `.lg-activation`/`.lg-schedule` 等目标区块）。

- DB 迁移（仅当改过 schema）：

```bash
ssh root@192.168.95.76 '/opt/smart-router-v3/.venv/bin/python -c "import sqlite3;print(sqlite3.connect(\"/opt/smart-router-v3/router-v3.db\").execute(\"select max(version) from schema_migrations\").fetchone())"'
```

## 第 6 步 · 记录到部署台账

在 `docs/部署到76.md` 末尾追加一节（唯一部署台账；若同一批改动此前已记录，更新那一节而不要重复新增）：

```markdown
## <主题>上线（YYYY-MM-DD HH:MM）

- 提交：`<sha> <subject>`（范围 `<from>..<to>`）
- 变更：<文件/行为；是否含 DB 迁移；是否需重启；configs 是否同步>
- 备份：`/opt/smart-router-v3/.deploy-backup-<TS>-<slug>/`（router-before.tgz / router-v3.db.bak / 单元文件）
- 部署：rsync（不带 --delete）+（是/否）重启
- 验证：md5 一致的文件清单；healthz/readyz/6 页面/真实 `/v1`/requests.recent；journal 无 warning
- 回滚：<具体命令>
```

> 台账开头几节还是 v1/v2 形态（`sr serve`、8899、`smart-router.service`），那些是历史记录；当前现网以本文的 v3（`smart-router-v3`、8400）为准。

## 回滚

含新增文件时先删掉它们（tar 还原不会删新文件，残留 `.py` 会改变 import 行为）：

```bash
cd /Users/yuansu/work/smart-router
PATHS="router/ configs/ tests/ assets/ conftest.py requirements.txt pyproject.toml README.md README.zh-CN.md package.json package-lock.json"
NEW=$(git diff --name-only --diff-filter=A <上次部署 sha>..HEAD -- $PATHS | tr '\n' ' ')
if [ -n "$NEW" ]; then ssh root@192.168.95.76 "cd /opt/smart-router-v3 && rm -f $NEW"; else echo "无新增文件"; fi
```

纯静态（未重启）：

```bash
ssh root@192.168.95.76 'rsync -a /opt/smart-router-v3/.deploy-backup-<TS>-<slug>/router/console/ /opt/smart-router-v3/router/console/'
```

含后端或 DB 迁移：

```bash
ssh root@192.168.95.76 'systemctl stop smart-router-v3 litestream'
ssh root@192.168.95.76 'cd /opt/smart-router-v3 && tar xzf .deploy-backup-<TS>-<slug>/router-before.tgz -C /opt/smart-router-v3'
# 仅当这次动过库：先停 litestream 再恢复，否则副本与主库打架
ssh root@192.168.95.76 'cd /opt/smart-router-v3 && cp .deploy-backup-<TS>-<slug>/router-v3.db.bak router-v3.db && rm -f router-v3.db-wal router-v3.db-shm'
ssh root@192.168.95.76 'cd /opt/smart-router-v3 && cp .deploy-backup-<TS>-<slug>/smart-router-v3.service /etc/systemd/system/ && systemctl daemon-reload'
ssh root@192.168.95.76 'systemctl start smart-router-v3 litestream && sleep 2 && curl -fsS http://127.0.0.1:8400/healthz'
```

回滚后重跑闸 1（md5 应回到部署前）和闸 3（`/v1` 200）。恢复 DB 会丢掉部署后产生的请求记录。

## 常见错误 / 坑

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 用 `scripts/deploy-76.sh` 做增量更新 | 它是 bootstrap 脚本：`--delete` 删远端未排除目录、重写 systemd 单元、按旧默认模型跑 `configure_upstream.py` | 增量一律 rsync + 按需重启；只有起新机才用脚本 |
| `rsync --delete` | 删掉 `routing-graph/`、`data/`、`models/`、`backups/` | 永远不带 `--delete` |
| 改了 Python 却忘记重启 | 进程跑旧代码，验证「通过」是假的 | 第 0 步分类；重启后核对 `ActiveEnterTimestamp` |
| 直接 `cp` 运行中的 `.db` | WAL 未合并，备份不可用 | 用 sqlite `Connection.backup()` |
| `/v1` 404/502 就当部署坏了 | 误判回滚，其实上游模型漂移 | 按闸 3 排查上游并重挂 |
| 以为 `console_qa.mjs` 会打 76 | 它是本地自起 router+mock 的 QA | 线上用闸 3/闸 4 的 curl + Playwright |
| 到 76 上 `git checkout` 回滚 | `/opt/smart-router-v3` 不是 git 仓库 | 用备份目录 |
| 回滚只还原不删新增文件 | 新 `.py` 残留，import 行为被改变 | 回滚前按 `--diff-filter=A` 清单删远端新增文件 |
| 把 `data/signal_questions.json` 覆盖成仓库版 | 冲掉线上运行期编辑 | `data/` 排除；改仓库 `configs/` 后走 API |
| 忘了 `docs/`、`scripts/` 在 `.gitignore` | 新写的部署记录/skill 不进 `git status` | 需要入库时 `git add -f` |
| 重复部署同一批改动 | 白重启、清日志环 | 第 0 步先做幂等核对 |

## 快速参考

| 目的 | 命令 |
|---|---|
| 看服务 | `ssh root@192.168.95.76 'systemctl status smart-router-v3 --no-pager'` |
| 看日志 | `ssh root@192.168.95.76 'journalctl -u smart-router-v3 -n 50 --no-pager'` |
| 备份命名 | `/opt/smart-router-v3/.deploy-backup-<YYYYMMDD-HHMM>-<slug>/` |
| 起新机 / bootstrap | `SR_JEV_URL=http://127.0.0.1:30104 bash scripts/deploy-76.sh` |
| 查上游模型 | `ssh root@192.168.95.76 'kubectl -n default get modelserving'` |
| 重挂上游 | 在 76 的 `/opt/smart-router-v3`：`SR_BASE=http://127.0.0.1:8400 SR_UPSTREAM_MODEL=<name> ./.venv/bin/python scripts/configure_upstream.py` |
