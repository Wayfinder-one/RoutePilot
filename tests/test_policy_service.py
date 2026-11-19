"""PolicyService 发布原子性（brief Step 4）：真实仓库 + Publisher 桩。

桩只实现 SnapshotPublisher 的 ``locked/prepare/publish`` 契约，并在每次调用时
记录当时的磁盘指纹，用来逐项证明发布次序：

- create 只写文档与审计，绝不发布；
- update 已启用策略：prepare（候选）→ 文件写入 → fingerprint 复核 → publish，
  冲突时不写文件、不发布；
- enable 校验失败不改变 current/快照/审计；成功时指针写入后才发布；
- disable 当前策略发布空快照；
- delete active 抛 ActivePolicyError（API 映射 409）。
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from router.control.errors import (
    AlreadyExists,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.control.config import ConfigurationService
from router.control.policies import PolicyService
from router.jev.config import JevSettings
from router.routing.policy import policy_from_dict, policy_revision_of
from router.store.db import connect, migrate
from router.store.jev_config import JevConfigRepo
from router.store.policies import (
    ActivePolicyError,
    PolicyChangedDuringLoad,
    PolicyConflictError,
    PolicyRepo,
)
from router.store.repositories import ChangeRepo, ModelRepo, seed_default
from tests.routing_fixtures import policy_dict


class PublisherStub:
    """SnapshotPublisher 桩：记录事件与调用时刻的磁盘指纹，锁内调用契约。"""

    def __init__(self, repo):
        self.repo = repo
        self.events = []
        self.prepared = []
        self.published = []
        self._locked = False

    def reset(self):
        self.events.clear()
        self.prepared.clear()
        self.published.clear()

    @contextmanager
    def locked(self):
        self._locked = True
        try:
            yield self
        finally:
            self._locked = False

    def prepare(self, conn, resource, fingerprint):
        assert self._locked, "prepare must run inside publisher.locked()"
        self.events.append(("prepare", self.repo.fingerprint()))
        prepared = SimpleNamespace(resource=resource, fingerprint=fingerprint)
        self.prepared.append(prepared)
        return prepared

    def publish(self, prepared):
        assert self._locked, "publish must run inside publisher.locked()"
        self.events.append(("publish", self.repo.fingerprint()))
        self.published.append(prepared)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "control.db"))
    migrate(connection)
    seed_default(connection)
    # policy_dict 的目标引用 fast/strong：注册进模型目录后启用校验可通过。
    ModelRepo(connection).create("fast", "fast", "local")
    ModelRepo(connection).create("strong", "strong", "local")
    # rules 与 questions 非空 ⇒ 启用校验要求 JEV 配置存在（设计 §7.2）。
    JevConfigRepo(connection).save(JevSettings("http://jev.test", "d1-2B"))
    yield connection
    connection.close()


@pytest.fixture
def repo(tmp_path):
    repository = PolicyRepo(tmp_path / "routing-policy")
    repository.initialize()
    return repository


@pytest.fixture
def stub(repo):
    return PublisherStub(repo)


def audit_records(conn):
    """kind == "policy" 的审计行，按时间正序。"""
    rows = [row for row in ChangeRepo(conn).list(limit=200)
            if row["kind"] == "policy"]
    return list(reversed(rows))


def audit_details(conn):
    return [row["detail"] for row in audit_records(conn)]


def test_create_validates_structure_and_never_publishes(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    created = service.create("main", policy_from_dict(policy_dict()))
    assert created.enabled is False
    assert created.policy_revision == policy_revision_of(created.policy)
    assert stub.prepared == [] and stub.published == []
    assert audit_details(conn) == [{"action": "create", "id": "main"}]


def test_create_generates_id_rejects_duplicates_and_invalid_documents(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    auto = service.create(None, policy_from_dict(policy_dict()))
    assert auto.id and repo.get(auto.id) is not None

    with pytest.raises(AlreadyExists):
        service.create(auto.id, policy_from_dict(policy_dict()))

    raw = policy_dict()
    raw["rules"][0]["target"] = "ghost-target"
    with pytest.raises(ValidationFailed):
        service.create("broken", policy_from_dict(raw))
    assert repo.get("broken") is None
    assert [detail["action"] for detail in audit_details(conn)] == ["create"]


def test_update_of_enabled_policy_prepare_write_recheck_publish_in_order(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    created = service.create("main", policy_from_dict(policy_dict()))
    service.enable("main")
    stub.reset()

    replacement = policy_from_dict(dict(policy_dict(), name="替换后的名字"))
    updated = service.update(
        "main", replacement, base_document_sha256=repo.get("main").document_sha256)

    assert updated.policy.name == "替换后的名字"
    assert updated.enabled is True
    # 次序证据：prepare 时文件仍是旧文档；publish 时文件已是新文档且与候选指纹一致。
    assert stub.events == [
        ("prepare", ("main", created.document_sha256)),
        ("publish", ("main", updated.document_sha256)),
    ]
    prepared = stub.published[0]
    assert prepared.fingerprint == ("main", updated.document_sha256)
    assert prepared.resource.policy.name == "替换后的名字"
    assert audit_details(conn)[-1] == {
        "action": "update", "id": "main", "policy_revision": updated.policy_revision}


def test_update_conflict_never_writes_the_file(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    service.enable("main")
    before = (repo.policies / "main.json").read_bytes()
    replacement = policy_from_dict(dict(policy_dict(), name="并发过期"))
    stub.reset()

    with pytest.raises(PolicyConflictError) as conflict:
        service.update("main", replacement, base_document_sha256="0" * 64)
    assert conflict.value.actual_document_sha256 == repo.get("main").document_sha256
    assert (repo.policies / "main.json").read_bytes() == before
    assert stub.published == []
    assert audit_details(conn)[-1]["action"] == "enable"

    # 畸形 If-Match 在候选构造之前就被拒绝。
    stub.reset()
    with pytest.raises(PolicyConflictError):
        service.update("main", replacement, base_document_sha256="")
    assert stub.prepared == [] and stub.published == []


def test_update_of_disabled_policy_skips_enable_validation_and_publish(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    stub.reset()
    # 结构合法、目录未知的模型：草稿可保存（启用校验只在启用档位生效）。
    raw = policy_dict()
    raw["targets"]["fast"] = {"models": [{"model": "ghost-model"}]}
    updated = service.update(
        "main", policy_from_dict(raw),
        base_document_sha256=repo.get("main").document_sha256)
    assert updated.enabled is False
    assert stub.prepared == [] and stub.published == []


def test_update_unknown_policy_is_missing_error(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    with pytest.raises(MissingError):
        service.update("ghost", policy_from_dict(policy_dict()),
                       base_document_sha256="0" * 64)


def test_enable_publishes_after_validation_and_pointer_write(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    created = service.create("main", policy_from_dict(policy_dict()))
    assert stub.published == []  # create 不发布

    service.enable("main")
    assert repo.active_id() == "main"
    # 次序证据：prepare 时指针未写入（指纹仍空）；publish 时指针已写入。
    assert stub.events == [
        ("prepare", (None, "")),
        ("publish", ("main", created.document_sha256)),
    ]
    prepared = stub.published[0]
    assert prepared.fingerprint == ("main", created.document_sha256)
    assert prepared.resource.enabled is True
    assert audit_details(conn) == [
        {"action": "create", "id": "main"},
        {"action": "enable", "id": "main"},
    ]


def test_enable_validation_failure_changes_no_current_snapshot_or_audit(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    service.enable("main")
    stub.reset()
    # 草稿允许未知模型（保存档只做结构校验），启用校验必须拒绝。
    raw = policy_dict()
    raw["targets"]["fast"] = {"models": [{"model": "ghost-model"}]}
    service.create("draft", policy_from_dict(raw))
    audit_before = audit_details(conn)

    with pytest.raises(ValidationFailed) as failure:
        service.enable("draft")

    assert {issue.code for issue in failure.value.issues} == {"unknown_model"}
    assert repo.active_id() == "main"                    # current 指针不变
    assert repo.get("draft").enabled is False
    assert stub.prepared == [] and stub.published == []   # 快照不变
    assert audit_details(conn) == audit_before            # 审计不变


def test_disable_current_policy_publishes_empty_snapshot(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    service.enable("main")
    service.create("other", policy_from_dict(dict(policy_dict(), name="另一个")))
    stub.reset()

    service.disable("main")

    assert repo.active_id() is None
    assert stub.events == [
        ("prepare", ("main", repo.get("main").document_sha256)),
        ("publish", (None, "")),
    ]
    assert stub.published[0].fingerprint == (None, "")
    assert stub.published[0].resource is None  # 空快照候选

    # 关闭非当前策略不发布任何快照。
    stub.reset()
    service.disable("other")
    assert stub.prepared == [] and stub.published == []
    with pytest.raises(MissingError):
        service.disable("ghost")


def test_delete_active_policy_is_rejected_and_delete_clears_layout(conn, repo, stub):
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    repo.put_layout("main", {"panels": {"rules": [20, 40]}})
    service.enable("main")

    with pytest.raises(ActivePolicyError):
        service.delete("main")
    assert repo.get("main") is not None

    service.disable("main")
    service.delete("main")
    assert repo.get("main") is None
    assert not (repo.layouts / "main.json").exists()  # 同 ID 重建不继承旧 UI

    service.create("main", policy_from_dict(policy_dict()))
    assert repo.get_layout("main") == {}
    with pytest.raises(MissingError):
        service.delete("ghost")
    assert [detail["action"] for detail in audit_details(conn)] == [
        "create", "enable", "disable", "delete", "create",
    ]


def test_fingerprint_recheck_blocks_publish_after_document_write(
        conn, repo, stub, monkeypatch):
    """复核位于文件写入与发布之间：指纹不稳定 ⇒ 文件已写但绝不发布。"""
    service = PolicyService(conn, repo, stub)
    service.create("main", policy_from_dict(policy_dict()))
    service.enable("main")
    stub.reset()
    before = (repo.policies / "main.json").read_bytes()

    monkeypatch.setattr(repo, "fingerprint", lambda: ("main", "f" * 64))
    with pytest.raises(PolicyChangedDuringLoad):
        service.update(
            "main", policy_from_dict(dict(policy_dict(), name="复核失败")),
            base_document_sha256=repo.get("main").document_sha256)

    assert (repo.policies / "main.json").read_bytes() != before  # 整份替换已落盘
    assert stub.published == []                                 # 复核失败 ⇒ 不发布


# ---------------------------------------------------------------------------
# ConfigurationService（brief Step 5）：策略仓库驱动配置写用例
# ---------------------------------------------------------------------------

def test_configuration_service_scans_policy_targets_for_model_references(conn, repo, stub):
    PolicyService(conn, repo, stub).create("main", policy_from_dict(policy_dict()))
    service = ConfigurationService(conn, repo, stub)
    with pytest.raises(ReferencedByError) as failure:
        service.delete_model("fast")
    revision = repo.get("main").policy_revision
    assert failure.value.detail == {
        "message": "model fast 被策略引用",
        "references": [
            {"policy_id": "main", "policy_revision": revision,
             "target_id": "fast", "via": "target"},
            {"policy_id": "main", "policy_revision": revision,
             "target_id": "safe", "via": "target"},
        ],
    }


def test_configuration_service_validates_active_policy_and_publishes_once(conn, repo, stub):
    PolicyService(conn, repo, stub).create("main", policy_from_dict(policy_dict()))
    PolicyService(conn, repo, stub).enable("main")
    stub.reset()

    ConfigurationService(conn, repo, stub).save_jev(
        JevSettings("http://jev-2.test", "d1-2B"))
    assert len(stub.published) == 1
    assert stub.published[0].fingerprint == repo.fingerprint()

    # 删除未被策略引用的模型：active 策略校验通过后提交并发布恰一次。
    stub.reset()
    ConfigurationService(conn, repo, stub).delete_model("qwen3-8b-medical")
    assert len(stub.published) == 1
    assert stub.published[0].fingerprint == repo.fingerprint()
