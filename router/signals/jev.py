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
* the decode dicts additionally carry the **full probability distribution**
  (``prob_by_value`` / ``p_top`` / ``margin`` for choice and noul,
  ``p_by_type`` for PII) for offline calibration and replay (2026-10-24 item
  1). These keys are record-only: matching/engine never read them, and the
  existing ``value``/``confidence``/``is_rule`` values are bit-identical;
  ``JevTagger.info()`` fingerprints the loaded model + question config;
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

**Input window (2026-10-21, configurable head/tail 2026-10-24).** The model
signals read a tokenizer-exact **window** of the request text, configured as
``<head>:<tail>`` (env ``ROUTER_JEV_WINDOW`` → ``Settings.jev_window`` →
``JevTagger(window=...)``). With no window configured, the tagger uses
``"<max_length>:0"`` — tokenizer-exact head truncation, the pre-2026-10-24
behavior for **any** max_length (the deployed model's max_length is 1024, so
production reports ``"1024:0"``; a custom ``ROUTER_JEV_MAX_LENGTH`` keeps
scoring its first N tokens instead of failing startup). ``"0:1024"`` is pure
tail; ``"256:768"`` / ``"512:512"`` keep both ends joined by the 4K-simulation
separator ``"\\n\\n[...]\\n\\n"``
(``docs/jev-pii-200k/runner/eval_r4b3_headtail.py``). Texts that already fit
``max_length`` tokens are returned **verbatim** for every window, so short
requests are identical across A/B arms. There is no 8-round context shrink any
more: long requests are cropped once instead of paying tens of seconds of
re-tokenization (200K input measured 46-48 s,
``docs/jev-pii-200k/REPORT.md`` §4.6), and the window is explicit instead of
"the head that happens to survive". The rendered question template adds 47-120
tokens on top, so the prompt cap is ``max_length + PROMPT_OVERHEAD_SLACK``; the
canonical config therefore never loses its question. Only a question config
edited far beyond that slack falls back to tokenizer left-truncation
(question/options tail kept). The deterministic rule signals (``rule_signals``
/ ``merge_rule_pii``) still scan the **full** text — they are the only layer
that still sees long requests. The truncation facts (including the
``input_window`` spec) are reported through ``TagResult.input`` and land in the
console calculation log.

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
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from router.signals._math import (PII_HIT_THRESHOLD, PII_MIN_CONFIDENCE,
                                 binary_entropy_confidence, entropy_confidence,
                                 pii_hits)
from router.signals.registry import SIGNALS
from router.signals.pii_rules import merge_rule_pii, rule_pii_hits
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
def questions_sha256(questions) -> str:
    """sha256 hex digest of the question config in canonical JSON form.

    Canonical = ``sort_keys=True`` + ``ensure_ascii=False`` + compact
    separators (``(",", ":")``, UTF-8 bytes): the same config re-serialized
    with a different key order / indentation hashes identically, while any
    wording/candidate edit changes the digest. ``JevTagger.info()`` exposes it
    so an action--result ledger row can be replayed against the exact question
    pack that produced it.
    """
    canonical = json.dumps(questions, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: Separator between the head and tail slices when both are kept. Must match
#: the 4K-padded simulation (``docs/jev-pii-200k/runner/eval_r4b3_headtail.py``:
#: ``decode(ids[:512]) + "\n\n[...]\n\n" + decode(ids[-512:])``).
WINDOW_MARKER = "\n\n[...]\n\n"


def default_window_spec(max_length: int) -> str:
    """Window spec when none is configured: the legacy head-only window.

    Deriving the spec from ``max_length`` instead of a literal ``"1024:0"``
    keeps the pre-2026-10-24 behavior for a custom ``ROUTER_JEV_MAX_LENGTH`` /
    ``--max-length`` when ``ROUTER_JEV_WINDOW`` is unset: the first
    ``max_length`` tokens are scored and startup does not fail on the default
    spec. The deployed model has ``max_length=1024``, so production reports
    ``"1024:0"``.
    """
    return f"{max_length}:0"


def parse_window(spec: str, max_length: int) -> tuple[int, int]:
    """Parse + validate a ``"<head>:<tail>"`` input-window spec.

    Both numbers are non-negative integers, at least one token is kept
    (``head+tail > 0``) and the retained content may not exceed the model's
    context window (``head+tail <= max_length``). Invalid specs raise
    :class:`JevTaggerError`: a bad ``ROUTER_JEV_WINDOW`` must refuse startup
    instead of silently scoring a different window than the operator intended.
    """
    raw = str(spec).strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise JevTaggerError(
            f"window spec {spec!r} must be '<head>:<tail>' (e.g. '1024:0')")
    try:
        head, tail = (int(part) for part in parts)
    except ValueError:
        raise JevTaggerError(
            f"window spec {spec!r} must have integer head and tail") from None
    if head < 0 or tail < 0:
        raise JevTaggerError(
            f"window spec {spec!r} must have non-negative head and tail")
    if head + tail == 0:
        raise JevTaggerError(f"window spec {spec!r} keeps no tokens")
    if head + tail > max_length:
        raise JevTaggerError(
            f"window spec {spec!r} keeps {head + tail} tokens > max_length "
            f"{max_length}")
    return head, tail


def _decode_head(ids: list[int], tokenizer, budget: int) -> tuple[str, int]:
    """Decode the first ``budget`` tokens (head), repaired + re-encode checked.

    ``tokenizer.decode`` of a truncated id list can end on a partial byte
    (U+FFFD) or re-encode longer than the slice; both are trimmed by dropping
    tokens off the **end** until the decoded text re-encodes to <= ``budget``.
    This is the pre-2026-10-24 ``head_window`` loop unchanged (same text out).
    """
    cut = min(len(ids), budget)
    while cut > 0:
        head = tokenizer.decode(ids[:cut], skip_special_tokens=False)
        if head.endswith("\ufffd"):       # cut landed inside a multi-byte char
            cut -= 1
            continue
        used = len(tokenizer(head, add_special_tokens=False)["input_ids"])
        if used <= budget:
            return head, used
        cut -= max(1, used - budget)
    return "", 0


def _decode_tail(ids: list[int], tokenizer, budget: int) -> tuple[str, int]:
    """Decode the last ``budget`` tokens (tail), repaired + re-encode checked.

    Symmetric to :func:`_decode_head`: a decode of a truncated slice can start
    on a partial byte (U+FFFD) or re-encode longer than the slice; tokens are
    dropped off the **head** of the tail slice until the decoded text
    re-encodes to <= ``budget``.
    """
    start = max(0, len(ids) - budget)
    while start < len(ids):
        tail = tokenizer.decode(ids[start:], skip_special_tokens=False)
        if tail.startswith("\ufffd"):     # cut landed inside a multi-byte char
            start += 1
            continue
        used = len(tokenizer(tail, add_special_tokens=False)["input_ids"])
        if used <= budget:
            return tail, used
        start += max(1, used - budget)    # decode grew: drop from the head
    return "", 0


def _decode_head_tail(ids: list[int], tokenizer, head: int, tail: int
                      ) -> tuple[str, int]:
    """Decode the head + tail slices and join them with :data:`WINDOW_MARKER`.

    Each side is repaired independently; the **joined content** (marker
    excluded) is then re-encoded and checked against ``head+tail`` because
    tokenization is not strictly additive at the join (the concatenation can
    cost an extra token). While over budget one token is dropped from the
    larger slice (head from its end, tail from its start). The returned
    ``input_visible_tokens`` counts content tokens only.
    """
    head_ids = list(ids[:head])
    tail_ids = list(ids[-tail:])
    budget = head + tail
    while True:
        head_text, _ = _decode_head(head_ids, tokenizer, head)
        tail_text, _ = _decode_tail(tail_ids, tokenizer, tail)
        content = head_text + tail_text
        visible = (len(tokenizer(content, add_special_tokens=False)["input_ids"])
                   if content else 0)
        if visible <= budget:
            if head_text and tail_text:
                return head_text + WINDOW_MARKER + tail_text, visible
            return head_text or tail_text, visible
        if not tail_ids or (head_ids and len(head_ids) >= len(tail_ids)):
            head_ids = head_ids[:-1]
        else:
            tail_ids = tail_ids[1:]
        if not head_ids and not tail_ids:
            return "", 0


def window_text(text: str, tokenizer, cap: int, head: int, tail: int
                ) -> tuple[str, dict]:
    """Apply a ``<head>:<tail>`` token window to ``text``.

    * ``total <= cap``: the text already fits the model context — returned
      **verbatim** for every window (short requests are arm-independent);
    * ``tail == 0``: head-only, byte-identical to the old ``head_window``;
    * ``head == 0``: pure tail (the last ``tail`` tokens; ``== cap`` for the
      canonical ``"0:1024"`` spec), no marker;
    * both > 0: head + :data:`WINDOW_MARKER` + tail, content tokens <= head+tail.

    ``stats`` carries the request-log keys: ``input_tokens`` (original),
    ``input_visible_tokens`` (scored **content** tokens, marker excluded),
    ``input_truncated`` and ``input_window`` (the ``"<head>:<tail>"`` spec).
    """
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    total = len(ids)
    spec = f"{head}:{tail}"
    if total <= cap:
        return text, {"input_tokens": total, "input_visible_tokens": total,
                      "input_truncated": False, "input_window": spec}
    if head == 0:
        window, visible = _decode_tail(ids, tokenizer, tail)
    elif tail == 0:
        window, visible = _decode_head(ids, tokenizer, head)
    else:
        window, visible = _decode_head_tail(ids, tokenizer, head, tail)
    return window, {"input_tokens": total, "input_visible_tokens": visible,
                    "input_truncated": True, "input_window": spec}


def head_window(text: str, tokenizer, cap: int) -> tuple[str, dict]:
    """Keep the first ``cap`` tokenizer tokens of ``text`` (head-only window).

    Backward-compatible wrapper for the pre-2026-10-24 helper:
    ``window_text(..., cap, head=cap, tail=0)``. The text and the three
    original stats values are identical to the old implementation; only the
    extra ``input_window`` key is new.
    """
    return window_text(text, tokenizer, cap, cap, 0)


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
        if len(candidates) < 2:
            # K=1 has no distribution: the argmax is forced and the entropy
            # confidence is 0.0 (no evidence), so such a signal can never
            # behave like a calibrated choice. Reject at save/startup instead.
            raise JevTaggerError(
                f"{key}: candidates must have at least 2 values "
                f"(got {len(candidates)})")
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
    """Decode a choice softmax: value/confidence plus the full distribution.

    The added keys are record-only (offline calibration/replay, plan 1.1):

    * ``prob_by_value`` -- candidate key -> softmax probability, in the
      configured candidate order (``record["answer_keys"]``);
    * ``p_top`` -- the winning probability (``max(probs)``);
    * ``margin`` -- ``p_top - second`` (0.0 when fewer than two candidates
      reach here; the config validator rejects K < 2).

    ``value``/``confidence``/``is_rule`` keep the exact old values: matching
    and the routing engine read only those.
    """
    keys = record["answer_keys"]
    idx = max(range(len(probs)), key=lambda i: probs[i])
    ranked = sorted(probs, reverse=True)
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else 0.0
    return {"value": keys[idx], "confidence": entropy_confidence(probs),
            "is_rule": False,
            "prob_by_value": {key: probs[i] for i, key in enumerate(keys)},
            "p_top": ranked[0], "margin": margin}


def decode_noul(record: dict, probs: list[float], *, positive: str,
                negative: str) -> dict:
    """Decode a yes/no softmax: value/confidence plus the full distribution.

    The added keys are record-only (plan 1.1): ``p_yes`` = ``probs[1]`` (after
    softmax, the temperature was applied in ``_score``), ``prob_by_value`` =
    ``{"false": probs[0], "true": probs[1]}`` and ``margin`` = the absolute
    gap between the two. ``value`` (strictly > 0.5 decodes positive) and
    ``confidence`` are unchanged: matching/engine read only those.
    """
    p_yes = probs[1]
    p_no = probs[0]
    # Strictly greater: at p_yes == 0.5 the model has no evidence either way,
    # and the positive class is the intercepting one for jailbreak (P2 4.2).
    return {"value": positive if p_yes > 0.5 else negative,
            "confidence": entropy_confidence(probs), "is_rule": False,
            "p_yes": p_yes,
            "prob_by_value": {"false": p_no, "true": p_yes},
            "margin": abs(p_yes - p_no)}


def fill_rule_pii_probabilities(signals: dict, text: str, *,
                                scored_p_by_type: dict | None = None) -> dict:
    """Attach ``signals['pii']['p_by_type']`` after ``merge_rule_pii``.

    ``merge_rule_pii`` rebuilds ``signals['pii']`` with the decision keys only
    (``value``/``confidence``/``conf_by_value``/``is_rule``), dropping the
    model distribution, so ``tag()`` snapshots the scored ``p_by_type`` first
    and passes it in here. The rule hits (deterministic) are then added at
    **1.0**, the same convention ``conf_by_value`` uses for them.

    ``p_by_type`` and ``conf_by_value`` answer different questions and must not
    be conflated: ``p_by_type`` = the full distribution over every scored
    subtype (plus the deterministic 1.0 overrides for regex hits), while
    ``conf_by_value`` = the decision confidence per *hit* (binary entropy for
    model hits, 1.0 for rule hits). Types in ``p_by_type`` are therefore a
    superset of the hit types and of ``conf_by_value`` keys: weak positives
    stay in the distribution even though they are not a hit.

    A trimmed scope may not score ``pii`` at all; if a rule fires anyway the
    map is created with just the rule hits, so a replay sees every type whose
    probability effectively participated in the decision.
    """
    pii = signals.get("pii")
    if not isinstance(pii, dict):
        return signals
    p_by_type = dict(scored_p_by_type or {})
    for typ in rule_pii_hits(text):
        p_by_type[typ] = 1.0
    pii["p_by_type"] = p_by_type
    return signals


# --------------------------------------------------------------------------- #
# Tagger
# --------------------------------------------------------------------------- #
class JevTagger:
    """Open-Jev main model as a Tagger (name="jev")."""

    name = "jev"

    def __init__(self, model_dir: str | Path, *, questions_path: str | Path | None = None,
                 device: str | None = None, max_length: int | None = None,
                 window: str | None = None, kw_groups: dict | None = None,
                 model_factory=None, warmup: bool = True):
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
        #: Input window ``"<head>:<tail>"`` (plan item 2). ``window=None``
        #: (``ROUTER_JEV_WINDOW`` unset) keeps the legacy head-only behavior at
        #: the **effective** max_length (``default_window_spec``), so a custom
        #: ``ROUTER_JEV_MAX_LENGTH`` / ``--max-length`` still scores the first
        #: max_length tokens and never fails on the literal 1024 default.
        #: Explicit specs are validated against ``max_length`` here (pure
        #: parse_window), so a bad ``ROUTER_JEV_WINDOW`` refuses startup instead
        #: of silently scoring a different window than the operator intended.
        #: The saved spec is canonicalised (" 256: 768 " -> "256:768"), so
        #: info() and the request-log/ledger ``jev_window`` always agree.
        spec = (window if window is not None
                else default_window_spec(self.max_length))
        self.window = parse_window(spec, self.max_length)
        self.window_spec = f"{self.window[0]}:{self.window[1]}"
        #: The rendered question template sits on top of the context window, so
        #: the prompt cap is relaxed by PROMPT_OVERHEAD_SLACK instead of
        #: shrinking the context to fit (the old shrink accidentally read only
        #: the document head; see the module docstring). The marker joining a
        #: head+tail window costs a few extra tokens, well inside the slack.
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

    def info(self) -> dict:
        """Version/fingerprint of the loaded signal pack (ledger replay).

        Consumers must probe with ``getattr(tagger, "info", None)``: only the
        JEV tagger implements this (plan 1.3). ``questions_sha256`` hashes the
        in-memory validated config, so a hot config edit changes it on the
        next request; ``model_dir`` is absolute, so a ledger row can be matched
        to the deployed export; ``window`` is the effective ``<head>:<tail>``
        spec (``"<max_length>:0"`` when no window is configured — ``"1024:0"``
        for the deployed model) so a replay reproduces the input window.
        """
        model_dir = Path(self.model_dir).resolve()
        return {"engine": "jev",
                "model_dir": str(model_dir),
                "model_name": model_dir.name,
                "temperature": float(self.temperature),
                "max_length": int(self.max_length),
                "window": self.window_spec,
                "questions_sha256": questions_sha256(self.questions)}

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
        """Apply the configured ``<head>:<tail>`` token window to the request."""
        head, tail = self.window
        windowed, window = window_text(text, self.tokenizer, self.max_length,
                                       head, tail)
        if window["input_truncated"]:
            logger.debug("jev input truncated: tokens=%d visible=%d window=%s",
                         window["input_tokens"], window["input_visible_tokens"],
                         window["input_window"])
        return windowed, window

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
        # ``text`` is the windowed context from _tag_row; direct callers (unit
        # tests, bench scripts) default to the un-windowed request text.
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
                "conf_by_value": conf_by_value, "is_rule": False,
                # Full model distribution over **every scored subtype** (full
                # pack = 30, trimmed scope = scope subset), not just the hits:
                # weak positives are absent from value/conf_by_value but stay
                # here for calibration. Rule hits are added in tag() at 1.0
                # (fill_rule_pii_probabilities) — see the semantics note there.
                "p_by_type": dict(zip(labels, p_yes_per_type))}
        return signals

    def _tag_row(self, req: TagRequest) -> tuple[dict, None, dict]:
        # Model signals read the configured window only; tag() still runs the
        # rule signals over the full text (long-request safety net).
        text, window = self._context_window(req.text)
        return self._model_signals(req, text=text), None, window

    async def tag(self, req: TagRequest) -> TagResult:
        signals, vec, window = await asyncio.to_thread(self._tag_row, req)
        # Rule signals keep the FULL request text even though the model only
        # reads the window: pii_rules is the only layer that still sees long
        # requests (docs/jev-pii-200k/REPORT.md §5.4).
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups, time_periods=self.time_periods))
        # merge_rule_pii rebuilds signals["pii"] with the decision keys only, so
        # snapshot the model distribution (if pii was scored) across the merge.
        pii_sig = signals.get("pii")
        scored_p_by_type = (dict(pii_sig.get("p_by_type") or {})
                            if isinstance(pii_sig, dict) else None)
        merge_rule_pii(signals, req.text)
        # The full-distribution field is JEV-only: re-attach it and record the
        # deterministic regex hits at p=1.0 (merge_rule_pii itself stays shared
        # with the checkpoint tagger and only touches value/conf_by_value).
        fill_rule_pii_probabilities(signals, req.text,
                                    scored_p_by_type=scored_p_by_type)
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
