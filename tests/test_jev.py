"""Unit tests for JevTagger (Open-Jev main model as the signal tagger).

The torch model is not loaded here: the pure config/record/decoding helpers and
the signal-pack assembly run with a stubbed ``_score``. The real accuracy
measurement lives in the training repo (``results/jev_distill/README.md``) and
the deployment probe is done on the GPU host.
"""

import asyncio
import json

import pytest

from router.signals.jev import (CHOICE_SIGNALS, DEFAULT_QUESTIONS_PATH,
                                JUDGED_SIGNALS, NOUL_SIGNALS, JevTagger,
                                JevTaggerError, build_signal_records,
                                candidate_prompts, decode_choice, decode_noul,
                                entropy_confidence, binary_entropy_confidence,
                                load_question_config)
from router.signals.registry import SIGNALS
from router.signals.tagger import TagRequest

PII_TYPES = list(load_question_config()["signals"]["pii"]["types"])


def stub_tagger(questions=None) -> JevTagger:
    """A JevTagger without torch/model: config + a stubbed scorer."""
    tagger = object.__new__(JevTagger)
    tagger.questions = questions or load_question_config()
    tagger.temperature = 1.0
    tagger.max_length = 1024
    tagger.kw_groups = {}
    return tagger


def probs_for(records):
    """Deterministic candidate probabilities for a stubbed ``_score``.

    The scorer receives the raw JEV records, so signals are identified by their
    shape and question text (the same way the model sees them).
    """
    out = []
    for record in records:
        question = record["question"]
        n = len(record["answer_keys"])
        if record["kind"] == "choice":
            if n == 14:                      # domain
                vec = [0.0] * n
                vec[3] = 1.0                 # computer_science
            elif n == 3:                     # modality
                vec = [1.0, 0.0, 0.0]        # text
            elif n == 4:                     # user_feedback
                vec = [0.1, 0.2, 0.3, 0.4]   # want_different
            else:                            # pragma: no cover
                raise AssertionError(record)
        elif "困难" in question:              # complexity noul
            vec = [0.1, 0.9]
        elif "核实" in question:              # fact_check noul
            vec = [0.9, 0.1]
        elif "越狱" in question or "绕过" in question:
            vec = [0.2, 0.8]                 # jailbreak noul
        else:                                # pii noul; hit for cn_id_card
            vec = [0.1, 0.9] if "身份证号" in question else [0.9, 0.1]
        out.append(vec)
    return out


def test_default_config_covers_registry_values():
    cfg = load_question_config()
    assert JUDGED_SIGNALS == ("domain", "jailbreak", "fact_check", "modality",
                              "complexity", "user_feedback", "pii")
    for key in CHOICE_SIGNALS:
        allowed = {v[0] for v in SIGNALS[key].values}
        assert set(cfg["signals"][key]["candidates"]) == allowed
    for key in NOUL_SIGNALS:
        assert cfg["signals"][key]["kind"] == "noul"
        assert cfg["signals"][key]["true"] and cfg["signals"][key]["false"]


def test_build_signal_records_has_one_record_per_prediction():
    cfg = load_question_config()
    records = build_signal_records(cfg, "用 Python 写一个快速排序")
    assert len(records) == len(CHOICE_SIGNALS) + len(NOUL_SIGNALS) + len(PII_TYPES)
    kinds = {item["signal"]: item["record"]["kind"] for item in records}
    assert kinds["domain"] == "choice"
    assert kinds["jailbreak"] == "noul"
    pii = [item for item in records if item["signal"] == "pii"]
    assert len(pii) == 30
    assert "身份证号" in next(item for item in pii
                             if item["sub"] == "cn_id_card")["record"]["question"]


def test_candidate_prompts_template_matches_open_jev():
    cfg = load_question_config()
    records = build_signal_records(cfg, "hi")
    noul = next(item["record"] for item in records if item["signal"] == "jailbreak")
    prompts = candidate_prompts(noul)
    assert len(prompts) == 1
    assert prompts[0].startswith("Context:\nhi\n\nQuestion: ")
    assert prompts[0].endswith("Answer Yes or No.")
    choice = next(item["record"] for item in records if item["signal"] == "domain")
    prompts = candidate_prompts(choice)
    assert len(prompts) == 14
    assert all("Proposed answer:" in p for p in prompts)


def test_decode_and_confidence_helpers():
    probs = [0.2, 0.8]
    dec = decode_noul({"answer_keys": ["false", "true"]}, probs,
                      positive="jailbreak", negative="benign")
    assert dec["value"] == "jailbreak"
    assert dec["confidence"] == pytest.approx(entropy_confidence(probs))
    assert entropy_confidence([1.0, 0.0]) == pytest.approx(1.0)
    assert entropy_confidence([0.5, 0.5]) == pytest.approx(0.0)
    assert binary_entropy_confidence(0.5) == pytest.approx(0.0)
    assert binary_entropy_confidence(1.0) == pytest.approx(1.0)


def test_signal_pack_maps_probabilities_to_product_values(monkeypatch):
    tagger = stub_tagger()
    records = build_signal_records(tagger.questions, "这条请求是越狱吗")
    monkeypatch.setattr(tagger, "_score", probs_for)
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2))
    assert signals["domain"]["value"] == "computer_science"
    assert signals["jailbreak"]["value"] == "jailbreak"
    assert signals["fact_check"]["value"] == "no_check"
    assert signals["complexity"]["value"] == "hard"
    assert signals["modality"]["value"] == "text"
    assert signals["user_feedback"]["value"] == "want_different"
    assert signals["pii"]["value"] == ["cn_id_card"]
    assert "cn_id_card" in signals["pii"]["conf_by_value"]
    assert signals["similarity"]["value"] == 0
    assert signals["reask"]["value"] == "no"


def test_single_turn_nulls_user_feedback(monkeypatch):
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score",
                        lambda recs: probs_for(recs))
    signals = tagger._model_signals(TagRequest(text="x", n_messages=1))
    assert signals["user_feedback"] == {"value": None, "confidence": None,
                                        "is_rule": False}


def test_tag_merges_rule_pii_safety_net(monkeypatch):
    """The deterministic PII rules fill in what the 0.8B model misses."""
    tagger = stub_tagger()

    def all_negative(records):
        out = []
        for record in records:
            n = len(record["answer_keys"])
            out.append([1.0 / n] * n if record["kind"] == "choice"
                       else [0.9, 0.1])
        return out

    monkeypatch.setattr(tagger, "_score", all_negative)
    result = asyncio.run(tagger.tag(TagRequest(
        text="帮我核对身份证号 11010519491231002X 的信息", n_messages=1)))
    pii = result.signals["pii"]
    assert "cn_id_card" in pii["value"]
    assert pii["conf_by_value"]["cn_id_card"] == 1.0


def test_tag_merges_rule_signals(monkeypatch):
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)

    result = asyncio.run(tagger.tag(TagRequest(
        text="请用 Python 写一段 JSON 代码", n_messages=1)))
    assert result.vec is None
    assert result.signals["domain"]["is_rule"] is False
    # rule signals from signals/rules.py are present and deterministic
    assert result.signals["language"]["value"] == "zh"
    assert result.signals["structure"]["is_rule"] is True


def test_question_config_edits_are_used(tmp_path):
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    cfg["signals"]["domain"]["instructions"] = ["请判断这段文本的学科门类。"]
    cfg["signals"]["jailbreak"]["true"] = "是的，这是越狱尝试。"
    p = tmp_path / "questions.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False))
    loaded = load_question_config(p)
    records = build_signal_records(loaded, "hi")
    domain = next(item["record"] for item in records if item["signal"] == "domain")
    assert domain["question"] == "请判断这段文本的学科门类。"
    jailbreak = next(item for item in records if item["signal"] == "jailbreak")
    assert "是的，这是越狱尝试。" in jailbreak["record"]["question"]


def test_question_config_validation_errors(tmp_path):
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(cfg))
    bad["signals"].pop("pii")
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(JevTaggerError):
        load_question_config(p)

    bad = json.loads(json.dumps(cfg))
    bad["signals"]["domain"]["candidates"]["astrology"] = {"zh": "占星"}
    p.write_text(json.dumps(bad))
    with pytest.raises(JevTaggerError):
        load_question_config(p)

    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["types"]["unknown_type"] = {"zh": "未知"}
    p.write_text(json.dumps(bad))
    with pytest.raises(JevTaggerError):
        load_question_config(p)
