"""JEV ``choice`` scoring boundary: ``JevTagger.score_choice`` + the pure
``router.decision.choice`` record/serialisation helpers.

The tagger tests are characterization tests: they pin the candidate order, the
input window and the returned shape **before** the pure helpers are extracted,
so the refactor that moves construction/decoding out of ``JevTagger`` cannot
change the observable contract (2026-10-26 unified routing tree, Task 1).
"""

import ast
import subprocess
import sys
import textwrap
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


# ── boundary ownership: catalog stands alone after Task 11 ────────────────
def test_catalog_is_importable_without_the_retired_policy_modules():
    """The model-catalog boundary must not depend on the retired policy modules.

    Task 11 deletes the flat capability-policy module and its pack; the catalog
    survives (``profile.capability`` is a model-catalog field), so importing it
    in a fresh interpreter must work and must not pull the retired modules in.
    """
    repo_root = Path(__file__).resolve().parent.parent
    script = textwrap.dedent("""
        import sys

        import router.decision.catalog as catalog

        assert "router.decision.capability" not in sys.modules
        assert "router.decision.pack" not in sys.modules
        assert catalog.MAX_CAPABILITY_CHARS == 2000
        assert catalog.normalize_capability("  x  ") == "x"
        print("catalog-isolated-ok")
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=repo_root,
        capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "catalog-isolated-ok" in completed.stdout


# ── deliberate fail-open tightening at the adapter boundary ────────────────
def test_score_choice_rejects_probability_count_mismatch_as_jev_error():
    """3 candidates + a 2-probability answer is a ChoiceError -> JevTaggerError.

    The retired ad-hoc path silently truncated/zipped the distribution; the
    decision-runner contract must refuse it (a caller that wants fail-open
    catches ``JevTaggerError``, see the unified-tree NL node).
    """
    class MismatchedChoiceTagger(FakeChoiceTagger):
        def _score(self, records):
            self.records = records
            return [[0.2, 0.8]]        # wrong length for three candidates

    tagger = MismatchedChoiceTagger()
    with pytest.raises(JevTaggerError) as excinfo:
        JevTagger.score_choice(tagger, "request", "choose",
                               {"a": "A", "b": "B", "c": "C"})
    assert "probability count" in str(excinfo.value)
