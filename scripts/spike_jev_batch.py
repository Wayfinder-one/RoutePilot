from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from router.api.upstream import Backend
from router.jev.client import (
    DecisionQuestion,
    JevCandidate,
    JevError,
    _wire_question,
    parse_answer,
)
from router.jev.config import DEFAULT_STATE_TEMPLATE, JevSettings
from router.jev.window import apply_window, estimate_tokens
from router.routing.affinity import SessionAffinity
from router.routing.graph.conditions import evaluate_condition
from router.routing.graph.engine import GraphExecutionEngine
from router.routing.graph.model import Condition, Edge, QuestionNode
from router.routing.model import tree_from_dict
from router.routing.scheduler import LoadBalancer
from router.routing.snapshot import build_snapshot
from router.routing.types import RoutingRequest

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {"all", "equivalence", "capacity", "budget"}


@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    length_bucket: str
    boundary_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestBudget:
    h1: int
    repeats: int
    scan: int
    concurrency: int
    reserve: int

    @property
    def total(self) -> int:
        return self.h1 + self.repeats + self.scan + self.concurrency + self.reserve


def load_samples(path: Path, limit: int = 0) -> list[Sample]:
    result = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != {"id", "text", "length_bucket", "boundary_tags"}:
            raise ValueError(f"invalid sample at line {line_number}")
        if not all(isinstance(raw[key], str) and raw[key] for key in ("id", "text", "length_bucket")):
            raise ValueError(f"invalid sample strings at line {line_number}")
        if raw["length_bucket"] not in {"short", "medium", "long"}:
            raise ValueError(f"invalid length_bucket at line {line_number}")
        if not isinstance(raw["boundary_tags"], list) or not all(isinstance(x, str) for x in raw["boundary_tags"]):
            raise ValueError(f"invalid boundary_tags at line {line_number}")
        if raw["id"] in seen:
            raise ValueError(f"duplicate sample id {raw['id']!r}")
        seen.add(raw["id"])
        result.append(Sample(raw["id"], raw["text"], raw["length_bucket"], tuple(raw["boundary_tags"])))
    if not 60 <= len(result) <= 80:
        raise ValueError("sample count must be in 60..80")
    return result[:limit] if limit else result


def plan_request_budget(sample_count: int, scenario: str = "all") -> RequestBudget:
    if not 1 <= sample_count <= 80 or scenario not in SCENARIOS:
        raise ValueError("invalid sample count or scenario")
    do_h1 = scenario in {"all", "equivalence"}
    do_h2 = scenario in {"all", "capacity"}
    do_h3 = scenario in {"all", "budget"}
    return RequestBudget(
        h1=sample_count * 12 if do_h1 else 0,
        repeats=min(sample_count, 20) * 3 if do_h1 else 0,
        scan=120 if do_h2 else 0,
        concurrency=640 if do_h2 else 0,
        reserve=200 if do_h3 else 0,
    )


def _load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _as_decision(node):
    if node.question_type == "choice":
        return DecisionQuestion("choice", node.instructions, options=tuple(
            JevCandidate(item.key, item.label, item.capability) for item in node.options
        ))
    if node.question_type == "noul":
        return DecisionQuestion("noul", node.instructions)
    return DecisionQuestion("score", node.instructions, criteria=node.criteria)


def _source_trees():
    default = tree_from_dict({
        "id": "default", "name": "", "enabled": True,
        "root": _load_json("configs/default_routing_tree.json")["root"],
    })
    logic_raw = _load_json("configs/cases/case-logic.json")
    industry_raw = _load_json("configs/cases/case-industry.json")
    logic = tree_from_dict({"id": "logic", "name": "", "enabled": True, "root": logic_raw["root"]})
    industry = tree_from_dict({"id": "industry", "name": "", "enabled": True, "root": industry_raw["root"]})
    return default, logic, industry


def build_question_groups():
    default, logic, industry = _source_trees()
    default_nodes = {node.id: node for node in default.root.nodes}
    logic_nodes = {node.id: node for node in logic.root.nodes}
    industry_nodes = {node.id: node for node in industry.root.nodes}
    capability = default_nodes["capability"]
    industry_choice = replace(
        industry_nodes["industry"], options=tuple(industry_nodes["industry"].options[:4])
    )
    return {
        "G1": {"capability": _as_decision(capability), "urgent": _as_decision(logic_nodes["q-urgent"])},
        "G2": {"industry": _as_decision(industry_choice), "severity": _as_decision(logic_nodes["q-severity"]),
               "urgent": _as_decision(logic_nodes["q-urgent"])},
        "G3": {"q1": _as_decision(capability), "q2": _as_decision(capability),
               "q3": _as_decision(logic_nodes["q-urgent"]), "q4": _as_decision(logic_nodes["q-severity"])},
    }


def _g3_tree_dict():
    """G3 fixture 的原始 dict：既用于建图，也原样写进 g3-graph.json。"""
    default_raw = _load_json("configs/default_routing_tree.json")["root"]
    logic_raw = _load_json("configs/cases/case-logic.json")["root"]
    choice = next(node for node in default_raw["nodes"] if node.get("id") == "capability")
    urgent = next(node for node in logic_raw["nodes"] if node.get("id") == "q-urgent")
    severity = next(node for node in logic_raw["nodes"] if node.get("id") == "q-severity")
    nodes = [{"id": "start", "type": "start"}]
    for node_id, source in (("q1", choice), ("q2", choice), ("q3", urgent), ("q4", severity)):
        item = copy.deepcopy(source)
        item["id"] = node_id
        item.pop("position", None)
        nodes.append(item)
    for node_id, priority in (("t-review", 3), ("t-normal", 2), ("t-error", 1)):
        nodes.append({"id": node_id, "type": "target", "priority": priority,
                      "models": [{"model": "mock", "reasoning": None}]})
    edges = [
        {"id": "e0", "source": "start", "source_port": "out", "target": "q1", "target_port": "in_1", "condition": None},
        {"id": "e1", "source": "q1", "source_port": "prob.strong", "target": "q2", "target_port": "in_1", "condition": {"op": "gt", "value": 0.65}},
        {"id": "e2", "source": "q2", "source_port": "prob.strong", "target": "q3", "target_port": "in_1", "condition": {"op": "gte", "value": 0.5}},
        {"id": "e3", "source": "q3", "source_port": "yes", "target": "q4", "target_port": "in_1", "condition": {"op": "gt", "value": 0.5}},
        {"id": "e4", "source": "q4", "source_port": "value", "target": "t-review", "target_port": "in_1", "condition": {"op": "gt", "value": 4}},
        {"id": "e5", "source": "q4", "source_port": "value", "target": "t-normal", "target_port": "in_1", "condition": {"op": "lte", "value": 4}},
    ]
    return {"id": "g3", "name": "G3", "enabled": True,
            "root": {"id": "root", "type": "graph", "nodes": nodes, "edges": edges,
                     "error_fallback": "t-error"}}


def build_g3_tree():
    return tree_from_dict(_g3_tree_dict())


@dataclass
class ResponseRecord:
    stage: str
    status: int | None
    body: dict | None
    error: str | None
    duration_ms: float
    input_tokens: int | None


class BudgetExceeded(RuntimeError): pass
class SpikeAbort(RuntimeError): pass


class SpikeTransport:
    def __init__(self, base_url, model, *, api_key="", timeout=20.0,
                 max_requests=2000, sleep_seconds=0.02):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_requests = max_requests
        self.sleep_seconds = sleep_seconds
        self.count = 0
        self.stage_counts = {}
        self._consecutive_5xx = 0
        self._lock = threading.Lock()

    def post(self, state: str, questions: Mapping[str, DecisionQuestion], stage: str) -> ResponseRecord:
        if not questions:
            raise ValueError("questions must not be empty")
        with self._lock:
            if self.count >= self.max_requests:
                raise BudgetExceeded(f"request cap {self.max_requests} reached")
            self.count += 1
            self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1
        wire = {key: _wire_question(question) for key, question in questions.items()}
        payload = {"model": self.model, "state": state, "questions": wire}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(
            self.base_url + "/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        started = time.monotonic()
        status = None
        body = None
        error = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise ValueError("response body must be an object")
        except urllib.error.HTTPError as exc:
            status = exc.code
            error = f"HTTP {exc.code}"
            try:
                parsed = json.loads(exc.read())
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            error = type(exc).__name__
        duration = (time.monotonic() - started) * 1000
        with self._lock:
            self._consecutive_5xx = self._consecutive_5xx + 1 if status is not None and 500 <= status < 600 else 0
            should_abort = self._consecutive_5xx >= 2
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if should_abort:
            raise SpikeAbort("two consecutive 5xx responses")
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
            input_tokens = None
        return ResponseRecord(stage, status, body, error, duration, input_tokens)


def typed_answer(key, question, record):
    if record.status != 200 or record.body is None:
        raise JevError(record.error or f"HTTP {record.status}")
    return parse_answer(key, question, record.body, record.duration_ms)


def g3_snapshot(tree):
    backend = Backend(url="http://mock.test", headers={}, upstream_name="mock", provider="mock")
    return build_snapshot(
        tree=tree, backends={"mock": backend}, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
        jev_input_tokens=2048, jev_context_tokens=4096,
    )


class ReplayJev:
    def __init__(self, records, all_questions, strict_all):
        self.records = records
        self.all_questions = all_questions
        self.strict_all = strict_all

    async def choose_many(self, settings, text, questions):
        if self.strict_all:
            if set(self.records) != set(self.all_questions):
                raise JevError("packed record keys do not match G3")
            parsed = {
                key: typed_answer(key, question, self.records[key])
                for key, question in self.all_questions.items()
            }
            return {key: parsed[key] for key in questions}
        return {
            key: typed_answer(key, question, self.records[key])
            for key, question in questions.items()
        }


def replay_g3(tree, text, records, *, strict_all):
    all_questions = {
        node.id: _as_decision(node) for node in tree.root.nodes
        if isinstance(node, QuestionNode)
    }
    engine = GraphExecutionEngine(
        ReplayJev(records, all_questions, strict_all), SessionAffinity(), LoadBalancer()
    )
    decision = asyncio.run(engine.route(
        g3_snapshot(tree), RoutingRequest(text),
        now=datetime.fromisoformat("2026-11-05T12:00:00+08:00"), dry_run=True,
    ))
    decision.release()
    return decision


def route_outcome(decision, http_requests):
    return {
        "target": decision.target_id,
        "candidates": [item.target_id for item in decision.candidates],
        "activated": tuple(decision.answers),
        "failed": any(wave.failed for wave in decision.waves),
        "http_requests": http_requests,
    }


def condition_passes(node: QuestionNode, port, op, threshold, value):
    edge = Edge("metric", node.id, port, "metric", "in_1", Condition(op, threshold))
    result = evaluate_condition(edge, node, value)
    if result.status not in {"true", "false"}:
        raise ValueError(f"invalid metric condition: {result.reason or result.status}")
    return result.value


def wilson_interval(failures, total):
    if total <= 0 or not 0 <= failures <= total:
        raise ValueError("require total > 0 and 0 <= failures <= total")
    z = 1.96
    p = failures / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


PROB_THRESHOLDS = (0.5, 0.6, 0.65, 0.7)
SCORE_THRESHOLDS = (4.0, 8.0)
OPS = ("gt", "gte", "lt", "lte", "eq", "neq")

_AB_TOLERANCE = 0.02
_FLIP_TOLERANCE = 0.005


def render_state(text):
    return DEFAULT_STATE_TEMPLATE.format(request=text)


def canonical_questions(questions):
    return json.dumps({key: _wire_question(value) for key, value in questions.items()},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _call(transport, cache, key, *, stage, state, questions, sample_id, group, path, question_id):
    if key not in cache:
        record = transport.post(state, questions, stage)
        cache[key] = {
            "sample_id": sample_id, "group": group, "path": path, "question_id": question_id,
            "question_count": len(questions), "state_est_tokens": estimate_tokens(state),
            "wire_est_tokens": estimate_tokens(state + "\n" + canonical_questions(questions)),
            "response": record,
        }
    return cache[key]


def call_document(cached):
    metadata = {key: value for key, value in cached.items() if key != "response"}
    return {**metadata, **cached["response"].__dict__}


def _question_node(key, question):
    from router.routing.graph.model import QuestionOption
    return QuestionNode(key, question.question_type, question.instructions,
                        options=tuple(QuestionOption(x.key, x.label, x.capability) for x in question.options),
                        criteria=question.criteria)


def _a_before_b(sample_id, group):
    """按 sha256(sample:group) 的奇偶交错 A/B 请求顺序，只影响发送次序。"""
    digest = hashlib.sha256(f"{sample_id}:{group}".encode("utf-8")).digest()
    return digest[0] % 2 == 0


def _metric_values(question, answer):
    if question.question_type == "choice":
        return ({f"prob.{option.key}": answer.probabilities[option.key] for option in question.options},
                PROB_THRESHOLDS)
    if question.question_type == "noul":
        return {"yes": answer.yes}, PROB_THRESHOLDS
    if question.question_type == "score":
        return {"value": answer.value}, SCORE_THRESHOLDS
    raise ValueError(f"unsupported question type {question.question_type!r}")


def threshold_records(sample_id, group, question_id, question, a, b):
    node = _question_node(question_id, question)
    a_values, thresholds = _metric_values(question, a)
    b_values, b_thresholds = _metric_values(question, b)
    if thresholds != b_thresholds:
        raise ValueError("both answers must belong to the same question type")
    rows = []
    for port, a_value in a_values.items():
        b_value = b_values[port]
        for op in OPS:
            for threshold in thresholds:
                rows.append({
                    "sample_id": sample_id, "group": group, "question_id": question_id,
                    "port": port, "op": op, "threshold": threshold,
                    "a_value": a_value, "b_value": b_value,
                    "flipped": condition_passes(node, port, op, threshold, a_value)
                    != condition_passes(node, port, op, threshold, b_value),
                })
    return rows


def _comparison_row(sample_id, group, question_id, question, a, b):
    a_values, _ = _metric_values(question, a)
    b_values, _ = _metric_values(question, b)
    ports = {
        port: {"a": a_value, "b": b_values[port], "delta": a_value - b_values[port]}
        for port, a_value in a_values.items()
    }
    deltas = [abs(item["delta"]) for item in ports.values()]
    row = {
        "sample_id": sample_id, "group": group, "question_id": question_id,
        "question_type": question.question_type,
        "is_probability": question.question_type != "score",
        "ports": ports,
        "max_abs_delta": max(deltas),
        "mean_abs_delta": sum(deltas) / len(deltas),
    }
    if question.question_type == "choice":
        row.update({
            "a_selected": a.selected_key, "b_selected": b.selected_key,
            "selected_same": a.selected_key == b.selected_key,
            "a_margin": a.margin, "b_margin": b.margin,
        })
    elif question.question_type == "score":
        row.update({"a_selected": a.value, "b_selected": b.value,
                    "selected_same": a.value == b.value})
    else:
        row.update({"a_selected": None, "b_selected": None, "selected_same": None})
    return row


def _repeat_row(sample_id, group, question_id, question, b, repeat):
    b_values, _ = _metric_values(question, b)
    repeat_values, _ = _metric_values(question, repeat)
    ports = {
        port: {"b": b_value, "repeat": repeat_values[port], "delta": repeat_values[port] - b_value}
        for port, b_value in b_values.items()
    }
    deltas = [abs(item["delta"]) for item in ports.values()]
    return {
        "sample_id": sample_id, "group": group, "question_id": question_id,
        "question_type": question.question_type,
        "is_probability": question.question_type != "score",
        "ports": ports,
        "max_abs_delta": max(deltas),
        "mean_abs_delta": sum(deltas) / len(deltas),
    }


def _repeat_question(questions):
    for question_id, question in questions.items():
        if question.question_type == "noul":
            return question_id, question
    question_id = next(iter(questions))
    return question_id, questions[question_id]


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def _threshold_stats(flips, total):
    if total <= 0:
        return {"flips": 0, "total": 0, "rate": None, "wilson_low": None, "wilson_high": None}
    low, high = wilson_interval(flips, total)
    return {"flips": flips, "total": total, "rate": flips / total,
            "wilson_low": low, "wilson_high": high}


def _rows_stats(rows):
    return _threshold_stats(sum(1 for row in rows if row["flipped"]), len(rows))


def _aggregate_thresholds(rows):
    buckets = {}
    for row in rows:
        key = (row["group"], row["question_id"], row["port"], row["op"], row["threshold"])
        bucket = buckets.setdefault(key, {"flips": 0, "total": 0})
        bucket["flips"] += 1 if row["flipped"] else 0
        bucket["total"] += 1
    aggregated = []
    for key in sorted(buckets, key=lambda item: (item[0], item[1], item[2], OPS.index(item[3]), item[4])):
        group, question_id, port, op, threshold = key
        aggregated.append({
            "group": group, "question_id": question_id, "port": port, "op": op,
            "threshold": threshold, **_threshold_stats(buckets[key]["flips"], buckets[key]["total"]),
        })
    return aggregated


def _group_summary(rows, repeat_rows):
    choice = [row for row in rows if row["question_type"] == "choice"]
    noul = [row for row in rows if row["question_type"] == "noul"]
    score = [row for row in rows if row["question_type"] == "score"]
    probability_deltas = [abs(item["delta"]) for row in rows if row["is_probability"]
                          for item in row["ports"].values()]
    score_deltas = [abs(item["delta"]) for row in score for item in row["ports"].values()]
    repeat_probability_deltas = [abs(item["delta"]) for row in repeat_rows if row["is_probability"]
                                 for item in row["ports"].values()]
    repeat_score_deltas = [abs(item["delta"]) for row in repeat_rows if not row["is_probability"]
                           for item in row["ports"].values()]
    choice_deltas = [abs(item["delta"]) for row in choice for item in row["ports"].values()]
    noul_deltas = [abs(item["delta"]) for row in noul for item in row["ports"].values()]
    argmax_same = sum(1 for row in choice if row["selected_same"])
    selected_same = sum(1 for row in score if row["selected_same"])
    return {
        "samples": len({row["sample_id"] for row in rows}),
        "questions": len({row["question_id"] for row in rows}),
        "valid_comparisons": len(rows),
        "max_ab": max(probability_deltas) if probability_deltas else None,
        "mean_ab": _mean(probability_deltas),
        "choice": {
            "count": len(choice),
            "max_abs_delta": max(choice_deltas) if choice_deltas else None,
            "mean_abs_delta": _mean(choice_deltas),
            "argmax_same": argmax_same,
            "argmax_rate": (argmax_same / len(choice)) if choice else None,
        },
        "noul": {
            "count": len(noul),
            "max_abs_delta": max(noul_deltas) if noul_deltas else None,
            "mean_abs_delta": _mean(noul_deltas),
        },
        "score": {
            "count": len(score),
            "max_abs_delta": max(score_deltas) if score_deltas else None,
            "mean_abs_delta": _mean(score_deltas),
            "selected_same": selected_same,
            "selected_rate": (selected_same / len(score)) if score else None,
        },
        "repeat_count": len(repeat_rows),
        "max_repeat": max(repeat_probability_deltas) if repeat_probability_deltas else None,
        "max_repeat_score": max(repeat_score_deltas) if repeat_score_deltas else None,
    }


def _equivalence_summary(samples, names, comparisons, threshold_rows, routes, invalid, repeats):
    groups = {}
    for name in names:
        summary = _group_summary(
            [row for row in comparisons if row["group"] == name],
            [row for row in repeats if row["group"] == name],
        )
        summary["invalid_comparisons"] = sum(1 for row in invalid if row["group"] == name)
        groups[name] = summary

    probability_deltas = [abs(item["delta"]) for row in comparisons if row["is_probability"]
                          for item in row["ports"].values()]
    max_ab = max(probability_deltas) if probability_deltas else None
    repeat_deltas = [abs(item["delta"]) for row in repeats if row["is_probability"]
                     for item in row["ports"].values()]
    max_repeat = max(repeat_deltas) if repeat_deltas else None
    repeat_score_deltas = [abs(item["delta"]) for row in repeats if not row["is_probability"]
                           for item in row["ports"].values()]

    choice_rows = [row for row in comparisons if row["question_type"] == "choice"]
    score_rows = [row for row in comparisons if row["question_type"] == "score"]
    argmax_same = sum(1 for row in choice_rows if row["selected_same"])
    selected_same = sum(1 for row in score_rows if row["selected_same"])

    strata = {}
    for bucket in sorted({sample.length_bucket for sample in samples}):
        ids = {sample.id for sample in samples if sample.length_bucket == bucket}
        rows = [row for row in threshold_rows if row["sample_id"] in ids]
        strata[bucket] = {"samples": len(ids), **_rows_stats(rows)}

    boundary_tags = sorted({tag for sample in samples for tag in sample.boundary_tags})
    boundary = {}
    for tag in boundary_tags:
        ids = {sample.id for sample in samples if tag in sample.boundary_tags}
        boundary[tag] = {"samples": len(ids), **_rows_stats(
            [row for row in threshold_rows if row["sample_id"] in ids])}
    tagged_ids = {sample.id for sample in samples if sample.boundary_tags}
    boundary_overall = {"samples": len(tagged_ids), **_rows_stats(
        [row for row in threshold_rows if row["sample_id"] in tagged_ids])}
    empty_boundary_classes = [tag for tag in boundary_tags if boundary[tag]["total"] == 0]

    g3_rows = [row for row in routes if row["group"] == "G3"]
    if g3_rows:
        g3 = {
            "samples": len(g3_rows),
            "target_same": sum(1 for row in g3_rows if row["target_same"]),
            "target_rate": sum(1 for row in g3_rows if row["target_same"]) / len(g3_rows),
            "candidates_same": sum(1 for row in g3_rows if row["candidates_same"]),
            "candidates_rate": sum(1 for row in g3_rows if row["candidates_same"]) / len(g3_rows),
            "activated_same": sum(1 for row in g3_rows if row["activated_same"]),
            "activated_rate": sum(1 for row in g3_rows if row["activated_same"]) / len(g3_rows),
            "new_error_fallbacks": sum(1 for row in g3_rows if row["new_error_fallback"]),
            "a_http_requests": sum(row["a_http_requests"] for row in g3_rows),
            "c_http_requests": sum(row["c_http_requests"] for row in g3_rows),
        }
    else:
        g3 = None

    noise_ok = (max_repeat is not None and max_ab is not None
                and max_repeat <= _AB_TOLERANCE and max_ab <= _AB_TOLERANCE)

    fail_reasons = []
    conditional_reasons = []
    if invalid:
        fail_reasons.append(f"{len(invalid)} invalid comparison(s)")
    if max_ab is None:
        conditional_reasons.append("no valid probability comparisons")
    elif max_ab > _AB_TOLERANCE:
        fail_reasons.append(f"max_ab {max_ab:.6f} > {_AB_TOLERANCE}")
    if not choice_rows:
        conditional_reasons.append("no choice comparisons")
    elif argmax_same != len(choice_rows):
        fail_reasons.append(f"argmax agreement {argmax_same}/{len(choice_rows)}")
    if not score_rows:
        conditional_reasons.append("no score comparisons")
    elif selected_same != len(score_rows):
        fail_reasons.append(f"score selected agreement {selected_same}/{len(score_rows)}")
    for bucket, stats in strata.items():
        if stats["total"] == 0:
            conditional_reasons.append(f"length bucket {bucket!r} has no threshold judgments")
        elif stats["rate"] > _FLIP_TOLERANCE:
            fail_reasons.append(
                f"length bucket {bucket!r} flip rate {stats['rate']:.6f} > {_FLIP_TOLERANCE}")
    for tag, stats in boundary.items():
        if stats["total"] == 0:
            conditional_reasons.append(f"boundary class {tag!r} has no threshold judgments")
        elif stats["rate"] > _FLIP_TOLERANCE:
            fail_reasons.append(
                f"boundary class {tag!r} flip rate {stats['rate']:.6f} > {_FLIP_TOLERANCE}")
    if not boundary_tags:
        conditional_reasons.append("no boundary classes present")
    elif boundary_overall["total"] == 0:
        conditional_reasons.append("boundary subset has no threshold judgments")
    elif boundary_overall["rate"] > _FLIP_TOLERANCE:
        fail_reasons.append(
            f"boundary subset flip rate {boundary_overall['rate']:.6f} > {_FLIP_TOLERANCE}")
    if g3 is None:
        conditional_reasons.append("G3 route comparison not measured")
    else:
        if g3["target_rate"] < 1.0:
            fail_reasons.append(f"G3 target agreement {g3['target_same']}/{g3['samples']}")
        if g3["candidates_rate"] < 1.0:
            fail_reasons.append(f"G3 candidates agreement {g3['candidates_same']}/{g3['samples']}")
        if g3["new_error_fallbacks"]:
            fail_reasons.append(f"{g3['new_error_fallbacks']} new error_fallback route(s)")
    if max_repeat is None:
        conditional_reasons.append("no repeat baseline observations")
    elif max_repeat > _AB_TOLERANCE:
        conditional_reasons.append(f"max_repeat {max_repeat:.6f} > {_AB_TOLERANCE}")

    if fail_reasons:
        status = "fail"
    elif conditional_reasons:
        status = "conditional"
    else:
        status = "pass"

    return {
        "status": status,
        "fail_reasons": fail_reasons,
        "conditional_reasons": conditional_reasons,
        "noise_ok": noise_ok,
        "max_ab": max_ab,
        "mean_ab": _mean(probability_deltas),
        "argmax_rate": (argmax_same / len(choice_rows)) if choice_rows else None,
        "selected_rate": (selected_same / len(score_rows)) if score_rows else None,
        "max_repeat": max_repeat,
        "max_repeat_score": max(repeat_score_deltas) if repeat_score_deltas else None,
        "groups": groups,
        "strata": strata,
        "boundary": boundary,
        "boundary_overall": boundary_overall,
        "boundary_classes": boundary_tags,
        "empty_boundary_classes": empty_boundary_classes,
        "g3": g3,
        "repeat": {
            "observations": len(repeats),
            "max_abs_delta": max_repeat,
            "mean_abs_delta": _mean(repeat_deltas),
        },
    }


def run_equivalence(samples, groups, tree, transport, *, cache=None,
                    group_names=("G1", "G2", "G3"), repeat_samples=20):
    samples = list(samples)
    cache = {} if cache is None else cache
    names = tuple(group_names)
    unknown = [name for name in names if name not in groups]
    if unknown:
        raise ValueError(f"unknown question group {unknown[0]!r}")
    if isinstance(repeat_samples, bool) or not isinstance(repeat_samples, int) or repeat_samples < 0:
        raise ValueError("repeat_samples must be a nonnegative integer")
    tree_question_ids = {node.id for node in tree.root.nodes if isinstance(node, QuestionNode)}
    if "G3" in names and set(groups["G3"]) != tree_question_ids:
        raise ValueError("G3 question group does not match the routing tree")

    comparisons = []
    threshold_rows = []
    routes = []
    invalid = []
    repeats = []

    for index, sample in enumerate(samples):
        state = render_state(sample.text)
        for name in names:
            questions = groups[name]
            a_cached = None
            b_cached = None
            order = ("A", "B") if _a_before_b(sample.id, name) else ("B", "A")
            for step in order:
                if step == "A":
                    a_cached = _call(
                        transport, cache, (sample.id, name, "A", "*"), stage="H1-A",
                        state=state, questions=questions, sample_id=sample.id, group=name,
                        path="A", question_id="*",
                    )
                else:
                    b_cached = {
                        question_id: _call(
                            transport, cache, (sample.id, name, "B", question_id), stage="H1-B",
                            state=state, questions={question_id: question}, sample_id=sample.id,
                            group=name, path="B", question_id=question_id,
                        )
                        for question_id, question in questions.items()
                    }
            a_record = a_cached["response"]
            b_records = {
                question_id: cached["response"] for question_id, cached in b_cached.items()
            }

            a_answers = {}
            a_errors = {}
            b_answers = {}
            b_errors = {}
            for question_id, question in questions.items():
                try:
                    a_answers[question_id] = typed_answer(question_id, question, a_record)
                except JevError as exc:
                    a_errors[question_id] = str(exc)
                try:
                    b_answers[question_id] = typed_answer(question_id, question, b_records[question_id])
                except JevError as exc:
                    b_errors[question_id] = str(exc)
                if question_id in a_answers and question_id in b_answers:
                    comparisons.append(_comparison_row(
                        sample.id, name, question_id, question,
                        a_answers[question_id], b_answers[question_id],
                    ))
                    threshold_rows.extend(threshold_records(
                        sample.id, name, question_id, question,
                        a_answers[question_id], b_answers[question_id],
                    ))
                else:
                    invalid.append({
                        "sample_id": sample.id, "group": name, "question_id": question_id,
                        "kind": "a_b", "a_path": "A", "b_path": "B",
                        "a_error": a_errors.get(question_id), "b_error": b_errors.get(question_id),
                    })

            if name == "G3":
                a_decision = replay_g3(
                    tree, sample.text, {question_id: a_record for question_id in questions},
                    strict_all=True,
                )
                b_decision = replay_g3(tree, sample.text, dict(b_records), strict_all=False)
                a_outcome = route_outcome(a_decision, 1)
                c_outcome = route_outcome(b_decision, len(b_decision.waves))
                fallback = tree.root.error_fallback
                routes.append({
                    "sample_id": sample.id, "group": name,
                    "a": a_outcome, "b": c_outcome,
                    "target_same": a_outcome["target"] == c_outcome["target"],
                    "candidates_same": a_outcome["candidates"] == c_outcome["candidates"],
                    "activated_same": a_outcome["activated"] == c_outcome["activated"],
                    "a_http_requests": 1,
                    "c_http_requests": len(b_decision.waves),
                    "new_error_fallback": (
                        fallback is not None
                        and a_outcome["target"] == fallback
                        and c_outcome["target"] != fallback
                    ),
                })

            if index < repeat_samples:
                repeat_id, repeat_question = _repeat_question(questions)
                repeat_record = _call(
                    transport, cache, (sample.id, name, "repeat", repeat_id), stage="H1-repeat",
                    state=state, questions={repeat_id: repeat_question}, sample_id=sample.id,
                    group=name, path="repeat", question_id=repeat_id,
                )["response"]
                try:
                    repeat_answer = typed_answer(repeat_id, repeat_question, repeat_record)
                    repeat_error = None
                except JevError as exc:
                    repeat_answer = None
                    repeat_error = str(exc)
                if repeat_id in b_answers and repeat_answer is not None:
                    repeats.append(_repeat_row(
                        sample.id, name, repeat_id, repeat_question,
                        b_answers[repeat_id], repeat_answer,
                    ))
                else:
                    invalid.append({
                        "sample_id": sample.id, "group": name, "question_id": repeat_id,
                        "kind": "repeat", "a_path": "B", "b_path": "repeat",
                        "a_error": b_errors.get(repeat_id), "b_error": repeat_error,
                    })

    return {
        "calls": [call_document(cached) for cached in cache.values()],
        "comparisons": comparisons,
        "thresholds": {
            "records": threshold_rows,
            "aggregated": _aggregate_thresholds(threshold_rows),
        },
        "routes": routes,
        "invalid_comparisons": invalid,
        "summary": _equivalence_summary(
            samples, names, comparisons, threshold_rows, routes, invalid, repeats),
    }


_CAPACITY_CLAUSE = "用户反馈生产环境的模型路由偶发超时，请结合告警等级和影响范围判断处理优先级。"
_CAPACITY_TEXT_REPEATS = 10
_SCAN_NS = (1, 2, 4, 8, 16, 32)
_SCAN_PASS_NS = (1, 2, 4, 8)
_CONCURRENCY_NS = (2, 4)
_CAPACITY_SCAN_STAGE = "H2-scan"
_CAPACITY_A_STAGE = "H2-A"
_QPS_FLOOR = 0.7


def percentile(values, q):
    if not values or not 0 < q <= 1:
        raise ValueError("values must be nonempty and q in (0,1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def expand_questions(questions, n):
    if not 1 <= n <= 32:
        raise ValueError("n must be in 1..32")
    source = list(questions.values())
    return {f"q{i}": source[(i - 1) % len(source)] for i in range(1, n + 1)}


class LiveJev:
    def __init__(self, transport, state, stage="H2-B"):
        self.transport = transport
        self.state = state
        self.stage = stage
        self.records = []
        self.abort = None

    async def choose_many(self, settings, text, questions):
        try:
            record = self.transport.post(self.state, questions, self.stage)
        except (BudgetExceeded, SpikeAbort) as exc:
            # 引擎会把波次内的所有异常吞成 error_fallback；预算/中止必须留给调用方。
            self.abort = exc
            raise
        self.records.append(record)
        return {key: typed_answer(key, question, record) for key, question in questions.items()}


def run_live_g3(tree, text, transport):
    live = LiveJev(transport, render_state(text))
    engine = GraphExecutionEngine(live, SessionAffinity(), LoadBalancer())
    decision = asyncio.run(engine.route(
        g3_snapshot(tree), RoutingRequest(text),
        now=datetime.fromisoformat("2026-11-05T12:00:00+08:00"), dry_run=True,
    ))
    decision.release()
    if live.abort is not None:
        raise live.abort
    return decision, live.records


def _capacity_state():
    text = _CAPACITY_CLAUSE * _CAPACITY_TEXT_REPEATS
    state = render_state(text)
    tokens = estimate_tokens(state)
    if not 300 <= tokens <= 500:
        raise ValueError(f"capacity state estimates {tokens} tokens, expected 300..500")
    return text, state, tokens


def _record_error_kind(record):
    if record.status == 200 and record.error is None:
        return None
    error = (record.error or "").lower()
    if "timeout" in error or "timed out" in error:
        return "timeout"
    if record.status is None or record.status == 200:
        return "transport"
    return "http"


def _token_stats(values, expected):
    present = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return {
        "observed": len(present),
        "missing": expected - len(present),
        "values": present,
        "min": min(present) if present else None,
        "p50": percentile(present, 0.5) if present else None,
        "p95": percentile(present, 0.95) if present else None,
        "max": max(present) if present else None,
        "mean": (sum(present) / len(present)) if present else None,
    }


def _scan_n(transport, state, questions, repeats):
    records = [transport.post(state, questions, _CAPACITY_SCAN_STAGE) for _ in range(repeats)]
    latencies = [record.duration_ms for record in records]
    kinds = [_record_error_kind(record) for record in records]
    answer_errors = 0
    for record, kind in zip(records, kinds):
        if kind is not None:
            continue
        try:
            for key, question in questions.items():
                typed_answer(key, question, record)
        except JevError:
            answer_errors += 1
    errors = sum(1 for kind in kinds if kind is not None) + answer_errors
    return {
        "question_count": len(questions),
        "requests": repeats,
        "p50_ms": percentile(latencies, 0.5),
        "p95_ms": percentile(latencies, 0.95),
        "max_ms": max(latencies),
        "input_tokens": _token_stats([record.input_tokens for record in records], repeats),
        "http_errors": sum(1 for kind in kinds if kind == "http"),
        "transport_errors": sum(1 for kind in kinds if kind == "transport"),
        "timeouts": sum(1 for kind in kinds if kind == "timeout"),
        "answer_errors": answer_errors,
        "errors": errors,
        "error_rate": errors / repeats,
    }


def _prefix_tree(tree, question_count):
    """G3 的前 question_count 问前缀图：保留 start/q1..qN、激活边和 error_fallback target。"""
    if isinstance(question_count, bool) or not isinstance(question_count, int) or question_count < 1:
        raise ValueError("question_count must be a positive integer")
    fallback = tree.root.error_fallback
    if not fallback:
        raise ValueError("tree has no error_fallback target")
    nodes_by_id = {node.id: node for node in tree.root.nodes}
    keep = ["start"] + [f"q{i}" for i in range(1, question_count + 1)] + [fallback]
    missing = [node_id for node_id in keep if node_id not in nodes_by_id]
    if missing:
        raise ValueError(f"tree is missing node {missing[0]!r}")
    nodes = tuple(nodes_by_id[node_id] for node_id in keep)
    edges = [edge for edge in tree.root.edges if edge.source == "start" and edge.target == "q1"]
    for index in range(2, question_count + 1):
        edges.extend(
            edge for edge in tree.root.edges
            if edge.source == f"q{index - 1}" and edge.target == f"q{index}"
        )
    if len(edges) < question_count:
        raise ValueError("tree has no activation edge for every prefix question")
    return replace(tree, root=replace(tree.root, nodes=nodes, edges=tuple(edges)))


def _packed_canvas(transport, state, questions, index):
    started = time.monotonic()
    record = transport.post(state, questions, _CAPACITY_A_STAGE)
    answered = 0
    error = None
    if record.status == 200:
        for key, question in questions.items():
            try:
                typed_answer(key, question, record)
            except JevError:
                continue
            answered += 1
        if answered < len(questions):
            error = f"incomplete answers {answered}/{len(questions)}"
    else:
        error = record.error or f"HTTP {record.status}"
    latency_ms = (time.monotonic() - started) * 1000
    return {
        "index": index,
        "latency_ms": latency_ms,
        "question_count": answered,
        "request_count": 1,
        "error": error,
    }


def _serial_canvas(tree, text, transport, index):
    started = time.monotonic()
    decision, records = run_live_g3(tree, text, transport)
    latency_ms = (time.monotonic() - started) * 1000
    failed = [wave for wave in decision.waves if wave.failed]
    error = (failed[-1].error or "wave failed") if failed else None
    if not records:
        error = error or "no requests produced"
    if len(decision.waves) == len(records):
        successful = [record for wave, record in zip(decision.waves, records) if not wave.failed]
    else:
        successful = [record for record in records if record.status == 200]
    answered = 0
    for record in successful:
        if record.status == 200 and isinstance(record.body, dict):
            answers = record.body.get("answers")
            if isinstance(answers, dict):
                answered += len(answers)
    return {
        "index": index,
        "latency_ms": latency_ms,
        "question_count": answered,
        "request_count": len(records),
        "error": error,
    }


def _run_round(worker, workers):
    started = time.monotonic()
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index) for index in range(workers)]
        try:
            canvases = [future.result() for future in as_completed(futures)]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    canvases.sort(key=lambda canvas: canvas["index"])
    return canvases, time.monotonic() - started


def _concurrency_stats(canvases, round_walls, planned_n, rounds, workers):
    latencies = [canvas["latency_ms"] for canvas in canvases]
    wall_seconds = sum(round_walls)
    question_count = sum(canvas["question_count"] for canvas in canvases)
    request_count = sum(canvas["request_count"] for canvas in canvases)
    error_count = sum(1 for canvas in canvases if canvas["error"] is not None)
    return {
        "planned_question_count": planned_n * rounds * workers,
        "canvas_count": len(canvases),
        "wall_seconds": wall_seconds,
        "round_wall_seconds": list(round_walls),
        "request_count": request_count,
        "question_count": question_count,
        "questions_per_second": (question_count / wall_seconds) if wall_seconds > 0 else None,
        "requests_per_second": (request_count / wall_seconds) if wall_seconds > 0 else None,
        "canvas_p50_ms": percentile(latencies, 0.5) if latencies else None,
        "canvas_p95_ms": percentile(latencies, 0.95) if latencies else None,
        "canvas_max_ms": max(latencies) if latencies else None,
        "error_count": error_count,
        "error_rate": (error_count / len(canvases)) if canvases else None,
        "canvases": canvases,
    }


def _comparison(a, b):
    b_qps = b["questions_per_second"]
    b_p95 = b["canvas_p95_ms"]
    a_qps = a["questions_per_second"]
    a_p95 = a["canvas_p95_ms"]
    if b_qps is None or b_p95 is None:
        floor = None
        qps_ok = False
        p95_ok = False
        qps_ratio = None
        p95_ratio = None
    else:
        floor = _QPS_FLOOR * b_qps
        qps_ok = a_qps is not None and a_qps >= floor
        p95_ok = a_p95 is not None and a_p95 <= b_p95
        qps_ratio = (a_qps / b_qps) if b_qps > 0 and a_qps is not None else None
        p95_ratio = (a_p95 / b_p95) if b_p95 > 0 and a_p95 is not None else None
    passed = bool(qps_ok and p95_ok)
    return {
        "questions_per_second_floor": floor,
        "questions_per_second_ratio": qps_ratio,
        "questions_per_second_ok": bool(qps_ok),
        "canvas_p95_ratio": p95_ratio,
        "canvas_p95_ok": bool(p95_ok),
        "baseline_error_count": b["error_count"],
        "baseline_error_rate": b["error_rate"],
        "baseline_has_errors": b["error_count"] > 0,
        "pass": passed,
        "status": "pass" if passed else "fail",
    }


def _run_packed_rounds(transport, state, questions, rounds, workers):
    canvases = []
    round_walls = []
    for round_index in range(rounds):
        def worker(index, questions=questions):
            return _packed_canvas(transport, state, questions, index)
        round_canvases, wall_seconds = _run_round(worker, workers)
        for canvas in round_canvases:
            canvas["round"] = round_index
        canvases.extend(round_canvases)
        round_walls.append(wall_seconds)
    return canvases, round_walls


def _run_serial_rounds(tree, text, transport, rounds, workers):
    canvases = []
    round_walls = []
    for round_index in range(rounds):
        def worker(index, tree=tree):
            return _serial_canvas(tree, text, transport, index)
        round_canvases, wall_seconds = _run_round(worker, workers)
        for canvas in round_canvases:
            canvas["round"] = round_index
        canvases.extend(round_canvases)
        round_walls.append(wall_seconds)
    return canvases, round_walls


def run_capacity(transport, groups, tree, *, scan_repeats=20, rounds=10, workers=8):
    if isinstance(scan_repeats, bool) or not isinstance(scan_repeats, int) or scan_repeats < 1:
        raise ValueError("scan_repeats must be a positive integer")
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    for name in ("G2", "G3"):
        if name not in groups:
            raise ValueError(f"unknown question group {name!r}")
    tree_question_ids = {node.id for node in tree.root.nodes if isinstance(node, QuestionNode)}
    if set(groups["G3"]) != tree_question_ids:
        raise ValueError("G3 question group does not match the routing tree")

    text, state, state_tokens = _capacity_state()

    scan = {
        "state_text": text,
        "state_est_tokens": state_tokens,
        "scan_repeats": scan_repeats,
        "ns": list(_SCAN_NS),
        "pass_rule": "n<=8: errors==0 and p95_ms<=2*baseline_p95_ms; n>8: record-only",
        "by_n": {},
    }
    for n in _SCAN_NS:
        questions = expand_questions(groups["G2"], n)
        scan["by_n"][str(n)] = _scan_n(transport, state, questions, scan_repeats)
    baseline_p95 = scan["by_n"]["1"]["p95_ms"]
    scan["baseline_p95_ms"] = baseline_p95
    for n in _SCAN_NS:
        entry = scan["by_n"][str(n)]
        if n <= 8:
            entry["p95_limit_ms"] = 2 * baseline_p95
            entry["p95_within_limit"] = entry["p95_ms"] <= 2 * baseline_p95
            entry["zero_errors"] = entry["errors"] == 0
            entry["pass"] = entry["zero_errors"] and entry["p95_within_limit"]
            entry["status"] = "pass" if entry["pass"] else "fail"
        else:
            entry["p95_limit_ms"] = None
            entry["p95_within_limit"] = None
            entry["zero_errors"] = entry["errors"] == 0
            entry["pass"] = None
            entry["status"] = "recorded"
    failed_n = [n for n in _SCAN_PASS_NS if not scan["by_n"][str(n)]["pass"]]
    scan["failed_n"] = failed_n
    scan["status"] = "pass" if not failed_n else "fail"

    concurrency = {
        "rounds": rounds,
        "workers": workers,
        "ns": list(_CONCURRENCY_NS),
        "pass_rule": (
            "questions_per_second >= 0.7 * serial baseline and "
            "canvas_p95_ms <= serial baseline canvas_p95_ms"
        ),
        "by_n": {},
    }
    prefix_tree = _prefix_tree(tree, 2)
    for n in _CONCURRENCY_NS:
        questions = expand_questions(groups["G3"], n)
        a_canvases, a_rounds = _run_packed_rounds(transport, state, questions, rounds, workers)
        a_stats = _concurrency_stats(a_canvases, a_rounds, n, rounds, workers)
        b_tree = prefix_tree if n == 2 else tree
        b_canvases, b_rounds = _run_serial_rounds(b_tree, text, transport, rounds, workers)
        b_stats = _concurrency_stats(b_canvases, b_rounds, n, rounds, workers)
        concurrency["by_n"][str(n)] = {
            "n": n,
            "packed": a_stats,
            "serial": b_stats,
            "comparison": _comparison(a_stats, b_stats),
        }
    concurrency["status"] = (
        "pass" if all(item["comparison"]["pass"] for item in concurrency["by_n"].values())
        else "fail"
    )
    return {"scan": scan, "concurrency": concurrency}


_CONTEXT_TOKENS = 4096
_INPUT_TOKENS = 2048
_MIN_INPUT_TOKENS = 256
_DEFAULT_SAFETY = 128
_H3_RESERVE = 200
_COVERAGE_STATES = (256, 1024, 2048)
_COVERAGE_COUNTS = (1, 2, 4, 8)
_COVERAGE_GROUPS = ("G1", "G2")
_BOUNDARY_TARGETS = (("near", 3900), ("at", 4096), ("over", 4352))
_H3_COVERAGE_STAGE = "H3-coverage"
_H3_BOUNDARY_STAGE = "H3-boundary"
_H3_PROBE_STAGE = "H3-probe"
_API_KEY_ENV = "ROUTER_JEV_API_KEY"
# 合成文本只描述“用于测量窗口预算”，不含任何用户请求内容。
_SYNTHETIC_CLAUSE = "这是用于测量决策服务窗口预算的合成文本，不含任何真实用户数据。"


@dataclass(frozen=True)
class BudgetDecision:
    enabled: bool
    effective_context: int
    state_budget: int
    raw_overhead: int


def window_budget(context, input_tokens, raw_overhead, calibration, safety):
    if min(context, input_tokens, raw_overhead) < 0 or calibration < 1 or safety < 0:
        raise ValueError("invalid budget arguments")
    effective = math.floor((context - safety) / calibration)
    return BudgetDecision(
        enabled=raw_overhead + 256 <= effective,
        effective_context=effective,
        state_budget=min(input_tokens, effective - raw_overhead),
        raw_overhead=raw_overhead,
    )


def calibrate_budget(records, safety=128):
    usable = [dict(item) for item in records
              if item.get("status") == 200 and isinstance(item.get("input_tokens"), int)
              and item["input_tokens"] > 0
              and isinstance(item.get("wire_est_tokens"), int) and item["wire_est_tokens"] > 0]
    if not usable:
        raise ValueError("no usable token observations")
    calibration = max(1.0, max(item["input_tokens"] / item["wire_est_tokens"] for item in usable))
    return {"calibration": calibration, "safety": safety, "records": usable,
            "max_est_over_actual": max(item["wire_est_tokens"] / item["input_tokens"] for item in usable)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _state_bucket(tokens):
    if tokens <= (_COVERAGE_STATES[0] + _COVERAGE_STATES[1]) // 2:
        return _COVERAGE_STATES[0]
    if tokens <= (_COVERAGE_STATES[1] + _COVERAGE_STATES[2]) // 2:
        return _COVERAGE_STATES[1]
    return _COVERAGE_STATES[2]


def _count_bucket(count):
    return count if count in _COVERAGE_COUNTS else None


def _coverage_cells(observations):
    cells = []
    for group in _COVERAGE_GROUPS:
        for state in _COVERAGE_STATES:
            for count in _COVERAGE_COUNTS:
                matches = [
                    row for row in observations
                    if row.get("group") == group
                    and isinstance(row.get("state_est_tokens"), int)
                    and _state_bucket(row["state_est_tokens"]) == state
                    and _count_bucket(row.get("question_count")) == count
                    and row.get("status") == 200
                    and isinstance(row.get("input_tokens"), int)
                ]
                cells.append({
                    "group": group, "state_tokens": state, "question_count": count,
                    "covered": bool(matches), "source": "reused" if matches else "missing",
                    "observations": len(matches),
                    "refs": [
                        {"source": item.get("source"), "stage": item.get("stage"),
                         "sample_id": item.get("sample_id"),
                         "state_est_tokens": item.get("state_est_tokens"),
                         "input_tokens": item.get("input_tokens")}
                        for item in matches[:3]
                    ],
                    "fill": None,
                })
    return cells


def _observations_from_calls(calls):
    rows = []
    for call in calls:
        rows.append({
            "source": "H1", "stage": call.get("stage"), "sample_id": call.get("sample_id"),
            "group": call.get("group"), "path": call.get("path"),
            "question_id": call.get("question_id"), "question_count": call.get("question_count"),
            "state_est_tokens": call.get("state_est_tokens"),
            "wire_est_tokens": call.get("wire_est_tokens"),
            "status": call.get("status"), "input_tokens": call.get("input_tokens"),
            "duration_ms": call.get("duration_ms"), "error": call.get("error"),
        })
    return rows


def _observations_from_capacity(capacity, groups):
    scan = (capacity or {}).get("scan") or {}
    state_text = scan.get("state_text")
    state_est = scan.get("state_est_tokens")
    rows = []
    if not isinstance(state_text, str):
        return rows
    for key, entry in (scan.get("by_n") or {}).items():
        n = int(key)
        questions = expand_questions(groups["G2"], n)
        wire_est = estimate_tokens(state_text + "\n" + canonical_questions(questions))
        for value in (entry.get("input_tokens") or {}).get("values") or []:
            rows.append({
                "source": "H2", "stage": _CAPACITY_SCAN_STAGE, "sample_id": None,
                "group": "G2", "path": "scan", "question_id": "*",
                "question_count": n, "state_est_tokens": state_est,
                "wire_est_tokens": wire_est, "status": 200, "input_tokens": value,
                "duration_ms": None, "error": None,
            })
    return rows


def _observation_from_cached(cached, *, source):
    return {
        "source": source, "stage": cached["response"].stage, "sample_id": cached["sample_id"],
        "group": cached["group"], "path": cached["path"], "question_id": cached["question_id"],
        "question_count": cached["question_count"], "state_est_tokens": cached["state_est_tokens"],
        "wire_est_tokens": cached["wire_est_tokens"], "status": cached["response"].status,
        "input_tokens": cached["response"].input_tokens,
        "duration_ms": cached["response"].duration_ms, "error": cached["response"].error,
    }


def _synthetic_text(target_state_tokens):
    """生成 state 估算恰为 target_state_tokens 的非敏感合成文本。"""
    if isinstance(target_state_tokens, bool) or not isinstance(target_state_tokens, int) \
            or target_state_tokens < 1:
        raise ValueError("target_state_tokens must be a positive integer")
    base_tokens = estimate_tokens(DEFAULT_STATE_TEMPLATE.format(request=""))
    needed = target_state_tokens - base_tokens
    if needed <= 0:
        return ""
    clause = _SYNTHETIC_CLAUSE
    text = (clause * math.ceil(needed / len(clause)))[:needed]
    while text and estimate_tokens(DEFAULT_STATE_TEMPLATE.format(request=text)) > target_state_tokens:
        text = text[:-1]
    tokens = estimate_tokens(DEFAULT_STATE_TEMPLATE.format(request=text))
    if tokens != target_state_tokens:
        raise ValueError(f"synthetic text estimates {tokens} tokens, expected {target_state_tokens}")
    return text


def _raw_overhead(questions):
    return estimate_tokens(canonical_questions(questions) + "\n"
                           + DEFAULT_STATE_TEMPLATE.format(request=""))


def _windowed_payload(text, questions, *, context_tokens, input_tokens, calibration, safety):
    """按生产窗口公式给出一次请求的窗口化 state 与预测值。"""
    raw_overhead = _raw_overhead(questions)
    decision = window_budget(context_tokens, input_tokens, raw_overhead, calibration, safety)
    raw_state_est = estimate_tokens(render_state(text))
    post_window_est = raw_overhead + min(raw_state_est, decision.state_budget)
    gate_passed = bool(decision.enabled) and post_window_est <= decision.effective_context
    payload = {
        "raw_overhead": raw_overhead,
        "raw_state_est_tokens": raw_state_est,
        "raw_total_est_tokens": raw_state_est + raw_overhead,
        "effective_context": decision.effective_context,
        "state_budget": decision.state_budget,
        "enabled": bool(decision.enabled),
        "post_window_est": post_window_est,
        "gate_passed": gate_passed,
        "truncated": False,
        "sent_state_est_tokens": None,
        "state": None,
    }
    if gate_passed:
        windowed = apply_window(text, decision.state_budget)
        state = render_state(windowed.text)
        payload["truncated"] = bool(windowed.truncated)
        payload["state"] = state
        payload["sent_state_est_tokens"] = estimate_tokens(state)
    return payload


def _public_payload(payload):
    """请求记录只保留可序列化的预测值，不落 state 原文。"""
    return {key: value for key, value in payload.items() if key != "state"}


def _gate_check(entry, calibration, context_tokens, input_tokens, safety):
    """用最终 CALIBRATION 复核一次正常请求是否会被窗口公式拦截。"""
    raw_overhead = entry.get("raw_overhead")
    state_est = entry.get("sent_state_est_tokens")
    if not isinstance(state_est, int):
        state_est = entry.get("raw_state_est_tokens")
    if not isinstance(raw_overhead, int) or not isinstance(state_est, int):
        return False
    decision = window_budget(context_tokens, input_tokens, raw_overhead, calibration, safety)
    post_window_est = raw_overhead + min(state_est, decision.state_budget)
    return bool(decision.enabled) and post_window_est <= decision.effective_context


def _observation_label(row):
    return "/".join(str(row.get(key)) for key in ("source", "group", "question_id", "stage"))


def _error_message(record):
    body = record.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
        elif isinstance(error, str):
            message = error
        else:
            message = body.get("detail") or body.get("message")
        if isinstance(message, str) and message:
            return message[:300]
    return record.error[:300] if record.error else None


def _run_coverage_fill(transport, cache, groups, rows, cells, *, context_tokens, input_tokens,
                       calibration, safety, spent, reserve):
    """按覆盖矩阵补齐缺失组合；返回 (请求记录, 已用额度)。"""
    requests = []
    for cell in cells:
        if cell["covered"]:
            continue
        if spent >= reserve:
            cell["fill"] = {"sent": False, "status": None, "reason": "reserve exhausted"}
            requests.append({"group": cell["group"], "question_count": cell["question_count"],
                             "target_state_tokens": cell["state_tokens"], "status": None,
                             "error": "reserve exhausted", "gate_passed": None,
                             "skipped": True, "route_usable": True,
                             "intentional_over_limit": False})
            continue
        questions = expand_questions(groups[cell["group"]], cell["question_count"])
        text = _synthetic_text(cell["state_tokens"])
        payload = _windowed_payload(text, questions, context_tokens=context_tokens,
                                    input_tokens=input_tokens, calibration=calibration,
                                    safety=safety)
        entry = {"group": cell["group"], "question_count": cell["question_count"],
                 "target_state_tokens": cell["state_tokens"], "route_usable": True,
                 "intentional_over_limit": False, **_public_payload(payload),
                 "wire_est_tokens": None, "status": None, "error": None,
                 "input_tokens": None, "duration_ms": None}
        if payload["gate_passed"]:
            cached = _call(
                transport, cache,
                ("H3", "coverage", cell["group"], cell["question_count"], cell["state_tokens"]),
                stage=_H3_COVERAGE_STAGE, state=payload["state"], questions=questions,
                sample_id=f"synthetic-{cell['group']}-{cell['state_tokens']}-{cell['question_count']}",
                group=cell["group"], path="H3-coverage", question_id="*",
            )
            spent += 1
            record = cached["response"]
            entry.update({
                "status": record.status, "error": record.error,
                "input_tokens": record.input_tokens,
                "wire_est_tokens": cached["wire_est_tokens"],
                "sent_state_est_tokens": cached["state_est_tokens"],
                "duration_ms": record.duration_ms,
            })
            rows.append(_observation_from_cached(cached, source="H3-coverage"))
        else:
            entry["error"] = "window gate intercepted"
        cell["fill"] = {"sent": payload["gate_passed"], "status": entry["status"],
                        "source": "synthetic",
                        "sent_state_est_tokens": entry["sent_state_est_tokens"]}
        if entry["status"] == 200:
            cell["covered"] = True
            cell["source"] = "synthetic"
        requests.append(entry)
    return requests, spent


def run_budget(transport, groups, tree, *, observations=(), cache=None,
               context_tokens=_CONTEXT_TOKENS, input_tokens=_INPUT_TOKENS,
               safety=_DEFAULT_SAFETY, reserve=_H3_RESERVE):
    """H3：复用 H1/H2 token 观测做校准，补齐覆盖矩阵，验证窗口公式与 4096 边界。"""
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise ValueError("reserve must be a nonnegative integer")
    if context_tokens < _MIN_INPUT_TOKENS or input_tokens < _MIN_INPUT_TOKENS:
        raise ValueError("context_tokens and input_tokens must be >= 256")
    for name in _COVERAGE_GROUPS + ("G3",):
        if name not in groups:
            raise ValueError(f"unknown question group {name!r}")
    tree_ids = {node.id for node in tree.root.nodes if isinstance(node, QuestionNode)}
    if set(groups["G3"]) != tree_ids:
        raise ValueError("G3 question group does not match the routing tree")

    cache = {} if cache is None else cache
    rows = [dict(item) for item in observations]
    counts = {"reused_observations": len(rows), "coverage_requests": 0,
              "boundary_requests": 0, "probe_requests": 0, "route_usable_samples": 0}
    fail_reasons = []
    conditional_reasons = []
    spent = 0

    cells = _coverage_cells(rows)
    try:
        provisional = calibrate_budget(rows, safety=safety)["calibration"]
    except ValueError:
        provisional = 1.0

    coverage_requests, spent = _run_coverage_fill(
        transport, cache, groups, rows, cells, context_tokens=context_tokens,
        input_tokens=input_tokens, calibration=provisional, safety=safety,
        spent=spent, reserve=reserve,
    )
    counts["coverage_requests"] = spent

    calibration = None
    try:
        calibration = calibrate_budget(rows, safety=safety)
    except ValueError as exc:
        fail_reasons.append(f"calibration: {exc}")
    coefficient = calibration["calibration"] if calibration else 1.0
    g3_raw_overhead = _raw_overhead(groups["G3"])
    decision = window_budget(context_tokens, input_tokens, g3_raw_overhead, coefficient, safety)

    intercepts = []
    for entry in coverage_requests:
        if entry.get("skipped"):
            continue
        entry["final_gate_passed"] = _gate_check(
            entry, coefficient, context_tokens, input_tokens, safety)
        if not entry["final_gate_passed"]:
            intercepts.append(entry)

    boundaries = []
    for kind, target in _BOUNDARY_TARGETS:
        state_target = max(1, target - g3_raw_overhead)
        text = _synthetic_text(state_target)
        payload = _windowed_payload(text, groups["G3"], context_tokens=context_tokens,
                                    input_tokens=input_tokens, calibration=coefficient,
                                    safety=safety)
        entry = {"kind": kind, "target_total_tokens": target,
                 "target_state_tokens": state_target, "route_usable": True,
                 "intentional_over_limit": False, **_public_payload(payload),
                 "wire_est_tokens": None, "status": None, "error": None,
                 "input_tokens": None, "duration_ms": None}
        entry["identified_over_context"] = entry["raw_total_est_tokens"] > context_tokens
        entry["identified_over_effective"] = entry["raw_total_est_tokens"] > decision.effective_context
        if payload["gate_passed"] and spent < reserve:
            cached = _call(
                transport, cache, ("H3", "boundary", kind), stage=_H3_BOUNDARY_STAGE,
                state=payload["state"], questions=groups["G3"],
                sample_id=f"synthetic-boundary-{kind}", group="G3",
                path="H3-boundary", question_id="*",
            )
            spent += 1
            record = cached["response"]
            entry.update({
                "status": record.status, "error": record.error,
                "input_tokens": record.input_tokens,
                "wire_est_tokens": cached["wire_est_tokens"],
                "sent_state_est_tokens": cached["state_est_tokens"],
                "duration_ms": record.duration_ms,
            })
            rows.append(_observation_from_cached(cached, source="H3-boundary"))
        else:
            entry["error"] = (
                "window gate intercepted" if not payload["gate_passed"] else "reserve exhausted")
            if payload["gate_passed"]:
                conditional_reasons.append(f"reserve exhausted before boundary {kind}")
        entry["final_gate_passed"] = _gate_check(
            entry, coefficient, context_tokens, input_tokens, safety)
        if not entry["final_gate_passed"]:
            intercepts.append(entry)
        boundaries.append(entry)
    counts["boundary_requests"] = sum(1 for entry in boundaries if entry["status"] is not None)

    probe = None
    over_target = _BOUNDARY_TARGETS[-1][1]
    raw_text = _synthetic_text(max(1, over_target - g3_raw_overhead))
    raw_state = render_state(raw_text)
    raw_state_est = estimate_tokens(raw_state)
    ratio = calibration["max_est_over_actual"] if calibration else 1.0
    raw_total_est = raw_state_est + g3_raw_overhead
    actual_lower_bound = raw_total_est / ratio if ratio > 0 else raw_total_est
    prediction = {
        "raw_state_est_tokens": raw_state_est,
        "raw_total_est_tokens": raw_total_est,
        "est_over_actual_upper": ratio,
        "predicted_actual_lower_bound": actual_lower_bound,
        "predicted_over_context": raw_total_est > context_tokens,
        "predicted_over_effective": raw_total_est > decision.effective_context,
        "expected": "error" if actual_lower_bound > context_tokens else "uncertain",
    }
    probe = {"kind": "over_raw_probe", "intentional_over_limit": True, "route_usable": False,
             "excluded_from_route_usable": True, "sent": False,
             "excluded_reason": "intentional_over_limit", "prediction": prediction,
             "status": None, "error": None, "error_code": None, "error_message": None,
             "input_tokens": None, "wire_est_tokens": None, "state_est_tokens": None,
             "duration_ms": None, "prediction_matched": None}
    if spent >= reserve:
        conditional_reasons.append("reserve exhausted before over-limit probe")
    if spent < reserve:
        cached = _call(
            transport, cache, ("H3", "probe", "over"), stage=_H3_PROBE_STAGE,
            state=raw_state, questions=groups["G3"],
            sample_id="synthetic-over-limit-probe", group="G3",
            path="H3-probe", question_id="*",
        )
        spent += 1
        record = cached["response"]
        if prediction["expected"] == "error":
            matched = record.status is not None and record.status >= 400
        else:
            matched = record.status is not None
        probe.update({
            "sent": True, "status": record.status, "error": record.error,
            "error_code": record.status, "error_message": _error_message(record),
            "input_tokens": record.input_tokens, "wire_est_tokens": cached["wire_est_tokens"],
            "state_est_tokens": cached["state_est_tokens"],
            "accepted_by_server": record.status == 200,
            "duration_ms": record.duration_ms, "prediction_matched": matched,
        })
        rows.append(_observation_from_cached(cached, source="H3-probe"))
        if record.status is None:
            conditional_reasons.append(
                "over-limit probe produced no HTTP status; error body not verified")
        elif prediction["expected"] == "error" and record.status == 200:
            conditional_reasons.append(
                "over-limit probe was accepted although the calibrated lower bound exceeds context")

    for entry in coverage_requests + boundaries + [probe]:
        est = entry.get("wire_est_tokens")
        actual = entry.get("input_tokens")
        if isinstance(est, int) and isinstance(actual, int) and est > 0:
            entry["upper_bound_tokens"] = est * coefficient + safety
            entry["within_upper_bound"] = actual <= entry["upper_bound_tokens"]

    violations = []
    missing_usage = 0
    for row in rows:
        if row.get("status") == 200 and not isinstance(row.get("input_tokens"), int):
            missing_usage += 1
        est = row.get("wire_est_tokens")
        actual = row.get("input_tokens")
        if isinstance(est, int) and isinstance(actual, int) and est > 0:
            row["upper_bound_tokens"] = est * coefficient + safety
            row["within_upper_bound"] = actual <= row["upper_bound_tokens"]
            if not row["within_upper_bound"]:
                violations.append(row)
    if violations:
        fail_reasons.append(
            f"{len(violations)} observation(s) above calibrated upper bound "
            f"({_observation_label(violations[0])})")
    if missing_usage:
        conditional_reasons.append(
            f"{missing_usage} successful observation(s) missing usage.input_tokens")

    counts["probe_requests"] = 1 if probe["sent"] else 0
    counts["route_usable_samples"] = (
        sum(1 for entry in coverage_requests if entry["status"] == 200)
        + sum(1 for entry in boundaries if entry["status"] == 200))
    counts["h3_requests"] = spent
    counts["reserve_used"] = spent
    counts["reserve_remaining"] = reserve - spent
    counts["intentional_over_limit"] = counts["probe_requests"]
    if spent > reserve:
        fail_reasons.append(f"H3 used {spent} requests, above reserve {reserve}")

    if intercepts:
        fail_reasons.append(
            f"{len(intercepts)} normal request(s) must be intercepted by the window formula")
    for entry in coverage_requests + boundaries:
        if entry.get("status") is not None and entry["status"] != 200:
            fail_reasons.append(
                f"{entry.get('kind') or entry.get('group')} normal request returned "
                f"status {entry['status']}")
    reused_cells = sum(1 for cell in cells if cell["observations"] > 0)
    filled_cells = sum(1 for cell in cells if cell["source"] == "synthetic" and cell["covered"])
    covered_cells = sum(1 for cell in cells if cell["covered"])
    incomplete = len(cells) - covered_cells
    if incomplete:
        conditional_reasons.append(f"{incomplete} coverage cell(s) not measured")

    if fail_reasons:
        status = "fail"
    elif conditional_reasons:
        status = "conditional"
    else:
        status = "pass"

    coverage = {
        "states": list(_COVERAGE_STATES), "counts": list(_COVERAGE_COUNTS),
        "groups": list(_COVERAGE_GROUPS), "cells": cells,
        "total_cells": len(cells), "covered_cells": covered_cells,
        "reused_cells": reused_cells, "synthetic_cells": filled_cells,
        "complete": incomplete == 0, "requests": coverage_requests,
    }
    formula = {
        "context_tokens": context_tokens, "input_tokens": input_tokens, "safety": safety,
        "calibration": coefficient, "raw_overhead": g3_raw_overhead,
        "effective_context": decision.effective_context,
        "state_budget": decision.state_budget, "enabled": bool(decision.enabled),
        "min_input_tokens": _MIN_INPUT_TOKENS,
        "effective_context_formula": "floor((context_tokens - SAFETY) / CALIBRATION)",
        "state_budget_formula": "min(input_tokens, effective_context - raw_overhead)",
        "enabled_formula": "raw_overhead + MIN_INPUT_TOKENS <= effective_context",
        "post_window_formula": "raw_overhead + min(state_est_tokens, state_budget)",
        "intercept_condition": "enabled is False or post_window_est > effective_context",
        "wire_serialization": "json.dumps(..., ensure_ascii=False, separators=(',', ':'))",
        "token_estimator": "router.jev.window.estimate_tokens",
        "boundary_targets": [{"kind": kind, "total_tokens": total}
                             for kind, total in _BOUNDARY_TARGETS],
    }
    return {
        "status": status,
        "fail_reasons": fail_reasons,
        "conditional_reasons": conditional_reasons,
        "formula": formula,
        "calibration": calibration,
        "coverage": coverage,
        "boundaries": boundaries,
        "probes": [probe],
        "observations": rows,
        "counts": counts,
        "reserve": {"limit": reserve, "used": spent, "remaining": reserve - spent},
    }


def _capacity_status(capacity):
    scan = (capacity or {}).get("scan") or {}
    concurrency = (capacity or {}).get("concurrency") or {}
    if scan.get("status") == "pass" and concurrency.get("status") == "pass":
        return "pass"
    return "fail"


def _safe_message(exc, api_key=""):
    """异常信息脱敏：不出现 Authorization header 或 API key。"""
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "***")
    for marker in ("Authorization:", "Bearer "):
        head, separator, _ = message.partition(marker)
        if separator:
            message = head + marker + "***"
    return message[:500]


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="spike_jev_batch",
        description="JEV 多问题批量 spike：H1 等价性 / H2 容量 / H3 预算；--dry-run 只打印请求计划。",
    )
    parser.add_argument("--url", default="http://127.0.0.1:30104")
    parser.add_argument("--model", default="d1-2B")
    parser.add_argument("--samples", required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="all")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--out-dir", default=str(ROOT / "artifacts" / "jev-batch-spike"))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _graph_document(groups):
    return {
        "tree": _g3_tree_dict(),
        "questions": {name: json.loads(canonical_questions(value))
                      for name, value in groups.items()},
        "wire": {
            "serialization": "json.dumps(..., ensure_ascii=False, separators=(',', ':'))",
            "token_estimator": "router.jev.window.estimate_tokens",
            "state_template": DEFAULT_STATE_TEMPLATE,
        },
    }


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.repeats != 2:
        print("--repeats 只接受 2（保留该参数以兼容方案命令，不翻倍 A/B）", file=sys.stderr)
        return 2
    if args.limit < 0 or args.max_requests < 1 or not 0 < args.timeout <= 120:
        print("--limit 必须 >=0，--max-requests 必须 >=1，--timeout 必须在 (0,120]", file=sys.stderr)
        return 2
    try:
        samples = load_samples(Path(args.samples), args.limit)
        plan = plan_request_budget(len(samples), args.scenario)
    except (OSError, ValueError) as exc:
        print(f"样本或参数错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "planned_requests": plan.total,
        "by_stage": {"h1": plan.h1, "repeats": plan.repeats, "scan": plan.scan,
                     "concurrency": plan.concurrency, "reserve": plan.reserve},
        "max_requests": args.max_requests,
    }, ensure_ascii=False))
    if plan.total > args.max_requests:
        print(f"计划请求数 {plan.total} 超过 --max-requests {args.max_requests}", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = {}
    api_key = os.environ.get(_API_KEY_ENV, "")
    transport = None
    statuses = {"equivalence": "not_run", "capacity": "not_run", "budget": "not_run"}
    started_at = datetime.now().astimezone()
    parameters = {
        "url": args.url, "model": args.model, "scenario": args.scenario,
        "samples": str(args.samples), "sample_count": len(samples), "limit": args.limit,
        "repeats": args.repeats, "max_requests": args.max_requests,
        "out_dir": str(out_dir), "timeout": args.timeout,
        "context_tokens": _CONTEXT_TOKENS, "input_tokens": _INPUT_TOKENS,
        "safety": _DEFAULT_SAFETY, "h3_reserve": plan.reserve,
        "api_key_env": _API_KEY_ENV, "api_key_present": bool(api_key),
    }
    try:
        groups = build_question_groups()
        tree = build_g3_tree()
        transport = SpikeTransport(args.url, args.model, api_key=api_key,
                                   timeout=args.timeout, max_requests=args.max_requests)
        # 首个产物必须是图，方便失败后仍能核对本轮使用的 G3 画布。
        write_json(out_dir / "g3-graph.json", _graph_document(groups))
        equivalence = None
        capacity = None
        if args.scenario in {"all", "equivalence"}:
            warmup = None
            if args.scenario == "all":
                warmup = run_equivalence(samples[:20], groups, tree, transport, cache=cache,
                                         group_names=("G1", "G2"), repeat_samples=20)
            equivalence = run_equivalence(samples, groups, tree, transport, cache=cache,
                                          group_names=("G1", "G2", "G3"), repeat_samples=20)
            if warmup is not None:
                equivalence["warmup"] = {
                    "samples": len(samples[:20]), "groups": ["G1", "G2"],
                    "requests": len(warmup["calls"]), "summary": warmup["summary"],
                }
            statuses["equivalence"] = equivalence["summary"]["status"]
            write_json(out_dir / "equivalence.json", equivalence)
        if args.scenario in {"all", "capacity"}:
            capacity = run_capacity(transport, groups, tree)
            statuses["capacity"] = _capacity_status(capacity)
            write_json(out_dir / "capacity.json", capacity)
        if args.scenario in {"all", "budget"}:
            observations = []
            if equivalence is not None:
                observations.extend(_observations_from_calls(equivalence["calls"]))
            if capacity is not None:
                observations.extend(_observations_from_capacity(capacity, groups))
            budget_doc = run_budget(transport, groups, tree, observations=observations,
                                    cache=cache, reserve=plan.reserve)
            statuses["budget"] = budget_doc["status"]
            write_json(out_dir / "budget.json", budget_doc)
    except Exception as exc:
        message = _safe_message(exc, api_key)
        write_json(out_dir / "summary.json", {
            "status": "fail",
            "statuses": statuses,
            "planned_requests": plan.total,
            "actual_requests": transport.count if transport is not None else 0,
            "stage_counts": dict(transport.stage_counts) if transport is not None else {},
            "parameters": parameters,
            "error": {"type": type(exc).__name__, "message": message},
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
        })
        print(f"执行失败：{type(exc).__name__}: {message}", file=sys.stderr)
        return 2
    if any(value == "fail" for value in statuses.values()):
        overall = "fail"
    elif any(value == "conditional" for value in statuses.values()):
        overall = "conditional"
    else:
        overall = "pass"
    write_json(out_dir / "summary.json", {
        "status": overall,
        "statuses": statuses,
        "planned_requests": plan.total,
        "actual_requests": transport.count,
        "stage_counts": dict(transport.stage_counts),
        "parameters": parameters,
        "requests": {"planned": plan.total, "actual": transport.count,
                     "stage_counts": dict(transport.stage_counts),
                     "reserve": {"limit": plan.reserve}},
        "artifacts": ["g3-graph.json", "equivalence.json", "capacity.json",
                      "budget.json", "summary.json"],
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
    })
    print(json.dumps({
        "status": overall, "statuses": statuses, "planned_requests": plan.total,
        "actual_requests": transport.count,
    }, ensure_ascii=False))
    return 0 if overall == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
