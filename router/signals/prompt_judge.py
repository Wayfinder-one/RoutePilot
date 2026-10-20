"""PromptJudgeTagger: one LLM call judges all model signals from a prompt.

Feasibility alternative to :class:`router.signals.checkpoint.CheckpointTagger`.
Instead of eight trained heads (a single forward pass), this tagger sends the
user request plus a **configurable prompt** — one question per signal — to an
OpenAI-compatible chat endpoint, and parses one JSON answer:

    {"signals": {"domain": {"value": "...", "confidence": 0.9}, ..., 
                 "pii": {"values": [...], "conf_by_value": {...}}}}

Rule signals are still computed locally (``signals/rules.py``), exactly as in
the checkpoint tagger. Session signals (``similarity``/``reask``) need an
embedding, which this mode does not produce, so they are reported as "no
history" (0 / "no", confidence None) — a documented limitation of the mode.

The prompt config is JSON (``configs/signal_prompts.json`` by default, or
``ROUTER_SIGNAL_PROMPTS``). Allowed value keys come from the signal registry
(the single source of truth), so the config only carries the question text.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from router.signals.registry import SIGNALS
from router.signals.pii_rules import merge_rule_pii
from router.signals.rules import rule_signals
from router.signals.tagger import TagRequest, TagResult

logger = logging.getLogger(__name__)

#: Model signals judged by the prompt (the two session/embedding signals are
#: not judged: they need the previous-turn embedding).
JUDGED_SIGNALS: tuple[str, ...] = tuple(
    key for key, spec in SIGNALS.items()
    if spec.kind == "model" and key not in ("similarity", "reask"))

#: Shipped default prompt config (edit the JSON, not this module).
DEFAULT_PROMPTS_PATH = (Path(__file__).resolve().parent.parent.parent
                        / "configs" / "signal_prompts.json")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class PromptJudgeError(Exception):
    """The judge call or its JSON answer could not be used."""


# --------------------------------------------------------------------------- #
# Prompt config (the JSON that the console will expose as editable)
# --------------------------------------------------------------------------- #
def load_prompt_config(path: str | Path | None = None) -> dict:
    """Load + validate the signal prompt config (see module docstring)."""
    p = Path(path) if path else DEFAULT_PROMPTS_PATH
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromptJudgeError(f"cannot read prompt config {p}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise PromptJudgeError(f"prompt config {p} must be a JSON object")
    if not isinstance(cfg.get("instructions"), str) or not cfg["instructions"].strip():
        raise PromptJudgeError("prompt config needs a non-empty 'instructions'")
    signals = cfg.get("signals")
    if not isinstance(signals, dict):
        raise PromptJudgeError("prompt config needs a 'signals' object")
    missing = [k for k in JUDGED_SIGNALS if not isinstance(
        (signals.get(k) or {}).get("prompt"), str) or not signals[k]["prompt"].strip()]
    if missing:
        raise PromptJudgeError(f"prompt config lacks a prompt for: {missing}")
    unknown = sorted(set(signals) - set(JUDGED_SIGNALS))
    if unknown:
        raise PromptJudgeError(f"prompt config has unknown signals: {unknown}")
    cfg.setdefault("judge", {})
    return cfg


def _allowed_values(signal: str) -> list[tuple[str, str]]:
    return [(v[0], v[1]) for v in SIGNALS[signal].values]


def _schema_sample() -> dict:
    out = {}
    for key in JUDGED_SIGNALS:
        spec = SIGNALS[key]
        if spec.multi:
            out[key] = {"values": [], "conf_by_value": {}}
        elif key == "user_feedback":
            out[key] = {"value": None, "confidence": 0.0}
        else:
            out[key] = {"value": "<allowed>", "confidence": 0.8}
    return {"signals": out}


def build_system_prompt(cfg: dict) -> str:
    """Render the one system prompt: instructions + schema + questions + examples."""
    lines = [cfg["instructions"].strip(), "",
             "输出结构（只输出这一个 JSON 对象）：",
             json.dumps(_schema_sample(), ensure_ascii=False), "",
             "信号定义与允许值："]
    for key in JUDGED_SIGNALS:
        spec = SIGNALS[key]
        values = "、".join(f"{v}({zh})" for v, zh in _allowed_values(key))
        prompt = cfg["signals"][key]["prompt"].strip()
        if spec.multi:
            lines.append(f"- {key}（{spec.zh}，可多值）：{prompt} 允许值：{values}。")
        elif key == "user_feedback":
            lines.append(f"- {key}（{spec.zh}）：{prompt} 允许值：{values}；"
                         "看不出反馈意图时 value=null。")
        else:
            lines.append(f"- {key}（{spec.zh}，单选）：{prompt} 允许值：{values}。")
    examples = cfg.get("examples") or []
    if examples:
        lines.append("")
        lines.append("示例（仅示意判断口径与格式；confidence 必须自己判断，不要照抄）：")
        for ex in examples:
            lines.append(f"请求：{ex.get('text', '')}")
            lines.append("输出：" + json.dumps(ex.get("output"), ensure_ascii=False))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #
def _clamp_conf(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromptJudgeError(f"confidence must be a number, got {value!r}")
    return max(0.0, min(1.0, float(value)))


def _strip_wrapping(text: str) -> str:
    """Remove <think> blocks / code fences and return the first JSON object."""
    text = _THINK_RE.sub("", text or "")
    text = _FENCE_RE.sub("", text).strip()
    start = text.find("{")
    if start < 0:
        raise PromptJudgeError(f"judge answer has no JSON object: {text[:160]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise PromptJudgeError(f"judge answer is not valid JSON: {exc}; "
                               f"text={text[start:start + 160]!r}") from exc
    return obj


def _coerce_signal(key: str, entry) -> dict:
    if not isinstance(entry, dict):
        raise PromptJudgeError(f"{key}: entry must be an object, got {entry!r}")
    allowed = {v for v, _ in _allowed_values(key)}
    if SIGNALS[key].multi:
        values = entry.get("values") or []
        if not isinstance(values, list):
            raise PromptJudgeError(f"{key}.values must be a list, got {values!r}")
        bad = [v for v in values if v not in allowed]
        if bad:
            raise PromptJudgeError(f"{key}: unknown values {bad}")
        confs = entry.get("conf_by_value") or {}
        if not isinstance(confs, dict):
            raise PromptJudgeError(f"{key}.conf_by_value must be an object")
        conf_by_value = {v: _clamp_conf(confs.get(v, entry.get("confidence", 0.0)))
                         for v in values}
        return {"value": values,
                "confidence": max(conf_by_value.values()) if conf_by_value else None,
                "conf_by_value": conf_by_value, "is_rule": False}

    value = entry.get("value")
    if value is None:
        if key != "user_feedback":
            raise PromptJudgeError(f"{key}: value must not be null")
        return {"value": None, "confidence": None, "is_rule": False}
    if value not in allowed:
        raise PromptJudgeError(f"{key}: unknown value {value!r} (allowed {sorted(allowed)})")
    return {"value": value, "confidence": _clamp_conf(entry.get("confidence")),
            "is_rule": False}


def parse_output(text: str) -> dict[str, dict]:
    """Judge answer text -> canonical signal pack (model signals only)."""
    obj = _strip_wrapping(text)
    signals = obj.get("signals") if isinstance(obj, dict) else None
    if not isinstance(signals, dict):
        raise PromptJudgeError("judge answer lacks a 'signals' object")
    missing = [k for k in JUDGED_SIGNALS if k not in signals]
    if missing:
        raise PromptJudgeError(f"judge answer misses signals: {missing}")
    return {k: _coerce_signal(k, signals[k]) for k in JUDGED_SIGNALS}


# --------------------------------------------------------------------------- #
# Tagger
# --------------------------------------------------------------------------- #
class PromptJudgeTagger:
    """Tagger protocol implementation backed by one chat-completion call."""

    name = "prompt"

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 prompts_path: str | None = None, kw_groups: dict | None = None,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 120.0, config: dict | None = None):
        self.config = config or load_prompt_config(prompts_path)
        self.system_prompt = build_system_prompt(self.config)
        self.model = model
        self.kw_groups = kw_groups or {}
        self.time_periods = {}               # replaced wholesale on rule update
        judge = self.config.get("judge") or {}
        self.temperature = float(judge.get("temperature", 0))
        self.max_tokens = int(judge.get("max_tokens", 1200))
        self.response_format = judge.get("response_format", "json_object")
        self.enable_thinking = judge.get("enable_thinking")   # None = leave to model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers,
            timeout=timeout, transport=transport)
        #: counters for diagnostics / the feasibility report
        self.stats = {"calls": 0, "errors": 0, "retries": 0,
                      "latency_ms": [], "prompt_tokens": 0, "completion_tokens": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chat(self, text: str) -> str:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
        }
        if self.response_format:
            body["response_format"] = {"type": self.response_format}
        if self.enable_thinking is not None:
            # Qwen3-style chat templates: without this the model spends the
            # whole budget on <think> and the guided-JSON answer comes back empty.
            body["chat_template_kwargs"] = {"enable_thinking": bool(self.enable_thinking)}
        self.stats["calls"] += 1
        started = time.monotonic()
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.stats["errors"] += 1
            raise PromptJudgeError(f"judge call failed: {exc!r}") from exc
        finally:
            self.stats["latency_ms"].append((time.monotonic() - started) * 1000)
        usage = data.get("usage") or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            self.stats["errors"] += 1
            raise PromptJudgeError(f"unexpected judge response: {data!r}") from exc

    async def tag(self, req: TagRequest) -> TagResult:
        """One judge call for all model signals + local rule signals."""
        judged: dict | None = None
        last_error: Exception | None = None
        for attempt in range(2):                    # one repair retry
            try:
                judged = parse_output(await self._chat(req.text))
                break
            except PromptJudgeError as exc:
                last_error = exc
                if attempt == 0:
                    self.stats["retries"] += 1
                    continue
                self.stats["errors"] += 1
        if judged is None:
            raise PromptJudgeError(f"judge output unusable after retry: {last_error}")
        # user_feedback is only meaningful with a previous turn (like checkpoint)
        if req.n_messages <= 1:
            judged["user_feedback"] = {"value": None, "confidence": None,
                                       "is_rule": False}
        judged.update(rule_signals(req.text, req.headers, req.n_messages,
                                   self.kw_groups, time_periods=self.time_periods))
        merge_rule_pii(judged, req.text)
        # this mode has no embedding => report "no session history"
        judged["similarity"] = {"value": 0, "confidence": None, "is_rule": False}
        judged["reask"] = {"value": "no", "confidence": None, "is_rule": False}
        return TagResult(signals=judged, vec=None)
