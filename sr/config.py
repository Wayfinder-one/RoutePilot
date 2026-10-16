"""配置 YAML 导入导出 + 校验（spec §9.2 四节包；Task 11 控制者裁定 1）。

四节包形状（§9.2）::

    providers: [{name, proto, base_url, key_env, extra}]
    models:    [{id, provider, upstream, caps, aliases, step,
                 price_in, price_out, enabled}]
    tree:      {version, nodes: […]}      # GET /admin/tree 同形状
    settings:  {key: value}               # 键 ⊆ sr.db.DEFAULT_SETTINGS

- :func:`export_yaml` —— 只导可编辑配置（providers/models 的 ok/break_reason 是
  熔断运行态，不进配置包——Task 9 端点口径原样迁入）。
- :func:`import_yaml` —— normalize 全量校验（四节结构 + 字段/值域/引用）+ 体检
  run_checkup：hard 整包拒绝（返回 findings 列表，**不写库**）；过则单事务
  UPSERT 写入 providers → models → settings → tree（不删既有行）+ 审计 + commit，
  返回 None。``actor`` 记 changes.actor（端点转调为 "admin"，CLI 为 "cli"）。
- 结构/YAML/引用非法 raise ValueError（消息即 detail：HTTP 层转 400，CLI 退出码 2）。

消费方：sr/server（GET /admin/export、POST /admin/import 转调，裁定 1）与
sr/__main__（``sr export`` / ``sr import`` / ``sr init --import``）。

依赖方向：本模块 → sr.db / sr.engine / sr.health / sr.tags（均无环）；对
sr.server 的树辅助（_tree_from_json / _tree_json / _write_tree_rows）用**函数内
延迟 import**——server 顶层 import 本模块，模块级反向引用会成环。
"""

from __future__ import annotations

import json
import sqlite3

import yaml

import sr.db as db
from sr.health import run_checkup
from sr.tags import TAGS

# 四节包必需节（缺任一 → ValueError "missing sections: …"，与原端点消息逐字一致）
_SECTIONS = ("providers", "models", "tree", "settings")


def _srv():
    """延迟取 sr.server（树 JSON 辅助所在；见模块头依赖方向说明）。"""
    from sr import server
    return server


def _tree_now(conn) -> dict:
    """当前树 → §9.2 JSON 形状（不经 server 的树缓存：本模块只拿 conn，
    直接 rows_to_tree——与缓存内容恒等，缓存镜像的就是这张表）。"""
    from sr.engine import rows_to_tree
    return _srv()._tree_json(
        rows_to_tree(conn.execute("SELECT * FROM tree").fetchall()),
        db.get_tree_version(conn))


def export_yaml(conn: sqlite3.Connection) -> str:
    """DB 当前配置 → 四节 YAML 文本（pyyaml safe_dump，依赖白名单 §13 明列）。"""
    providers = [{"name": r["name"], "proto": r["proto"],
                  "base_url": r["base_url"], "key_env": r["key_env"],
                  "extra": json.loads(r["extra"] or "{}")}
                 for r in conn.execute("SELECT * FROM providers ORDER BY name")]
    models = [{"id": r["id"], "provider": r["provider"],
               "upstream": r["upstream"],
               "caps": json.loads(r["caps"] or "[]"),
               "aliases": json.loads(r["aliases"] or "[]"),
               "step": r["step"], "price_in": r["price_in"],
               "price_out": r["price_out"], "enabled": bool(r["enabled"])}
              for r in conn.execute("SELECT * FROM models ORDER BY id")]
    settings = _srv()._load_settings(conn)
    return yaml.safe_dump({"providers": providers, "models": models,
                           "tree": _tree_now(conn), "settings": settings},
                          allow_unicode=True, sort_keys=False)


def import_yaml(conn: sqlite3.Connection, text, *, actor: str = "admin"):
    """四节 YAML 包 → 校验 + 单事务写入。

    :param text: YAML 文本/字节（HTTP 端点转原始 body，CLI 转文件内容）。
    :returns: None（成功，已 commit）；hard findings 列表（``f.to_json()`` dict，
              硬错误优先排序——原端点口径，整包未写）。
    :raises ValueError: YAML 解析失败 / 非映射 / 缺节 / 行形状或引用非法
              （消息与原端点 detail 逐字一致）。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("YAML package must be a mapping with "
                         "providers/models/tree/settings")
    missing = [s for s in _SECTIONS if s not in data]
    if missing:
        raise ValueError(f"missing sections: {missing}")
    provs = []
    for p in data["providers"]:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) \
                or p.get("proto") not in ("openai", "anthropic") \
                or not isinstance(p.get("base_url"), str):
            raise ValueError(f"invalid provider row: {p!r}")
        extra = p.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        provs.append((p["name"], p["proto"], p["base_url"],
                      p.get("key_env"), db._dump(extra)))
    prov_names = {p[0] for p in provs}
    model_rows = []
    for m in data["models"]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) \
                or not isinstance(m.get("provider"), str) \
                or not isinstance(m.get("upstream"), str):
            raise ValueError(f"invalid model row: {m!r}")
        if m["provider"] not in prov_names:
            raise ValueError(f"model {m['id']}: unknown provider "
                             f"{m['provider']!r}")
        caps, aliases = m.get("caps", []), m.get("aliases", [])
        if not isinstance(caps, list) or not isinstance(aliases, list):
            raise ValueError(f"model {m['id']}: caps/aliases must be lists")
        step = m.get("step")
        if step is not None and (not isinstance(step, int)
                                 or isinstance(step, bool)):
            raise ValueError(f"model {m['id']}: step must be int or null")
        model_rows.append((m["id"], m["provider"], m["upstream"],
                           db._dump(caps), db._dump(aliases), step,
                           m.get("price_in"), m.get("price_out"),
                           1 if m.get("enabled", True) else 0))
    settings = data["settings"]
    if not isinstance(settings, dict) \
            or set(settings) - set(db.DEFAULT_SETTINGS):
        raise ValueError("settings keys must be within DEFAULT_SETTINGS")
    try:
        nodes = _srv()._tree_from_json(data["tree"])
    except ValueError as exc:
        raise ValueError(f"invalid tree: {exc}") from exc   # 原端点 detail 前缀
    model_ids = set(_srv()._models_set(conn)) | {r[0] for r in model_rows}
    findings = run_checkup(nodes, {m: {} for m in model_ids}, TAGS)
    if any(f.level == "hard" for f in findings):
        ordered = sorted(findings, key=lambda f: f.level != "hard")
        return [f.to_json() for f in ordered]
    old = _tree_now(conn)
    for name, proto, base_url, key_env, extra in provs:
        conn.execute(
            "INSERT INTO providers (name, proto, base_url, key_env, extra) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET proto = excluded.proto, "
            "base_url = excluded.base_url, key_env = excluded.key_env, "
            "extra = excluded.extra",
            (name, proto, base_url, key_env, extra))
    for mid_, provider, upstream, caps, aliases, step, pin, pout, en \
            in model_rows:
        conn.execute(
            "INSERT INTO models (id, provider, proto, upstream, caps, "
            "aliases, step, price_in, price_out, enabled) "
            "VALUES (?, ?, (SELECT proto FROM providers WHERE name = ?), "
            "?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, "
            "proto = excluded.proto, upstream = excluded.upstream, "
            "caps = excluded.caps, aliases = excluded.aliases, "
            "step = excluded.step, price_in = excluded.price_in, "
            "price_out = excluded.price_out, enabled = excluded.enabled",
            (mid_, provider, provider, upstream, caps, aliases, step,
             pin, pout, en))
    for k, v in settings.items():
        if k != "tree_version":        # 系统管理键：不收写入口
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)))
    _srv()._write_tree_rows(conn, nodes)
    db.bump_tree_version(conn)
    db.log_change(conn, actor, "import", "*",
                  before={"version": old["version"], "nodes": old["nodes"]},
                  after={"sections": sorted(data)})
    conn.commit()
    return None
