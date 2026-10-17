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


# --------------------------------------------------------------------------- #
# F4 (second audit round): the cross-replica latch imports the DB's failure
# count and its ORIGINAL opened_at; it does not reset opened_at = now.
# --------------------------------------------------------------------------- #
def _breaker_db(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    conn.close()
    return db


def _set_db_opened_at(db, model, opened_at):
    conn = connect(db)
    try:
        with conn:
            conn.execute("UPDATE breaker SET opened_at = ? WHERE model = ?",
                         (opened_at, model))
    finally:
        conn.close()


def _db_opened_at(db, model):
    conn = connect(db)
    try:
        row = conn.execute("SELECT opened_at FROM breaker WHERE model = ?",
                           (model,)).fetchone()
    finally:
        conn.close()
    return row[0]


def test_latch_imports_db_count_and_original_opened_at(tmp_path):
    """A replica that first sees the DB quarantine latches the DB state as-is:
    failure count = DB count, opened_at = the DB's original open time (mapped
    onto the local monotonic clock), not `now`."""
    import time

    db = _breaker_db(tmp_path)
    a = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    for _ in range(3):
        a.failure("m")
    opened_at_db = _db_opened_at(db, "m")
    assert opened_at_db is not None

    time.sleep(0.05)
    b = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    assert b.is_open("m") is True

    # count imported wholesale (not incremented / reset)
    assert b._failures["m"] == 3
    # local opened_at represents the same instant as the DB one
    elapsed_local = time.monotonic() - b._opened_at["m"]
    elapsed_db = time.time() - opened_at_db
    assert abs(elapsed_local - elapsed_db) < 2.0


def test_stale_db_quarantine_lets_latched_replica_probe_immediately(tmp_path):
    """F4: the old docstring claimed a latched replica is conservative. It is
    not once the DB row is older than the half-open window: the probe is
    allowed immediately (and that is the intended self-healing behaviour)."""
    import time

    db = _breaker_db(tmp_path)
    a = CircuitBreaker(threshold=3, half_open_after=60.0, db_path=db)
    for _ in range(3):
        a.failure("m")
    _set_db_opened_at(db, "m", time.time() - 3600.0)      # opened an hour ago

    c = CircuitBreaker(threshold=3, half_open_after=60.0, db_path=db)
    assert c.is_open("m") is False      # window long elapsed -> probe allowed
    assert c._failures["m"] == 3        # ... but the count was still imported


def test_latched_replica_keeps_window_while_db_row_is_fresh(tmp_path):
    db = _breaker_db(tmp_path)
    a = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    for _ in range(3):
        a.failure("m")

    b = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    assert b.is_open("m") is True
    assert b.is_open("m") is True       # stays open on repeated checks
