"""分流树 JSON 双向序列化 + 重编号 + 落库（spec §9.2 形状 / §3.1 tree 表）。

A1（audit §3）：自 sr/server.py 拆出（原「7) admin 读端点辅助」的树逆组装 +
「9) admin 写端点辅助」的树解析/重编号/落库）。GET /admin/tree 的 §9.2 形状与
PUT /admin/tree、config.import_yaml 的 tree 节共用同一套转换——拆出后 config.py
直接 import 本模块，不再 `import sr.server`（消掉 A1 点名的反向依赖）。

依赖方向：本模块 → sr.db（dump/now）、sr.engine（Branch/Node）、sr.health（Finding）、
fastapi（_tree_full_422 的 JSONResponse）。**不** import sr.server。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

import sr.db as db
from sr.engine import Branch, Node
from sr.health import Finding

# 写端点的 changes.actor（§3.1 changes 表；M1 无登录体系，固定操作者）。
WRITE_ACTOR = "admin"


# ─────────────────────────────────────────────────────────────────────────────
# 1) rows_to_tree 的逆组装 → §9.2 JSON（GET /admin/tree / export）
# ─────────────────────────────────────────────────────────────────────────────


def _branch_json(b: Branch) -> dict:
    """Branch → §9.2 分支形状；cond/action 原样透出（含 conf_min/fallback/
    block_msg 等全部规范键，不剥任何键——Task 10 GET→PUT 回环依赖此保证）。"""
    return {"idx": b.idx, "cond": b.cond, "action": b.action, "trial": b.trial,
            "enabled": b.enabled, "kids": [_branch_json(k) for k in b.kids]}


def _tree_json(nodes: list, version: int) -> dict:
    """rows_to_tree 的逆组装 → §9.2 首块形状（与原型 TREE 结构一一对应）。"""
    return {"version": version,
            "nodes": [{"idx": n.idx, "kind": n.kind, "name": n.name,
                       "desc": n.desc, "enabled": n.enabled,
                       "branches": [_branch_json(b) for b in n.branches]}
                      for n in nodes]}


# ─────────────────────────────────────────────────────────────────────────────
# 2) §9.2 JSON → Node/Branch（PUT /admin/tree、import 的 tree 节）
# ─────────────────────────────────────────────────────────────────────────────


def _take_idx(idx, require_idx: bool, seen: set) -> int:
    """idx 校验：整数、（require_idx 时）必填、全树唯一（tree.idx 是全表主键，
    节点/分支共用一个编号空间，§3.1）。非法抛 ValueError（端点转 400）。"""
    if not isinstance(idx, int) or isinstance(idx, bool):
        if require_idx:
            raise ValueError(f"idx must be an integer: {idx!r}")
        return -1                                  # 候选节点/estimate 占位
    if idx in seen:
        raise ValueError(f"duplicate idx: {idx}")
    seen.add(idx)
    return idx


def _branch_from_json(obj, *, require_idx: bool, seen: set) -> Branch:
    """§9.2 分支 JSON → Branch（cond/action 原样收，零剥键——GET→PUT 回环无损）。"""
    if not isinstance(obj, dict):
        raise ValueError("branch must be an object")
    idx = _take_idx(obj.get("idx"), require_idx, seen)
    cond = obj.get("cond")
    action = obj.get("action")
    if not isinstance(cond, list):
        raise ValueError(f"branch {idx}: cond must be a list")
    if not isinstance(action, dict):
        raise ValueError(f"branch {idx}: action must be an object")
    kids = [_branch_from_json(k, require_idx=require_idx, seen=seen)
            for k in (obj.get("kids") or [])]
    if len(kids) > 9:
        raise ValueError(f"branch {idx}: more than 9 kids (idx convention, §9.2)")
    trial = obj.get("trial")
    if trial is not None and not isinstance(trial, dict):
        raise ValueError(f"branch {idx}: trial must be an object or null")
    return Branch(idx=idx, cond=cond, action=action, trial=trial,
                  enabled=bool(obj.get("enabled", True)), kids=kids)


def _node_from_json(obj, *, require_idx: bool, seen: set) -> Node:
    """§9.2 节点 JSON → Node。``kind`` 不入参（读侧由 rows_to_tree 按内容推导，
    Task 9 口径）；候选节点（POST nodes / estimate）可无 idx（require_idx=False）。"""
    if not isinstance(obj, dict):
        raise ValueError("node must be an object")
    idx = _take_idx(obj.get("idx"), require_idx, seen)
    branches = [_branch_from_json(b, require_idx=require_idx, seen=seen)
                for b in (obj.get("branches") or [])]
    if len(branches) > 9:
        raise ValueError(f"node {idx}: more than 9 branches (idx convention, §9.2)")
    return Node(idx=idx, kind=str(obj.get("kind") or "normal"),
                name=str(obj.get("name") or ""), desc=obj.get("desc"),
                enabled=bool(obj.get("enabled", True)), branches=branches)


def _tree_from_json(body) -> list[Node]:
    """PUT /admin/tree（或 import 的 tree 节）请求体 → Node 列表。

    形状非法抛 ValueError（端点统一转 400）：body 是对象、nodes 非空列表、
    全行 idx 整数且唯一、每节点分支数/每分支子分支数 ≤ 9（「父 idx*10 + 段内
    序号」的段内上限，§9.2）。GET 附带的 version/kind 等键忽略。"""
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), list) \
            or not body["nodes"]:
        raise ValueError("body must be {nodes: [...]} with at least one node")
    seen: set = set()
    return [_node_from_json(n, require_idx=True, seen=seen) for n in body["nodes"]]


# ─────────────────────────────────────────────────────────────────────────────
# 3) 重编号 + 撞号检测（§9.2 编号约定）
# ─────────────────────────────────────────────────────────────────────────────


def _renumber_kids(b: Branch) -> None:
    """分支的子分支递归重编号（kid.idx = 父分支 idx×10 + 段内序号）。

    M2（backlog 二.4）：旧实现只重编两层——三层嵌套候选的孙分支保持 _take_idx
    的占位（require_idx=False 时为 -1）落库。改为递归后任意深度都按公式编号。
    """
    for k, kid in enumerate(b.kids):
        kid.idx = b.idx * 10 + k
        _renumber_kids(kid)


def _renumber(nodes: list[Node]) -> None:
    """位置序 → idx（§9.2 约定：分支 = 父 idx*10 + 段内序号；节点 0 的 0 段与
    顶层节点号冲突，特例落 100+序号——seed 同款）。全树 ≤ 10 节点、每段 ≤ 9 子
    （调用方校验）时编号空间互不重叠。子分支递归（M2：支持任意嵌套深度）。"""
    for i, n in enumerate(nodes):
        n.idx = i
        for j, b in enumerate(n.branches):
            b.idx = (100 + j) if i == 0 else (i * 10 + j)
            _renumber_kids(b)


def _row_idxs(nodes: list[Node]):
    """全树落库行 idx（节点 + 分支 + kids 递归）——tree.idx 是全表主键（§3.1），
    节点/分支共用一个编号空间；撞号检测口径与 _write_tree_rows 的行集一致。"""
    for n in nodes:
        yield n.idx
        stack = list(n.branches)
        while stack:
            b = stack.pop(0)
            yield b.idx
            stack.extend(b.kids)


def _idx_conflict(nodes: list[Node]) -> int | None:
    """重编后的首个撞号 idx（无撞号 → None）。

    撞号来源是 §9.2 编号公式的固有重叠：节点 1 的第 0 条分支派生子分支
    10×10+k = 100+k，与节点 0 特例段 100+序号相撞（节点号 ≥ 10 同理落 100+
    段）。_write_tree_rows 是直接 INSERT，撞号以 tree.idx 主键 IntegrityError
    （HTTP 500）收场，必须在写库前拦下转 422。"""
    seen: set[int] = set()
    for idx in _row_idxs(nodes):
        if idx in seen:
            return idx
        seen.add(idx)
    return None


def _tree_full_422(conflict: int | None = None) -> JSONResponse:
    """idx 空间耗尽/无法无损重编 → 422 findings（与体检拦截同形状；code
    "tree_full"、message 双语、引导改用 PUT /admin/tree 整树替换）。不写库、
    不记 log_change——与体检 422 路径同语义（事务未动，无可回滚内容）。"""
    where_zh = f"（撞号 idx {conflict}）" if conflict is not None else ""
    where_en = f": conflict at idx {conflict}" if conflict is not None else ""
    finding = Finding(
        "hard", "tree_full", None, None,
        f"分流树编号空间已用尽{where_zh}：M1 编号约定（分支 = 父 idx×10 + 段内"
        "序号，节点 0 占 100+ 段）下无法无损重编，保存会使分支号与既有行相撞。"
        "请改用 PUT /admin/tree 整树替换。",
        f"The tree idx space is exhausted{where_en}: under the M1 convention "
        "(branch idx = parent idx × 10 + position, node 0 occupies the 100+ "
        "range) this tree cannot be renumbered losslessly and would collide "
        "with existing rows. Use PUT /admin/tree to replace the whole tree "
        "instead.",
        {"action": "use_put_tree"})
    return JSONResponse({"findings": [finding.to_json()]}, status_code=422)


# ─────────────────────────────────────────────────────────────────────────────
# 4) 落库（先 DELETE 后 INSERT；事务由调用方 commit）
# ─────────────────────────────────────────────────────────────────────────────


def _write_branch_rows(conn, parent_idx: int, b: Branch) -> None:
    """分支行 INSERT（kids 递归；分支行 name 存空串——显示名由控制台按 cond
    渲染，§3.1；updated_at/by 记审计面）。"""
    conn.execute(
        "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, enabled, "
        "updated_at, updated_by) VALUES (?, ?, '', NULL, ?, ?, ?, ?, ?, ?)",
        (b.idx, parent_idx, db._dump(b.cond), db._dump(b.action),
         None if b.trial is None else db._dump(b.trial),
         1 if b.enabled else 0, db._now(), WRITE_ACTOR))
    for kid in b.kids:
        _write_branch_rows(conn, b.idx, kid)


def _write_tree_rows(conn, nodes: list[Node]) -> None:
    """整树落库（事务由调用方 commit）：先 DELETE 后按 §3.1 行结构 INSERT。
    节点行 cond/action 为 NOT NULL 占位（'[]' / 'null'，init_db._seed 同款）。"""
    conn.execute("DELETE FROM tree")
    for n in nodes:
        conn.execute(
            "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, "
            "enabled, updated_at, updated_by) "
            "VALUES (?, NULL, ?, ?, '[]', 'null', NULL, ?, ?, ?)",
            (n.idx, n.name, n.desc, 1 if n.enabled else 0, db._now(), WRITE_ACTOR))
        for b in n.branches:
            _write_branch_rows(conn, n.idx, b)
