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
    assert 30 <= len(keys) <= 50 and len(set(keys)) == len(keys)
    qs = fixture["queries"]
    assert 15 <= len(qs) <= 20
    assert {q["category"] for q in qs} == {"exact", "short", "paraphrase", "tag"}
    for q in qs:
        assert q["relevant"] and set(q["relevant"]) <= set(keys)


def test_score_known_ranking():
    s = ev.score(["a", "b", "c", "d", "e", "f", "g"], {"c", "g", "z"})
    assert s["recall@5"] == pytest.approx(1 / 3)
    assert s["recall@10"] == pytest.approx(2 / 3)
    assert s["rr"] == pytest.approx(1 / 3)


def test_score_no_hit():
    assert ev.score(["a"], {"x"}) == {"recall@5": 0.0, "recall@10": 0.0, "rr": 0.0}


def test_baseline_of_current_search(fixture, tmp_path):
    """Pins today's behaviour (FTS AND match, newest first). If search changes on purpose,
    re-run scripts/eval_search.py and update this and docs/search-eval-baseline.md together."""
    res = ev.evaluate(fixture, tmp_path / "eval.db")
    o = res["overall"]
    assert (o["recall@5"], o["recall@10"], o["rr"]) == pytest.approx((0.6625, 0.7, 0.7))
    assert res["missed"] == ["q11", "q12", "q13", "q14", "q15", "q16"]
    json.dumps(res, ensure_ascii=False)  # machine-readable output stays serializable
