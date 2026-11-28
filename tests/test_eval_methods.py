"""Guards for scripts/eval_methods.py: Janome content-word extraction, methods A/B/B2/C/D smoke tests."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_methods", ROOT / "scripts" / "eval_methods.py")
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)

janome = pytest.importorskip("janome")
needs_model = pytest.mark.skipif(not em.model_available(), reason="ruri-v3-30m ONNX not downloaded (see docs/eval/week2-spike.md)")


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
    assert set(em.METHODS) == {"A", "B", "B2", "C", "D"}
    res = em.evaluate(em.es.load_fixture(), tmp_path / "a.db", em.METHODS["A"])
    assert round(res["overall"]["recall@5"], 3) == 0.431 and round(res["overall"]["rr"], 3) == 0.444
    assert res["no_match"]["false_positives"] == 0


def test_method_b_beats_baseline_on_paraphrase_family(tmp_path):
    res = em.evaluate(em.es.load_fixture(), tmp_path / "b.db", em.METHODS["B"])
    assert res["overall"]["recall@5"] > 0.431
    assert res["by_category"]["composite"]["recall@5"] > 0


def test_b2_keeps_keiyoudoushi_stem_that_b_drops():
    assert em.content_words("健康") == []
    assert em.content_words("健康", em.CONTENT_POS_B2) == ["健康"]
    assert em.content_words("食器を自動で洗う機械", em.CONTENT_POS_B2) == em.content_words("食器を自動で洗う機械")


def test_rrf_fuses_two_rankings():
    assert em.rrf(["a", "b", "c"], ["c", "b", "d"]) == ["c", "b", "a", "d"]
    assert em.rrf(["a"], []) == ["a"]


@needs_model
def test_method_c_and_d_rank_paraphrase_first(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "CACHE_DIR", tmp_path / "cache")
    db_path = tmp_path / "t.db"
    d = em.es.db
    d.init_db(db_path)
    save = lambda title, body, at: d.save_memory(title, body, "", [], source="t", recorded_at=at, db_path=db_path)["id"]
    moving = save("引っ越しの検討", "来月、新しい家に転居するので不動産屋に相談した", "2026-10-01T09:00:00")
    save("夕飯の献立", "今夜はカレーを作った", "2026-10-02T09:00:00")
    save("車検の予約", "ディーラーに電話して車検を予約した", "2026-10-03T09:00:00")
    ranked = em.search_c("転居", db_path)
    assert len(ranked) == 3 and ranked[0] == moving
    assert em.search_d("転居", db_path)[0] == moving
    assert (tmp_path / "cache").exists()  # document embeddings were cached
