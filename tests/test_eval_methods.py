"""Guards for scripts/eval_methods.py: Janome content-word extraction, method B smoke test, method A baseline."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_methods", ROOT / "scripts" / "eval_methods.py")
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)

janome = pytest.importorskip("janome")


def test_content_words_keeps_nouns_verbs_adjectives_only():
    assert em.content_words("食器を自動で洗う機械") == ["食器", "自動", "洗う", "機械"]  # particles dropped, verb in base form
    assert em.content_words("ネット回線が遅い") == ["ネット", "回線", "遅い"]


def test_content_words_dedupes_and_drops_light_verbs():
    assert em.content_words("面談の面談をする") == ["面談"]
    assert em.content_words("の と を") == []


def test_method_b_or_search_and_short_term_like(tmp_path):
    db_path = tmp_path / "t.db"
    em.es.db.init_db(db_path)
    save = lambda title, body, at: em.es.db.save_memory(title, body, "", [], source="t", recorded_at=at, db_path=db_path)["id"]
    fts = save("食洗機の設置", "食器を洗う機械を設置した", "2026-10-01T09:00:00")
    short = save("引っ越し", "転居の手続きをした", "2026-10-02T09:00:00")
    save("無関係", "今日は晴れだった", "2026-10-03T09:00:00")
    # every extracted word here is under 3 characters, so this goes through the LIKE path
    assert em.search_b("食器を自動で洗う機械", db_path) == [fts]
    assert em.search_b("転居", db_path) == [short]
    assert em.search_b("ハワイ旅行", db_path) == []


def test_method_b_long_term_ranked_by_bm25_before_like_candidates(tmp_path):
    db_path = tmp_path / "t.db"
    em.es.db.init_db(db_path)
    d = em.es.db
    a = d.save_memory("Raspberryの話", "メモ", "", [], source="t", recorded_at="2026-10-01T09:00:00", db_path=db_path)["id"]
    b = d.save_memory("別件", "話だけ", "", [], source="t", recorded_at="2026-10-05T09:00:00", db_path=db_path)["id"]
    # "Raspberry" (FTS hit) must come before the newer LIKE-only "話" candidate
    assert em.search_b("Raspberryの話", db_path) == [a, b]


def test_registry_and_method_a_matches_week1_baseline(tmp_path):
    assert set(em.METHODS) == {"A", "B"}
    res = em.evaluate(em.es.load_fixture(), tmp_path / "a.db", em.METHODS["A"])
    assert round(res["overall"]["recall@5"], 3) == 0.431 and round(res["overall"]["rr"], 3) == 0.444
    assert res["no_match"]["false_positives"] == 0


def test_method_b_beats_baseline_on_paraphrase_family(tmp_path):
    res = em.evaluate(em.es.load_fixture(), tmp_path / "b.db", em.METHODS["B"])
    assert res["overall"]["recall@5"] > 0.431
    assert res["by_category"]["composite"]["recall@5"] > 0
