"""Repositories: one per aggregate, plain SQL, no ORM.

The pipeline (stations) is stored as one JSON document per station and saved by
wholesale replacement inside one transaction — station edits are rare and small,
and replacement keeps ordering and deletion trivially correct.

Transaction convention: every write method owns its transaction — ``with
self.conn:`` inside the method commits before it returns. The one deliberate
exception is ``SettingsRepo.set``, which never commits (callers bind it into a
larger write). A composer may also hold an outer ``with conn:`` itself — e.g.
``put_settings`` — and an inner ``with self.conn:`` then commits the composed
transaction; that is by design, not a bug.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from router.pipeline.model import Station, TreeNode, Weight, station_from_dict, \
    station_to_dict
from router.presets import PRESET_BY_ID

#: Default confidence floor of a model-signal condition (2026-10-19 product
#: decision). Rule signals are deterministic and carry no floor.
CONF = 0.70


def _uid() -> str:
    return uuid.uuid4().hex[:12]


DEFAULT_KEYWORD_GROUPS = {"医疗急症": ["急救", "急诊", "救护车", "晕倒"],
                          "账单": ["发票", "报销", "账单"]}

DEFAULT_MODELS = [
    ("qwen3-0.6b-medical", "qwen3-0.6b-medical", "local",
     {"domains": ["health"], "tier": "small"}),
    ("qwen3-8b-medical", "qwen3-8b-medical", "local",
     {"domains": ["health"], "tier": "large"}),
    ("deepseek-v3", "deepseek-v3", "local",
     {"domains": ["computer_science"], "tier": "large"}),
    ("qwen3-8b", "qwen3-8b", "local", {"domains": [], "tier": "small"}),
    ("qwen3-32b", "qwen3-32b", "local", {"domains": [], "tier": "large"}),
]


def _node(node_id: str, signal: str | None, value: str,
          conf_min: float | None, *, kind: str = "leaf",
          action: str = "route", model: str | None = None,
          min_score: float | None = None,
          weights: list[Weight] | None = None,
          children: list[TreeNode] | None = None) -> TreeNode:
    return TreeNode(id=node_id, signal=signal, value=value, conf_min=conf_min,
                    kind=kind, action=action, model=model,
                    min_score=min_score, weights=weights or [],
                    children=children or [])


def default_pipeline() -> list[Station]:
    """Four independent station trees: ① 安全闸 / ② 关键词直达 / ③ 学科 → 难度 / ④ 兜底.

    All model-signal conditions default to a 0.70 confidence floor."""
    safety = Station(id=_uid(), name="① 安全闸", root=_node(
        "n1", None, "", None, kind="fork", children=[
            _node("n1a", "jailbreak", "jailbreak", CONF,
                  action="block"),
            _node("n1b", "pii", "cn_id_card", CONF, action="block"),
        ]))
    keyword = Station(id=_uid(), name="② 关键词直达", root=_node(
        "n2", None, "", None, kind="fork", children=[
            _node("n2a", "keyword", "医疗急症", None,   # rule signal: no floor
                  action="route", model="qwen3-8b-medical"),
        ]))
    subject = Station(id=_uid(), name="③ 学科 → 难度", root=_node(
        "n3", None, "", None, kind="fork", children=[
            _node("n3a", "domain", "health", CONF, kind="fork",
                  children=[
                      _node("n3a1", "complexity", "hard", CONF,
                            action="route", model="qwen3-8b-medical"),
                      _node("n3a2", "complexity", "easy", CONF,
                            action="route", model="qwen3-0.6b-medical"),
                  ]),
            _node("n3b", "domain", "computer_science", CONF, kind="fork",
                  children=[
                      _node("n3b1", "complexity", "hard", CONF,
                            action="route", model="deepseek-v3"),
                  ]),
        ]))
    fallback = Station(id=_uid(), name="④ 兜底", root=_node(
        "n4", None, "", None, action="route", model="qwen3-8b"))
    return [safety, keyword, subject, fallback]


def seed_default(conn: sqlite3.Connection) -> None:
    """Seed an empty database (idempotent).

    The local provider's base url comes from the preset catalog
    (``router.presets``), so the seeded provider and the console's preset
    point at the same place.
    """
    TimePeriodRepo(conn).seed_defaults()
    if conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]:
        return                                  # idempotent: only seed an empty DB
    local = PRESET_BY_ID["local"]
    # Explicit column lists: v9 widened `providers` (reasoning_format/api_key),
    # so a bare `VALUES (...)` would break on the new table width.
    conn.execute("INSERT OR IGNORE INTO providers "
                 "(name, base_url, key_env, reasoning_format, api_key) "
                 "VALUES ('local', ?, ?, ?, ?)",
                 (local["base_url"], local.get("key_env") or "",
                  local.get("reasoning_format") or "openai", ""))
    for mid, upstream, provider, profile in DEFAULT_MODELS:
        conn.execute("INSERT OR IGNORE INTO models "
                     "(id, upstream, provider, profile) VALUES (?, ?, ?, ?)",
                     (mid, upstream, provider, json.dumps(profile)))
    for name, words in DEFAULT_KEYWORD_GROUPS.items():
        conn.execute("INSERT OR IGNORE INTO keyword_groups (name, words) "
                     "VALUES (?, ?)",
                     (name, json.dumps(words, ensure_ascii=False)))
    StationRepo(conn).save(default_pipeline())
    conn.commit()


class StationRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> list[Station]:
        return [station_from_dict(json.loads(r["tree"]))
                for r in self.conn.execute("SELECT tree FROM stations ORDER BY ord")]

    def save(self, stations: list[Station]) -> None:
        with self.conn:                          # one transaction: replace all
            self.conn.execute("DELETE FROM stations")
            for si, station in enumerate(stations):
                self.conn.execute(
                    "INSERT INTO stations (id, ord, name, tree) "
                    "VALUES (?, ?, ?, ?)",
                    (station.id, si, station.name,
                     json.dumps(station_to_dict(station), ensure_ascii=False)))
            v = int(SettingsRepo(self.conn).get("pipeline_version", "0")) + 1
            SettingsRepo(self.conn).set("pipeline_version", str(v))


class ModelRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM models ORDER BY id")]

    def get(self, model_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM models WHERE id = ?",
                                (model_id,)).fetchone()
        return dict(row) if row else None

    def create(self, model_id: str, upstream: str, provider: str,
               profile: str = "{}") -> bool:
        """Insert one model row; False when the id already exists (409 at API)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO models (id, upstream, provider, profile) "
                "VALUES (?, ?, ?, ?)",
                (model_id, upstream, provider, profile))
        return cur.rowcount > 0

    def delete(self, model_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM models WHERE id = ?",
                                    (model_id,))
        return cur.rowcount > 0

    def update(self, model_id: str, *, upstream: str | None = None,
               provider: str | None = None, profile: str | None = None) -> bool:
        """Patch only the provided fields; False when the model is unknown."""
        fields, values = [], []
        for col, val in (("upstream", upstream), ("provider", provider),
                         ("profile", profile)):
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if not fields:
            return self.get(model_id) is not None
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE models SET {', '.join(fields)} WHERE id = ?",
                (*values, model_id))
        return cur.rowcount > 0


def model_references(stations: list[Station], model_id: str) -> list[dict]:
    """分树里引用 ``model_id`` 的路由叶子：``[{station, node}]``（删除前 409 用）。"""
    refs: list[dict] = []

    def walk(station: Station, node: TreeNode) -> None:
        if (node.kind == "leaf" and node.action == "route"
                and node.model == model_id):
            refs.append({"station": station.id, "node": node.id})
        for child in node.children:
            walk(station, child)

    for station in stations:
        if station.root is not None:
            walk(station, station.root)
    return refs


def model_ids_using_provider(conn: sqlite3.Connection,
                             provider_name: str) -> list[str]:
    """引用该提供方的模型 id（删除提供方前 409 用）。"""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM models WHERE provider = ? ORDER BY id",
        (provider_name,))]


class ProviderRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM providers ORDER BY name")]

    def get(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM providers WHERE name = ?",
                                (name,)).fetchone()
        return dict(row) if row else None

    def upsert(self, name: str, base_url: str, *, key_env: str | None = None,
               reasoning_format: str | None = None,
               api_key: str | None = None) -> None:
        """Insert-or-update. ``None`` = 保持现有值（新行用默认：''/openai/''）。"""
        existing = self.get(name)
        new_key_env = (existing or {}).get("key_env", "") \
            if key_env is None else key_env
        new_fmt = (existing or {}).get("reasoning_format", "openai") \
            if reasoning_format is None else reasoning_format
        new_api_key = (existing or {}).get("api_key", "") \
            if api_key is None else api_key
        with self.conn:
            self.conn.execute(
                "INSERT INTO providers "
                "(name, base_url, key_env, reasoning_format, api_key) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_url = excluded.base_url, "
                "key_env = excluded.key_env, "
                "reasoning_format = excluded.reasoning_format, "
                "api_key = excluded.api_key",
                (name, base_url, new_key_env, new_fmt, new_api_key))

    def delete(self, name: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM providers WHERE name = ?",
                                    (name,))
        return cur.rowcount > 0


class KeywordRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def groups(self) -> dict[str, list[str]]:
        return {r["name"]: json.loads(r["words"]) for r in
                self.conn.execute("SELECT * FROM keyword_groups ORDER BY name")}

    def save(self, groups: dict[str, list[str]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM keyword_groups")
            for name, words in groups.items():
                self.conn.execute("INSERT INTO keyword_groups VALUES (?, ?)",
                                  (name, json.dumps(words, ensure_ascii=False)))


class SettingsRepo:
    """Runtime settings (key/value)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?",
                                (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        """Write one setting. Does NOT commit: the caller wraps the whole
        write in ``with conn:`` (a setting must not depend on a later,
        unrelated write to be persisted)."""
        self.conn.execute(
            "INSERT INTO settings VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def all(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in
                self.conn.execute("SELECT key, value FROM settings ORDER BY key")}


class TimePeriodRepo:
    """时段词表：命名的时间段（关键词词表的时间版）。

    每行 {name, days: [0..6]（周一=0）, start: "HH:MM", end: "HH:MM"}；
    start > end 表示跨午夜，start == end 表示全天。请求时刻命中了哪些时段，
    time_window 信号的取值就是哪些名字（列表，与 keyword 词表同语义）。"""

    DEFAULTS = [
        {"name": "白天", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"},
        {"name": "夜间", "days": [0, 1, 2, 3, 4, 5, 6], "start": "18:00", "end": "09:00"},
        {"name": "周末", "days": [5, 6], "start": "00:00", "end": "00:00"},   # 全天
    ]

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> list[dict]:
        """Read the period table. Never writes (a GET must not mutate)."""
        return [{"name": r["name"],
                 "days": [int(d) for d in json.loads(r["days"])],
                 "start": r["start"], "end": r["end"]}
                for r in self.conn.execute(
                    "SELECT * FROM time_periods ORDER BY rowid")]

    def seed_defaults(self) -> None:
        """Seed the default periods exactly once per database lifetime.

        An empty table also results from a deliberate ``PUT /time-periods []
        (user emptied the table)``: resurrecting the defaults on the next
        restart would silently undo that delete (2026-10-22 review). A marker
        in ``settings`` therefore decides, not the table state.
        """
        settings = SettingsRepo(self.conn)
        if settings.get("time_periods_seeded"):
            return
        if not self.conn.execute(
                "SELECT COUNT(*) FROM time_periods").fetchone()[0]:
            self.save(self.DEFAULTS)     # self-commits (save owns its transaction)
        with self.conn:
            settings.set("time_periods_seeded", "1")

    def save(self, periods: list[dict]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM time_periods")
            for p in periods:
                self.conn.execute("INSERT OR REPLACE INTO time_periods VALUES (?, ?, ?, ?)",
                                  (p["name"], json.dumps(p.get("days") or []),
                                   p["start"], p["end"]))


class ChangeRepo:
    """Configuration audit trail (design 5.1): every admin write lands here."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, ts: str, kind: str, detail: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO changes (ts, kind, detail) VALUES (?, ?, ?)",
                              (ts, kind, json.dumps(detail, ensure_ascii=False)))

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ts, kind, detail FROM changes ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


class EvalRepo:
    """Evaluation reports (design 8)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, ts: str, mode: str, total: int, matched: int, rate: float,
             report: dict) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO evals (ts, mode, total, matched, rate, report) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, mode, total, matched, rate,
                 json.dumps(report, ensure_ascii=False)))
        return int(cur.lastrowid)

    def recent(self, limit: int = 3) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ts, mode, total, matched, rate, report FROM evals "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "report": json.loads(r["report"])} for r in rows]
