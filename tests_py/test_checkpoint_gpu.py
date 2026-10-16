"""M2：CheckpointTagger 的 torch 前向端到端（合成模型，验证 CUDA/bf16/微批/嵌入）。

不需真实检查点——训练侧模型类不在本仓库，这里用一个**同契约**的合成模型
（``forward(input_ids, attention_mask, with_embedding=True)`` 返回**扁平** dict
``{head_name: logits, "embedding": vec}``，与 `SmartRouterModel.forward` 一致；
B1 起代码只认这一种形状）经 ``model_factory`` 注入，从而验证 CheckpointTagger 的
加载（§2.3）/前向（§4.2）/嵌入比对全链路。

运行：
- 无 torch → 自动 skip（本机开发机）；
- 有 torch（CPU）：``python -m pytest tests_py/test_checkpoint_gpu.py``；
- GPU：``SR_TEST_DEVICE=cuda python tests_py/test_checkpoint_gpu.py``（或 pytest）。

可作脚本直跑（remote 无 pytest 时）：``python tests_py/test_checkpoint_gpu.py``。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")  # 无 torch → skip

from sr.checkpoint import HEAD_SPECS, CheckpointError, CheckpointTagger  # noqa: E402
from sr.tagger import TagRequest  # noqa: E402

DEVICE = os.environ.get("SR_TEST_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
_VOCAB, _HIDDEN = 100, 16


def _labels(spec):
    return list(spec.ckpt_labels)


class TinyTokenizer:
    """同 AutoTokenizer 调用契约的极简分词器（字符 id + 右 padding）。"""

    def __call__(self, texts, padding=True, truncation=True, max_length=512,
                 return_tensors="pt"):
        if isinstance(texts, str):
            texts = [texts]
        rows = [[(ord(c) % (_VOCAB - 1)) + 1 for c in t][:max_length] or [1]
                for t in texts]
        width = max(len(r) for r in rows)
        ids = [r + [0] * (width - len(r)) for r in rows]
        mask = [[1] * len(r) + [0] * (width - len(r)) for r in rows]
        return {"input_ids": torch.tensor(ids, dtype=torch.long),
                "attention_mask": torch.tensor(mask, dtype=torch.long)}


class TinyRoutePilot(torch.nn.Module):
    """同契约的合成 RoutePilot：词嵌入 mean-pool → 7 个分类头 + pii 多标签 + 嵌入头。"""

    def __init__(self, cfg):
        super().__init__()
        self.emb = torch.nn.Embedding(_VOCAB, _HIDDEN)
        self.cls = torch.nn.ModuleDict({
            s.json_key: torch.nn.Linear(_HIDDEN, len(s.ckpt_labels))
            for s in HEAD_SPECS if s.head == "classification"})
        self.pii = torch.nn.Linear(_HIDDEN, len(_labels(
            next(s for s in HEAD_SPECS if s.json_key == "pii"))))
        self.emb_head = torch.nn.Linear(_HIDDEN, cfg.embedding_dim)

    def forward(self, input_ids, attention_mask=None, with_embedding=True):
        # B1（audit）：合成模型必须复现**真实训练侧契约**——`SmartRouterModel.forward`
        # 返回扁平 dict {head_name: logits, ..., "embedding": vec}。旧测试返回
        # {classification_heads, multilabel_heads, embedding_head} 的嵌套形状，于是
        # 只有「双形状回退」那一版代码能过，测试绿而生产契约未被验证。
        base = self.emb(input_ids).mean(dim=1)
        out = {k: v(base) for k, v in self.cls.items()}
        out["pii"] = self.pii(base)
        if with_embedding:
            out["embedding"] = torch.nn.functional.normalize(self.emb_head(base), dim=-1)
        return out


def _make_checkpoint(tmp_path, *, device=DEVICE):
    d = Path(tmp_path) / "ckpt"
    d.mkdir(parents=True)
    cfg = {"base_model": "tiny", "max_length": 32, "embedding_dim": 768,
           "hidden_size": _HIDDEN}
    (d / "router_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (d / "tags.json").write_text(json.dumps({"schema": 1, "tags": {
        s.json_key: ({"type": "projection", "dim": 768} if s.head == "embedding"
                     else {"type": s.product_kind, "labels": _labels(s)})
        for s in HEAD_SPECS}}), encoding="utf-8")
    model = TinyRoutePilot(type("C", (), cfg))   # 结构无关，仅取 embedding_dim
    torch.save(model.state_dict(), d / "model.pt")
    return str(d)


def _tagger(tmp_path, device=DEVICE):
    return CheckpointTagger(_make_checkpoint(tmp_path), device=device,
                            model_factory=TinyRoutePilot,
                            tokenizer_factory=lambda cfg: TinyTokenizer())


def test_forward_produces_nine_model_tags_and_rules(tmp_path):
    tagger = _tagger(tmp_path)
    req = TagRequest(text="设计一个高并发订单系统", headers={"x-sr-tenant": "acme"},
                     n_messages=2, now=__import__("datetime").datetime(2026, 10, 16, 10),
                     last_msg=None)
    res = asyncio.get_event_loop().run_until_complete(tagger.tag(req))
    sig = res.signals
    for spec in HEAD_SPECS:
        if spec.product_tag:
            assert spec.product_tag in sig, spec.product_tag
    # 规则标签同在（§4.2-6 同一套代码）
    for k in ("language", "structure", "context", "turns", "tenant", "time_window"):
        assert k in sig, k
    assert sig["tenant"]["value"] == "acme"
    # 嵌入向量 768-d 且已 L2 归一
    assert res.vec is not None and len(res.vec) == 768
    norm = sum(v * v for v in res.vec) ** 0.5
    assert abs(norm - 1.0) < 1e-3, norm
    # complexity 产品形是档位 int（ordered）
    assert sig["complexity"]["value"] in (0, 1)
    # pii 是多标签列表
    assert isinstance(sig["pii"]["value"], list)


def test_similarity_and_reask_use_last_vec(tmp_path):
    tagger = _tagger(tmp_path)
    first = asyncio.get_event_loop().run_until_complete(tagger.tag(TagRequest(
        text="怎么配置 nginx", headers={}, n_messages=1,
        now=__import__("datetime").datetime(2026, 10, 16, 10), last_msg=None)))
    # 用同一向量当上一轮 → 相似度满档、reask=yes
    res = asyncio.get_event_loop().run_until_complete(tagger.tag(TagRequest(
        text="怎么配置 nginx", headers={}, n_messages=2,
        now=__import__("datetime").datetime(2026, 10, 16, 10),
        last_msg="怎么配置 nginx", last_vec=first.vec)))
    assert res.signals["similarity"]["value"] == 2
    assert res.signals["reask"]["value"] == "yes"


def test_device_and_dtype_selection(tmp_path):
    tagger = _tagger(tmp_path, device=DEVICE)
    assert tagger.device == DEVICE
    if DEVICE.startswith("cuda"):
        assert tagger.dtype == torch.bfloat16
        assert torch.cuda.is_available()
    else:
        assert tagger.dtype == torch.float32


def test_state_dict_mismatch_rejected(tmp_path):
    """权重与模型不匹配（缺失/多余键非空）→ CheckpointError（§2.3-3）。"""
    d = _make_checkpoint(tmp_path)
    torch.save({"bogus.weight": torch.zeros(3)}, Path(d) / "model.pt")
    with pytest.raises(CheckpointError, match="state_dict"):
        CheckpointTagger(d, device="cpu", model_factory=TinyRoutePilot,
                         tokenizer_factory=lambda cfg: TinyTokenizer())


# ── 脚本直跑（remote 无 pytest：python tests_py/test_checkpoint_gpu.py） ──────────

def _run_script():
    import tempfile
    checks = [test_forward_produces_nine_model_tags_and_rules,
              test_similarity_and_reask_use_last_vec,
              test_device_and_dtype_selection,
              test_state_dict_mismatch_rejected]
    ok = 0
    for fn in checks:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(td)
                print(f"  ✓ {fn.__name__}")
                ok += 1
            except Exception as exc:                       # noqa: BLE001
                print(f"  ✗ {fn.__name__}: {exc!r}")
    print(f"\n{ok}/{len(checks)} 通过 · device={DEVICE} · torch={torch.__version__}")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(_run_script())


def test_nested_shape_is_rejected_with_clear_error(tmp_path):
    """B1：非真实契约（嵌套形状）必须**显式报错**，不再静默降级。"""
    class Nested(TinyRoutePilot):
        # 参数与 TinyRoutePilot 完全一致（state_dict 能加载），只把 **forward 返回值**
        # 换成旧的嵌套假形状——这样失败点必然落在「形状契约」而不是加载期。
        def forward(self, input_ids, attention_mask=None, with_embedding=True):
            base = self.emb(input_ids).mean(dim=1)
            return {"classification_heads": {k: v(base) for k, v in self.cls.items()},
                    "multilabel_heads": {"pii": self.pii(base)},
                    "embedding_head": self.emb_head(base)}     # 旧假形状

    d = _make_checkpoint(tmp_path)
    # fail fast：契约不符必须在**构造期（预热前向）**就报错——checkpoint 模式的
    # 「生产不可达」因此能在启动时暴露，而不是等到请求期静默降级。
    with pytest.raises(CheckpointError) as ei:
        CheckpointTagger(d, device="cpu", model_factory=Nested,
                         tokenizer_factory=lambda cfg: TinyTokenizer())
    msg = str(ei.value)
    assert "扁平" in msg and "tags.json" in msg, msg
