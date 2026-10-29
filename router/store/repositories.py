from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from router import auth
from router.presets import PRESET_BY_ID
from router.routing.conditions import TimePeriod
from router.routing.model import RoutingTree, Target, tree_revision_of, walk_nodes


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def _tx(conn: sqlite3.Connection):
    if conn.in_transaction:
        yield
        return
    with conn:
        yield


def record_revision(conn: sqlite3.Connection, kind: str, content: str) -> str:
    sha = sha256_text(content)
    conn.execute(
        "INSERT OR IGNORE INTO config_revisions (kind, sha256, content, created_at) VALUES (?, ?, ?, ?)",
        (kind, sha, content, _now_iso()),
    )
    return sha


DEFAULT_MODELS = [
    ("qwen3-0.6b-medical", "qwen3-0.6b-medical", "local", {"domains": ["health"], "tier": "small"}),
    ("qwen3-8b-medical", "qwen3-8b-medical", "local", {"domains": ["health"], "tier": "large"}),
    ("deepseek-v3", "deepseek-v3", "local", {"domains": ["computer_science"], "tier": "large"}),
    ("qwen3-8b", "qwen3-8b", "local", {"domains": [], "tier": "small"}),
    ("qwen3-32b", "qwen3-32b", "local", {"domains": [], "tier": "large"}),
]


def seed_default(conn: sqlite3.Connection) -> None:
    TimePeriodRepo(conn).seed_defaults()
    settings = SettingsRepo(conn)
    if settings.get("catalog_seeded"):
        return
    local = PRESET_BY_ID["local"]
    with _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO providers (name, base_url, key_env, reasoning_format, api_key) VALUES ('local', ?, ?, ?, ?)",
            (local["base_url"], local.get("key_env") or "", local.get("reasoning_format") or "openai", ""),
        )
        for model_id, upstream, provider, profile in DEFAULT_MODELS:
            conn.execute(
                "INSERT OR IGNORE INTO models (id, upstream, provider, profile) VALUES (?, ?, ?, ?)",
                (model_id, upstream, provider, json.dumps(profile)),
            )
        settings.set("catalog_seeded", "1")


class ConfigRevisionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, kind: str, content: str) -> str:
        with _tx(self.conn):
            return record_revision(self.conn, kind, content)

    def get(self, kind: str, sha256: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, kind, sha256, content, created_at FROM config_revisions WHERE kind = ? AND sha256 = ?",
            (kind, sha256),
        ).fetchone()
        return dict(row) if row else None

    def latest(self, kind: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, kind, sha256, content, created_at FROM config_revisions WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return dict(row) if row else None


class ModelRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM models ORDER BY id")]

    def get(self, model_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        return dict(row) if row else None

    def create(self, model_id: str, upstream: str, provider: str, profile: str = "{}") -> bool:
        with _tx(self.conn):
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO models (id, upstream, provider, profile) VALUES (?, ?, ?, ?)",
                (model_id, upstream, provider, profile),
            )
        return cursor.rowcount > 0

    def delete(self, model_id: str) -> bool:
        with _tx(self.conn):
            cursor = self.conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        return cursor.rowcount > 0

    def update(self, model_id: str, *, upstream: str | None = None, provider: str | None = None, profile: str | None = None) -> bool:
        fields, values = [], []
        for column, value in (("upstream", upstream), ("provider", provider), ("profile", profile)):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        if not fields:
            return self.get(model_id) is not None
        with _tx(self.conn):
            cursor = self.conn.execute(
                f"UPDATE models SET {', '.join(fields)} WHERE id = ?",
                (*values, model_id),
            )
        return cursor.rowcount > 0


def model_references(trees: list[RoutingTree], model_id: str) -> list[dict]:
    refs = []
    for tree in trees:
        revision = tree.tree_revision or tree_revision_of(tree)
        for node in walk_nodes(tree.root):
            if isinstance(node, Target) and any(item.model == model_id for item in node.models):
                refs.append({"tree_id": tree.id, "tree_revision": revision, "node_id": node.id, "via": "target"})
    return refs


def model_ids_using_provider(conn: sqlite3.Connection, provider_name: str) -> list[str]:
    return [row["id"] for row in conn.execute(
        "SELECT id FROM models WHERE provider = ? ORDER BY id", (provider_name,)
    )]


class ProviderRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM providers ORDER BY name")]

    def get(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM providers WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def upsert(self, name: str, base_url: str, *, key_env: str | None = None, reasoning_format: str | None = None, api_key: str | None = None) -> None:
        existing = self.get(name) or {}
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_url = excluded.base_url, key_env = excluded.key_env, "
                "reasoning_format = excluded.reasoning_format, api_key = excluded.api_key",
                (
                    name,
                    base_url,
                    existing.get("key_env", "") if key_env is None else key_env,
                    existing.get("reasoning_format", "openai") if reasoning_format is None else reasoning_format,
                    existing.get("api_key", "") if api_key is None else api_key,
                ),
            )

    def delete(self, name: str) -> bool:
        with _tx(self.conn):
            cursor = self.conn.execute("DELETE FROM providers WHERE name = ?", (name,))
        return cursor.rowcount > 0


class SettingsRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM settings ORDER BY key")}


class TimePeriodRepo:
    DEFAULTS = [
        {"name": "白天", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"},
        {"name": "夜间", "days": [0, 1, 2, 3, 4, 5, 6], "start": "18:00", "end": "09:00"},
        {"name": "周末", "days": [5, 6], "start": "00:00", "end": "00:00"},
    ]

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> list[dict]:
        return [
            {"name": row["name"], "days": [int(day) for day in json.loads(row["days"])], "start": row["start"], "end": row["end"]}
            for row in self.conn.execute("SELECT * FROM time_periods ORDER BY rowid")
        ]

    def seed_defaults(self) -> None:
        settings = SettingsRepo(self.conn)
        if settings.get("time_periods_seeded"):
            return
        if not self.conn.execute("SELECT COUNT(*) FROM time_periods").fetchone()[0]:
            self.save(self.DEFAULTS)
        with _tx(self.conn):
            settings.set("time_periods_seeded", "1")

    def save(self, periods: list[dict]) -> None:
        validated = [TimePeriod.from_dict(period).to_dict() for period in periods]
        names = [period["name"] for period in validated]
        if len(names) != len(set(names)):
            raise ValueError("period names must be unique")
        with _tx(self.conn):
            self.conn.execute("DELETE FROM time_periods")
            for period in validated:
                self.conn.execute(
                    "INSERT INTO time_periods VALUES (?, ?, ?, ?)",
                    (period["name"], json.dumps(period["days"]), period["start"], period["end"]),
                )


class ChangeRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, ts: str, kind: str, detail: dict) -> None:
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO changes (ts, kind, detail) VALUES (?, ?, ?)",
                (ts, kind, json.dumps(detail, ensure_ascii=False)),
            )

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT id, ts, kind, detail FROM changes ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]


class EmailExists(Exception):
    pass


def _is_expired(value: str, now: datetime) -> bool:
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.astimezone()
    return when <= now


class UserRepo:
    PUBLIC_FIELDS = ("id", "email", "role", "status", "created_at", "last_login_at")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM users ORDER BY created_at, id")]

    def get(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_by_email(self, email: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE email = ?", (auth.normalize_email(email),)).fetchone()
        return dict(row) if row else None

    def public(self, user: dict) -> dict:
        return {key: user.get(key) for key in self.PUBLIC_FIELDS}

    def count_active_admins(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'").fetchone()[0])

    def create(self, email: str, password: str, role: str = "viewer") -> dict:
        email = auth.normalize_email(email)
        if self.get_by_email(email) is not None:
            raise EmailExists(email)
        user_id = uuid.uuid4().hex
        now = _now_iso()
        password_hash = auth.hash_password(password)
        try:
            with _tx(self.conn):
                self.conn.execute(
                    "INSERT INTO users (id, email, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, email, password_hash, role, "active", now, now),
                )
        except sqlite3.IntegrityError:
            raise EmailExists(email) from None
        return self.get(user_id)

    def update(self, user_id: str, *, email: str | None = None, role: str | None = None, status: str | None = None) -> dict | None:
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
                cursor = self.conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        except sqlite3.IntegrityError:
            raise EmailExists(auth.normalize_email(email or "")) from None
        return self.get(user_id) if cursor.rowcount else None

    def set_password(self, user_id: str, password: str) -> bool:
        password_hash = auth.hash_password(password)
        with _tx(self.conn):
            cursor = self.conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, _now_iso(), user_id),
            )
        return cursor.rowcount > 0

    def delete(self, user_id: str) -> bool:
        with _tx(self.conn):
            cursor = self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0

    def record_login(self, user_id: str) -> None:
        with _tx(self.conn):
            self.conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now_iso(), user_id))

    def seed_admin(self, email: str, password: str) -> bool:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return False
        self.create(email, password, role="admin")
        return True


class SessionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, user_id: str, ttl_hours: int) -> tuple[str, str]:
        token = auth.new_session_token()
        now = datetime.now().astimezone()
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO user_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (auth.session_token_hash(token), user_id, now.isoformat(timespec="seconds"), expires_at),
            )
        return token, expires_at

    def user_for_token(self, token: str) -> dict | None:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT s.expires_at AS session_expires_at, u.* FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (auth.session_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        expires_at = row["session_expires_at"]
        if _is_expired(expires_at, datetime.now().astimezone()):
            self.delete(token)
            return None
        user = {key: row[key] for key in row.keys() if key != "session_expires_at"}
        if user["status"] != "active":
            return None
        return {**UserRepo(self.conn).public(user), "expires_at": expires_at}

    def delete(self, token: str) -> None:
        with _tx(self.conn):
            self.conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (auth.session_token_hash(token),))

    def delete_for_user(self, user_id: str) -> None:
        with _tx(self.conn):
            self.conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))

    def purge_expired(self) -> None:
        now = datetime.now().astimezone()
        expired = [row["token_hash"] for row in self.conn.execute("SELECT token_hash, expires_at FROM user_sessions") if _is_expired(row["expires_at"], now)]
        if not expired:
            return
        with _tx(self.conn):
            self.conn.executemany("DELETE FROM user_sessions WHERE token_hash = ?", [(token_hash,) for token_hash in expired])
