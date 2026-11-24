import copy
import hashlib
import json

import pytest

from router.routing.document import (
    graph_document,
    graph_document_bytes,
    parse_graph_document,
)
from router.routing.tree import tree_behavior_dict, tree_from_dict, tree_revision_of
from tests.routing_fixtures import runtime_dict, tree_dict

RUNTIME_FIELDS = ("backends", "jev", "jev_window", "time_periods")


def load(content):
    return parse_graph_document(
        content,
        tree_id="portable",
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )


def portable_tree_dict():
    raw = tree_dict()
    raw.update(runtime_dict())
    return raw


def test_self_contained_document_roundtrip_preserves_runtime_without_keys():
    source = tree_from_dict(portable_tree_dict())

    content = graph_document_bytes(source)
    document = json.loads(content)
    assert document["backends"]["models"][0]["upstream"] == "json-upstream"
    assert document["backends"]["models"][0]["profile"]["capability"] == "来自 JSON 的能力"
    assert document["jev"]["id"] == "json-jev-service"
    assert document["jev"]["model"] == "json-jev"
    assert document["jev"]["key_env"] == "ROUTER_JEV_API_KEY"
    assert document["jev_window"] == {"input_tokens": 1024, "context_tokens": 4096}
    assert document["time_periods"][0]["name"] == "peak"
    assert "api_key" not in document["backends"]["providers"][0]
    assert "api_key" not in document["jev"]

    # 运行配置进入行为表单：JEV id/key_env 与窗口都参与 revision。
    behavior = tree_behavior_dict(source)
    assert behavior["jev"]["id"] == "json-jev-service"
    assert behavior["jev"]["key_env"] == "ROUTER_JEV_API_KEY"
    assert behavior["jev"]["timeout_seconds"] == 5

    loaded = load(content)
    assert loaded.runtime == source.runtime
    assert loaded.tree_revision == tree_revision_of(source)


def test_tree_requires_a_runtime_and_runtime_changes_revision():
    raw = tree_dict()
    for field in RUNTIME_FIELDS:
        raw.pop(field)
    with pytest.raises(ValueError, match="runtime fields must appear together"):
        tree_from_dict(raw)

    first = tree_from_dict(tree_dict())
    changed = tree_dict()
    changed["jev_window"] = {"input_tokens": 2048, "context_tokens": 4096}
    second = tree_from_dict(changed)
    assert tree_revision_of(first) != tree_revision_of(second)


@pytest.mark.parametrize("field", RUNTIME_FIELDS)
def test_partial_runtime_fields_are_rejected(field):
    """四个运行字段必须成组出现：缺任意一个都拒绝解析。"""
    raw = portable_tree_dict()
    del raw[field]

    with pytest.raises(ValueError, match="runtime fields must appear together"):
        tree_from_dict(raw)


def test_jev_id_is_required():
    raw = portable_tree_dict()
    del raw["jev"]["id"]
    with pytest.raises(ValueError, match="jev.id"):
        tree_from_dict(raw)

    raw = portable_tree_dict()
    raw["jev"]["api_key"] = "must-not-be-persisted"
    with pytest.raises(ValueError, match="secret field is not allowed"):
        tree_from_dict(raw)

    raw = portable_tree_dict()
    raw["jev"]["key_present"] = True
    with pytest.raises(ValueError, match="secret field is not allowed"):
        tree_from_dict(raw)


#: ``_jev`` 边界的正反用例：key_env 必须是环境变量名，timeout 必须在 [0.1,120]，
#: 未知字段一律拒绝。``tree_from_dict`` 会走到 ``runtime_from_dict`` 的 ``_jev``。
@pytest.mark.parametrize(("field", "value", "message"), [
    ("key_env", "bad-env", "invalid JEV key_env"),
    ("key_env", "1ABC", "invalid JEV key_env"),
    ("timeout_seconds", 0, "timeout_seconds must be in"),
    ("timeout_seconds", 121, "timeout_seconds must be in"),
    ("timeout_seconds", True, "timeout_seconds must be in"),
    ("foo", "bar", "jev has unknown fields: foo"),
])
def test_jev_boundary_validation_rejects_invalid_fields(field, value, message):
    raw = portable_tree_dict()
    raw["jev"][field] = value

    with pytest.raises(ValueError, match=message):
        tree_from_dict(raw)


def test_jev_valid_boundary_fields_roundtrip_through_document():
    """合法的 id/key_env/timeout/instructions/state_template 完整往返，且无密钥字段。"""
    raw = portable_tree_dict()
    raw["jev"] = {
        "id": "roundtrip-jev",
        "base_url": "http://jev-roundtrip.test",
        "model": "jev-roundtrip",
        "key_env": "ROUNDTRIP_JEV_API_KEY",
        "timeout_seconds": 42.5,
        "instructions": "自定义决策提示词",
        "state_template": "请求：{request}",
    }
    source = tree_from_dict(raw)

    document = graph_document(source)

    # document 里的 jev 就是 snapshot_dict() 形状：只有非密钥字段。
    assert document["jev"] == source.runtime.jev.snapshot_dict()
    assert set(document["jev"]) == {
        "id", "base_url", "model", "key_env", "timeout_seconds",
        "instructions", "state_template",
    }
    assert "api_key" not in document["jev"]

    loaded = load(graph_document_bytes(source))

    assert loaded.runtime == source.runtime
    assert loaded.runtime.jev.snapshot_dict() == document["jev"]
    assert "api_key" not in json.loads(graph_document_bytes(source))["jev"]


@pytest.mark.parametrize(("path", "message"), [
    (("backends", "providers", 0, "api_key"), "secret field is not allowed"),
    (("jev", "api_key"), "secret field is not allowed"),
])
def test_runtime_rejects_secret_fields(path, message):
    raw = portable_tree_dict()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "must-not-be-persisted"

    with pytest.raises(ValueError, match=message):
        tree_from_dict(raw)


#: 大小写/连字符/下划线变体都必须命中同一条密钥规则（Julia/K8s 风格键名也拒绝）。
SECRET_KEY_SPELLINGS = ["api_key", "API-KEY", "authorization", "key_present", "key_source"]


@pytest.mark.parametrize("key", SECRET_KEY_SPELLINGS)
@pytest.mark.parametrize("place", ["provider", "jev", "profile", "profile_array", "period"])
def test_runtime_rejects_secret_keys_recursively(key, place):
    """profile 是合法 JSON 元数据容器，但任何层级的密钥字段都不得进入 runtime。"""
    raw = portable_tree_dict()
    if place == "provider":
        raw["backends"]["providers"][0][key] = "review-secret"
    elif place == "jev":
        raw["jev"][key] = "review-secret"
    elif place == "profile":
        raw["backends"]["models"][0]["profile"][key] = "review-secret"
    elif place == "profile_array":
        raw["backends"]["models"][0]["profile"]["metadata"] = [{key: "review-secret"}]
    else:
        raw["time_periods"][0][key] = "review-secret"

    with pytest.raises(ValueError, match="secret field is not allowed"):
        tree_from_dict(raw)


@pytest.mark.parametrize("key", ["monkey", "key", "keys", "keychain", "hot_key"])
def test_runtime_keeps_legitimate_metadata_that_merely_mentions_key(key):
    """只有固定密钥集合被拒绝；包含 key 的正常元数据必须原样保留。"""
    raw = portable_tree_dict()
    raw["backends"]["models"][0]["profile"]["metadata"] = [{key: "keep-me"}]

    tree = tree_from_dict(raw)

    assert tree.runtime.backends.models[0]["profile"]["metadata"] == [{key: "keep-me"}]


@pytest.mark.parametrize(("kind", "message"), [
    ("duplicate_provider", "duplicate provider"),
    ("duplicate_model", "duplicate model"),
    ("missing_provider", "unknown provider"),
    ("profile", "profile must be an object"),
    ("window", "input_tokens cannot exceed context_tokens"),
    ("period", "period needs exactly"),
])
def test_runtime_rejects_invalid_catalog_window_and_periods(kind, message):
    raw = portable_tree_dict()
    if kind == "duplicate_provider":
        provider = raw["backends"]["providers"][0]
        raw["backends"]["providers"].append(copy.deepcopy(provider))
    elif kind == "duplicate_model":
        model = raw["backends"]["models"][0]
        raw["backends"]["models"].append(copy.deepcopy(model))
    elif kind == "missing_provider":
        raw["backends"]["models"][0]["provider"] = "missing"
    elif kind == "profile":
        raw["backends"]["models"][0]["profile"] = []
    elif kind == "window":
        raw["jev_window"] = {"input_tokens": 8192, "context_tokens": 4096}
    elif kind == "period":
        del raw["time_periods"][0]["end"]

    with pytest.raises(ValueError, match=message):
        tree_from_dict(raw)
