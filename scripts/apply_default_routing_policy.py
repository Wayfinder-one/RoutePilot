#!/usr/bin/env python3
"""Apply a Routing Policy v4 document through the admin API.

Creates the policy via POST /admin/policies, or replaces the whole stored
document via PUT /admin/policies/{id} with If-Match when it already exists.
Never reads graph-era files; the JSON on disk must be a complete v4 document.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router.routing.policy import policy_from_dict  # noqa: E402

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
    parser = argparse.ArgumentParser(description="Apply a Routing Policy v4 document through the admin API")
    parser.add_argument("--policy-json", default=str(Path(__file__).resolve().parent.parent / "configs" / "default_routing_policy.json"))
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    raw = json.loads(Path(args.policy_json).read_text(encoding="utf-8"))
    try:
        policy_from_dict(raw)     # 结构解析失败直接失败，不发送半份文档
    except ValueError as exc:
        raise SystemExit(f"{args.policy_json} is not a valid sr-routing-policy v4 document: {exc}") from exc

    TOKEN = call("POST", "/admin/login", {"email": EMAIL, "password": PASSWORD}).get("token", "")
    if not TOKEN:
        raise SystemExit("login returned no session token")
    existing = {item["id"] for item in call("GET", "/admin/policies")["policies"]}
    if args.policy_id in existing:
        current = call("GET", f"/admin/policies/{args.policy_id}")["policy_resource"]
        # 整份替换必须带 If-Match：不带哈希的 PUT 会被 409 policy_conflict 拒绝。
        request = urllib.request.Request(
            BASE + f"/admin/policies/{args.policy_id}",
            data=json.dumps(raw, ensure_ascii=False).encode(),
            method="PUT",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}",
                     "If-Match": current["document_sha256"]},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read() or b"{}")["policy_resource"]
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"PUT /admin/policies/{args.policy_id} -> {exc.code}: {exc.read().decode()}") from exc
    else:
        result = call("POST", "/admin/policies", {"id": args.policy_id, "policy": raw})["policy_resource"]
    if args.enable:
        call("POST", f"/admin/policies/{args.policy_id}/enable")
    print(json.dumps({
        "policy_id": args.policy_id,
        "policy_revision": result["policy_revision"],
        "document_sha256": result["document_sha256"],
        "enabled": bool(args.enable),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
