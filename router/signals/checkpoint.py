"""CheckpointTagger (M2): the RoutePilot checkpoint as a Tagger.

One forward pass of ``SmartRouterModel`` (smart-router-train) yields every model
signal plus a 768-d embedding. Three things this module exists for:

1. **Label mapping layer** (design doc section 2.5). The registry value keys are
   product-canonical; the checkpoint's ``tags.json`` spellings differ
   (``computer science`` -> ``computer_science``, ``FACT_CHECK_NEEDED`` ->
   ``needs_check``, ``SAT`` -> ``satisfied``, pii UPPER -> lower). Without this
   layer every model-signal row misses and traffic silently falls through to the
   fallback. We reuse the training repo's ``normalize_label`` when importable.
2. **Confidence** ``c = 1 - H(p) / log K`` for classification heads; per-type
   binary entropy for pii, written to ``conf_by_value`` (design section 2.2/2.3).
3. **Embedding** -> ``similarity`` (3 bands) / ``reask`` against ``last_vec``,
   cosine clamped to [0,1] so the score bound in section 3.3 holds.

torch / transformers and the training package are imported lazily: validation
and all pure helpers work (and are unit-tested) without a GPU or torch.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from router.signals.registry import SIGNALS
from router.signals.rules import rule_signals
from router.signals.tagger import TagRequest, TagResult


class CheckpointError(Exception):
    """Checkpoint validation/loading failure — the app refuses to start on it."""


# --------------------------------------------------------------------------- #
# Head -> product-signal mapping (the checkpoint's flat forward contract).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeadSpec:
    head: str            # tags.json key == SmartRouterModel head name
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

# Training-repo alias table (mirrors smart_router.signals.LABEL_ALIASES). Kept
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


def _train_normalize_label(head: str, label) -> str:
    """Reuse the training repo's alias table when importable (design 2.5),
    then the local mirror (identical spellings) as the portable fallback."""
    text = str(label)
    try:
        from smart_router.signals import normalize_label  # type: ignore
        text = str(normalize_label(head, text))
    except Exception:                                     # noqa: BLE001
        pass
    return _TRAIN_ALIASES.get(head, {}).get(text, text)



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
# Pure numerics
# --------------------------------------------------------------------------- #
def softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def entropy_confidence(probs: list[float]) -> float:
    """1 - H(p) / log K, clamped to [0,1] (K <= 1 => 1.0)."""
    k = len(probs)
    if k <= 1:
        return 1.0
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def binary_entropy_confidence(p: float) -> float:
    """1 - H2(p) / log 2 for a per-type presence probability."""
    p = max(0.0, min(1.0, p))
    if p <= 0.0 or p >= 1.0:
        return 1.0
    h2 = -(p * math.log(p) + (1 - p) * math.log(1 - p)) / math.log(2)
    return max(0.0, min(1.0, 1.0 - h2))


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def similarity_signals(vec: list[float], last_vec: list[float] | None
                       ) -> tuple[dict, dict]:
    """Embedding cosine -> similarity (0/1/2) and reask (yes/no).

    Cosine is clamped to [0,1] (design 2.3): a negative cosine would produce a
    negative confidence and break the score bound in 3.3.
    """
    if not last_vec:
        return ({"value": 0, "confidence": None, "is_rule": False},
                {"value": "no", "confidence": None, "is_rule": False})
    dim = min(len(vec), len(last_vec))
    cos = sum(vec[i] * last_vec[i] for i in range(dim))
    cos = max(0.0, min(1.0, cos))
    band = 0 if cos < 0.55 else (1 if cos < 0.85 else 2)
    return ({"value": band, "confidence": cos, "is_rule": False},
            {"value": "yes" if cos >= 0.85 else "no", "confidence": cos,
             "is_rule": False})


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
        import torch                                    # lazy: checkpoint only

        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if model_factory is not None:
            self.model = model_factory(files.checkpoint_dir, self.device)
        else:
            self.model = self._default_model_factory()(files.checkpoint_dir,
                                                       self.device)
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
        try:
            from smart_router.modeling.router import SmartRouterModel
        except Exception as exc:                        # noqa: BLE001
            raise CheckpointError(
                "cannot import smart_router.modeling.router.SmartRouterModel "
                "(checkpoint mode needs the training package on PYTHONPATH and "
                "torch/transformers installed)") from exc

        def build(checkpoint_dir, device):
            return SmartRouterModel.load_checkpoint(checkpoint_dir, device=device)
        return build

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
                "dict from SmartRouterModel.forward")
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
                hits = [labels[i] for i, p in enumerate(probs) if p >= 0.5]
                conf_by_value = {labels[i]: binary_entropy_confidence(probs[i])
                                 for i in range(len(labels)) if probs[i] >= 0.5}
                signals[spec.signal] = {
                    "value": hits,
                    "confidence": max(conf_by_value.values()) if conf_by_value else None,
                    "conf_by_value": conf_by_value, "is_rule": False}
        # user_feedback only means something when there is a previous turn
        if req.n_messages <= 1:
            signals["user_feedback"] = {"value": None, "confidence": None,
                                        "is_rule": False}
        vec = l2_normalize([float(x) for x in out_row["embedding"]])
        sim, reask = similarity_signals(vec, req.last_vec)
        signals["similarity"], signals["reask"] = sim, reask
        return signals, vec

    async def tag(self, req: TagRequest) -> TagResult:
        signals, vec = await asyncio.to_thread(self._tag_row, req)
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                    self.kw_groups))
        return TagResult(signals=signals, vec=vec)

    def _tag_row(self, req: TagRequest) -> tuple[dict, list[float]]:
        out = self._forward([req.text])
        row = {k: [float(x) for x in out[k][0]] for k in HEAD_KEYS}
        return self._model_signals(req, row)
