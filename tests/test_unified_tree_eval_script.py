"""Golden-set runner for the unified routing tree (plan Task 15, CPU only).

The runner must be a thin reporter: it maps the golden jsonl onto the
``/admin/eval`` labels payload and persists the server's own report. These tests
pin the mapping (``expect`` → ``expect_model``, never the old demo's
``expected``), the "report must cover every case" refusal and the error path
without touching a real server or the real JEV model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import unified_tree_eval                 # noqa: E402  (scripts/ on sys.path)


GOLDEN_LINES = [
    {"id": "fr_capital", "text": "法国的首都是哪里？", "expect": "qwen3-8b"},
    {"id": "translate_short", "text": "把 Good morning 翻译成中文。",
     "expect": "qwen3-32b"},
    {"id": "sql", "text": "写一条 SQL 统计每日订单。", "expect": "qwen3-32b"},
]


def write_golden(tmp_path: Path, rows=GOLDEN_LINES) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8")
    return path


def report_for(labels: list[dict]) -> dict:
    cases = [{"id": case["id"], "text": case["text"],
              "expect": case["expect_model"], "got": case["expect_model"],
              "tree_id": "t1", "tree_revision": "rev-tree",
              "snapshot_revision": "rev-snap"} for case in labels]
    return {"mode": "labels", "total": len(labels), "matched": len(labels),
            "rate": 1.0, "matrix": [], "mismatches": [], "cases": cases}


def recording_transport(seen: list[dict], report_factory=report_for,
                        eval_status: int = 200,
                        login_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/login":
            seen.append({"kind": "login", "body": json.loads(request.content)})
            return httpx.Response(login_status,
                                  json={"token": "session-token"})
        assert request.url.path == "/admin/eval", request.url.path
        body = json.loads(request.content)
        seen.append({"kind": "eval", "body": body})
        if eval_status != 200:
            return httpx.Response(eval_status, json={"detail": "nope"})
        return httpx.Response(200, json=report_factory(body["labels"]))
    return httpx.MockTransport(handler)


def test_golden_rows_map_expect_to_expect_model_and_cover_every_case(tmp_path):
    """The 30-row contract: every row maps id/text/expect -> labels payload and
    the persisted file carries the server report plus the per-case labels."""
    seen: list[dict] = []
    out = tmp_path / "out" / "report.json"
    report = unified_tree_eval.run(
        "http://router.test", write_golden(tmp_path), out,
        "admin@test.com", "secret", transport=recording_transport(seen))

    assert [entry["kind"] for entry in seen] == ["login", "eval"]
    assert seen[0]["body"] == {"email": "admin@test.com", "password": "secret"}
    eval_body = seen[1]["body"]
    assert eval_body["mode"] == "labels" and eval_body["days"] == 1
    assert eval_body["labels"] == [
        {"id": "fr_capital", "text": "法国的首都是哪里？",
         "expect_model": "qwen3-8b"},
        {"id": "translate_short", "text": "把 Good morning 翻译成中文。",
         "expect_model": "qwen3-32b"},
        {"id": "sql", "text": "写一条 SQL 统计每日订单。",
         "expect_model": "qwen3-32b"},
    ]
    assert report["total"] == 3 and len(report["cases"]) == 3

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["source"] == str(write_golden(tmp_path))
    assert saved["report"] == report


def test_old_demo_field_expected_is_not_treated_as_the_expectation(tmp_path):
    """The legacy demo used ``expected``; the golden file's ``expect`` wins and
    a row without ``expect`` is rejected instead of silently scoring None."""
    rows = [{"id": "a", "text": "x", "expect": "m1", "expected": "legacy"}]
    seen: list[dict] = []
    unified_tree_eval.run(
        "http://router.test", write_golden(tmp_path, rows), tmp_path / "o.json",
        "a@b.c", "pw", transport=recording_transport(seen))
    assert seen[1]["body"]["labels"] == [
        {"id": "a", "text": "x", "expect_model": "m1"}]

    legacy_only = tmp_path / "legacy.jsonl"
    legacy_only.write_text(json.dumps({"id": "a", "text": "x",
                                       "expected": "m1"}) + "\n",
                           encoding="utf-8")
    with pytest.raises(ValueError, match="expect"):
        unified_tree_eval.load_labels(legacy_only)


def test_non_200_eval_response_fails_without_writing_a_report(tmp_path):
    seen: list[dict] = []
    out = tmp_path / "report.json"
    with pytest.raises(httpx.HTTPStatusError):
        unified_tree_eval.run(
            "http://router.test", write_golden(tmp_path), out,
            "admin@test.com", "secret",
            transport=recording_transport(seen, eval_status=503))
    assert not out.exists()


def test_login_failure_stops_before_calling_eval(tmp_path):
    seen: list[dict] = []
    with pytest.raises(httpx.HTTPStatusError):
        unified_tree_eval.run(
            "http://router.test", write_golden(tmp_path), tmp_path / "r.json",
            "admin@test.com", "wrong", transport=recording_transport(
                seen, login_status=401))
    assert [entry["kind"] for entry in seen] == ["login"]


def test_incomplete_report_is_rejected(tmp_path):
    """A report that drops cases (or the cases array) must not be accepted as a
    golden run: the operator would review an incomplete accuracy denominator."""
    def missing_cases(labels):
        report = report_for(labels)
        report["cases"] = report["cases"][:-1]
        return report

    with pytest.raises(RuntimeError, match="every golden case"):
        unified_tree_eval.run(
            "http://router.test", write_golden(tmp_path),
            tmp_path / "r.json", "a@b.c", "pw",
            transport=recording_transport([], report_factory=missing_cases))


def test_cli_requires_credentials_from_environment(tmp_path, monkeypatch,
                                                   capsys):
    monkeypatch.delenv("ROUTER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ROUTER_ADMIN_PASSWORD", raising=False)
    assert unified_tree_eval.main([
        "--golden", str(write_golden(tmp_path)),
        "--out", str(tmp_path / "r.json")]) == 2
    assert "ROUTER_ADMIN_EMAIL" in capsys.readouterr().err
