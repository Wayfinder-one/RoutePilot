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
from contextlib import contextmanager
from datetime import datetime, timedelta

from router import auth
from router.pipeline.model import Station, TreeNode, Weight, station_from_dict, \
    station_to_dict
from router.pipeline.snapshot import (REVISION_PIPELINE, REVISION_QUESTIONS,
                                      canonical_json, pipeline_content,
                                      sha256_text)
from router.presets import PRESET_BY_ID

#: Default confidence floor of a model-signal condition (2026-10-19 product
#: decision). Rule signals are deterministic and carry no floor.
CONF = 0.70


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record_revision(conn: sqlite3.Connection, kind: str, content: str) -> str:
    """Append-only content-addressed revision insert; returns its sha.

    Does NOT open a transaction (the caller owns one): ``UNIQUE(kind, sha256)``
    plus ``INSERT OR IGNORE`` means re-saving identical content keeps the
    original row/id and never rewrites history.
    """
    sha = sha256_text(content)
    conn.execute(
        "INSERT OR IGNORE INTO config_revisions "
        "(kind, sha256, content, created_at) VALUES (?, ?, ?, ?)",
        (kind, sha, content, _now_iso()))
    return sha


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

    def save(self, stations: list[Station]) -> str:
        """Replace the whole pipeline, bump its version and record the revision.

        One transaction: the stations rows, the version bump, the canonical
        ``config_revisions`` content and ``settings.pipeline_revision`` are
        committed together (a crash can never leave a tree whose revision is
        unknown). Old revisions are never deleted — replay can load them.

        Returns the canonical content sha (``pipeline_revision``).
        """
        content = pipeline_content(stations)
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
            revision = record_revision(self.conn, REVISION_PIPELINE, content)
            SettingsRepo(self.conn).set("pipeline_revision", revision)
        return revision


def ensure_pipeline_revision(conn: sqlite3.Connection,
                             stations: list[Station]) -> str:
    """Backfill the content revision for the CURRENT tree (idempotent).

    Needed on the first boot after migration v15 (stations predate the
    revisions table) and harmless afterwards: identical content maps to the
    same sha, so the original revision row is kept.
    """
    content = pipeline_content(stations)
    with conn:
        revision = record_revision(conn, REVISION_PIPELINE, content)
        SettingsRepo(conn).set("pipeline_revision", revision)
    return revision


def ensure_questions_revision(conn: sqlite3.Connection, cfg: dict) -> str:
    """Record the canonical questions config revision (idempotent)."""
    content = canonical_json(cfg)
    with conn:
        revision = record_revision(conn, REVISION_QUESTIONS, content)
        SettingsRepo(conn).set("questions_revision", revision)
    return revision


class ConfigRevisionRepo:
    """Append-only canonical config history (migration v15)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, kind: str, content: str) -> str:
        with self.conn:
            return record_revision(self.conn, kind, content)

    def get(self, kind: str, sha256: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, kind, sha256, content, created_at FROM config_revisions "
            "WHERE kind = ? AND sha256 = ?", (kind, sha256)).fetchone()
        return dict(row) if row else None

    def latest(self, kind: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, kind, sha256, content, created_at FROM config_revisions "
            "WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return dict(row) if row else None



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


# --------------------------------------------------------------------------- #
# 用户体系（migration v17，2026-10-26）
# --------------------------------------------------------------------------- #
class EmailExists(Exception):
    """邮箱已被占用（唯一性大小写不敏感）；API 层映射成 409。"""


def _is_expired(value: str, now: datetime) -> bool:
    """ISO 时间串是否已到期；解析不了的值按「已过期」处理（安全优先）。"""
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:              # 手工写入的无时区值按本地时区理解
        when = when.astimezone()
    return when <= now


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：先拿写锁，再做「读-判断-写」。

    普通 ``with conn:`` 是 DEFERRED：两个并发请求可能都先读到「还有 2 个
    active admin」然后各自降级，最后 0 个 admin。用户体系的保护规则
    （spec §7「并发最后 admin 保护」）要求不依赖前端与请求顺序，所以守卫写
    用这个 helper 串行化。UserRepo/SessionRepo 的写方法通过 ``_tx`` 加入外层
    事务而不是各自提交。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def _tx(conn: sqlite3.Connection):
    """新仓储的写事务：外层已有事务就加入（由外层提交），否则自己开一个。

    旧仓储的 ``with self.conn:`` 在嵌套时会提前提交外层事务，因此用户/会话
    仓储改用这个 helper，让 ``write_transaction`` 里的组合写保持原子。
    """
    if conn.in_transaction:
        yield
        return
    with conn:
        yield


class UserRepo:
    """控制台账号（users 表）。

    ``create``/``get``/``get_by_email`` 返回的行**含 password_hash**（登录校验
    需要）；对外输出必须先过 ``public()``——spec 明确要求任何响应/日志都不带
    哈希。审计同理，只记 metadata。
    """

    #: 对外的用户字段（顺序即 API 输出顺序）
    PUBLIC_FIELDS = ("id", "email", "role", "status", "created_at",
                     "last_login_at")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM users ORDER BY created_at, id")]

    def get(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?",
                                (user_id,)).fetchone()
        return dict(row) if row else None

    def get_by_email(self, email: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE email = ?",
                                (auth.normalize_email(email),)).fetchone()
        return dict(row) if row else None

    def public(self, user: dict) -> dict:
        """Strip password_hash/updated_at: the only shape the API may return."""
        return {key: user.get(key) for key in self.PUBLIC_FIELDS}

    def count_active_admins(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' "
            "AND status = 'active'").fetchone()[0])

    def create(self, email: str, password: str, role: str = "viewer") -> dict:
        """Insert one account; returns the full row (callers use public())."""
        email = auth.normalize_email(email)
        if self.get_by_email(email) is not None:
            raise EmailExists(email)          # 先查一次：重复请求不必付哈希成本
        user_id = uuid.uuid4().hex
        now = _now_iso()
        # 哈希慢（~0.3s）且不可回滚，放在事务外：别让它把写锁一起捧着。
        password_hash = auth.hash_password(password)
        try:
            with _tx(self.conn):
                self.conn.execute(
                    "INSERT INTO users (id, email, password_hash, role, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, email, password_hash, role, "active", now, now))
        except sqlite3.IntegrityError:
            raise EmailExists(email) from None
        return self.get(user_id)              # type: ignore[return-value]

    def update(self, user_id: str, *, email: str | None = None,
               role: str | None = None, status: str | None = None
               ) -> dict | None:
        """Patch only the provided fields; None when the user is unknown."""
        fields, values = [], []
        if email is not None:
            fields.append("email = ?")
            values.append(auth.normalize_email(email))
        if role is not None:
            fields.append("role = ?")
            values.append(role)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if not fields:
            return self.get(user_id)
        fields.append("updated_at = ?")
        values.extend([_now_iso(), user_id])
        try:
            with _tx(self.conn):
                cur = self.conn.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        except sqlite3.IntegrityError:
            raise EmailExists(auth.normalize_email(email or "")) from None
        return self.get(user_id) if cur.rowcount else None

    def set_password(self, user_id: str, password: str) -> bool:
        """Replace the password hash; False when the user is unknown.

        Plan Task 1 的接口清单没列这个方法，但 Task 2 的改密/重置密码需要它；
        单独一个方法比给 update() 加 password 参数更不容易误把哈希当普通字段。
        """
        password_hash = auth.hash_password(password)
        with _tx(self.conn):
            cur = self.conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, _now_iso(), user_id))
        return cur.rowcount > 0

    def delete(self, user_id: str) -> bool:
        with _tx(self.conn):
            cur = self.conn.execute("DELETE FROM users WHERE id = ?",
                                    (user_id,))
        return cur.rowcount > 0

    def record_login(self, user_id: str) -> None:
        with _tx(self.conn):
            self.conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_now_iso(), user_id))

    def seed_admin(self, email: str, password: str) -> bool:
        """Seed the default admin only into an empty users table (idempotent).

        非空即返回，绝不覆盖已改过的密码；空表才做一次 scrypt（0.3s 的量级，
        所以 user_auth 关闭的部署不该走到这里）。
        """
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return False
        self.create(email, password, role="admin")
        return True


class SessionRepo:
    """登录会话（user_sessions 表，只存 sha256(token)）。

    TTL 默认 168h（spec §6）；过期行在校验/清理时惰性删除。会话绑定用户，
    校验时查 status：停用账号已签发的 token 立即失效。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, user_id: str, ttl_hours: int) -> tuple[str, str]:
        """Issue one session; returns ``(token, expires_at)`` (token 明文只出现一次)."""
        token = auth.new_session_token()
        now = datetime.now().astimezone()
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds")
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO user_sessions (token_hash, user_id, created_at, "
                "expires_at) VALUES (?, ?, ?, ?)",
                (auth.session_token_hash(token), user_id,
                 now.isoformat(timespec="seconds"), expires_at))
        return token, expires_at

    def user_for_token(self, token: str) -> dict | None:
        """Resolve a session token to its active user (None = invalid).

        返回的用户 dict 是 ``UserRepo.public()`` 的字段再加 ``expires_at``；
        中间件只需要身份信息，所以不带 password_hash。
        """
        if not token:
            return None
        row = self.conn.execute(
            "SELECT s.expires_at AS session_expires_at, u.* "
            "FROM user_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?",
            (auth.session_token_hash(token),)).fetchone()
        if row is None:
            return None
        expires_at = row["session_expires_at"]
        if _is_expired(expires_at, datetime.now().astimezone()):
            self.delete(token)                # 惰性清理过期行
            return None
        user = {key: row[key] for key in row.keys()
                if key != "session_expires_at"}
        if user["status"] != "active":
            # 停用账号立即失效；会话行留着——重新启用后无需再登录
            # （spec §7 只要求「校验时查 status」，没有要求删除）。
            return None
        return {**UserRepo(self.conn).public(user), "expires_at": expires_at}

    def delete(self, token: str) -> None:
        with _tx(self.conn):
            self.conn.execute("DELETE FROM user_sessions WHERE token_hash = ?",
                              (auth.session_token_hash(token),))

    def delete_for_user(self, user_id: str) -> None:
        with _tx(self.conn):
            self.conn.execute("DELETE FROM user_sessions WHERE user_id = ?",
                              (user_id,))

    def purge_expired(self) -> None:
        now = datetime.now().astimezone()
        expired = [r["token_hash"] for r in self.conn.execute(
            "SELECT token_hash, expires_at FROM user_sessions")
            if _is_expired(r["expires_at"], now)]
        if not expired:
            return
        with _tx(self.conn):
            self.conn.executemany(
                "DELETE FROM user_sessions WHERE token_hash = ?",
                [(token_hash,) for token_hash in expired])
