"""Kernel multi-replica graph reloading: one graph writer, many readers.

After the core/peripheral split the graph JSON has exactly one writer class (the
kernel) and any number of kernel replicas read the same graph directory.  This
file keeps the original multi-replica contract on the kernel boundary:

* a replica started before a graph change loads it on its next reload poll;
* an invalid enabled document marks the replica stale but keeps the last valid
  route set serving decisions;
* fixing the document clears ``stale``/``last_load_error`` again.

The former read-only-writer case is obsolete: the kernel is the only graph
writer by design (global constraint 2), and the peripheral has no graph
repository at all (it proxies management calls).
"""
import asyncio
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from router.api.core_app import create_core_app
from router.core_config import CoreSettings, LogSinkSettings
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
        "error_fallback": "target",
    }
    return tree_dict(root=root)


def kernel_replica(graph_dir):
    return create_core_app(CoreSettings(
        graph_dir=graph_dir, logging=LogSinkSettings(type="disabled")))


def create_payload(raw):
    payload = dict(raw)
    payload.pop("enabled", None)
    payload.pop("default", None)
    return payload


def test_reader_loads_writer_change_and_survives_failures(tmp_path):
    settings = test_settings(tmp_path)
    # writer/reader 是共享同一图目录的两个内核副本（图目录是唯一事实源）。
    writer = kernel_replica(settings.graph_dir)
    reader = kernel_replica(settings.graph_dir)
    with TestClient(reader) as reader_client:
        with TestClient(writer) as writer_client:
            assert writer_client.post(
                "/internal/v1/trees", json=create_payload(direct_tree("qwen3-8b"))
            ).status_code == 201
            assert writer_client.post(
                "/internal/v1/trees/main/enable").status_code == 200
            asyncio.run(reader.state.graph_reloader.poll_once())
            saved = writer_client.get("/internal/v1/trees/main").json()["tree"]
            updated = create_payload(direct_tree("qwen3-32b"))
            updated["base_document_sha256"] = saved["document_sha256"]
            assert writer_client.put(
                "/internal/v1/trees/main", json=updated).status_code == 200
            asyncio.run(reader.state.graph_reloader.poll_once())
            response = reader_client.post(
                "/internal/v1/route-decision", json={
                    "messages": [{"role": "user", "content": "hello"}]})
            assert response.status_code == 200, response.text
            assert response.json()["model"] == "qwen3-32b"

        # writer 的内核 lifespan 已关闭，reader 仍使用内存中的新快照。
        response = reader_client.post(
            "/internal/v1/route-decision", json={
                "messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 200
        assert response.json()["model"] == "qwen3-32b"

        path = Path(settings.graph_dir) / "graphs" / "main.json"
        good = path.read_bytes()
        path.with_suffix(".test-tmp").write_bytes(b"{broken")
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader.state.graph_reloader.poll_once())
        assert reader_client.get("/readyz").json()["stale"] is True
        assert reader_client.post(
            "/internal/v1/route-decision", json={
                "messages": [{"role": "user", "content": "hello"}]}
        ).status_code == 200
        path.with_suffix(".test-tmp").write_bytes(good)
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader.state.graph_reloader.poll_once())
        assert reader_client.get("/readyz").json()["stale"] is False

        # 路由集不变量失败（启用但非默认）：保留旧快照并报告 stale；修复后恢复。
        document = json.loads(good)
        document["default"] = False
        broken_route_set = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        path.with_suffix(".test-tmp").write_bytes(broken_route_set)
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader.state.graph_reloader.poll_once())
        unavailable = reader_client.get("/readyz")
        assert unavailable.status_code == 200
        assert unavailable.json()["stale"] is True
        assert "no_default_tree" in unavailable.json()["last_load_error"]
        assert reader_client.post(
            "/internal/v1/route-decision", json={
                "messages": [{"role": "user", "content": "hello"}]}
        ).status_code == 200

        path.with_suffix(".test-tmp").write_bytes(good)
        os.replace(path.with_suffix(".test-tmp"), path)
        asyncio.run(reader.state.graph_reloader.poll_once())
        recovered = reader_client.get("/readyz")
        assert recovered.status_code == 200
        assert recovered.json()["stale"] is False
        assert recovered.json()["last_load_error"] == ""

        # 盘上遗留的 current 指针不再被读取：垃圾内容也不影响就绪状态。
        (Path(settings.graph_dir) / "current").write_text("garbage!\n", encoding="utf-8")
        asyncio.run(reader.state.graph_reloader.poll_once())
        assert reader_client.get("/readyz").json()["stale"] is False
