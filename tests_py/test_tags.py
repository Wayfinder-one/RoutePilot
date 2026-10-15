from sr.tags import TAGS, DOMAIN_KEYS, normalize, conf_band

def test_registry_has_17_tags():
    assert len(TAGS) == 17
    assert sum(1 for t in TAGS.values() if t.source == "model") == 9
    assert sum(1 for t in TAGS.values() if t.source == "rule") == 8

def test_domain_keys_match_checkpoint_order():
    assert DOMAIN_KEYS == ["biology","business","chemistry","computer_science",
        "economics","engineering","health","history","law","math","other",
        "philosophy","physics","psychology"]

def test_normalize_lowercase_and_underscore():
    assert normalize("domain", "computer science") == "computer_science"
    assert normalize("pii", "CN_ID_CARD") == "cn_id_card"

def test_normalize_aliases():
    assert normalize("modality", "AR") == "text"
    assert normalize("modality", "DIFFUSION") == "image"
    assert normalize("user_feedback", "SAT") == "satisfied"
    assert normalize("fact_check", "FACT_CHECK_NEEDED") == "needs_check"

def test_normalize_ordered_band_index():
    assert normalize("complexity", "hard") == 1      # 有序档存索引（§3.1 tags 表约定）
    assert normalize("complexity", "easy") == 0

def test_normalize_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        normalize("domain", "astrology")

def test_conf_band_default_cuts():
    assert conf_band(0.10) == 0
    assert conf_band(0.45) == 1
    assert conf_band(0.75) == 2
    assert conf_band(0.95) == 3
