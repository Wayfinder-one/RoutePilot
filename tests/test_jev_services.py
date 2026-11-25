"""``jev_services`` 存储：只保存非密钥目录字段，密钥不再进入 SQLite。

v7 删除了 ``api_key`` 列与 ``stored_api_key()`` 读取接口；``snapshot_dict()``
仍输出固定的内核环境变量名，仅供图文档/画布运行配置兼容。
"""
import inspect

from router.jev.service import JevService
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo


def service(service_id="jev-a", model="d1-a", base_url="http://jev-a.test", **overrides):
    values = {
        "id": service_id, "base_url": base_url, "model": model,
        "key_env": "JEV_A_API_KEY", "input_tokens": 2048, "context_tokens": 4096,
    }
    values.update(overrides)
    return JevService(**values)


def _columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(jev_services)")}


def test_service_roundtrip_has_no_secret_and_fixed_kernel_key_env(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service())
    loaded = repo.get("jev-a")
    assert loaded.id == "jev-a"
    assert loaded.base_url == "http://jev-a.test"
    assert loaded.model == "d1-a"
    assert loaded.snapshot_dict() == {
        "id": "jev-a", "base_url": "http://jev-a.test", "model": "d1-a",
        "key_env": "ROUTER_JEV_API_KEY", "timeout_seconds": 10.0,
        "instructions": loaded.instructions, "state_template": loaded.state_template,
    }
    assert loaded.window_dict() == {"input_tokens": 2048, "context_tokens": 4096}
    assert "api_key" not in loaded.snapshot_dict()
    assert "api_key" not in _columns(conn)
    conn.close()


def test_save_takes_no_api_key_argument(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    parameters = inspect.signature(JevServiceRepo.save).parameters
    assert list(parameters) == ["self", "service"]
    assert "api_key" not in inspect.signature(repo.save).parameters
    assert not hasattr(repo, "stored_api_key")
    conn.close()


def test_catalog_read_fixes_key_env_to_the_kernel_variable(tmp_path):
    """旧数据可能带着任意环境变量名；目录读取只输出内核固定变量名。"""
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(key_env="LEGACY_JEV_API_KEY"))
    loaded = repo.get("jev-a")
    assert loaded.key_env == "ROUTER_JEV_API_KEY"
    assert loaded.snapshot_dict()["key_env"] == "ROUTER_JEV_API_KEY"
    # 列值只是历史数据，升级时按非密钥列保留；读取投影不再使用它。
    assert conn.execute(
        "SELECT key_env FROM jev_services WHERE id = 'jev-a'").fetchone()[0] == (
            "LEGACY_JEV_API_KEY")
    conn.close()


def test_upsert_updates_non_secret_config_and_window(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service())
    repo.save(service(model="d1-b", base_url="http://jev-b.test",
                      timeout_seconds=4.5, instructions="新指令",
                      state_template="新模板 {request}",
                      input_tokens=1024, context_tokens=3072))
    loaded = repo.get("jev-a")
    assert (loaded.base_url, loaded.model) == ("http://jev-b.test", "d1-b")
    assert loaded.timeout_seconds == 4.5
    assert loaded.instructions == "新指令"
    assert loaded.state_template == "新模板 {request}"
    assert loaded.window_dict() == {"input_tokens": 1024, "context_tokens": 3072}
    assert len(repo.list()) == 1
    conn.close()


def test_list_is_ordered_by_id_and_delete_reports_missing(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    for service_id in ("jev-c", "jev-a", "jev-b"):
        repo.save(service(service_id))
    assert [item.id for item in repo.list()] == ["jev-a", "jev-b", "jev-c"]
    assert repo.delete("jev-b") is True
    assert repo.delete("jev-b") is False
    assert [item.id for item in repo.list()] == ["jev-a", "jev-c"]
    assert repo.get("missing") is None
    conn.close()
