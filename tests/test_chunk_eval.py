"""tests for scripts/jev_chunk_eval.py（P2-C 200K 分块召回评估 runner）。

全部用合成 corpus + 注入 fake tagger：**不依赖 GPU / 网络 / torch / transformers**，
也不读写 ``docs/jev-pii-200k`` 真实数据（超慢）。覆盖：

* arm 解析 / CLI 校验 / dry-run；
* corpus 读取（丢 doc_ids 大字段）、set 过滤、sha256 校验；
* score 阶段的 rows/meta/resume（身份不一致拒绝）、chunk 能力探测与清晰报错；
* p10 校准纪律（只用 p10 拟合、p50/p90 验证、改 validation 概率不改阈值）；
* 指标（位置桶 recall / clean FPR / rule-guided 拆分 / 混淆矩阵走 eval_metrics）；
* 报告自动生成（JSON + markdown，无手抄）、report 阶段可单独 CPU 跑。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import jev_chunk_eval as runner  # noqa: E402

SCRIPT_PATH = SCRIPTS_DIR / "jev_chunk_eval.py"

#: 合成 questions 配置里的 PII 类型（够用即可，不追求真实 30 类）。
PII_TYPES = ["age", "phone_number", "cn_id_card", "person"]


# --------------------------------------------------------------------------- #
# 合成 corpus / fake tagger
# --------------------------------------------------------------------------- #
def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_corpus(tmp_path: Path, docs: list[dict]) -> dict:
    """按真实 manifest 的字段写合成 corpus；返回 docs_root/问题配置/索引。"""
    docs_root = tmp_path / "corpus"
    (docs_root / "docs").mkdir(parents=True, exist_ok=True)
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({
        "signals": {"pii": {"types": {typ: {"zh": typ} for typ in PII_TYPES}}}
    }), encoding="utf-8")
    lines = []
    docs_by_id: dict[str, dict] = {}
    for spec in docs:
        doc_id = spec["doc_id"]
        text = "DOC " + doc_id + " " + ("filler " * 20)
        text_path = docs_root / "docs" / (doc_id + ".txt")
        text_path.write_text(text, encoding="utf-8")
        needles = []
        if spec.get("group") != "clean":
            needles = [{"type": spec["type"], "lang": spec.get("lang", "zh"),
                        "pos": spec.get("pos", 0.5)}]
        rule_hits = ([spec["type"]] if spec.get("rule") else [])
        actual_tokens = int(spec.get("actual_tokens", 32))
        row = {
            "doc_id": doc_id,
            "group": spec["group"],
            "base": 0,
            "target_tokens": 200000,
            "actual_tokens": actual_tokens,
            "chars": len(text),
            "needles": needles,
            "needle_token_index": [
                {"type": needle["type"], "lang": needle["lang"],
                 "pos": needle["pos"], "index": 10, "n_tokens": 3}
                for needle in needles],
            "rule_pii_hits": rule_hits,
            "text_file": "docs/" + doc_id + ".txt",
            "sha256": _sha256(text),
            # 真实 manifest 的重字段：load_corpus 必须丢掉，不驻留内存。
            "doc_ids": list(range(2000)),
        }
        lines.append(json.dumps(row, ensure_ascii=False))
        docs_by_id[doc_id] = row
    manifest = docs_root / "manifest.jsonl"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"docs_root": docs_root, "manifest": manifest,
            "questions": questions, "docs_by_id": docs_by_id}


def sample_docs() -> list[dict]:
    """覆盖 p10/p50/p90 × rule-guided/model-only + len/en/multi/clean。"""
    return [
        # main：每个位置 rule-guided(phone) / model-only(age) 各一
        {"doc_id": "main_age_p10", "group": "main", "type": "age", "pos": 0.1},
        {"doc_id": "main_phone_number_p10", "group": "main",
         "type": "phone_number", "pos": 0.1, "rule": True},
        {"doc_id": "main_age_p50", "group": "main", "type": "age", "pos": 0.5},
        {"doc_id": "main_phone_number_p50", "group": "main",
         "type": "phone_number", "pos": 0.5, "rule": True},
        {"doc_id": "main_age_p90", "group": "main", "type": "age", "pos": 0.9},
        {"doc_id": "main_phone_number_p90", "group": "main",
         "type": "phone_number", "pos": 0.9, "rule": True},
        # len：长度分层（8k rule / 128k model-only）
        {"doc_id": "len_cn_id_card_8k", "group": "len", "type": "cn_id_card",
         "pos": 0.5, "rule": True, "actual_tokens": 8000},
        {"doc_id": "len_person_128k", "group": "len", "type": "person",
         "pos": 0.5, "actual_tokens": 128000},
        # en / multi / clean
        {"doc_id": "en_cn_id_card_p50", "group": "en", "type": "cn_id_card",
         "pos": 0.5, "rule": True, "lang": "en"},
        {"doc_id": "multi_0", "group": "multi", "type": "cn_id_card",
         "pos": 0.5, "rule": True},
        {"doc_id": "clean_b0", "group": "clean"},
    ]


class FakeResult:
    def __init__(self, signals, input_stats, tagger_info, questions_sha256):
        self.signals = signals
        self.input = input_stats
        self.tagger_info = tagger_info
        self.questions_sha256 = questions_sha256


class FakeTagger:
    """注入用 tagger：按 (arm, doc, type) 查表返回概率，并模拟 chunk input 键。"""

    def __init__(self, arm, docs_by_id, p_fn, pii_types, *,
                 omit_chunk_keys: bool = False):
        self.arm = arm
        self.docs_by_id = docs_by_id
        self.p_fn = p_fn
        self.pii_types = list(pii_types)
        self.omit_chunk_keys = omit_chunk_keys
        self.max_length = 16
        self.calls = 0
        self.closed = 0
        chunk_enabled = arm.kind == "chunk"
        self._info = {
            "engine": "jev",
            "model_name": "fake-jev",
            "model_dir": "/fake/model",
            "temperature": 1.0,
            "max_length": self.max_length,
            "window": arm.window,
            "questions_sha256": "q-sha-fake",
            "weights_sha256": "w" * 64,
            "head_sha256": "h" * 64,
            "tokenizer_sha256": "t" * 64,
            "chat_template_sha256": "c" * 64,
            "chunk_segments": arm.chunk_segments,
            "chunk_size": arm.chunk_size,
            "chunk_agg": arm.chunk_agg if chunk_enabled else "off",
            "chunk_overlap": 0.25,
            "chunk_calibration": "none",
        }

    def info(self) -> dict:
        return dict(self._info)

    async def tag(self, req):
        from router.pipeline.scope import SignalScope

        self.calls += 1
        doc_id = req.text.split()[1]
        doc = self.docs_by_id[doc_id]
        scope = req.scope
        if scope is None or scope.full:
            types = list(self.pii_types)
        else:
            types = sorted(scope.pii_types)
        probs = {typ: float(self.p_fn(self.arm, doc, typ)) for typ in types}
        rules = list(doc.get("rule_pii_hits") or [])
        input_stats = {
            "input_tokens": int(doc.get("actual_tokens") or 0),
            "input_visible_tokens": min(1024, int(doc.get("actual_tokens") or 0)),
            "input_window": self.arm.window,
        }
        if (self.arm.kind == "chunk" and not self.omit_chunk_keys
                and input_stats["input_tokens"] > self.max_length):
            input_stats.update({
                "chunk_segments": self.arm.chunk_segments,
                "chunk_scored_segments": self.arm.chunk_segments,
                "chunk_prompt_tokens": (self.arm.chunk_segments * len(types) * 100),
                "chunk_strategy": "head+tail+rule+uniform:" + self.arm.chunk_agg,
            })
        signals = {
            "pii": {
                "value": rules,
                "confidence": 1.0 if rules else None,
                "conf_by_value": {typ: 1.0 for typ in rules},
                "is_rule": bool(rules),
                "model_p_by_type": probs,
                "model_hits": [],
                "rule_hits": rules,
            },
            "user_feedback": {"value": None, "confidence": None, "is_rule": False},
        }
        # 断言 eval 打分用的是 trimmed scope（needle 文档只问 needle 类型）；
        # p10-fullpack 的校准调用会问全部类型，单独放行。
        if doc.get("needles") and scope is not None and not scope.full:
            expected = sorted({needle["type"] for needle in doc["needles"]})
            if set(scope.pii_types) != set(PII_TYPES):
                assert isinstance(scope, SignalScope)
                assert sorted(scope.pii_types) == expected
        return FakeResult(signals, input_stats, self.info(), "q-sha-fake")

    async def aclose(self):
        self.closed += 1


def make_factory(docs_by_id, p_fn, *, omit_chunk_keys=False):
    built: list[FakeTagger] = []

    def factory(arm):
        tagger = FakeTagger(arm, docs_by_id, p_fn, PII_TYPES,
                            omit_chunk_keys=omit_chunk_keys)
        built.append(tagger)
        return tagger

    factory.built = built
    return factory


def default_p_fn(arm, doc, typ):
    """确定性概率：chunk 的 rule 段能看到 rule-guided needle，window 全盲。"""
    if doc.get("group") == "clean":
        return 0.02
    needle_types = {needle["type"] for needle in doc.get("needles") or []}
    if typ not in needle_types:
        return 0.01
    if arm.kind == "chunk":
        rules = set(doc.get("rule_pii_hits") or [])
        return 0.90 if typ in rules else 0.10
    return 0.05


def make_args(tmp_path: Path, corpus: dict, *extra: str):
    argv = [
        "--out", str(tmp_path / "run"),
        "--docs-root", str(corpus["docs_root"]),
        "--manifest", str(corpus["manifest"]),
        "--questions", str(corpus["questions"]),
        "--n-boot", "60",
        "--limit", "0",
        *extra,
    ]
    return runner.parse_args(argv)


# --------------------------------------------------------------------------- #
# arms / CLI
# --------------------------------------------------------------------------- #
def test_parse_arms() -> None:
    arms = runner.parse_arms("head1024,headtail256:768,chunk4,chunk8",
                             chunk_size=512, chunk_agg="noisy_or")
    assert [arm.name for arm in arms] == [
        "head1024", "headtail256:768", "chunk4", "chunk8"]
    assert arms[0].kind == "window" and arms[0].window == "1024:0"
    assert arms[1].window == "256:768"
    assert arms[2].kind == "chunk" and arms[2].chunk_segments == 4
    assert arms[3].chunk_segments == 8
    assert arms[2].chunk_size == 512 and arms[2].chunk_agg == "noisy_or"
    assert arms[2].window == runner.DEFAULT_CHUNK_WINDOW
    explicit = runner.parse_arm("window:0:1024")
    assert explicit.kind == "window" and explicit.window == "0:1024"
    with pytest.raises(ValueError):
        runner.parse_arm("chunk0")
    with pytest.raises(ValueError):
        runner.parse_arm("chunkx")
    with pytest.raises(ValueError):
        runner.parse_arm("bogus")
    with pytest.raises(ValueError):
        runner.parse_arms("head1024,head1024")


def test_parse_args_defaults_and_validation(tmp_path: Path) -> None:
    args = runner.parse_args(["--out", str(tmp_path / "x")])
    assert args.stages == ["score", "report"]
    assert args.arms == runner.DEFAULT_ARMS
    assert args.calibration == "p10-quantile"
    assert args.sets == list(runner.SET_NAMES)
    report_only = runner.parse_args(
        ["--out", str(tmp_path / "x"), "--stages", "report"])
    assert report_only.stages == ["report"]
    with pytest.raises(SystemExit):
        runner.parse_args(["--out", str(tmp_path / "x"), "--stages", "bogus"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--out", str(tmp_path / "x"), "--sets", "nope"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--out", str(tmp_path / "x"), "--limit", "-1"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--out", str(tmp_path / "x"), "--arms", "bogus"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--out", str(tmp_path / "x"),
                           "--cal-target-recall", "2"])


def test_dry_run_plan_without_touching_files(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main,clean",
                     "--arms", "head1024,chunk4")
    args.dry_run = True

    def no_factory(arm):            # 不允许在 dry-run 里构造模型
        raise AssertionError("dry-run 不应该构造 tagger")

    result = runner.run_score(args, tagger_factory=no_factory)
    assert result["dry_run"] is True
    assert result["docs"] == 7
    assert result["arms"] == ["head1024", "chunk4"]
    assert not Path(str(tmp_path / "run") + ".jsonl").exists()
    assert not Path(str(tmp_path / "run") + ".meta.json").exists()


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #
def test_load_corpus_drops_heavy_doc_ids_and_selects(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    docs = runner.load_corpus(corpus["manifest"])
    assert len(docs) == 11
    assert all("doc_ids" not in row for row in docs)
    assert {row["group"] for row in docs} == {
        "main", "len", "en", "multi", "clean"}
    selected = runner.select_docs(docs, ["main", "clean"], limit=2, seed=0)
    assert len(selected) == 3          # main 2 + clean 1（每 set 各取前 2）
    assert all(row["group"] in ("main", "clean") for row in selected)
    assert selected == runner.select_docs(docs, ["main", "clean"], limit=2,
                                          seed=0)
    assert selected != runner.select_docs(docs, ["main", "clean"], limit=2,
                                          seed=1)
    with pytest.raises(ValueError):
        runner.select_docs(docs, ["bogus"])


def test_read_doc_text_sha256_validation(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    docs = runner.load_corpus(corpus["manifest"])
    doc = docs[0]
    text = runner.read_doc_text(corpus["docs_root"], doc, verify_sha256=True)
    assert text.startswith("DOC " + doc["doc_id"])
    text_path = corpus["docs_root"] / doc["text_file"]
    text_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(runner.ChunkEvalError):
        runner.read_doc_text(corpus["docs_root"], doc, verify_sha256=True)
    assert runner.read_doc_text(corpus["docs_root"], doc,
                                verify_sha256=False) == "tampered"


def test_bucket_and_p10_helpers() -> None:
    assert runner.bucket_for_pos(0.1) == "p10"
    assert runner.bucket_for_pos(0.5) == "p50"
    assert runner.bucket_for_pos(0.9) == "p90"
    assert runner.bucket_for_pos(0.2) == "other"
    assert runner.is_p10_doc({"group": "main",
                              "needles": [{"type": "age", "pos": 0.1}]})
    assert not runner.is_p10_doc({"group": "main",
                                  "needles": [{"type": "age", "pos": 0.5}]})
    assert not runner.is_p10_doc({"group": "multi",
                                  "needles": [{"type": "age", "pos": 0.1}]})


# --------------------------------------------------------------------------- #
# chunk 能力探测
# --------------------------------------------------------------------------- #
def test_chunk_support_probe_shape() -> None:
    support = runner.chunk_support()
    assert isinstance(support, dict)
    assert isinstance(support.get("available"), bool)
    assert "functions" in support


def test_chunk_unavailable_gives_clear_error(monkeypatch, tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024,chunk4")
    monkeypatch.setattr(runner, "chunk_support",
                        lambda: {"available": False,
                                 "ctor_has_chunk_segments": False,
                                 "functions": []})
    factory = make_factory(corpus["docs_by_id"], default_p_fn)
    with pytest.raises(runner.ChunkUnavailable) as excinfo:
        runner.run_score(args, tagger_factory=factory)
    message = str(excinfo.value)
    assert "chunk" in message and "--skip-chunk-arms" in message
    # --skip-chunk-arms：跳过 chunk 臂后仍能跑窗口臂
    args.skip_chunk_arms = True
    result = runner.run_score(args, tagger_factory=factory)
    assert result["rows"] == 6           # 6 篇 main × head1024
    arms = {row["arm"] for row in runner.read_jsonl(
        str(tmp_path / "run.jsonl"))}
    assert arms == {"head1024"}


def test_chunk_missing_input_keys_raises(monkeypatch, tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "chunk4")
    factory = make_factory(corpus["docs_by_id"], default_p_fn,
                           omit_chunk_keys=True)
    with pytest.raises(runner.ChunkUnavailable) as excinfo:
        runner.run_score(args, tagger_factory=factory)
    assert "chunk_segments" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# score 阶段：rows/meta/resume
# --------------------------------------------------------------------------- #
def test_score_stage_rows_meta_and_resume(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main,clean",
                     "--arms", "head1024,chunk4")
    factory = make_factory(corpus["docs_by_id"], default_p_fn)
    result = runner.run_score(args, tagger_factory=factory)
    raw_path = Path(str(tmp_path / "run") + ".jsonl")
    meta_path = Path(str(tmp_path / "run") + ".meta.json")
    assert raw_path.is_file() and meta_path.is_file()
    rows = runner.read_jsonl(raw_path)
    assert len(rows) == 2 * result["docs"]
    assert result["rows"] == len(rows)
    by_key = {(row["arm"], row["doc_id"]): row for row in rows}
    head = by_key[("head1024", "main_age_p50")]
    assert head["probs"] == {"age": 0.05}
    assert head["arm_config"]["kind"] == "window"
    assert "chunk_segments" not in head["input"]
    chunk = by_key[("chunk4", "main_phone_number_p50")]
    assert chunk["probs"] == {"phone_number": 0.9}
    assert chunk["input"]["chunk_segments"] == 4
    assert chunk["input"]["chunk_scored_segments"] == 4
    clean = by_key[("head1024", "clean_b0")]
    assert set(clean["probs"]) == set(PII_TYPES)       # clean 问全部类型
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["corpus"]["manifest_sha256"]
    assert meta["model"]["questions_sha256"] == "q-sha-fake"
    assert [arm["name"] for arm in meta["arms"]] == ["head1024", "chunk4"]
    assert meta["settings"]["pii_types"] == PII_TYPES

    # resume：不重复打分行、不再构造 tagger（pending 为空时跳过该臂）
    factory2 = make_factory(corpus["docs_by_id"], default_p_fn)
    before = raw_path.read_text(encoding="utf-8")
    runner.run_score(args, tagger_factory=factory2)
    assert raw_path.read_text(encoding="utf-8") == before
    assert not factory2.built

    # run 身份变化（seed）→ 拒绝 resume，并给出清晰差异
    changed = make_args(tmp_path, corpus, "--sets", "main,clean",
                        "--arms", "head1024,chunk4", "--seed", "1")
    with pytest.raises(runner.ResumeMismatch) as excinfo:
        runner.run_score(changed, tagger_factory=factory2)
    assert "seed" in str(excinfo.value)
    # --no-resume：清空后重写
    reset = make_args(tmp_path, corpus, "--sets", "main,clean",
                      "--arms", "head1024,chunk4", "--no-resume")
    runner.run_score(reset, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    assert len(runner.read_jsonl(raw_path)) == 2 * result["docs"]


def test_p10_fullpack_rows_only_for_p10_docs(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "chunk4",
                     "--calibration", "p10-fullpack")
    factory = make_factory(corpus["docs_by_id"], default_p_fn)
    runner.run_score(args, tagger_factory=factory)
    rows = runner.read_jsonl(str(tmp_path / "run.jsonl"))
    p10 = [row for row in rows if runner.is_p10_doc(
        {"group": row["group"], "needles": row["needles"]})]
    assert len(p10) == 2
    for row in p10:
        assert "calibration_fullpack" in row
        assert set(row["calibration_fullpack"]["probs"]) == set(PII_TYPES)
    for row in rows:
        if row not in p10:
            assert "calibration_fullpack" not in row


# --------------------------------------------------------------------------- #
# 指标 / 校准纪律
# --------------------------------------------------------------------------- #
def _score_and_report(tmp_path: Path, *extra: str):
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main,clean,len,en,multi",
                     *extra)
    factory = make_factory(corpus["docs_by_id"], default_p_fn)
    runner.run_score(args, tagger_factory=factory)
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60", *extra])
    return corpus, args, runner.run_report(report_args)


def test_report_metrics_and_structure(tmp_path: Path) -> None:
    corpus, args, report = _score_and_report(
        tmp_path, "--arms", "head1024,chunk4")
    assert report["schema_version"] == runner.SCHEMA_VERSION
    assert report["corpus"]["n_selected"] == 11
    assert report["corpus"]["manifest_sha256"]
    arms = report["arms"]
    assert set(arms) == {"head1024", "chunk4"}
    head = arms["head1024"]
    chunk = arms["chunk4"]
    # window 臂：p50/p90 model recall raw = 0（结构性失明）
    assert head["buckets"]["p50"]["recall"]["raw"]["recall"] == 0.0
    assert head["buckets"]["p90"]["recall"]["raw"]["recall"] == 0.0
    # chunk 臂：rule-guided needle 被 rule 段覆盖 → 1/1，model-only 0/1
    p50 = chunk["buckets"]["p50"]
    assert p50["recall"]["raw"]["recall"] == 0.5
    assert p50["split"]["raw"]["rule_guided"]["recall"] == 1.0
    assert p50["split"]["raw"]["model_only"]["recall"] == 0.0
    p90 = chunk["buckets"]["p90"]
    assert p90["split"]["raw"]["rule_guided"]["recall"] == 1.0
    assert p90["split"]["raw"]["model_only"]["recall"] == 0.0
    # clean FPR：clean p=0.02 <0.5 → raw 与 cal 都是 0/4
    for name in ("head1024", "chunk4"):
        clean = arms[name]["clean_fpr"]
        assert clean["raw"]["fp"] == 0 and clean["raw"]["n"] == len(PII_TYPES)
        assert clean["raw"]["fpr"] == 0.0
    # 校准：window 臂 p10 全 0.05 → degenerate；chunk 臂有 0.9 → 非 degenerate
    assert head["calibration"]["degenerate"] is True
    assert head["calibration"]["degenerate_reason"] == "raw_hits_zero"
    assert head["calibration"]["available"] is True
    assert chunk["calibration"]["available"] is True
    assert chunk["calibration"]["degenerate"] is False
    assert chunk["calibration"]["degenerate_reason"] is None
    assert chunk["calibration"]["bias"] is not None
    assert chunk["calibration"]["tau"] is not None
    # 非劣/分类指标结构（eval_metrics.Confusion.as_dict + ECE/AURC）
    type_conf = head["validation"]["type_level"]["raw"]
    for key in ("tp", "fp", "tn", "fn", "precision", "recall", "f1",
                "fpr", "fdr", "ece", "aurc"):
        assert key in type_conf
    # 请求级混淆矩阵（main p50/p90 正例 + clean 负例）
    request_conf = head["validation"]["request_level"]["raw"]
    assert request_conf["n"] == 4 + 1        # 4 篇 p50/p90 + 1 篇 clean
    assert request_conf["fp"] == 0
    # 分组 / 类型维度
    assert set(chunk["groups"]) >= {"len", "en", "multi"}
    assert chunk["groups"]["en"]["n"] == 1
    assert chunk["len_buckets"]["8k"]["n"] == 1
    assert chunk["len_buckets"]["128k"]["n"] == 1
    assert chunk["per_type_bucket"]["phone_number"]["p50"]["tp_raw"] == 1
    # 成本 / 延迟 / rule
    assert chunk["cost"]["mean_scored_segments"] == 4.0
    assert chunk["cost"]["mean_prompt_tokens"]
    assert chunk["latency"]["n"] == 10                 # needle 文档口径
    assert chunk["latency"]["n_clean_rows"] == 1
    assert head["rule"]["rule_guided_needles"] >= 4
    # 自动结论 / 限制
    assert report["conclusions"] and any(
        "head1024" in bullet for bullet in report["conclusions"])
    assert any("chunk4" in bullet for bullet in report["conclusions"])
    assert report["limitations"]
    # 汇总表
    summary = {entry["arm"]: entry for entry in report["summary_table"]}
    assert summary["head1024"]["recall_p50_raw"] == 0.0
    assert summary["chunk4"]["recall_p50_raw"] == 0.5
    assert summary["chunk4"]["mean_scored_segments"] == 4.0


def test_calibration_only_uses_p10(tmp_path: Path) -> None:
    """改 p50/p90 的概率必须不改 p10 拟合出的 bias/tau（禁止同集选阈值）。"""
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024,chunk4")
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60"])
    first = runner.run_report(report_args)
    bias_first = {name: (arm["calibration"]["bias"],
                         arm["calibration"]["tau"])
                  for name, arm in first["arms"].items()}
    # 篡改所有非 p10 行的概率（validation 集）
    raw_path = Path(str(tmp_path / "run") + ".jsonl")
    rows = runner.read_jsonl(raw_path)
    for row in rows:
        if runner.is_p10_doc({"group": row["group"],
                              "needles": row["needles"]}):
            continue
        row["probs"] = {typ: 0.97 for typ in row["probs"]}
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    second = runner.run_report(report_args)
    bias_second = {name: (arm["calibration"]["bias"],
                          arm["calibration"]["tau"])
                   for name, arm in second["arms"].items()}
    assert bias_first == bias_second
    # 但 validation 指标应该变化（chunk 的 p50/p90 现在全命中）
    assert (second["arms"]["chunk4"]["buckets"]["p50"]["recall"]["raw"]
            ["recall"] == 1.0)


def test_fullpack_calibration_fit(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024",
                     "--calibration", "p10-fullpack")
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--calibration", "p10-fullpack", "--n-boot", "60"])
    report = runner.run_report(report_args)
    cal = report["arms"]["head1024"]["calibration"]
    assert cal["method"] == "p10-fullpack"
    assert cal["available"] is True
    assert cal["n_negatives"] > 0
    assert cal["p10_fpr_cal"] <= 0.01 + 1e-12
    assert cal["tau"] is not None


def test_report_calibration_must_match_score(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024",
                     "--calibration", "p10-fullpack")
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    mismatch = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60"])          # 默认 quantile，与打分时 fullpack 不一致
    with pytest.raises(runner.ChunkEvalError) as excinfo:
        runner.run_report(mismatch)
    assert "p10-fullpack" in str(excinfo.value)
    matched = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--calibration", "p10-fullpack", "--n-boot", "60"])
    assert runner.run_report(matched)["config"]["calibration"] == "p10-fullpack"


def test_fullpack_degenerate_reason_no_threshold_satisfies_fpr(
        tmp_path: Path) -> None:
    """FPR 约束下没有 recall>0 可用阈值 → no_threshold_satisfies_fpr（文案修）。"""
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024",
                     "--calibration", "p10-fullpack")
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    raw_path = Path(str(tmp_path / "run") + ".jsonl")
    rows = runner.read_jsonl(raw_path)
    for row in rows:
        if not runner.is_p10_doc({"group": row["group"],
                                  "needles": row["needles"]}):
            continue
        needle_types = {needle["type"] for needle in row["needles"]}
        probs = row["calibration_fullpack"]["probs"]
        for typ in list(probs):
            probs[typ] = 0.2 if typ in needle_types else 0.9
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--calibration", "p10-fullpack", "--cal-target-fpr", "0.01",
         "--n-boot", "60"])
    report = runner.run_report(report_args)
    calibration = report["arms"]["head1024"]["calibration"]
    assert calibration["degenerate"] is True
    assert calibration["degenerate_reason"] == "no_threshold_satisfies_fpr"
    summary = (tmp_path / "run.report.md").read_text(encoding="utf-8")
    assert "no_threshold_satisfies_fpr" in summary
    assert any("no_threshold_satisfies_fpr" in bullet
               for bullet in report["conclusions"])


def test_fullpack_report_requires_fullpack_score(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024")     # 默认 quantile 打分
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--calibration", "p10-fullpack", "--n-boot", "60"])
    with pytest.raises(runner.ChunkEvalError) as excinfo:
        runner.run_report(report_args)
    assert "p10-fullpack" in str(excinfo.value)


def test_report_flags_incomplete_coverage(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "head1024,chunk4")
    runner.run_score(args, tagger_factory=make_factory(
        corpus["docs_by_id"], default_p_fn))
    raw_path = Path(str(tmp_path / "run") + ".jsonl")
    rows = [row for row in runner.read_jsonl(raw_path)
            if row["arm"] != "chunk4"]
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report_args = runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60"])
    report = runner.run_report(report_args)
    chunk = report["arms"]["chunk4"]
    assert chunk["coverage"]["complete"] is False
    assert chunk["coverage"]["rows"] == 0
    assert any("不完整产物" in bullet for bullet in report["conclusions"])


# --------------------------------------------------------------------------- #
# 报告落盘 / 入口 / Python 3.10 兼容
# --------------------------------------------------------------------------- #
def test_report_stage_writes_files_and_markdown(tmp_path: Path) -> None:
    corpus, _args, report = _score_and_report(tmp_path, "--arms",
                                              "head1024,chunk4")
    report_md = Path(str(tmp_path / "run") + ".report.md")
    report_json = Path(str(tmp_path / "run") + ".report.json")
    assert report_md.is_file() and report_json.is_file()
    loaded = json.loads(report_json.read_text(encoding="utf-8"))
    assert loaded["summary_table"] == report["summary_table"]
    markdown = report_md.read_text(encoding="utf-8")
    for needle in ("位置桶", "p10", "p50", "p90", "clean 文档 FPR",
                   "校准", "rule-guided", "限制与口径", "未手抄"):
        assert needle in markdown, needle
    assert "head1024" in markdown and "chunk4" in markdown
    # 报告标题里的计数必须来自数据（不是手抄的固定 90/5/30）
    assert "main 6 篇" in markdown          # 合成 corpus main=6
    assert "FPR（1 篇 × 4 类型）" in markdown
    assert "⚠ yes" in markdown              # window 臂 p10 校准 degenerate 标记
    assert "window=1024:0" in markdown and "seg=4 size=512" in markdown


def test_report_missing_raw_gives_clean_error(tmp_path: Path) -> None:
    code = runner.entrypoint(
        ["--out", str(tmp_path / "nope"), "--stages", "report"])
    assert code == 2


def test_entrypoint_returns_clean_error(monkeypatch, tmp_path: Path,
                                        capsys) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    monkeypatch.setattr(runner, "chunk_support",
                        lambda: {"available": False, "functions": []})
    code = runner.entrypoint([
        "--out", str(tmp_path / "run"),
        "--docs-root", str(corpus["docs_root"]),
        "--manifest", str(corpus["manifest"]),
        "--questions", str(corpus["questions"]),
        "--sets", "main", "--limit", "1", "--arms", "chunk4",
        "--stages", "score"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ERROR:" in err and "chunk" in err


def test_report_stage_does_not_import_torch(tmp_path: Path) -> None:
    _corpus, _args, report = _score_and_report(tmp_path, "--arms", "head1024")
    assert report["arms"]["head1024"]["coverage"]["complete"] is True
    # 本地 venv 没有 torch；如果模块顶层 import 了 torch，import 就已失败。
    assert "transformers" not in sys.modules


def test_script_is_python310_compatible() -> None:
    """76 venv 是 Python 3.10：用本机 python3.11 做 py_compile 自检。"""
    candidates = [os.environ.get("PYTHON311"), "python3.11",
                  str(Path.home() / ".local/bin/python3.11")]
    executable = None
    for candidate in candidates:
        if candidate and (Path(candidate).is_file()
                          or shutil.which(candidate)):
            executable = candidate
            break
    if executable is None:
        pytest.skip("本机没有 python3.11，跳过 3.10 语法自检")
    subprocess.run([executable, "-m", "py_compile", str(SCRIPT_PATH)],
                   check=True, capture_output=True)
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    ast.parse(source)                    # 3.14 parser 兜底
    assert "from __future__ import annotations" in source
