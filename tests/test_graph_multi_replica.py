import asyncio
import os
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from tests.routing_fixtures import test_settings, tree_dict


def direct_tree(model):
    root = {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "target", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-target", "source": "start", "source_port": "out",
            "target": "target", "target_port": "in_1", "condition": None,
        }],
        "otherwise_target": "target", "error_fallback": "target",
    }
    return tree_dict(root=root)


def test_reader_loads_writer_change_and_survives_failures(tmp_path):
    settings = test_settings(tmp_path)

    def upstream(request):
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "qwen3-32b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    transport = httpx.MockTransport(upstream)
    writer_app = create_app(settings, transport=transport)
    reader_app = create_app(replace(settings, graph_writer=False), transport=transport)
    with TestClient(reader_app) as reader:
        with TestClient(writer_app) as writer:
            created_payload = direct_tree("qwen3-8b")
            created_payload.pop("enabled")
            assert writer.post("/admin/trees", json=created_payload).status_code == 201
            assert writer.post("/admin/trees/main/enable").status_code == 200
            asyncio.run(reader_app.state.graph_reloader.poll_once())
            saved = writer.get("/admin/trees/main").json()["tree"]
            updated = direct_tree("qwen3-32b")
            updated.pop("enabled")
            updated["base_document_sha256"] = saved["document_sha256"]
            assert writer.put("/admin/trees/main", json=updated).status_code == 200
            asyncio.run(reader_app.state.graph_reloader.poll_once())
            response = reader.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert response.status_code == 200, response.text
            assert response.headers["x-sr-model"] == "qwen3-32b"
            readonly = reader.put("/admin/trees/main", json=updated)
            assert readonly.status_code == 503
            assert readonly.json()["detail"]["code"] == "graph_read_only"

        # writer 的 lifespan 已关闭，reader 仍使用内存中的新快照。
        response = reader.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200 and response.headers["x-sr-model"] == "qwen3-32b"

        path = Path(settings.graph_dir) / "graphs" / "main.json"
        good = path.read_bytes()
        path.with_suffix(".test-tmp").write_bytes(b"{broken")
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader_app.state.graph_reloader.poll_once())
        assert reader.get("/readyz").json()["stale"] is True
        assert reader.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        }).status_code == 200
        path.with_suffix(".test-tmp").write_bytes(good)
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader_app.state.graph_reloader.poll_once())
        assert reader.get("/readyz").json()["stale"] is False

        pointer = Path(settings.graph_dir) / "current"
        pointer.unlink()
        asyncio.run(reader_app.state.graph_reloader.poll_once())
        unavailable = reader.get("/readyz")
        assert unavailable.status_code == 200
        assert unavailable.json()["stale"] is True
        assert "current pointer is unavailable" in unavailable.json()["last_load_error"]
        assert reader.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        }).status_code == 200

        pointer.write_text("main\n", encoding="utf-8")
        asyncio.run(reader_app.state.graph_reloader.poll_once())
        recovered = reader.get("/readyz")
        assert recovered.status_code == 200
        assert recovered.json()["stale"] is False
        assert recovered.json()["last_load_error"] == ""
