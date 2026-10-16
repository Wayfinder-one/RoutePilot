import sr.db as db

def test_init_creates_twelve_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"providers","models","tree","tag_settings","conf_settings",
            "settings","requests","tags","sessions","changes","estimates",
            "metric_counters"} <= names

def test_default_settings_present(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    s = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert s["session_sticky"] == "1"
    assert s["session_idle"] == "1800"
    assert s["sticky_detect_feedback"] == "0"
    assert s["budget_protect"] == "1"
    assert s["budget_water"] == "0.8"
    assert s["max_retries"] == "2"
    assert s["batch_window_ms"] == "5"
    assert s["retention_days"] == "30"
    assert s["ladder"] == "qwen3-8b,qwen3-32b,claude-sonnet"
    assert s["tree_version"] == "1"

def test_tree_version_bump(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    assert db.get_tree_version(conn) == 1
    db.bump_tree_version(conn)
    assert db.get_tree_version(conn) == 2

def test_models_seed_has_five(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    ids = {r[0] for r in conn.execute("SELECT id FROM models").fetchall()}
    assert ids == {"qwen3-8b", "qwen3-32b", "claude-sonnet",
                   "llava-omni", "sdxl-image"}

def test_migrate_adds_tree_version_column(tmp_path):
    """M2：旧库缺 requests.tree_version 时 init_db 自动 ALTER 补列（幂等）。"""
    path = str(tmp_path / "old.db")
    conn = db.init_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
    assert "tree_version" in cols
    conn.execute("ALTER TABLE requests DROP COLUMN tree_version")   # 模拟旧库
    conn.commit()
    conn.close()
    conn2 = db.init_db(path)                                        # 触发迁移
    try:
        cols2 = {r[1] for r in conn2.execute("PRAGMA table_info(requests)")}
        assert "tree_version" in cols2
    finally:
        conn2.close()


def test_models_not_in_ladder_have_step_zero(tmp_path):
    # backlog 修：多媒体种子曾写 step NULL，与 spec §3.1 注释「0=不在阶梯」不统一。
    conn = db.init_db(str(tmp_path / "t.db"))
    steps = dict(conn.execute("SELECT id, step FROM models").fetchall())
    assert steps["llava-omni"] == 0 and steps["sdxl-image"] == 0
    assert steps["qwen3-8b"] == 1 and steps["claude-sonnet"] == 3

def test_is_initialized_and_partial_db_rejected(tmp_path):
    # backlog 修：旧幂等探测只认 settings 表——残缺库会被误判为已初始化。
    import sqlite3
    import pytest
    path = str(tmp_path / "partial.db")
    assert db.is_initialized(path) is False           # 文件不存在
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit(); conn.close()
    assert db.is_initialized(path) is False            # 只有 settings，不算初始化
    with pytest.raises(RuntimeError):
        db.init_db(path)                              # 残缺库不静默续用
    # 全新路径正常建库，且幂等
    fresh = str(tmp_path / "fresh.db")
    assert db.is_initialized(fresh) is False
    db.init_db(fresh).close()
    assert db.is_initialized(fresh) is True
    db.init_db(fresh).close()                          # 幂等不炸

def test_balanced_template_loaded(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    tops = conn.execute(
        "SELECT COUNT(*) c FROM tree WHERE parent IS NULL").fetchone()["c"]
    assert tops == 8          # 起点 + 安全/多媒体/编程与推理/法律与事实/成本控制/专属/兜底
