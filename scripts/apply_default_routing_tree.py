#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router.routing.document import graph_document, parse_graph_document  # noqa: E402

BASE = os.environ.get("SR_BASE", "http://127.0.0.1:8400").rstrip("/")
EMAIL = os.environ.get("SR_ADMIN_EMAIL", "admin@test.com")
PASSWORD = os.environ.get("SR_ADMIN_PASSWORD", "1qaz@WSX")
TOKEN = ""


def call(method: str, path: str, body=None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc


def main() -> int:
    global TOKEN
    parser = argparse.ArgumentParser(description="Apply a v3 routing graph through the admin API")
    parser.add_argument("--tree-json", default=str(Path(__file__).resolve().parent.parent / "configs" / "default_routing_tree.json"))
    parser.add_argument("--tree-id", required=True)
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    content = Path(args.tree_json).read_bytes()
    tree = parse_graph_document(
        content,
        tree_id=args.tree_id,
        enabled=False,
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )
    TOKEN = call("POST", "/admin/login", {"email": EMAIL, "password": PASSWORD}).get("token", "")
    if not TOKEN:
        raise SystemExit("login returned no session token")
    payload = graph_document(tree)
    existing = {item["id"] for item in call("GET", "/admin/trees")["trees"]}
    if args.tree_id in existing:
        current = call("GET", f"/admin/trees/{args.tree_id}")["tree"]
        payload["base_document_sha256"] = current["document_sha256"]
        result = call("PUT", f"/admin/trees/{args.tree_id}", payload)["tree"]
    else:
        payload["id"] = args.tree_id
        result = call("POST", "/admin/trees", payload)["tree"]
    if args.enable:
        call("POST", f"/admin/trees/{args.tree_id}/enable")
    print(json.dumps({
        "tree_id": args.tree_id,
        "tree_revision": result["tree_revision"],
        "document_sha256": result["document_sha256"],
        "enabled": bool(args.enable),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
