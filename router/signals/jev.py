"""JevTagger: the Open-Jev main model computes every model signal in-process.

JEV standard: a shared context (the user request) plus **typed questions**
(``noul`` yes/no with explicit criteria, ``choice`` with candidate set) are
scored by one model that returns a probability per candidate — no text
generation and no JSON parsing. The model is the merged single-model export
produced by the training repo (``scripts/14_export_jev_merged.py``): the
Qwen3.5-0.8B backbone with its LoRA merged in plus the scalar decision head
(deployed: r4b3, ``temperature.json`` = 3.45, ``model.json`` max_length 1024).

This module keeps the same contracts as :class:`CheckpointTagger`:

* signal values are product-canonical (``computer_science``, ``needs_check``,
  ``text_image``, ``satisfied`` ...);
* confidence is ``1 - H(p)/log K`` for choices and binary entropy for the
  per-type PII probabilities, so downstream score/gate semantics are unchanged;
* rule signals are computed locally; ``similarity``/``reask`` signals no longer
  exist (session feature removed 2026-10-21);
* ``user_feedback`` is null for single-turn requests.

The question wording is a JSON config (``configs/signal_questions.json`` or
``ROUTER_SIGNAL_QUESTIONS``) that can be edited without code changes — the
console "signal config" page edits this file. Only the **first** wording of a
signal (``instructions[0]``) is rendered at runtime; the other list entries are
kept as documentation of the training paraphrases and are not scored.

**Use the canonical wording.** r4b3 was trained on three wording variants per
question, but only the canonical one (training ``r4_questions.json`` variant
``0``, byte-identical to this config's first entry) is the calibrated hot path:
unseen rewordings drift by up to ~5pt macro and ``user_feedback`` is the most
wording-sensitive signal (canonical .802 vs .945 reworded). Do not paraphrase
``instructions[0]`` without re-running the eval (``results/jev_r4/README.md`` in
the training repo). Candidate order is free.

**Input window (2026-10-21).** The model signals read only the **first
``max_length`` tokens of the request text** (deployed: 1024), tokenizer-exact
head truncation. There is no 8-round context shrink any more: long requests are
cropped once instead of paying tens of seconds of re-tokenization (200K input
measured 46-48 s, ``docs/jev-pii-200k/REPORT.md`` §4.6), and the window is
explicit instead of "the head that happens to survive". The rendered question
template adds 47-120 tokens on top, so the prompt cap is ``max_length +
PROMPT_OVERHEAD_SLACK``; the canonical config therefore never loses its
question. Only a question config edited far beyond that slack falls back to
tokenizer left-truncation (question/options tail kept). The deterministic rule
signals (``rule_signals`` / ``merge_rule_pii``) still scan the **full** text —
they are the only layer that still sees long requests. The truncation facts are
reported through ``TagResult.input`` and land in the console calculation log.

**Pipeline-aware trimming** (``TagRequest.scope``): the routing tree only reads
the signals that appear as rows, so the hot path passes a
:class:`router.pipeline.scope.SignalScope` and this tagger renders/scores only
the referenced questions and PII subtypes (``docs/pipeline-aware-trimming.md``).
``scope=None`` keeps the old full-pack behavior (``/admin/try``, eval endpoints).
Choice signals always keep all configured candidates: trimming candidate
options would change the argmax/entropy of the signal. The scope only skips
questions the tree cannot read, so every value the pipeline can observe is
computed exactly as before.

torch / transformers are imported lazily so config validation and the pure
helpers stay testable without a GPU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from router.signals._math import (PII_HIT_THRESHOLD, PII_MIN_CONFIDENCE,
                                 binary_entropy_confidence, entropy_confidence,
                                 pii_hits)
from router.signals.registry import SIGNALS
from router.signals.pii_rules import merge_rule_pii
from router.signals.rules import rule_signals
from router.signals.tagger import TagRequest, TagResult

if TYPE_CHECKING:                            # pure-helper module: no runtime import
    from router.pipeline.scope import SignalScope

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
#: PII: hits are written as a sorted list of canonical types. The hit rule
#: (probability strictly above the threshold + a minimum binary-entropy
#: confidence) is shared with the other taggers — see
#: ``router.signals._math.pii_hits``. Regex hits from ``merge_rule_pii``
#: enter at confidence 1.0.

#: Extra tokens the rendered question template may add on top of the context
#: window. The deployed pack (54 candidate prompts, canonical wording) measures
#: 47-120 tokens of template overhead with the Qwen3.5 tokenizer (2026-10-21),
#: so this margin keeps every canonical prompt whole. Only an admin-edited
#: question config much longer than this can reach the prompt cap; that
#: fallback left-truncates so the question/options tail survives (audit P1 3.1).
PROMPT_OVERHEAD_SLACK = 160


class JevTaggerError(Exception):
    """Config/model/forward failure — startup refuses or the request fails."""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without torch)
# --------------------------------------------------------------------------- #
def head_window(text: str, tokenizer, cap: int) -> tuple[str, dict]:
    """Keep the first ``cap`` tokenizer tokens of ``text`` (head truncation).

    The model-input policy is "the first ``max_length`` tokens of the request
    text", so this keeps the beginning and drops the tail. ``decode`` of a
    truncated id list can end on a partial byte (U+FFFD) or re-encode one token
    longer; both cases are trimmed until the retained text re-encodes to at most
    ``cap`` tokens. The text is returned unchanged when it already fits.

    Returns ``(text, stats)`` with ``stats`` carrying the keys written to the
    request log: ``input_tokens`` (original), ``input_visible_tokens`` (scored)
    and ``input_truncated``.
    """
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    total = len(ids)
    if total <= cap:
        return text, {"input_tokens": total, "input_visible_tokens": total,
                      "input_truncated": False}
    cut = cap
    while cut > 0:
        head = tokenizer.decode(ids[:cut], skip_special_tokens=False)
        if head.endswith("\ufffd"):       # cut landed inside a multi-byte char
            cut -= 1
            continue
        used = len(tokenizer(head, add_special_tokens=False)["input_ids"])
        if used <= cap:
            return head, {"input_tokens": total,
                          "input_visible_tokens": used,
                          "input_truncated": True}
        cut -= max(1, used - cap)
    return "", {"input_tokens": total, "input_visible_tokens": 0,
                "input_truncated": True}


def validate_question_config(cfg) -> dict:
    """Validate an in-memory question config (used by load and the admin PUT)."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("signals"), dict):
        raise JevTaggerError("question config must have a 'signals' object")
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
    # The pii branch validates like the noul branch: a missing/blank true or
    # false used to blow up at request time (KeyError in build_signal_records)
    # instead of at save time.
    for field in ("true", "false"):
        value = pii.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JevTaggerError(f"pii: {field} must be a non-empty string")
    types = pii.get("types")
    if not isinstance(types, dict) or not types:
        raise JevTaggerError("pii: types must be a non-empty object")
    allowed_pii = {v[0] for v in SIGNALS["pii"].values}
    bad = sorted(set(types) - allowed_pii)
    if bad:
        raise JevTaggerError(f"pii: unknown types {bad}")
    # Request time renders every template as `template.format(zh=<type zh>)`
    # (build_signal_records): a type entry without a usable 'zh' is a KeyError
    # per request, and a {name} other than {zh} in a template blows up the same
    # way. All three templates exist by this point (checked above).
    for typ, meta in types.items():
        zh = meta.get("zh") if isinstance(meta, dict) else None
        if not isinstance(zh, str) or not zh.strip():
            raise JevTaggerError(
                f"pii types.{typ}: 'zh' must be a non-empty string")
        for name in ("instructions", "true", "false"):
            try:
                pii[name].format(zh=zh)
            except (KeyError, ValueError, IndexError) as exc:
                raise JevTaggerError(
                    f"pii {name}: template is not renderable for "
                    f"types.{typ} (zh={zh!r}): "
                    f"{exc.__class__.__name__}: {exc}") from exc
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


def build_signal_records(cfg: dict, text: str,
                         scope: "SignalScope | None" = None) -> list[dict]:
    """Compile one request into JEV records (one per signal / PII type).

    ``scope=None`` builds the complete pack (the pre-trimming behavior).  A
    pipeline-derived scope drops whole questions the tree cannot read and PII
    subtypes that are not referenced; choice signals always keep every
    configured candidate (the signal value/confidence must match the full pack).
    """
    signals = cfg["signals"]
    full = scope is None or scope.full
    wanted = None if full else scope.signals
    pii_wanted = None if full else scope.pii_types
    out: list[dict] = []
    for key in CHOICE_SIGNALS + NOUL_SIGNALS:
        if wanted is not None and key not in wanted:
            continue
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
            # Option labels intentionally render the zh form regardless of the
            # request language: the deployed model was calibrated on this
            # exact rendering, and rewording drifts accuracy (see the
            # canonical-wording note in the module docstring). The en labels
            # in the config are for the console palette/display only.
            record["options"] = [
                f"{key}: {value.get('zh') if isinstance(value, dict) else value}"
                for key, value in candidates.items()]
            record["answer_keys"] = keys
        out.append({"signal": key, "sub": None, "record": record})
    pii = signals["pii"]
    if wanted is None or "pii" in wanted:
        for typ, meta in pii["types"].items():
            if pii_wanted is not None and typ not in pii_wanted:
                continue
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
    # Strictly greater: at p_yes == 0.5 the model has no evidence either way,
    # and the positive class is the intercepting one for jailbreak (P2 4.2).
    return {"value": positive if p_yes > 0.5 else negative,
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
        self.time_periods = {}               # replaced wholesale on rule update
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
        #: Context window for the model signals: only the first ``max_length``
        #: tokens of the request text are scored. The rendered question template
        #: sits on top of the window, so the prompt cap is relaxed by
        #: PROMPT_OVERHEAD_SLACK instead of shrinking the context to fit (the
        #: old shrink accidentally read only the document head; see the module
        #: docstring).
        self.prompt_max_length = self.max_length + PROMPT_OVERHEAD_SLACK

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
        # candidate_prompts puts the shared context FIRST and the question +
        # Yes/No options LAST, so the final token-truncation fallback must keep
        # the tail: the HF default 'right' side would cut the question off and
        # score a context-only prompt (audit P1 3.1). The fallback is only
        # reachable with an edited question config longer than
        # PROMPT_OVERHEAD_SLACK — the input window itself already fits.
        self.tokenizer.truncation_side = "left"
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

    def _context_window(self, text: str) -> tuple[str, dict]:
        """Head-truncate the request text to the model's context window."""
        window_text, window = head_window(text, self.tokenizer, self.max_length)
        if window["input_truncated"]:
            logger.debug("jev input truncated: tokens=%d visible=%d",
                         window["input_tokens"], window["input_visible_tokens"])
        return window_text, window

    def _encode(self, records: list[dict]) -> tuple[dict, list[int]]:
        """Render + tokenize the candidate prompts in one pass (no shrinking).

        The shared context already fits ``max_length`` tokens, so only a
        question config edited far past PROMPT_OVERHEAD_SLACK can exceed
        ``prompt_max_length``; that fallback left-truncates so the
        question/options tail survives (audit P1 3.1).
        """
        prompts, counts = self._render(records)
        enc = self.tokenizer(prompts, padding=True, truncation=False,
                             return_tensors="pt")
        longest = int(enc["attention_mask"].sum(-1).max().item())
        if longest > self.prompt_max_length:
            logger.warning("JEV prompt exceeds cap %d after input truncation; "
                           "left-truncated (question/options kept)",
                           self.prompt_max_length)
            enc = self.tokenizer(prompts, padding=True, truncation=True,
                                 max_length=self.prompt_max_length,
                                 return_tensors="pt")
        return enc, counts

    #: Token budget per forward pass. A full pack with 1024-token contexts is
    #: ~54 prompts / ~60k tokens, so the single-forward fast path is only taken
    #: for short requests; longer input is sliced into budgeted chunks of the
    #: already-encoded rows so peak attention memory stays bounded. The split is
    #: math-neutral: the softmax is per prompt and padding aligns within a
    #: forward, so chunk rows score exactly as in one big pass.
    max_tokens_per_forward = 12000

    def _score(self, records: list[dict]) -> list[list[float]]:
        """Independent candidate probabilities per record (temperature applied)."""
        if not records:                       # trimmed scope may drop everything
            return []
        enc, counts = self._encode(records)
        lengths = enc["attention_mask"].sum(-1).tolist()
        if sum(lengths) <= self.max_tokens_per_forward:
            return self._score_encoded(enc, counts)
        # Over budget: slice the encoded rows into token-budgeted chunks —
        # re-encoding each chunk would tokenize the long input a second time.
        # Records stay atomic (a choice softmax needs every candidate score),
        # so a single record bigger than the budget scores alone.
        out: list[list[float]] = []
        first_rec = first_row = tokens = row = 0
        for i, n in enumerate(counts):
            record_tokens = sum(lengths[row:row + n])
            if i > first_rec and tokens + record_tokens > self.max_tokens_per_forward:
                out.extend(self._score_encoded(
                    {k: v[first_row:row] for k, v in enc.items()},
                    counts[first_rec:i]))
                first_rec, first_row, tokens = i, row, 0
            tokens += record_tokens
            row += n
        out.extend(self._score_encoded(
            {k: v[first_row:row] for k, v in enc.items()}, counts[first_rec:]))
        return out

    def _score_encoded(self, enc, counts: list[int]) -> list[list[float]]:
        torch = self._torch
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with torch.inference_mode():
            outputs = self.backbone(**enc, use_cache=False, return_dict=True)
            lengths = enc["attention_mask"].sum(-1)
            idx = torch.arange(len(lengths), device=self.device)
            hidden = outputs.last_hidden_state[idx, lengths - 1]
            scores = self.head(hidden.float()).squeeze(-1)
            # One host copy, then decode on the CPU. Per-record GPU work here
            # (slices/syncs for 36 records, 30 of them noul single-logit rows)
            # cost ~100ms of driver sync latency on the 0.8B JEV path.
            scores_cpu = scores.cpu().tolist()
        temperature = max(self.temperature, 1e-6)
        out: list[list[float]] = []
        offset = 0
        for count in counts:
            values = ([0.0, scores_cpu[offset]] if count == 1
                      else scores_cpu[offset:offset + count])
            top = max(values)
            exps = [math.exp((value - top) / temperature) for value in values]
            total = sum(exps)
            out.append([value / total for value in exps])
            offset += count
        return out

    def _model_signals(self, req: TagRequest, *,
                       text: str | None = None) -> dict:
        scope = req.scope
        if scope is not None and scope.trimmed:
            logger.debug("jev trimmed pack: signals=%s pii=%s",
                         sorted(scope.signals), sorted(scope.pii_types))
        # ``text`` is the head-truncated context window from _tag_row; direct
        # callers (unit tests, bench scripts) default to the request text.
        records = build_signal_records(self.questions,
                                       req.text if text is None else text,
                                       scope)
        # user_feedback is unconditionally overridden to null for single-turn
        # requests below (there is no previous turn), so its questions are dead
        # compute. Both the full and the trimmed path skip them => same result.
        if req.n_messages <= 1:
            records = [item for item in records
                       if item["signal"] != "user_feedback"]
        probs = self._score([item["record"] for item in records])
        by_key: dict[str, list[tuple[dict, list[float]]]] = {}
        for item, prob in zip(records, probs):
            by_key.setdefault(item["signal"], []).append((item, prob))

        signals: dict = {}
        for key in CHOICE_SIGNALS + NOUL_SIGNALS:
            items = by_key.get(key)
            if not items:                     # trimmed away by the pipeline scope
                continue
            item, prob = items[0]
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
        # PII: one noul question per type; hits are canonical type keys.  With a
        # trimmed scope only the referenced subtypes were asked; merge_rule_pii
        # (in tag()) still unions the deterministic regex hits of every type.
        pii_items = by_key.get("pii")
        if pii_items is not None:
            labels = [item["sub"] for item, _ in pii_items]
            p_yes_per_type = [prob[1] for _, prob in pii_items]
            hits, conf_by_value = pii_hits(p_yes_per_type, labels)
            signals["pii"] = {
                "value": hits,
                "confidence": max(conf_by_value.values()) if conf_by_value else None,
                "conf_by_value": conf_by_value, "is_rule": False}
        return signals

    def _tag_row(self, req: TagRequest) -> tuple[dict, None, dict]:
        # Model signals read the head-truncated window only; tag() still runs
        # the rule signals over the full text (long-request safety net).
        text, window = self._context_window(req.text)
        return self._model_signals(req, text=text), None, window

    async def tag(self, req: TagRequest) -> TagResult:
        signals, vec, window = await asyncio.to_thread(self._tag_row, req)
        # Rule signals keep the FULL request text even though the model only
        # reads the head window: pii_rules is the only layer that still sees
        # long requests (docs/jev-pii-200k/REPORT.md §5.4).
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups, time_periods=self.time_periods))
        merge_rule_pii(signals, req.text)
        return TagResult(signals=signals, vec=vec, input=window)

    async def aclose(self) -> None:
        self.model = None
        self.backbone = None
        self.head = None
        try:
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        except Exception:                         # noqa: BLE001
            pass
