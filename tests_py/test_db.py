import sr.db as db

def test_init_creates_ten_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"providers","models","tree","tag_settings","conf_settings",
            "settings","requests","tags","sessions","changes","estimates"} <= names

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

def test_balanced_template_loaded(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    tops = conn.execute(
        "SELECT COUNT(*) c FROM tree WHERE parent IS NULL").fetchone()["c"]
    assert tops == 8          # 起点 + 安全/多媒体/编程与推理/法律与事实/成本控制/专属/兜底
