"""分流树引擎：cond 求值 + first-hit-wins evaluate + 试算轨迹 + 四模板（spec §5.1–5.3）
+ 会话沿用（spec §4.4：extract_session / resolve_session）。

模块职责（全项目的心脏）：
- :data:`RouteDecision` —— spec §5.1 末定义逐字（kind/model/source/think/fallbacks/
  block_msg/hit_path，可空字段默认 None）。
- :func:`match` / :func:`_atom` —— §5.1 条件语法：子句列表默认 AND；``or`` / ``not``
  只包普通子句不嵌套；``conf_min`` 把握度门槛走 :func:`sr.tags.conf_band`
  （规则标签 confidence=None 永不满足任何门槛）。**形状防御**（收尾微修）：
  ``or`` 体非 list、``not`` 体非 dict、子句本身非 dict 一律整体 False 且不抛
  ——体检是主闸（PUT /admin/tree 422），历史库/绕闸数据不得让 /v1 持久 500。
- :func:`_walk` —— evaluate / evaluate_trace 的公共求值内核（Task 6 折入项 c）：
  按 §5.2 同一循环顺序产出 skip/miss/hit/also 事件，两个消费端零重复。
- :func:`evaluate` —— §5.2 求值：首个 hit 事件 resolve（first-hit-wins）；special
  （会话沿用）分支恒视为 miss——沿用/解锁在进树前由 resolve_session 处理（§4.4）；
  兜底节点 cond=[] 由 match 恒真自然兜底；全 miss（无兜底的坏树，体检硬错误 §10
  拦截保存，正常不可达）返回防御性决定，不抛异常。
- :func:`evaluate_trace` —— 同一内核的试算版（§5.2 试算形状）：逐节点轨迹 path +
  also_match（命中后被上方抢先的分支）+ decision，`POST /admin/tree/try` 的数据来源；
  全 miss 时 decision 复用 evaluate 的同一防御决定（Task 6 折入项 a），
  ``{path, also_match, decision}`` 形状恒成立。
- :func:`extract_session` —— §4.4 会话标识三级：x-session-id 头 → OpenAI ``user`` /
  Anthropic ``metadata.user_id`` → sha1 退化指纹（仅 session_sticky 开启时启用，
  R1 风险见 §4.4）。
- :func:`resolve_session` —— §4.4 会话沿用判定：会话不存在 / idle 过期 / 阶梯无处
  可升 → None 继续走树；无解锁反馈直接沿用（source='sticky'）；wrong_answer /
  want_different 反馈解锁升一档或换一个（source='unlocked'，unlocks+1）。
- :func:`rows_to_tree` —— tree 表行（parent IS NULL = 节点行，其余 = 分支行）→ 内存树。
- :data:`TEMPLATES` —— §5.3 四模板 balanced/saver/quality/compliance；值为可被
  sr.db 落库的树定义（节点 dict 列表，与 init_db 种子同构）。

依赖方向：engine → tags（conf_band / PII_KEYS）、engine → models（ladder_up /
ladder_next，§4.4 解锁升档用）；engine 不 import db——db 单向 import 本模块的
TEMPLATES，models 不 import 工程内模块，均无循环依赖。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sr.models import ladder_next, ladder_up
from sr.tags import PII_KEYS, conf_band

# ─────────────────────────────────────────────────────────────────────────────
# 1) RouteDecision + Node/Branch —— spec §5.1 末 dataclass 逐字（字段序一致）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RouteDecision:
    """一次求值的去向决定（spec §5.1 末定义逐字）。"""

    kind: str                                # 'route' | 'block'
    model: str | None = None                 # 最终模型 id（block 时为 None）
    source: str = "tree"                     # spec §5.1 枚举：'tree' | 'sticky' |
                                             # 'unlocked' | 'trial' | 'direct' |
                                             # 'fallback'；树命中 'tree'，试水掷签
                                             # 后 'trial'，会话沿用/解锁由
                                             # resolve_session 产 'sticky'/'unlocked'
    think: str | None = None                 # 'off' | 'light' | 'deep'（§5.1 透传上游）
    fallbacks: list[str] = field(default_factory=list)   # 回退链（可能为空，§5.1 fallback）
    block_msg: dict | None = None            # {"zh":…,"en":…}（§6.5 双语）
    hit_path: list[int] = field(default_factory=list)    # 命中路径 [节点idx, 节点内分支序号]


@dataclass
class Branch:
    """一条分支（tree 表分支行；cond/action/trial 见 spec §3.1/§5.1）。

    ``kids`` 为子分支（分支行的行 parent 指向另一分支行时归此）；本任务求值不
    下钻（控制者裁定），但 rows_to_tree 仍按 parent 如实挂载——balanced 分支 21
    实有子分支 210/211（db 种子注释同载），丢弃会静默丢行。
    """

    idx: int                    # tree.idx（编号约定：父 idx*10 + 段内序号，§9.2）
    cond: list                  # 条件表达式（JSON 已解析，语法 §5.1）
    action: dict                # 去向（JSON 已解析：route/block/special + params）
    trial: dict | None          # 比例试水 {ratio, to}（列 NULL → None）
    enabled: bool
    kids: list                  # 子分支 list[Branch]，idx 升序（本任务恒不下钻）


@dataclass
class Node:
    """一个顶层节点（tree 表节点行；§9.2 kind ∈ normal | sticky | fallback）。"""

    idx: int
    kind: str = "normal"        # normal | sticky(起点 idx 0) | fallback(兜底 cond[])
    name: str = ""
    desc: str | None = None
    enabled: bool = True
    branches: list = field(default_factory=list)   # list[Branch]，idx 升序


# ─────────────────────────────────────────────────────────────────────────────
# 2) TEMPLATES —— §5.3 四模板（值为可落库树定义；db._seed 取 TEMPLATES["balanced"]）
# ─────────────────────────────────────────────────────────────────────────────

# balanced 种子树（原 sr/db.py BALANCED_YAML 逐字迁入，内容零改动——迁移做了
# 「git HEAD 旧字面量 vs 本常量」的逐字段全等 + 规范化 JSON 字节比对，见任务报告）。
# cond/action 用规范键，语法见 spec §5.1（op ∈ is/in/gte/lte/contains；or/not 只包
# 普通子句；cond:[] 兜底仅出现在最后一个节点 idx=9）。
# 源：console-v7.html `const TREE` 的 8 节点，中文显示词按 §6.3 键表换成规范键；
# 难度按 spec §2.2 两档收敛：`至少 较难/很难` 统一落 {"op":"is","values":[1]}（hard）。
# idx 编号：顶层节点 = 设计方案节点号；分支 = 父 idx*10 + 段内序号（§9.2 示例：节点 3
# 分支 0 → 30），节点 0（起点）的 0 段与顶层节点号冲突，特例落 100+序号；
# 子分支同公式（21 → 210/211）。
_BLOCK_MSG = {"zh": "请求被安全策略拦截", "en": "Blocked by safety policy"}

_BALANCED: list[dict] = [
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


def _route_all_to(nodes: list[dict], model: str) -> list[dict]:
    """深拷贝并把全部 route 去向（含子分支）改为指定模型（saver：阶梯最低档）。

    只改 action.model——think / fallback 链 / special / block 原样保留
    （控制者裁定「所有分支去向阶梯最低档」仅指去向模型）。
    """

    def walk(branches: list[dict]) -> None:
        for branch in branches:
            action = branch.get("action")
            if isinstance(action, dict) and action.get("kind") == "route":
                action["model"] = model
            walk(branch.get("branches", []))

    out = copy.deepcopy(nodes)
    for node in out:
        walk(node.get("branches", []))
    return out


def _quality(nodes: list[dict]) -> list[dict]:
    """quality：仅 context 档 0 落最低档，其余不变（§5.3「默认高档，简单请求才降级」）。

    以 balanced 为底改两处（裁定声明，记任务报告）：
    - 成本控制（节点 5）三条降级支收窄为一条 context 档 0 → qwen3-8b
      （原 50 的 easy+短/中、51 长对话、52 夜间不再降档，否则「仅 context 档 0
      落最低档」不成立）；
    - 兜底（节点 9）去向升为 qwen3-32b——兜底是「默认」路径，若仍落 8b 则
      §5.3「默认高档」与主句同时被违反（夜间/长对话/未命中流量全绕回最低档）。
    """
    out = copy.deepcopy(nodes)
    for node in out:
        if node["idx"] == 5:
            node["branches"] = [
                {"idx": 50,
                 "cond": [{"tag": "context", "op": "is", "values": [0]}],
                 "action": {"kind": "route", "model": "qwen3-8b"}},
            ]
        elif node["idx"] == 9:
            node["branches"][0]["action"]["model"] = "qwen3-32b"
    return out


def _compliance(nodes: list[dict]) -> list[dict]:
    """compliance：可疑 ≥1 或 敏感信息非空 一律 block（§5.3「严格合规」）。

    以 balanced 为底改，仅放宽安全（节点 1）两条分支的条件，其余逐字不变：
    - jailbreak 门槛从档 2（很可疑）放宽到档 1（≥1 即拦）；
    - pii 从三型扩为值空间全集（tags.PII_KEYS 全 30 型）——contains 全集 ⇔ 非空
      （cond 语法无「非空」操作符，§5.1 op 枚举固定，全集 contains 是等价表达）。
    """
    out = copy.deepcopy(nodes)
    for node in out:
        if node["idx"] == 1:
            node["branches"][0]["cond"][0]["values"] = [1]
            node["branches"][1]["cond"][0]["values"] = list(PII_KEYS)
    return out


TEMPLATES: dict[str, list] = {
    "balanced": _BALANCED,     # 均衡：安全 → 多媒体 → 编程与推理 → 法律与事实 → 成本控制 → 专属 → 兜底
    "saver": _route_all_to(_BALANCED, "qwen3-8b"),   # 省钱优先：简单请求全部压到阶梯最低档
    "quality": _quality(_BALANCED),                  # 质量优先：默认高档，简单请求才降级
    "compliance": _compliance(_BALANCED),            # 严格合规：PII / 可疑一律拦下或走本地模型
}

# 模板切换（config.apply_template）时整体替换 tree 表并 tree_version += 1（§5.3）；
# 初始化种子取 TEMPLATES["balanced"]（sr/db.py 自 Task 5 起从本模块 import）。

# ─────────────────────────────────────────────────────────────────────────────
# 3) match / _atom —— cond 求值（简报 Step 3 参考实现逐字；规格 §5.1–5.2）
# ─────────────────────────────────────────────────────────────────────────────


def match(cond, signals, cuts):
    for clause in cond:                       # 默认 AND
        if not isinstance(clause, dict):
            return False                      # 非 dict 子句：无法求值 ⇒ False（不抛）
        if "or" in clause:
            body = clause["or"]
            if not isinstance(body, list):
                return False                  # or 体非数组：无法求值 ⇒ False（不抛）
            if not any(_atom(c, signals, cuts) for c in body):
                return False
        elif "not" in clause:
            body = clause["not"]
            if not isinstance(body, dict):
                return False                  # not 体非普通子句对象 ⇒ False（不抛）
            if _atom(body, signals, cuts):
                return False
        elif not _atom(clause, signals, cuts):
            return False
    return True


def _atom(c, signals, cuts):
    """单个普通子句求值；**任何非法形状一律 False，绝不抛异常**。

    I1（终审 Important）解析层兜底：体检（health.check_bad_ref）是主闸门，会把
    嵌套 or/not、op×kind 不匹配、非整数 conf_min 等在 PUT /admin/tree / import
    前 422 拦下；但历史库、旧包或任何绕闸路径仍可能让非法子句落库——此时若按
    §5.1 字面实现（values[0] / op 查表 / int 比较），每条 /v1 请求都会 500 且
    持久不复原。语义上「引擎无法求值的子句」按恒 False 处理（与未知标签
    ``sig is None`` 同口径），由体检/审计面板暴露问题，不在请求路径炸。

    M4（audit Major）类型守卫：op 调用一律裹 :func:`_guard`——`gte/lte` 的
    bound 非数值（``"2"`` / ``None``）会 `int >= str` TypeError，``contains``
    遇到非容器信号值（int）会 `x in 1` TypeError。两类都按「无法求值 ⇒ False」
    收口，与 can't-evaluate ⇒ False 同语义。
    """
    if not isinstance(c, dict):
        return False
    sig = signals.get(c.get("tag"))
    if sig is None:
        return False
    if not isinstance(sig, dict):                     # 非标准信号包：无法求值 ⇒ False
        return False
    v = sig.get("value")
    values = c.get("values")
    if not isinstance(values, list) or not values:
        return False

    def num(x) -> bool:
        """数值（bool 不算——Python 里它是 int 的子类）。"""
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def num_bound(x) -> bool:
        """**整数**档位 bound：§5.1 gte/lte 只服务 ordered 标签的档位索引（int）。"""
        return isinstance(x, int) and not isinstance(x, bool)

    ops = {
        "is":  lambda: v == values[0],
        "in":  lambda: v in values,
        "gte": lambda: num_bound(values[0]) and num(v) and v >= values[0],
        "lte": lambda: num_bound(values[0]) and num(v) and v <= values[0],
        "contains": lambda: isinstance(v, (list, tuple, set, str, dict))
        and any(x in v for x in values),
    }
    fn = ops.get(c.get("op"))
    if fn is None:
        return False
    try:
        if not fn():                                  # 无法求值 ⇒ False（不抛）
            return False
    except TypeError:
        return False
    if "conf_min" in c:                       # 把握度门槛：规则标签 confidence=None → 永不满足
        cm = c["conf_min"]
        cf = sig.get("confidence")
        if not isinstance(cm, int) or isinstance(cm, bool) or cf is None:
            return False
        try:
            band = conf_band(cf, cuts)
        except (TypeError, ValueError):       # 信号包 confidence 非数：防御性拒绝
            return False
        if band < cm:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 4) rows_to_tree —— tree 表行 → 内存树（spec §3.1 行结构 / §9.2 kind）
# ─────────────────────────────────────────────────────────────────────────────


def rows_to_tree(rows: list) -> list[Node]:
    """tree 表行 → 内存树（节点 idx 升序；节点内分支 idx 升序）。

    - parent IS NULL 为节点行；其余为分支行，按 parent 归属：父为节点 → 该节点
      branches；父为分支 → 该分支 kids（二级，语法限定节点嵌套 ≤ 3 层）。
    - 节点行 kind/name/desc/enabled 从行读——tree 表无 kind 列（§3.1），按行内容
      推导：idx 0 → sticky（§5.1 起点·会话沿用节点恒为 idx 0）；存在 cond=[] 分支
      → fallback（兜底，体检硬约束仅末节点）；其余 normal。
    - 分支行 cond/action JSON 解析；trial 列 NULL → None，否则 JSON 解析；
      分支行 name 存空串（显示名由控制台按 cond 渲染），不入 Branch。
    - 悬空 parent（父分支行不存在）抛 ValueError——§6.8「不静默兜底」。
    """
    node_rows = [r for r in rows if r["parent"] is None]
    node_rows.sort(key=lambda r: r["idx"])
    by_parent: dict[int, list] = {}
    for row in rows:
        if row["parent"] is not None:
            by_parent.setdefault(row["parent"], []).append(row)

    def make_branch(row) -> Branch:
        kids = by_parent.pop(row["idx"], [])
        return Branch(
            idx=row["idx"],
            cond=json.loads(row["cond"]),
            action=json.loads(row["action"]),
            trial=None if row["trial"] is None else json.loads(row["trial"]),
            enabled=bool(row["enabled"]),
            kids=[make_branch(k) for k in sorted(kids, key=lambda r: r["idx"])],
        )

    nodes: list[Node] = []
    for row in node_rows:
        branches = [make_branch(b)
                    for b in sorted(by_parent.pop(row["idx"], []),
                                    key=lambda r: r["idx"])]
        if row["idx"] == 0:
            kind = "sticky"
        elif any(b.cond == [] for b in branches):
            kind = "fallback"
        else:
            kind = "normal"
        nodes.append(Node(idx=row["idx"], kind=kind, name=row["name"],
                          desc=row["desc"], enabled=bool(row["enabled"]),
                          branches=branches))
    if by_parent:
        raise ValueError(f"tree 行存在悬空 parent: {sorted(by_parent)}")
    return nodes


# ─────────────────────────────────────────────────────────────────────────────
# 5) _walk + evaluate + resolve —— first-hit-wins（spec §5.2 伪码）
# ─────────────────────────────────────────────────────────────────────────────


def _walk(nodes: list[Node], signals: dict, conf_thresholds):
    """evaluate / evaluate_trace 的公共求值内核（Task 6 折入项 c：消两份循环）。

    按 §5.2 同一顺序（节点 idx 升序 × 分支 idx 升序）产出事件元组：
    - ``("skip", node)``                    停用节点；
    - ``("miss", node, reason)``            节点无命中（reason: new_session|no_match）；
    - ``("hit", node, i, action, trial)``   首个 enabled 且 match 的非 special 分支；
    - ``("also", node, i)``                 命中后余下节点里 match 的被抢先分支。

    special 分支（起点会话沿用）恒视为 miss——沿用/解锁在进树前由 resolve_session
    处理（§4.4 流程：先查会话，再走树），树内节点 0 仅供控制台展示语义。

    M4：action / trial 形状防御（:func:`_action_of`）——历史库或绕闸数据里的
    ``action: null`` / 非 dict 不得让 ``action.get`` AttributeError 逃出。
    """

    def branch_hit(branch) -> bool:
        return branch.enabled and match(branch.cond, signals, conf_thresholds)

    hit = False
    for node in nodes:
        if hit:                                     # 命中后只收集 also_match
            for i, branch in enumerate(node.branches):
                if branch_hit(branch):
                    yield ("also", node, i)
            continue
        if not node.enabled:
            yield ("skip", node)
            continue
        found = None
        for i, branch in enumerate(node.branches):
            if not branch_hit(branch):
                continue
            if _action_of(branch).get("kind") == "special":
                continue                            # §4.4：沿用/解锁在进树前判定
            found = (i, branch)
            break
        if found is None:
            yield ("miss", node,
                   "new_session" if node.kind == "sticky" else "no_match")
        else:
            i, branch = found
            yield ("hit", node, i, _action_of(branch), branch.trial)
            hit = True


def _defensive() -> RouteDecision:
    """全树未命中（无兜底的坏树，体检硬错误 §10 拦截保存，正常不可达）的防御决定。

    evaluate 与 evaluate_trace 全 miss 时共用本函数（Task 6 折入项 a：trace 的
    decision 不再为 None）；每次新建实例，避免调用方互相改到同一对象。
    """
    return RouteDecision(kind="route", model=None, source="fallback")


# 空 route action 的**模板**（只读）。所有使用点都 ``dict(_EMPTY_ACTION)`` 复制——
# C1 Minor 4：旧实现把本模块级可变 dict 直接返回给调用方，任何就地改（如
# `_action_of(b)["model"] = x`）都会污染之后所有「空 action」分支（跨请求串味）。
_EMPTY_ACTION: dict = {"kind": "route"}


def _action_of(branch) -> dict:
    """分支 action 防御取值：非 dict（null / 标量）→ 空 route action 的新副本（M4）。

    历史库或绕闸数据里的 ``action: null`` 在旧实现让 ``action.get(...)``
    AttributeError 逃出 evaluate（/v1 持久 500）；空 action 的 model 为 None，
    由 server 的「routing produced no model」防御路径收口（502 no_route），
    体检 bad_ref 仍是主闸。
    """
    action = branch.action
    return action if isinstance(action, dict) else dict(_EMPTY_ACTION)


def evaluate(nodes: list[Node], signals: dict, conf_thresholds) -> RouteDecision:
    """节点 idx 升序 × 分支 idx 升序，首个 enabled 且 match 的分支 resolve。

    - 遍历顺序与命中语义在 :func:`_walk`（与 evaluate_trace 共用，行为不变）。
    - 兜底节点（kind=fallback）分支 cond=[] 由 match 恒真自然命中，无需特判。
    - 全树未命中（无兜底的坏树）返回 :func:`_defensive` 的防御性决定，不抛异常。
    """
    for ev in _walk(nodes, signals, conf_thresholds):
        if ev[0] == "hit":
            _, node, i, action, trial = ev
            return _resolve(action, trial, [node.idx, i])
    return _defensive()


def _resolve(action: dict, trial: dict | None, hit_path: list[int]) -> RouteDecision:
    """分支 action + trial 列 → RouteDecision（§5.1 去向表；special 不经过本函数）。

    - trial 掷签 ``random.random() < ratio``（控制者裁定口径）命中 → 改道 trial.to、
      source='trial'，think/fallback 仍取原 action；trial 优先取分支列，缺省回退
      §5.1 示例中内嵌 action 的 trial 键。
    - block → kind='block' + block_msg（model/think 为 None，回退链为空）。
    - special 不进入本函数（_walk 按恒 miss 跳过；沿用/解锁的 RouteDecision 由
      resolve_session 直接构造，不经树 resolve）。

    M4（audit Major）形状防御——历史库/绕闸数据不得让本函数抛异常，也不得产出
    ``model=None`` 的「合法配置变永久 502」：
    - action 非 dict（null / 标量）→ 空 route action（model=None 由 server 收口）；
    - action.fallback 非 list（null / 标量）→ 视为 ``[]``（声明：不是「按契约 false
      跳过该分支」——分支已命中，去向模型仍应生效，只丢弃无法解释的回退链）；
    - trial 非 dict → 无试水；
    - trial.ratio 非数值（或不在 [0,1]）→ 不掷签（按正常去向，等价于未命中）；
    - trial.to 非字符串或引用不存在的模型 → 忽略试水（走原 model）——旧实现产出
      ``model=None, source='trial'``，server 当 502 no_route（体检 hard 拦保存为
      主闸，此处只保证绕闸数据下仍可用）。
    - 其余形状（未知模型等）如实透出：路由上游失败由 server 的错误信封负责，
      引擎不替体检做决定。
    """
    if not isinstance(action, dict):
        action = dict(_EMPTY_ACTION)            # C1 Minor 4：副本，不共享模块级 dict
    if trial is None:
        trial = action.get("trial")
    if action.get("kind") == "block":
        return RouteDecision(kind="block", model=None, source="tree", think=None,
                             fallbacks=[], block_msg=action.get("block_msg"),
                             hit_path=hit_path)
    raw_fallback = action.get("fallback")
    fallbacks = list(raw_fallback) if isinstance(raw_fallback, list) else []
    if isinstance(trial, dict) and _trial_hit(trial):
        to = trial.get("to")
        if isinstance(to, str) and to:
            return RouteDecision(kind="route", model=to, source="trial",
                                 think=action.get("think"), fallbacks=fallbacks,
                                 block_msg=None, hit_path=hit_path)
    return RouteDecision(kind="route", model=action.get("model"), source="tree",
                         think=action.get("think"), fallbacks=fallbacks,
                         block_msg=None, hit_path=hit_path)


def _trial_hit(trial: dict) -> bool:
    """试水掷签：``random.random() < ratio``；ratio 非数值或不在 [0,1] → 不掷签。

    M4：旧实现 ``random.random() < trial.get("ratio", 0)`` 对字符串/null ratio
    抛 TypeError（``'<' not supported between int and str``）。
    """
    ratio = trial.get("ratio", 0)
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return False
    if not 0 <= ratio <= 1:
        return False
    return random.random() < ratio


# ─────────────────────────────────────────────────────────────────────────────
# 6) evaluate_trace —— 试算轨迹 + also_match（§5.2 试算形状；与 evaluate 同一内核）
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_trace(nodes: list[Node], signals: dict, conf_thresholds) -> dict:
    """试算：逐节点轨迹 + also_match + decision（spec §5.2 试算形状）。

    返回 ``{"path": […], "also_match": […], "decision": …}``：
    - path 每节点一条 ``{"node": idx, "branch": i|null, "result": hit|miss|skip,
      "reason": …}``；命中节点记 ``{"node": idx, "branch": i, "result": "hit"}``
      （无需 reason）；起点节点（sticky）miss 记 reason="new_session"（沿用/解锁
      在进树前由 resolve_session 判定，§4.4）；其余 miss 记 "no_match"；停用节点记
      result="skip"、reason="node_disabled"。
    - also_match 收集命中后被上方抢先的分支（``{"node": idx, "branch": i}``）——
      「试算高亮 + 标灰被抢先分支」的数据来源。
    - decision 与 evaluate 同一 _resolve 路径（trial 掷签同语义）；全 miss 时取
      evaluate 同款防御决定（Task 6 折入项 a），形状恒成立。
    """
    path: list[dict] = []
    also_match: list[dict] = []
    decision: RouteDecision | None = None
    for ev in _walk(nodes, signals, conf_thresholds):
        if ev[0] == "skip":
            path.append({"node": ev[1].idx, "branch": None, "result": "skip",
                         "reason": "node_disabled"})
        elif ev[0] == "miss":
            path.append({"node": ev[1].idx, "branch": None, "result": "miss",
                         "reason": ev[2]})
        elif ev[0] == "hit":
            _, node, i, action, trial = ev
            decision = _resolve(action, trial, [node.idx, i])
            path.append({"node": node.idx, "branch": i, "result": "hit"})
        else:                                       # ("also", node, i)
            also_match.append({"node": ev[1].idx, "branch": ev[2]})
    if decision is None:
        decision = _defensive()                     # 真·全 miss → 与 evaluate 恒等
    return {"path": path, "also_match": also_match, "decision": decision}


# ─────────────────────────────────────────────────────────────────────────────
# 7) 会话沿用 —— extract_session 三级标识 + resolve_session 判定（spec §4.4）
# ─────────────────────────────────────────────────────────────────────────────

# 沿用语义的反馈（§4.4 is_unlocked 集合之外）与解锁语义的反馈：
_UNLOCK_FEEDBACK = {"wrong_answer", "want_different"}
# 接口裁定：裁定原文把「换个说法」写作 'rephrase'，§4.4 的规范键是 'want_different'，
# 两串都收（宽容别名，规范化归 §6.8 tags 层）。
_REPHRASE_FEEDBACK = {"want_different", "rephrase"}


def _header(headers: dict, name: str) -> str | None:
    """大小写不敏感取请求头；缺失返回 None（server 层的 headers 是朴素 dict）。"""
    name = name.lower()
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name:
            return v
    return None


def _first_user_text(body: dict) -> str:
    """首条 user 消息文本（指纹第 3 段）；非字符串内容（多模态分片等）JSON 序列化，
    保证同一请求恒得同一指纹；无 user 消息返回空串。"""
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return ""


def extract_session(proto: str, body: dict, headers: dict, client_ip: str,
                    sticky_enabled: bool = True) -> str | None:
    """会话标识三级，优先级从高到低（spec §4.4）：

    1. 请求头 ``x-session-id``（客户端显式给出，推荐）；
    2. OpenAI 兼容体的 ``user`` 字段 / Anthropic 体的 ``metadata.user_id``；
    3. 退化指纹 ``sha1(客户端 IP + Authorization 头 + 首条 user 消息文本)``（40 位
       小写 hex）——可能把不同会话误判为同一个（R1 风险，§4.4），故仅当
       session_sticky 开启时才启用。

    各级取到空值即落下一级；全空返回 None（无会话）。
    接口裁定：简报签名 4 参拿不到 settings，故加第 5 个带默认值形参
    ``sticky_enabled``（指纹门控），server 层（Task 8）传 settings 值；
    简报 6 个测试按 4 参调用全部成立（默认 True）。
    """
    sid = _header(headers, "x-session-id")
    if sid:
        return sid
    if proto == "anthropic":
        sid = (body.get("metadata") or {}).get("user_id")
    else:                       # openai 兼容体（未知 proto 按其超集形态处理）
        sid = body.get("user")
    if sid:
        return sid
    if not sticky_enabled:
        return None
    digest = hashlib.sha1(
        (client_ip + (_header(headers, "authorization") or "")
         + _first_user_text(body)).encode("utf-8"))
    return digest.hexdigest()


def _elapsed_s(ts: str) -> float:
    """sessions.last_active 距今的秒数。

    兼容两种写法：sqlite ``datetime('now')`` 的 UTC ``YYYY-MM-DD HH:MM:SS``
    （naive → 视作 UTC）与 ISO 8601 带时区（db._now 同款，控制台/导入可能混入）。
    解析失败返回 0（视为刚活跃：宁可多沿用，不因脏时间戳误杀会话）。
    """
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def resolve_session(conn, session_id: str | None, signals, settings, feedback=None):
    """会话沿用判定（spec §4.4 伪码逐行）→ RouteDecision；返回 None 则继续走树。

    - ``session_sticky != '1'`` 或无 session_id → None（§4.4 卫语句
      ``if session_id and settings.session_sticky``：整个沿用块被跳过；指纹级的
      单独门控在 extract_session 的 sticky_enabled 形参）。
    - 会话行不存在 → None。
    - idle 过期（last_active 距今 > ``session_idle`` 秒）→ None，视为新会话；
      不删会话行（保留 turns/unlocks 供统计，M1 从简忽略，下次同 id 命中会被
      UPDATE 覆盖——声明记任务报告）。
    - 反馈未传入 feedback=None（sticky_detect_feedback=0 时 server 层不传值，§4.4
      反馈来源优先级）或 satisfied / need_clarification → 直接沿用 current_model：
      source='sticky'，think=None、fallbacks=[]（M1 无分支记忆，沿用不复原上轮
      think/fallback 链——裁定声明，记任务报告）。
    - feedback='wrong_answer' → ``ladder_up or ladder_next``；'want_different'
      （含裁定别名 'rephrase'）→ ladder_next；解锁成功 unlocks+1，
      source='unlocked'（think/fallbacks 同上取空）。
    - 解锁但阶梯无处可升/可换（未配置或到顶回环失效，两函数均 None）→ 返回 None
      退化「重新走树」（§4.4 R3），不动 unlocks。
    - 沿用/解锁成功都更新 last_active（sqlite ``datetime('now')``，UTC
      ``YYYY-MM-DD HH:MM:SS``——与简报测试种子同款）与 turns+1。
    - signals 形参本任务不消费（树求值由调用方在 None 之后自做；保留形参位次与
      简报签名一致）。
    - 本函数不自行 commit（约定同 sr/db.py，事务由调用方控制）。
    """
    if not session_id or settings.get("session_sticky") != "1":
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    idle = int(settings.get("session_idle") or 1800)     # 默认半小时（§4.4）
    if row["last_active"] and _elapsed_s(row["last_active"]) > idle:
        return None                                      # 超阈值 → 新会话（§4.4）
    if feedback in _UNLOCK_FEEDBACK | _REPHRASE_FEEDBACK:
        # 反馈解锁：升一档或换一个（§4.4：ladder_up(row.current_model) or ladder_next）
        current = row["current_model"] or ""
        if feedback == "wrong_answer":
            new_model = ladder_up(conn, current) or ladder_next(conn, current)
        else:                    # want_different / rephrase → 换一个（ladder_next）
            new_model = ladder_next(conn, current)
        if new_model is None:
            return None          # 阶梯未配置/无处可去 → 重新走树（§4.4 R3），不动 unlocks
        conn.execute(
            "UPDATE sessions SET current_model = ?, last_active = datetime('now'), "
            "turns = turns + 1, unlocks = unlocks + 1 WHERE id = ?",
            (new_model, session_id))
        return RouteDecision(kind="route", model=new_model, source="unlocked",
                             think=None, fallbacks=[])
    # 沿用：feedback=None（sticky_detect_feedback=0 时 server 层不传值）/ satisfied /
    # need_clarification / 枚举外未知值（保守沿用，§4.4 is_unlocked 白名单外不解锁）。
    if not row["current_model"]:
        return None              # 无可沿用模型（§4.4：沿用要求 row.current_model）
    conn.execute(
        "UPDATE sessions SET last_active = datetime('now'), turns = turns + 1 "
        "WHERE id = ?", (session_id,))
    return RouteDecision(kind="route", model=row["current_model"], source="sticky",
                         think=None, fallbacks=[])
