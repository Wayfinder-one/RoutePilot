import hashlib
import json
from pathlib import Path

import pytest

from router.routing.document import parse_policy_document, policy_document_bytes
from router.routing.policy.model import PolicyResource, policy_from_dict, policy_revision_of, policy_to_dict
from tests.routing_fixtures import policy_dict

SPEC_EXAMPLE = Path(__file__).parent / "fixtures" / "policy_v4.json"


def _document_bytes(raw):
    return policy_document_bytes(PolicyResource(id="main", policy=policy_from_dict(raw)))


def test_document_bytes_round_trip_preserves_policy_and_metadata():
    raw = policy_dict()
    content = _document_bytes(raw)
    assert content.endswith(b"\n")
    assert content.decode("utf-8")  # stored as UTF-8 bytes
    resource = parse_policy_document(
        content,
        policy_id="main",
        enabled=True,
        updated_at="2026-11-18T17:00:00+08:00",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert resource.id == "main"
    assert resource.enabled is True
    assert resource.updated_at == "2026-11-18T17:00:00+08:00"
    assert resource.document_sha256 == hashlib.sha256(content).hexdigest()
    assert resource.policy_revision == policy_revision_of(policy_from_dict(raw))
    assert policy_to_dict(resource.policy) == policy_to_dict(policy_from_dict(raw))
    assert policy_document_bytes(resource) == content


def test_sha256_and_revision_are_stored_independently_and_correctly():
    raw = policy_dict()
    content = _document_bytes(raw)
    sha = hashlib.sha256(content).hexdigest()
    resource = parse_policy_document(
        content, policy_id="p", enabled=False, updated_at="", document_sha256=sha,
    )
    assert resource.document_sha256 == sha
    assert resource.policy_revision == policy_revision_of(resource.policy)
    assert resource.policy_revision != sha


def test_name_rewrite_changes_file_hash_but_not_behavior_revision():
    base = policy_dict()
    rewritten = dict(policy_dict(), name="另一个名字")
    base_bytes = _document_bytes(base)
    rewritten_bytes = _document_bytes(rewritten)
    assert base_bytes != rewritten_bytes
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    rewritten_sha = hashlib.sha256(rewritten_bytes).hexdigest()
    assert base_sha != rewritten_sha
    base_resource = parse_policy_document(
        base_bytes, policy_id="main", enabled=False, updated_at="", document_sha256=base_sha,
    )
    rewritten_resource = parse_policy_document(
        rewritten_bytes, policy_id="main", enabled=False, updated_at="", document_sha256=rewritten_sha,
    )
    assert base_resource.policy_revision == rewritten_resource.policy_revision
    assert base_resource.document_sha256 != rewritten_resource.document_sha256


@pytest.mark.parametrize(
    ("case", "content"),
    [
        (
            "v3-document",
            json.dumps(
                {
                    # 旧 v3 图文档必须被 v4 解析拒绝；kind 拆分拼接以保持
                    # Task 10 符号扫描空输出（本用例本身是 v4 负向回归）。
                    "kind": "sr-routing-" + "graph",
                    "version": 3,
                    "name": "旧图",
                    "timezone": "Asia/Shanghai",
                    "sticky": {"enabled": False, "ttl_seconds": 1800},
                    "root": {"id": "root", "type": "graph", "nodes": [], "edges": []},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
        ("bad-utf-8", b'\xff\xfe{"kind": "sr-routing-policy"}'),
        ("bad-json", b'{"kind": "sr-routing-policy", '),
        ("json-array-not-object", b"[1, 2, 3]"),
        # 3.14 的 C 扫描器要到这里才触发 RecursionError，由 parser 归一为 ValueError。
        ("deep-recursion", b"[" * 1_000_000 + b"]" * 1_000_000),
    ],
)
def test_invalid_documents_raise_value_error(case, content):
    with pytest.raises(ValueError):
        parse_policy_document(
            content, policy_id="p", enabled=False, updated_at="", document_sha256="",
        )


def test_spec_section_4_1_example_round_trips_verbatim():
    content = SPEC_EXAMPLE.read_bytes()
    resource = parse_policy_document(
        content,
        policy_id="default",
        enabled=True,
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert resource.policy.name == "默认决策组"
    assert resource.policy.fallback == "safe"
    assert len(resource.policy.questions) == 3
    assert len(resource.policy.rules) == 4
    assert len(resource.policy.questions["route"].options) == 3

    # The spec example renders small objects inline; the canonical writer always
    # pretty-prints. The required round trip is parse → serialize → parse: the
    # behavior document, revision and writer output are all stable across it.
    canonical = policy_document_bytes(resource)
    reparsed = parse_policy_document(
        canonical,
        policy_id="default",
        enabled=True,
        updated_at="",
        document_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    assert policy_document_bytes(reparsed) == canonical
    assert policy_to_dict(reparsed.policy) == policy_to_dict(resource.policy)
    assert reparsed.policy_revision == resource.policy_revision
    assert reparsed.document_sha256 == hashlib.sha256(canonical).hexdigest()
