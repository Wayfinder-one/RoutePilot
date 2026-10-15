"""分流树引擎：cond 求值 + first-hit-wins evaluate + 试算轨迹 + 四模板（spec §5.1–5.3）。

模块职责（全项目的心脏）：
- :data:`RouteDecision` —— spec §5.1 末定义逐字（kind/model/source/think/fallbacks/
  block_msg/hit_path，可空字段默认 None）。
- :func:`match` / :func:`_atom` —— §5.1 条件语法：子句列表默认 AND；``or`` / ``not``
  只包普通子句不嵌套；``conf_min`` 把握度门槛走 :func:`sr.tags.conf_band`
  （规则标签 confidence=None 永不满足任何门槛）。
- :func:`evaluate` —— §5.2 求值：节点 idx 升序 × 分支 idx 升序，首个 enabled 且
  match 的分支 resolve（first-hit-wins）；special（会话沿用）本任务直接 continue
  视为 miss（真沿用判定 Task 6）；兜底节点 cond=[] 由 match 恒真自然兜底。
- :func:`evaluate_trace` —— 同一循环的试算版（§5.2 试算形状）：逐节点轨迹 path +
  also_match（命中后被上方抢先的分支）+ decision，`POST /admin/tree/try` 的数据来源。
- :func:`rows_to_tree` —— tree 表行（parent IS NULL = 节点行，其余 = 分支行）→ 内存树。
- :data:`TEMPLATES` —— §5.3 四模板 balanced/saver/quality/compliance；值为可被
  sr.db 落库的树定义（节点 dict 列表，与 init_db 种子同构）。

依赖方向：engine → tags（conf_band / PII_KEYS）；engine 不 import db——db 单向
import 本模块的 TEMPLATES，无循环依赖。
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field

from sr.tags import PII_KEYS, conf_band

# ─────────────────────────────────────────────────────────────────────────────
# 1) RouteDecision + Node/Branch —— spec §5.1 末 dataclass 逐字（字段序一致）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RouteDecision:
    """一次求值的去向决定（spec §5.1 末定义逐字）。"""

    kind: str                                # 'route' | 'block'
    model: str | None = None                 # 最终模型 id（block 时为 None）
    source: str = "rule"                     # 树命中 'rule'（spec §5.1 枚举字面写作
                                             # 'tree'，按控制者裁定取 'rule'）；试水掷签
                                             # 后 'trial'；Task 6 另有 sticky/unlocked
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
        if "or" in clause:
            if not any(_atom(c, signals, cuts) for c in clause["or"]):
                return False
        elif "not" in clause:
            if _atom(clause["not"], signals, cuts):
                return False
        elif not _atom(clause, signals, cuts):
            return False
    return True


def _atom(c, signals, cuts):
    sig = signals.get(c["tag"])
    if sig is None:
        return False
    v = sig["value"]
    ok = {"is":  lambda: v == c["values"][0],
          "in":  lambda: v in c["values"],
          "gte": lambda: isinstance(v, int) and v >= c["values"][0],
          "lte": lambda: isinstance(v, int) and v <= c["values"][0],
          "contains": lambda: any(x in (v or []) for x in c["values"])}[c["op"]]()
    if not ok:
        return False
    if "conf_min" in c:                       # 把握度门槛：规则标签 confidence=None → 永不满足
        cf = sig.get("confidence")
        if cf is None or conf_band(cf, cuts) < c["conf_min"]:
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
# 5) evaluate + resolve —— first-hit-wins（spec §5.2 伪码；Task 6 前的 M1 形态）
# ─────────────────────────────────────────────────────────────────────────────


def evaluate(nodes: list[Node], signals: dict, conf_thresholds) -> RouteDecision:
    """节点 idx 升序 × 分支 idx 升序，首个 enabled 且 match 的分支 resolve。

    - special 分支（起点会话沿用）即使 match 也 continue 视为 miss——真沿用判定
      在 Task 6，本任务「新会话起点不适用」；节点 0 三分支全跳过后继续下一节点。
    - 兜底节点（kind=fallback）分支 cond=[] 由 match 恒真自然命中，无需特判。
    - 停用节点/分支跳过；全树未命中（无兜底的坏树，体检硬错误 §10 拦截保存，
      正常不可达）返回 model=None、source='fallback' 的防御性决定，不抛异常。
    """
    for node in nodes:                              # idx 升序：位置即优先级
        if not node.enabled:
            continue
        for i, branch in enumerate(node.branches):  # 同级亦按 idx
            if not branch.enabled or not match(branch.cond, signals, conf_thresholds):
                continue
            if branch.action.get("kind") == "special":
                continue                            # Task 6；新会话起点不适用 → miss
            return _resolve(branch.action, branch.trial, [node.idx, i])
    return RouteDecision(kind="route", model=None, source="fallback")


def _resolve(action: dict, trial: dict | None, hit_path: list[int]) -> RouteDecision:
    """分支 action + trial 列 → RouteDecision（§5.1 去向表；Task 6 前不含 special）。

    - trial 掷签 ``random.random() < ratio``（控制者裁定口径）命中 → 改道 trial.to、
      source='trial'，think/fallback 仍取原 action；trial 优先取分支列，缺省回退
      §5.1 示例中内嵌 action 的 trial 键。
    - block → kind='block' + block_msg（model/think 为 None，回退链为空）。
    - special 不进入本函数（调用方按 miss continue）。
    """
    if trial is None:
        trial = action.get("trial")
    if action.get("kind") == "block":
        return RouteDecision(kind="block", model=None, source="rule", think=None,
                             fallbacks=[], block_msg=action.get("block_msg"),
                             hit_path=hit_path)
    fallbacks = list(action.get("fallback", []))
    if trial and random.random() < trial.get("ratio", 0):
        return RouteDecision(kind="route", model=trial.get("to"), source="trial",
                             think=action.get("think"), fallbacks=fallbacks,
                             block_msg=None, hit_path=hit_path)
    return RouteDecision(kind="route", model=action.get("model"), source="rule",
                         think=action.get("think"), fallbacks=fallbacks,
                         block_msg=None, hit_path=hit_path)


# ─────────────────────────────────────────────────────────────────────────────
# 6) evaluate_trace —— 试算轨迹 + also_match（§5.2 试算形状；与 evaluate 同一循环）
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_trace(nodes: list[Node], signals: dict, conf_thresholds) -> dict:
    """试算：逐节点轨迹 + also_match + decision（spec §5.2 试算形状）。

    返回 ``{"path": […], "also_match": […], "decision": …}``：
    - path 每节点一条 ``{"node": idx, "branch": i|null, "result": hit|miss|skip,
      "reason": …}``；命中节点记 ``{"node": idx, "branch": i, "result": "hit"}``
      （无需 reason）；起点节点（sticky）miss 记 reason="new_session"（本任务语义：
      新会话起点不适用）；其余 miss 记 "no_match"；停用节点记 result="skip"、
      reason="node_disabled"。
    - 命中后继续扫余下节点收集 also_match（``{"node": idx, "branch": i}``，命中
      但被上方抢先的分支，不再 resolve）——「试算高亮 + 标灰被抢先分支」的数据来源。
    - decision 与 evaluate 同一 resolve 路径（trial 掷签同语义）。
    """
    path: list[dict] = []
    also_match: list[dict] = []
    decision: RouteDecision | None = None
    hit = False
    for node in nodes:
        if hit:                                     # 命中后只收集 also_match
            for i, branch in enumerate(node.branches):
                if branch.enabled and match(branch.cond, signals, conf_thresholds):
                    also_match.append({"node": node.idx, "branch": i})
            continue
        if not node.enabled:
            path.append({"node": node.idx, "branch": None, "result": "skip",
                         "reason": "node_disabled"})
            continue
        hit_at = None
        for i, branch in enumerate(node.branches):
            if not branch.enabled or not match(branch.cond, signals, conf_thresholds):
                continue
            if branch.action.get("kind") == "special":
                continue                            # Task 6；新会话起点不适用 → miss
            hit_at = i
            break
        if hit_at is None:
            path.append({"node": node.idx, "branch": None, "result": "miss",
                         "reason": "new_session" if node.kind == "sticky" else "no_match"})
        else:
            branch = node.branches[hit_at]
            decision = _resolve(branch.action, branch.trial, [node.idx, hit_at])
            path.append({"node": node.idx, "branch": hit_at, "result": "hit"})
            hit = True
    return {"path": path, "also_match": also_match, "decision": decision}
