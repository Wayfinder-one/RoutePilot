from router.routing.affinity import SessionAffinity


def test_sticky_reuses_only_same_tree_path_and_allowed_model():
    clock = [100.0]
    affinity = SessionAffinity(clock=lambda: clock[0])
    affinity.put("session-raw", "strong-a", tree_revision="tree-1", policy_path="root/jev", ttl_seconds=30)
    assert affinity.get("session-raw", tree_revision="tree-1", policy_path="root/jev", allowed_models=["strong-a", "strong-b"]) == "strong-a"
    assert affinity.get("session-raw", tree_revision="tree-2", policy_path="root/jev", allowed_models=["strong-a"]) is None
    affinity.put("session-raw", "strong-a", tree_revision="tree-1", policy_path="root/jev", ttl_seconds=30)
    assert affinity.get("session-raw", tree_revision="tree-1", policy_path="root/other", allowed_models=["strong-a"]) is None
    affinity.put("session-raw", "strong-a", tree_revision="tree-1", policy_path="root/jev", ttl_seconds=30)
    assert affinity.get("session-raw", tree_revision="tree-1", policy_path="root/jev", allowed_models=["fast"]) is None


def test_ttl_is_sliding_and_expires():
    clock = [0.0]
    affinity = SessionAffinity(clock=lambda: clock[0])
    affinity.put("s", "a", tree_revision="t", policy_path="p", ttl_seconds=10)
    clock[0] = 8
    assert affinity.get("s", tree_revision="t", policy_path="p", allowed_models=["a"]) == "a"
    clock[0] = 17
    assert affinity.get("s", tree_revision="t", policy_path="p", allowed_models=["a"]) == "a"
    clock[0] = 28
    assert affinity.get("s", tree_revision="t", policy_path="p", allowed_models=["a"]) is None


def test_lru_is_bounded_and_raw_session_is_not_a_cache_key():
    affinity = SessionAffinity(max_entries=2)
    for session in ("raw-one", "raw-two", "raw-three"):
        affinity.put(session, "a", tree_revision="t", policy_path="p", ttl_seconds=100)
    assert len(affinity) == 2
    assert affinity.get("raw-one", tree_revision="t", policy_path="p", allowed_models=["a"]) is None
    assert all(session not in key for key in affinity._entries for session in ("raw-one", "raw-two", "raw-three"))


def test_missing_session_never_reads_or_writes():
    affinity = SessionAffinity()
    assert affinity.get("", tree_revision="t", policy_path="p", allowed_models=["a"]) is None
    affinity.put("", "a", tree_revision="t", policy_path="p", ttl_seconds=100)
    assert len(affinity) == 0
