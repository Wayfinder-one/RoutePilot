"""Natural-language capability routing (decision-model candidate scoring).

The operator writes, for every candidate model, a short **natural-language
capability description**.  At request time the deployed JEV decision model
answers one ``choice`` question — "which candidate matches this request?" —
and returns a probability per candidate.  The highest-probability candidate
becomes the route target, subject to two safety gates and a fail-open
fallback:

* ``min_prob``   — top probability must clear this floor;
* ``min_margin`` — ``p_top - p_second`` must clear this floor (the model is
  unsure when two candidates are close);
* ``fallback_model`` — used when the decision fails or is below a gate.

This module is pure logic (no torch, no FastAPI): the model call lives in
:meth:`router.signals.jev.JevTagger.score_choice`, the demo CLI in
``scripts/capability_route_demo.py``, and the pipeline integration is a thin
caller of :-func:`decide` / :func:`score_with_tagger`.

Config shape (JSON)::

    {
      "version": 1,
      "decision": {
        "instructions": "根据候选模型的能力说明和本次请求的任务内容，选择最适合处理该请求的模型。",
        "min_prob": 0.45,
        "min_margin": 0.10,
        "fallback_model": "qwen3-32b"
      },
      "candidates": {
        "qwen3-8b": "适合短翻译、字段抽取、事实问答、基础算术；上下文预算 8192 token",
        "qwen3-32b": "适合带约束的算法实现、SQL 编写、局部代码调试；上下文预算 16384 token",
        "deepseek-v3": "适合复杂推理、带测试的并发代码和分布式架构设计"
      }
    }

Candidate keys are **model IDs from the router model catalog** (the same ids a
pipeline leaf can route to), so a decision result is directly resolvable to an
upstream backend.  The description is free text and is the only thing that
encodes the routing policy — editing it changes the assignment without any
code change (see ``configs/capability_routing.example*.json``).

Known caveat (same as the signal questions): the deployed r4b3 model was
calibrated on canonical JEV wording; paraphrasing ``instructions`` or
descriptions changes probabilities.  Keep them stable and re-run the golden
set after edits (``probe.py`` in the training repo shows the drift).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# ``MAX_CAPABILITY_CHARS`` is owned by the model-catalog boundary
# (``router.decision.catalog``); this policy module is retired by the cleanup
# task while the catalog outlives it, so the import direction points at the
# survivor.
from router.decision.catalog import MAX_CAPABILITY_CHARS

#: Default decision question (mirrors the article's "决策指令" + capability table).
DEFAULT_INSTRUCTIONS = (
    "根据候选模型的能力说明和本次请求的任务内容，选择最适合处理该请求的模型。"
    "优先选择能够完成任务的较低成本候选。"
)
DEFAULT_MIN_PROB = 0.45
DEFAULT_MIN_MARGIN = 0.10
MAX_CANDIDATES = 255
MAX_INSTRUCTIONS_CHARS = 4000


class CapabilityConfigError(ValueError):
    """The capability-routing config is malformed."""


def _unit(value, name: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not 0.0 <= float(value) <= 1.0:
        raise CapabilityConfigError(f"{name} must be a number in [0,1], got {value!r}")
    return float(value)


def validate_capability_config(raw: dict) -> dict:
    """Validate + normalise a capability-routing config (raises on malformed)."""
    if not isinstance(raw, dict):
        raise CapabilityConfigError("capability config must be a JSON object")

    candidates = raw.get("candidates")
    if not isinstance(candidates, dict) or not candidates:
        raise CapabilityConfigError("candidates must be a nonempty object")
    if not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise CapabilityConfigError(
            f"candidates must contain 2..{MAX_CANDIDATES} models, got {len(candidates)}")
    normalised: dict[str, str] = {}
    for model, capability in candidates.items():
        if not isinstance(model, str) or not model.strip():
            raise CapabilityConfigError("candidate model ids must be nonempty strings")
        if not isinstance(capability, str) or not capability.strip():
            raise CapabilityConfigError(
                f"capability description for {model!r} must be a nonempty string")
        text = capability.strip()
        if len(text) > MAX_CAPABILITY_CHARS:
            raise CapabilityConfigError(
                f"capability description for {model!r} exceeds "
                f"{MAX_CAPABILITY_CHARS} chars")
        normalised[model] = text

    decision = raw.get("decision") or {}
    if not isinstance(decision, dict):
        raise CapabilityConfigError("decision must be an object")
    instructions = decision.get("instructions", DEFAULT_INSTRUCTIONS)
    if not isinstance(instructions, str) or not instructions.strip():
        raise CapabilityConfigError("decision.instructions must be a nonempty string")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise CapabilityConfigError(
            f"decision.instructions exceeds {MAX_INSTRUCTIONS_CHARS} chars")
    fallback = decision.get("fallback_model")
    if fallback is not None and (not isinstance(fallback, str) or not fallback.strip()):
        raise CapabilityConfigError(
            "decision.fallback_model must be a nonempty string or null")

    version = raw.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise CapabilityConfigError("version must be an integer")

    return {
        "version": version,
        "decision": {
            "instructions": instructions.strip(),
            "min_prob": _unit(decision.get("min_prob"), "decision.min_prob",
                              DEFAULT_MIN_PROB),
            "min_margin": _unit(decision.get("min_margin"),
                                "decision.min_margin", DEFAULT_MIN_MARGIN),
            "fallback_model": None if fallback is None else fallback.strip(),
        },
        "candidates": normalised,
    }


def load_capability_config(path: str | Path) -> dict:
    """Load + validate a capability-routing config file."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapabilityConfigError(
            f"cannot read capability config {source}: {exc}") from exc
    cfg = validate_capability_config(raw)
    cfg["source"] = str(source)
    return cfg


def fingerprint(cfg: dict) -> str:
    """Stable content hash of the decision question + capability descriptions.

    Goes into the request ledger next to the existing pipeline/questions
    revisions so a decision can be replayed against the exact descriptions
    that produced it.
    """
    payload = {
        "instructions": cfg["decision"]["instructions"],
        "candidates": cfg["candidates"],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_choice_record(state: str, cfg: dict) -> dict:
    """Compile one request into a single JEV ``choice`` record.

    Rendering matches ``router.signals.jev.build_signal_records`` (and
    Open-Jev): one prompt per candidate, ``<model>: <capability description>``
    as the option label, candidate names in ``answer_keys``.
    """
    candidates = cfg["candidates"]
    return {
        "state": state,
        "kind": "choice",
        "question": cfg["decision"]["instructions"],
        "options": [f"{model}: {capability}"
                    for model, capability in candidates.items()],
        "answer_keys": list(candidates),
    }


def rank(probabilities: dict) -> list[dict]:
    """``{model: p}`` -> ranked list (descending probability)."""
    rows = [{"model": model, "p": float(p)} for model, p in probabilities.items()]
    rows.sort(key=lambda row: row["p"], reverse=True)
    return rows


def decide(cfg: dict, probabilities: dict | None, *,
           error: Exception | None = None, input_window: dict | None = None) -> dict:
    """Turn candidate probabilities into a routing decision.

    Never raises for a failed decision: when ``error`` is given (or the
    probability map is unusable) the configured ``fallback_model`` is returned
    with ``reason="score_failed"`` — this is the fail-open contract the article
    calls "publishing no rank": the request still gets a model.
    """
    decision_cfg = cfg["decision"]
    fallback = decision_cfg.get("fallback_model")
    base = {
        "input": input_window,
        "fingerprint": fingerprint(cfg),
        "min_prob": decision_cfg["min_prob"],
        "min_margin": decision_cfg["min_margin"],
    }

    if error is not None or not probabilities:
        return {**base, "model": fallback, "fallback_used": True,
                "reason": "score_failed",
                "error": None if error is None else f"{type(error).__name__}: {error}",
                "ranking": [], "probabilities": {}, "p_top": None, "margin": None}

    ranking = rank(probabilities)
    if not ranking:
        return {**base, "model": fallback, "fallback_used": True,
                "reason": "score_failed", "error": "empty probabilities",
                "ranking": [], "probabilities": {}, "p_top": None, "margin": None}
    top = ranking[0]
    runner_up = ranking[1] if len(ranking) > 1 else {"model": None, "p": 0.0}
    margin = top["p"] - runner_up["p"]
    base.update({"ranking": ranking, "probabilities": dict(probabilities),
                 "p_top": top["p"], "margin": margin,
                 "runner_up": runner_up["model"]})

    if fallback is None:
        return {**base, "model": top["model"], "fallback_used": False,
                "reason": "decision_model"}
    if top["p"] < decision_cfg["min_prob"]:
        return {**base, "model": fallback, "fallback_used": True,
                "reason": "low_confidence", "top_model": top["model"]}
    if margin < decision_cfg["min_margin"]:
        return {**base, "model": fallback, "fallback_used": True,
                "reason": "ambiguous", "top_model": top["model"]}
    return {**base, "model": top["model"], "fallback_used": False,
            "reason": "decision_model"}


def score_with_tagger(tagger, cfg: dict, state: str) -> dict:
    """Score ``state`` with a JevTagger and return a fail-open decision.

    ``tagger`` is any object exposing
    ``score_choice(state, instructions, candidates) -> {"probabilities", "input"}``.
    Exceptions are caught on purpose: a decision-model failure must degrade to
    the fallback model instead of failing the user's request.
    """
    try:
        scored = tagger.score_choice(
            state, cfg["decision"]["instructions"], cfg["candidates"])
    except Exception as exc:                      # noqa: BLE001 - fail-open
        return decide(cfg, None, error=exc)
    probabilities = scored.get("probabilities") if isinstance(scored, dict) else None
    window = scored.get("input") if isinstance(scored, dict) else None
    return decide(cfg, probabilities, input_window=window)
