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
  (``prob_by_value`` / ``p_top`` / ``margin`` for choice and noul) for offline
  calibration and replay. For PII the **model evidence is kept separate from
  the regex rules** (2026-10-24 P1-A): ``model_p_by_type`` is the raw model
  softmax per scored subtype and is never overwritten by a rule, ``model_hits``
  is the pure-model hit list, ``rule_hits`` is the full-text regex hit list,
  while ``value`` / ``confidence`` / ``conf_by_value`` / ``is_rule`` stay the
  merged *effective* decision signal (rules enter at confidence 1.0). These
  keys are record-only: matching/engine never read them;
* ``TagResult`` carries a frozen version snapshot captured at ``tag()`` entry:
  ``questions_sha256`` (canonical JSON of the config actually used to render
  the scored pack) and ``tagger_info`` (``JevTagger.info()``: model/window +
  head/tokenizer/chat-template/weights content hashes). A hot question-config
  edit or a later ``info()`` call can therefore never desync a request's
  ledger row from the pack that was scored;
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

**Long-text PII chunking (2026-10-24 P2-A, opt-in).** For requests longer than
``max_length`` the head/tail window structurally cannot see the middle of the
text, so the **PII multi-signal** can optionally be scored on a small set of
planned segments instead of the window (``chunk_segments > 0``, default off):
one head segment, one tail segment, segments around full-text regex hits
(``pii_rules.rule_pii_spans``) and uniformly spaced fillers, with a fixed 25%
overlap. Each segment asks only the PII subtypes the pipeline scope can read;
per-type segment probabilities are combined with :func:`aggregate_presence`
(``noisy_or`` by default, optional ``logsumexp``/``max``) into request-level
``model_p_by_type``, and the normal PII hit rule + rule merge then apply
unchanged (P1-A evidence contract). The other six signals always keep the
single-window path. With chunking disabled (or a short request) the signals and
``TagResult.input`` are byte-identical to the pre-chunk behavior.

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

from router.decision.pack import CAPABILITY_SIGNAL
from router.signals._math import (PII_HIT_THRESHOLD, PII_MIN_CONFIDENCE,
                                 binary_entropy_confidence, entropy_confidence,
                                 pii_hits)
from router.signals.registry import SIGNALS
from router.signals.pii_rules import (merge_rule_pii, rule_pii_hits,
                                    rule_pii_spans)
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


#: Files hashed into ``JevTagger.info()`` / ``TagResult.tagger_info``. They are
#: content versions of the *frozen* model artifacts, so a ledger/replay can pin
#: the exact deployed export. Computed once at startup (weights are streamed in
#: 1 MiB chunks: the deployed file is ~1.7 GB and hashing costs ~1-2 s).
HEAD_FILE = "head.pt"
#: Tokenizer vocabulary: the fast tokenizer JSON is canonical; the
#: sentencepiece model is the fallback for older exports.
TOKENIZER_FILES = ("tokenizer.json", "tokenizer.model")
#: Fallback chat-template file when ``tokenizer.chat_template`` is not set.
CHAT_TEMPLATE_FILE = "chat_template.jinja"


def sha256_file(path) -> str | None:
    """Streaming sha256 hex digest of one file (``None`` if it is missing)."""
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_files(paths) -> str | None:
    """Streaming sha256 over model weight files (``None`` if none exist).

    A single file is hashed as plain file content (so it equals
    ``sha256sum <file>``); multiple shards are hashed in the given order with
    ``<name>\0<content>`` per shard so the shard layout is part of the digest.
    """
    files = [Path(p) for p in paths]
    files = [p for p in files if p.is_file()]
    if not files:
        return None
    if len(files) == 1:
        return sha256_file(files[0])
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def model_file_hashes(model_dir) -> dict:
    """Content hashes of the deployed model files (``None`` when absent).

    Returns ``head_sha256`` (``head.pt``), ``tokenizer_sha256`` (the first
    existing file of ``TOKENIZER_FILES``) and ``weights_sha256`` (sorted
    ``*.safetensors``, falling back to ``*.bin``; streamed, see
    :func:`sha256_files`).
    """
    d = Path(model_dir)
    weights = sorted(d.glob("*.safetensors"))
    if not weights:
        weights = sorted(d.glob("*.bin"))
    tokenizer_file = next(
        (d / name for name in TOKENIZER_FILES if (d / name).is_file()), None)
    return {
        "head_sha256": sha256_file(d / HEAD_FILE),
        "tokenizer_sha256": sha256_file(tokenizer_file) if tokenizer_file else None,
        "weights_sha256": sha256_files(weights),
    }


def chat_template_sha256(tokenizer, model_dir) -> str | None:
    """sha256 of the *effective* chat template (tokenizer string, file fallback).

    The template string is what transformers renders prompts with, so its hash
    is the meaningful version; ``chat_template.jinja`` is only used when the
    tokenizer does not expose a template (e.g. a test fake).
    """
    template = getattr(tokenizer, "chat_template", None) if tokenizer is not None else None
    if isinstance(template, str) and template:
        return hashlib.sha256(template.encode("utf-8")).hexdigest()
    return sha256_file(Path(model_dir) / CHAT_TEMPLATE_FILE)


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


# --------------------------------------------------------------------------- #
# Long-text PII chunking (2026-10-24 P2-A)
# --------------------------------------------------------------------------- #
#: Fixed overlap between planned PII segments (fraction of the segment size).
CHUNK_OVERLAP = 0.25
#: Supported request-level presence aggregators. ``max`` is a control arm only:
#: taking the per-segment maximum after the p>.5 rule under-counts the evidence
#: of a type that appears in several segments.
CHUNK_AGGS = ("noisy_or", "logsumexp", "max")


def _sigmoid(value: float) -> float:
    """Numerically stable logistic function."""
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _logit(prob: float, eps: float = 1e-6) -> float:
    p = min(1.0 - eps, max(eps, _clamp01(prob)))
    return math.log(p / (1.0 - p))


def aggregate_presence(segment_probs, method: str = "noisy_or", *,
                       bias: float = 0.0, scale: float = 1.0) -> float:
    """Combine per-segment presence probabilities into a request-level p.

    ``segment_probs`` are the model's ``p_yes`` for ONE PII type on each scored
    segment. A PII type is presence-like ("does the request contain it"), so
    the segment evidence must be combined before the request-level p>.5 hit
    rule is applied -- taking ``max`` after the per-segment threshold would
    throw away multi-segment evidence.

    * ``noisy_or`` (default): ``1 - prod(1 - p_i)``;
    * ``logsumexp``: ``sigmoid(scale * (logsumexp(logit(p_i)) - log(n)) + bias)``;
    * ``max``: ``max(p_i)`` -- control arm only.

    ``bias``/``scale`` are the offline calibration for this type (``scale`` is
    only used inside the ``logsumexp`` formula; ``noisy_or``/``max`` apply them
    as a logit-space calibration after aggregation). With the defaults the
    function is the identity calibration.
    """
    probs = [_clamp01(p) for p in segment_probs]
    if not probs:
        return 0.0
    n = len(probs)
    if method == "noisy_or":
        p = 1.0 - math.prod(1.0 - value for value in probs)
        if bias == 0.0 and scale == 1.0:
            return _clamp01(p)
        return _sigmoid(scale * _logit(p) + bias)
    if method == "max":
        p = max(probs)
        if bias == 0.0 and scale == 1.0:
            return _clamp01(p)
        return _sigmoid(scale * _logit(p) + bias)
    if method == "logsumexp":
        logits = [_logit(value) for value in probs]
        top = max(logits)
        lse = top + math.log(sum(math.exp(value - top) for value in logits))
        return _sigmoid(scale * (lse - math.log(n)) + bias)
    raise ValueError(f"unknown presence aggregation {method!r}; "
                     f"choose from {CHUNK_AGGS}")


def _char_positions_to_token_indices(tokenizer, text: str, positions) -> list[int]:
    """Map char offsets to token indices, deterministic and tokenizer-exact.

    Fast tokenizers expose ``offset_mapping`` so the whole text is tokenized
    once; slow/test tokenizers fall back to tokenizing the prefix up to each
    position (exact, but O(positions * text) -- fine for the handful of regex
    hits that reach this helper).
    """
    positions = [max(0, min(int(pos), len(text))) for pos in positions]
    if not positions:
        return []
    try:
        enc = tokenizer(text, add_special_tokens=False,
                        return_offsets_mapping=True)
        offsets = enc.get("offset_mapping") if isinstance(enc, dict) else None
        if offsets is not None:
            pairs = [(int(pair[0]), int(pair[1])) for pair in offsets]
            unique = sorted(set(positions))
            mapping: dict[int, int] = {}
            index = 0
            for pos in unique:
                while index < len(pairs) and pairs[index][1] <= pos:
                    index += 1
                mapping[pos] = index
            return [mapping[pos] for pos in positions]
    except (TypeError, ValueError, KeyError):
        pass                                 # fall back to prefix tokenization
    return [len(tokenizer(text[:pos], add_special_tokens=False)["input_ids"])
            for pos in positions]


def _clamp_segment(start: int, size: int, total: int) -> tuple[int, int]:
    start = max(0, min(int(start), max(0, total - int(size))))
    return start, min(total, start + int(size))


def plan_pii_segments(text: str, tokenizer, *, size: int, max_segments: int,
                      rule_spans=None, ids=None) -> list[dict]:
    """Plan the token segments for long-text PII scoring.

    Returns an ordered, de-duplicated list within the ``max_segments`` budget:
    ``[{"start", "end", "reason"}]`` with **token** offsets (``end`` excluded)
    into the full-text ids. Sources, in priority order:

    * head segment (``reason="head"``) and tail segment (``reason="tail"``);
    * one window around each ``pii_rules.rule_pii_spans`` hit
      (``reason="rule:<type>"``; char spans are mapped to tokens);
    * uniformly spaced fillers until the budget is used (``reason="uniform"``).

    Segments do not exceed ``size`` tokens and near-identical windows (more than
    75% of a window overlapping an already planned one) are dropped; the fixed
    :data:`CHUNK_OVERLAP` (25%) is the intended overlap of neighbouring windows.
    ``rule_spans``/``ids`` may be supplied to avoid recomputation. Empty text
    returns ``[]``. Output is sorted by ``(start, end)`` and deterministic.
    """
    if size <= 0 or max_segments <= 0:
        return []
    text = text or ""
    if ids is None:
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    ids = list(ids)
    total = len(ids)
    if total <= 0:
        return []
    window = min(int(size), total)
    if rule_spans is None:
        rule_spans = rule_pii_spans(text)

    #: A candidate that overlaps an already planned window by more than
    #: ``1 - CHUNK_OVERLAP`` is a near-duplicate (the intended overlap between
    #: neighbouring windows is CHUNK_OVERLAP = 25%); drop it to keep the budget
    #: for genuinely new text.
    duplicate_overlap = (1.0 - CHUNK_OVERLAP) * window

    def accepted_ok(candidate: dict, kept: list[dict]) -> bool:
        for segment in kept:
            overlap = (min(candidate["end"], segment["end"])
                       - max(candidate["start"], segment["start"]))
            if overlap > duplicate_overlap:
                return False
        return True

    planned: list[dict] = []
    head = {"start": 0, "end": window, "reason": "head"}
    planned.append(head)
    if max_segments >= 2:
        tail_start = max(0, total - window)
        tail = {"start": tail_start, "end": total, "reason": "tail"}
        if tail["start"] != head["start"]:
            planned.append(tail)

    if rule_spans and len(planned) < max_segments:
        positions = []
        for _typ, start_char, end_char in rule_spans:
            positions.extend((start_char, end_char))
        token_pos = _char_positions_to_token_indices(tokenizer, text, positions)
        candidates = []
        for index, (typ, _start_char, _end_char) in enumerate(rule_spans):
            tok_start = token_pos[2 * index]
            tok_end = token_pos[2 * index + 1]
            center = (tok_start + tok_end) // 2
            start, end = _clamp_segment(center - window // 2, window, total)
            candidates.append({"start": start, "end": end,
                               "reason": f"rule:{typ}"})
        candidates.sort(key=lambda item: (item["start"], item["end"],
                                          item["reason"]))
        for candidate in candidates:
            if len(planned) >= max_segments:
                break
            if candidate["start"] >= candidate["end"]:
                continue
            if accepted_ok(candidate, planned):
                planned.append(candidate)

    remaining = max_segments - len(planned)
    if remaining > 0 and total > window:
        # More candidates than slots: a near-duplicate can be skipped and the
        # next uniformly spaced window still gets a chance.
        tries = max(remaining * 2, remaining + 2)
        for slot in range(tries):
            center = int(total * (slot + 0.5) / tries)
            start, end = _clamp_segment(center - window // 2, window, total)
            candidate = {"start": start, "end": end, "reason": "uniform"}
            if candidate["start"] >= candidate["end"]:
                continue
            if accepted_ok(candidate, planned):
                planned.append(candidate)
                if len(planned) >= max_segments:
                    break

    planned.sort(key=lambda item: (item["start"], item["end"], item["reason"]))
    return planned[:max_segments]


def load_chunk_calibration(source) -> tuple[dict, str]:
    """Load the optional per-type PII presence calibration.

    Format: JSON object mapping a canonical PII type to
    ``{"bias": float, "scale": float}`` (both optional, defaults 0.0/1.0). The
    reserved key ``"*"`` is a fallback applied to every type that has no explicit
    entry. ``source`` may be ``None``/``""``/``{}`` (no calibration), an
    in-memory dict, or a JSON file path. Returns ``(mapping, description)`` where
    the description goes into ``info()``/``tagger_info``. Invalid input raises
    :class:`JevTaggerError` so a bad ``ROUTER_JEV_CHUNK_CALIBRATION`` refuses
    startup.
    """
    if source is None or source == "" or source == {}:
        return {}, "none"
    if isinstance(source, dict):
        data = dict(source)
        description = "inline"
    elif isinstance(source, (str, Path)):
        path = Path(source)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JevTaggerError(
                f"cannot read chunk calibration {path}: {exc}") from exc
        description = str(path)
    else:
        raise JevTaggerError(
            f"chunk calibration must be dict/JSON path/None, got {type(source)!r}")
    if not isinstance(data, dict):
        raise JevTaggerError("chunk calibration must be a JSON object")
    calibration: dict[str, dict] = {}
    for typ, entry in data.items():
        if not isinstance(entry, dict):
            raise JevTaggerError(
                f"chunk calibration for {typ!r} must be an object")
        try:
            bias = float(entry.get("bias", 0.0))
            scale = float(entry.get("scale", 1.0))
        except (TypeError, ValueError) as exc:
            raise JevTaggerError(
                f"chunk calibration for {typ!r} has non-numeric bias/scale") from exc
        if not (math.isfinite(bias) and math.isfinite(scale)):
            raise JevTaggerError(
                f"chunk calibration for {typ!r} must be finite")
        calibration[str(typ)] = {"bias": bias, "scale": scale}
    return calibration, description


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


def attach_pii_evidence(signals: dict, text: str, *,
                        model_p_by_type: dict | None = None,
                        model_hits: list[str] | None = None) -> dict:
    """Attach the three-way PII evidence to ``signals['pii']`` after the merge.

    ``pii_rules.merge_rule_pii`` rebuilds ``signals['pii']`` with the effective
    decision keys only (``value``/``confidence``/``conf_by_value``/``is_rule``),
    so ``tag()`` captures the model evidence before the merge and passes it in
    here. The three evidence fields are independent and must never be collapsed
    (2026-10-24 P1-A review):

    * ``model_p_by_type`` -- raw model softmax ``p_yes`` per **scored** subtype
      (full pack = 30; trimmed scope = scope subset). Rules never overwrite it,
      so a probability of 0.1 stays 0.1 even when the regex hits that type;
    * ``model_hits`` -- the pure-model hit list (``p > PII_HIT_THRESHOLD`` and
      ``binary_entropy_confidence >= PII_MIN_CONFIDENCE``);
    * ``rule_hits`` -- deterministic regex hits over the **full** request text
      (even when a trimmed scope did not score ``pii`` at all).

    The effective ``value``/``conf_by_value``/``is_rule`` (union of model hits
    and rule hits, rules at confidence 1.0) are left exactly as
    ``merge_rule_pii`` derived them; consumers that only route/block keep
    reading those. Offline PII reports use the separate layers above.
    """
    pii = signals.get("pii")
    if not isinstance(pii, dict):
        return signals
    pii["model_p_by_type"] = dict(model_p_by_type or {})
    pii["model_hits"] = list(model_hits or [])
    pii["rule_hits"] = rule_pii_hits(text)
    return signals


# --------------------------------------------------------------------------- #
# Tagger
# --------------------------------------------------------------------------- #
class JevTagger:
    """Open-Jev main model as a Tagger (name="jev")."""

    name = "jev"

    def __init__(self, model_dir: str | Path, *, questions_path: str | Path | None = None,
                 device: str | None = None, max_length: int | None = None,
                 window: str | None = None,
                 chunk_segments: int = 0, chunk_size: int = 512,
                 chunk_agg: str = "noisy_or", chunk_calibration=None,
                 kw_groups: dict | None = None,
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
        #: Long-text PII chunking (2026-10-24 P2-A, opt-in). Only the PII
        #: multi-signal is chunked; the other six signals keep the single
        #: window. ``chunk_segments=0`` (default) disables the whole path so
        #: signals and TagResult.input stay byte-identical to the old behavior.
        self.chunk_segments = int(chunk_segments)
        if self.chunk_segments < 0:
            raise JevTaggerError("chunk_segments must be >= 0")
        self.chunk_size = int(chunk_size)
        if self.chunk_size <= 0:
            raise JevTaggerError("chunk_size must be > 0")
        # Only validate against max_length when the feature is actually on: a
        # deployment that disables chunking must not fail on an unused size.
        if self.chunk_segments > 0 and self.chunk_size > self.max_length:
            raise JevTaggerError(
                f"chunk_size {self.chunk_size} > max_length {self.max_length}")
        self.chunk_agg = str(chunk_agg).strip()
        if self.chunk_agg not in CHUNK_AGGS:
            raise JevTaggerError(
                f"chunk_agg must be one of {CHUNK_AGGS}, got {chunk_agg!r}")
        self.chunk_enabled = self.chunk_segments > 0
        self.chunk_overlap = CHUNK_OVERLAP
        self.chunk_calibration, self._chunk_calib_desc = \
            load_chunk_calibration(chunk_calibration)
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
        #: Frozen content-version snapshot: model/window metadata + head /
        #: tokenizer / chat-template / weights hashes. Hashed ONCE here (the
        #: weights file is ~1.7 GB and is streamed), cached for info() and for
        #: the per-request TagResult.tagger_info snapshot; nothing re-reads the
        #: tagger or the files after scoring.
        self._static_info_cache = self._compute_static_info()
        if warmup:
            self._warmup()

    def set_questions(self, cfg: dict) -> dict:
        """Hot-apply an edited question config (validated before replacing)."""
        self.questions = validate_question_config(cfg)
        return self.questions

    def _compute_static_info(self) -> dict:
        """Artifact versions that only change when the model export changes.

        Computed once at startup (the weights hash streams ~1.7 GB) and then
        reused: ``questions_sha256`` stays per-call because the question config
        is hot-editable, while these keys are frozen. Missing files hash to
        ``None`` so unit-test fakes keep working.
        """
        model_dir = Path(self.model_dir).resolve()
        info = {
            "engine": "jev",
            "model_dir": str(model_dir),
            "model_name": model_dir.name,
            "temperature": float(self.temperature),
            "max_length": int(self.max_length),
            "window": self.window_spec,
        }
        info.update(model_file_hashes(self.model_dir))
        info["chat_template_sha256"] = chat_template_sha256(
            getattr(self, "tokenizer", None), self.model_dir)
        # Long-text PII chunking config is part of the frozen version map even
        # when disabled (chunk_agg "off"), so a replay knows whether the same
        # request would have been chunked. getattr keeps object.__new__ stubs
        # (unit tests) working without the full constructor.
        info["chunk_segments"] = int(getattr(self, "chunk_segments", 0))
        info["chunk_size"] = int(getattr(self, "chunk_size", 512))
        info["chunk_agg"] = (getattr(self, "chunk_agg", "noisy_or")
                             if getattr(self, "chunk_enabled", False) else "off")
        info["chunk_overlap"] = CHUNK_OVERLAP
        info["chunk_calibration"] = getattr(self, "_chunk_calib_desc", "none")
        return info

    def _static_info(self) -> dict:
        """The cached static snapshot (lazily built for object.__new__ stubs)."""
        cached = getattr(self, "_static_info_cache", None)
        if cached is None:                  # unit tests build taggers via __new__
            cached = self._compute_static_info()
            self._static_info_cache = cached
        return cached

    def _info_for(self, questions) -> dict:
        """info() against one captured config object (never re-reads the tagger)."""
        return {**self._static_info(),
                "questions_sha256": questions_sha256(questions)}

    def info(self) -> dict:
        """Version/fingerprint of the loaded signal pack (ledger replay).

        Consumers must probe with ``getattr(tagger, "info", None)``: only the
        JEV tagger implements this. The static artifact hashes
        (``head_sha256``/``tokenizer_sha256``/``chat_template_sha256``/
        ``weights_sha256``) and model/window metadata are computed once at
        startup; ``questions_sha256`` hashes the **current** in-memory config,
        so a hot edit changes that key on the next request. The request path
        must use ``TagResult.questions_sha256``/``TagResult.tagger_info``
        (captured at tag() entry) instead of calling info() after a response.
        """
        return self._info_for(self.questions)

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

    def _decode_signal_pack(self, records: list[dict],
                            probs: list[list[float]],
                            req: TagRequest) -> dict:
        """Decode scored records into the signal pack (shared by both paths).

        The window path and the long-text chunk path both score a list of JEV
        records (each with one candidate distribution) and decode them with the
        exact same rules: choice argmax + entropy confidence, noul strict >.5,
        PII hit rule and P1-A model evidence. Keeping this in one place is what
        makes the chunk off/on short-text paths byte-identical.
        """
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
        # PII: one noul question per type; hits are canonical type keys. With a
        # trimmed scope only the referenced subtypes were asked; merge_rule_pii
        # (in tag()) still unions the deterministic regex hits of every type.
        # The model evidence below is pure-model and is re-attached unchanged
        # after the merge (attach_pii_evidence); value/conf_by_value/is_rule
        # here are pre-merge model values that merge_rule_pii derives the
        # effective decision signal from.
        pii_items = by_key.get("pii")
        if pii_items is not None:
            labels = [item["sub"] for item, _ in pii_items]
            p_yes_per_type = [prob[1] for _, prob in pii_items]
            hits, conf_by_value = pii_hits(p_yes_per_type, labels)
            signals["pii"] = {
                "value": hits,
                "confidence": max(conf_by_value.values()) if conf_by_value else None,
                "conf_by_value": conf_by_value, "is_rule": False,
                # Model-only evidence (2026-10-24 P1-A): the raw softmax over
                # every scored subtype (full pack = 30, trimmed = scope subset)
                # and the pure-model hit list. THE RULES NEVER OVERWRITE THESE.
                "model_p_by_type": dict(zip(labels, p_yes_per_type)),
                "model_hits": list(hits),
            }
        return signals

    def _model_signals(self, req: TagRequest, *,
                       text: str | None = None,
                       questions: dict | None = None) -> dict:
        scope = req.scope
        if scope is not None and scope.trimmed:
            logger.debug("jev trimmed pack: signals=%s pii=%s",
                         sorted(scope.signals), sorted(scope.pii_types))
        # ``questions`` is the config object tag() captured at its entry (hot
        # config edits must not desync the pack from its fingerprint); direct
        # callers (unit tests, bench scripts) default to the current attribute.
        # ``text`` is the windowed context from _tag_row; direct callers
        # default to the un-windowed request text.
        cfg = questions if questions is not None else self.questions
        records = build_signal_records(cfg,
                                       req.text if text is None else text,
                                       scope)
        # user_feedback is unconditionally overridden to null for single-turn
        # requests below (there is no previous turn), so its questions are dead
        # compute. Both the full and the trimmed path skip them => same result.
        if req.n_messages <= 1:
            records = [item for item in records
                       if item["signal"] != "user_feedback"]
        # Natural-language capability routing (2026-10-26): one extra choice
        # record scored in the SAME forward batch as the signals, so enabling
        # it costs no extra pass when the tree reads it. Trimming keeps it off
        # trees that do not reference the signal.
        capability_record = None
        if (isinstance(req.capability, dict)
                and self._capability_in_scope(scope)):
            capability_record = self._build_capability_record(
                req.capability, req.text if text is None else text)
        if capability_record is not None:
            records.append({"signal": CAPABILITY_SIGNAL, "sub": None,
                            "record": capability_record})
        probs = self._score([item["record"] for item in records])
        if capability_record is not None:
            capability_probs = probs.pop()
            signals = self._decode_signal_pack(records[:-1], probs, req)
            signals[CAPABILITY_SIGNAL] = self._decode_capability(
                req.capability, capability_record, capability_probs)
            return signals
        return self._decode_signal_pack(records, probs, req)

    # ── long-text PII chunking (opt-in) ─────────────────────────────────────
    @staticmethod
    def _pii_in_scope(scope) -> bool:
        """True when the scope asks for the PII signal at all."""
        return scope is None or scope.full or "pii" in scope.signals

    def _chunk_applies(self, req: TagRequest, window: dict) -> bool:
        """Chunking only for enabled PII + a request longer than the window."""
        return (self.chunk_enabled
                and int(window.get("input_tokens") or 0) > self.max_length
                and self._pii_in_scope(req.scope))

    def _segment_text(self, ids: list[int], start: int, end: int) -> str:
        """Decode one planned token segment (boundary U+FFFD trimmed)."""
        text = self.tokenizer.decode(ids[start:end], skip_special_tokens=False)
        return text.strip("\ufffd")

    def _chunk_prompt_tokens(self, records: list[dict]) -> int:
        """Exact prompt-token cost of the segment scoring (all types x segments)."""
        if not records:
            return 0
        prompts, _counts = self._render(records)
        return sum(len(self.tokenizer(prompt, add_special_tokens=True)["input_ids"])
                   for prompt in prompts)

    def _aggregate_type_probability(self, typ: str,
                                    segment_probs: list[float]) -> float:
        """Request-level presence probability for one PII type (with calibration)."""
        fallback = self.chunk_calibration.get("*") or {}
        config = self.chunk_calibration.get(typ) or fallback
        return aggregate_presence(
            segment_probs, method=self.chunk_agg,
            bias=float(config.get("bias", 0.0)),
            scale=float(config.get("scale", 1.0)))

    def _model_signals_chunked(self, req: TagRequest, *, text: str,
                               questions: dict | None = None
                               ) -> tuple[dict, dict]:
        """Windowed signals for the other six signals + chunked PII.

        Only the PII multi-signal takes the chunk path: domain/complexity/... are
        not presence-like (their aggregation semantics would be wrong), so they
        keep the configured head/tail window. Each segment asks only the PII
        subtypes the pipeline scope references; per-type segment probabilities
        are aggregated into the request-level ``model_p_by_type`` and then run
        through the unchanged request-level hit rule and P1-A evidence contract.
        """
        cfg = questions if questions is not None else self.questions
        scope = req.scope
        records = build_signal_records(cfg, text, scope)
        non_pii = [item for item in records if item["signal"] != "pii"]
        pii_items = [item for item in records if item["signal"] == "pii"]
        if req.n_messages <= 1:
            non_pii = [item for item in non_pii
                       if item["signal"] != "user_feedback"]
        window_probs = self._score([item["record"] for item in non_pii])
        signals = self._decode_signal_pack(non_pii, window_probs, req)
        if not pii_items:                     # defensive: caller checked the scope
            return signals, {}

        ids = self.tokenizer(req.text, add_special_tokens=False)["input_ids"]
        segments = plan_pii_segments(req.text, self.tokenizer,
                                     size=self.chunk_size,
                                     max_segments=self.chunk_segments,
                                     ids=ids)
        if not segments:                      # empty/undecodable text
            return signals, {}
        labels = [item["sub"] for item in pii_items]
        segment_texts = [self._segment_text(ids, segment["start"],
                                            segment["end"])
                         for segment in segments]
        staged_records: list[dict] = []
        for segment_text in segment_texts:
            for item in pii_items:
                record = dict(item["record"])
                record["state"] = segment_text
                staged_records.append(record)
        segment_probs = self._score(staged_records)
        per_type: dict[str, list[float]] = {label: [] for label in labels}
        offset = 0
        for _segment_text in segment_texts:
            for item in pii_items:
                per_type[item["sub"]].append(float(segment_probs[offset][1]))
                offset += 1

        aggregated = [self._aggregate_type_probability(label, per_type[label])
                      for label in labels]
        hits, conf_by_value = pii_hits(aggregated, labels)
        signals["pii"] = {
            "value": hits,
            "confidence": max(conf_by_value.values()) if conf_by_value else None,
            "conf_by_value": conf_by_value, "is_rule": False,
            # Request-level aggregated presence probabilities, P1-A contract:
            # the model evidence map, never overwritten by the regex rules.
            "model_p_by_type": dict(zip(labels, aggregated)),
            "model_hits": list(hits),
        }
        stats = {
            "chunk_segments": len(segments),
            "chunk_scored_segments": len(segment_texts),
            "chunk_prompt_tokens": self._chunk_prompt_tokens(staged_records),
            "chunk_strategy": "head+tail+rule+uniform:" + self.chunk_agg,
        }
        logger.debug("jev pii chunked: segments=%d types=%d prompt_tokens=%d",
                     len(segments), len(labels), stats["chunk_prompt_tokens"])
        return signals, stats

    def _tag_row(self, req: TagRequest,
                 questions: dict | None = None) -> tuple[dict, None, dict]:
        # Model signals read the configured window only; tag() still runs the
        # rule signals over the full text (long-request safety net). With the
        # opt-in long-text PII chunking on, only PII is additionally scored on
        # planned segments; everything else stays on the single-window path.
        text, window = self._context_window(req.text)
        if self._chunk_applies(req, window):
            signals, chunk_stats = self._model_signals_chunked(
                req, text=text, questions=questions)
            window.update(chunk_stats)
            # The chunked path batches only the segment prompts; score the
            # capability question separately (chunking is opt-in and rare).
            signal = self._capability_signal(req, text)
            if signal is not None:
                signals[CAPABILITY_SIGNAL] = signal
        else:
            signals = self._model_signals(req, text=text, questions=questions)
        return signals, None, window

    #: ------------------------------------------------------------------ #
    #: Natural-language capability routing (2026-10-26)
    #: ------------------------------------------------------------------ #
    @staticmethod
    def _capability_in_scope(scope) -> bool:
        """True when the pipeline tree (or a full-pack caller) reads it."""
        return (scope is None or scope.full
                or CAPABILITY_SIGNAL in scope.signals)

    def _build_capability_record(self, pack: dict, text: str) -> dict | None:
        """One JEV choice record over the pack's model capability descriptions."""
        candidates = pack.get("candidates") if isinstance(pack, dict) else None
        if not isinstance(candidates, dict) or len(candidates) < 2:
            return None
        return {"state": text, "kind": "choice",
                "question": pack.get("instructions") or "",
                "options": [f"{model}: {capability}"
                            for model, capability in candidates.items()],
                "answer_keys": list(candidates)}

    def _decode_capability(self, pack: dict, record: dict,
                           probs: list[float]) -> dict:
        """Candidate probabilities -> the ``capability_route`` signal.

        ``value`` is the model id the tree should route to. The pack's
        ``min_prob``/``min_margin`` gates implement the article's fail-open
        contract: a low-confidence / ambiguous decision routes the configured
        fallback model instead of the argmax (``fallback_used`` + ``reason``
        are record-only and never read by matching). ``confidence`` keeps the
        same entropy scale as every other model choice signal.
        """
        keys = record["answer_keys"]
        ranked = sorted(zip(keys, probs), key=lambda item: item[1], reverse=True)
        top_model, p_top = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = p_top - runner_up
        fallback = pack.get("fallback_model")
        min_prob = float(pack.get("min_prob", 0.45))
        min_margin = float(pack.get("min_margin", 0.10))
        gated, reason = False, "decision_model"
        if fallback:
            if p_top < min_prob:
                gated, reason = True, "low_confidence"
            elif margin < min_margin:
                gated, reason = True, "ambiguous"
        return {
            "value": fallback if gated else top_model,
            "confidence": entropy_confidence(probs),
            "is_rule": False,
            # record-only evidence (matching/engine read value+confidence only)
            "prob_by_value": dict(zip(keys, probs)),
            "p_top": p_top, "margin": margin, "runner_up": ranked[1][0]
            if len(ranked) > 1 else None,
            "top_model": top_model, "fallback_used": gated, "reason": reason,
            "fingerprint": pack.get("fingerprint") or "",
        }

    def _capability_signal(self, req: TagRequest, text: str) -> dict | None:
        """Score just the capability question (chunked-path fallback)."""
        pack = req.capability
        if not isinstance(pack, dict) or not self._capability_in_scope(req.scope):
            return None
        record = self._build_capability_record(pack, text)
        if record is None:
            return None
        return self._decode_capability(pack, record, self._score([record])[0])

    #: ------------------------------------------------------------------ #
    #: Natural-language capability routing (2026-10-26)
    #: ------------------------------------------------------------------ #
    def score_choice(self, state: str, instructions: str,
                     candidates: dict) -> dict:
        """Score an ad-hoc ``choice`` question with the JEV decision model.

        Used by natural-language capability routing
        (``router.decision.capability``): the operator's per-model capability
        descriptions are compiled into one JEV choice record, scored by the
        same model/head/temperature as the routing signals, and returned as a
        probability per candidate. The request text goes through the same
        tokenizer-exact input window as a signal request (``self.window``), so
        a decision never sees more context than the signal pack would.

        This is the same "one model answers a typed question" contract as
        :meth:`tag`; it does not touch the signal question config, the rule
        signals, the PII merge or any tagger state — callers can run it on the
        hot path next to ``tag()``.

        Returns ``{"probabilities": {model: p}, "ranking": [{model, p}...],
        "input": <window info>, "temperature": T}``. Raises ``JevTaggerError``
        for a malformed candidate map (a caller that wants fail-open must
        catch it; see :func:`router.decision.capability.score_with_tagger`).
        """
        if not isinstance(instructions, str) or not instructions.strip():
            raise JevTaggerError("choice instructions must be a nonempty string")
        if not isinstance(candidates, dict) or not 2 <= len(candidates) <= 255:
            raise JevTaggerError("choice needs 2..255 candidates")
        keys = list(candidates)
        for key, value in candidates.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise JevTaggerError(
                    "choice candidates must map model ids to description strings")
        windowed, window = self._context_window(state)
        record = {"state": windowed, "kind": "choice",
                  "question": instructions,
                  "options": [f"{key}: {candidates[key]}" for key in keys],
                  "answer_keys": keys}
        probs = self._score([record])[0]
        ranking = sorted(
            ({"model": key, "p": float(prob)} for key, prob in zip(keys, probs)),
            key=lambda row: row["p"], reverse=True)
        return {"probabilities": dict(zip(keys, probs)), "ranking": ranking,
                "input": window, "temperature": self.temperature}

    async def tag(self, req: TagRequest) -> TagResult:
        # Frozen snapshot (2026-10-24 P1-A): capture the question-config object
        # and the version map BEFORE the (slow) scoring starts. A concurrent
        # PUT /admin/signal-questions may swap self.questions while tag() runs;
        # the scored pack and the ledger fingerprint must describe the SAME
        # config, so both use the captured object -- never a post-hoc re-read.
        questions = self.questions
        tagger_info = self._info_for(questions)
        # Capability-routing provenance is per request (the pack is passed in
        # from the immutable snapshot), unlike the model/question fingerprints.
        if isinstance(req.capability, dict) and req.capability.get("fingerprint"):
            tagger_info = {**tagger_info,
                           "capability_fingerprint":
                               req.capability["fingerprint"]}
        signals, vec, window = await asyncio.to_thread(
            self._tag_row, req, questions)
        # Rule signals keep the FULL request text even though the model only
        # reads the window: pii_rules is the only layer that still sees long
        # requests (docs/jev-pii-200k/REPORT.md §5.4).
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups, time_periods=self.time_periods))
        # merge_rule_pii rebuilds signals["pii"] with the effective decision
        # keys only; capture the pure-model evidence across the merge so the
        # rules can never overwrite the model probabilities.
        pii_sig = signals.get("pii")
        model_p_by_type: dict = {}
        model_hits: list[str] = []
        if isinstance(pii_sig, dict):
            model_p_by_type = dict(pii_sig.get("model_p_by_type") or {})
            model_hits = list(pii_sig.get("model_hits") or [])
        merge_rule_pii(signals, req.text)
        attach_pii_evidence(signals, req.text,
                            model_p_by_type=model_p_by_type,
                            model_hits=model_hits)
        return TagResult(signals=signals, vec=vec, input=window,
                         questions_sha256=tagger_info["questions_sha256"],
                         tagger_info=tagger_info)

    async def aclose(self) -> None:
        self.model = None
        self.backbone = None
        self.head = None
        try:
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        except Exception:                         # noqa: BLE001
            pass
