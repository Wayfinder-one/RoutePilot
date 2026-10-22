"""CheckpointTagger (M2): the RoutePilot checkpoint as a Tagger.

One forward pass of ``RoutePilotModel`` (the routepilot training repo,
formerly smart-router-train) yields every model signal plus a 768-d embedding.
Three things this module exists for:

1. **Label mapping layer** (design doc section 2.5). The registry value keys are
   product-canonical; the checkpoint's ``tags.json`` spellings differ
   (``computer science`` -> ``computer_science``, ``FACT_CHECK_NEEDED`` ->
   ``needs_check``, ``SAT`` -> ``satisfied``, pii UPPER -> lower). Without this
   layer every model-signal row misses and traffic silently falls through to the
   fallback. We reuse the training repo's ``normalize_label`` when importable.
2. **Confidence** ``c = 1 - H(p) / log K`` for classification heads; per-type
   binary entropy for pii, written to ``conf_by_value`` (design section 2.2/2.3).
3. **Embedding** — the embedding head output is exposed as ``TagResult.vec``
   only (the similarity/reask signals were removed 2026-10-21); it is still
   passed through ``l2_normalize`` for the /admin/try display.

torch / transformers and the training package are imported lazily: validation
and all pure helpers work (and are unit-tested) without a GPU or torch.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from router.signals._math import (binary_entropy_confidence, entropy_confidence,
                                 l2_normalize, pii_hits, softmax)
from router.signals.registry import SIGNALS
from router.signals.pii_rules import merge_rule_pii
from router.signals.rules import rule_signals
from router.signals.tagger import TagRequest, TagResult

logger = logging.getLogger(__name__)

#: The training repo was renamed ``smart_router`` -> ``routepilot`` on
#: 2026-10-14 (smart-router-train commit e9c0781) and the model class became
#: ``RoutePilotModel``. We prefer the renamed package and keep the pre-rename
#: name as an explicit fallback so a stale checkout still boots — but a stale
#: checkout must never be *silently* mistaken for the real training repo.
TRAIN_PACKAGE_CANDIDATES = ("routepilot", "smart_router")
TRAIN_MODEL_CLASSES = {"routepilot": "RoutePilotModel",
                       "smart_router": "SmartRouterModel"}


class CheckpointError(Exception):
    """Checkpoint validation/loading failure — the app refuses to start on it."""


# --------------------------------------------------------------------------- #
# Head -> product-signal mapping (the checkpoint's flat forward contract).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeadSpec:
    head: str            # tags.json key == RoutePilotModel head name
    signal: str | None   # product signal key; None for the embedding head
    kind: str            # 'classification' | 'multilabel' | 'embedding'


HEADS: tuple[HeadSpec, ...] = (
    HeadSpec("domain", "domain", "classification"),
    HeadSpec("jailbreak", "jailbreak", "classification"),
    HeadSpec("fact_check", "fact_check", "classification"),
    HeadSpec("user_feedback", "user_feedback", "classification"),
    HeadSpec("modality", "modality", "classification"),
    HeadSpec("complexity", "complexity", "classification"),
    HeadSpec("pii", "pii", "multilabel"),
    HeadSpec("embedding", None, "embedding"),
)
HEAD_KEYS = tuple(s.head for s in HEADS)

# Training-repo alias table (mirrors routepilot.signals.LABEL_ALIASES). Kept
# local so mapping works without the training package importable; the repo's
# own normalize_label is consulted first when present.
_TRAIN_ALIASES: dict[str, dict[str, str]] = {
    "jailbreak": {"0": "benign", "1": "jailbreak",
                  "label_0": "benign", "label_1": "jailbreak"},
    "fact_check": {"FACT_CHECK_NEEDED": "fact_check_needed",
                   "NO_FACT_CHECK_NEEDED": "no_fact_check_needed"},
}
# Checkpoint raw label -> product-canonical value, applied AFTER mechanical
# normalisation (lower-case, whitespace/hyphen -> underscore). Keys are the
# mechanically normalised training spellings.
_PRODUCT_ALIASES: dict[str, dict[str, str]] = {
    "fact_check": {"fact_check_needed": "needs_check",
                   "no_fact_check_needed": "no_check"},
    "modality": {"ar": "text", "diffusion": "image", "both": "text_image"},
    "user_feedback": {"sat": "satisfied"},
}

_MECH = re.compile(r"[\s\-]+")


@functools.lru_cache(maxsize=1)
def _train_signals_module():
    """The importable training-repo ``signals`` module, or None.

    Tries the renamed ``routepilot`` first, then the pre-rename
    ``smart_router``. A miss is logged once at WARNING level (the audit found
    the old code swallowed this ImportError and silently used the local
    mirror forever); it is not fatal because three pieces of functionality do
    not need it: pure validation, the label mapping's local mirror, and
    heuristic mode.
    """
    tried = []
    for package in TRAIN_PACKAGE_CANDIDATES:
        try:
            module = importlib.import_module(f"{package}.signals")
        except Exception as exc:                        # noqa: BLE001
            tried.append(f"{package}: {exc!r}")
            continue
        logger.info("label mapping reuses %s.signals.normalize_label", package)
        return module
    logger.warning(
        "training signals package not importable (%s); label mapping falls "
        "back to the local mirror of its alias table. Checkpoint mode itself "
        "still needs the training model class and will fail loudly at startup.",
        "; ".join(tried))
    return None


def _train_normalize_label(head: str, label) -> str:
    """Reuse the training repo's alias table when importable (design 2.5),
    then the local mirror (identical spellings) as the portable fallback."""
    text = str(label)
    module = _train_signals_module()
    if module is not None:
        try:
            text = str(module.normalize_label(head, text))
        except Exception as exc:                        # noqa: BLE001
            logger.warning("training normalize_label(%r, %r) failed (%r); "
                           "falling back to the local mirror", head, text, exc)
    return _TRAIN_ALIASES.get(head, {}).get(text, text)


def import_train_model_class():
    """Import the training model class; returns ``(class, qualified_name)``.

    Prefers ``routepilot.modeling.router.RoutePilotModel`` (current training
    repo) and falls back to ``smart_router.modeling.router.SmartRouterModel``
    (pre-rename checkout). Raises :class:`CheckpointError` naming both when
    neither is importable — checkpoint mode must never silently start without
    the real model class.
    """
    tried = []
    for package in TRAIN_PACKAGE_CANDIDATES:
        qualified = f"{package}.modeling.router.{TRAIN_MODEL_CLASSES[package]}"
        try:
            module = importlib.import_module(f"{package}.modeling.router")
            cls = getattr(module, TRAIN_MODEL_CLASSES[package])
        except Exception as exc:                        # noqa: BLE001
            tried.append(f"{qualified}: {exc!r}")
            continue
        if package != TRAIN_PACKAGE_CANDIDATES[0]:
            logger.warning(
                "using the pre-rename training package %r (%s); update the "
                "training checkout to the renamed 'routepilot' package",
                package, qualified)
        return cls, qualified
    raise CheckpointError(
        "cannot import the training model class (checkpoint mode needs the "
        "training repo's src/ on PYTHONPATH plus torch/transformers "
        "installed). Tried: " + "; ".join(tried))


def canonical_label(head: str, label) -> str:
    """Checkpoint label -> product-canonical value key.

    Order: train-repo alias (raw spelling) -> mechanical normalisation ->
    product alias. Mechanical normalisation alone covers domain
    ("computer science"), pii (UPPER) and user_feedback (WRONG_ANSWER).
    """
    text = _train_normalize_label(head, label)
    text = _MECH.sub("_", str(text).strip().lower())
    return _PRODUCT_ALIASES.get(head, {}).get(text, text)


# --------------------------------------------------------------------------- #
# Validation (pure: no torch, no model build)
# --------------------------------------------------------------------------- #
@dataclass
class CheckpointFiles:
    checkpoint_dir: str
    base_model: str
    max_length: int
    embedding_dim: int
    labels: dict[str, list[str]]    # head -> canonical labels (index order)


def _expected_values(signal: str) -> set[str]:
    return {v[0] for v in SIGNALS[signal].values}


def load_and_validate(checkpoint_dir: str | Path) -> CheckpointFiles:
    """Validate router_config.json + model.pt + tags.json against the registry.

    Fails fast (CheckpointError) so a mismatched checkpoint is rejected at
    startup rather than silently missing every model signal at request time.
    """
    d = Path(checkpoint_dir)
    if not d.is_dir():
        raise CheckpointError(f"checkpoint dir not found: {d}")
    cfg_path, model_path, tags_path = (d / "router_config.json", d / "model.pt",
                                       d / "tags.json")
    for p in (cfg_path, model_path, tags_path):
        if not p.is_file():
            raise CheckpointError(f"checkpoint missing {p.name}: {p}")
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        tags = json.loads(tags_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"checkpoint JSON unreadable: {exc}") from exc
    if not isinstance(cfg, dict) or not cfg.get("base_model"):
        raise CheckpointError("router_config.json lacks base_model")
    if not isinstance(tags, dict) or not isinstance(tags.get("tags"), dict):
        raise CheckpointError("tags.json lacks a tags mapping")
    got = tags["tags"]
    if set(got) != set(HEAD_KEYS):
        raise CheckpointError(
            f"tags.json heads {sorted(got)} != expected {sorted(HEAD_KEYS)}")

    labels: dict[str, list[str]] = {}
    for spec in HEADS:
        entry = got[spec.head]
        if not isinstance(entry, dict):
            raise CheckpointError(f"tags.json.{spec.head} must be an object")
        if spec.kind == "embedding":
            if not isinstance(entry.get("dim"), int):
                raise CheckpointError(f"tags.json.{spec.head} needs an int dim")
            continue
        raw = entry.get("labels")
        if not isinstance(raw, list) or not raw:
            raise CheckpointError(f"tags.json.{spec.head} needs labels")
        canon = [canonical_label(spec.head, x) for x in raw]
        if len(set(canon)) != len(canon):
            raise CheckpointError(
                f"tags.json.{spec.head} labels collide after canonicalisation: {canon}")
        expected = _expected_values(spec.signal)
        if set(canon) != expected:
            raise CheckpointError(
                f"tags.json.{spec.head} canonical labels != registry values for "
                f"'{spec.signal}': missing={sorted(expected - set(canon))} "
                f"extra={sorted(set(canon) - expected)}")
        labels[spec.head] = canon
    return CheckpointFiles(
        checkpoint_dir=str(d), base_model=str(cfg["base_model"]),
        max_length=int(cfg.get("max_length") or 512),
        embedding_dim=int(cfg.get("embedding_dim") or 768), labels=labels)


# --------------------------------------------------------------------------- #
# Pure numerics (shared with the other taggers — see router/signals/_math.py)
# --------------------------------------------------------------------------- #
def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------- #
# Tagger
# --------------------------------------------------------------------------- #
class CheckpointTagger:
    """RoutePilot checkpoint tagger (Tagger protocol, name="checkpoint")."""

    name = "checkpoint"

    def __init__(self, checkpoint_dir: str | Path, *, device: str | None = None,
                 model_factory=None, kw_groups: dict | None = None,
                 warmup_samples: list[str] | None = None):
        files = load_and_validate(checkpoint_dir)      # pure check before torch
        self.files = files
        self.kw_groups = kw_groups or {}
        self.time_periods = {}               # replaced wholesale on rule update
        import torch                                    # lazy: checkpoint only

        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if model_factory is not None:
            self.model = model_factory(files.checkpoint_dir, self.device)
            self.model_binding = "injected"
        else:
            build, self.model_binding = self._default_model_factory()
            self.model = build(files.checkpoint_dir, self.device)
        logger.info("checkpoint tagger bound to %s", self.model_binding)
        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32
        self.model.to(device=self.device, dtype=dtype)
        self.model.eval()
        self.tokenizer = getattr(self.model, "tokenizer", None)
        if self.tokenizer is None:
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(files.base_model)
        try:
            self.tokenizer.padding_side = "right"
        except Exception:                               # noqa: BLE001
            pass
        self.max_length = files.max_length
        self._warmup(warmup_samples or ["hello", "你好", "1+1=?", "写段代码"])

    @staticmethod
    def _default_model_factory():
        cls, qualified = import_train_model_class()

        def build(checkpoint_dir, device):
            return cls.load_checkpoint(checkpoint_dir, device=device)
        build.model_class = cls                           # for tests/diagnostics
        build.qualified_name = qualified
        return build, qualified

    # ── forward ────────────────────────────────────────────────────────────
    def _forward(self, texts: list[str]) -> dict:
        torch = self._torch
        enc = self.tokenizer(texts, padding=True, truncation=True,
                             max_length=self.max_length, return_tensors="pt")
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with torch.no_grad():
            out = self.model.forward(enc["input_ids"],
                                     attention_mask=enc.get("attention_mask"),
                                     with_embedding=True)
        if not isinstance(out, dict):
            raise CheckpointError("checkpoint forward must return a dict")
        missing = [k for k in HEAD_KEYS if k not in out]
        if missing:
            raise CheckpointError(
                f"checkpoint forward missing heads {missing}; expected the flat "
                "dict from RoutePilotModel.forward")
        return {k: out[k].detach().float().cpu() for k in HEAD_KEYS}

    def _warmup(self, samples: list[str]) -> None:
        out = self._forward(samples)
        for spec in HEADS:
            if spec.kind == "embedding":
                dim = out["embedding"].shape[1]
                if dim != self.files.embedding_dim:
                    raise CheckpointError(
                        f"embedding dim {dim} != configured {self.files.embedding_dim}")
            else:
                n = out[spec.head].shape[1]
                if n != len(self.files.labels[spec.head]):
                    raise CheckpointError(
                        f"head {spec.head} width {n} != label count "
                        f"{len(self.files.labels[spec.head])}")

    def _model_signals(self, req: TagRequest, out_row: dict) -> tuple[dict, list[float]]:
        signals: dict = {}
        for spec in HEADS:
            if spec.kind == "embedding":
                continue
            row = out_row[spec.head]
            if spec.kind == "classification":
                probs = softmax(list(row))
                idx = max(range(len(probs)), key=lambda i: probs[i])
                signals[spec.signal] = {
                    "value": self.files.labels[spec.head][idx],
                    "confidence": entropy_confidence(probs), "is_rule": False}
            else:  # multilabel (pii): per-type binary entropy confidence
                labels = self.files.labels[spec.head]
                probs = [sigmoid(float(x)) for x in row]
                # the same hit rule as the other taggers (router/signals/_math.py)
                hits, conf_by_value = pii_hits(probs, labels)
                signals[spec.signal] = {
                    "value": hits,
                    "confidence": max(conf_by_value.values()) if conf_by_value else None,
                    "conf_by_value": conf_by_value, "is_rule": False}
        # user_feedback only means something when there is a previous turn
        if req.n_messages <= 1:
            signals["user_feedback"] = {"value": None, "confidence": None,
                                        "is_rule": False}
        vec = l2_normalize([float(x) for x in out_row["embedding"]])
        return signals, vec

    async def tag(self, req: TagRequest) -> TagResult:
        signals, vec = await asyncio.to_thread(self._tag_row, req)
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups, time_periods=self.time_periods))
        merge_rule_pii(signals, req.text)
        return TagResult(signals=signals, vec=vec)

    def _tag_row(self, req: TagRequest) -> tuple[dict, list[float]]:
        out = self._forward([req.text])
        row = {k: [float(x) for x in out[k][0]] for k in HEAD_KEYS}
        return self._model_signals(req, row)
