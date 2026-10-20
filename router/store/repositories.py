"""Repositories: one per aggregate, plain SQL, no ORM.

The pipeline (stations) is stored as one JSON document per station and saved by
wholesale replacement inside one transaction — station edits are rare and small,
and replacement keeps ordering and deletion trivially correct.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from router.pipeline.model import Station, TreeNode, Weight, station_from_dict, \
    station_to_dict


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


# Seed thresholds per tagger mode.
#
# Every condition's confidence floor defaults to 0.70 (2026-10-19 product
# decision). Rule signals are deterministic and carry no floor. Calibration
# presets were dropped; all taggers share the same defaults.
CALIBRATIONS = {"default": {}}


def _leaf(node_id: str, signal: str | None, value: str,
          conf_min: float | None, *, kind: str = "leaf",
          action: str = "route", model: str | None = None,
          min_score: float | None = None,
          weights: list[Weight] | None = None,
          children: list[TreeNode] | None = None) -> TreeNode:
    return TreeNode(id=node_id, signal=signal, value=value, conf_min=conf_min,
                    kind=kind, action=action, model=model,
                    min_score=min_score, weights=weights or [],
                    children=children or [])


def default_pipeline(calibration: str = "default") -> list[Station]:
    """Four independent station trees: gate / keyword gate / subject fork / fallback.

    All model-signal conditions default to a 0.70 confidence floor; the
    ``calibration`` argument is kept for API compatibility."""
    c = {"conf": 0.70}
    safety = Station(id=_uid(), type="gate", name="① 安全闸", root=_leaf(
        "n1", None, "", None, kind="fork", children=[
            _leaf("n1a", "jailbreak", "jailbreak", c["conf"],
                  action="block"),
            _leaf("n1b", "pii", "cn_id_card", c["conf"], action="block"),
        ]))
    keyword = Station(id=_uid(), type="gate", name="② 关键词直达", root=_leaf(
        "n2", None, "", None, kind="fork", children=[
            _leaf("n2a", "keyword", "医疗急症", None,   # rule signal: no floor
                  action="route", model="qwen3-8b-medical"),
        ]))
    subject = Station(id=_uid(), type="split", name="③ 学科 → 难度", root=_leaf(
        "n3", None, "", None, kind="fork", children=[
            _leaf("n3a", "domain", "health", c["conf"], kind="fork",
                  children=[
                      _leaf("n3a1", "complexity", "hard", c["conf"],
                            action="route", model="qwen3-8b-medical"),
                      _leaf("n3a2", "complexity", "easy", c["conf"],
                            action="route", model="qwen3-0.6b-medical"),
                  ]),
            _leaf("n3b", "domain", "computer_science", c["conf"], kind="fork",
                  children=[
                      _leaf("n3b1", "complexity", "hard", c["conf"],
                            action="route", model="deepseek-v3"),
                  ]),
        ]))
    fallback = Station(id=_uid(), type="gate", name="④ 兜底", root=_leaf(
        "n4", None, "", None, action="route", model="qwen3-8b"))
    return [safety, keyword, subject, fallback]


def seed_default(conn: sqlite3.Connection, calibration: str = "heuristic") -> None:
    if conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]:
        return                                  # idempotent: only seed an empty DB
    conn.execute("INSERT OR IGNORE INTO providers VALUES ('local', "
                 "'http://127.0.0.1:9000', '')")
    for mid, upstream, provider, profile in DEFAULT_MODELS:
        conn.execute("INSERT OR IGNORE INTO models VALUES (?, ?, ?, ?)",
                     (mid, upstream, provider, json.dumps(profile)))
    for name, words in DEFAULT_KEYWORD_GROUPS.items():
        conn.execute("INSERT OR IGNORE INTO keyword_groups VALUES (?, ?)",
                     (name, json.dumps(words, ensure_ascii=False)))
    StationRepo(conn).save(default_pipeline(calibration))
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
                    "INSERT INTO stations (id, ord, type, name, tree) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (station.id, si, station.type, station.name,
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


class TimeWindowRepo:
    """时段规则：白天/夜间起止小时与周末定义（settings 表 JSON，键 time_window_rules）。

    weekend_days 用 Python weekday 数字（周一=0）。命中优先级：周末 > 白天 > 夜间。"""

    KEY = "time_window_rules"
    DEFAULTS = {"day_start": 9, "night_start": 18, "weekend_days": [5, 6]}

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> dict:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?",
                                (self.KEY,)).fetchone()
        cfg = dict(self.DEFAULTS)
        if row and row["value"]:
            try:
                cfg.update(json.loads(row["value"]))
            except (ValueError, TypeError):
                pass
        try:
            cfg["day_start"] = min(23, max(0, int(cfg.get("day_start", 9))))
            cfg["night_start"] = min(24, max(1, int(cfg.get("night_start", 18))))
        except (TypeError, ValueError):
            cfg["day_start"], cfg["night_start"] = 9, 18
        cfg["weekend_days"] = sorted({int(d) for d in cfg.get("weekend_days", [])
                                      if int(d) in range(7)})
        if cfg["day_start"] >= cfg["night_start"]:
            cfg["day_start"], cfg["night_start"] = 9, 18
        return cfg

    def save(self, cfg: dict) -> None:
        SettingsRepo(self.conn).set(self.KEY, json.dumps(cfg))
        self.conn.commit()


class SessionRepo:
    """Per-session state: last-turn embedding + last routed model (M4)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, session_id: str) -> dict:
        row = self.conn.execute(
            "SELECT last_vec, last_model FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()
        if not row:
            return {"vec": None, "model": None}
        vec = None
        if row["last_vec"]:
            try:
                parsed = json.loads(row["last_vec"])
                if isinstance(parsed, list):
                    vec = [float(x) for x in parsed if isinstance(x, (int, float))]
            except ValueError:
                vec = None                     # corrupt row: behave as "no history"
        return {"vec": vec, "model": row["last_model"]}

    def get_vec(self, session_id: str) -> list[float] | None:
        return self.get(session_id)["vec"]

    def put_vec(self, session_id: str, vec: list[float], updated: str) -> None:
        self.put(session_id, vec, None, updated)

    def put(self, session_id: str, vec: list[float] | None, model: str | None,
            updated: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (session_id, last_vec, updated, last_model) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_vec = COALESCE(excluded.last_vec, sessions.last_vec), "
                "last_model = COALESCE(excluded.last_model, sessions.last_model), "
                "updated = excluded.updated",
                (session_id, json.dumps(vec) if vec else None, updated, model))



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

    def recent(self, limit: int = 20) -> list[dict]:
        """Newest requests with their signal pack (console 'calculation log')."""
        rows = self.conn.execute(
            "SELECT id, ts, text, decision, model, node_id, branch_id, score, "
            "latency_ms, tree_path FROM requests ORDER BY ts DESC, id DESC "
            "LIMIT ?", (limit,))
        out = []
        for r in rows:
            pack = {}
            for s in self.conn.execute(
                    "SELECT signal, value, confidence, is_rule FROM request_signals "
                    "WHERE request_id = ?", (r["id"],)):
                try:
                    value = json.loads(s["value"]) if s["value"] is not None else None
                except ValueError:
                    value = s["value"]
                pack[s["signal"]] = {"value": value,
                                     "confidence": s["confidence"],
                                     "is_rule": bool(s["is_rule"])}
            out.append({**dict(r), "signals": pack})
        return out

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


