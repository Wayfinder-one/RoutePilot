"""M2 pure-part tests: label mapping, confidence, clamp, conf_by_value.

These run without torch. The torch-backed forward pass is exercised on the
training host (tests/test_checkpoint_torch.py, skipped when torch is absent).
The ``tags.json`` used as the fixture is the byte copy of the file the trained
checkpoint serves on 76 (``/root/smart-router-train/checkpoints/quick/tags.json``,
fetched 2026-10-17) committed as ``tests/fixtures/checkpoint_tags.json``. Its
producer is ``scripts/export_checkpoint_tags.py`` (second-round audit F2), which
regenerates it from the training repo's label spaces with the same canonical
spellings.
"""

import json
from pathlib import Path

import pytest

from router.pipeline.matching import match_row
from router.signals.checkpoint import (CheckpointError, binary_entropy_confidence,
                                       canonical_label, entropy_confidence,
                                       l2_normalize, load_and_validate, sigmoid,
                                       softmax)

FIXTURE_TAGS = Path(__file__).parent / "fixtures" / "checkpoint_tags.json"


def training_tags() -> dict:
    """tags.json as shipped with the trained checkpoint (76), committed here."""
    return json.loads(FIXTURE_TAGS.read_text(encoding="utf-8"))


def make_checkpoint(tmp_path, tags=None) -> str:
    d = tmp_path / "ckpt"
    d.mkdir()
    (d / "router_config.json").write_text(
        '{"base_model": "models/x", "max_length": 256, "embedding_dim": 768}')
    (d / "model.pt").write_bytes(b"")
    (d / "tags.json").write_text(json.dumps(tags or training_tags()))
    return str(d)



def test_canonical_label_maps_training_spellings():
    assert canonical_label("domain", "computer science") == "computer_science"
    assert canonical_label("domain", "health") == "health"
    assert canonical_label("pii", "CN_ID_CARD") == "cn_id_card"
    assert canonical_label("pii", "US_SSN") == "us_ssn"
    assert canonical_label("user_feedback", "SAT") == "satisfied"
    assert canonical_label("user_feedback", "WRONG_ANSWER") == "wrong_answer"
    assert canonical_label("modality", "AR") == "text"
    assert canonical_label("modality", "DIFFUSION") == "image"
    assert canonical_label("modality", "BOTH") == "text_image"
    assert canonical_label("fact_check", "FACT_CHECK_NEEDED") == "needs_check"
    assert canonical_label("fact_check", "NO_FACT_CHECK_NEEDED") == "no_check"
    assert canonical_label("jailbreak", "1") == "jailbreak"
    assert canonical_label("jailbreak", "0") == "benign"
    assert canonical_label("complexity", "hard") == "hard"


def test_softmax_and_entropy_confidence():
    assert abs(sum(softmax([1.0, 2.0, 3.0])) - 1.0) < 1e-9
    # a point mass is fully confident
    assert entropy_confidence([1.0, 0.0]) == pytest.approx(1.0)
    # a uniform 2-way distribution is 0
    assert entropy_confidence([0.5, 0.5]) == pytest.approx(0.0)
    # uniform 4-way is also 0 (K is accounted for)
    assert entropy_confidence([0.25] * 4) == pytest.approx(0.0)
    # single label => 1.0 by convention
    assert entropy_confidence([1.0]) == 1.0


def test_binary_entropy_confidence():
    assert binary_entropy_confidence(1.0) == pytest.approx(1.0)
    assert binary_entropy_confidence(0.0) == pytest.approx(1.0)
    assert binary_entropy_confidence(0.5) == pytest.approx(0.0)
    assert 0.0 < binary_entropy_confidence(0.9) < 1.0


def test_l2_normalize():
    v = l2_normalize([3.0, 4.0])
    assert v == pytest.approx([0.6, 0.8])


def test_match_row_prefers_conf_by_value_for_multi():
    signals = {"pii": {"value": ["cn_id_card", "password"], "confidence": 0.4,
                       "conf_by_value": {"cn_id_card": 0.9}, "is_rule": False}}
    assert match_row("pii", "cn_id_card", signals) == (1, 0.9)
    # no per-value entry -> falls back to the scalar confidence
    assert match_row("pii", "password", signals) == (1, 0.4)
    # non-match keeps the scalar confidence
    assert match_row("pii", "email_address", signals) == (0, 0.4)


def test_load_and_validate_accepts_training_tags(tmp_path):
    files = load_and_validate(make_checkpoint(tmp_path))
    assert files.labels["domain"][3] == "computer_science"
    assert files.labels["fact_check"] == ["no_check", "needs_check"]
    assert files.labels["modality"] == ["text", "image", "text_image"]
    assert files.labels["user_feedback"][0] == "satisfied"
    assert len(files.labels["pii"]) == 30
    assert "cn_id_card" in files.labels["pii"]
    assert files.embedding_dim == 768


def test_load_and_validate_rejects_mismatched_labels(tmp_path):
    tags = training_tags()
    tags["tags"]["domain"]["labels"] = tags["tags"]["domain"]["labels"][:-1]
    with pytest.raises(CheckpointError):
        load_and_validate(make_checkpoint(tmp_path, tags))


def test_load_and_validate_rejects_missing_file(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(CheckpointError):
        load_and_validate(str(d))


def test_load_and_validate_rejects_head_set_mismatch(tmp_path):
    tags = training_tags()
    del tags["tags"]["modality"]
    with pytest.raises(CheckpointError):
        load_and_validate(make_checkpoint(tmp_path, tags))

