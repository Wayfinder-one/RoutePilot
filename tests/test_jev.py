"""Unit tests for JevTagger (Open-Jev main model as the signal tagger).

The torch model is not loaded here: the pure config/record/decoding helpers and
the signal-pack assembly run with a stubbed ``_score``. The real accuracy
measurement lives in the training repo (``results/jev_distill/README.md``) and
the deployment probe is done on the GPU host.
"""

import asyncio
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest

from router.pipeline.model import Station, TreeNode
from router.pipeline.scope import (compute_signal_scope, full_scope,
                                   SignalScope)
from router.signals.jev import (CHOICE_SIGNALS, DEFAULT_QUESTIONS_PATH,
                                JUDGED_SIGNALS, NOUL_SIGNALS,
                                PROMPT_OVERHEAD_SLACK, WINDOW_MARKER,
                                JevTagger, JevTaggerError, build_signal_records,
                                candidate_prompts, decode_choice, decode_noul,
                                default_window_spec, entropy_confidence,
                                binary_entropy_confidence, head_window,
                                load_question_config, parse_window,
                                questions_sha256, validate_question_config,
                                window_text, chat_template_sha256,
                                model_file_hashes, sha256_file, sha256_files,
                                load_chunk_calibration)
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


class _FakeNumber:
    """The tiny tensor API ``_encode``/``_score`` touch (sum/max/item)."""

    def __init__(self, value):
        self.value = value

    def max(self):
        return self

    def item(self):
        return self.value


class _FakeVector:
    """Per-row lengths tensor: ``tolist`` feeds the token budgeting in
    ``_score``, ``max().item()`` the prompt-cap check in ``_encode``."""

    def __init__(self, values):
        self.values = values

    def max(self):
        return _FakeNumber(max(self.values))

    def tolist(self):
        return list(self.values)


class _FakeMask:
    def __init__(self, lengths):
        self.lengths = lengths

    def __getitem__(self, item):
        # _score row-slices the encoded batch into budgeted chunks
        lengths = self.lengths[item]
        return _FakeMask(lengths if isinstance(lengths, list) else [lengths])

    def sum(self, dim=None):
        if dim in (-1, 1):
            return _FakeVector(self.lengths)
        return _FakeNumber(sum(self.lengths))


class FakeTokenizer:
    """Char-level stand-in for the Qwen tokenizer (tests run without torch).

    One character is one token (``ord`` id), so a cap of N tokens is N
    characters and ``decode`` is the exact inverse. The batch call returns the
    two shapes ``_score``/``_encode`` need; ``apply_chat_template`` renders the
    prompt content 1:1, so template overhead is just the question text here.
    """

    padding_side = "right"
    truncation_side = "right"
    pad_token_id = 0
    pad_token = "<pad>"
    eos_token = "<eos>"

    def __call__(self, text, add_special_tokens=True, padding=False,
                 truncation=False, max_length=None, return_tensors=None):
        if isinstance(text, str):
            return {"input_ids": [ord(c) for c in text]}
        rows = [[ord(c) for c in prompt] for prompt in text]
        if truncation and max_length is not None:
            rows = [row[:max_length] for row in rows]
        return {"input_ids": rows,
                "attention_mask": _FakeMask([len(row) for row in rows])}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(chr(i) for i in ids)

    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=True, enable_thinking=False):
        return "".join(message["content"] for message in messages)


def prompt_count(records: list[dict]) -> int:
    return sum(len(candidate_prompts(item["record"])) for item in records)


#: Minimal torch stand-in so the REAL JevTagger.__init__ can run without torch:
#: only ``cuda.is_available`` is touched before the injected model/tokenizer.
class _FakeTorch:
    class cuda:
        @staticmethod
        def is_available():
            return False


class _FakeConfig:
    def __init__(self):
        self.use_cache = True
        self.hidden_size = 8


class _FakeBackbone:
    def __init__(self):
        self.config = _FakeConfig()

    def eval(self):
        return self


class _FakeBackboneHolder:
    def __init__(self):
        self.language_model = _FakeBackbone()


class _FakeModel:
    def __init__(self):
        self.model = _FakeBackboneHolder()


class _FakeHead:
    def eval(self):
        return self


def _write_jev_model_dir(tmp_path, max_length=1024) -> Path:
    """model.json + head.pt so the real __init__ passes its file checks."""
    d = tmp_path / f"jev-model-{len(list(tmp_path.iterdir()))}"
    d.mkdir()
    (d / "model.json").write_text(json.dumps({"max_length": max_length}))
    (d / "head.pt").write_bytes(b"")
    return d


def real_tagger(tmp_path, monkeypatch, *, model_max_length=1024, **kwargs):
    """A JevTagger built by the REAL __init__ with an injected fake model."""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    model_dir = _write_jev_model_dir(tmp_path, model_max_length)

    def factory(_model_dir, _device):
        return _FakeModel(), FakeTokenizer(), _FakeHead()

    return JevTagger(str(model_dir), model_factory=factory, warmup=False,
                     **kwargs)


def trimmed_scope():
    return compute_signal_scope(default_pipeline())


def stub_tagger(questions=None, tokenizer=None, max_length=1024,
                window=None, chunk_segments=0, chunk_size=512,
                chunk_agg="noisy_or", chunk_calibration=None) -> JevTagger:
    """A JevTagger without torch/model: config + a stubbed scorer."""
    tagger = object.__new__(JevTagger)
    tagger.questions = questions or load_question_config()
    tagger.temperature = 1.0
    tagger.model_dir = Path("/models/jev-test")
    tagger.max_length = max_length
    # None mirrors a production tagger built with ROUTER_JEV_WINDOW unset: the
    # real __init__ derives the legacy head-only "<max_length>:0" window, so a
    # shrunk max_length stub gets the matching spec.
    tagger.window_spec = (window if window is not None
                          else default_window_spec(max_length))
    tagger.window = parse_window(tagger.window_spec, max_length)
    tagger.prompt_max_length = tagger.max_length + PROMPT_OVERHEAD_SLACK
    tagger.tokenizer = tokenizer or FakeTokenizer()
    tagger.kw_groups = {}
    tagger.time_periods = {}
    # Chunking defaults to off, exactly like the real constructor.
    tagger.chunk_segments = chunk_segments
    tagger.chunk_size = chunk_size
    tagger.chunk_agg = chunk_agg
    tagger.chunk_enabled = chunk_segments > 0
    tagger.chunk_overlap = 0.25
    tagger.chunk_calibration, tagger._chunk_calib_desc = \
        load_chunk_calibration(chunk_calibration)
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


def test_noul_neutral_tie_decodes_negative():
    """2026-10-21 audit P2 4.2: p_yes == 0.5 (raw logit 0, no evidence) must
    not decode as the positive class — for jailbreak that class intercepts."""
    dec = decode_noul({"answer_keys": ["false", "true"]}, [0.5, 0.5],
                      positive="jailbreak", negative="benign")
    assert dec["value"] == "benign"
    assert dec["confidence"] == pytest.approx(0.0)
    assert dec["p_yes"] == pytest.approx(0.5)
    assert dec["margin"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Full distributions (2026-10-24 item 1.1: record-only extra keys)
# --------------------------------------------------------------------------- #
def test_choice_decode_records_the_full_distribution():
    probs = [0.05, 0.1, 0.4, 0.45]
    dec = decode_choice({"answer_keys": ["a", "b", "c", "d"]}, probs)
    # decision keys unchanged
    assert dec["value"] == "d"
    assert dec["confidence"] == pytest.approx(entropy_confidence(probs))
    assert dec["is_rule"] is False
    # record-only keys: full distribution, configured candidate order
    assert list(dec["prob_by_value"]) == ["a", "b", "c", "d"]
    assert dec["prob_by_value"] == {"a": 0.05, "b": 0.1, "c": 0.4, "d": 0.45}
    assert dec["p_top"] == pytest.approx(0.45)
    assert dec["margin"] == pytest.approx(0.05)


def test_noul_decode_records_the_full_distribution():
    dec = decode_noul({"answer_keys": ["false", "true"]}, [0.2, 0.8],
                      positive="jailbreak", negative="benign")
    assert dec["value"] == "jailbreak"
    assert dec["confidence"] == pytest.approx(entropy_confidence([0.2, 0.8]))
    assert dec["p_yes"] == pytest.approx(0.8)
    assert dec["prob_by_value"] == {"false": 0.2, "true": 0.8}
    assert dec["margin"] == pytest.approx(0.6)


def test_pii_weak_candidates_are_not_hits(monkeypatch):
    """2026-10-21 audit P2 4.2: a PII candidate must be strictly above neutral
    AND carry PII_MIN_CONFIDENCE; barely-positive p_yes is noise (its ~0
    confidence could never pass a condition floor anyway)."""
    tagger = stub_tagger()

    def soft_yes(records):
        out = []
        for record in records:
            n = len(record["answer_keys"])
            if record["kind"] == "choice":
                out.append([1.0 / n] * n)
            elif "身份证号" in record["question"]:
                out.append([0.4, 0.6])       # > 0.5 but conf ~0.03 < 0.2
            else:
                out.append([0.9, 0.1])
        return out

    monkeypatch.setattr(tagger, "_score", soft_yes)
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2))
    assert signals["pii"]["value"] == []
    assert signals["pii"]["conf_by_value"] == {}
    # ... the pure-model distribution keeps the weak positive, but it is not a
    # model hit (2026-10-24 P1-A evidence split)
    assert signals["pii"]["model_p_by_type"]["cn_id_card"] == pytest.approx(0.6)
    assert signals["pii"]["model_hits"] == []


def test_pii_model_evidence_covers_every_scored_type(monkeypatch):
    """full pack: one model p_yes per scored subtype (30); hits stay separate."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2))
    pii = signals["pii"]
    assert set(pii["model_p_by_type"]) == set(PII_TYPES)
    assert len(pii["model_p_by_type"]) == 30
    assert pii["model_p_by_type"]["cn_id_card"] == pytest.approx(0.9)
    assert pii["model_p_by_type"]["phone_number"] == pytest.approx(0.1)
    assert pii["model_hits"] == ["cn_id_card"]
    # pre-merge decision value/conf are the pure-model ones; rule evidence is
    # attached by tag() (here we call _model_signals directly)
    assert pii["value"] == ["cn_id_card"]
    assert set(pii["conf_by_value"]) == {"cn_id_card"}


def test_pii_trimmed_scope_keeps_model_subset_and_full_text_rule_hits(
        monkeypatch):
    """trimmed scope: model evidence only for scored subtypes, but the regex
    layer still scans the FULL text — the scope must never drop rule evidence."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    text = ("联系我 someone@example.com，密码: abc123，"
            "身份证号 11010519491231002X")
    result = asyncio.run(tagger.tag(TagRequest(
        text=text, n_messages=2, scope=trimmed_scope())))
    pii = result.signals["pii"]
    assert pii["model_p_by_type"] == {"cn_id_card": pytest.approx(0.9)}
    assert set(pii["model_hits"]) <= {"cn_id_card"}
    assert {"email_address", "password"}.issubset(set(pii["rule_hits"]))
    assert "cn_id_card" in pii["rule_hits"]      # valid checksum ID
    assert {"cn_id_card", "email_address", "password"} <= set(pii["value"])
    assert pii["conf_by_value"]["email_address"] == 1.0


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


def test_signal_pack_carries_the_distribution_fields(monkeypatch):
    """item 1.1: choice/noul signal dicts expose the full distribution too."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2))

    domain = signals["domain"]
    assert domain["p_top"] == pytest.approx(1.0)          # fake one-hot
    assert domain["margin"] == pytest.approx(1.0)
    assert len(domain["prob_by_value"]) == 14
    assert domain["prob_by_value"]["computer_science"] == pytest.approx(1.0)

    modality = signals["modality"]
    assert modality["prob_by_value"] == {"text": 1.0, "image": 0.0,
                                         "text_image": 0.0}
    assert modality["margin"] == pytest.approx(1.0)

    user_feedback = signals["user_feedback"]
    assert list(user_feedback["prob_by_value"].values()) == [0.1, 0.2, 0.3, 0.4]
    assert user_feedback["p_top"] == pytest.approx(0.4)
    assert user_feedback["margin"] == pytest.approx(0.1)

    for key, (p_no, p_yes) in (("jailbreak", (0.2, 0.8)),
                               ("complexity", (0.1, 0.9)),
                               ("fact_check", (0.9, 0.1))):
        sig = signals[key]
        assert sig["p_yes"] == pytest.approx(p_yes)
        assert sig["prob_by_value"] == {"false": p_no, "true": p_yes}
        assert sig["margin"] == pytest.approx(abs(p_yes - p_no))


def test_decision_fields_keep_the_pre_distribution_semantics(monkeypatch):
    """item 1.1: new keys are additive — value/confidence are bit-identical.

    Recompute the old formulas from the same stubbed probabilities and compare.
    """
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    signals = tagger._model_signals(TagRequest(text="x", n_messages=2))
    # choice: argmax over the configured candidate keys + entropy confidence
    assert signals["user_feedback"]["value"] == "want_different"
    uf = [0.1, 0.2, 0.3, 0.4]
    h = -sum(p * math.log(p) for p in uf)
    assert signals["user_feedback"]["confidence"] == pytest.approx(
        1.0 - h / math.log(4))
    # noul: strictly > 0.5 decodes positive, binary entropy confidence
    assert signals["jailbreak"]["value"] == "jailbreak"
    assert signals["fact_check"]["value"] == "no_check"
    assert signals["jailbreak"]["confidence"] == pytest.approx(
        entropy_confidence([0.2, 0.8]))


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
    # effective decision signal: model (empty here) unioned with the rule hit
    assert pii["value"] == ["cn_id_card"]
    assert pii["conf_by_value"]["cn_id_card"] == 1.0
    assert pii["is_rule"] is True                # only the rule contributed
    # P1-A: the rule NEVER rewrites the model evidence...
    assert set(pii["model_p_by_type"]) == set(PII_TYPES)
    assert pii["model_p_by_type"]["cn_id_card"] == pytest.approx(0.1)
    assert pii["model_p_by_type"]["phone_number"] == pytest.approx(0.1)
    assert pii["model_hits"] == []
    assert pii["rule_hits"] == ["cn_id_card"]
    assert set(pii["conf_by_value"]) == {"cn_id_card"}


def test_pii_overlap_keeps_the_raw_model_probability(monkeypatch):
    """overlap: both layers hit cn_id_card; the effective confidence is the
    rule's 1.0 but model_p_by_type keeps the raw softmax (never 1.0)."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    result = asyncio.run(tagger.tag(TagRequest(
        text="帮我核对身份证号 11010519491231002X", n_messages=1)))
    pii = result.signals["pii"]
    assert pii["model_hits"] == ["cn_id_card"]
    assert pii["rule_hits"] == ["cn_id_card"]
    assert pii["model_p_by_type"]["cn_id_card"] == pytest.approx(0.9)
    assert pii["conf_by_value"]["cn_id_card"] == 1.0
    assert pii["is_rule"] is False


def test_rule_pii_hits_are_recorded_even_when_pii_is_not_scored(monkeypatch):
    """A trimmed-away pii question still gets its deterministic hits; the model
    evidence stays empty (scope did not score pii) — the layers never mix."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", lambda records: [])
    result = asyncio.run(tagger.tag(TagRequest(
        text="帮我核对身份证号 11010519491231002X", n_messages=2,
        scope=SignalScope())))
    pii = result.signals["pii"]
    assert pii["value"] == ["cn_id_card"]
    assert pii["conf_by_value"] == {"cn_id_card": 1.0}
    assert pii["is_rule"] is True
    assert pii["model_p_by_type"] == {}
    assert pii["model_hits"] == []
    assert pii["rule_hits"] == ["cn_id_card"]


def test_merge_rule_pii_does_not_touch_model_probabilities(monkeypatch):
    """A1 acceptance: the model probability map is bit-identical before/after
    merge_rule_pii, for the full and the trimmed pack."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    text = "帮我核对身份证号 11010519491231002X，请联系 someone@example.com"
    for scope in (None, trimmed_scope()):
        req = TagRequest(text=text, n_messages=1, scope=scope)
        pre = tagger._model_signals(req)["pii"]
        post = asyncio.run(tagger.tag(req)).signals["pii"]
        assert post["model_p_by_type"] == pre["model_p_by_type"]   # 逐位
        assert post["model_hits"] == pre["model_hits"]
        assert set(post["rule_hits"]) >= {"cn_id_card", "email_address"}


def test_pii_three_layers_have_different_confusion_matrices():
    """A1 acceptance: model / rule / union are independent layers and their
    confusion matrices differ on the same gold set."""
    from router.eval_metrics import Confusion

    # (gold present, model p_yes, regex hit)
    cases = [
        (True, 0.90, True),    # T1 overlap: both hit
        (True, 0.10, True),    # T2 model miss + rule hit
        (False, 0.10, True),   # T3 rule false positive (model correctly no)
        (True, 0.10, False),   # T4 both miss
        (False, 0.10, False),  # T5 true negative
        (True, 0.90, False),   # T6 model-only hit
    ]
    gold = [case[0] for case in cases]
    model = Confusion.from_predictions(gold, [p > 0.5 for _, p, _ in cases])
    rule = Confusion.from_predictions(gold, [r for _, _, r in cases])
    union = Confusion.from_predictions(
        gold, [p > 0.5 or r for _, p, r in cases])
    assert (model.tp, model.fp, model.tn, model.fn) == (2, 0, 2, 2)
    assert (rule.tp, rule.fp, rule.tn, rule.fn) == (2, 1, 1, 2)
    assert (union.tp, union.fp, union.tn, union.fn) == (3, 1, 1, 1)
    assert model.fpr == 0.0 and rule.fpr == pytest.approx(0.5)
    assert union.recall > model.recall and union.fdr > model.fdr


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


# --------------------------------------------------------------------------- #
# Input window (2026-10-21 head-only; configurable head/tail 2026-10-24)
# --------------------------------------------------------------------------- #
def test_head_window_keeps_the_head_tokens():
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    head, stats = head_window(text, tok, 1024)
    assert head == text[:1024]
    assert stats == {"input_tokens": 3000, "input_visible_tokens": 1024,
                     "input_truncated": True, "input_window": "1024:0"}


def test_head_window_leaves_short_text_untouched():
    tok = FakeTokenizer()
    text = "你好，帮我写快排"
    head, stats = head_window(text, tok, 1024)
    assert head == text
    assert stats == {"input_tokens": len(text),
                     "input_visible_tokens": len(text),
                     "input_truncated": False, "input_window": "1024:0"}


def test_head_window_repairs_reencode_and_partial_char():
    class Expanding(FakeTokenizer):
        def decode(self, ids, skip_special_tokens=False):
            return "x" * (len(ids) + 3)       # decode grows after re-encoding

    head, stats = head_window("a" * 100, Expanding(), 10)
    assert len(head) <= 10
    assert stats["input_visible_tokens"] <= 10

    class PartialChar(FakeTokenizer):
        def decode(self, ids, skip_special_tokens=False):
            out = "".join(chr(i) for i in ids)
            return out + "\ufffd" if ids and ids[-1] % 2 else out

    text = "".join(chr(i) for i in range(0, 100))
    head, stats = head_window(text, PartialChar(), 10)
    assert "\ufffd" not in head             # cut only between whole chars
    assert stats["input_visible_tokens"] <= 10


# --------------------------------------------------------------------------- #
# Configurable head/tail window (2026-10-24 item 2, env ROUTER_JEV_WINDOW)
# --------------------------------------------------------------------------- #
def test_default_window_spec_and_marker_match_the_4k_simulation():
    """No window configured = legacy head-only at max_length (deployed 1024:0)."""
    assert default_window_spec(1024) == "1024:0"
    assert default_window_spec(512) == "512:0"
    assert WINDOW_MARKER == "\n\n[...]\n\n"


def test_constructor_window_none_derives_the_legacy_head_only_spec(tmp_path,
                                                                  monkeypatch):
    """Follow-up to task-2, through the REAL __init__: a custom max_length with
    ROUTER_JEV_WINDOW unset (window=None) keeps the legacy head-only window and
    never fails startup on the literal 1024 default."""
    tagger = real_tagger(tmp_path, monkeypatch)      # model.json max_length=1024
    assert tagger.max_length == 1024
    assert tagger.window_spec == "1024:0"
    assert tagger.window == (1024, 0)
    assert tagger.info()["window"] == "1024:0"

    tagger = real_tagger(tmp_path, monkeypatch, max_length=512)
    assert tagger.max_length == 512
    assert tagger.window_spec == "512:0"
    assert tagger.window == (512, 0)
    assert tagger.info()["window"] == "512:0"
    text = "".join(chr(0x4E00 + i) for i in range(1000))
    windowed, stats = tagger._context_window(text)
    old_text, old_stats = head_window(text, tagger.tokenizer, 512)
    assert windowed == old_text                    # == old head_window(512)
    for key in ("input_tokens", "input_visible_tokens", "input_truncated"):
        assert stats[key] == old_stats[key]
    assert stats["input_window"] == "512:0"

    # an explicit window still wins over the legacy default
    tagger = real_tagger(tmp_path, monkeypatch, window="256:768")
    assert tagger.window_spec == "256:768" and tagger.window == (256, 768)
    assert tagger.info()["window"] == "256:768"

    # an invalid explicit spec refuses startup (JevTaggerError)
    with pytest.raises(JevTaggerError):
        real_tagger(tmp_path, monkeypatch, window="0:0")


def test_parse_window_accepts_valid_specs():
    assert parse_window("1024:0", 1024) == (1024, 0)
    assert parse_window("0:1024", 1024) == (0, 1024)
    assert parse_window("256:768", 1024) == (256, 768)
    assert parse_window("512:512", 1024) == (512, 512)
    # head+tail is a cap, not a quota: a smaller sum is a valid window
    assert parse_window("100:200", 1024) == (100, 200)
    assert parse_window(" 64: 128 ", 1024) == (64, 128)


@pytest.mark.parametrize("spec", ["", "1024", "1024:0:0", "a:1", "1:b",
                                  "1.5:2", "-1:1024", "1024:-1", "0:0",
                                  "1025:0", "600:600"])
def test_parse_window_rejects_invalid_specs(spec):
    """A bad ROUTER_JEV_WINDOW must refuse startup (JevTaggerError), never
    silently score a different window than the operator configured."""
    with pytest.raises(JevTaggerError):
        parse_window(spec, 1024)


def test_window_text_short_text_is_verbatim_for_every_window():
    """Texts that already fit max_length are arm-independent (plan §2.2a)."""
    tok = FakeTokenizer()
    text = "你好，帮我写快排"
    for head, tail in ((1024, 0), (0, 1024), (256, 768), (512, 512)):
        windowed, stats = window_text(text, tok, 1024, head, tail)
        assert windowed == text
        assert stats == {"input_tokens": len(text),
                         "input_visible_tokens": len(text),
                         "input_truncated": False,
                         "input_window": f"{head}:{tail}"}


def test_window_text_head_only_matches_old_head_window():
    """The 5000-token default arm is byte-identical to the old head_window."""
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(5000))
    windowed, stats = window_text(text, tok, 1024, 1024, 0)
    old_text, old_stats = head_window(text, tok, 1024)
    assert windowed == old_text == text[:1024]
    for key in ("input_tokens", "input_visible_tokens", "input_truncated"):
        assert stats[key] == old_stats[key]
    assert stats["input_window"] == "1024:0"
    assert head_window(text, tok, 1024)[1]["input_window"] == "1024:0"


def test_window_text_tail_only_keeps_the_tail_without_a_marker():
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    windowed, stats = window_text(text, tok, 1024, 0, 1024)
    assert windowed == text[-1024:]
    assert WINDOW_MARKER not in windowed
    assert stats["input_visible_tokens"] == 1024
    assert stats["input_truncated"] is True
    assert stats["input_window"] == "0:1024"


def test_window_text_mixed_keeps_head_and_tail_with_the_4k_marker():
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    windowed, stats = window_text(text, tok, 1024, 256, 768)
    assert windowed == text[:256] + WINDOW_MARKER + text[-768:]
    assert stats["input_visible_tokens"] == 256 + 768
    assert stats["input_truncated"] is True
    assert stats["input_window"] == "256:768"


@pytest.mark.parametrize("head,tail", [(1024, 0), (0, 1024), (256, 768),
                                       (512, 512)])
def test_window_text_arms_are_ufffd_free_and_within_budget(head, tail):
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    windowed, stats = window_text(text, tok, 1024, head, tail)
    assert "\ufffd" not in windowed
    content = windowed.replace(WINDOW_MARKER, "")
    assert len(tok(content)["input_ids"]) <= head + tail
    assert stats["input_visible_tokens"] <= head + tail
    assert stats["input_window"] == f"{head}:{tail}"


def test_window_text_repairs_partial_chars_at_both_ends():
    text = "".join(chr(i) for i in range(2, 101))     # 99 tokens, even ends

    class PartialEnds(FakeTokenizer):
        """decode reports a partial byte when a cut boundary is an odd id."""
        def decode(self, ids, skip_special_tokens=False):
            out = "".join(chr(i) for i in ids)
            if ids and ids[0] % 2:
                out = "\ufffd" + out
            if ids and ids[-1] % 2:
                out += "\ufffd"
            return out

    head, stats = window_text(text, PartialEnds(), 10, 10, 0)
    assert "\ufffd" not in head and stats["input_visible_tokens"] <= 10
    tail, stats = window_text(text, PartialEnds(), 10, 0, 10)
    assert "\ufffd" not in tail and stats["input_visible_tokens"] <= 10
    mixed, stats = window_text(text, PartialEnds(), 16, 10, 6)
    assert "\ufffd" not in mixed
    assert WINDOW_MARKER in mixed
    assert stats["input_visible_tokens"] <= 16


def test_window_text_mixed_trims_when_the_join_overshoots_the_budget():
    """Re-encoding the joined content can cost an extra token: the loop trims
    (head from its tail, tail from its head) until it fits head+tail."""
    class JoinExpanding(FakeTokenizer):
        """Re-encoding a medium-length string costs one extra token, so the
        head+tail concatenation can exceed head+tail even when each side fits."""
        def __call__(self, text, add_special_tokens=True, padding=False,
                     truncation=False, max_length=None, return_tensors=None):
            if isinstance(text, str):
                ids = [ord(c) for c in text]
                if 8 < len(ids) <= 20:        # the "join" is 10 chars long
                    ids = ids + [0]
                return {"input_ids": ids}
            return super().__call__(text, add_special_tokens=add_special_tokens,
                                    padding=padding, truncation=truncation,
                                    max_length=max_length,
                                    return_tensors=return_tensors)

    tok = JoinExpanding()
    windowed, stats = window_text("a" * 100, tok, 10, 4, 6)
    # head 4 + tail 6 re-encode to 11 > 10 -> one tail token dropped
    assert windowed == "aaaa" + WINDOW_MARKER + "aaaaa"
    assert stats["input_visible_tokens"] <= 10


def test_tagger_context_window_uses_the_configured_spec(monkeypatch):
    tagger = stub_tagger(window="256:768")
    assert tagger.window == (256, 768)
    assert tagger.info()["window"] == "256:768"
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    seen: list[dict] = []

    def capture(records):
        seen.extend(records)
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    result = asyncio.run(tagger.tag(TagRequest(text=text, n_messages=1)))
    expected = text[:256] + WINDOW_MARKER + text[-768:]
    assert seen and {record["state"] for record in seen} == {expected}
    # TagResult.input carries the window spec next to the three old keys
    assert result.input == {"input_tokens": 3000,
                            "input_visible_tokens": 1024,
                            "input_truncated": True,
                            "input_window": "256:768"}


def test_model_signals_score_the_head_window(monkeypatch):
    tagger = stub_tagger(max_length=16)
    seen: list[dict] = []

    def capture(records):
        seen.extend(records)
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    tagger._tag_row(TagRequest(text="a" * 100, n_messages=1))
    assert seen and {record["state"] for record in seen} == {"a" * 16}


def test_tag_exposes_window_and_keeps_rules_on_full_text(monkeypatch):
    """PII rules are the long-request safety net: they scan text beyond the
    model window even though the model signals only read the window."""
    tagger = stub_tagger(max_length=16)
    monkeypatch.setattr(tagger, "_score", lambda records: probs_for(records))
    text = "a" * 64 + " 联系我 someone@example.com"
    result = asyncio.run(tagger.tag(TagRequest(text=text, n_messages=1)))
    assert result.input == {"input_tokens": len(text),
                            "input_visible_tokens": 16,
                            "input_truncated": True,
                            "input_window": "16:0"}
    # the model window cut the address off; the deterministic rule still sees it
    assert "email_address" in result.signals["pii"]["value"]
    assert result.signals["pii"]["conf_by_value"]["email_address"] == 1.0


def test_encode_keeps_the_whole_prompt_without_shrinking(monkeypatch):
    """The old 8-round 0.7x context shrink is gone: one encode, full context."""
    tagger = stub_tagger(max_length=16)
    # The char-level fake tokenizer counts the Chinese question 1:1, so give it
    # a prompt cap wide enough that the canonical wording never hits the
    # defensive left-truncation fallback (the real tokenizer measures 120 tokens
    # of template overhead, well inside PROMPT_OVERHEAD_SLACK).
    tagger.prompt_max_length = 16 + 1024
    captured: list[tuple[dict, list[int]]] = []

    def capture(enc, counts):
        captured.append((enc, counts))
        return []

    monkeypatch.setattr(tagger, "_score_encoded", capture)
    records = build_signal_records(tagger.questions, "x" * 16)
    jailbreak = [item["record"] for item in records
                 if item["signal"] == "jailbreak"]
    assert tagger._score(jailbreak) == []
    enc, counts = captured[0]
    assert counts == [1]
    # The fake renderer keeps the 16-char context verbatim; the old shrink
    # would have shortened it to fit max_length=16 (context + template > 16).
    assert enc["attention_mask"].lengths == [
        len(candidate_prompts(jailbreak[0])[0])]
    assert enc["attention_mask"].lengths[0] > tagger.max_length
    assert "x" * 16 in candidate_prompts(jailbreak[0])[0]


def test_token_budget_chunking_matches_the_single_forward(monkeypatch):
    """P3-30: over-budget packs are sliced into token-budgeted chunks of the
    already-encoded rows. The softmax is per prompt and padding aligns within
    a forward, so chunked scoring must equal one big pass exactly — and the
    long input is tokenized once, not once per chunk."""
    tagger = stub_tagger()
    records = [item["record"] for item in
               build_signal_records(tagger.questions, "上下文" * 100)]

    def fake_forward(enc, counts):
        # one deterministic logit per prompt row, then the real softmax shape
        out, offset = [], 0
        for count in counts:
            values = [(sum(enc["input_ids"][offset + j]) % 997) / 997.0
                      for j in range(count)]
            if count == 1:
                values = [0.0, values[0]]
            top = max(values)
            exps = [math.exp(v - top) for v in values]
            total = sum(exps)
            out.append([v / total for v in exps])
            offset += count
        return out

    forwards: list[tuple[dict, list[int]]] = []

    def recording_forward(enc, counts):
        forwards.append((enc, counts))
        return fake_forward(enc, counts)

    encodes: list[int] = []
    real_encode = tagger._encode

    def spy_encode(recs):
        encodes.append(len(recs))
        return real_encode(recs)

    monkeypatch.setattr(tagger, "_score_encoded", recording_forward)
    monkeypatch.setattr(tagger, "_encode", spy_encode)

    tagger.max_tokens_per_forward = 10 ** 9
    single = tagger._score(records)
    assert len(forwards) == 1 and encodes == [len(records)]

    forwards.clear()
    encodes.clear()
    tagger.max_tokens_per_forward = 4000      # this pack renders ~19k tokens
    chunked = tagger._score(records)
    assert chunked == single
    assert len(forwards) > 1                  # the budget actually chunked
    assert encodes == [len(records)]          # one tokenization, rows reused
    for enc, counts in forwards:
        if len(counts) > 1:                   # multi-record chunks fit the budget
            assert sum(len(row) for row in enc["input_ids"]) <= 4000


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


def test_choice_signal_requires_at_least_two_candidates():
    """item 1.2: K=1 has no distribution (forced argmax, confidence 0.0), so
    the config validator rejects it at save/startup (admin PUT => 422)."""
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    for key in CHOICE_SIGNALS:
        bad = json.loads(json.dumps(cfg))
        candidates = bad["signals"][key]["candidates"]
        only = list(candidates)[0]
        bad["signals"][key]["candidates"] = {only: candidates[only]}
        with pytest.raises(JevTaggerError) as exc:
            validate_question_config(bad)
        assert key in str(exc.value)
        assert "2" in str(exc.value)


def test_config_without_signals_object_raises_jev_error():
    """A config without a 'signals' object must be a clean 422, not a
    NameError (the old message interpolated an undefined variable)."""
    with pytest.raises(JevTaggerError) as exc:
        validate_question_config({"foo": 1})
    assert "signals" in str(exc.value)


def test_pii_true_false_must_be_non_empty_strings():
    """The pii branch validates like the noul branch: missing/blank
    true/false used to blow up at request time (KeyError), not at save time."""
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"].pop("true")
    with pytest.raises(JevTaggerError):
        validate_question_config(bad)

    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["false"] = "   "
    with pytest.raises(JevTaggerError):
        validate_question_config(bad)


def test_pii_types_need_a_usable_zh_label():
    """build_signal_records renders `template.format(zh=meta['zh'])`: a type
    entry without a usable zh is a KeyError on every request that scopes pii —
    rejected at save time instead (2026-10-22 review)."""
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["types"]["phone_number"].pop("zh")
    with pytest.raises(JevTaggerError) as exc:
        validate_question_config(bad)
    assert "phone_number" in str(exc.value)

    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["types"]["phone_number"] = {"zh": ""}
    with pytest.raises(JevTaggerError):
        validate_question_config(bad)


def test_pii_templates_must_render_with_zh():
    """A {name} other than {zh} in the pii templates is a request-time KeyError:
    render all three templates against the type's zh at save time."""
    cfg = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["instructions"] = \
        "判断文本是否包含：{zh} 或 {oops}"
    bad["signals"]["pii"]["true"] = "是的，包含{zh}"
    bad["signals"]["pii"]["false"] = "否"
    with pytest.raises(JevTaggerError) as exc:
        validate_question_config(bad)
    assert "oops" in str(exc.value)

    bad = json.loads(json.dumps(cfg))
    bad["signals"]["pii"]["true"] = "包含：{broken"
    with pytest.raises(JevTaggerError):
        validate_question_config(bad)


# --------------------------------------------------------------------------- #
# Version info for the ledger (2026-10-24 item 1.3)
# --------------------------------------------------------------------------- #
def test_questions_sha256_is_canonical_and_wording_sensitive():
    cfg = load_question_config()
    digest = questions_sha256(cfg)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    # canonical JSON: nested key order / formatting does not change the digest
    reordered = json.loads(json.dumps(cfg))
    types = reordered["signals"]["pii"]["types"]
    reordered["signals"]["pii"]["types"] = dict(reversed(list(types.items())))
    assert questions_sha256(reordered) == digest
    assert questions_sha256(json.loads(json.dumps(cfg, indent=2))) == digest
    # any wording / candidate edit does change it
    edited = json.loads(json.dumps(cfg))
    edited["signals"]["jailbreak"]["instructions"][0] += "？"
    assert questions_sha256(edited) != digest


def test_info_reports_model_identity_and_question_fingerprint():
    tagger = stub_tagger()
    tagger.model_dir = Path("/models/jev-r4b3")
    tagger.temperature = 3.45
    info = tagger.info()
    assert set(info) == {
        "engine", "model_dir", "model_name", "temperature", "max_length",
        "window", "questions_sha256", "head_sha256", "tokenizer_sha256",
        "chat_template_sha256", "weights_sha256", "chunk_segments",
        "chunk_size", "chunk_agg", "chunk_overlap", "chunk_calibration"}
    # the stub's model dir does not exist -> artifact hashes are null
    assert info["head_sha256"] is None
    assert info["tokenizer_sha256"] is None
    assert info["chat_template_sha256"] is None
    assert info["weights_sha256"] is None
    assert info["engine"] == "jev"
    assert info["model_dir"] == str(Path("/models/jev-r4b3").resolve())
    assert info["model_name"] == "jev-r4b3"
    assert info["temperature"] == 3.45
    assert info["max_length"] == 1024
    assert info["window"] == "1024:0"
    expected = hashlib.sha256(json.dumps(
        tagger.questions, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    assert info["questions_sha256"] == expected


def test_default_tagger_records_chunking_as_off():
    """Chunking is opt-in: the default version map records it as off so a
    replay can tell that the single-window path was used."""
    tagger = stub_tagger()
    assert tagger.chunk_enabled is False
    info = tagger.info()
    assert info["chunk_segments"] == 0
    assert info["chunk_size"] == 512
    assert info["chunk_agg"] == "off"
    assert info["chunk_overlap"] == 0.25
    assert info["chunk_calibration"] == "none"

    enabled = stub_tagger(chunk_segments=4, chunk_agg="logsumexp")
    info = enabled.info()
    assert info["chunk_segments"] == 4
    assert info["chunk_agg"] == "logsumexp"
    assert enabled.chunk_enabled is True


def test_info_question_fingerprint_follows_a_hot_config_edit():
    tagger = stub_tagger()
    tagger.model_dir = Path("/models/jev-r4b3")
    before = tagger.info()["questions_sha256"]
    edited = json.loads(json.dumps(tagger.questions))
    edited["signals"]["jailbreak"]["instructions"][0] += "？"
    tagger.set_questions(edited)
    assert tagger.info()["questions_sha256"] != before
    assert tagger.info()["questions_sha256"] == questions_sha256(edited)


def test_model_artifact_hash_helpers(tmp_path):
    """A1: head/tokenizer/weights/chat-template content hashes (streamed)."""
    head = tmp_path / "head.pt"
    head.write_bytes(b"head-bytes")
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"w" * 1000)
    (tmp_path / "tokenizer.json").write_text('{"vocab": 1}', encoding="utf-8")
    hashes = model_file_hashes(tmp_path)
    assert hashes["head_sha256"] == hashlib.sha256(b"head-bytes").hexdigest()
    assert hashes["tokenizer_sha256"] == hashlib.sha256(
        '{"vocab": 1}'.encode()).hexdigest()
    assert hashes["weights_sha256"] == hashlib.sha256(b"w" * 1000).hexdigest()
    assert sha256_file(tmp_path / "missing.bin") is None
    assert sha256_files([]) is None
    # multiple weight shards: names are part of the digest (layout pinned) and
    # the result differs from hashing only the concatenated bytes
    (tmp_path / "model-00001.safetensors").write_bytes(b"a")
    shard_dir = tmp_path / "sharded"
    shard_dir.mkdir()
    (shard_dir / "model-00001.safetensors").write_bytes(b"a")
    (shard_dir / "model-00002.safetensors").write_bytes(b"b")
    import hashlib as _hashlib
    multi = sha256_files(sorted(shard_dir.glob("*.safetensors")))
    assert multi == sha256_files([shard_dir / "model-00001.safetensors",
                                  shard_dir / "model-00002.safetensors"])
    assert multi != _hashlib.sha256(b"ab").hexdigest()
    assert model_file_hashes(shard_dir)["weights_sha256"] == multi

    class _Tok:
        chat_template = "{{ messages }}"

    assert chat_template_sha256(_Tok(), tmp_path) == hashlib.sha256(
        b"{{ messages }}").hexdigest()
    (tmp_path / "chat_template.jinja").write_text("file-template", encoding="utf-8")

    class _NoTemplate:
        pass

    assert chat_template_sha256(_NoTemplate(), tmp_path) == hashlib.sha256(
        b"file-template").hexdigest()


def test_real_constructor_info_carries_artifact_hashes(tmp_path, monkeypatch):
    tagger = real_tagger(tmp_path, monkeypatch)
    info = tagger.info()
    # real_tagger writes an (empty) head.pt and no tokenizer/weights/template
    assert info["head_sha256"] == hashlib.sha256(b"").hexdigest()
    assert info["weights_sha256"] is None
    assert info["tokenizer_sha256"] is None
    assert info["chat_template_sha256"] is None


def test_non_jev_taggers_leave_the_snapshot_fields_empty():
    """B1 contract: heuristic/checkpoint/prompt taggers keep None/{} so the
    ledger can store NULL without special-casing the tagger type."""
    from router.signals.heuristic import HeuristicTagger

    result = asyncio.run(HeuristicTagger().tag(
        TagRequest(text="你好", n_messages=1)))
    assert result.questions_sha256 is None
    assert result.tagger_info == {}


def test_tag_result_carries_the_scoring_snapshot(monkeypatch):
    """A1: tag() snapshots questions_sha256 + tagger_info at entry."""
    tagger = stub_tagger()
    monkeypatch.setattr(tagger, "_score", probs_for)
    result = asyncio.run(tagger.tag(TagRequest(text="x", n_messages=1)))
    assert result.questions_sha256 == questions_sha256(tagger.questions)
    assert result.tagger_info["questions_sha256"] == result.questions_sha256
    assert result.tagger_info["engine"] == "jev"
    assert "model_name" in result.tagger_info and "window" in result.tagger_info


def test_tag_freezes_the_questions_config_during_a_hot_update(monkeypatch):
    """A1: a hot PUT that lands while scoring must not desync the pack from its
    fingerprint — tag() uses the config object captured at entry."""
    tagger = stub_tagger()
    cfg_before = tagger.questions
    edited = json.loads(json.dumps(cfg_before))
    domain = edited["signals"]["domain"]["candidates"]
    edited["signals"]["domain"]["candidates"] = dict(list(domain.items())[:2])
    seen: list[list[dict]] = []

    def score(records):
        seen.append(records)
        tagger.set_questions(edited)          # hot config PUT mid-scoring
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", score)
    result = asyncio.run(tagger.tag(TagRequest(text="x", n_messages=1)))
    # the rendered pack still has all 14 domain candidates (captured config)
    assert len(seen) == 1
    assert any(record["kind"] == "choice" and len(record["answer_keys"]) == 14
               for record in seen[0])
    assert result.questions_sha256 == questions_sha256(cfg_before)
    # the tagger itself did hot-swap; a later info() reflects the new config
    assert tagger.info()["questions_sha256"] == questions_sha256(edited)


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
    assert set(signals) == {"jailbreak", "pii", "domain", "complexity"}


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
    assert trimmed["pii"]["model_p_by_type"] == {
        "cn_id_card": full["pii"]["model_p_by_type"]["cn_id_card"]}
    assert ("cn_id_card" in trimmed["pii"]["value"]) == \
        ("cn_id_card" in full["pii"]["value"])
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
    weird = [Station(id="s", name="怪", root=TreeNode(
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
    stations = default_pipeline()
    before = compute_signal_scope(stations)
    assert "fact_check" not in before.signals
    new_station = Station(id="st-fc", name="需核实", root=TreeNode(
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
    assert signals == {}
