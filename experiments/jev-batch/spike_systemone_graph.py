#!/usr/bin/env python3
"""M0 spike: probe the live decision service /v1/systemone with a multi-question
graph payload (choice / noul / score) and freeze the measured wire contract.

Key-free and self-contained (urllib only); never prints the API key.
Exit code 0 only if all four checks are true.

Frozen contract per M0 measurements (2026-11-02):
- noul answer reads key `noul` (= P(yes)), not `yes`;
- score question request carries `criteria` (2-10 finite levels); the score
  answer returns the selected level index (`score`) plus a `legend` mapping
  index -> scale value, so the engine value = legend[str(int(round(score)))].
"""
import json
import math
import os
import sys
import urllib.error
import urllib.request


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def object_at(value, key):
    nested = value.get(key, {}) if isinstance(value, dict) else {}
    return nested if isinstance(nested, dict) else {}


def main():
    base = os.environ.get("ROUTER_JEV_URL", "http://127.0.0.1:30104").rstrip("/")
    model = os.environ.get("ROUTER_JEV_MODEL", "d1-2B")
    key = os.environ.get("ROUTER_JEV_API_KEY", "")
    payload = {
        "model": model,
        "state": "【待判断的用户请求】\n请翻译一句短句。\n\n【任务】回答路由问题。",
        "questions": {
            "capability": {
                "type": "choice",
                "instructions": "选择能力档位。",
                "criteria": {"轻量模型": "短翻译和格式转换", "增强模型": "复杂推理"},
            },
            "domain": {"type": "noul", "instructions": "这是简单请求吗？"},
            "complexity": {"type": "score", "instructions": "给出有限数值复杂度。", "criteria": [1, 10]},
        },
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        base + "/v1/systemone", data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, ValueError) as exc:
        print(json.dumps({"ok": False, "stage": "transport", "error": str(exc)}, ensure_ascii=False))
        return 2
    answers = object_at(body, "answers")
    choice = object_at(object_at(answers, "capability"), "probabilities")
    noul = object_at(answers, "domain").get("noul")
    score = object_at(answers, "complexity").get("score")
    legend = object_at(answers, "complexity").get("legend")
    score_value = None
    if finite_number(score) and isinstance(legend, dict):
        score_value = legend.get(str(int(round(score))))
    choice_values = list(choice.values())
    checks = {
        "multi_question_keys": set(answers) >= {"capability", "domain", "complexity"},
        "choice": set(choice) == {"轻量模型", "增强模型"} and all(map(finite_number, choice_values))
                  and math.isclose(sum(choice_values), 1.0, rel_tol=0.0, abs_tol=1e-3),
        "noul": finite_number(noul) and 0 <= noul <= 1,
        "score": finite_number(score) and finite_number(score_value),
    }
    usage = body.get("usage", {}) if isinstance(body, dict) else {}
    print(json.dumps({"ok": all(checks.values()), "checks": checks, "usage": usage}, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
