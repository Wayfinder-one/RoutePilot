"""tests for ``scripts/jev_candidate_eval.py``（P5-A 候选引导评估 runner）。

全部用合成 corpus + 注入 fake scorer：不依赖 GPU / 网络 / torch / sklearn。
覆盖：arms/CLI、候选 + uniform + head 窗口、批量 ``_score``（不调 tag())、
候选窗只问 needle 类型 / p10+clean 全类型、resume 身份校验、候选召回 vs 模型
召回分离、p10 FPR 约束校准（只用 p10）、报告自动生成、入口错误、3.10 语法。
"""

from __future__ import annotations

import json
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

import jev_candidate_eval as eval_runner  # noqa: E402

PII_TYPES = ["age", "phone_number", "cn_id_card", "person"]
SCRIPT_PATH = SCRIPTS_DIR / "jev_candidate_eval.py"


# --------------------------------------------------------------------------- #
# 合成 corpus / fake scorer
# --------------------------------------------------------------------------- #
class FakeTokenizer:
    """char = 1 token 的 fake tokenizer（offset 精确）。"""

    def __call__(self, text, add_special_tokens=False,
                 return_offsets_mapping=False, **kwargs):
        ids = [ord(char) for char in text]
        if return_offsets_mapping:
            return {"input_ids": ids,
                    "offset_mapping": [[index, index + 1]
                                       for index in range(len(text))]}
        return {"input_ids": ids}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(chr(int(token)) for token in ids)


def filler(length: int) -> str:
    base = "这是一段用来填充的普通文本内容。"
    return (base * (length // len(base) + 2))[:length]


def write_corpus(tmp_path: Path, docs: list[dict]) -> dict:
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "docs").mkdir(parents=True, exist_ok=True)
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({
        "signals": {"pii": {
            "instructions": "{zh}是什么？",
            "true": "包含{zh}",
            "false": "不包含{zh}",
            "types": {typ: {"zh": typ} for typ in PII_TYPES},
        }}
    }), encoding="utf-8")
    lines = []
    docs_by_id = {}
    for spec in docs:
        doc_id = spec["doc_id"]
        group = spec["group"]
        needle = spec.get("needle")
        total = int(spec.get("total", 4000))
        pos = float(spec.get("pos", 0.5))
        cache = spec.get("cache", "")
        marker = "NEEDLE_" + needle if needle else ""
        body = filler(total)
        if needle:
            cue = spec.get("cue", "")
            insert = cue + marker + " "
            # 让正文里也散布一些候选查询词（给 retrieval 一些可检索内容）
            body = ("邮箱 银行卡号 密码 身份证 地址 " * 3) + body
            cut = int(len(body) * pos)
            text = body[:cut] + insert + body[cut:]
        else:
            text = body
        index = text.index(marker) if marker else 0
        text_path = corpus_dir / "docs" / (doc_id + ".txt")
        text_path.write_text(text, encoding="utf-8")
        needles = ([{"type": needle, "lang": spec.get("lang", "zh"),
                     "pos": pos}] if needle else [])
        needle_index = ([{"type": needle, "lang": spec.get("lang", "zh"),
                          "pos": pos, "index": index,
                          "n_tokens": len(marker),
                          "cache_key": cache}]
                        if needle else [])
        row = {
            "doc_id": doc_id,
            "group": group,
            "base": 0,
            "target_tokens": total,
            "actual_tokens": len(text),
            "chars": len(text),
            "needles": needles,
            "needle_token_index": needle_index,
            "rule_pii_hits": ([needle] if needle == "phone_number" else []),
            "text_file": "docs/" + doc_id + ".txt",
            "sha256": eval_runner.chunk_eval.sha256_text(text),
            "doc_ids": list(range(100)),         # 重字段必须被 load_corpus 丢弃
        }
        lines.append(json.dumps(row, ensure_ascii=False))
        docs_by_id[doc_id] = row
    manifest = corpus_dir / "manifest.jsonl"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"docs_root": corpus_dir, "manifest": manifest,
            "questions": questions, "docs_by_id": docs_by_id}


def sample_docs() -> list[dict]:
    return [
        {"doc_id": "main_age_p10", "group": "main", "needle": "age",
         "pos": 0.1, "cue": "我的年龄是"},
        {"doc_id": "main_phone_number_p10", "group": "main",
         "needle": "phone_number", "pos": 0.1,
         "cue": "手机号是 13800138000，"},
        {"doc_id": "main_age_p50", "group": "main", "needle": "age",
         "pos": 0.5, "cue": "我的年龄是"},
        {"doc_id": "main_phone_number_p50", "group": "main",
         "needle": "phone_number", "pos": 0.5,
         "cue": "手机号是 13800138000，"},
        {"doc_id": "main_age_p90", "group": "main", "needle": "age",
         "pos": 0.9, "cue": "我的年龄是"},
        {"doc_id": "main_phone_number_p90", "group": "main",
         "needle": "phone_number", "pos": 0.9,
         "cue": "手机号是 13800138000，"},
        {"doc_id": "len_person_8k", "group": "len", "needle": "person",
         "pos": 0.5, "total": 8000, "cue": "联系人姓名是"},
        {"doc_id": "en_cn_id_card_p50", "group": "en", "needle": "cn_id_card",
         "pos": 0.5, "lang": "en", "cue": "身份证号是"},
        {"doc_id": "multi_0", "group": "multi", "needle": "cn_id_card",
         "pos": 0.5, "cue": "身份证号是"},
        {"doc_id": "clean_b0", "group": "clean"},
    ]


def make_questions_dict() -> dict:
    return {"signals": {"pii": {
        "instructions": "{zh}是什么？",
        "true": "包含{zh}",
        "false": "不包含{zh}",
        "types": {typ: {"zh": typ} for typ in PII_TYPES},
    }}}


class FakeScorer:
    """注入 scorer：只实现 runner 用到的 tokenizer/questions/_score/_render/info。"""

    def __init__(self, *, p_fn=None):
        self.tokenizer = FakeTokenizer()
        self.questions = make_questions_dict()
        self.batches: list[int] = []
        self.records: list[dict] = []
        self.closed = 0
        self.p_fn = p_fn or self.default_p_fn

    @staticmethod
    def default_p_fn(state: str, typ: str) -> float:
        return 0.9 if ("NEEDLE_" + typ) in state else 0.02

    def info(self) -> dict:
        return {
            "engine": "jev", "model_name": "fake-jev",
            "model_dir": "/fake/model", "temperature": 1.0,
            "max_length": 1024, "window": "1024:0",
            "questions_sha256": "q-sha-fake",
            "weights_sha256": "w" * 64, "head_sha256": "h" * 64,
            "tokenizer_sha256": "t" * 64, "chat_template_sha256": "c" * 64,
        }

    def _typ_of(self, record: dict) -> str:
        for typ in PII_TYPES:
            if typ in record["question"]:
                return typ
        raise AssertionError("fake record 缺类型: %r" % record["question"])

    def _render(self, records):
        return ["<prompt>" + record["state"] for record in records], [1] * len(records)

    def _score(self, records):
        self.batches.append(len(records))
        self.records.extend(records)
        out = []
        for record in records:
            typ = self._typ_of(record)
            value = float(self.p_fn(record["state"], typ))
            out.append([1.0 - value, value])
        return out

    async def aclose(self):
        self.closed += 1


def make_args(tmp_path: Path, corpus: dict, *extra: str):
    argv = [
        "--out", str(tmp_path / "run"),
        "--docs-root", str(corpus["docs_root"]),
        "--manifest", str(corpus["manifest"]),
        "--questions", str(corpus["questions"]),
        "--n-boot", "60",
        "--limit", "0",
        "--max-candidates", "4",
        "--chunk-size", "256",
        *extra,
    ]
    return eval_runner.parse_args(argv)


def _row_by_key(paths: dict) -> dict[tuple[str, str], dict]:
    rows = eval_runner.chunk_eval.read_jsonl(str(paths["raw"]))
    return {(row["arm"], row["doc_id"]): row for row in rows}


# --------------------------------------------------------------------------- #
# arms / CLI / dry-run
# --------------------------------------------------------------------------- #
def test_parse_arms() -> None:
    arms = eval_runner.parse_arms(eval_runner.DEFAULT_ARMS,
                                  max_candidates=8, chunk_size=512)
    names = [arm.name for arm in arms]
    assert names == list(eval_runner.ARM_NAMES)
    by_name = {arm.name: arm for arm in arms}
    assert by_name["head1024"].kind == "head"
    assert by_name["uniform_chunk8"].kind == "uniform"
    assert by_name["uniform_chunk8"].aggregation == "noisy_or"
    assert by_name["cand_rule"].use_rule is True
    assert by_name["cand_rule"].use_cue is False
    assert by_name["cand_rule_cue"].use_cue is True
    assert by_name["cand_rule_cue_retrieval"].use_retrieval is True
    assert by_name["cand_rule"].max_candidates == 8
    assert by_name["cand_rule"].chunk_size == 512
    with pytest.raises(ValueError):
        eval_runner.parse_arm("bogus")
    with pytest.raises(ValueError):
        eval_runner.parse_arms("head1024,head1024")


def test_parse_args_defaults_and_validation(tmp_path: Path) -> None:
    args = eval_runner.parse_args(["--out", str(tmp_path / "x")])
    assert args.stages == ["score", "report"]
    assert args.cal_target_fprs == [0.01, 0.02]
    assert args.max_candidates == 8
    report_only = eval_runner.parse_args(
        ["--out", str(tmp_path / "x"), "--stages", "report"])
    assert report_only.stages == ["report"]
    with pytest.raises(SystemExit):
        eval_runner.parse_args(["--out", str(tmp_path / "x"),
                                "--stages", "bogus"])
    with pytest.raises(SystemExit):
        eval_runner.parse_args(["--out", str(tmp_path / "x"),
                                "--arms", "bogus"])
    with pytest.raises(SystemExit):
        eval_runner.parse_args(["--out", str(tmp_path / "x"),
                                "--cal-target-fprs", "1.5"])
    with pytest.raises(SystemExit):
        eval_runner.parse_args(["--out", str(tmp_path / "x"),
                                "--score-batch", "0"])


def test_dry_run_does_not_touch_gpu_or_files(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main,clean",
                     "--arms", "head1024,cand_rule_cue")
    args.dry_run = True

    def no_scorer():
        raise AssertionError("dry-run 不应该构造 scorer")

    result = eval_runner.run_score(args, scorer_factory=no_scorer)
    assert result["dry_run"] is True
    assert result["docs"] == 7
    assert result["arms"] == ["head1024", "cand_rule_cue"]
    assert not Path(str(tmp_path / "run") + ".jsonl").exists()
    assert not Path(str(tmp_path / "run") + ".meta.json").exists()


# --------------------------------------------------------------------------- #
# score 阶段
# --------------------------------------------------------------------------- #
def test_score_stage_rows_meta_and_resume(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--score-batch", "3")
    scorer = FakeScorer()
    result = eval_runner.run_score(args, scorer_factory=lambda: scorer)
    rows = eval_runner.chunk_eval.read_jsonl(str(tmp_path / "run.jsonl"))
    assert result["rows"] == len(rows) == 5 * 10
    assert scorer.batches and max(scorer.batches) <= 3        # 批量 ≤ score_batch
    assert sum(scorer.batches) == len(scorer.records)
    by_key = _row_by_key(result["paths"])
    head = by_key[("head1024", "main_age_p50")]
    assert head["n_candidates"] == 1
    assert head["candidate_windows"][0]["reason"] == "head"
    assert head["asked_types"] == ["age"]                    # needle 只问自己
    uniform = by_key[("uniform_chunk8", "main_age_p50")]
    assert uniform["n_candidates"] == 8
    assert uniform["aggregation"] == "noisy_or"
    rule = by_key[("cand_rule", "main_phone_number_p50")]
    assert rule["candidate_windows"]
    assert {window["reason"] for window in rule["candidate_windows"]} == {"rule"}
    cue = by_key[("cand_rule_cue", "main_age_p50")]
    assert cue["candidate_windows"]
    p10 = by_key[("head1024", "main_age_p10")]
    assert sorted(p10["asked_types"]) == sorted(PII_TYPES)    # p10 全类型
    clean = by_key[("head1024", "clean_b0")]
    assert sorted(clean["asked_types"]) == sorted(PII_TYPES)  # clean 全类型
    assert p10["needle_candidate_recall"] == [True]
    assert clean["probs"] and set(clean["probs"]) == set(PII_TYPES)
    meta = json.loads(Path(str(tmp_path / "run") + ".meta.json").read_text(
        encoding="utf-8"))
    assert meta["corpus"]["manifest_sha256"]
    assert meta["model"]["questions_sha256"] == "q-sha-fake"
    assert meta["settings"]["max_candidates"] == 4
    assert [arm["name"] for arm in meta["arms"]] == list(
        eval_runner.ARM_NAMES)

    # resume：不重复打分（pending 为空），不报错
    fresh = FakeScorer()
    old_text = Path(str(tmp_path / "run") + ".jsonl").read_text(
        encoding="utf-8")
    eval_runner.run_score(args, scorer_factory=lambda: fresh)
    assert fresh.batches == []
    assert Path(str(tmp_path / "run") + ".jsonl").read_text(
        encoding="utf-8") == old_text

    # run 身份变化（seed）→ 拒绝
    changed = make_args(tmp_path, corpus, "--score-batch", "3", "--seed", "1")
    with pytest.raises(eval_runner.CandidateResumeMismatch) as excinfo:
        eval_runner.run_score(changed, scorer_factory=FakeScorer)
    assert "seed" in str(excinfo.value)
    # --no-resume：清空重写
    reset = make_args(tmp_path, corpus, "--score-batch", "3", "--no-resume")
    eval_runner.run_score(reset, scorer_factory=FakeScorer)
    assert len(eval_runner.chunk_eval.read_jsonl(
        str(tmp_path / "run.jsonl"))) == 5 * 10


def test_planning_does_not_receive_needle_types(tmp_path: Path,
                                                monkeypatch) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "cand_rule_cue_retrieval")
    calls = []
    original = eval_runner.candidates.plan_candidates

    def wrapper(*call_args, **call_kwargs):
        calls.append(dict(call_kwargs))
        return original(*call_args, **call_kwargs)

    monkeypatch.setattr(eval_runner.candidates, "plan_candidates", wrapper)
    eval_runner.run_score(args, scorer_factory=FakeScorer)
    assert calls
    for kwargs in calls:
        assert kwargs.get("types") is None          # 不把 needle 类型喂给候选生成
        assert kwargs.get("per_type") is False
        assert kwargs.get("context") is not None    # 每文档上下文复用


# --------------------------------------------------------------------------- #
# report / 指标
# --------------------------------------------------------------------------- #
def _score_and_report(tmp_path: Path, *extra: str):
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, *extra)
    scorer = FakeScorer()
    eval_runner.run_score(args, scorer_factory=lambda: scorer)
    report_args = eval_runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60", *extra])
    return corpus, args, eval_runner.run_report(report_args), scorer


def test_report_metrics_and_structure(tmp_path: Path) -> None:
    corpus, args, report, _scorer = _score_and_report(tmp_path)
    assert report["schema_version"] == eval_runner.SCHEMA_VERSION
    assert report["corpus"]["n_selected"] == 10
    assert report["corpus"]["manifest_sha256"]
    arms = report["arms"]
    assert set(arms) == set(eval_runner.ARM_NAMES)
    summary = {entry["arm"]: entry for entry in report["summary_table"]}
    # 候选召回 × 来源（rule/cue/format/retrieval），便于判断增益来自哪一层
    assert arms["cand_rule"]["candidate_sources"].get("rule", 0) > 0
    assert arms["cand_rule_cue"]["candidate_sources"].get("cue", 0) > 0
    # 候选召回：head 只看头窗（p50/p90 针剂在窗外）；cue 臂覆盖 cue 词附近
    assert summary["head1024"]["candidate_recall"] < 1.0
    assert summary["cand_rule_cue"]["candidate_recall"] == 1.0
    assert summary["cand_rule"]["candidate_recall"] == 3 / 9    # 仅 rule 命中 phone
    # 模型请求召回：head 对 p50/p90 = 0；cue 臂 = 1
    assert summary["head1024"]["recall_p50_raw"] == 0.0
    assert summary["head1024"]["recall_p90_raw"] == 0.0
    assert summary["cand_rule_cue"]["recall_p50_raw"] == 1.0
    assert summary["cand_rule_cue"]["recall_p90_raw"] == 1.0
    # clean FPR：clean 文档没有 cue/rule → 候选臂 0 窗 → raw FPR 0
    for name in ("cand_rule", "cand_rule_cue"):
        assert summary[name]["fpr_raw"] == 0.0
    # 成本：head 1 窗；uniform 8 窗；候选 ≤ max_candidates(4)
    assert summary["head1024"]["mean_windows_per_needle_doc"] == 1.0
    assert summary["uniform_chunk8"]["mean_windows_per_needle_doc"] == 8.0
    for name in ("cand_rule", "cand_rule_cue",
                 "cand_rule_cue_retrieval"):
        windows = summary[name]["mean_windows_per_needle_doc"]
        assert windows is None or windows <= 4.0
    assert summary["cand_rule_cue"]["prompt_tokens_vs_head"] is not None
    # 校准：只用 p10；两个 target 都有 fit
    calibration = arms["cand_rule_cue"]["calibration"]
    assert calibration["method"] == "p10-fullpack-fpr"
    assert set(calibration["fits"]) == {"0.01", "0.02"}
    assert calibration["p10_doc_ids"]
    for fit in calibration["fits"].values():
        assert "tau" in fit and "bias" in fit
    # 混淆矩阵 / ECE / AURC（router.eval_metrics）
    validation = arms["cand_rule_cue"]["validation"]
    for key in ("raw", "fpr_0.01", "fpr_0.02"):
        block = validation[key]["type_level"]
        for field in ("tp", "fp", "tn", "fn", "precision", "recall", "f1",
                      "fpr", "fdr", "ece", "aurc"):
            assert field in block
    # gate：仅候选臂有；cand_rule_cue 应满足合成门槛
    assert arms["head1024"].get("gate") is None
    assert arms["uniform_chunk8"].get("gate") is None
    gate = arms["cand_rule_cue"]["gate"]
    assert gate["mean_windows_per_needle_doc"] <= 8
    assert gate["fpr_0.01"]["passes"] is True
    # 结论 / 限制 / markdown 自动生成
    assert report["conclusions"] and report["limitations"]
    markdown = Path(str(tmp_path / "run") + ".report.md").read_text(
        encoding="utf-8")
    for needle in ("候选召回", "FPR≤1%", "门槛检查", "诚实边界", "校准",
                   "未手抄"):
        assert needle in markdown, needle
    assert "head1024" in markdown and "cand_rule_cue" in markdown


def test_candidate_recall_vs_model_recall_are_separate(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "cand_rule_cue")
    blind = FakeScorer(p_fn=lambda state, typ: 0.02)     # 模型永远低分
    eval_runner.run_score(args, scorer_factory=lambda: blind)
    report = eval_runner.run_report(eval_runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "40"]))
    arm = report["arms"]["cand_rule_cue"]
    assert arm["candidate_recall"]["recall"] == 1.0      # 候选到齐
    assert arm["buckets"]["main_all"]["model"]["raw"]["raw"]["recall"] == 0.0
    # 候选漏掉 vs 模型漏掉在报告里分列（doc-level 由 candidate/model 字段各自表达）
    markdown = Path(str(tmp_path / "run") + ".report.md").read_text(
        encoding="utf-8")
    assert "候选召回" in markdown and "模型请求召回" in markdown


def test_calibration_only_uses_p10(tmp_path: Path) -> None:
    corpus, args, report, _scorer = _score_and_report(tmp_path)
    fits_before = {
        arm: {key: (fit.get("tau"), fit.get("bias"))
              for key, fit in arm_report["calibration"]["fits"].items()}
        for arm, arm_report in report["arms"].items()}
    # 篡改所有非 p10 行（p50/p90 与 clean）的 request-level p
    raw_path = Path(str(tmp_path / "run") + ".jsonl")
    rows = eval_runner.chunk_eval.read_jsonl(str(raw_path))
    for row in rows:
        if eval_runner.chunk_eval.is_p10_doc(row):
            continue
        row["probs"] = {typ: 0.97 for typ in row.get("probs", {})}
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report2 = eval_runner.run_report(eval_runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "60"]))
    fits_after = {
        arm: {key: (fit.get("tau"), fit.get("bias"))
              for key, fit in arm_report["calibration"]["fits"].items()}
        for arm, arm_report in report2["arms"].items()}
    assert fits_before == fits_after                     # 阈值只用 p10 拟合
    # 但 validation 指标应变化（p50/p90 p=0.97 → 全命中）
    assert (report2["arms"]["cand_rule_cue"]["buckets"]["p50"]["model"]
            ["raw"]["raw"]["recall"] == 1.0)


def test_fit_fpr_threshold_and_reasons() -> None:
    feasible = eval_runner.fit_fpr_threshold([0.9, 0.8], [0.1, 0.2], 0.01)
    assert feasible["available"] is True
    assert feasible["recall"] == 1.0 and feasible["fpr"] == 0.0
    assert feasible["tau"] is not None
    no_threshold = eval_runner.fit_fpr_threshold([0.2], [0.9], 0.01)
    assert no_threshold["available"] is True
    assert no_threshold["degenerate_reason"] == "no_threshold_satisfies_fpr"
    empty = eval_runner.fit_fpr_threshold([], [0.1], 0.01)
    assert empty["available"] is False


def test_report_degenerate_reason_surfaces(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path, sample_docs())
    args = make_args(tmp_path, corpus, "--sets", "main",
                     "--arms", "uniform_chunk8")
    eval_runner.run_score(args, scorer_factory=FakeScorer)
    report = eval_runner.run_report(eval_runner.parse_args(
        ["--out", str(tmp_path / "run"), "--stages", "report",
         "--n-boot", "40"]))
    fits = report["arms"]["uniform_chunk8"]["calibration"]["fits"]
    reasons = {fit.get("degenerate_reason") for fit in fits.values()}
    assert "no_threshold_satisfies_fpr" in reasons
    markdown = Path(str(tmp_path / "run") + ".report.md").read_text(
        encoding="utf-8")
    assert "no_threshold_satisfies_fpr" in markdown
    assert any("no_threshold_satisfies_fpr" in bullet
               for bullet in report["conclusions"])


def test_report_stage_cpu_only_and_entrypoint_errors(tmp_path: Path) -> None:
    _corpus, _args, _report, _scorer = _score_and_report(tmp_path)
    assert "transformers" not in sys.modules
    assert "torch" not in sys.modules
    # report 缺 raw → 入口返回 2（一行错误，不打 traceback）
    missing = tmp_path / "missing"
    code = eval_runner.entrypoint(["--out", str(missing), "--stages",
                                   "report"])
    assert code == 2
    # 正常 dry-run 的入口返回 0（无 GPU/无产物）
    code = eval_runner.entrypoint([
        "--out", str(tmp_path / "dry"),
        "--docs-root", str(_corpus["docs_root"]),
        "--manifest", str(_corpus["manifest"]),
        "--questions", str(_corpus["questions"]),
        "--sets", "main", "--limit", "1", "--stages", "score",
        "--dry-run"])
    assert code == 0


def test_script_python310_compatible() -> None:
    executable = None
    for candidate in (str(Path.home() / ".local/bin/python3.11"),
                      "python3.11"):
        if Path(candidate).is_file() or shutil.which(candidate):
            executable = candidate
            break
    if executable is None:
        pytest.skip("本机没有 python3.11")
    subprocess.run([executable, "-m", "py_compile", str(SCRIPT_PATH)],
                   check=True, capture_output=True)
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "from __future__ import annotations" in source
