"""POST /admin/try 草稿试算与 layout 独立存取（Task 6，设计 §6.2/§6.3）。

try 是纯 dry-run：不落库、不写文件、不发布快照、不写粘性、不增加真实
在途计数、不调用上游生成模型；响应携带目标/模型/命中规则/JEV 答案与
机器 trace（time/window/jev/question/rule/final/schedule/scheduled）。
layout 是前端私有 JSON：独立 GET/PUT，写入不触碰策略文档、行为版本或
已启用快照（§6.3）。
"""

from tests.policy_harness import make_policy_client
from tests.routing_fixtures import policy_dict


def _event_kinds(trace):
    return [event["event"] for event in trace]


def _create_main(client, policy_id="main", policy=None):
    created = client.post("/admin/policies", json={
        "id": policy_id, "policy": policy or policy_dict()})
    assert created.status_code == 201, created.text
    return created.json()["policy_resource"]


# --------------------------------------------------------------------------- #
# DTO：policy_id XOR policy
# --------------------------------------------------------------------------- #
def test_try_requires_exactly_one_policy_source_and_does_not_save(tmp_path):
    client, app = make_policy_client(tmp_path)
    for body in [
        {"text": "x"},
        {"text": "x", "policy_id": "main", "policy": policy_dict()},
    ]:
        assert client.post("/admin/try", json=body).status_code == 422
    response = client.post("/admin/try", json={
        "policy": policy_dict(), "text": "复杂电力抢修",
        "now": "2026-11-18T10:00:00+08:00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["fallback"] is False
    assert len([e for e in response.json()["trace"] if e["event"] == "jev"]) == 1
    assert client.get("/admin/policies").json()["policies"] == []


def test_try_rejects_unknown_fields_and_strict_value_types(tmp_path):
    client, app = make_policy_client(tmp_path)
    base = {"policy": policy_dict(), "text": "x"}
    assert client.post("/admin/try", json={**base, "bogus": 1}).status_code == 422
    # text 必须是字符串；session_id 同为严格字符串
    assert client.post("/admin/try", json={"policy": policy_dict(),
                                           "text": 42}).status_code == 422
    assert client.post("/admin/try", json={**base, "session_id": 7}).status_code == 422
    # now 必须是带 offset 的严格 ISO-8601 字符串
    for now in ("2026-11-18 10:00:00", "not-a-date", 123, "2026-11-18T10:00:00"):
        assert client.post("/admin/try", json={**base, "now": now}).status_code == 422, now
    # policy_id 必须是非空字符串；未知 policy 404
    assert client.post("/admin/try", json={"policy_id": "", "text": "x"}).status_code == 422
    assert client.post("/admin/try", json={"policy_id": "main", "text": "x"}).status_code == 404


def test_try_with_inline_policy_does_not_publish_or_touch_state(tmp_path):
    """内联草稿试算：不写文件、不发布、不动粘性与在途计数、不调用上游。"""
    client, app = make_policy_client(tmp_path)
    snapshot_before = app.state.snapshot
    response = client.post("/admin/try", json={
        "policy": policy_dict(), "text": "复杂电力抢修",
        "session_id": "s-1", "now": "2026-11-18T10:00:00+08:00",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # MockTransport 固定回答 route=轻量(0.8)：命中第一条可用规则「轻量请求」
    assert body["target"] == "fast"
    assert body["models"] == ["fast"]
    assert body["matched_rule"] == "轻量请求"
    assert body["fallback"] is False and body["reason"] is None
    assert set(body["answers"]) == {"route", "risk", "severity"}
    assert body["answers"]["route"]["probabilities"] == {"fast": 0.8, "strong": 0.2}
    assert body["answers"]["risk"] == {"yes": 0.1, "no": 0.9,
                                       "usage": {"input_tokens": 100, "output_tokens": 0},
                                       "duration_ms": body["answers"]["risk"]["duration_ms"]}
    assert body["answers"]["severity"]["value"] == 5.0

    kinds = _event_kinds(body["trace"])
    assert kinds.count("jev") == 1
    assert kinds.count("time") == 1 and kinds.count("window") == 1
    assert "question" in kinds and "rule" in kinds and "final" in kinds
    assert "schedule" in kinds and "scheduled" in kinds
    final = next(e for e in body["trace"] if e["event"] == "final")
    assert final == {"event": "final", "kind": "target", "target_id": "fast",
                     "reason": None}
    rule_events = [e for e in body["trace"] if e["event"] == "rule"]
    assert [e["matched"] for e in rule_events] == [False, False, True]
    jev_event = next(e for e in body["trace"] if e["event"] == "jev")
    assert jev_event["questions"] == ["route", "risk", "severity"]
    assert jev_event["failed"] is False and jev_event["error"] is None
    question_events = {e["question_id"]: e["answer"] for e in body["trace"]
                       if e["event"] == "question"}
    assert question_events["route"]["selected"] == "fast"
    assert question_events["risk"] == {"type": "noul", "yes": 0.1, "no": 0.9}
    assert question_events["severity"] == {"type": "score", "value": 5.0}

    # dry-run 边界：未保存、未发布、无粘性、无在途、无上游调用
    assert client.get("/admin/policies").json()["policies"] == []
    assert app.state.snapshot is snapshot_before
    assert len(app.state.affinity) == 0
    assert app.state.balancer.inflight("fast") == 0
    assert app.state.test_upstream_requests == []
    assert len(app.state.test_jev_requests) == 1


def test_try_with_policy_id_uses_stored_resource(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    response = client.post("/admin/try", json={
        "policy_id": "main", "text": "复杂电力抢修",
        "now": "2026-11-18T10:00:00+08:00"})
    assert response.status_code == 200, response.text
    assert response.json()["target"] == "fast"
    assert response.json()["matched_rule"] == "轻量请求"
    # 存量资源试算同样不落任何新文件
    assert [item["id"] for item in client.get("/admin/policies").json()["policies"]] == ["main"]


def test_try_structurally_invalid_policy_is_422(tmp_path):
    client, app = make_policy_client(tmp_path)
    broken = policy_dict()
    broken["rules"][0]["when"] = {"answer": "missing.yes", "gte": 0.8}
    response = client.post("/admin/try", json={"policy": broken, "text": "x"})
    assert response.status_code == 422, response.text
    issues = response.json()["detail"]
    assert next(item for item in issues
                if item["code"] == "unknown_answer_path")["path"] == "rules.0.when.answer"

    parse_failed = client.post("/admin/try", json={"policy": {"kind": "x"}, "text": "x"})
    assert parse_failed.status_code == 422
    assert parse_failed.json()["detail"][0]["code"] == "policy_parse_error"


def test_try_jev_failure_falls_back_with_reason(tmp_path):
    """JEV 返回 500：命中 fallback 目标，reason=jev_error，answers 为空。"""
    import httpx
    client, app = make_policy_client(tmp_path)

    def failing(request):
        if request.url.path == "/v1/systemone":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={})

    app.state.http._transport = httpx.MockTransport(failing)
    response = client.post("/admin/try", json={
        "policy": policy_dict(), "text": "复杂电力抢修",
        "now": "2026-11-18T10:00:00+08:00"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fallback"] is True
    assert body["reason"] == "jev_error"
    assert body["matched_rule"] is None
    assert body["target"] == "safe" and body["models"] == ["fast"]
    assert body["answers"] == {}
    kinds = _event_kinds(body["trace"])
    assert kinds.count("jev") == 1
    jev = next(e for e in body["trace"] if e["event"] == "jev")
    assert jev["failed"] is True and jev["error"]
    final = next(e for e in body["trace"] if e["event"] == "final")
    assert final["kind"] == "fallback" and final["reason"] == "jev_error"


def test_try_no_rule_matched_falls_back(tmp_path):
    """全部规则未命中：fallback 目标 + reason=no_rule_matched（JEV 正常）。"""
    client, app = make_policy_client(tmp_path)
    none = policy_dict()
    none["rules"] = []            # 空规则：不求值 JEV，直接兜底
    response = client.post("/admin/try", json={
        "policy": none, "text": "普通请求", "now": "2026-11-18T10:00:00+08:00"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fallback"] is True and body["reason"] == "no_rule_matched"
    assert body["target"] == "safe"
    assert body["answers"] == {}
    assert _event_kinds(body["trace"]).count("jev") == 0


def test_try_unregistered_target_model_is_503(tmp_path):
    """命中目标的模型未登记：调度无可用后端 → 503（不静默换目标）。"""
    client, app = make_policy_client(tmp_path)
    ghost = policy_dict()
    ghost["targets"]["fast"] = {"models": [{"model": "ghost-model"}]}
    response = client.post("/admin/try", json={
        "policy": ghost, "text": "复杂电力抢修",
        "now": "2026-11-18T10:00:00+08:00"})
    assert response.status_code == 503, response.text


# --------------------------------------------------------------------------- #
# layout：独立存取，不触碰行为版本（§6.3）
# --------------------------------------------------------------------------- #
def test_layout_roundtrip_and_policy_metadata_unchanged(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    _enable_response = client.post("/admin/policies/main/enable")
    assert _enable_response.status_code == 200
    before = client.get("/admin/policies/main").json()["policy_resource"]
    snapshot_before = app.state.snapshot
    revision_before = snapshot_before.snapshot_revision

    missing = client.get("/admin/policies/main/layout")
    assert missing.status_code == 200
    assert missing.json() == {}

    saved = client.put("/admin/policies/main/layout",
                       json={"panels": {"left": 120}})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"panels": {"left": 120}}
    assert client.get("/admin/policies/main/layout").json() == {"panels": {"left": 120}}

    after = client.get("/admin/policies/main").json()["policy_resource"]
    assert after["policy_revision"] == before["policy_revision"]
    assert after["document_sha256"] == before["document_sha256"]
    assert app.state.snapshot is snapshot_before
    assert app.state.snapshot.snapshot_revision == revision_before
    status = client.get("/admin/policies").json()["policy_status"]
    assert status["ready"] is True and status["stale"] is False


def test_layout_rejects_non_object_and_oversize(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    for body in ([1, 2], "text", 15):
        response = client.put("/admin/policies/main/layout", json=body)
        assert response.status_code == 422, body
        assert client.get("/admin/policies/main/layout").json() == {}

    oversize = {"panels": ["x" * 64] * 20000}     # > 1 MiB 编码后
    response = client.put("/admin/policies/main/layout", json=oversize)
    assert response.status_code == 422, response.text
    assert client.get("/admin/policies/main/layout").json() == {}

    # 未知 policy 的 layout 读写都是 404
    assert client.get("/admin/policies/missing/layout").status_code == 404
    assert client.put("/admin/policies/missing/layout",
                      json={"a": 1}).status_code == 404


def test_layout_deleted_with_policy_and_not_shared(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client, policy_id="one")
    _create_main(client, policy_id="two")
    assert client.put("/admin/policies/one/layout",
                      json={"panels": {"left": 10}}).status_code == 200
    assert client.put("/admin/policies/two/layout",
                      json={"panels": {"left": 20}}).status_code == 200
    assert client.delete("/admin/policies/one").status_code == 200
    assert client.get("/admin/policies/one/layout").status_code == 404
    assert client.get("/admin/policies/two/layout").json() == {"panels": {"left": 20}}
