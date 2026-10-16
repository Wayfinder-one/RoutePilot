"""SQLite 读写层：建表（spec §3.1/§3.2 逐字）、默认配置与种子落库（balanced 树定义
在 sr/engine.TEMPLATES，本模块只落库）、版本号、审计。

约定：
- 所有入库字符串一律语言无关键（spec §3.1 存储约定）；节点 name/desc 与 block_msg
  属用户可见文案，按 §9.2/§6.5 原样存双语/显示词。
- ``get_tree_version`` / ``bump_tree_version`` / ``log_change`` 不自行 commit，
  事务由调用方控制（便于多写操作原子提交）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime

from sr.engine import TEMPLATES

# 预置键（spec §3.1 settings 表注释），值一律字符串。
DEFAULT_SETTINGS: dict[str, str] = {
    "session_sticky": "1",
    "session_idle": "1800",           # 半小时
    "sticky_detect_feedback": "0",
    "budget_protect": "1",
    "budget_water": "0.8",
    "budget_daily_limit_usd": "10.0",   # 当日花费（USD）触发线基数（× budget_water）
    "budget_route": "qwen3-8b",
    "max_retries": "2",
    "batch_window_ms": "5",
    "retention_days": "30",
    "ladder": "qwen3-8b,qwen3-32b,claude-sonnet",
    "tree_version": "1",
}

# spec §3.1 全部 CREATE TABLE + §3.2 四条索引，逐字录入（含原注释）。
SCHEMA = """
-- ── 配置（可编辑） ──
CREATE TABLE providers (
  name      TEXT PRIMARY KEY,        -- 供应商名称
  proto     TEXT NOT NULL,          -- 'openai' | 'anthropic'
  base_url  TEXT NOT NULL,
  key_env   TEXT,                   -- 环境变量名
  extra     TEXT DEFAULT '{}',      -- JSON: extra_headers 等
  ok        INTEGER DEFAULT 1,      -- 0=熔断
  break_reason TEXT                 -- 熔断原因
);

CREATE TABLE models (
  id        TEXT PRIMARY KEY,        -- 内部名称（分流树里显示的）
  provider  TEXT NOT NULL REFERENCES providers(name),
  proto     TEXT NOT NULL,           -- 跟随 provider
  upstream  TEXT NOT NULL,          -- 上游模型名（发给后端的真实名）
  caps      TEXT NOT NULL,          -- JSON: ["text","long_ctx","cheap","fast"]（见 §6.2）
  aliases   TEXT DEFAULT '[]',      -- JSON array: ["auto","best"]
  step      INTEGER DEFAULT 0,      -- 阶梯档位（0=不在阶梯）
  price_in  REAL,                   -- 每百万 token 输入价（USD）
  price_out REAL,                   -- 每百万 token 输出价（USD）
  ok        INTEGER DEFAULT 1,
  break_reason TEXT,
  enabled   INTEGER DEFAULT 1
);

CREATE TABLE tree (
  idx       INTEGER PRIMARY KEY,    -- 顺序
  parent    INTEGER,                -- 父分支 idx（NULL=顶层节点）
  name      TEXT NOT NULL,          -- 节点名称
  desc      TEXT,                   -- 说明
  cond      TEXT NOT NULL,          -- JSON: 条件表达式
  action    TEXT NOT NULL,          -- JSON: 去向（route/block/special + params）
  trial     TEXT,                   -- JSON: 比例试水 {ratio, to}
  enabled   INTEGER DEFAULT 1,
  updated_at TEXT,
  updated_by TEXT
);

CREATE TABLE tag_settings (
  tag       TEXT PRIMARY KEY,
  thresholds TEXT NOT NULL,          -- JSON: [0.20, 0.50, 0.80]
  keywords   TEXT                    -- JSON: 规则标签的关键词表
);

CREATE TABLE conf_settings (
  -- 把握度分档（单行，全局共用）
  id        INTEGER PRIMARY KEY DEFAULT 1,
  thresholds TEXT NOT NULL           -- JSON: [0.30, 0.60, 0.85]
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
  -- 预置键:
  --   session_sticky (1/0)
  --   session_idle (半小时，秒数)
  --   sticky_detect_feedback (0/1，沿用前是否仍打 user_feedback，见 §4.4)
  --   budget_protect (1/0)
  --   budget_water (0.8)
  --   budget_daily_limit_usd (10.0)
  --   budget_route (qwen3-8b)
  --   max_retries (2)
  --   batch_window_ms (5)
  --   retention_days (30)
  --   ladder ('qwen3-8b,qwen3-32b,claude-sonnet')
  --   tree_version (整数，每次改 tree 时 +1)
);

-- ── 运行记录（只写） ──
CREATE TABLE requests (
  id          TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,         -- ISO 8601
  proto       TEXT NOT NULL,        -- 'openai' | 'anthropic'
  model_asked TEXT,                 -- 客户端请求的 model
  hit_branch  TEXT,                 -- 命中分支，存 tree.idx 路径（如 "3.0"）；
                                     -- 显示名在控制台按语言渲染，不入库
  routed_to   TEXT,                 -- 实际去向（模型 id 或 'block'）
  session_id  TEXT,                 -- 会话标识
  session_tag TEXT,                 -- 'new' | 'sticky' | 'unlocked'
  outcome     TEXT,                  -- 'upstream' | 'fallback' | 'block' | 'error'
  status      INTEGER,
  answer_ms   REAL,                 -- 打标签耗时
  upstream_ms REAL,                 -- 上游耗时
  cost        REAL,                  -- 本次成本（USD）
  tree_version INTEGER              -- 判定当时的 tree_version（M2）；与当前不符
                                     -- = 分支归属可能已失效（新增节点会重编 idx）
);

CREATE TABLE tags (
  request_id  TEXT NOT NULL REFERENCES requests(id),
  tag         TEXT NOT NULL,         -- 语言无关键，如 'domain'（见 §6.3）
  value       TEXT NOT NULL,         -- 语言无关键：分类存枚举键（'computer_science'），
                                     -- 有序档位存索引（'2'）；禁止存中文/英文显示词
  confidence  REAL,                  -- 0-1（仅模型标签）
  is_rule     INTEGER DEFAULT 0,
  PRIMARY KEY (request_id, tag)
);

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,     -- 会话标识
  current_model TEXT,                -- 当前沿用模型
  last_active TEXT,                  -- 最近活跃时间
  turns       INTEGER DEFAULT 0,
  unlocks     INTEGER DEFAULT 0,
  last_msg    TEXT,                  -- 末条用户消息原文（供重复提问 / 相似度）
  last_vec    BLOB                   -- 末条消息的嵌入向量（仅 checkpoint 模式，可空）
);

-- ── 审计（只写，供撤销） ──
CREATE TABLE changes (
  ts     TEXT NOT NULL,
  actor  TEXT,
  tbl    TEXT NOT NULL,
  row    TEXT,
  before TEXT,                       -- JSON
  after  TEXT                         -- JSON
);

-- ── 影响预估（写，供复盘） ──
CREATE TABLE estimates (
  id     TEXT PRIMARY KEY,
  ts     TEXT NOT NULL,
  node   TEXT NOT NULL,              -- 节点 idx 路径（如 "3"）
  params TEXT NOT NULL,              -- JSON: 入树的节点快照（条件 + 去向）
  est    TEXT NOT NULL,              -- JSON: 预估 {daily, ratio, from_fallback, from_other, cost_delta}
  actual TEXT                        -- JSON: 复盘回填 {daily, cost_delta, reviewed_at}；NULL = 未复盘
);

-- ── 索引（spec §3.2） ──
CREATE INDEX idx_tags_rid ON tags(request_id);
CREATE INDEX idx_req_ts ON requests(ts DESC);
CREATE INDEX idx_req_session ON requests(session_id);
CREATE INDEX idx_tree_order ON tree(parent, idx);
"""

# balanced 种子树定义在 sr/engine.py 的 TEMPLATES["balanced"]（Task 5 自本文件迁出，
# 内容零改动；_seed 落库与模板切换共用）。

# 种子供应商/模型（本任务计划授权的补种，Task 6/3 依赖）。
# base_url 为本地占位；部署时经控制台或 YAML 导入覆盖。caps 表意见 spec §6.2，
# 阶梯档位见设计方案 §3.5：qwen3-8b（便宜·快）→ qwen3-32b（中等）→ claude-sonnet（贵·慢）。
_SEED_PROVIDERS = [
    # (name, proto, base_url, key_env)
    ("local", "openai", "http://127.0.0.1:8123", "LOCAL_OPENAI_API_KEY"),
    ("anthropic", "anthropic", "http://127.0.0.1:8123", "ANTHROPIC_API_KEY"),
]

_SEED_MODELS = [
    # (id, provider, upstream, caps, step)；proto 跟随 provider
    ("qwen3-8b", "local", "qwen3-8b",
     ["text", "long_ctx", "cheap", "fast"], 1),
    ("qwen3-32b", "local", "qwen3-32b",
     ["text", "long_ctx", "medium", "medium_speed"], 2),
    ("claude-sonnet", "anthropic", "claude-sonnet",
     ["text", "long_ctx", "expensive", "slow"], 3),
    # 种子树去向引用的两个多媒体模型（设计方案 §3.6 模型卡清单）：
    # llava-omni「文字＋图片 · 中等 · 中速」、sdxl-image「只画图 · 便宜 · 慢」。
    # 不在阶梯：step = 0（spec §3.1 schema 注释「0 = 不在阶梯」，与列 DEFAULT 0 一致；
    # 旧种子写 None 与注释不统一，backlog 修）。价格未定价（price NULL）。
    ("llava-omni", "local", "llava-omni",
     ["text", "image", "medium", "medium_speed"], 0),
    ("sdxl-image", "local", "sdxl-image",
     ["image", "cheap", "slow"], 0),
]

_SEED_TAG_SETTINGS = [
    # (tag, thresholds)：context 词数四档切点（补种要求）
    ("context", [20, 100, 400]),
]

_SEED_CONF_SETTINGS = [0.30, 0.60, 0.85]  # 把握度切点（单行，id=1）

# 建库后必须齐全的表（11 张，自 SCHEMA 提取——避免两处各写一份名单漂移）。
# backlog：旧幂等探测只认 settings 表，指向「有 settings、缺其余表」的残缺/外来库时
# 会误判为已初始化 → 后续查询裸 OperationalError。
_REQUIRED_TABLES: frozenset[str] = frozenset(
    re.findall(r"CREATE TABLE\s+(\w+)", SCHEMA))


def _now() -> str:
    """ISO 8601 本地时区时间戳（requests.ts/changes.ts 同款格式）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def get(path: str) -> sqlite3.Connection:
    """打开连接：WAL + Row 工厂。调用方负责 commit/close。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    """库内表名集合（sqlite_master）。"""
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def is_initialized(path: str) -> bool:
    """库是否已完成 :func:`init_db` 建表（11 张表齐全）。不建文件、不建表。

    backlog：``sr export`` / ``sr import`` 对未初始化库曾先静默建空文件、再在
    config 层裸 OperationalError——CLI 侧先用本函数判定，给出干净退出码。
    非 SQLite 文件 / 无法打开一律按「未初始化」处理（不抛）。
    """
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _REQUIRED_TABLES <= _existing_tables(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def init_db(path: str) -> sqlite3.Connection:
    """建 11 表 + 索引 + 默认 settings + 默认模板 balanced（含补种供应商/模型/分档）。

    幂等：库内 11 张表齐全时原样返回连接，不重放种子。残缺库（部分表存在，
    如外来库恰好有 settings）不静默续用——抛 RuntimeError 明确指出缺表，
    避免后续查询在别处裸 OperationalError（backlog）。
    """
    conn = get(path)
    have = _existing_tables(conn)
    missing = _REQUIRED_TABLES - have
    if not missing:
        _migrate(conn)                     # 已建库：补新增列（幂等）
        return conn
    if have & _REQUIRED_TABLES:            # 部分表在 → 库不完整，不冒然补建
        conn.close()
        raise RuntimeError(
            f"库 {path} 不完整（缺表 {sorted(missing)}），非本程序建库？"
            "请另择路径或先修复/删除后再 init")
    conn.executescript(SCHEMA)
    _seed(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量 schema 迁移：给既有库补新增列（PRAGMA 探测 + ALTER，幂等）。

    M2：requests.tree_version（§3.1）是后加的列——旧库 CREATE TABLE 时没有它，
    必须 ALTER 补上，否则「记录当时 tree_version」的写侧 INSERT 会因缺列失败。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
    if cols and "tree_version" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN tree_version INTEGER")
        conn.commit()


def _seed(conn: sqlite3.Connection) -> None:
    """写入默认 settings、供应商/模型补种、分档补种与 balanced 种子树。"""
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        sorted(DEFAULT_SETTINGS.items()),
    )
    conn.executemany(
        "INSERT INTO providers (name, proto, base_url, key_env) VALUES (?, ?, ?, ?)",
        _SEED_PROVIDERS,
    )
    conn.executemany(
        "INSERT INTO models (id, provider, proto, upstream, caps, step) "
        "VALUES (?, ?, (SELECT proto FROM providers WHERE name = ?), ?, ?, ?)",
        [(mid, prov, prov, up, _dump(caps), step) for mid, prov, up, caps, step in _SEED_MODELS],
    )
    conn.executemany(
        "INSERT INTO tag_settings (tag, thresholds) VALUES (?, ?)",
        [(tag, _dump(th)) for tag, th in _SEED_TAG_SETTINGS],
    )
    conn.execute(
        "INSERT INTO conf_settings (id, thresholds) VALUES (1, ?)",
        (_dump(_SEED_CONF_SETTINGS),),
    )
    for node in TEMPLATES["balanced"]:
        # 节点行：cond/action 为 NOT NULL 占位（求值只读分支行）
        conn.execute(
            "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, enabled) "
            "VALUES (?, NULL, ?, ?, '[]', 'null', NULL, 1)",
            (node["idx"], node["name"], node.get("desc")),
        )
        for branch in node.get("branches", []):
            _seed_branch(conn, node["idx"], branch)


def _seed_branch(conn: sqlite3.Connection, parent: int, branch: dict) -> None:
    """逐分支行 INSERT；分支行 name 存空串（显示名由控制台按 cond 渲染）。"""
    conn.execute(
        "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, enabled) "
        "VALUES (?, ?, '', NULL, ?, ?, NULL, 1)",
        (branch["idx"], parent, _dump(branch["cond"]), _dump(branch["action"])),
    )
    for kid in branch.get("branches", []):
        _seed_branch(conn, branch["idx"], kid)


def model_ids(conn: sqlite3.Connection) -> dict[str, dict]:
    """models 表 → ``{model_id: {}}``（run_checkup 按 id 集合语义消费，简报测试
    传 ``{"qwen3-8b": {}, …}`` 即此口径）。A1：自 sr/server.py 迁入（config.
    import_yaml 与 server 端点共用，无需反向 import server）。"""
    return {r["id"]: {} for r in conn.execute("SELECT id FROM models")}


def get_tree_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'tree_version'").fetchone()
    return int(row[0])


def bump_tree_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'tree_version'")


def log_change(conn: sqlite3.Connection, actor, tbl, row, before, after) -> None:
    """写 changes 审计表；before/after 为可 JSON 序列化对象或 None。"""
    conn.execute(
        "INSERT INTO changes (ts, actor, tbl, row, before, after) VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), actor, tbl, row,
         None if before is None else _dump(before),
         None if after is None else _dump(after)))
