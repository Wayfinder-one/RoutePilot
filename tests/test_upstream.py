from dataclasses import FrozenInstanceError

import pytest

from router.routing.snapshot import build_snapshot
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend, load_backends


def test_load_backends_returns_model_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_KEY", "secret")
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE providers SET key_env = 'LOCAL_KEY' WHERE name = 'local'")
    backends = load_backends(conn)
    b = backends["qwen3-8b"]
    assert b.url == "http://127.0.0.1:8000/v1/chat/completions"
    assert b.headers["Authorization"] == "Bearer secret"
    assert b.upstream_name == "qwen3-8b"
    assert "nope" not in backends        # unknown model is simply absent
    conn.close()


def test_backend_is_frozen():
    backend = Backend(url="http://u", headers={}, upstream_name="m")
    with pytest.raises(FrozenInstanceError):
        backend.upstream_name = "other"


def test_snapshot_copies_backend_mutable_leaves():
    headers = {"Authorization": "Bearer k"}
    reasoning = {"declared": True, "supported": True, "format": "openai",
                 "efforts": [{"id": "high", "wire": "high"}]}
    backend = Backend(url="http://u", headers=headers, upstream_name="m",
                      reasoning=reasoning)
    snapshot = build_snapshot(tree=None, backends={"m": backend},
                              time_periods=[], jev=None, capabilities={})
    published = snapshot.backends["m"]
    assert published is not backend
    assert published == backend
    assert published.headers is not headers
    assert published.reasoning is not reasoning
    assert published.reasoning["efforts"] is not reasoning["efforts"]
    # Later edits to the catalog result cannot leak into the published snapshot.
    headers["Authorization"] = "Bearer changed"
    reasoning["efforts"].append({"id": "off", "wire": None})
    assert published.headers["Authorization"] == "Bearer k"
    assert len(published.reasoning["efforts"]) == 1


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


# --------------------------------------------------------------------------- #
# 2026-10-21 audit P0 2.2 / P2 4.3: failure() must re-arm the open clock, and
# the DB sync is low-rate with writes only on state changes.
# --------------------------------------------------------------------------- #
def test_failed_probe_rearms_the_circuit():
    """P0 2.2: after the half-open window a failed probe must close the circuit
    again. The old setdefault kept the ORIGINAL opened_at, so one elapsed
    window permanently disarmed the breaker (in memory AND via a latched DB
    row)."""
    import time

    cb = CircuitBreaker(threshold=3, half_open_after=0.05)
    cb.failure("m"); cb.failure("m"); cb.failure("m")
    assert cb.is_open("m") is True
    time.sleep(0.06)
    assert cb.is_open("m") is False              # window elapsed -> probe
    cb.failure("m")                              # probe failed
    assert cb.is_open("m") is True               # circuit re-armed


# --------------------------------------------------------------------------- #
# P3-29: half-open admits a TRUE single probe — concurrent requests stay
# blocked until the probe concludes (success/failure) or times out.
# --------------------------------------------------------------------------- #
def _open_circuit(cb, model="m", failures=3):
    for _ in range(failures):
        cb.failure(model)


def test_half_open_admits_exactly_one_probe():
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    _open_circuit(cb)
    assert cb.is_open("m") is True
    cb._opened_at["m"] -= 400.0                  # window elapsed
    results = [cb.is_open("m") for _ in range(5)]
    assert results == [False, True, True, True, True]


def test_probe_success_fully_recovers():
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    _open_circuit(cb)
    cb._opened_at["m"] -= 400.0
    assert cb.is_open("m") is False              # probe admitted
    cb.success("m")
    assert cb.is_open("m") is False
    assert cb.is_open("m") is False              # recovered, not more probes


def test_failed_probe_recloses_and_retimes_window():
    import time

    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    _open_circuit(cb)
    cb._opened_at["m"] -= 400.0
    assert cb.is_open("m") is False              # probe admitted
    cb.failure("m")                              # probe failed
    assert cb.is_open("m") is True               # re-closed ...
    assert time.monotonic() - cb._opened_at["m"] < 1.0   # ... window re-timed


def test_timed_out_probe_allows_a_replacement():
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    _open_circuit(cb)
    cb._opened_at["m"] -= 400.0
    assert cb.is_open("m") is False              # first probe
    assert cb.is_open("m") is True               # still in flight
    cb._probe_at["m"] -= cb.probe_timeout + 1.0  # probe wedged past timeout
    assert cb.is_open("m") is False              # replacement probe admitted
    assert cb.is_open("m") is True


def test_stale_db_row_cannot_disarm_later_failures(tmp_path):
    """P0 2.2 repro: latching an hours-old DB quarantine, then failing again,
    must re-open the circuit from now (and refresh the DB row)."""
    import time

    db = _breaker_db(tmp_path)
    conn = connect(db)
    conn.execute("INSERT INTO breaker (model, failures, opened_at) "
                 "VALUES ('m', 3, ?)", (time.time() - 1000.0,))
    conn.commit()
    conn.close()

    cb = CircuitBreaker(threshold=3, half_open_after=60.0, db_path=db)
    assert cb.is_open("m") is False              # stale row -> probe allowed
    for _ in range(5):
        cb.failure("m")
        assert cb.is_open("m") is True           # stays re-armed
    assert abs(_db_opened_at(db, "m") - time.time()) < 5.0


def test_db_reads_are_rate_limited_per_model(tmp_path, monkeypatch):
    """P2 4.3: is_open hits the DB at most once per db_sync_seconds per model."""
    db = _breaker_db(tmp_path)
    cb = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db,
                        db_sync_seconds=30.0)
    calls = []
    original = cb._db_read

    def counting(model):
        calls.append(model)
        return original(model)

    monkeypatch.setattr(cb, "_db_read", counting)
    for _ in range(3):
        cb.is_open("m")
    assert len(calls) == 1                       # first check only
    cb._db_read_at["m"] = 0.0                    # pretend the window elapsed
    cb.is_open("m")
    assert len(calls) == 2


def test_healthy_requests_never_write_the_breaker_table(tmp_path, monkeypatch):
    """P2 4.3: success() without local failure state must not open the DB."""
    db = _breaker_db(tmp_path)
    cb = CircuitBreaker(threshold=3, half_open_after=300.0, db_path=db)
    writes = []
    monkeypatch.setattr(cb, "_db_write",
                        lambda *a: writes.append(a))
    cb.is_open("m")                              # reads once, no row
    cb.success("m")
    assert writes == []


# --------------------------------------------------------------------------- #
# v9: reasoning capability + stored-key priority in the backend snapshot
# --------------------------------------------------------------------------- #
def test_load_backends_carries_reasoning_and_provider(tmp_path):
    import json

    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE providers SET reasoning_format = 'deepseek' "
                 "WHERE name = 'local'")
    conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                 (json.dumps({"domains": [], "reasoning": {
                     "efforts": {"off": None, "high": None}}}),))
    conn.commit()
    backends = load_backends(conn)
    backend = backends["qwen3-8b"]
    assert backend.provider == "local"
    assert backend.reasoning["declared"] is True
    assert backend.reasoning["format"] == "deepseek"
    assert backend.reasoning["efforts"] == [{"id": "off", "wire": None},
                                            {"id": "high", "wire": None}]
    assert backends["qwen3-32b"].reasoning is None      # undeclared -> passthrough
    conn.close()


def test_load_backends_ignores_corrupt_or_illegal_declarations(tmp_path):
    import json

    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE models SET profile = 'not-json' WHERE id = 'qwen3-8b'")
    conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-32b'",
                 (json.dumps({"reasoning": {"format": "nope",
                                            "efforts": {"high": "high"}}}),))
    conn.execute("UPDATE models SET profile = ? WHERE id = 'deepseek-v3'",
                 (json.dumps({"reasoning": False}),))
    conn.commit()
    backends = load_backends(conn)
    assert backends["qwen3-8b"].reasoning is None      # corrupt JSON tolerated
    assert backends["qwen3-32b"].reasoning is None     # illegal declaration tolerated
    assert backends["deepseek-v3"].reasoning is None   # explicit "unsupported"
    conn.close()


def test_load_backends_stored_api_key_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_KEY", "env-secret")
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE providers SET key_env = 'LOCAL_KEY' WHERE name = 'local'")
    conn.commit()
    # env only
    assert load_backends(conn)["qwen3-8b"].headers["Authorization"] == \
        "Bearer env-secret"
    # stored key wins
    conn.execute("UPDATE providers SET api_key = 'stored-secret' WHERE name = 'local'")
    conn.commit()
    assert load_backends(conn)["qwen3-8b"].headers["Authorization"] == \
        "Bearer stored-secret"
    # clearing the stored key falls back to the env
    conn.execute("UPDATE providers SET api_key = '' WHERE name = 'local'")
    conn.commit()
    assert load_backends(conn)["qwen3-8b"].headers["Authorization"] == \
        "Bearer env-secret"
    # no key at all -> no Authorization header
    conn.execute("UPDATE providers SET key_env = '' WHERE name = 'local'")
    conn.commit()
    assert "Authorization" not in load_backends(conn)["qwen3-8b"].headers
    conn.close()
