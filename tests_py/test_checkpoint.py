"""M2：CheckpointTagger 的 §2.3 校验红线 + 头出处映射（不需 torch/GPU 的部分）。

真·torch 前向（CUDA/bf16/微批/嵌入）在 tests_py/test_checkpoint_gpu.py（仅在装了
torch 的机器 / GPU 上跑，缺 torch 自动 skip）。
"""

import json

import pytest

from sr.checkpoint import (DEFAULT_BAND_THRESHOLDS, HEAD_SPECS, CheckpointError,
                           _band_index, _conf_of, _l2_normalize, _similarity_signals,
                           load_and_validate, load_router_config)


def _write_ckpt(tmp_path, *, router=None, tags=None, model=True):
    d = tmp_path / "ckpt"
    d.mkdir()
    (d / "router_config.json").write_text(json.dumps(
        router if router is not None else
        {"base_model": "distilbert-base-multilingual-cased", "max_length": 512,
         "embedding_dim": 768, "hidden_size": 768}), encoding="utf-8")
    if model:
        (d / "model.pt").write_bytes(b"dummy-not-a-real-pt")   # 校验不 load 权重
    if tags is None:
        tags = {"schema": 1, "max_length": 512, "tags": {
            s.json_key: ({"type": "projection", "dim": 768} if s.head == "embedding"
                         else {"type": s.product_kind, "labels": list(s.ckpt_labels)})
            for s in HEAD_SPECS}}
    if tags is not False:
        (d / "tags.json").write_text(json.dumps(tags), encoding="utf-8")
    return d


def test_valid_checkpoint_passes(tmp_path):
    files = load_and_validate(_write_ckpt(tmp_path))
    assert files.config.base_model.endswith("multilingual-cased")
    assert files.config.max_length == 512 and files.config.embedding_dim == 768
    assert files.head_specs == HEAD_SPECS


def test_missing_router_config_rejected(tmp_path):
    d = _write_ckpt(tmp_path)
    (d / "router_config.json").unlink()
    with pytest.raises(CheckpointError, match="router_config.json"):
        load_and_validate(d)


def test_missing_model_pt_rejected(tmp_path):
    with pytest.raises(CheckpointError, match="model.pt"):
        load_and_validate(_write_ckpt(tmp_path, model=False))


def test_missing_tags_json_rejected(tmp_path):
    with pytest.raises(CheckpointError, match="tags.json"):
        load_and_validate(_write_ckpt(tmp_path, tags=False))


def test_tags_json_label_order_mismatch_rejected(tmp_path):
    """fact_check 的 labels 顺序反了 → 拒绝启动（M2 前置 1 的正是这一处：
    head 是 Linear(hidden, n_labels)，顺序错则静默全错）。"""
    tags = {"schema": 1, "tags": {
        s.json_key: ({"type": "projection", "dim": 768} if s.head == "embedding"
                     else {"type": s.product_kind, "labels": list(s.ckpt_labels)})
        for s in HEAD_SPECS}}
    tags["tags"]["fact_check"]["labels"] = ["needs_check", "no_check"]   # 反序
    with pytest.raises(CheckpointError, match="fact_check"):
        load_and_validate(_write_ckpt(tmp_path, tags=tags))


def test_tags_json_label_count_mismatch_rejected(tmp_path):
    tags = {"schema": 1, "tags": {
        s.json_key: ({"type": "projection", "dim": 768} if s.head == "embedding"
                     else {"type": s.product_kind, "labels": list(s.ckpt_labels)})
        for s in HEAD_SPECS}}
    tags["tags"]["domain"]["labels"] = tags["tags"]["domain"]["labels"][:-1]  # 少一个
    with pytest.raises(CheckpointError, match="domain"):
        load_and_validate(_write_ckpt(tmp_path, tags=tags))


def test_tags_json_unknown_head_rejected(tmp_path):
    tags = {"schema": 1, "tags": {
        s.json_key: ({"type": "projection", "dim": 768} if s.head == "embedding"
                     else {"type": s.product_kind, "labels": list(s.ckpt_labels)})
        for s in HEAD_SPECS}}
    tags["tags"]["mystery_head"] = {"type": "noul", "labels": ["a", "b"]}
    with pytest.raises(CheckpointError, match="头清单"):
        load_and_validate(_write_ckpt(tmp_path, tags=tags))


def test_router_config_requires_base_model(tmp_path):
    d = _write_ckpt(tmp_path, router={"max_length": 128})
    with pytest.raises(CheckpointError, match="base_model"):
        load_and_validate(d)


# ── 纯函数：置信度 / 档位 / 嵌入比对 ──────────────────────────────────────────

def test_confidence_bounds():
    assert _conf_of([1.0]) == 1.0                     # K=1 → 1.0
    assert _conf_of([0.5, 0.5]) == pytest.approx(0.0)  # 均匀分布 → 0
    assert 0.0 < _conf_of([0.9, 0.1]) < 1.0


def test_band_index_matches_conf_band_semantics():
    cuts = [0.25, 0.5, 0.75]
    assert _band_index(0.1, cuts) == 0
    assert _band_index(0.5, cuts) == 2               # 含等号
    assert _band_index(0.9, cuts) == 3
    assert DEFAULT_BAND_THRESHOLDS["jailbreak"] == cuts


def test_canonical_label_and_build_tags_json():
    """M2：从训练侧标签注册表生成 tags.json（规范键 + 与 HEAD_SPECS 一致）。"""
    from sr.checkpoint import (_validate_tags_json, build_tags_json,
                               canonical_label)
    from sr.tags import PII_KEYS
    assert canonical_label("domain", "computer science") == "computer_science"
    assert canonical_label("fact_check", "FACT_CHECK_NEEDED") == "needs_check"
    assert canonical_label("fact_check", "no_fact_check_needed") == "no_check"
    assert canonical_label("modality", "AR") == "text"
    assert canonical_label("user_feedback", "SAT") == "satisfied"
    assert canonical_label("pii", "CN_ID_CARD") == "cn_id_card"
    assert canonical_label("complexity", "easy") == "easy"

    labels = {
        "domain": ("biology", "business", "chemistry", "computer science",
                   "economics", "engineering", "health", "history", "law", "math",
                   "other", "philosophy", "physics", "psychology"),
        "jailbreak": ("benign", "jailbreak"),
        "fact_check": ("no_fact_check_needed", "fact_check_needed"),
        "user_feedback": ("SAT", "NEED_CLARIFICATION", "WRONG_ANSWER", "WANT_DIFFERENT"),
        "modality": ("AR", "DIFFUSION", "BOTH"),
        "complexity": ("easy", "hard"),
        "pii": tuple(x.upper() for x in PII_KEYS),
    }
    tj = build_tags_json(labels, embedding_dim=768)
    _validate_tags_json(tj)                     # 不抛 = 与 HEAD_SPECS 逐项一致
    assert tj["tags"]["fact_check"]["labels"] == ["no_check", "needs_check"]


def test_embedding_similarity_and_reask():
    vec = _l2_normalize([3.0, 4.0])
    assert vec == pytest.approx([0.6, 0.8])
    same = _similarity_signals(vec, vec)             # cos=1 → 档 2 / yes
    assert same[0]["value"] == 2 and same[1]["value"] == "yes"
    none = _similarity_signals(vec, None)            # 无上一轮 → 档 0 / no
    assert none[0]["value"] == 0 and none[1]["value"] == "no"
    orth = _similarity_signals([1.0, 0.0], [0.0, 1.0])   # cos=0 → 档 0 / no
    assert orth[0]["value"] == 0 and orth[1]["value"] == "no"
