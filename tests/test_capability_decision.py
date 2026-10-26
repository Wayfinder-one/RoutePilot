"""Model capability catalog: ``profile.capability`` validation and lookup.

Migrated from the retired flat capability-routing tests (2026-10-26 unified
routing tree, Task 11).  The capability *policy* tests (global instructions /
thresholds / fallback, ``decide``/``rank``/config loading) were deleted with
``router/decision/capability.py``.  The file name is kept because the catalog is
the surviving "capability decision" boundary: a direct ``model`` candidate's
natural-language text still comes from ``models.profile.capability``; per-node
``nl_choice`` configuration replaced the global policy.
"""

import pytest

from router.decision.catalog import (MAX_CAPABILITY_CHARS, CapabilityPackError,
                                     capability_of_model, normalize_capability)


def test_max_capability_chars_is_the_shared_product_limit():
    assert MAX_CAPABILITY_CHARS == 2000


def test_normalize_capability_accepts_none_whitespace_and_the_limit():
    assert normalize_capability(None) == ""
    assert normalize_capability("  x  ") == "x"
    limit = "x" * MAX_CAPABILITY_CHARS
    assert normalize_capability(limit) == limit


def test_normalize_capability_rejects_long_text_and_non_string():
    with pytest.raises(CapabilityPackError):
        normalize_capability(123)
    with pytest.raises(CapabilityPackError):
        normalize_capability("x" * (MAX_CAPABILITY_CHARS + 1))


def test_capability_of_model_tolerates_bad_json_and_bad_type():
    assert capability_of_model({"profile": "not json"}) == ""
    assert capability_of_model({"profile": None}) == ""
    assert capability_of_model({"profile": {"capability": 3}}) == ""
    assert capability_of_model({"profile": {"capability": "  x  "}}) == "x"


def test_capability_of_model_reads_a_json_text_profile():
    row = {"id": "m", "profile": '{"capability": "适合短翻译", "tier": "small"}'}
    assert capability_of_model(row) == "适合短翻译"
