import asyncio
from dataclasses import FrozenInstanceError

import pytest

from router.routing.snapshot import build_snapshot
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.upstream.breaker import CircuitBreaker
from router.upstream.breaker_store import BreakerStore
from router.upstream.breaker_worker import BreakerSyncWorker
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


def test_breaker_without_worker_stays_process_local():
    cb = CircuitBreaker(threshold=3, half_open_after=300.0)
    for _ in range(3):
        cb.failure("m")
    assert cb.is_open("m")
    cb.success("m")
    assert not cb.is_open("m")


def test_breaker_state_machine_has_no_storage():
    """The pure state machine carries no sqlite import and no db_path knob."""
    import inspect

    import router.upstream.breaker as breaker_module

    assert not hasattr(breaker_module, "sqlite3")
    assert not hasattr(CircuitBreaker, "_db_read")
    assert not hasattr(CircuitBreaker, "_db_write")
    assert "db_path" not in inspect.signature(CircuitBreaker).parameters
    assert "db_sync_seconds" not in inspect.signature(CircuitBreaker).parameters


def test_is_open_and_failure_never_open_the_database(monkeypatch):
    """is_open()/failure()/success() must stay pure memory: sqlite is poisoned."""
    import sqlite3 as sqlite_module

    def boom(*args, **kwargs):
        raise AssertionError("circuit breaker touched sqlite")
    monkeypatch.setattr(sqlite_module, "connect", boom)

    cb = CircuitBreaker(threshold=1, half_open_after=300.0)
    for _ in range(5):
        cb.failure("m")
        assert cb.is_open("m") is True
    cb.success("m")
    assert cb.is_open("m") is False


# --------------------------------------------------------------------------- #
# 2026-10-21 audit P0 2.2 / P2 4.3: failure() must re-arm the open clock.
# --------------------------------------------------------------------------- #
def test_failed_probe_rearms_the_circuit():
    """P0 2.2: after the half-open window a failed probe must close the circuit
    again. The old setdefault kept the ORIGINAL opened_at, so one elapsed
    window permanently disarmed the breaker."""
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


# --------------------------------------------------------------------------- #
# Task 4: cross-replica quarantine lives in BreakerStore (SQLite) and is synced
# explicitly by BreakerSyncWorker. These tests call the sync methods directly;
# no request-path (is_open) call ever reads the database.
# --------------------------------------------------------------------------- #
def _breaker_db(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    conn.close()
    return db


def _replica(db, *, threshold=3, half_open_after=300.0):
    breaker = CircuitBreaker(threshold=threshold, half_open_after=half_open_after)
    return breaker, BreakerSyncWorker(breaker, BreakerStore(db))


def _sync(worker):
    asyncio.run(worker.sync_once())


def _stored(db, model):
    return BreakerStore(db).load_all().get(model)


def test_breaker_state_shared_across_replicas(tmp_path):
    db = _breaker_db(tmp_path)
    a, worker_a = _replica(db)
    for _ in range(3):
        a.failure("m")
    assert a.is_open("m")
    assert _stored(db, "m") is None     # nothing written until the worker syncs
    _sync(worker_a)
    assert _stored(db, "m")[0] == 3

    b, worker_b = _replica(db)
    assert not b.is_open("m")           # not loaded before the sync
    _sync(worker_b)
    assert b.is_open("m")               # replica b sees the shared quarantine

    a.success("m")
    _sync(worker_a)                      # clears the shared row
    assert not a.is_open("m")           # ... without re-latching itself
    c, worker_c = _replica(db)
    _sync(worker_c)
    assert not c.is_open("m")           # a fresh replica is clear
    assert b.is_open("m")               # ... a latched replica stays conservative


def test_recovery_is_not_undone_by_the_same_tick_import(tmp_path):
    """Regression repro: 3 failures -> sync -> success() -> sync.

    The shared row is cleared in the clear loop, but `shared` was snapshotted
    BEFORE that loop; importing from it re-latches the model this same tick and
    silently undoes the recovery. Both the replica and the shared table must be
    clear afterwards.
    """
    db = _breaker_db(tmp_path)
    a, worker_a = _replica(db)
    for _ in range(3):
        a.failure("m")
    _sync(worker_a)
    assert a.is_open("m") is True
    assert _stored(db, "m")[0] == 3

    a.success("m")
    _sync(worker_a)

    assert not a.is_open("m")
    assert a._failures.get("m") is None
    assert "m" not in BreakerStore(db).load_all()


def test_latch_imports_shared_count_and_original_opened_at(tmp_path):
    """A replica that first sees the shared quarantine latches it as-is:
    failure count = shared count, opened_at = the ORIGINAL open time (mapped
    onto the local monotonic clock), not `now`."""
    import time

    db = _breaker_db(tmp_path)
    a, worker_a = _replica(db)
    for _ in range(3):
        a.failure("m")
    _sync(worker_a)
    opened_at_stored = _stored(db, "m")[1]
    assert opened_at_stored is not None

    time.sleep(0.05)
    b, worker_b = _replica(db)
    _sync(worker_b)
    assert b.is_open("m") is True

    # count imported wholesale (not incremented / reset)
    assert b._failures["m"] == 3
    # local opened_at represents the same instant as the stored one
    elapsed_local = time.monotonic() - b._opened_at["m"]
    elapsed_stored = time.time() - opened_at_stored
    assert abs(elapsed_local - elapsed_stored) < 2.0


def test_stale_shared_quarantine_lets_latched_replica_probe_immediately(tmp_path):
    """F4: a row older than the half-open window lets the latched replica probe
    immediately (intended self-healing behaviour)."""
    import time

    db = _breaker_db(tmp_path)
    BreakerStore(db).write_failure("m", 3, time.time() - 3600.0)   # an hour old

    cb, worker = _replica(db, half_open_after=60.0)
    _sync(worker)
    assert cb.is_open("m") is False      # window long elapsed -> probe allowed
    assert cb._failures["m"] == 3        # ... but the count was still imported


def test_latched_replica_keeps_window_while_shared_row_is_fresh(tmp_path):
    db = _breaker_db(tmp_path)
    a, worker_a = _replica(db)
    for _ in range(3):
        a.failure("m")
    _sync(worker_a)

    b, worker_b = _replica(db)
    _sync(worker_b)
    assert b.is_open("m") is True
    assert b.is_open("m") is True        # stays open on repeated checks


def test_stale_shared_row_cannot_disarm_later_failures(tmp_path):
    """P0 2.2 repro: latching an hours-old quarantine, then failing again, must
    re-open the circuit from now (and refresh the shared row)."""
    import time

    db = _breaker_db(tmp_path)
    BreakerStore(db).write_failure("m", 3, time.time() - 1000.0)

    cb, worker = _replica(db, half_open_after=60.0)
    _sync(worker)
    assert cb.is_open("m") is False              # stale row -> probe allowed
    for _ in range(5):
        cb.failure("m")
        assert cb.is_open("m") is True           # stays re-armed
    _sync(worker)
    failures, opened_at = _stored(db, "m")
    assert failures == 8                         # 3 imported + 5 local failures
    assert abs(opened_at - time.time()) < 5.0


def test_worker_does_not_rewrite_unchanged_state(tmp_path, monkeypatch):
    """The periodic push only writes state that changed locally, so two
    replicas with different counts cannot overwrite each other every tick."""
    db = _breaker_db(tmp_path)
    cb, worker = _replica(db)
    for _ in range(3):
        cb.failure("m")
    _sync(worker)

    writes = []
    monkeypatch.setattr(worker.store, "write_failure",
                        lambda *args: writes.append(args))
    _sync(worker)                                # unchanged -> no write
    assert writes == []
    cb.failure("m")                              # local change -> one write
    _sync(worker)
    assert [args[0] for args in writes] == ["m"]
    assert writes[0][1] == 4


def test_healthy_requests_never_write_the_breaker_table(tmp_path):
    """A replica that never opened a circuit keeps the store empty."""
    db = _breaker_db(tmp_path)
    cb, worker = _replica(db)
    cb.is_open("m")                              # healthy read only
    cb.success("m")                              # no local state -> no delete
    _sync(worker)
    assert BreakerStore(db).load_all() == {}


def test_worker_start_loads_shared_quarantine_before_returning(tmp_path):
    """start() awaits one sync, so startup ordering guarantees the shared
    quarantine is in memory before the app serves traffic."""
    import time

    db = _breaker_db(tmp_path)
    BreakerStore(db).write_failure("m", 3, time.time())
    cb, worker = _replica(db)

    async def scenario():
        await worker.start()
        try:
            assert cb.is_open("m") is True
        finally:
            await worker.close()

    asyncio.run(scenario())


def test_app_lifespan_loads_shared_quarantine_before_traffic(tmp_path):
    """The app serves traffic only after the shared quarantine is in memory."""
    import time

    from fastapi.testclient import TestClient

    from router.api.app import create_app
    from tests.routing_fixtures import test_settings

    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    BreakerStore(settings.db_path).write_failure("fast", 3, time.time())

    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.breaker.is_open("fast") is True
        assert client.get("/healthz").json() == {"ok": True}


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
