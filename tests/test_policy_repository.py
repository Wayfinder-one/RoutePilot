"""Policy 文件仓库（brief Step 1）：整份替换、If-Match 冲突、布局隔离与删除保护。

布局是前端私有 JSON（设计 §6.3）：独立文件、1 MiB 上限，写入绝不触碰策略
文档 bytes、policy_revision、current 指针或 If-Match。删除当前启用策略被
ActivePolicyError 拒绝；disable 后删除成功且布局一并清除（同 ID 重建不继承
旧 UI）。
"""
import json

import pytest

from router.routing.policy import policy_from_dict, policy_revision_of
from router.store.policies import (
    ActivePolicyError,
    PolicyAlreadyExistsError,
    PolicyConflictError,
    PolicyReadOnlyError,
    PolicyStorageError,
    PolicyRepo,
)
from tests.routing_fixtures import policy_dict


@pytest.fixture
def repo(tmp_path):
    repository = PolicyRepo(tmp_path / "routing-policy")
    repository.initialize()
    return repository


def test_policy_repository_round_trip_conflict_and_layout_isolation(tmp_path):
    repo = PolicyRepo(tmp_path / "routing-policy")
    repo.initialize()
    created = repo.create("main", policy_from_dict(policy_dict()))
    assert created.enabled is False
    assert created.policy_revision == policy_revision_of(created.policy)
    assert repo.get_layout("main") == {}

    replacement = policy_from_dict(dict(policy_dict(), name="替换后的名字"))
    updated = repo.update(
        "main", replacement,
        base_document_sha256=created.document_sha256,
    )
    assert updated.policy.name == "替换后的名字"
    with pytest.raises(PolicyConflictError):
        repo.update(
            "main", replacement,
            base_document_sha256=created.document_sha256,
        )

    before = (updated.policy_revision, updated.document_sha256)
    assert repo.put_layout("main", {"panels": {"rules": [20, 40]}}) == {
        "panels": {"rules": [20, 40]}
    }
    after = repo.get("main")
    assert (after.policy_revision, after.document_sha256) == before
    repo.enable("main")
    with pytest.raises(ActivePolicyError):
        repo.delete("main")


def test_layout_round_trip_keeps_policy_bytes_and_pointer_untouched(repo):
    created = repo.create("main", policy_from_dict(policy_dict()))
    before = (repo.policies / "main.json").read_bytes()
    layout = {"panels": {"rules": [20, 40]}, "zoom": 0.8, "标题": "中文布局"}
    assert repo.put_layout("main", layout) == layout
    assert (repo.layouts / "main.json").read_text(encoding="utf-8") == (
        json.dumps(layout, ensure_ascii=False, indent=2) + "\n"
    )
    after = repo.get("main")
    assert (after.policy_revision, after.document_sha256) == (
        created.policy_revision, created.document_sha256)
    assert (repo.policies / "main.json").read_bytes() == before
    assert repo.get_layout("main") == layout
    assert repo.active_id() is None


def test_invalid_resource_ids_and_bad_pointer_raise_storage_error(repo):
    policy = policy_from_dict(policy_dict())
    for bad_id in ("bad id", "../evil", "-lead", ".lead", "x" * 65):
        with pytest.raises(PolicyStorageError):
            repo.create(bad_id, policy)
        with pytest.raises(PolicyStorageError):
            repo.get(bad_id)
        with pytest.raises(PolicyStorageError):
            repo.get_layout(bad_id)
    (repo.root / "current").write_text("bad id\n", encoding="utf-8")
    with pytest.raises(PolicyStorageError):
        repo.active_id()
    with pytest.raises(PolicyStorageError):
        repo.fingerprint()


def test_read_only_repo_rejects_every_write_but_keeps_reading(repo):
    policy = policy_from_dict(policy_dict())
    repo.create("main", policy)
    reader = PolicyRepo(repo.root, writer=False)
    assert reader.get("main").id == "main"
    assert reader.get_layout("main") == {}
    with pytest.raises(PolicyReadOnlyError):
        reader.initialize()
    with pytest.raises(PolicyReadOnlyError):
        reader.create("other", policy)
    with pytest.raises(PolicyReadOnlyError):
        reader.update("main", policy, base_document_sha256="0" * 64)
    with pytest.raises(PolicyReadOnlyError):
        reader.enable("main")
    with pytest.raises(PolicyReadOnlyError):
        reader.disable("main")
    with pytest.raises(PolicyReadOnlyError):
        reader.delete("main")
    with pytest.raises(PolicyReadOnlyError):
        reader.put_layout("main", {"any": True})
    assert reader.get("main").policy_revision == repo.get("main").policy_revision


def test_duplicate_create_is_rejected(repo):
    policy = policy_from_dict(policy_dict())
    repo.create("main", policy)
    with pytest.raises(PolicyAlreadyExistsError):
        repo.create("main", policy)


def test_update_of_missing_policy_raises_key_error(repo):
    with pytest.raises(KeyError):
        repo.update("ghost", policy_from_dict(policy_dict()),
                    base_document_sha256="0" * 64)


def test_missing_active_file_raises_storage_error(repo):
    repo.create("main", policy_from_dict(policy_dict()))
    repo.enable("main")
    (repo.policies / "main.json").unlink()
    with pytest.raises(PolicyStorageError):
        repo.active()
    with pytest.raises(PolicyStorageError):
        repo.fingerprint()


def test_invalid_policy_document_raises_storage_error(repo):
    repo.create("main", policy_from_dict(policy_dict()))
    (repo.policies / "main.json").write_bytes(b"{broken")
    with pytest.raises(PolicyStorageError):
        repo.get("main")


def test_atomic_write_cleans_up_temporary_files_on_failure(repo, monkeypatch):
    repo.create("seed", policy_from_dict(policy_dict()))
    before = sorted(path.name for path in repo.policies.iterdir())

    def failing_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("router.store.policies.os.replace", failing_replace)
    with pytest.raises(OSError):
        repo.create("main", policy_from_dict(policy_dict()))
    assert sorted(path.name for path in repo.policies.iterdir()) == before


def test_layout_must_be_an_object_within_one_mib_and_finite(repo):
    repo.create("main", policy_from_dict(policy_dict()))
    with pytest.raises(ValueError):
        repo.put_layout("main", [1, 2])
    with pytest.raises(ValueError):
        repo.put_layout("main", {"pos": float("nan")})
    with pytest.raises(ValueError):
        repo.put_layout("main", {"blob": "x" * (1024 * 1024)})
    assert repo.get_layout("main") == {}
    assert not (repo.layouts / "main.json").exists()


def test_layout_on_unknown_policy_id_raises_key_error(repo):
    with pytest.raises(KeyError):
        repo.get_layout("ghost")
    with pytest.raises(KeyError):
        repo.put_layout("ghost", {"a": 1})


def test_enable_disable_switch_active_pointer_and_fingerprint(repo):
    first = repo.create("a", policy_from_dict(policy_dict()))
    repo.create("b", policy_from_dict(dict(policy_dict(), name="B")))
    enabled = repo.enable("a")
    assert enabled.enabled is True
    assert repo.active_id() == "a"
    assert repo.fingerprint() == ("a", first.document_sha256)
    repo.enable("b")
    assert repo.get("a").enabled is False
    assert [resource.id for resource in repo.list() if resource.enabled] == ["b"]
    assert repo.disable("b") is True
    assert repo.active_id() is None
    assert repo.fingerprint() == (None, "")


def test_enable_missing_policy_raises_and_disable_missing_is_false(repo):
    with pytest.raises(KeyError):
        repo.enable("ghost")
    assert repo.disable("ghost") is False


def test_disable_then_delete_succeeds_and_clears_layout(repo):
    policy = policy_from_dict(policy_dict())
    repo.create("main", policy)
    repo.put_layout("main", {"panels": {"rules": [20, 40]}})
    repo.enable("main")
    with pytest.raises(ActivePolicyError):
        repo.delete("main")
    assert repo.get("main") is not None
    assert repo.disable("main") is True
    assert repo.delete("main") is True
    assert repo.get("main") is None
    assert not (repo.layouts / "main.json").exists()
    # 同 ID 重建不继承旧 UI：布局从空对象重新开始。
    repo.create("main", policy)
    assert repo.get_layout("main") == {}
    with pytest.raises(KeyError):
        repo.delete("ghost")
