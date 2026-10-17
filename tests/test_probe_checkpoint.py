"""Pure helpers of scripts/probe_checkpoint.py (the M2 calibration probe, F6)."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "probe_checkpoint", Path(__file__).parent.parent / "scripts" / "probe_checkpoint.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_prompt_set_is_the_documented_24():
    assert len(probe.PROMPTS) == 24
    assert len(set(probe.PROMPTS)) == 24


def test_percentile_matches_linear_interpolation():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert probe.pctl(values, 0.0) == 0.0
    assert probe.pctl(values, 1.0) == 4.0
    assert probe.pctl(values, 0.5) == 2.0
    assert probe.pctl(values, 0.25) == 1.0
    assert abs(probe.pctl(values, 0.75) - 3.0) < 1e-9
    assert probe.pctl([7.0], 0.9) == 7.0


def test_summary_shape_and_rounding():
    row = probe.summarize("domain", [0.0, 0.5, 1.0])
    assert row["signal"] == "domain" and row["n"] == 3
    assert row["p50"] == 0.5 and row["mean"] == 0.5
    assert probe.summarize("empty", []) == {"signal": "empty", "n": 0}
