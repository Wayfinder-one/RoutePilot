"""Repositories: one per aggregate, plain SQL, no ORM. The pipeline is stored
relationally (nodes/branches/branch_rows) and saved by wholesale replacement
inside one transaction — pipeline edits are rare and small, and replacement
keeps ordering and deletion trivially correct.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from router.pipeline.model import Branch, Node, Row


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


def default_pipeline() -> list[Node]:
    # Thresholds are calibrated for the heuristic tagger's confidence cap (0.5,
    # design doc section 3.5); M2 recalibrates them once the real checkpoint
    # produces full-range confidences.
    return [
        Node(id=_uid(), type="gate", name="① 安全闸", branches=[
            Branch(id=_uid(), name="拦越狱",
                   rows=[Row("jailbreak", "jailbreak", conf_min=0.45)],
                   action="block"),
            Branch(id=_uid(), name="拦身份证",
                   rows=[Row("pii", "cn_id_card", conf_min=0.45)],
                   action="block"),
        ]),
        Node(id=_uid(), type="gate", name="② 关键词直达", branches=[
            Branch(id=_uid(), name="医疗急症",
                   rows=[Row("keyword", "医疗急症", conf_min=0.5)],
                   action="route", model="qwen3-8b-medical"),
        ]),
        Node(id=_uid(), type="score", name="③ 领域 × 难度 打分", branches=[
            Branch(id=_uid(), name="8B 医疗", model="qwen3-8b-medical",
                   min_score=0.45,
                   rows=[Row("domain", "health", weight=0.5),
                         Row("complexity", "hard", weight=0.5)]),
            Branch(id=_uid(), name="0.6B 医疗", model="qwen3-0.6b-medical",
                   min_score=0.40,
                   rows=[Row("domain", "health", weight=0.5),
                         Row("complexity", "easy", weight=0.5)]),
            Branch(id=_uid(), name="DeepSeek 编程", model="deepseek-v3",
                   min_score=0.45,
                   rows=[Row("domain", "computer_science", weight=0.4),
                         Row("complexity", "hard", weight=0.6)]),
        ]),
        Node(id=_uid(), type="gate", name="④ 兜底", branches=[
            Branch(id=_uid(), name="默认", rows=[], action="route",
                   model="qwen3-8b"),
        ]),
    ]


def seed_default(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]:
        return                                  # idempotent: only seed an empty DB
    conn.execute("INSERT OR IGNORE INTO providers VALUES ('local', "
                 "'http://127.0.0.1:9000', '')")
    for mid, upstream, provider, profile in DEFAULT_MODELS:
        conn.execute("INSERT OR IGNORE INTO models VALUES (?, ?, ?, ?)",
                     (mid, upstream, provider, json.dumps(profile)))
    for name, words in DEFAULT_KEYWORD_GROUPS.items():
        conn.execute("INSERT OR IGNORE INTO keyword_groups VALUES (?, ?)",
                     (name, json.dumps(words, ensure_ascii=False)))
    PipelineRepo(conn).save(default_pipeline())
    conn.commit()


class PipelineRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> list[Node]:
        nodes = []
        for n in self.conn.execute("SELECT * FROM nodes ORDER BY ord"):
            branches = []
            for b in self.conn.execute(
                    "SELECT * FROM branches WHERE node_id = ? ORDER BY ord",
                    (n["id"],)):
                rows = [Row(signal=r["signal"], value=r["value"],
                            weight=r["weight"], conf_min=r["conf_min"])
                        for r in self.conn.execute(
                            "SELECT * FROM branch_rows WHERE branch_id = ? "
                            "ORDER BY ord", (b["id"],))]
                branches.append(Branch(id=b["id"], name=b["name"], rows=rows,
                                       action=b["action"], model=b["model"],
                                       min_score=b["min_score"]))
            nodes.append(Node(id=n["id"], type=n["type"], name=n["name"],
                              branches=branches))
        return nodes

    def save(self, nodes: list[Node]) -> None:
        with self.conn:                          # one transaction: replace all
            self.conn.execute("DELETE FROM branch_rows")
            self.conn.execute("DELETE FROM branches")
            self.conn.execute("DELETE FROM nodes")
            for ni, n in enumerate(nodes):
                self.conn.execute("INSERT INTO nodes VALUES (?, ?, ?, ?)",
                                  (n.id, ni, n.type, n.name))
                for bi, b in enumerate(n.branches):
                    self.conn.execute(
                        "INSERT INTO branches VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (b.id, n.id, bi, b.name, b.action, b.model, b.min_score))
                    for ri, r in enumerate(b.rows):
                        self.conn.execute(
                            "INSERT INTO branch_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_uid(), b.id, ri, r.signal, r.value,
                             r.weight, r.conf_min))
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


class ProviderRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[dict]:
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM providers ORDER BY name")]

    def upsert(self, name: str, base_url: str, key_env: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO providers VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_url = excluded.base_url, "
                "key_env = excluded.key_env", (name, base_url, key_env))


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
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?",
                                (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def all(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in
                self.conn.execute("SELECT key, value FROM settings ORDER BY key")}


class SessionRepo:
    """Per-session last-turn embedding (JSON list of floats)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_vec(self, session_id: str) -> list[float] | None:
        row = self.conn.execute("SELECT last_vec FROM sessions WHERE session_id = ?",
                                (session_id,)).fetchone()
        if not row or not row["last_vec"]:
            return None
        try:
            vec = json.loads(row["last_vec"])
        except ValueError:
            return None                       # corrupt row: behave as "no history"
        if not isinstance(vec, list):
            return None
        return [float(x) for x in vec if isinstance(x, (int, float))]

    def put_vec(self, session_id: str, vec: list[float], updated: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_vec = excluded.last_vec, "
                "updated = excluded.updated",
                (session_id, json.dumps(vec), updated))


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


class RequestRepo:
    """Read side for replay evaluation: stored requests + their signal packs."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def replayable(self, since: str, limit: int = 5000) -> list[dict]:
        """Route decisions only (block/none carry no model to compare)."""
        rows = self.conn.execute(
            "SELECT id, ts, text, model FROM requests "
            "WHERE decision = 'route' AND model IS NOT NULL AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?", (since, limit))
        out = []
        for r in rows:
            signals = self.conn.execute(
                "SELECT signal, value, confidence, is_rule FROM request_signals "
                "WHERE request_id = ?", (r["id"],))
            pack = {}
            for s in signals:
                try:
                    value = json.loads(s["value"]) if s["value"] is not None else None
                except ValueError:
                    value = s["value"]
                pack[s["signal"]] = {"value": value,
                                     "confidence": s["confidence"],
                                     "is_rule": bool(s["is_rule"])}
            if pack:
                out.append({"id": r["id"], "ts": r["ts"], "model": r["model"],
                            "signals": pack})
        return out


