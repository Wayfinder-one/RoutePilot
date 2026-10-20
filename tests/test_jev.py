"""Unit tests for JevTagger (Open-Jev main model as the signal tagger).

The torch model is not loaded here: the pure config/record/decoding helpers and
the signal-pack assembly run with a stubbed ``_score``. The real accuracy
measurement lives in the training repo (``results/jev_distill/README.md``) and
the deployment probe is done on the GPU host.
"""

import asyncio
import json

import pytest

from router.pipeline.model import Station, TreeNode
from router.pipeline.scope import (compute_signal_scope, full_scope,
                                   SignalScope)
from router.signals.jev import (CHOICE_SIGNALS, DEFAULT_QUESTIONS_PATH,
                                JUDGED_SIGNALS, NOUL_SIGNALS, JevTagger,
                                JevTaggerError, build_signal_records,
                                candidate_prompts, decode_choice, decode_noul,
                                entropy_confidence, binary_entropy_confidence,
                                load_question_config)
from router.signals.registry import SIGNALS
from router.signals.tagger import TagRequest
from router.store.repositories import default_pipeline

PII_TYPES = list(load_question_config()["signals"]["pii"]["types"])
#: Full pack = 54 candidate prompts from 36 records: 3 choice records
#: (domain 14 + modality 3 + user_feedback 4 = 21 prompts), 3 noul records
#: (3 prompts) and 30 PII noul records (30 prompts).
FULL_RECORDS, FULL_PROMPTS = 36, 54
#: The deployed / default tree references jailbreak, pii.cn_id_card, domain and
#: complexity => 4 records / 17 candidate prompts.
TRIMMED_RECORDS, TRIMMED_PROMPTS = 4, 14 + 1 + 1 + 1


def prompt_count(records: list[dict]) -> int:
    return sum(len(candidate_prompts(item["record"])) for item in records)


def trimmed_scope():
    return compute_signal_scope(default_pipeline("checkpoint"))


def stub_tagger(questions=None) -> JevTagger:
    """A JevTagger without torch/model: config + a stubbed scorer."""
    tagger = object.__new__(JevTagger)
    tagger.questions = questions or load_question_config()
    tagger.temperature = 1.0
    tagger.max_length = 1024
    tagger.kw_groups = {}
    tagger.time_rules = {}
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


# --------------------------------------------------------------------------- #
# Pipeline-aware trimming: TagRequest.scope[ sign | pii subtype ] -> pack
# --------------------------------------------------------------------------- #
def _record_signals(records: list[dict]) -> dict[str, list[str | None]]:
    by_signal: dict[str, list[str | None]] = {}
    for item in records:
        by_signal.setdefault(item["signal"], []).append(item["sub"])
    return by_signal


def test_trimmed_records_come_from_the_pipeline_scope():
    """① the trimmed pack == the signals/subtypes the tree references."""
    scope = trimmed_scope()
    assert scope.trimmed
    records = build_signal_records(load_question_config(), "用 Python 写快排", scope)
    by_signal = _record_signals(records)
    assert set(by_signal) == {"domain", "jailbreak", "complexity", "pii"}
    assert by_signal["domain"] == [None]             # one choice record ...
    domain = next(i["record"] for i in records if i["signal"] == "domain")
    assert len(domain["options"]) == 14              # ... with every candidate
    assert by_signal["pii"] == ["cn_id_card"]
    assert len(records) == TRIMMED_RECORDS
    assert prompt_count(records) == TRIMMED_PROMPTS
    # unreferenced signals are not rendered at all
    assert {"fact_check", "modality", "user_feedback"}.isdisjoint(by_signal)


def test_unreferenced_signals_are_not_scored(monkeypatch):
    """② scores never see an unreferenced question (no dead GPU work)."""
    tagger = stub_tagger()
    calls: list[int] = []
    seen: list[dict] = []

    def capture(records):
        calls.append(len(records))
        seen.extend(records)
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    signals = tagger._model_signals(TagRequest(
        text="这条请求是越狱吗", n_messages=2, scope=trimmed_scope()))
    assert calls == [TRIMMED_RECORDS]
    questions = "\n".join(record["question"] for record in seen)
    cfg = tagger.questions["signals"]
    assert cfg["fact_check"]["instructions"][0] not in questions
    assert cfg["modality"]["instructions"][0] not in questions
    assert cfg["user_feedback"]["instructions"][0] not in questions
    assert set(signals) == {"jailbreak", "pii", "domain", "complexity",
                            "similarity", "reask"}


def test_full_and_trimmed_packs_agree_on_every_referenced_value(monkeypatch):
    """⑤ values/confidences the pipeline can read are identical to the full pack."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    text = "这条请求是越狱吗，我的身份证号也要核对"
    full = tagger._model_signals(TagRequest(text=text, n_messages=2))
    trimmed = tagger._model_signals(TagRequest(text=text, n_messages=2,
                                               scope=trimmed_scope()))
    for key in ("jailbreak", "domain", "complexity"):
        assert trimmed[key] == full[key]
    # PII: referenced subtype membership + per-value confidence match; only the
    # unreferenced subtypes are missing from the trimmed pack.
    assert trimmed["pii"]["conf_by_value"].get("cn_id_card") == \
        full["pii"]["conf_by_value"].get("cn_id_card")
    assert ("cn_id_card" in trimmed["pii"]["value"]) == \
        ("cn_id_card" in full["pii"]["value"])
    assert trimmed["similarity"] == full["similarity"]
    assert trimmed["reask"] == full["reask"]
    other_types = [typ for typ in full["pii"]["value"] if typ != "cn_id_card"]
    for typ in other_types:                      # expected omissions only
        assert typ not in trimmed["pii"]["value"]


def test_unknown_reference_falls_back_to_the_full_pack(monkeypatch):
    """④ a tree the extractor cannot reason about scores all 54 candidates."""
    tagger = stub_tagger()
    calls: list[int] = []

    def capture(records):
        calls.append(len(records))
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    weird = [Station(id="s", type="gate", name="怪", root=TreeNode(
        id="g", signal=None, kind="fork", children=[
            TreeNode(id="b", signal="telepathy", value="yes", kind="leaf",
                     action="route", model="qwen3-8b")]))]
    scope = compute_signal_scope(weird)
    assert scope.full
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2,
                                               scope=scope))
    assert calls == [FULL_RECORDS]
    assert set(JUDGED_SIGNALS) <= set(signals)


def test_hot_pipeline_edit_widens_scope_and_pack():
    """③ a new question/candidate in the tree is picked up automatically."""
    stations = default_pipeline("checkpoint")
    before = compute_signal_scope(stations)
    assert "fact_check" not in before.signals
    new_station = Station(id="st-fc", type="gate", name="需核实", root=TreeNode(
        id="n-fc", signal=None, kind="fork", children=[
            TreeNode(id="c-fc", signal="fact_check", value="needs_check",
                     conf_min=0.5, kind="leaf", action="route",
                     model="qwen3-8b")]))
    after = compute_signal_scope(stations[:-1] + [new_station] + stations[-1:])
    assert "fact_check" in after.signals
    records = build_signal_records(load_question_config(), "x", after)
    assert any(item["signal"] == "fact_check" for item in records)
    # narrowing again (removing the station) shrinks the pack back
    narrowed = compute_signal_scope(stations)
    assert "fact_check" not in narrowed.signals
    assert len(build_signal_records(load_question_config(), "x", narrowed)) == \
        TRIMMED_RECORDS


def test_hot_question_config_edit_is_used_by_the_trimmed_pack():
    """③ config edits (candidate/PII wording) flow into the trimmed render."""
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    seen = load_question_config()
    scope = trimmed_scope()

    # Add a candidate set edit for a referenced choice signal: all its configured
    # candidates are rendered, so a config change is visible immediately even
    # though the tree only references health/computer_science.
    shortened = json.loads(json.dumps(seen))
    domain = shortened["signals"]["domain"]["candidates"]
    keep = dict(list(domain.items())[:2])
    shortened["signals"]["domain"]["candidates"] = keep
    tagger = stub_tagger()
    tagger.set_questions(shortened)      # validated exactly like the admin PUT
    records = build_signal_records(tagger.questions, "x", scope)
    domain_record = next(i["record"] for i in records if i["signal"] == "domain")
    assert len(domain_record["options"]) == 2

    # A PII type removed from the config cannot be asked (same as the full pack);
    # adding it back makes the very next render include it.
    reduced = json.loads(json.dumps(cfg))
    reduced["signals"]["pii"]["types"].pop("cn_id_card")
    tagger.set_questions(reduced)
    records = build_signal_records(tagger.questions, "x", scope)
    assert not [item for item in records if item["signal"] == "pii"]
    tagger.set_questions(cfg)
    records = build_signal_records(tagger.questions, "x", scope)
    assert [item["sub"] for item in records if item["signal"] == "pii"] == \
        ["cn_id_card"]


def test_single_turn_user_feedback_question_is_dead_compute(monkeypatch):
    """The single-turn user_feedback value is nulled anyway => never asked."""
    tagger = stub_tagger()
    calls: list[int] = []

    def capture(records):
        calls.append(len(records))
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    tagger._model_signals(TagRequest(text="x", n_messages=2))
    assert calls == [FULL_RECORDS]
    signals = tagger._model_signals(TagRequest(text="x", n_messages=1))
    assert calls == [FULL_RECORDS, FULL_RECORDS - 1]      # user_feedback record
    assert signals["user_feedback"] == {"value": None, "confidence": None,
                                        "is_rule": False}


def test_empty_scope_computes_no_model_questions(monkeypatch):
    """A rule-only tree needs no JEV forward at all (still returns constants)."""
    tagger = stub_tagger()
    calls: list[list] = []

    def capture(records):
        calls.append(records)
        return []

    monkeypatch.setattr(tagger, "_score", capture)
    signals = tagger._model_signals(TagRequest(
        text="x", n_messages=2, scope=SignalScope()))
    assert calls == [[]]
    assert signals == {
        "similarity": {"value": 0, "confidence": None, "is_rule": False},
        "reask": {"value": "no", "confidence": None, "is_rule": False},
    }
