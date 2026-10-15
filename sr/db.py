"""SQLite 读写层：建表（spec §3.1/§3.2 逐字）、默认配置与 balanced 种子树、版本号、审计。

约定：
- 所有入库字符串一律语言无关键（spec §3.1 存储约定）；节点 name/desc 与 block_msg
  属用户可见文案，按 §9.2/§6.5 原样存双语/显示词。
- ``get_tree_version`` / ``bump_tree_version`` / ``log_change`` 不自行 commit，
  事务由调用方控制（便于多写操作原子提交）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

# 预置键（spec §3.1 settings 表注释），值一律字符串。
DEFAULT_SETTINGS: dict[str, str] = {
    "session_sticky": "1",
    "session_idle": "1800",           # 半小时
    "sticky_detect_feedback": "0",
    "budget_protect": "1",
    "budget_water": "0.8",
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
  cost        REAL                   -- 本次成本（USD）
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

# balanced 种子树（spec §5.3 所指「设计方案 §3.2 的 8 节点树」）。
# cond/action 用规范键，语法见 spec §5.1（op ∈ is/in/gte/lte/contains；or/not 只包普通子句；
# cond:[] 兜底仅出现在最后一个节点 idx=9）。
# 源：console-v7.html `const TREE` 的 8 节点，中文显示词按 §6.3 键表换成规范键；
# 难度按 spec §2.2 两档收敛：`至少 较难/很难` 统一落 {"op":"is","values":[1]}（hard）。
# idx 编号：顶层节点 = 设计方案节点号；分支 = 父 idx*10 + 段内序号（§9.2 示例：节点 3 分支 0 → 30），
# 节点 0（起点）的 0 段与顶层节点号冲突，特例落 100+序号；子分支同公式（21 → 210/211）。
# Task 5 落地后将本常量收回 sr/engine.py 的 TEMPLATES（config.apply_template 调用），此处删除。
_BLOCK_MSG = {"zh": "请求被安全策略拦截", "en": "Blocked by safety policy"}

BALANCED_YAML: list[dict] = [
    {"idx": 0, "name": "会话沿用",
     "desc": "同一个会话里，沿用第一次判断的结果（省算力，也省模型切换的开销）",
     "branches": [
         # 上轮反馈 是 满意 / 想让我说清楚 → 沿用上次的模型（多值 → in）
         {"idx": 100,
          "cond": [{"tag": "user_feedback", "op": "in",
                    "values": ["satisfied", "need_clarification"]}],
          "action": {"kind": "special", "special": "sticky"}},
         # 上轮反馈 是 答错了 → 升一档
         {"idx": 101,
          "cond": [{"tag": "user_feedback", "op": "is", "values": ["wrong_answer"]}],
          "action": {"kind": "special", "special": "ladder_up"}},
         # 上轮反馈 是 想换个说法 → 换一个模型
         {"idx": 102,
          "cond": [{"tag": "user_feedback", "op": "is", "values": ["want_different"]}],
          "action": {"kind": "special", "special": "ladder_next"}},
     ]},
    {"idx": 1, "name": "安全", "desc": "先拦危险请求",
     "branches": [
         # 可疑 至少 很可疑（§6.3 档位索引 2）
         {"idx": 10,
          "cond": [{"tag": "jailbreak", "op": "gte", "values": [2]}],
          "action": {"kind": "block", "block_msg": _BLOCK_MSG}},
         # 敏感信息 含有 身份证号 / 银行卡号 / 密码
         {"idx": 11,
          "cond": [{"tag": "pii", "op": "contains",
                    "values": ["cn_id_card", "cn_bank_card", "password"]}],
          "action": {"kind": "block", "block_msg": _BLOCK_MSG}},
     ]},
    {"idx": 2, "name": "多媒体", "desc": "要生成图片的请求（位于成本控制之前）",
     "branches": [
         # 要什么 是 图片
         {"idx": 20,
          "cond": [{"tag": "modality", "op": "is", "values": ["image"]}],
          "action": {"kind": "route", "model": "sdxl-image"}},
         # 要什么 是 文字加图片 →（往下细看）；本行动作为分组层兜底，
         # 两个子分支恒命中其一（末位为「其他」），正常求值不可达。
         {"idx": 21,
          "cond": [{"tag": "modality", "op": "is", "values": ["text_image"]}],
          "action": {"kind": "route", "model": "llava-omni"},
          "branches": [
              # 难度 至少 较难 → llava-omni（深度）；两档制落 is [1]
              {"idx": 210,
               "cond": [{"tag": "complexity", "op": "is", "values": [1]}],
               "action": {"kind": "route", "model": "llava-omni", "think": "deep"}},
              # 其他 → llava-omni（cond:[] 仅限最后节点，此处用 not 表达「其他」）
              {"idx": 211,
               "cond": [{"not": {"tag": "complexity", "op": "is", "values": [1]}}],
               "action": {"kind": "route", "model": "llava-omni"}},
          ]},
     ]},
    {"idx": 3, "name": "编程与推理", "desc": "按难度换模型",
     "branches": [
         # 学科 是 计算机 而且 难度 至少 较难（把握度 至少 比较确定 → conf_min 并入该子句）
         {"idx": 30,
          "cond": [{"tag": "domain", "op": "is", "values": ["computer_science"]},
                   {"tag": "complexity", "op": "is", "values": [1], "conf_min": 2}],
          "action": {"kind": "route", "model": "qwen3-32b", "think": "deep",
                     "fallback": ["qwen3-8b"]}},
         # 难度 至少 很难 → 深度
         {"idx": 31,
          "cond": [{"tag": "complexity", "op": "is", "values": [1]}],
          "action": {"kind": "route", "model": "qwen3-32b", "think": "deep"}},
         # 难度 至少 较难 → 轻度
         {"idx": 32,
          "cond": [{"tag": "complexity", "op": "is", "values": [1]}],
          "action": {"kind": "route", "model": "qwen3-32b", "think": "light"}},
     ]},
    {"idx": 4, "name": "法律与事实", "desc": "需要严肃对待的问题",
     "branches": [
         # 学科 是 法律 或者 需核实 是 需要核实
         {"idx": 40,
          "cond": [{"or": [{"tag": "domain", "op": "is", "values": ["law"]},
                           {"tag": "fact_check", "op": "is",
                            "values": ["needs_check"]}]}],
          "action": {"kind": "route", "model": "qwen3-32b", "think": "light",
                     "fallback": ["qwen3-8b"]}},
     ]},
    {"idx": 5, "name": "成本控制", "desc": "简单请求走小模型",
     "branches": [
         # 难度 是 简单 而且 长度 属于 很短、一般（context 档位 0/1）
         {"idx": 50,
          "cond": [{"tag": "complexity", "op": "is", "values": [0]},
                   {"tag": "context", "op": "in", "values": [0, 1]}],
          "action": {"kind": "route", "model": "qwen3-8b"}},
         # 轮次 是 长对话（档位 2）
         {"idx": 51,
          "cond": [{"tag": "turns", "op": "is", "values": [2]}],
          "action": {"kind": "route", "model": "qwen3-8b"}},
         # 时段 是 夜间
         {"idx": 52,
          "cond": [{"tag": "time_window", "op": "is", "values": ["night"]}],
          "action": {"kind": "route", "model": "qwen3-8b"}},
     ]},
    {"idx": 6, "name": "专属", "desc": "指定人群走指定模型",
     "branches": [
         # 身份 是 premium
         {"idx": 60,
          "cond": [{"tag": "tenant", "op": "is", "values": ["premium"]}],
          "action": {"kind": "route", "model": "qwen3-32b"}},
     ]},
    {"idx": 9, "name": "其他（兜底）", "desc": "全部未命中时",
     "branches": [
         # 什么情况都算（cond:[] 兜底，仅最后节点）
         {"idx": 90,
          "cond": [],
          "action": {"kind": "route", "model": "qwen3-8b"}},
     ]},
]

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
    # 不在阶梯（step NULL，阶梯读 settings.ladder），价格未定价（price NULL）。
    ("llava-omni", "local", "llava-omni",
     ["text", "image", "medium", "medium_speed"], None),
    ("sdxl-image", "local", "sdxl-image",
     ["image", "cheap", "slow"], None),
]

_SEED_TAG_SETTINGS = [
    # (tag, thresholds)：context 词数四档切点（补种要求）
    ("context", [20, 100, 400]),
]

_SEED_CONF_SETTINGS = [0.30, 0.60, 0.85]  # 把握度切点（单行，id=1）


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


def init_db(path: str) -> sqlite3.Connection:
    """建十表 + 索引 + 默认 settings + 默认模板 balanced（含补种供应商/模型/分档）。

    幂等：库已初始化（settings 表存在）时原样返回连接，不重放种子。
    """
    conn = get(path)
    initialized = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    if not initialized:
        conn.executescript(SCHEMA)
        _seed(conn)
        conn.commit()
    return conn


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
    for node in BALANCED_YAML:
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
