"""sr/health.py —— 分流树体检：§10 八项检查 + Finding 形状（§9.2 audit 形状）。

模块职责：
- :class:`Finding` —— 一条体检结论（level/code/node/branch + 双语 message + fix）；
  :meth:`Finding.to_json` 按 §9.2 audit 形状直出（``message`` 为 ``{"zh":…,"en":…}``
  双语字典，控制台按当前语言取键渲染）。
- 八个 ``check_*`` —— §10 CHECKS 表逐项（函数名/硬错误标记逐字）。表内 True →
  ``hard``（拦截保存）；False 按「误配后果」分两级：``warn`` = 疑似配置错误
  （dead_branch / duplicate / conf_on_rule），``info`` = 仅提示（unused_tags /
  no_hits）。§9.2 只定义了 hard/warn/info 三级枚举，未逐项给非硬级，此划分为本
  实现裁定（记任务报告），与简报测试断言（duplicate/dead_branch 非 hard）一致。
- :data:`CHECKS` + :func:`run_checkup` —— §10 代码逐字（函数表 + is_hard 循环）。

接口裁定（记任务报告）：
- :func:`run_checkup` 增加可选第 4 参 ``hits``（hit_branch "N.M" → 近 30 天命中
  数，Task 5 裁定 M=分支位置序号）：check_no_hits 是八项里唯一需要运行数据的
  检查，§10 纯函数签名拿不到 DB——由 server 层查好传入；``hits=None``（无流量
  或库外直调）时该检查整体跳过。简报 3 参调用不受影响。
- 折入项（Task 5 评审遗留）：cond 子句 ``values`` 为空列表会让 engine._atom 取
  ``values[0]`` 抛 IndexError。§10 CHECKS 表没有独立的「子句语法」检查项，控制者
  裁定并入 check_bad_ref（hard）：空 values / 非法 op 与「引用不存在的标签/模型」
  同属「子句引用了引擎无法求值的东西」，在同一检查内处理（见 check_bad_ref 注释）。

依赖方向：health → tags（TagDef 只读）、health → engine（Node/Branch 结构），
均单向；server 单向 import 本模块，无循环依赖。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from sr.adapters import THINK_OPENAI
from sr.engine import Branch, Node
from sr.tags import TagDef

# 服务端日志（run_checkup_safe 的崩溃降级用；内核异常只进日志，不回吐客户端）
logger = logging.getLogger("sr.health")

# cond 子句合法操作符（§5.1 语法；引擎查表外的 op 会 KeyError → 体检拦下）。
_OPS = {"is", "in", "gte", "lte", "contains"}

# op 与标签 kind 的兼容表（I1(b)，终审 Important）：引擎 _atom 的 op 分支对不匹配
# 的 dtype 会崩（ordered 的 int 档位走 contains → `x in 1` 抛 TypeError；pii 的
# list 值走 gte → `list >= list` 抛 TypeError），旧闸门只查「op 在不在 _OPS」，
# 三类漏拦实测都可以 PUT 200 落库、之后 /v1 持久 500。表外的自由/动态词表标签
# （tenant/free、keyword/multi 空值空间）不静态校验——引擎对它们求值不崩。
# 说明：`is`/`in` 对任何 kind 都安全（== / in 不挑 dtype），故不入 _OP_KINDS。
_OP_KINDS = {
    "contains": {"multi"},            # 只有列表值标签（pii / keyword）
    "gte": {"ordered"},               # 只有档位索引标签（int 比较）
    "lte": {"ordered"},
}


def _op_kind_ok(op, spec) -> bool:
    """op 是否适用于该标签 kind（I1(b)）；未列出的 op 放行（见 _OP_KINDS 说明）。"""
    allow = _OP_KINDS.get(op)
    return allow is None or spec.kind in allow


# action.kind 值域（§5.1 去向表三种）与 special 值域（§5.1 三个语义）。
# C1 Important：kind 是去向的判别键——缺失或拼错时配置整体不可解释，必须 hard 拦。
_ACTION_KINDS = {"route", "block", "special"}
_SPECIALS = {"sticky", "ladder_up", "ladder_next"}


def _action_kind(b: Branch) -> str | None:
    """分支 action 的 kind；action 非 dict（历史库/绕闸 ``action: null``）→ None。

    结构类检查（dead_branch / no_hits / duplicate / special_misplaced）都用它取
    kind，别再用 ``b.action.get(...)``——C1 Minor 3：旧实现 check_dead_branch
    （CHECKS 表首项）先对非 dict action 调 ``.get`` 抛 AttributeError，逃出
    run_checkup 后被 run_checkup_safe 兜成通用 checkup_error，使 check_bad_ref 里
    「action 必须是对象」的**精确** finding 在 run_checkup 路径事实不可达。
    """
    act = b.action
    return act.get("kind") if isinstance(act, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# 1) Finding —— §9.2 audit 形状的内存态（message 双语拆两字段，序列化时合字典）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """一条体检结论（§9.2 audit 形状；字段序 = 简报 Produces 逐字）。"""

    level: str            # 'hard' | 'warn' | 'info'（§9.2 三级枚举）
    code: str             # §10 八项 code（check_* 去前缀，如 "no_fallback"）
    node: int | None      # 节点 idx；全树级结论为 None
    branch: int | None    # 分支 idx（tree.idx，§9.2 GET 同口径）；非分支级 None
    message_zh: str       # 中文结论（控制台 zh 态直出）
    message_en: str       # 英文结论（en 态直出）
    fix: dict | None = None   # 修复建议 {"action": …}（§9.2 示例形状）

    def to_json(self) -> dict:
        """→ §9.2 audit findings 逐项形状（message 双语字典直出）。"""
        return {"level": self.level, "code": self.code, "node": self.node,
                "branch": self.branch,
                "message": {"zh": self.message_zh, "en": self.message_en},
                "fix": self.fix}


# ─────────────────────────────────────────────────────────────────────────────
# 2) 公共小件：分支遍历 / cond 规范形
# ─────────────────────────────────────────────────────────────────────────────


def _iter_branches(nodes: list[Node]):
    """全部分支（含 kids 子分支）→ (node, branch)。

    M1 求值不下钻 kids（Task 5 裁定），但 kids 是落库行（rows_to_tree 如实挂载），
    体检不因「暂不求值」免检——结构类检查（bad_ref / special_misplaced /
    conf_on_rule / unused_tags 的引用收集）走本遍历。
    """
    for n in nodes:
        stack = list(n.branches)
        while stack:
            b = stack.pop(0)
            yield n, b
            stack.extend(b.kids)


def _atom_key(c: dict) -> tuple:
    """普通子句 → 可哈希规范形（dead_branch / duplicate 的「条件相同/被覆盖」判定）。"""
    values = c.get("values")
    if not isinstance(values, list):
        values = []
    return (c.get("tag"), c.get("op"),
            tuple(sorted(repr(v) for v in values)), c.get("conf_min"))


def _cond_key(cond: list) -> tuple:
    """cond 列表 → 规范形元组（§5.1 三种子句：普通 / or 组 / not 包装）。

    形状非法（``or`` 体非 list / ``not`` 体非 dict）时给保底可哈希形，绝不迭代
    非可迭代体——本检查在 CHECKS 表里先于 check_bad_ref 跑，旧实现会在这里
    TypeError 逃出体检（PUT /admin/tree 500）；非法形状的 hard finding 由
    check_bad_ref 报。"""
    out = []
    for c in cond:
        if not isinstance(c, dict):
            out.append(("?", repr(c)))          # 非法子句：保底可哈希（bad_ref 会另报）
        elif "or" in c:
            if not isinstance(c["or"], list):
                out.append(("or?", repr(c["or"])))      # 非法体：保底可哈希
                continue
            out.append(("or", frozenset(_atom_key(a) for a in c["or"]
                                        if isinstance(a, dict))))
        elif "not" in c:
            inner = c["not"]
            out.append(("not", _atom_key(inner) if isinstance(inner, dict)
                        else ("?", repr(inner))))
        else:
            out.append(_atom_key(c))
    return tuple(out)


def _covers(a_key: tuple, b_key: tuple) -> bool:
    """规范条件 A「覆盖」B：A 的每个子句在 B 中至少等量出现（多重集包含）。

    A 条件为空（兜底 cond=[]）⇒ Counter() ⊆ 任意 ⇒ 覆盖一切后序分支（§5.2
    first-hit-wins 语义下的正确推论）。"""
    ca, cb = Counter(a_key), Counter(b_key)
    return all(cb[k] >= n for k, n in ca.items())


# ─────────────────────────────────────────────────────────────────────────────
# 3) 八项检查（§10 CHECKS 表顺序 = 本节顺序；签名 (nodes, models, tag_defs, is_hard)）
# ─────────────────────────────────────────────────────────────────────────────


def check_dead_branch(nodes: list[Node], models, tag_defs, is_hard: bool) -> list:
    """分支被上方完全覆盖（§10）→ warn。

    「覆盖」的 M1 判定（声明记任务报告）：按求值顺序（节点 idx 升序 × 分支内
    列表序，§5.2）扫 enabled 的非 special 分支，前序分支 A 的规范条件 ⊆ 当前
    分支 B 的规范条件（:func:`_covers`，逐子句多重集包含）→ B 死支。不做完整
    逻辑蕴含证明（or/not 语义展开），同条件重复与「更宽条件在前」两类已覆盖，
    满足 §14.1「分支被上方完全覆盖」覆盖点；special 分支不参与——engine 视其
    恒 miss（§4.4），不遮蔽后序分支。"""
    level = "hard" if is_hard else "warn"
    findings: list[Finding] = []
    seen: list[tuple[tuple, Node, Branch]] = []
    for n in nodes:
        if not n.enabled:
            continue
        for b in n.branches:
            if not b.enabled or _action_kind(b) == "special":
                continue
            key = _cond_key(b.cond)
            for akey, an, ab in seen:
                if _covers(akey, key):
                    findings.append(Finding(
                        level, "dead_branch", n.idx, b.idx,
                        f"分支 {b.idx} 被上方分支 {an.idx}.{ab.idx} 完全覆盖，永远命中不到",
                        f"Branch {b.idx} is fully covered by branch {an.idx}.{ab.idx} "
                        f"above it and can never be reached",
                        {"action": "remove_branch"}))
                    break
            seen.append((key, n, b))
    return findings


def check_unused_tags(nodes: list[Node], models, tag_defs: dict[str, TagDef],
                      is_hard: bool) -> list:
    """标签从未被任何规则引用（§10）→ info（含 kids 子分支的引用收集）。"""
    referenced: set[str] = set()

    def walk(clauses: list) -> None:
        for c in clauses:
            if not isinstance(c, dict):
                continue
            if "or" in c:
                if isinstance(c["or"], list):       # 非 list 体：check_bad_ref 报 hard
                    walk(c["or"])
            elif "not" in c:
                walk([c["not"]])
            elif isinstance(c.get("tag"), str):
                referenced.add(c["tag"])

    for _, b in _iter_branches(nodes):
        walk(b.cond)
    return [Finding("info", "unused_tags", None, None,
                    f"标签「{tag_defs[t].zh}」（{t}）未被任何分支条件引用",
                    f"Tag '{t}' is not referenced by any branch condition",
                    {"action": "review_tag", "tag": t})
            for t in tag_defs if t not in referenced]


def check_no_hits(nodes: list[Node], models, tag_defs, is_hard: bool,
                  hits: dict[str, int] | None = None) -> list:
    """分支近 30 天无命中（§10）→ info。

    ``hits`` 由 server 层从 requests 表聚合（键 "N.M" = 节点 idx + 分支位置序号，
    Task 5 裁定口径）；None（窗口内无流量或库外直调）→ 整体跳过——无数据下
    「全部 0 命中」是噪声不是结论。special 分支同样跳过：沿用/解锁在进树前由
    resolve_session 判定（§4.4），hit_branch 永不记录它们。M1 求值不下钻，
    kids 分支无 hit 口径，不在本检查范围。"""
    if hits is None:
        return []
    level = "hard" if is_hard else "info"
    findings: list[Finding] = []
    for n in nodes:
        if not n.enabled:
            continue
        for i, b in enumerate(n.branches):
            if not b.enabled or _action_kind(b) == "special":
                continue
            if hits.get(f"{n.idx}.{i}", 0) == 0:
                findings.append(Finding(
                    level, "no_hits", n.idx, b.idx,
                    f"分支 {n.idx}.{i} 近 30 天无一次命中",
                    f"Branch {n.idx}.{i} has had no hits in the last 30 days",
                    {"action": "review_branch"}))
    return findings


def check_no_fallback(nodes: list[Node], models, tag_defs, is_hard: bool) -> list:
    """没有兜底节点（§10）← 硬错误。

    判定取双口径：kind=="fallback"（rows_to_tree 推导）或存在 cond=[] 分支
    （§5.1 兜底语法）——手组内存树（估算/导入中间态）可能缺 kind 标记。"""
    has_fallback = any(
        n.kind == "fallback" or any(b.cond == [] for b in n.branches)
        for n in nodes)
    if not has_fallback:
        return [Finding("hard" if is_hard else "warn", "no_fallback", None, None,
                        "没有兜底节点：全部未命中的请求将无路可走",
                        "No fallback node: requests that match nothing have "
                        "nowhere to go",
                        {"action": "add_fallback"})]
    return []


def check_bad_ref(nodes: list[Node], models, tag_defs: dict[str, TagDef],
                  is_hard: bool) -> list:
    """引用不存在的标签或模型（§10）← 硬错误。

    折入项（Task 5 评审，控制者裁定）：本检查同时承担「子句语法」守门——
    §10 CHECKS 表没有独立的子句语法检查项，空 ``values``（engine._atom 取
    values[0] 会 IndexError）与非法 ``op``（engine 查表 KeyError）并入此处，
    同为 hard。语义：子句引用了引擎无法求值的东西，与「引用不存在的标签/模型」
    同级。

    I1（终审 Important）再补三类「体检放行但 /v1 持久 500」的语法闸门，同为
    hard（PUT 200 落库后每条请求都崩，代价远高于一次 422）：

    (a) ``or`` / ``not`` 内层子句不得再含 ``or`` / ``not``（§5.1「or/not 只包
        普通子句不嵌套」；嵌套时 engine._atom 收到 ``{"or": …}`` 立刻 KeyError
        ``'tag'``）；
    (b) ``contains`` 仅列表值标签（multi）、``gte`` / ``lte`` 仅档位标签
        （ordered）；``is`` / ``in`` 放行（见 :data:`_OP_KINDS`，不匹配时
        engine 的 op 分支抛 TypeError）；
    (c) ``conf_min`` 必须是 int（bool 不算）——字符串会让 conf_band 比较抛
        ``'<' not supported between int and str``。

    收尾微修再补形状闸门（d）：``or`` 的体必须是 list、``not`` 的体必须是
    dict，否则 hard bad_ref——旧实现体检自身在 ``for sc in c["or"]``（乃至更早的
    ``_cond_key``）抛 TypeError 逃出 run_checkup，PUT /admin/tree 直接 500
    （库虽无写入，但错误码与语义都不对）；``{"or": [5]}`` 这类「臂非 dict」由
    :func:`atom` 的「条件子句不是对象」覆盖。

    **action 形状闸门（M3/M4/M5，audit Major）**：§10 CHECKS 表同样没有「action
    形状」检查项，三条写路径（PUT tree / POST nodes / import）都过本检查，故并入
    此处同族 hard bad_ref——它们对应的都是「PUT 200 落库 → 每条 /v1 持久 500 或
    永久 502」：

    (e) ``think`` 必须 ∈ ``THINK_OPENAI``（off|light|deep，None = 未指定），
        否则 ``to_upstream`` 抛 ValueError/TypeError（值域外 / 非字符串
        unhashable）逃逸成兜底 500；
    (f) ``fallback`` 键存在时必须是 list（``null`` **非法**：`*None` 让体检自己先倒，
        且回退链无法解释；键缺失 = 无回退链，合法），元素必须是模型 id 字符串；
        ``action`` 本身必须是对象；
    (g) ``trial``（分支列优先，缺省回 action.trial）必须是对象；``ratio`` 必须是
        [0,1] 内的数值；``to`` 必填且是模型 id——缺 ``to`` 时引擎产出
        ``model=None, source='trial'``，server 当 502 no_route（合法配置变
        永久 502）。

    以上形状检查与「引用不存在的模型」共用同一 hard bad_ref code（同族），
    控制台首屏直指拦截原因。

    **注意**：规则标签配 conf_min 仍归 :func:`check_conf_on_rule` 的 **warn**
    （§10 现状，I1 明确不连带改成 hard）——本函数只查 conf_min 的类型，不查
    标签 source。

    值空间校验：值不在标签值空间（如 domain 无 "astrology"）同报——引擎求值
    恒 False 的静默死支比报错更难排查（§6.8「不静默兜底」同精神）。有序档位
    标签按档位索引校验（0 ≤ v < 档数，§3.1 tags.value 存索引口径）；值空间为
    空元组的标签（free / 动态词表）不校验。
    """
    level = "hard" if is_hard else "warn"
    findings: list[Finding] = []

    def where(n: Node, b: Branch) -> str:
        return f"节点 {n.idx} 分支 {b.idx}"

    def bad(n: Node, b: Branch, zh: str, en: str) -> None:
        """条件子句类 finding：fix 指向「改条件」（fix_clause，既有枚举）。"""
        findings.append(Finding(level, "bad_ref", n.idx, b.idx, zh, en,
                                {"action": "fix_clause"}))

    def bad_action(n: Node, b: Branch, zh: str, en: str,
                   fix: str = "fix_action") -> None:
        """action 形状/值域类 finding：fix 指向「改行动作」，不是「改条件」。

        C1 Minor 2：旧实现复用 ``bad()`` → ``fix={"action":"fix_clause"}``，对
        think/fallback/trial 是误导性建议（控制台会引导去改条件）。分两档：
        - 形状/值域类（kind / block_msg / special / think / fallback 非 list /
          trial 形状 / ratio）→ ``fix_action``（既有枚举：非 dict action 也用它）；
        - 模型引用类（未知模型 / fallback 元素非 id / trial.to）→ ``fix_model``
          （调用点显式传 fix="fix_model"）。
        """
        findings.append(Finding(level, "bad_ref", n.idx, b.idx, zh, en,
                                {"action": fix}))

    def atom(c: dict, n: Node, b: Branch) -> None:
        """普通子句校验（or/not 内层调用，故内层再嵌 or/not 在此拦下，I1(a)）。"""
        if not isinstance(c, dict):
            bad(n, b, f"{where(n, b)}：条件子句不是对象：{c!r}",
                f"{where(n, b)}: condition clause is not an object: {c!r}")
            return
        if "or" in c or "not" in c:         # I1(a)：内层不得再嵌 or/not
            bad(n, b,
                f"{where(n, b)}：or/not 内层不得再嵌 or/not（§5.1 只包普通子句）",
                f"{where(n, b)}: or/not must not nest another or/not "
                f"(§5.1 wraps plain clauses only)")
            return
        tag = c.get("tag")
        if tag not in tag_defs:
            bad(n, b, f"{where(n, b)}：引用了不存在的标签 {tag!r}",
                f"{where(n, b)}: references unknown tag {tag!r}")
            return
        if c.get("op") not in _OPS:
            bad(n, b,
                f"{where(n, b)}：标签 {tag} 的操作符 {c.get('op')!r} 非法"
                f"（允许：{sorted(_OPS)}）",
                f"{where(n, b)}: invalid operator {c.get('op')!r} for tag {tag} "
                f"(allowed: {sorted(_OPS)})")
        elif not _op_kind_ok(c["op"], tag_defs[tag]):        # I1(b)
            bad(n, b,
                f"{where(n, b)}：标签 {tag}（{tag_defs[tag].kind}）不适用操作符 "
                f"{c['op']!r}——引擎求值会崩溃",
                f"{where(n, b)}: operator {c['op']!r} does not apply to tag {tag} "
                f"(kind {tag_defs[tag].kind}) — the engine would crash")
        values = c.get("values")
        if not isinstance(values, list) or not values:
            # 折入项：空 values → engine IndexError，hard 拦截（见函数 docstring）
            bad(n, b, f"{where(n, b)}：标签 {tag} 的 values 为空——引擎求值会崩溃",
                f"{where(n, b)}: empty values for tag {tag} — the engine would "
                f"crash evaluating it")
            return
        spec = tag_defs[tag]
        if not spec.values:
            return                            # free / 动态词表：不做静态校验（§4.1）
        for v in values:
            if spec.kind == "ordered":
                ok = isinstance(v, int) and not isinstance(v, bool) \
                    and 0 <= v < len(spec.values)
            else:
                ok = v in spec.values
            if not ok:
                bad(n, b, f"{where(n, b)}：值 {v!r} 不在标签 {tag} 的值空间内",
                    f"{where(n, b)}: value {v!r} is outside the value space of "
                    f"tag {tag}")
        if "conf_min" in c:                   # I1(c)：conf_min 必须 int（bool 不算）
            cm = c["conf_min"]
            if not isinstance(cm, int) or isinstance(cm, bool):
                bad(n, b,
                    f"{where(n, b)}：标签 {tag} 的 conf_min 必须是整数（当前 "
                    f"{cm!r}）——引擎比较会崩溃",
                    f"{where(n, b)}: conf_min for tag {tag} must be an integer "
                    f"(got {cm!r}) — the comparison would crash")

    def clause_errors(clauses, n: Node, b: Branch) -> None:
        for c in clauses:
            if not isinstance(c, dict):
                atom(c, n, b)
            elif "or" in c:
                body = c["or"]
                if not isinstance(body, list):      # 非法体：旧实现 for 迭代抛 TypeError
                    bad(n, b,
                        f"{where(n, b)}：or 的子句体必须是数组（当前 {body!r}）"
                        f"——引擎求值会崩溃",
                        f"{where(n, b)}: the 'or' body must be an array "
                        f"(got {body!r}) — the engine would crash")
                    continue
                for sc in body:                     # 臂非 dict 由 atom 报「不是对象」
                    atom(sc, n, b)
            elif "not" in c:
                body = c["not"]
                if not isinstance(body, dict):      # 非法体：not 只包单个普通子句
                    bad(n, b,
                        f"{where(n, b)}：not 的子句体必须是对象（当前 {body!r}）"
                        f"——引擎求值会崩溃",
                        f"{where(n, b)}: the 'not' body must be an object "
                        f"(got {body!r}) — the engine would crash")
                    continue
                atom(body, n, b)
            else:
                atom(c, n, b)

    for n, b in _iter_branches(nodes):
        clause_errors(b.cond, n, b)
        act = b.action
        if not isinstance(act, dict):              # 形状闸门（M4）：action 必须是对象
            bad_action(n, b,
                       f"{where(n, b)}：action 必须是对象（当前 {act!r}）"
                       f"——引擎求值会崩溃",
                       f"{where(n, b)}: action must be an object (got {act!r}) — "
                       f"the engine would crash evaluating it")
            continue
        # ── C1 Important：kind 闸门（§5.1 去向表的判别键）──────────────────────
        # 旧实现 `if act.get("kind") != "route": continue`：kind 缺失或拼错时**整体
        # 跳过** action 校验（payload 校验只要求 action 是对象）。实测 `action={}`
        # 与 `{"kind":"rout"}` → PUT 200、findings 空、落库成功，随后命中该分支的
        # /v1 恒 502 no_route（引擎 route 分支取不到 model → model=None）；而
        # `{"kind":"special"}` 无值则让分支恒 miss。三类都是「PUT 200 后永久
        # 502」，与 (e)(f)(g) 同族 → 同族 hard bad_ref。
        kind = act.get("kind")
        if kind not in _ACTION_KINDS:
            bad_action(n, b,
                       f"{where(n, b)}：action.kind 必须是 "
                       f"{sorted(_ACTION_KINDS)} 之一（当前 {kind!r}）"
                       f"——缺失或拼错会让这个去向无法解释，请求永久 502",
                       f"{where(n, b)}: action.kind must be one of "
                       f"{sorted(_ACTION_KINDS)} (got {kind!r}) — a missing or "
                       f"misspelled kind makes the action uninterpretable and "
                       f"the route would fail permanently")
            continue
        if kind == "special":
            # special 值域（§5.1 三个语义）。「只允许出现在起点节点」是既有的
            # check_special_misplaced，此处不重复报（简报明确提醒）。
            sp = act.get("special")
            if sp not in _SPECIALS:
                bad_action(n, b,
                           f"{where(n, b)}：special 必须是 "
                           f"{sorted(_SPECIALS)} 之一（当前 {sp!r}）"
                           f"——引擎无法执行该特殊去向",
                           f"{where(n, b)}: special must be one of "
                           f"{sorted(_SPECIALS)} (got {sp!r}) — the engine cannot "
                           f"perform this special action")
            continue
        if kind == "block":
            # block_msg 形状（§5.1/§6.5 双语对象）；server 的 block 分支对它做
            # ``msg_map.get(...)``，非对象（或双语缺一）会让 403 响应组装崩。
            msg = act.get("block_msg")
            if not (isinstance(msg, dict)
                    and all(isinstance(msg.get(k), str) and msg[k]
                            for k in ("zh", "en"))):
                bad_action(n, b,
                           f"{where(n, b)}：block_msg 必须是 {{\"zh\":…,\"en\":…}} "
                           f"双语对象（当前 {msg!r}）——拦截文案取不到会让响应崩溃",
                           f"{where(n, b)}: block_msg must be a bilingual object "
                           f"{{\"zh\":…,\"en\":…}} (got {msg!r}) — the block "
                           f"message cannot be rendered")
            continue
        # kind == "route"：以下沿用 M3/M4 的既有校验
        # ── M3：think 值域（off | light | deep；None = 未指定）───────────────
        # 值域外 / 非字符串 think 会在 to_upstream 抛 ValueError（值域外）或
        # TypeError（非字符串 → dict 查表 unhashable），_call_* 只捕
        # UpstreamError/httpx.HTTPError → 逃逸成兜底 500：PUT 200 落库后
        # **每条** /v1 持久 500。值域取自 adapters 的 THINK_OPENAI（唯一权威表，
        # 加新值只改一处）。
        think = act.get("think")
        if think is not None and (not isinstance(think, str)
                                  or think not in THINK_OPENAI):
            bad_action(n, b,
                       f"{where(n, b)}：think 值 {think!r} 非法"
                       f"（允许：{sorted(THINK_OPENAI)} 或未指定）"
                       f"——上游请求体会构造失败",
                       f"{where(n, b)}: invalid think value {think!r} "
                       f"(allowed: {sorted(THINK_OPENAI)} or unset) — building the "
                       f"upstream request body would fail")
        # ── M4/M5：fallback 形状 + 引用 ──────────────────────────────────────
        # 旧实现 refs = [model, *act.get("fallback", [])]：键在且值为 null 时默认值
        # 不生效，`*None` 抛 TypeError 逃出 run_checkup（PUT /admin/tree 500）——
        # 体检是给坏数据兜底的最后闸门，它自己不能先倒。
        # 口径（与 M4 的引擎防御**不同层**，报告声明）：体检侧显式写出的
        # ``fallback: null`` 是**非法形状**（键存在 = 明确给了回退链字段，null
        # 无法解释回退链），hard 拦截；键不存在的 route action（种子树/控制台
        # SRW_chipsToAction 就是这种形状）合法。引擎侧对 null 按 ``[]`` 防御——
        # 闸门拦得住，历史库也要能跑。
        # C1 Minor 1：refs 只收「形状合法的模型 id」——旧实现无条件
        # ``refs = [model, *fallback]`` / ``refs.append(to)``，对 fallback:[null]
        # 或缺失的 trial.to 除精确 finding 外还多报一条幻影「引用了不存在的模型
        # None」（误导控制台）。
        if "fallback" not in act:
            refs = [act.get("model")]
        else:
            fallback = act["fallback"]
            if not isinstance(fallback, list):
                bad_action(n, b,
                           f"{where(n, b)}：fallback 必须是数组（当前 {fallback!r}）"
                           f"——回退链无法解释（要清空回退链请省略该字段）",
                           f"{where(n, b)}: fallback must be an array "
                           f"(got {fallback!r}) — the fallback chain is "
                           f"uninterpretable (omit the field to clear the chain)")
                refs = [act.get("model")]
            else:
                refs = [act.get("model")]
                for m in fallback:
                    if not isinstance(m, str) or not m:
                        bad_action(n, b,
                                   f"{where(n, b)}：fallback 元素必须是模型 id "
                                   f"字符串（当前 {m!r}）",
                                   f"{where(n, b)}: every fallback item must be a "
                                   f"model id string (got {m!r})",
                                   fix="fix_model")
                    else:
                        refs.append(m)
        # ── M4：trial 形状（ratio 数值 ∈[0,1]、to 必填字符串）——缺 to 时引擎
        # 产出 model=None/source='trial'，server 当 502 no_route（合法配置变永久
        # 502）。
        trial = b.trial if b.trial is not None else act.get("trial")
        if trial is not None:
            if not isinstance(trial, dict):
                bad_action(n, b,
                           f"{where(n, b)}：trial 必须是对象或 null（当前 {trial!r}）",
                           f"{where(n, b)}: trial must be an object or null "
                           f"(got {trial!r})")
            else:
                ratio = trial.get("ratio", 0)
                if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) \
                        or not 0 <= ratio <= 1:
                    bad_action(n, b,
                               f"{where(n, b)}：trial.ratio 必须是 0–1 的数值"
                               f"（当前 {ratio!r}）——试水掷签会崩溃",
                               f"{where(n, b)}: trial.ratio must be a number in "
                               f"[0, 1] (got {ratio!r}) — the trial coin flip "
                               f"would crash")
                to = trial.get("to")
                if not isinstance(to, str) or not to:
                    bad_action(n, b,
                               f"{where(n, b)}：trial.to 必填且必须是模型 id"
                               f"（当前 {to!r}）——缺失时去向为空，请求会永久 502",
                               f"{where(n, b)}: trial.to is required and must be "
                               f"a model id (got {to!r}) — without it the request "
                               f"has no route and would fail permanently",
                               fix="fix_model")
                else:
                    refs.append(to)
        for m in refs:
            if m not in models:
                bad_action(n, b,
                           f"{where(n, b)}：引用了不存在的模型 {m!r}",
                           f"{where(n, b)}: references unknown model {m!r}",
                           fix="fix_model")
    return findings


def check_duplicate(nodes: list[Node], models, tag_defs, is_hard: bool) -> list:
    """两条规则条件完全相同（§10）→ warn。

    范围与 dead_branch 相同（enabled 非 special、按求值顺序、含跨节点同条件）；
    规范形全等（:func:`_cond_key`）即报，后者为重复（前者先生效）。"""
    level = "hard" if is_hard else "warn"
    findings: list[Finding] = []
    first: dict[tuple, tuple[Node, Branch]] = {}
    for n in nodes:
        if not n.enabled:
            continue
        for b in n.branches:
            if not b.enabled or _action_kind(b) == "special":
                continue
            key = _cond_key(b.cond)
            if key in first:
                fn, fb = first[key]
                findings.append(Finding(
                    level, "duplicate", n.idx, b.idx,
                    f"分支 {b.idx} 与分支 {fn.idx}.{fb.idx} 条件完全相同，只有前者会生效",
                    f"Branch {b.idx} duplicates branch {fn.idx}.{fb.idx}; only "
                    f"the first one can ever match",
                    {"action": "merge_branches"}))
            else:
                first[key] = (n, b)
    return findings


def check_special_misplaced(nodes: list[Node], models, tag_defs,
                            is_hard: bool) -> list:
    """特殊去向出现在「会话沿用」之外的节点（§10）← 硬错误。

    会话沿用节点 = kind=="sticky"（idx 0 起点，§5.1/§9.2）；special 落到别的
    节点会被 engine 恒判 miss（§4.4），语义失效且误导控制台。"""
    level = "hard" if is_hard else "warn"
    findings: list[Finding] = []
    for n, b in _iter_branches(nodes):
        if _action_kind(b) == "special" and n.kind != "sticky":
            findings.append(Finding(
                level, "special_misplaced", n.idx, b.idx,
                f"特殊去向出现在非会话沿用节点：节点 {n.idx} 分支 {b.idx}"
                f"（special 仅允许在会话沿用节点）",
                f"Special action outside the session node: node {n.idx} branch "
                f"{b.idx} (special is only allowed in the sticky session node)",
                {"action": "move_to_session_node"}))
    return findings


def check_conf_on_rule(nodes: list[Node], models,
                       tag_defs: dict[str, TagDef], is_hard: bool) -> list:
    """规则标签配了把握度门槛（§10）→ warn（永不满足，疑似误配）。

    规则标签 confidence 恒 None（§4.5），conf_min 门槛永不满足（engine._atom
    对 None 直接判 False，§5.1）——静默死支，报 warn 让控制台提示疑似误配。"""
    level = "hard" if is_hard else "warn"
    findings: list[Finding] = []

    def walk(clauses: list, n: Node, b: Branch) -> None:
        for c in clauses:
            if not isinstance(c, dict):
                continue
            if "or" in c:
                if isinstance(c["or"], list):       # 非 list 体：check_bad_ref 报 hard
                    walk(c["or"], n, b)
            elif "not" in c:
                walk([c["not"]], n, b)
            elif "conf_min" in c and c.get("tag") in tag_defs \
                    and tag_defs[c["tag"]].source == "rule":
                findings.append(Finding(
                    level, "conf_on_rule", n.idx, b.idx,
                    f"规则标签 {c['tag']} 配了把握度门槛（规则标签没有置信度，"
                    f"门槛永不满足）",
                    f"Rule tag {c['tag']} has a confidence threshold (rule tags "
                    f"carry no confidence, so the gate can never pass)",
                    {"action": "remove_conf_min"}))

    for n, b in _iter_branches(nodes):
        walk(b.cond, n, b)
    return findings


# §10 CHECKS 表逐字（函数, 是否硬错误）；check_no_hits 的 hits 由 run_checkup 传。
CHECKS: list[tuple] = [
    (check_dead_branch,    False),  # 分支被上方完全覆盖
    (check_unused_tags,    False),  # 标签从未被任何规则引用
    (check_no_hits,        False),  # 分支近 30 天无命中
    (check_no_fallback,    True),   # 没有兜底节点 ← 硬错误
    (check_bad_ref,        True),   # 引用不存在的标签或模型 ← 硬错误
    (check_duplicate,      False),  # 两条规则条件完全相同
    (check_special_misplaced, True),  # 特殊去向出现在「会话沿用」之外的节点 ← 硬错误
    (check_conf_on_rule,   False),  # 规则标签配了把握度门槛（永不满足，疑似误配）
]


def run_checkup(nodes: list[Node], models, tag_defs,
                hits: dict[str, int] | None = None) -> list[Finding]:
    """八项体检（§10 逐字：函数表循环 + is_hard 标记）→ list[Finding]。

    :param nodes: 内存树（rows_to_tree 产物或同构 Node 列表）。
    :param models: 可用模型字典（{model_id: …}，按 id 集合语义消费——简报测试
      传 ``{"qwen3-8b": {}, …}`` 即此口径）。
    :param tag_defs: 标签注册表（sr.tags.TAGS）。
    :param hits: 可选，hit_branch → 近 30 天命中数（check_no_hits 用，见模块
      docstring 接口裁定）；None 跳过该检查。
    """
    findings: list[Finding] = []
    for fn, is_hard in CHECKS:
        if fn is check_no_hits:
            findings.extend(fn(nodes, models, tag_defs, is_hard, hits=hits))
        else:
            findings.extend(fn(nodes, models, tag_defs, is_hard))
    return findings


def run_checkup_safe(nodes: list[Node], models, tag_defs,
                     hits: dict[str, int] | None = None) -> list[Finding]:
    """run_checkup 的**崩溃安全**入口（M5，audit Major）：体检内部异常降级为一条
    hard finding（code="checkup_error"），绝不逃逸成 PUT /admin/tree 500。

    体检是给坏数据兜底的最后闸门，它自己不能先倒——旧实现对 run_checkup 无
    try/except，体检里任何一个未预料的 TypeError（如 `fallback: null` 的 `*None`）
    都是 500（真 uvicorn 下 HTML/无信封）。口径声明（二选一，本实现取前者）：
    **降级为 hard finding** → 仍按 422 拦截保存（不放行、不掩盖：库零写入这一
    fail-safe 行为不变），message 为通用文案 + 服务端日志留原始异常，不回吐 Python
    异常原文。正常路径零开销（同一函数对象转发）；三条写路径与 import 全部经此入口。

    A1：自 sr/server.py 迁入（本模块是体检的自然归属；迁入后 config.import_yaml
    直接 import 本函数，不再反向 import sr.server——test_health 的崩溃用例改为
    patch ``sr.health.run_checkup``）。
    """
    try:
        return run_checkup(nodes, models, tag_defs, hits=hits)
    except Exception as exc:                    # 体检自身崩溃：绝不变成 500
        logger.exception("run_checkup crashed (degraded to hard finding): %r", exc)
        return [Finding(
            "hard", "checkup_error", None, None,
            "体检内部错误：本次改动无法完成检查，已按硬错误拦截保存"
            "（详细原因见服务端日志）",
            "Internal checkup error: this change could not be validated and was "
            "blocked as a hard error (see server logs for details)",
            {"action": "retry_or_report"})]
