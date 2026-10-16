"""CheckpointTagger（M2，spec §2 检查点契约 / §4.2 前向 / §4.3 微批）。

设计要点（本模块是启发式打标器之外的第二实现，走 sr.tagger.Tagger 协议）：
- :func:`load_and_validate` —— §2.3 加载流程的**校验红线**：router_config.json /
  model.pt / tags.json 齐备 + tags.json 与 sr.tags 逐项断言（名称 / 类型 / 标签数 /
  标签顺序）。校验失败抛 :class:`CheckpointError`，`sr serve --checkpoint` 据此
  拒绝启动（§2.5）——不再只查目录存在性。
- :data:`HEAD_SPECS` —— 头出处映射表（约 40 行）：tags.json 的 head（类型 noul /
  choice / multi / projection × labels）→ 产品形态（sr.tags 的 choice / ordered /
  multi / 嵌入头）。难点是 noul 二元头（jailbreak / complexity / fact_check）与
  产品 ordered 形态的换算（§2.2：「难度是两档」；jailbreak 产品 4 档）。
- :class:`CheckpointTagger` —— torch 前向：tokenize（transformers AutoTokenizer）→
  ``model.forward(input_ids, attention_mask, with_embedding=True)`` → 读
  classification_heads / multilabel_heads / embedding_head（§4.2-3）→ 归一化为
  规范键 + confidence（1 − H/log K）→ 与规则标签（sr.tags.tag_rules，同一套代码）
  合并成 signals。嵌入头 L2 归一化后与 ``req.last_vec`` 求余弦 → similarity/reask。
- 设备 / dtype：``device='cuda'`` 时 bf16 autocast，``'cpu'`` 时 fp32（§1.2）。
- 微批（§4.3）：``batch_window_ms>0`` 时启一个惰性 worker，把窗口内的请求合并成
  一次前向；默认 0（直接前向，行为确定、便于测试）。

依赖方向：本模块 → sr.tags / sr.tagger。**torch / transformers 只在真正构造
CheckpointTagger 时惰性 import**，故 ``load_and_validate`` 与 HEAD_SPECS 在无 torch
的环境里也可导入/测试。

模型类来自训练侧（`smart-router-train` 的 RoutePilotModel）——本模块按 spec 的
**契约**（router_config.json 键名 + forward 三头返回）编码，模型类可经
``model_factory`` 注入（训练侧包缺失时显式报错，不静默降级）。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from sr.tags import DOMAIN_KEYS, PII_KEYS, tag_rules
from sr.tagger import TagRequest, TagResult

# 二元 noul 头 → 产品 ordered 档位的默认切点（P(正类) 的三个分界；spec §4.2-5
# 说「阈值取自 tag_settings」，但 seed 只补种了 context 行；无行时用本默认，
# 可经 band_thresholds 覆盖）。jailbreak 产品 4 档、complexity 产品 2 档。
DEFAULT_BAND_THRESHOLDS: dict[str, list[float]] = {
    "jailbreak": [0.25, 0.5, 0.75],
}


class CheckpointError(Exception):
    """检查点校验失败（§2.3）——`sr serve --checkpoint` 据此拒绝启动（§2.5）。"""


@dataclass
class RouterConfig:
    """router_config.json → 结构（spec §2.1）。"""

    base_model: str
    max_length: int = 512
    embedding_dim: int = 768
    hidden_size: int | None = None
    dtype: str = "auto"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HeadSpec:
    """tags.json 一个头 → 产品形态的映射（§2.2 × §6.3）。"""

    json_key: str                 # tags.json 里的键（= head 名）
    product_tag: str | None       # sr.tags 里的产品标签键；嵌入头为 None
    head: str                     # 'classification' | 'multilabel' | 'embedding'
    ckpt_labels: tuple            # 检查点头的 label 顺序（权威 = 训练侧 signals.py）
    product_kind: str             # sr.tags kind：choice / noul / multi / ordered / projection


# 头出处映射表（约 40 行）：tags.json 的 8 个头（7 分类/多标签 + 1 嵌入）↔ 产品形态。
# 二元 noul 头：fact_check 产品本身就是 noul（2 值同序）；complexity 产品是 ordered
# 2 档（easy/hard）；jailbreak 产品是 ordered 4 档（benign/jailbreak 经 P 切 3 刀）。
HEAD_SPECS: tuple[HeadSpec, ...] = (
    HeadSpec("domain", "domain", "classification",
             tuple(DOMAIN_KEYS), "choice"),
    HeadSpec("jailbreak", "jailbreak", "classification",
             ("benign", "jailbreak"), "noul"),
    HeadSpec("fact_check", "fact_check", "classification",
             ("no_check", "needs_check"), "noul"),
    HeadSpec("user_feedback", "user_feedback", "classification",
             ("satisfied", "need_clarification", "wrong_answer", "want_different"),
             "choice"),
    HeadSpec("modality", "modality", "classification",
             ("text", "image", "text_image"), "choice"),
    HeadSpec("complexity", "complexity", "classification",
             ("easy", "hard"), "ordered"),
    HeadSpec("pii", "pii", "multilabel", tuple(PII_KEYS), "multi"),
    HeadSpec("embedding", None, "embedding", (), "projection"),
)

HEAD_BY_KEY: dict[str, HeadSpec] = {s.json_key: s for s in HEAD_SPECS}


@dataclass
class CheckpointFiles:
    """一次校验通过后的检查点三件套内容（§2.3）。"""

    config: RouterConfig
    tags_json: dict
    head_specs: tuple
    checkpoint_dir: str
    model_path: str


def _re_mech(value) -> str:
    """§6.8 机械归一：小写 + 空白/连字符 → 下划线（连续分隔符并作一个）。"""
    return re.sub(r"[\s\-]+", "_", str(value).strip().lower())


# 检查点头的原始 label → 规范键（§6.8 别名；*不*做 ordered 档位转换——检查点
# 头是 Linear(hidden, n_labels)，labels 是**离散标签**而非产品 ordered 档位）。
_HEAD_LABEL_ALIASES: dict[str, dict[str, str]] = {
    "fact_check": {"fact_check_needed": "needs_check",
                   "no_fact_check_needed": "no_check"},
    "modality": {"ar": "text", "diffusion": "image", "both": "text_image"},
    "user_feedback": {"sat": "satisfied"},
}


def canonical_label(head: str, raw) -> str:
    """检查点头的原始 label → 规范存储键（§6.8 机械归一 + 别名）。"""
    v = _re_mech(raw)
    return _HEAD_LABEL_ALIASES.get(head, {}).get(v, v)


def build_tags_json(labels_by_head: dict, embedding_dim: int = 768) -> dict:
    """训练侧标签注册表（``{head: (raw_label, …)}``）→ tags.json（§2.2）。

    按 :data:`HEAD_SPECS` 顺序，label 经 :func:`canonical_label` 归一为规范键。
    生成结果与 :func:`_validate_tags_json` 的期望一致——供训练侧未写出 tags.json
    时离线补齐（§2.4 的训练侧待办）。
    """
    tags: dict = {}
    for spec in HEAD_SPECS:
        if spec.head == "embedding":
            tags[spec.json_key] = {"type": "projection", "dim": int(embedding_dim)}
            continue
        raw = labels_by_head.get(spec.json_key, ())
        tags[spec.json_key] = {"type": spec.product_kind,
                               "labels": [canonical_label(spec.json_key, x)
                                          for x in raw]}
    return {"schema": 1, "tags": tags}


def load_router_config(path: str | Path) -> RouterConfig:
    """读 router_config.json → RouterConfig（缺 base_model → CheckpointError）。"""
    p = Path(path)
    if not p.is_file():
        raise CheckpointError(f"router_config.json 缺失：{p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"router_config.json 不可解析：{exc}") from exc
    if not isinstance(raw, dict) or not raw.get("base_model"):
        raise CheckpointError("router_config.json 缺 base_model")
    return RouterConfig(
        base_model=str(raw["base_model"]),
        max_length=int(raw.get("max_length") or 512),
        embedding_dim=int(raw.get("embedding_dim") or raw.get("hidden_size") or 768),
        hidden_size=(int(raw["hidden_size"]) if raw.get("hidden_size") else None),
        dtype=str(raw.get("dtype") or "auto"),
        raw=raw)


def _validate_tags_json(tags_json: dict) -> None:
    """tags.json 与 sr.tags 逐项断言（§2.3 第 4 步）：名称 / 类型 / 标签数 / 标签顺序。"""
    if not isinstance(tags_json, dict) or not isinstance(tags_json.get("tags"), dict):
        raise CheckpointError("tags.json 形状非法：缺 tags 映射")
    got = tags_json["tags"]
    want_keys = [s.json_key for s in HEAD_SPECS]
    if list(got.keys()) != want_keys:
        raise CheckpointError(
            f"tags.json 头清单与产品注册表不符：期望 {want_keys}，实际 {list(got.keys())}")
    for spec in HEAD_SPECS:
        entry = got[spec.json_key]
        if not isinstance(entry, dict):
            raise CheckpointError(f"tags.json.{spec.json_key} 非对象")
        if spec.head == "embedding":
            dim = entry.get("dim")
            if entry.get("type") != "projection" or not isinstance(dim, int):
                raise CheckpointError(
                    f"tags.json.{spec.json_key} 应为 projection+dim：{entry!r}")
            continue
        labels = entry.get("labels")
        if not isinstance(labels, list) or list(labels) != list(spec.ckpt_labels):
            raise CheckpointError(
                f"tags.json.{spec.json_key} labels 与注册表不符（顺序/数量/名称）："
                f"期望 {list(spec.ckpt_labels)}，实际 {labels!r}")


def load_and_validate(checkpoint_dir: str | Path) -> CheckpointFiles:
    """§2.3 加载流程的校验红线（不建模型、不 load 权重，纯文件+注册表校验）。

    :raises CheckpointError: 目录/文件缺失、router_config 非法、tags.json 与
        sr.tags 不一致（名称/类型/标签数/标签顺序）。
    """
    d = Path(checkpoint_dir)
    if not d.is_dir():
        raise CheckpointError(f"检查点目录不存在：{d}")
    cfg = load_router_config(d / "router_config.json")
    model_path = d / "model.pt"
    if not model_path.is_file():
        raise CheckpointError(f"model.pt 缺失：{model_path}")
    tags_path = d / "tags.json"
    if not tags_path.is_file():
        raise CheckpointError(
            f"tags.json 缺失：{tags_path}（训练侧 save_pretrained 应写出，§2.4）")
    try:
        tags_json = json.loads(tags_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"tags.json 不可解析：{exc}") from exc
    _validate_tags_json(tags_json)
    return CheckpointFiles(config=cfg, tags_json=tags_json, head_specs=HEAD_SPECS,
                           checkpoint_dir=str(d), model_path=str(model_path))


def _conf_of(probs) -> float:
    """confidence = 1 − H(probs) / log K（§4.2-4；K≤1 时回 1.0）。"""
    k = len(probs)
    if k <= 1:
        return 1.0
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def _softmax(logits):
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    s = sum(exps)
    return [e / s for e in exps]


def _sigmoid(x) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _band_index(p: float, cuts: list[float]) -> int:
    """P → 档位索引（达到的切点个数，与 sr.tags.conf_band 同语义）。"""
    return sum(1 for c in cuts if p >= c)


class CheckpointTagger:
    """RoutePilot 检查点打标器（Tagger 协议第二实现，name="checkpoint"）。

    模型类默认从训练侧包 import（``from routepilot.model import RoutePilotModel``，
    与 spec「上游 = smart-router-train」一致）；测试/无训练侧时经 ``model_factory``
    注入同契约的模型（``model.forward(input_ids, attention_mask, with_embedding=True)``
    → ``{classification_heads / multilabel_heads / embedding_head}``）。
    """

    name = "checkpoint"

    def __init__(self, checkpoint_dir: str | Path, *, device: str | None = None,
                 model_factory=None, tokenizer_factory=None,
                 band_thresholds: dict[str, list[float]] | None = None,
                 batch_window_ms: int = 0, warmup_samples: list[str] | None = None):
        # 先做纯文件/注册表校验（§2.3）——无 torch 也能给出准确的拒绝原因；
        # 校验通过后再惰性 import torch（缺依赖时才暴露为 ImportError）。
        files = load_and_validate(checkpoint_dir)
        import torch  # 惰性：仅 checkpoint 模式需要

        self._torch = torch
        self.files = files
        cfg = files.config
        self.max_length = cfg.max_length
        self.embedding_dim = cfg.embedding_dim
        self.band_thresholds = dict(DEFAULT_BAND_THRESHOLDS)
        if band_thresholds:
            self.band_thresholds.update(band_thresholds)

        if device:
            self.device = device
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32

        model_cls = model_factory or self._default_model_factory()
        self.model = model_cls(cfg)
        state = torch.load(files.model_path, map_location="cpu", weights_only=False)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        result = self.model.load_state_dict(state, strict=False)
        missing = list(getattr(result, "missing_keys", []))
        unexpected = list(getattr(result, "unexpected_keys", []))
        # 额外检查 backbone. 前缀的键（§2.3-3）
        backbone_extra = [k for k in unexpected if k.startswith("backbone.")]
        if missing or unexpected or backbone_extra:
            raise CheckpointError(
                f"state_dict 与模型不匹配：missing={missing[:5]}"
                f"（共 {len(missing)}）、unexpected={unexpected[:5]}"
                f"（共 {len(unexpected)}）")
        self.model.to(self.device)
        if self.device.startswith("cuda"):
            self.model.to(dtype=self.dtype)
        self.model.eval()

        if tokenizer_factory is not None:
            self.tokenizer = tokenizer_factory(cfg)
        else:
            # 真实训练侧模型自带 tokenizer（from_pretrained 时建好）；否则按 base_model 取
            self.tokenizer = getattr(self.model, "tokenizer", None)
            if self.tokenizer is None:
                from transformers import AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained(cfg.base_model)
        # §4.2-1：右 padding
        try:
            self.tokenizer.padding_side = "right"
        except Exception:                                   # noqa: BLE001
            pass

        self._batch_window_s = max(0, int(batch_window_ms)) / 1000.0
        self._warmup(warmup_samples or ["hello", "你好", "1+1=?", "写一段代码"])
        self._warmup_ready = True

    @staticmethod
    def _default_model_factory():
        """真实训练侧模型工厂：返回 ``callable(cfg) -> model``。

        优先 ``smart_router.modeling.router.SmartRouterModel``（本仓库约定的训练侧
        权威实现，``from_pretrained`` 建 backbone + 头）；回退 ``routepilot.model``
        （spec 文本里的旧包名）。都缺失则显式报错，不静默降级。
        """
        try:
            from smart_router.modeling.router import RouterConfig as TrainCfg
            from smart_router.modeling.router import SmartRouterModel

            def build(cfg):
                train_cfg = TrainCfg(base_model=cfg.base_model,
                                     max_length=cfg.max_length,
                                     embedding_dim=cfg.embedding_dim,
                                     dtype=cfg.dtype)
                return SmartRouterModel.from_pretrained(train_cfg)
            return build
        except Exception:                                   # noqa: BLE001
            pass
        try:
            from routepilot.model import RoutePilotModel      # type: ignore
            return lambda cfg: RoutePilotModel(cfg)
        except Exception as exc:                            # noqa: BLE001
            raise CheckpointError(
                "找不到训练侧模型类（smart_router.modeling.router.SmartRouterModel "
                "或 routepilot.model.RoutePilotModel）——请安装 smart-router-train "
                "并把其 src 加入 PYTHONPATH，或经 model_factory 注入同契约模型") from exc

    def _warmup(self, samples: list[str]) -> None:
        """预热 4 条样本（§2.3-5）：断言输出形状与取值范围正常。"""
        out = self._forward(samples)
        for spec in self.files.head_specs:
            if spec.head == "embedding":
                dim = len(out["embedding_head"][0])
                assert dim == self.embedding_dim, \
                    f"embedding 维度不符：{dim} != {self.embedding_dim}"
            else:
                rows = out[spec.head][spec.json_key]
                assert len(rows[0]) == len(spec.ckpt_labels), \
                    f"{spec.json_key} 头标签数不符：{len(rows[0])}"
                assert all(abs(float(v)) <= 1e6 for row in rows for v in row)

    # ── 前向 ─────────────────────────────────────────────────────────────────
    def _tokenize(self, texts: list[str]):
        torch = self._torch
        enc = self.tokenizer(texts, padding=True, truncation=True,
                             max_length=self.max_length, return_tensors="pt")
        return {k: v.to(self.device) for k, v in enc.items()}

    def _forward(self, texts: list[str]) -> dict:
        torch = self._torch
        enc = self._tokenize(texts)
        with torch.no_grad():
            if self.device.startswith("cuda"):
                with torch.autocast(device_type="cuda", dtype=self.dtype):
                    out = self.model.forward(enc["input_ids"],
                                             attention_mask=enc.get("attention_mask"),
                                             with_embedding=True)
            else:
                out = self.model.forward(enc["input_ids"],
                                         attention_mask=enc.get("attention_mask"),
                                         with_embedding=True)

        def _tolist(t):
            return t.detach().float().cpu().tolist()

        # 归一为 {classification: {name: [[…]]}, multilabel: {name: [[…]]}, embedding_head: [[…]]}。
        # 支持两种上游形状：
        #  (a) 契约式嵌套 {classification_heads, multilabel_heads, embedding_head}
        #      （spec §4.2-3 的写法，测试合成模型用）；
        #  (b) 真训练侧 SmartRouterModel 的**扁平** dict {head_name: logits, "embedding": vec}。
        norm = {"classification": {}, "multilabel": {}}
        if isinstance(out, dict) and "classification_heads" in out:
            cls_raw = out["classification_heads"]
            ml_raw = out.get("multilabel_heads", {})
            emb_raw = out.get("embedding_head", out.get("embedding"))
        else:
            cls_raw = {s.json_key: out[s.json_key]
                       for s in self.files.head_specs if s.head == "classification"}
            ml_raw = {s.json_key: out[s.json_key]
                      for s in self.files.head_specs if s.head == "multilabel"}
            emb_raw = out.get("embedding")
        for spec in self.files.head_specs:
            if spec.head == "classification":
                norm["classification"][spec.json_key] = _tolist(cls_raw[spec.json_key])
            elif spec.head == "multilabel":
                norm["multilabel"][spec.json_key] = _tolist(ml_raw[spec.json_key])
        norm["embedding_head"] = _tolist(emb_raw)
        return norm

    def _signals_from_row(self, row: dict, req: TagRequest) -> tuple[dict, list[float]]:
        """一行前向输出 → 模型标签 signals + 嵌入向量（§4.2-3/4）。"""
        signals: dict = {}
        for spec in self.files.head_specs:
            if spec.head == "classification":
                logits = row["classification"][spec.json_key]
                probs = _softmax(logits)
                idx = max(range(len(probs)), key=lambda i: probs[i])
                conf = _conf_of(probs)
                if spec.product_kind == "ordered":
                    # noul 二元头 → 产品 ordered：直接以 argmax 档位（0/1）为值
                    value = idx
                elif spec.json_key == "jailbreak":
                    # benign/jailbreak → 产品 4 档：按 P(jailbreak) 切默认/配置切点
                    p = probs[spec.ckpt_labels.index("jailbreak")]
                    cuts = self.band_thresholds.get("jailbreak", [0.25, 0.5, 0.75])
                    value = _band_index(p, cuts)
                else:
                    value = spec.ckpt_labels[idx]
                signals[spec.product_tag] = {"value": value, "confidence": conf,
                                             "is_rule": False}
            elif spec.head == "multilabel":
                logits = row["multilabel"][spec.json_key]
                probs = [_sigmoid(x) for x in logits]
                hits = [spec.ckpt_labels[i] for i, p in enumerate(probs) if p >= 0.5]
                signals[spec.product_tag] = {"value": hits, "confidence": None,
                                             "is_rule": False}
        vec = _l2_normalize(row["embedding_head"])
        return signals, vec

    async def tag(self, req: TagRequest) -> TagResult:
        loop = asyncio.get_event_loop()
        signals, vec = await loop.run_in_executor(None, self._tag_row, req)
        # 规则标签（§4.2-6：与启发式同一套代码）
        rules = tag_rules(req.text, req.headers, req.n_messages, req.now,
                          context_thresholds=req.context_thresholds)
        signals.update(rules)
        return TagResult(signals=signals, vec=vec)

    def _tag_row(self, req: TagRequest):
        out = self._forward([req.text])
        row = {
            "classification": {name: rows[0]
                               for name, rows in out["classification"].items()},
            "multilabel": {name: rows[0]
                           for name, rows in out["multilabel"].items()},
            "embedding_head": out["embedding_head"][0],
        }
        signals, vec = self._signals_from_row(row, req)
        # similarity / reask：与上一轮向量余弦（§4.2-3）
        sim, reask = _similarity_signals(vec, req.last_vec)
        signals["similarity"] = sim
        signals["reask"] = reask
        return signals, vec


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _similarity_signals(vec: list[float], last_vec) -> tuple[dict, dict]:
    """嵌入比对 → similarity 档（0/1/2）/ reask（yes/no）（§4.2-3 嵌入头两用）。"""
    if not last_vec:
        return ({"value": 0, "confidence": None, "is_rule": False},
                {"value": "no", "confidence": None, "is_rule": False})
    dim = min(len(vec), len(last_vec))
    cos = sum(vec[i] * last_vec[i] for i in range(dim))
    band = 0 if cos < 0.55 else (1 if cos < 0.85 else 2)
    return ({"value": band, "confidence": None, "is_rule": False},
            {"value": "yes" if cos >= 0.85 else "no", "confidence": None,
             "is_rule": False})
