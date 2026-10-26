"""Backend routing document and canonical JSON: retained snapshot contracts.

Migrated characterization tests for the parts of the old snapshot module that
survive the unified-tree rewrite: backend content hashing never includes auth
material, and canonical JSON + sha256 are the content-addressing primitives every
revision (pipeline/questions/tree/snapshot) relies on.
"""

from __future__ import annotations

import hashlib
import json

from router.api.upstream import Backend
from router.pipeline.snapshot import (
    backends_document,
    backends_revision_of,
    canonical_json,
    sha256_text,
)


def backends() -> dict[str, Backend]:
    return {
        "m1": Backend(
            url="http://a.example/v1/chat/completions",
            headers={"Authorization": "Bearer secret-token"},
            upstream_name="m1-up", provider="local"),
        "m2": Backend(
            url="http://b.example/v1/chat/completions",
            headers={}, upstream_name="m2-up", provider="openai",
            reasoning={"declared": True, "supported": True, "format": "openai",
                       "efforts": [{"id": "high", "wire": "high"}],
                       "default": None}),
    }


def test_backend_document_contains_routing_fields_only():
    document = backends_document(backends())
    assert [row["id"] for row in document] == ["m1", "m2"]   # sorted by model id
    assert set(document[0]) == {"id", "url", "upstream", "provider", "reasoning"}
    assert document[0]["url"] == "http://a.example/v1/chat/completions"
    assert document[0]["upstream"] == "m1-up"
    assert document[0]["provider"] == "local"
    assert document[1]["reasoning"]["efforts"] == [
        {"id": "high", "wire": "high"}]
    dumped = canonical_json(document)
    assert "Authorization" not in dumped
    assert "secret-token" not in dumped


def test_backends_revision_is_content_addressed_and_ignores_auth_material():
    revision = backends_revision_of(backends())
    assert revision == sha256_text(canonical_json(backends_document(backends())))
    assert len(revision) == 64

    rotated = backends()
    rotated["m1"] = Backend(
        url=rotated["m1"].url,
        headers={"Authorization": "Bearer rotated-token"},
        upstream_name="m1-up", provider="local")
    assert backends_revision_of(rotated) == revision


def test_backends_revision_changes_with_routing_fields():
    revision = backends_revision_of(backends())
    changed = backends()
    changed["m1"] = Backend(
        url="http://new.example/v1/chat/completions",
        headers={}, upstream_name="m1-up", provider="local")
    assert backends_revision_of(changed) != revision
    assert backends_revision_of({}) == sha256_text(canonical_json([]))


def test_canonical_json_is_sorted_compact_and_utf8_friendly():
    first = {"b": 1, "a": "中文", "list": [2, 1]}
    second = {"a": "中文", "list": [2, 1], "b": 1}
    text = canonical_json(first)
    assert text == '{"a":"中文","b":1,"list":[2,1]}'
    assert canonical_json(second) == text
    assert canonical_json(json.loads(text)) == text
    # arrays keep their order (candidate order is behaviour)
    assert json.loads(text)["list"] == [2, 1]


def test_sha256_text_hashes_utf8_bytes():
    assert sha256_text("abc") == hashlib.sha256(b"abc").hexdigest()
    assert sha256_text("中文") == hashlib.sha256(
        "中文".encode("utf-8")).hexdigest()
