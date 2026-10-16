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
