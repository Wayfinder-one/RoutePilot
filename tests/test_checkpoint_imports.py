"""F1 guard: the checkpoint tagger must bind the *renamed* training repo.

The first audit round (2026-10-17, second round) found ``checkpoint.py`` still
importing ``smart_router.*`` — the training repo was renamed to ``routepilot``
on 2026-10-14 and the only reason checkpoint mode kept working on 76 was a
stale pre-rename copy of the training repo. These tests pin the package
preference order, the explicit fallback, and the loud failure mode. The
real-weights loading path is covered separately by
``tests/test_checkpoint_real.py`` (needs a checkpoint + torch, run on 76).
"""

import logging
import sys
import types

import pytest

from router.signals import checkpoint


@pytest.fixture(autouse=True)
def _clear_signals_cache():
    """_train_signals_module is process-cached; isolate every test."""
    checkpoint._train_signals_module.cache_clear()
    yield
    checkpoint._train_signals_module.cache_clear()


def _fake_package(monkeypatch, name, *, model_class=None, class_name=None,
                  normalize=None):
    """Register a fake training package in sys.modules (importable by name)."""
    pkg = types.ModuleType(name)
    signals = types.ModuleType(f"{name}.signals")
    if normalize is not None:
        signals.normalize_label = normalize
    pkg.signals = signals
    monkeypatch.setitem(sys.modules, name, pkg)
    monkeypatch.setitem(sys.modules, f"{name}.signals", signals)
    if model_class is not None:
        assert class_name, "class_name is required with model_class"
        modeling = types.ModuleType(f"{name}.modeling")
        router_mod = types.ModuleType(f"{name}.modeling.router")
        setattr(router_mod, class_name, model_class)
        modeling.router = router_mod
        pkg.modeling = modeling
        monkeypatch.setitem(sys.modules, f"{name}.modeling", modeling)
        monkeypatch.setitem(sys.modules, f"{name}.modeling.router", router_mod)
    return pkg


def test_prefers_the_renamed_routepilot_package():
    assert checkpoint.TRAIN_PACKAGE_CANDIDATES[0] == "routepilot"
    assert checkpoint.TRAIN_PACKAGE_CANDIDATES[1] == "smart_router"
    assert checkpoint.TRAIN_MODEL_CLASSES["routepilot"] == "RoutePilotModel"
    assert checkpoint.TRAIN_MODEL_CLASSES["smart_router"] == "SmartRouterModel"


def test_model_class_import_prefers_routepilot_when_both_exist(monkeypatch):
    routepilot_cls = type("RoutePilotModel", (), {})
    legacy_cls = type("SmartRouterModel", (), {})
    _fake_package(monkeypatch, "routepilot", model_class=routepilot_cls,
                  class_name="RoutePilotModel")
    _fake_package(monkeypatch, "smart_router", model_class=legacy_cls,
                  class_name="SmartRouterModel")

    cls, qualified = checkpoint.import_train_model_class()

    assert cls is routepilot_cls
    assert qualified == "routepilot.modeling.router.RoutePilotModel"


def test_model_class_import_falls_back_to_pre_rename_package(monkeypatch, caplog):
    legacy_cls = type("SmartRouterModel", (), {})
    _fake_package(monkeypatch, "smart_router", model_class=legacy_cls,
                  class_name="SmartRouterModel")
    # a real 'routepilot' may exist on PYTHONPATH (76); simulate its absence so
    # the fallback branch is what this test actually exercises
    real_import = checkpoint.importlib.import_module

    def hide_routepilot(name, package=None):
        if name == "routepilot" or name.startswith("routepilot."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, package)

    monkeypatch.setattr(checkpoint.importlib, "import_module", hide_routepilot)

    with caplog.at_level(logging.WARNING, logger=checkpoint.__name__):
        cls, qualified = checkpoint.import_train_model_class()

    assert cls is legacy_cls
    assert qualified == "smart_router.modeling.router.SmartRouterModel"
    assert any("pre-rename" in rec.message for rec in caplog.records)


def test_model_class_import_fails_loudly_with_both_names(monkeypatch):
    def boom(name, package=None):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(checkpoint.importlib, "import_module", boom)

    with pytest.raises(checkpoint.CheckpointError) as excinfo:
        checkpoint.import_train_model_class()

    message = str(excinfo.value)
    assert "routepilot.modeling.router.RoutePilotModel" in message
    assert "smart_router.modeling.router.SmartRouterModel" in message


def test_missing_train_signals_warns_instead_of_being_silent(monkeypatch, caplog):
    def boom(name, package=None):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(checkpoint.importlib, "import_module", boom)

    with caplog.at_level(logging.WARNING, logger=checkpoint.__name__):
        assert checkpoint._train_signals_module() is None

    assert any("not importable" in rec.message for rec in caplog.records)
    # the local mirror still yields the same canonical value
    assert checkpoint.canonical_label("domain", "computer science") == "computer_science"


def test_reuses_training_normalize_label_when_importable(monkeypatch):
    calls = []

    def normalize_label(head, label):
        calls.append((head, label))
        return "SHOUTY"

    _fake_package(monkeypatch, "routepilot", normalize=normalize_label)

    # consume the train alias (returns "SHOUTY"), then the mechanical + product
    # normalisation chain runs as usual
    assert checkpoint.canonical_label("domain", "computer science") == "shouty"
    assert calls == [("domain", "computer science")]


def test_local_mirror_is_used_when_training_repo_absent(monkeypatch):
    def boom(name, package=None):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(checkpoint.importlib, "import_module", boom)

    assert checkpoint._train_signals_module() is None
    # exactly the aliases mirrored from routepilot.signals.LABEL_ALIASES
    assert checkpoint.canonical_label("jailbreak", "1") == "jailbreak"
    assert checkpoint.canonical_label("fact_check", "FACT_CHECK_NEEDED") == "needs_check"


# --------------------------------------------------------------------------- #
# True-import guards: these pass trivially on a host without the training repo
# and become meaningful on 76 / CI where the repo is on PYTHONPATH.
# --------------------------------------------------------------------------- #
def test_real_routepilot_signals_import_when_available():
    pytest.importorskip("routepilot.signals")
    module = checkpoint._train_signals_module()
    assert module is not None
    assert module.__name__ == "routepilot.signals"
    assert callable(module.normalize_label)


def test_real_routepilot_model_class_import_when_available():
    pytest.importorskip("routepilot.modeling.router")
    pytest.importorskip("torch")
    cls, qualified = checkpoint.import_train_model_class()
    assert qualified == "routepilot.modeling.router.RoutePilotModel"
    assert cls.__name__ == "RoutePilotModel"
    assert callable(cls.load_checkpoint)
