"""F3 guard: the *default* checkpoint loading path, with real weights.

The audit found the torch tests all inject ``model_factory`` (ToyModel), so
the actual ``RoutePilotModel.load_checkpoint`` + real-weights path was never
exercised — which is exactly how the F1 rename bug survived. This module runs
only where a real checkpoint and torch exist (i.e. on 76):

    PYTHONPATH=/opt/smart-router-v2:/root/smart-router-train/src \
    ROUTER_TEST_CHECKPOINT=/root/smart-router-train/checkpoints/quick \
    /root/jev-2b/venv/bin/python -m pytest tests/test_checkpoint_real.py -q

It deliberately does **not** pass ``model_factory``: it must fail if the
training package binding is wrong.
"""

import asyncio
import os

import pytest

CKPT = os.environ.get("ROUTER_TEST_CHECKPOINT", "")

pytestmark = pytest.mark.skipif(
    not CKPT or not os.path.isdir(CKPT),
    reason="set ROUTER_TEST_CHECKPOINT to a real checkpoint dir (run on the GPU host)")


def _device() -> str:
    torch = pytest.importorskip("torch")
    return os.environ.get("ROUTER_TEST_DEVICE") or (
        "cuda" if torch.cuda.is_available() else "cpu")


def test_real_checkpoint_binds_routepilot_and_loads_weights():
    pytest.importorskip("torch")
    pytest.importorskip("routepilot.modeling.router")
    from router.signals.checkpoint import CheckpointTagger

    tagger = CheckpointTagger(CKPT, device=_device())

    # F1: the default factory must bind the renamed training repo
    assert tagger.model_binding.startswith("routepilot.modeling.router."), \
        f"bound to {tagger.model_binding}"
    assert type(tagger.model).__name__ == "RoutePilotModel"


def test_real_checkpoint_tags_all_signals_with_canonical_values():
    pytest.importorskip("torch")
    from router.signals.checkpoint import CheckpointTagger, load_and_validate
    from router.signals.registry import SIGNALS
    from router.signals.tagger import TagRequest

    files = load_and_validate(CKPT)
    tagger = CheckpointTagger(CKPT, device=_device())
    result = asyncio.run(tagger.tag(TagRequest(text="用 Python 写一个快速排序算法")))

    signals = result.signals
    assert set(signals) == set(SIGNALS)                    # 15 ordinary signals
    assert signals["domain"]["is_rule"] is False
    domain_values = {v[0] for v in SIGNALS["domain"].values}
    assert signals["domain"]["value"] in domain_values
    assert signals["jailbreak"]["value"] in {v[0] for v in SIGNALS["jailbreak"].values}
    assert signals["fact_check"]["value"] in {v[0] for v in SIGNALS["fact_check"].values}
    assert signals["modality"]["value"] in {v[0] for v in SIGNALS["modality"].values}
    assert signals["complexity"]["value"] in {v[0] for v in SIGNALS["complexity"].values}
    assert result.vec is not None
    assert len(result.vec) == files.embedding_dim
    assert abs(sum(x * x for x in result.vec) - 1.0) < 1e-3  # L2-normalised
