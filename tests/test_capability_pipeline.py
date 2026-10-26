"""Natural-language capability routing: pack, snapshot, tagger, health, admin."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.decision.pack import (CAPABILITY_SIGNAL, CapabilityPackError,
                                  build_pack, capability_of_model, empty_pack,
                                  freeze_pack, is_configured, normalize_capability,
                                  pack_revision)
from router.pipeline.health import check_pipeline
from router.pipeline.model import Station, TreeNode
from router.pipeline.scope import SignalScope, compute_signal_scope
from router.pipeline.snapshot import (REVISION_CAPABILITY, build_snapshot,
                                      ensure_capability_revision, load_snapshot)
from router.signals.tagger import TagRequest
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, SettingsRepo, seed_default

from tests.test_jev import stub_tagger

MODELS = [
    {"id": "qwen3-8b", "upstream": "qwen3-8b", "provider": "local",
     "profile": json.dumps({"capability": "适合短翻译、抽取、事实问答；8k 上下文"})},
    {"id": "qwen3-32b", "upstream": "qwen3-32b", "provider": "local",
     "profile": json.dumps({"capability": "适合算法实现、SQL、局部调试"})},
    {"id": "deepseek-v3", "upstream": "deepseek-v3", "provider": "local",
     "profile": json.dumps({"capability": "适合复杂推理与分布式设计"})},
    {"id": "plain-model", "upstream": "plain-model", "provider": "local",
     "profile": "{}"},
]


def pack(**settings):
    merged = {}
    return build_pack(MODELS, {"capability_instructions": "挑一个模型",
                               **settings})


# ── pack building ────────────────────────────────────────────────────────────
def test_build_pack_only_models_with_capability_text():
    built = build_pack(MODELS)
    assert list(built["candidates"]) == ["qwen3-8b", "qwen3-32b", "deepseek-v3"]
    assert built["candidates"]["qwen3-32b"].startswith("适合算法")
    assert "plain-model" not in built["candidates"]
    assert built["min_prob"] == pytest.approx(0.45)
    assert built["min_margin"] == pytest.approx(0.10)
    assert built["fallback_model"] is None
    assert is_configured(built) and built["fingerprint"] == pack_revision(built)


def test_build_pack_reads_policy_settings():
    built = pack(capability_min_prob="0.7", capability_min_margin=0.2,
                 capability_fallback_model="deepseek-v3")
    assert built["min_prob"] == pytest.approx(0.7)
    assert built["min_margin"] == pytest.approx(0.2)
    assert built["fallback_model"] == "deepseek-v3"
    assert built["instructions"] == "挑一个模型"


def test_build_pack_rejects_bad_settings():
    with pytest.raises(CapabilityPackError):
        build_pack(MODELS, {"capability_min_prob": "1.5"})
    with pytest.raises(CapabilityPackError):
        build_pack(MODELS, {"capability_min_prob": "abc"})


def test_empty_pack_is_unconfigured_and_has_no_revision():
    built = empty_pack()
    assert not is_configured(built)
    assert pack_revision(built) == ""
    assert freeze_pack(built)["candidates"] == {}


def test_capability_of_model_tolerates_bad_json_and_bad_type():
    assert capability_of_model({"profile": "not json"}) == ""
    assert capability_of_model({"profile": None}) == ""
    assert capability_of_model({"profile": {"capability": 3}}) == ""
    assert capability_of_model({"profile": {"capability": "  x  "}}) == "x"


def test_normalize_capability_rejects_long_text_and_non_string():
    with pytest.raises(CapabilityPackError):
        normalize_capability(123)
    with pytest.raises(CapabilityPackError):
        normalize_capability("x" * 2001)


def test_pack_revision_is_order_insensitive_for_candidates():
    left = build_pack(MODELS[:3])
    right = build_pack(list(reversed(MODELS[:3])))
    assert pack_revision(left) == pack_revision(right)
    changed = build_pack([{**MODELS[0], "profile": json.dumps(
        {"capability": "别的说明"})}] + MODELS[1:3])
    assert pack_revision(changed) != pack_revision(left)


# ── snapshot ─────────────────────────────────────────────────────────────────
def test_snapshot_carries_capability_pack_and_revision():
    built = pack(capability_fallback_model="deepseek-v3")
    snap = build_snapshot(stations=[], backends={}, pipeline_version=3,
                          jev_trim=True, capability=built)
    assert snap.capability["candidates"] == built["candidates"]
    assert snap.capability_revision == pack_revision(built)
    unconfigured = build_snapshot(stations=[], backends={}, pipeline_version=3,
                                  jev_trim=True)
    assert unconfigured.capability_revision == ""
    assert unconfigured.capability["candidates"] == {}


def test_load_snapshot_builds_capability_from_the_catalog(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    ModelRepo(conn).update(
        "qwen3-8b", profile=json.dumps({"capability": "短文翻译"}))
    SettingsRepo(conn).set("capability_fallback_model", "qwen3-8b")
    conn.commit()
    snap = load_snapshot(conn, jev_trim=True)
    assert snap.capability["candidates"]["qwen3-8b"] == "短文翻译"
    assert snap.capability["fallback_model"] == "qwen3-8b"
    assert snap.capability_revision
    conn.close()


def test_ensure_capability_revision_is_content_addressed(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    built = pack()
    first = ensure_capability_revision(conn, built)
    second = ensure_capability_revision(conn, built)
    assert first == second == pack_revision(built)
    rows = conn.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = ?",
                        (REVISION_CAPABILITY,)).fetchone()[0]
    assert rows == 1
    changed = pack(capability_min_prob="0.6")
    ensure_capability_revision(conn, changed)
    assert conn.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = ?",
                        (REVISION_CAPABILITY,)).fetchone()[0] == 2
    conn.close()


# ── scope + health ───────────────────────────────────────────────────────────
def _tree(*children):
    return Station(id="s", name="s", root=TreeNode(
        id="root", signal=None, kind="fork", children=list(children)))


def test_scope_includes_capability_route_when_the_tree_reads_it():
    station = _tree(TreeNode(id="cap", signal=CAPABILITY_SIGNAL,
                             value="qwen3-32b", conf_min=0.1))
    scope = compute_signal_scope([station])
    assert CAPABILITY_SIGNAL in scope.signals


def test_health_accepts_known_capability_candidate():
    station = _tree(TreeNode(id="cap", signal=CAPABILITY_SIGNAL,
                             value="qwen3-32b", conf_min=0.1))
    issues = check_pipeline([station], {"qwen3-32b"},
                            capability_candidates={"qwen3-8b", "qwen3-32b"})
    assert not [i for i in issues if i.code.startswith("unknown_capability")
                or i.code == "capability_unconfigured"]


def test_health_rejects_unknown_capability_candidate():
    station = _tree(TreeNode(id="cap", signal=CAPABILITY_SIGNAL,
                             value="nope", conf_min=0.1))
    issues = check_pipeline([station], {"nope"},
                            capability_candidates={"qwen3-32b"})
    assert "unknown_capability_model" in {i.code for i in issues}


def test_health_rejects_capability_tree_without_candidates():
    station = _tree(TreeNode(id="cap", signal=CAPABILITY_SIGNAL,
                             value="qwen3-32b", conf_min=0.1))
    issues = check_pipeline([station], {"qwen3-32b"},
                            capability_candidates=set())
    assert "capability_unconfigured" in {i.code for i in issues}


# ── tagger ───────────────────────────────────────────────────────────────────
def _capability_probs(record):
    keys = record["answer_keys"]
    return [0.7 if key == keys[0] else 0.15 for key in keys]


def _score_with_capability(records):
    out = []
    for record in records:
        if record["kind"] == "choice" and record["answer_keys"] and \
                record["answer_keys"][0] in {"qwen3-8b", "qwen3-32b",
                                             "deepseek-v3"}:
            out.append(_capability_probs(record))
        else:
            out.append([0.5] * len(record["answer_keys"]))
    return out


def _capability_scope():
    return SignalScope(signals=frozenset({CAPABILITY_SIGNAL}),
                       pii_types=frozenset(), full=False, reason="test")


def test_tagger_emits_capability_signal_with_distribution(monkeypatch):
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", _score_with_capability)
    built = pack()
    result = asyncio.run(tagger.tag(TagRequest(
        text="写一段区间合并代码", n_messages=2, scope=_capability_scope(),
        capability=built)))
    signal = result.signals[CAPABILITY_SIGNAL]
    assert signal["value"] == "qwen3-8b"
    assert signal["is_rule"] is False
    assert signal["prob_by_value"]["qwen3-8b"] == pytest.approx(0.7)
    assert signal["p_top"] == pytest.approx(0.7)
    assert signal["margin"] == pytest.approx(0.55)
    assert signal["reason"] == "decision_model"
    assert signal["fingerprint"] == built["fingerprint"]
    assert result.tagger_info["capability_fingerprint"] == built["fingerprint"]


def test_tagger_capability_gate_falls_open_to_the_fallback(monkeypatch):
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", _score_with_capability)
    built = pack(capability_min_prob="0.95", capability_fallback_model="deepseek-v3")
    result = asyncio.run(tagger.tag(TagRequest(
        text="x", n_messages=2, scope=_capability_scope(), capability=built)))
    signal = result.signals[CAPABILITY_SIGNAL]
    assert signal["value"] == "deepseek-v3"
    assert signal["fallback_used"] is True
    assert signal["reason"] == "low_confidence"


def test_tagger_omits_capability_signal_when_scope_does_not_read_it(monkeypatch):
    tagger = stub_tagger()
    records = []

    def score(rows):
        records.extend(rows)
        return [[0.5] * len(row["answer_keys"]) for row in rows]

    monkeypatch.setattr(tagger, "_score", score)
    result = asyncio.run(tagger.tag(TagRequest(
        text="x", n_messages=2,
        scope=SignalScope(signals=frozenset({"domain"}), pii_types=frozenset(),
                          full=False, reason="test"),
        capability=pack())))
    assert CAPABILITY_SIGNAL not in result.signals
    assert all(row["question"] != pack()["instructions"] for row in records)


def test_tagger_without_capability_pack_unchanged(monkeypatch):
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", _score_with_capability)
    result = asyncio.run(tagger.tag(TagRequest(
        text="x", n_messages=2, scope=_capability_scope())))
    assert CAPABILITY_SIGNAL not in result.signals


# ── admin API ────────────────────────────────────────────────────────────────
def _client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"))))


def test_model_capability_round_trip(tmp_path):
    c = _client(tmp_path)
    r = c.put("/admin/models/qwen3-8b",
              json={"profile": {"capability": "适合短翻译和抽取"}})
    assert r.status_code == 200
    assert r.json()["model"]["capability"] == "适合短翻译和抽取"
    signals = c.get("/admin/signals").json()["signals"]
    capability = next(s for s in signals if s["key"] == CAPABILITY_SIGNAL)
    assert capability["free"] is False
    assert [v[0] for v in capability["values"]] == ["qwen3-8b"]
    body = c.get("/admin/capability").json()
    assert body["configured"] is True
    assert body["candidates"][0]["id"] == "qwen3-8b"
    assert body["instructions"]


def test_model_capability_rejects_non_string(tmp_path):
    r = _client(tmp_path).put("/admin/models/qwen3-8b",
                              json={"profile": {"capability": 42}})
    assert r.status_code == 422


def test_capability_policy_round_trip(tmp_path):
    c = _client(tmp_path)
    c.put("/admin/models/qwen3-32b", json={"profile": {"capability": "算法"}})
    c.put("/admin/models/deepseek-v3",
          json={"profile": {"capability": "复杂推理"}})
    r = c.put("/admin/capability", json={
        "instructions": "挑最合适的模型",
        "min_prob": 0.6, "min_margin": 0.2,
        "fallback_model": "deepseek-v3"})
    assert r.status_code == 200
    body = r.json()
    assert body["instructions"] == "挑最合适的模型"
    assert body["min_prob"] == pytest.approx(0.6)
    assert body["min_margin"] == pytest.approx(0.2)
    assert body["fallback_model"] == "deepseek-v3"
    assert body["revision"]
    # GET reflects it and the snapshot was swapped for the hot path
    got = c.get("/admin/capability").json()
    assert got["fingerprint"] == body["fingerprint"]


def test_capability_policy_rejects_unknown_fallback(tmp_path):
    r = _client(tmp_path).put("/admin/capability",
                              json={"fallback_model": "nope"})
    assert r.status_code == 422


def test_pipeline_save_accepts_capability_rows_with_a_candidate(tmp_path):
    c = _client(tmp_path)
    c.put("/admin/models/qwen3-32b", json={"profile": {"capability": "算法"}})
    stations = c.get("/admin/pipeline").json()["stations"]
    subject = stations[2]
    subject["root"]["children"].append({
        "id": "n3cap", "signal": CAPABILITY_SIGNAL, "value": "qwen3-32b",
        "conf_min": 0.2, "kind": "leaf", "action": "route",
        "model": "qwen3-32b", "min_score": None, "weights": [],
        "children": [], "reasoning": None})
    r = c.put("/admin/pipeline", json={"stations": stations})
    assert r.status_code == 200, r.text


def test_pipeline_save_rejects_unknown_capability_value(tmp_path):
    c = _client(tmp_path)
    c.put("/admin/models/qwen3-32b", json={"profile": {"capability": "算法"}})
    stations = c.get("/admin/pipeline").json()["stations"]
    stations[2]["root"]["children"].append({
        "id": "n3cap", "signal": CAPABILITY_SIGNAL, "value": "deepseek-v3",
        "conf_min": 0.2, "kind": "leaf", "action": "route",
        "model": "deepseek-v3", "min_score": None, "weights": [],
        "children": [], "reasoning": None})
    r = c.put("/admin/pipeline", json={"stations": stations})
    assert r.status_code == 422
    assert any(i["code"] == "unknown_capability_model" for i in r.json()["detail"])


def test_capability_candidate_cannot_be_deleted_while_referenced(tmp_path):
    c = _client(tmp_path)
    c.put("/admin/models/qwen3-32b", json={"profile": {"capability": "算法"}})
    stations = c.get("/admin/pipeline").json()["stations"]
    stations[2]["root"]["children"].append({
        "id": "n3cap", "signal": CAPABILITY_SIGNAL, "value": "qwen3-32b",
        "conf_min": 0.2, "kind": "leaf", "action": "route",
        "model": "qwen3-32b", "min_score": None, "weights": [],
        "children": [], "reasoning": None})
    assert c.put("/admin/pipeline", json={"stations": stations}).status_code == 200
    r = c.delete("/admin/models/qwen3-32b")
    assert r.status_code == 409
    assert any(ref["via"] == "capability" for ref in r.json()["detail"]["references"])


def test_try_returns_capability_provenance(tmp_path):
    c = _client(tmp_path)
    c.put("/admin/models/qwen3-8b", json={"profile": {"capability": "翻译"}})
    body = c.post("/admin/try", json={"text": "把 good morning 翻成中文"}).json()
    assert body["capability"]["configured"] is True
    assert body["capability"]["fingerprint"]
    # heuristic tagger cannot produce the signal -> no selection, no crash
    assert "capability_route" not in body["signals"]
