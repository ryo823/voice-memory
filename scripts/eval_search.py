"""Search quality baseline. Usage: eval_search.py [--json OUT.json]

Builds a throwaway DB from tests/fixtures/search_eval.json (synthetic data only), runs
database.search_memories for every query and reports Recall@5 / Recall@10 / MRR.
"""
import argparse, json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from voice_memory import database as db  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "search_eval.json"
KS = (5, 10)


def load_fixture(path=FIXTURE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_db(fixture: dict, db_path: Path) -> dict[str, str]:
    """Create the fixture DB; returns {fixture key: memory id}."""
    db.init_db(db_path)
    ids = {}
    for m in fixture["memories"]:
        r = db.save_memory(m["title"], m["transcript"], m["summary"], m["tags"],
                           source="eval", recorded_at=m["recorded_at"], db_path=db_path)
        ids[m["key"]] = r["id"]
    return ids


def score(ranked: list[str], relevant: set[str]) -> dict:
    """Recall@k = relevant found in top k / all relevant; RR = 1/rank of first relevant (0 if none)."""
    out = {f"recall@{k}": len(relevant & set(ranked[:k])) / len(relevant) for k in KS}
    out["rr"] = next((1 / (i + 1) for i, r in enumerate(ranked) if r in relevant), 0.0)
    return out


def mean(rows: list[dict]) -> dict:
    n = len(rows)
    return {"n": n, **{k: sum(r[k] for r in rows) / n for k in (*(f"recall@{k}" for k in KS), "rr")}}


def evaluate(fixture: dict, db_path: Path) -> dict:
    ids = build_db(fixture, db_path)
    key_of = {v: k for k, v in ids.items()}
    per_query = []
    for q in fixture["queries"]:
        hits = db.search_memories(q["query"], limit=db.MAX_LIMIT, db_path=db_path)
        ranked = [key_of[h["id"]] for h in hits]
        per_query.append({**q, "returned": len(ranked), "top10": ranked[:10],
                          **score(ranked, set(q["relevant"]))})
    cats = sorted({q["category"] for q in per_query})
    return {
        "overall": mean(per_query),
        "by_category": {c: mean([q for q in per_query if q["category"] == c]) for c in cats},
        "queries": per_query,
        "missed": [q["id"] for q in per_query if q["rr"] == 0.0],
    }


def report(res: dict) -> str:
    def line(name, m):
        return f"{name:<12} n={m['n']:<3} R@5={m['recall@5']:.3f}  R@10={m['recall@10']:.3f}  MRR={m['rr']:.3f}"
    out = [line("OVERALL", res["overall"]), ""]
    out += [line(c, m) for c, m in res["by_category"].items()]
    out += ["", "id   category    R@5   R@10  RR    returned  query"]
    for q in res["queries"]:
        out.append(f"{q['id']}  {q['category']:<10} {q['recall@5']:.2f}  {q['recall@10']:.2f}  {q['rr']:.2f}  "
                   f"{q['returned']:>5}     {q['query']}" + ("   <- MISS" if q["rr"] == 0 else ""))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="also write the machine-readable result here")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        res = evaluate(load_fixture(), Path(d) / "eval.db")
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
