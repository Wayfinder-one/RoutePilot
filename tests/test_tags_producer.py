"""F2 guard: `tags.json` has a committed producer and a drift checker.

Second-round audit F2: the file the router validates had no writer in either
repository, so its origin was untraceable. ``scripts/export_checkpoint_tags.py``
is the writer; these tests pin its two contracts:

1. head label ORDER comes from the training registry (weight order) and the
   mapped value space satisfies the router's registry;
2. a deployed tags.json can be checked for drift, and the committed fixture
   (the copy shipped with the checkpoint on 76) matches a regenerate.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from router.signals.checkpoint import HEADS, load_and_validate
from router.signals.registry import SIGNALS

REPO = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "checkpoint_tags.json"
SCRIPT = REPO / "scripts" / "export_checkpoint_tags.py"

_spec = importlib.util.spec_from_file_location("export_checkpoint_tags", SCRIPT)
tagsexport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tagsexport)


class FakeSignals:
    """Minimal stand-in for routepilot.signals."""

    __name__ = "routepilot.signals"

    def __init__(self, labels_by_head):
        self.labels_by_head = labels_by_head

    def label_space(self, head):
        return tuple(self.labels_by_head[head])


def _fixture_labels_by_head() -> dict:
    tags = json.loads(FIXTURE.read_text(encoding="utf-8"))["tags"]
    return {head: tags[head]["labels"] for head in tags if "labels" in tags[head]}


def _make_checkpoint(tmp_path, tags):
    d = tmp_path / "ckpt"
    d.mkdir()
    (d / "router_config.json").write_text(
        '{"base_model": "models/x", "max_length": 256, "embedding_dim": 768}')
    (d / "model.pt").write_bytes(b"")
    (d / "tags.json").write_text(json.dumps(tags))
    return d


def test_committed_fixture_passes_router_validation(tmp_path):
    tags = json.loads(FIXTURE.read_text(encoding="utf-8"))
    files = load_and_validate(_make_checkpoint(tmp_path, tags))
    assert files.embedding_dim == 768
    assert len(files.labels["pii"]) == 30
    assert files.labels["domain"][3] == "computer_science"


def test_build_tags_keeps_training_label_order():
    from router.signals.checkpoint import canonical_label

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = _fixture_labels_by_head()
    # reverse every head: the producer must NOT sort the order (it does
    # canonicalise the spelling, which is a no-op on the committed fixture)
    reversed_raw = {head: list(reversed(labels)) for head, labels in raw.items()}
    tags = tagsexport.build_tags(FakeSignals(reversed_raw), embedding_dim=768)
    for head, labels in reversed_raw.items():
        expected = [canonical_label(head, x) for x in labels]
        assert tags["tags"][head]["labels"] == expected
        assert tags["tags"][head]["labels"] != fixture["tags"][head]["labels"] or \
            len(labels) == 1
    # types and embedding metadata are stable
    assert tags["tags"]["embedding"] == {"type": "projection", "dim": 768}
    assert tags["tags"]["domain"]["type"] == "choice"
    assert tags["tags"]["complexity"]["type"] == "ordered"


#: Inverse of the router's product aliasing, i.e. the raw spellings in the
#: training repo's label spaces (what a pre-canonical tags.json would contain).
def _to_training_spellings(canonical: dict) -> dict:
    to_raw = {
        "domain": {"computer_science": "computer science"},
        "fact_check": {"no_check": "no_fact_check_needed",
                       "needs_check": "fact_check_needed"},
        "user_feedback": {"satisfied": "SAT",
                          "need_clarification": "NEED_CLARIFICATION",
                          "wrong_answer": "WRONG_ANSWER",
                          "want_different": "WANT_DIFFERENT"},
        "modality": {"text": "AR", "image": "DIFFUSION",
                     "text_image": "BOTH"},
    }
    raw = {}
    for head, labels in canonical.items():
        if head == "pii":
            raw[head] = [x.upper() for x in labels]
        else:
            raw[head] = [to_raw.get(head, {}).get(x, x) for x in labels]
    return raw


def test_build_tags_canonicalises_raw_training_spellings():
    canonical = _fixture_labels_by_head()
    raw = _to_training_spellings(canonical)
    assert raw["domain"][3] == "computer science"          # sanity: raw input
    tags = tagsexport.build_tags(FakeSignals(raw), embedding_dim=768)
    assert tags == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_build_tags_covers_every_router_head_and_value():
    raw = _to_training_spellings(_fixture_labels_by_head())
    tags = tagsexport.build_tags(FakeSignals(raw), embedding_dim=768)
    assert tuple(tags["tags"]) == tuple(spec.head for spec in HEADS)
    for spec in HEADS:
        if spec.kind == "embedding":
            continue
        expected = {v[0] for v in SIGNALS[spec.signal].values}
        from router.signals.checkpoint import canonical_label
        mapped = {canonical_label(spec.head, x) for x in tags["tags"][spec.head]["labels"]}
        assert mapped == expected


def test_build_tags_rejects_a_head_missing_from_the_training_registry():
    raw = _fixture_labels_by_head()
    del raw["modality"]
    with pytest.raises(SystemExit):
        tagsexport.build_tags(FakeSignals(raw), embedding_dim=768)


def test_diff_detects_label_order_drift():
    expected = tagsexport.build_tags(FakeSignals(_fixture_labels_by_head()), 768)
    actual = json.loads(json.dumps(expected))
    labels = actual["tags"]["domain"]["labels"]
    labels[0], labels[1] = labels[1], labels[0]
    problems = tagsexport.diff_tags(expected, actual)
    assert any("ORDER drifted" in p for p in problems)


def test_diff_detects_value_drift_and_dim_drift():
    expected = tagsexport.build_tags(FakeSignals(_fixture_labels_by_head()), 768)
    actual = json.loads(json.dumps(expected))
    actual["tags"]["pii"]["labels"][0] = "MADE_UP_TYPE"
    actual["tags"]["embedding"]["dim"] = 1024
    problems = tagsexport.diff_tags(expected, actual)
    assert any("pii: labels differ" in p for p in problems)
    assert any("embedding: dim" in p for p in problems)


def _write_fake_train_repo(tmp_path, label_sets) -> Path:
    src = tmp_path / "train-src"
    package = src / "routepilot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "signals.py").write_text(
        "LABELS = " + repr(label_sets) + "\n\n"
        "def label_space(head):\n"
        "    return tuple(LABELS[head])\n")
    return src


def test_cli_generate_then_check_roundtrip(tmp_path):
    labels = _fixture_labels_by_head()
    train_src = _write_fake_train_repo(tmp_path, labels)
    out = tmp_path / "out-tags.json"

    gen = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--out", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    assert gen.returncode == 0, gen.stderr
    assert json.loads(out.read_text())["tags"]["domain"]["labels"] == labels["domain"]

    chk = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--check", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    assert chk.returncode == 0, chk.stderr
    assert "OK" in chk.stderr

    drift = json.loads(out.read_text())
    drift["tags"]["complexity"]["labels"] = ["hard", "easy"]
    out.write_text(json.dumps(drift))
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--check", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    assert bad.returncode == 1
    assert "DRIFT" in bad.stderr and "complexity" in bad.stderr


def test_cli_checkpoint_dir_defaults_output_and_reads_embedding_dim(tmp_path):
    labels = _fixture_labels_by_head()
    train_src = _write_fake_train_repo(tmp_path, labels)
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "router_config.json").write_text('{"base_model": "m", "embedding_dim": 512}')
    (ckpt / "model.pt").write_bytes(b"")

    gen = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--checkpoint-dir", str(ckpt)],
        capture_output=True, text=True, cwd=str(REPO))
    assert gen.returncode == 0, gen.stderr
    written = json.loads((ckpt / "tags.json").read_text())
    assert written["tags"]["embedding"]["dim"] == 512


def test_cli_refuses_to_overwrite_drifted_file_without_force(tmp_path):
    labels = _fixture_labels_by_head()
    train_src = _write_fake_train_repo(tmp_path, labels)
    out = tmp_path / "tags.json"
    out.write_text(json.dumps(tagsexport.build_tags(FakeSignals(labels), 768)
                              | {"schema": 2}))
    refused = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--out", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    assert refused.returncode == 1
    assert "refusing to overwrite" in refused.stderr
    forced = subprocess.run(
        [sys.executable, str(SCRIPT), "--train-src", str(train_src),
         "--out", str(out), "--force"],
        capture_output=True, text=True, cwd=str(REPO))
    assert forced.returncode == 0, forced.stderr
    assert json.loads(out.read_text())["schema"] == 1


def test_committed_fixture_matches_real_training_repo_when_importable():
    """On 76 / CI with the training repo on PYTHONPATH: the committed fixture
    (byte copy of the checkpoint's tags.json) must equal a fresh generate."""
    pytest.importorskip("routepilot.signals")
    module = tagsexport.load_training_signals()
    generated = tagsexport.build_tags(module, 768)
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert generated == committed
