"""Unit tests for JevTagger (Open-Jev main model as the signal tagger).

The torch model is not loaded here: the pure config/record/decoding helpers and
the signal-pack assembly run with a stubbed ``_score``. The real accuracy
measurement lives in the training repo (``results/jev_distill/README.md``) and
the deployment probe is done on the GPU host.
"""

import asyncio
import json
import math

import pytest

from router.pipeline.model import Station, TreeNode
from router.pipeline.scope import (compute_signal_scope, full_scope,
                                   SignalScope)
from router.signals.jev import (CHOICE_SIGNALS, DEFAULT_QUESTIONS_PATH,
                                JUDGED_SIGNALS, NOUL_SIGNALS,
                                PROMPT_OVERHEAD_SLACK, JevTagger,
                                JevTaggerError, build_signal_records,
                                candidate_prompts, decode_choice, decode_noul,
                                entropy_confidence, binary_entropy_confidence,
                                head_window, load_question_config,
                                validate_question_config)
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


def trimmed_scope():
    return compute_signal_scope(default_pipeline())


def stub_tagger(questions=None, tokenizer=None) -> JevTagger:
    """A JevTagger without torch/model: config + a stubbed scorer."""
    tagger = object.__new__(JevTagger)
    tagger.questions = questions or load_question_config()
    tagger.temperature = 1.0
    tagger.max_length = 1024
    tagger.prompt_max_length = tagger.max_length + PROMPT_OVERHEAD_SLACK
    tagger.tokenizer = tokenizer or FakeTokenizer()
    tagger.kw_groups = {}
    tagger.time_periods = {}
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


# --------------------------------------------------------------------------- #
# Input window (2026-10-21): first max_length tokens of the request text only
# --------------------------------------------------------------------------- #
def test_head_window_keeps_the_head_tokens():
    tok = FakeTokenizer()
    text = "".join(chr(0x4E00 + i) for i in range(3000))
    head, stats = head_window(text, tok, 1024)
    assert head == text[:1024]
    assert stats == {"input_tokens": 3000, "input_visible_tokens": 1024,
                     "input_truncated": True}


def test_head_window_leaves_short_text_untouched():
    tok = FakeTokenizer()
    text = "你好，帮我写快排"
    head, stats = head_window(text, tok, 1024)
    assert head == text
    assert stats == {"input_tokens": len(text),
                     "input_visible_tokens": len(text),
                     "input_truncated": False}


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


def test_model_signals_score_the_head_window(monkeypatch):
    tagger = stub_tagger()
    tagger.max_length = 16
    seen: list[dict] = []

    def capture(records):
        seen.extend(records)
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    tagger._tag_row(TagRequest(text="a" * 100, n_messages=1))
    assert seen and {record["state"] for record in seen} == {"a" * 16}


def test_tag_exposes_window_and_keeps_rules_on_full_text(monkeypatch):
    """PII rules are the long-request safety net: they scan text beyond the
    model window even though the model signals only read the head."""
    tagger = stub_tagger()
    tagger.max_length = 16
    monkeypatch.setattr(tagger, "_score", lambda records: probs_for(records))
    text = "a" * 64 + " 联系我 someone@example.com"
    result = asyncio.run(tagger.tag(TagRequest(text=text, n_messages=1)))
    assert result.input == {"input_tokens": len(text),
                            "input_visible_tokens": 16,
                            "input_truncated": True}
    # the model window cut the address off; the deterministic rule still sees it
    assert "email_address" in result.signals["pii"]["value"]
    assert result.signals["pii"]["conf_by_value"]["email_address"] == 1.0


def test_encode_keeps_the_whole_prompt_without_shrinking(monkeypatch):
    """The old 8-round 0.7x context shrink is gone: one encode, full context."""
    tagger = stub_tagger()
    tagger.max_length = 16
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
