"""JEV ``choice`` scoring boundary: ``JevTagger.score_choice`` + the pure
``router.decision.choice`` record/serialisation helpers.

The tagger tests are characterization tests: they pin the candidate order, the
input window and the returned shape **before** the pure helpers are extracted,
so the refactor that moves construction/decoding out of ``JevTagger`` cannot
change the observable contract (2026-10-26 unified routing tree, Task 1).
"""

import ast
from pathlib import Path

import pytest

from router.decision import choice
from router.decision.choice import (ChoiceError, build_choice_record,
                                    decode_choice)
from router.signals.jev import JevTagger, JevTaggerError


class FakeChoiceTagger:
    temperature = 0.7

    def __init__(self):
        self.records = None

    def _context_window(self, state):
        return f"window:{state}", {"input_window": "2:0"}

    def _score(self, records):
        self.records = records
        return [[0.2, 0.8]]

    def info(self):
        return {"engine": "fake-jev"}


def test_score_choice_preserves_order_and_result_shape():
    tagger = FakeChoiceTagger()
    result = JevTagger.score_choice(
        tagger, "request", "choose", {"b": "second", "a": "first"})
    assert tagger.records[0]["state"] == "window:request"
    assert tagger.records[0]["answer_keys"] == ["b", "a"]
    assert tagger.records[0]["options"] == ["b: second", "a: first"]
    assert result["probabilities"] == {"b": 0.2, "a": 0.8}
    assert result["ranking"][0] == {"model": "a", "p": 0.8}
    assert result["input"] == {"input_window": "2:0"}


def test_score_choice_keeps_the_jev_error_type_for_malformed_candidates():
    tagger = FakeChoiceTagger()
    with pytest.raises(JevTaggerError):
        JevTagger.score_choice(tagger, "request", "choose", {"only": "x"})
    assert tagger.records is None


# ── pure record/serialisation helpers (router.decision.choice) ──────────────
def test_build_choice_record_renders_options_in_candidate_order():
    record = build_choice_record("request", "choose",
                                 {"b": "second", "a": "first"})
    assert record == {"state": "request", "kind": "choice",
                      "question": "choose",
                      "options": ["b: second", "a: first"],
                      "answer_keys": ["b", "a"]}


@pytest.mark.parametrize("instructions,candidates", [
    ("  ", {"a": "x", "b": "y"}),                  # blank instructions
    ("choose", {"only": "x"}),                     # fewer than 2 candidates
    ("choose", {f"k{i}": "x" for i in range(256)}),  # more than 255
    ("choose", {"a": "x", "b": 3}),                # non-string description
    ("choose", {"a": "x", 3: "y"}),                # non-string key
    ("choose", ["a", "b"]),                        # not a mapping
])
def test_build_choice_record_rejects_malformed_input(instructions, candidates):
    with pytest.raises(ChoiceError):
        build_choice_record("request", instructions, candidates)


def test_decode_choice_ranks_descending_and_keeps_ties_in_candidate_order():
    record = {"answer_keys": ["a", "b", "c"]}
    out = decode_choice(record, [0.2, 0.6, 0.2], {"input_window": "3:0"}, 1.5)
    assert out["probabilities"] == {"a": 0.2, "b": 0.6, "c": 0.2}
    assert [row["model"] for row in out["ranking"]] == ["b", "a", "c"]
    assert out["input"] == {"input_window": "3:0"}
    assert out["temperature"] == 1.5


def test_decode_choice_rejects_probability_count_mismatch():
    with pytest.raises(ChoiceError):
        decode_choice({"answer_keys": ["a", "b"]}, [0.5], {}, 1.0)


def test_choice_module_does_not_import_the_jev_tagger():
    """The pure boundary must stay independent of router.signals."""
    source = Path(choice.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [name for name in imported
                if "signals" in name or "jev" in name.lower()]
