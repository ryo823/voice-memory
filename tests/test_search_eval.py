"""Guards for the search-eval harness (scripts/eval_search.py) and the pinned baseline of the current search."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_search", ROOT / "scripts" / "eval_search.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


@pytest.fixture(scope="module")
def fixture():
    return ev.load_fixture()


def test_fixture_is_consistent(fixture):
    keys = [m["key"] for m in fixture["memories"]]
    assert len(keys) == 120 and len(set(keys)) == len(keys)
    qs = fixture["queries"]
    assert len(qs) == 64 and len({q["id"] for q in qs}) == 64
    counts = {c: sum(q["category"] == c for q in qs) for c in {q["category"] for q in qs}}
    assert counts == {"exact": 6, "short": 4, "tag": 4, "paraphrase": 15, "temporal": 10,
                      "person_event": 10, "composite": 5, "no_match": 10}
    for q in qs:
        assert set(q["relevant"]) <= set(keys)
        assert bool(q["relevant"]) == (q["category"] != "no_match")


def test_score_known_ranking():
    s = ev.score(["a", "b", "c", "d", "e", "f", "g"], {"c", "g", "z"})
    assert s["recall@5"] == pytest.approx(1 / 3)
    assert s["recall@10"] == pytest.approx(2 / 3)
    assert s["rr"] == pytest.approx(1 / 3)


def test_score_no_hit():
    assert ev.score(["a"], {"x"}) == {"recall@5": 0.0, "recall@10": 0.0, "rr": 0.0}


def test_score_no_relevant_does_not_divide_by_zero():
    assert ev.score(["a", "b"], set()) == {"recall@5": None, "recall@10": None, "rr": None}


def test_no_match_is_reported_separately(tmp_path):
    """no_match queries stay out of the Recall/MRR averages and are counted as false positives when they return anything."""
    mem = lambda k, t: {"key": k, "title": t, "transcript": t, "summary": "s", "tags": ["x"],
                        "recorded_at": "2026-09-01T09:00:00"}
    fx = {"memories": [mem("a", "苺のジャム作り"), mem("b", "桃のゼリー")],
          "queries": [{"id": "h", "category": "exact", "query": "苺のジャム", "relevant": ["a"]},
                      {"id": "n1", "category": "no_match", "query": "蜜柑の缶詰", "relevant": []},
                      {"id": "n2", "category": "no_match", "query": "桃のゼリー", "relevant": []}]}
    res = ev.evaluate(fx, tmp_path / "nm.db")
    assert res["overall"]["n"] == 1 and res["overall"]["recall@5"] == 1.0
    assert res["no_match"] == {"n": 2, "false_positives": 1, "false_positive_rate": 0.5}
    assert res["by_category"]["no_match"] == res["no_match"]
    assert res["false_positives"] == ["n2"] and res["missed"] == []
    assert "false positive rate=0.500" in ev.report(res)
    json.dumps(res, ensure_ascii=False)


def test_baseline_of_current_search(fixture, tmp_path):
    """Pins the metrics of the default search (FTS AND match; bm25 and newest-first score identically on this set). If search changes on purpose,
    re-run scripts/eval_search.py and update this and docs/search-eval-baseline.md together."""
    res = ev.evaluate(fixture, tmp_path / "eval.db")
    o = res["overall"]
    assert o["n"] == 54
    assert (o["recall@5"], o["recall@10"], o["rr"]) == pytest.approx((0.4306, 0.4444, 0.4444), abs=1e-3)
    cats = res["by_category"]
    for c in ("exact", "short", "person_event"):
        assert cats[c]["rr"] == 1.0
    for c in ("paraphrase", "temporal", "composite"):
        assert cats[c]["rr"] == 0.0
    assert res["no_match"]["false_positive_rate"] == 0.0
    assert {q for q in res["missed"]} == {q["id"] for q in fixture["queries"]
                                          if q["category"] in ("paraphrase", "temporal", "composite")}
    json.dumps(res, ensure_ascii=False)  # machine-readable output stays serializable
