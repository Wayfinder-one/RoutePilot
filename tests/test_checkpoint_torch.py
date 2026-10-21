"""M2 torch-backed tests for CheckpointTagger.

Runs on the training host (torch + the synthetic-model injection path). Skipped
where torch is unavailable; the label/confidence/clamp logic is covered by
tests/test_checkpoint_pure.py without torch.
"""

import asyncio
import json

import pytest

torch = pytest.importorskip("torch")
from torch import nn                                            # noqa: E402

from router.signals.checkpoint import CheckpointTagger          # noqa: E402
from router.signals.registry import SIGNALS                     # noqa: E402
from router.signals.tagger import TagRequest                    # noqa: E402
from tests.test_checkpoint_pure import make_checkpoint, training_tags  # noqa: E402


class ToyTokenizer:
    padding_side = "left"

    def __call__(self, texts, padding=True, truncation=True, max_length=8,
                 return_tensors="pt"):
        rows = []
        for t in texts:
            row = [min(ord(c) % 50 + 1, 50) for c in t[:max_length]] or [0]
            rows.append(row)
        width = max(len(r) for r in rows)
        ids = torch.tensor([r + [0] * (width - len(r)) for r in rows])
        return {"input_ids": ids, "attention_mask": (ids != 0).long()}


class ToyModel(nn.Module):
    def __init__(self, labels, pii_hits=()):
        super().__init__()
        self.tokenizer = ToyTokenizer()
        self.labels = labels
        self.pii_hits = set(pii_hits)
        self.dummy = nn.Linear(4, 4)

    def forward(self, input_ids, attention_mask, *, with_embedding=False):
        batch = input_ids.shape[0]
        out = {}
        for head, labels in self.labels.items():
            if head == "pii":
                logits = torch.full((batch, len(labels)), -5.0)
                for i in self.pii_hits:
                    logits[:, i] = 5.0
            else:
                logits = torch.zeros(batch, len(labels))
                logits[:, 0] = 5.0                      # confident on label 0
            out[head] = logits
        out["embedding"] = nn.functional.normalize(torch.ones(batch, 768), dim=-1)
        return out


def build(tmp_path, pii_hits=()):
    ckpt = make_checkpoint(tmp_path)
    labels = json.loads((__import__("pathlib").Path(ckpt) / "tags.json")
                        .read_text())["tags"]
    from router.signals.checkpoint import load_and_validate
    files = load_and_validate(ckpt)
    model = lambda c, d: ToyModel(files.labels, pii_hits)   # noqa: E731
    tagger = CheckpointTagger(ckpt, device="cpu", model_factory=model)
    return tagger, files


def tag(tagger, req):
    return asyncio.run(tagger.tag(req)).signals


def test_full_pack_and_canonical_values(tmp_path):
    tagger, _ = build(tmp_path)
    s = tag(tagger, TagRequest(text="hello"))
    assert set(s) == set(SIGNALS) - {"similarity", "reask"}  # session signals removed
    assert s["domain"]["value"] == "biology"
    assert s["complexity"]["value"] == "easy"
    assert s["jailbreak"]["value"] == "benign"
    assert s["fact_check"]["value"] == "no_check"
    assert s["modality"]["value"] == "text"
    assert s["pii"]["value"] == []
    assert s["user_feedback"]["value"] is None            # single turn
    assert s["domain"]["confidence"] > 0.5                # a real, uncapped conf
    assert s["language"]["is_rule"] is True


def test_pii_conf_by_value(tmp_path):
    ckpt = make_checkpoint(tmp_path)
    from router.signals.checkpoint import load_and_validate
    files = load_and_validate(ckpt)
    idx = files.labels["pii"].index("cn_id_card")
    tagger = CheckpointTagger(ckpt, device="cpu",
                              model_factory=lambda c, d: ToyModel(files.labels, [idx]))
    s = tag(tagger, TagRequest(text="id 110101199003077758"))
    assert s["pii"]["value"] == ["cn_id_card"]
    assert s["pii"]["conf_by_value"]["cn_id_card"] > 0.9
    assert s["pii"]["confidence"] == s["pii"]["conf_by_value"]["cn_id_card"]


def test_validation_blocks_mismatched_checkpoint(tmp_path):
    tags = training_tags()
    tags["tags"]["modality"]["labels"] = ["AR", "DIFFUSION"]   # missing BOTH
    ckpt = make_checkpoint(tmp_path, tags)
    from router.signals.checkpoint import CheckpointError, load_and_validate
    with pytest.raises(CheckpointError):
        load_and_validate(ckpt)
