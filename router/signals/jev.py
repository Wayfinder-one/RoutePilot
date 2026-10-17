"""JevTagger: the Open-Jev main model computes every model signal in-process.

JEV standard: a shared context (the user request) plus **typed questions**
(``noul`` yes/no with explicit criteria, ``choice`` with candidate set) are
scored by one model that returns a probability per candidate — no text
generation and no JSON parsing. The model is the merged single-model export
produced by the training repo (``scripts/14_export_jev_merged.py``): the
Open-Jev-2B backbone with its LoRA merged in plus the scalar decision head.

This module keeps the same contracts as :class:`CheckpointTagger`:

* signal values are product-canonical (``computer_science``, ``needs_check``,
  ``text_image``, ``satisfied`` ...);
* confidence is ``1 - H(p)/log K`` for choices and binary entropy for the
  per-type PII probabilities, so downstream score/gate semantics are unchanged;
* rule signals are computed locally, ``similarity``/``reask`` are "no history"
  (this mode has no embedding head — a documented limitation);
* ``user_feedback`` is null for single-turn requests.

The question wording is a JSON config (``configs/signal_questions.json`` or
``ROUTER_SIGNAL_QUESTIONS``) that can be edited without code changes — the
console "signal config" page edits this file. The model tolerates reworded
questions to a small degree (measured <=1pt on the probe variants); a fully
new template should be validated offline first.

torch / transformers are imported lazily so config validation and the pure
helpers stay testable without a GPU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path

from router.signals.registry import SIGNALS
from router.signals.rules import rule_signals
from router.signals.tagger import TagRequest, TagResult

logger = logging.getLogger(__name__)

#: Shipped default question config (edit the JSON, not this module).
DEFAULT_QUESTIONS_PATH = (Path(__file__).resolve().parent.parent.parent
                          / "configs" / "signal_questions.json")

#: choice-signal -> candidate order is the config's order and must be a subset
#: of the registry values; these are the signals the JEV model answers.
JUDGED_SIGNALS: tuple[str, ...] = (
    "domain", "jailbreak", "fact_check", "modality", "complexity",
    "user_feedback", "pii")
CHOICE_SIGNALS: tuple[str, ...] = ("domain", "modality", "user_feedback")
NOUL_SIGNALS: tuple[str, ...] = ("jailbreak", "fact_check", "complexity")
#: noul -> (positive value, negative value) in product-canonical space
NOUL_VALUES: dict[str, tuple[str, str]] = {
    "jailbreak": ("jailbreak", "benign"),
    "fact_check": ("needs_check", "no_check"),
    "complexity": ("hard", "easy"),
}
#: PII: hits are written as a sorted list of canonical types.
PII_HIT_THRESHOLD = 0.5


class JevTaggerError(Exception):
    """Config/model/forward failure — startup refuses or the request fails."""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without torch)
# --------------------------------------------------------------------------- #
def softmax(values) -> list[float]:
    m = max(values)
    exps = [math.exp(v - m) for v in values]
    total = sum(exps)
    return [e / total for e in exps]


def entropy_confidence(probs: list[float]) -> float:
    """1 - H(p)/log K, clamped to [0,1] (same as CheckpointTagger)."""
    k = len(probs)
    if k <= 1:
        return 1.0
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def binary_entropy_confidence(p: float) -> float:
    p = max(0.0, min(1.0, p))
    if p <= 0.0 or p >= 1.0:
        return 1.0
    h2 = -(p * math.log(p) + (1 - p) * math.log(1 - p)) / math.log(2)
    return max(0.0, min(1.0, 1.0 - h2))


def validate_question_config(cfg) -> dict:
    """Validate an in-memory question config (used by load and the admin PUT)."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("signals"), dict):
        raise JevTaggerError(f"question config {p} must have a 'signals' object")
    signals = cfg["signals"]
    missing = [k for k in JUDGED_SIGNALS if k not in signals]
    if missing:
        raise JevTaggerError(f"question config lacks signals: {missing}")
    unknown = sorted(set(signals) - set(JUDGED_SIGNALS))
    if unknown:
        raise JevTaggerError(f"question config has unknown signals: {unknown}")

    for key in CHOICE_SIGNALS:
        entry = signals[key]
        if entry.get("kind") != "choice":
            raise JevTaggerError(f"{key}: kind must be 'choice'")
        candidates = entry.get("candidates")
        if not isinstance(candidates, dict) or not candidates:
            raise JevTaggerError(f"{key}: candidates must be a non-empty object")
        allowed = {v[0] for v in SIGNALS[key].values}
        bad = sorted(set(candidates) - allowed)
        if bad:
            raise JevTaggerError(f"{key}: unknown candidate keys {bad}")
        if not isinstance(entry.get("instructions"), list) or not entry["instructions"]:
            raise JevTaggerError(f"{key}: instructions must be a non-empty list")
    for key in NOUL_SIGNALS:
        entry = signals[key]
        if entry.get("kind") != "noul":
            raise JevTaggerError(f"{key}: kind must be 'noul'")
        for field in ("instructions", "true", "false"):
            value = entry.get(field)
            if field == "instructions":
                if not isinstance(value, list) or not value:
                    raise JevTaggerError(f"{key}: instructions must be a non-empty list")
            elif not isinstance(value, str) or not value.strip():
                raise JevTaggerError(f"{key}: {field} must be a non-empty string")
    pii = signals["pii"]
    if not isinstance(pii.get("instructions"), str) or "{zh}" not in pii["instructions"]:
        raise JevTaggerError("pii: instructions must be a string containing '{zh}'")
    types = pii.get("types")
    if not isinstance(types, dict) or not types:
        raise JevTaggerError("pii: types must be a non-empty object")
    allowed_pii = {v[0] for v in SIGNALS["pii"].values}
    bad = sorted(set(types) - allowed_pii)
    if bad:
        raise JevTaggerError(f"pii: unknown types {bad}")
    return cfg


def load_question_config(path: str | Path | None = None) -> dict:
    """Load + validate the editable JEV question config file."""
    p = Path(path) if path else DEFAULT_QUESTIONS_PATH
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JevTaggerError(f"cannot read question config {p}: {exc}") from exc
    return validate_question_config(cfg)


def candidate_prompts(record: dict) -> list[str]:
    """Render the independent candidate prompts (same template as Open-Jev)."""
    prefix = f"Context:\n{record['state']}\n\nQuestion: {record['question']}\n"
    if record["kind"] == "noul":
        return [prefix + "Is the answer to this question yes? Answer Yes or No."]
    return [prefix + f"Proposed answer: {option}\nIs this proposed answer correct? "
                     "Answer Yes or No."
            for option in record["options"]]


def build_signal_records(cfg: dict, text: str) -> list[dict]:
    """Compile one request into JEV records (one per signal / PII type)."""
    signals = cfg["signals"]
    out: list[dict] = []
    for key in CHOICE_SIGNALS + NOUL_SIGNALS:
        entry = signals[key]
        record = {"state": text, "kind": entry["kind"]}
        if entry["kind"] == "noul":
            question = entry["instructions"][0]
            record["question"] = (question
                                  + f"\nYes means: {entry['true']}"
                                  + f"\nNo means: {entry['false']}")
            record["options"] = ["no", "yes"]
            record["answer_keys"] = ["false", "true"]
        else:
            candidates = entry["candidates"]
            keys = list(candidates)
            record["question"] = entry["instructions"][0]
            record["options"] = [
                f"{key}: {value.get('zh') if isinstance(value, dict) else value}"
                for key, value in candidates.items()]
            record["answer_keys"] = keys
        out.append({"signal": key, "sub": None, "record": record})
    pii = signals["pii"]
    for typ, meta in pii["types"].items():
        record = {"state": text, "kind": "noul",
                  "question": (pii["instructions"].format(zh=meta["zh"])
                               + f"\nYes means: {pii['true'].format(zh=meta['zh'])}"
                               + f"\nNo means: {pii['false'].format(zh=meta['zh'])}"),
                  "options": ["no", "yes"], "answer_keys": ["false", "true"]}
        out.append({"signal": "pii", "sub": typ, "record": record})
    return out


def decode_choice(record: dict, probs: list[float]) -> dict:
    keys = record["answer_keys"]
    idx = max(range(len(probs)), key=lambda i: probs[i])
    return {"value": keys[idx], "confidence": entropy_confidence(probs),
            "is_rule": False}


def decode_noul(record: dict, probs: list[float], *, positive: str,
                negative: str) -> dict:
    p_yes = probs[1]
    return {"value": positive if p_yes >= 0.5 else negative,
            "confidence": entropy_confidence(probs), "is_rule": False}


# --------------------------------------------------------------------------- #
# Tagger
# --------------------------------------------------------------------------- #
class JevTagger:
    """Open-Jev main model as a Tagger (name="jev")."""

    name = "jev"

    def __init__(self, model_dir: str | Path, *, questions_path: str | Path | None = None,
                 device: str | None = None, max_length: int | None = None,
                 kw_groups: dict | None = None, model_factory=None,
                 warmup: bool = True):
        self.question_path = str(questions_path or DEFAULT_QUESTIONS_PATH)
        self.questions = load_question_config(questions_path)
        self.kw_groups = kw_groups or {}
        self.model_dir = Path(model_dir)
        meta_path = self.model_dir / "model.json"
        if not meta_path.is_file():
            raise JevTaggerError(f"{self.model_dir} has no model.json")
        self.meta = json.loads(meta_path.read_text())
        if not (self.model_dir / "head.pt").is_file():
            raise JevTaggerError(f"{self.model_dir} has no head.pt")
        temp_path = self.model_dir / "temperature.json"
        self.temperature = (json.loads(temp_path.read_text())["temperature"]
                            if temp_path.is_file() else 1.0)
        self.max_length = int(max_length or self.meta.get("max_length") or 1024)

        import torch
        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if model_factory is not None:
            self.model, self.tokenizer, self.head = model_factory(
                self.model_dir, self.device)
            self.model_binding = "injected"
        else:
            from transformers import AutoModelForImageTextToText, AutoTokenizer
            logger.info("loading JEV model %s on %s", self.model_dir, self.device)
            self.model = AutoModelForImageTextToText.from_pretrained(
                self.model_dir, torch_dtype=torch.bfloat16,
                attn_implementation="sdpa", device_map={"": self.device})
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
            self.model_binding = f"merged:{self.model_dir}"
        self.tokenizer.padding_side = "right"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.backbone = getattr(self.model, "model", self.model).language_model
        self.backbone.config.use_cache = False
        hidden = self.backbone.config.hidden_size
        if model_factory is None:
            self.head = torch.nn.Linear(hidden, 1, bias=True,
                                        device=self.device, dtype=torch.float32)
            self.head.load_state_dict(torch.load(self.model_dir / "head.pt",
                                                 map_location=self.device,
                                                 weights_only=True))
        self.backbone.eval()
        self.head.eval()
        if warmup:
            self._warmup()

    def set_questions(self, cfg: dict) -> dict:
        """Hot-apply an edited question config (validated before replacing)."""
        self.questions = validate_question_config(cfg)
        return self.questions

    # ── forward ────────────────────────────────────────────────────────────
    def _warmup(self) -> None:
        self._tag_row(TagRequest(text="你好，帮我写一段 Python 快排。", n_messages=2))

    def _render(self, records: list[dict]) -> tuple[list[str], list[int]]:
        prompts, counts = [], []
        for record in records:
            entries = candidate_prompts(record)
            counts.append(len(entries))
            for prompt in entries:
                prompts.append(self.tokenizer.apply_chat_template(
                    [{"role": "user", "content": prompt}], tokenize=False,
                    add_generation_prompt=True, enable_thinking=False))
        return prompts, counts

    def _shrink_to_fit(self, records: list[dict]) -> tuple[dict, list[int]]:
        """Shorten the shared context until the rendered prompts fit."""
        for _ in range(8):
            prompts, counts = self._render(records)
            enc = self.tokenizer(prompts, padding=True, truncation=False,
                                 return_tensors="pt")
            lengths = enc["attention_mask"].sum(-1)
            if int(lengths.max().item()) <= self.max_length:
                return enc, counts
            for record in records:
                state = str(record["state"])
                record["state"] = state[:max(200, int(len(state) * 0.7))]
        prompts, counts = self._render(records)
        enc = self.tokenizer(prompts, padding=True, truncation=True,
                             max_length=self.max_length, return_tensors="pt")
        logger.warning("JEV input still too long after shrinking; token-truncated")
        return enc, counts

    #: cap on candidate prompts per forward pass (keeps peak KV memory bounded
    #: when a request is long: at most ``max_prompts_per_forward * max_length``
    #: tokens are attended at once).
    max_prompts_per_forward = 16

    def _score(self, records: list[dict]) -> list[list[float]]:
        """Independent candidate probabilities per record (temperature applied)."""
        groups: list[list[dict]] = []
        current: list[dict] = []
        count = 0
        for record in records:
            n = 1 if record["kind"] == "noul" else len(record["options"])
            if current and count + n > self.max_prompts_per_forward:
                groups.append(current)
                current, count = [], 0
            current.append(record)
            count += n
        if current:
            groups.append(current)
        out: list[list[float]] = []
        for group in groups:
            out.extend(self._score_group(group))
        return out

    def _score_group(self, records: list[dict]) -> list[list[float]]:
        torch = self._torch
        enc, counts = self._shrink_to_fit(records)
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with torch.inference_mode():
            outputs = self.backbone(**enc, use_cache=False, return_dict=True)
            lengths = enc["attention_mask"].sum(-1)
            idx = torch.arange(len(lengths), device=self.device)
            hidden = outputs.last_hidden_state[idx, lengths - 1]
            scores = self.head(hidden.float()).squeeze(-1)
        out: list[list[float]] = []
        offset = 0
        for count in counts:
            values = scores[offset:offset + count]
            if count == 1:   # noul: fixed no-logit 0 plus the yes logit
                values = torch.stack([torch.zeros_like(values[0]), values[0]])
            probs = torch.softmax(values / max(self.temperature, 1e-6), dim=-1)
            out.append([float(x) for x in probs.cpu().tolist()])
            offset += count
        return out

    def _model_signals(self, req: TagRequest) -> dict:
        records = build_signal_records(self.questions, req.text)
        probs = self._score([item["record"] for item in records])
        by_key: dict[str, list[tuple[dict, list[float]]]] = {}
        for item, prob in zip(records, probs):
            by_key.setdefault(item["signal"], []).append((item, prob))

        signals: dict = {}
        for key in CHOICE_SIGNALS + NOUL_SIGNALS:
            item, prob = by_key[key][0]
            record = item["record"]
            if key in NOUL_VALUES:
                positive, negative = NOUL_VALUES[key]
                signals[key] = decode_noul(record, prob, positive=positive,
                                           negative=negative)
            else:
                signals[key] = decode_choice(record, prob)
        if req.n_messages <= 1:
            signals["user_feedback"] = {"value": None, "confidence": None,
                                        "is_rule": False}
        # PII: one noul question per type; hits are canonical type keys.
        hits: list[str] = []
        conf_by_value: dict[str, float] = {}
        for item, prob in by_key["pii"]:
            p_yes = prob[1]
            if p_yes >= PII_HIT_THRESHOLD:
                hits.append(item["sub"])
                conf_by_value[item["sub"]] = binary_entropy_confidence(p_yes)
        signals["pii"] = {
            "value": sorted(hits),
            "confidence": max(conf_by_value.values()) if conf_by_value else None,
            "conf_by_value": conf_by_value, "is_rule": False}
        # No embedding head in this mode (documented limitation).
        signals["similarity"] = {"value": 0, "confidence": None, "is_rule": False}
        signals["reask"] = {"value": "no", "confidence": None, "is_rule": False}
        return signals

    def _tag_row(self, req: TagRequest) -> tuple[dict, None]:
        return self._model_signals(req), None

    async def tag(self, req: TagRequest) -> TagResult:
        signals, vec = await asyncio.to_thread(self._tag_row, req)
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups))
        return TagResult(signals=signals, vec=vec)

    async def aclose(self) -> None:
        self.model = None
        self.backbone = None
        self.head = None
        try:
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        except Exception:                         # noqa: BLE001
            pass
