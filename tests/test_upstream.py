import pytest

from router.api.upstream import CircuitBreaker, load_backends, resolve_backend
from router.store.db import connect, migrate
from router.store.repositories import seed_default


def test_load_backends_and_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_KEY", "secret")
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE providers SET key_env = 'LOCAL_KEY' WHERE name = 'local'")
    backends = load_backends(conn)
    b = resolve_backend(backends, "qwen3-8b")
    assert b.url == "http://127.0.0.1:9000/chat/completions"
    assert b.headers["Authorization"] == "Bearer secret"
    assert b.upstream_name == "qwen3-8b"
    with pytest.raises(KeyError):
        resolve_backend(backends, "nope")
    conn.close()


def test_load_backends_omits_models_without_provider(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("DELETE FROM providers WHERE name = 'local'")
    assert load_backends(conn) == {}
    conn.close()


def test_circuit_breaker():
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    cb.failure("m"); cb.failure("m")
    assert not cb.is_open("m")
    cb.failure("m")
    assert cb.is_open("m")
    cb.success("m")
    assert not cb.is_open("m")


def test_half_open_after_window():
    cb = CircuitBreaker(threshold=1, half_open_after=0.0)
    cb.failure("m")
    assert not cb.is_open("m")         # window elapsed -> one probe allowed


def test_breaker_state_shared_across_replicas(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    conn.close()
    a = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    b = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    for _ in range(3):
        a.failure("m")
    assert a.is_open("m")
    assert b.is_open("m")              # replica b sees the DB quarantine
    a.success("m")                     # clears local + DB
    # a replica that latched (b) stays conservative; a fresh one is clear
    c = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    assert not c.is_open("m")


def test_breaker_without_db_is_process_local(tmp_path):
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    for _ in range(3):
        cb.failure("m")
    assert cb.is_open("m")
    cb.success("m")
    assert not cb.is_open("m")
