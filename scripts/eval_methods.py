"""Compare search methods on the eval fixture. Usage: eval_methods.py [--method A|B|all] [--json OUT.json]

Builds the throwaway DB from tests/fixtures/search_eval.json (synthetic data only) via eval_search, runs every
query through each registered method and reports Recall@5/@10, MRR (no_match excluded), the no_match false
positive rate and the mean response time per query. Methods are plain functions registered with @method("X").

A  current search_memories(order="bm25"): AND over whitespace terms, trigram FTS + LIKE for short terms.
B  morphological OR search (needs `pip install .[eval]` for Janome):
     1. Janome tokenizes the query; keep content words only (noun general / proper / suru-verb stem,
        independent verb, independent adjective; verbs and adjectives in dictionary form), de-duplicated,
        minus the light verbs in STOP_WORDS.
     2. Terms of 3+ characters are OR-ed into one FTS5 MATCH on the trigram index, ranked by bm25()
        with the same column weights as search_memories.
     3. Terms under 3 characters are not in the trigram index: memories matching any of them by LIKE on
        title / summary / transcript / tags are appended after the FTS hits (most terms matched first,
        then newest), skipping memories already returned.
"""
import argparse, importlib.util, json, sys, tempfile, time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from voice_memory import database as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("eval_search", ROOT / "scripts" / "eval_search.py")
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)

METHODS: dict = {}


def method(name: str):
    def register(fn):
        METHODS[name] = fn
        return fn
    return register


# ---- method A ---------------------------------------------------------------------------------

@method("A")
def search_a(query: str, db_path: Path) -> list[str]:
    return [h["id"] for h in db.search_memories(query, limit=db.MAX_LIMIT, db_path=db_path, order="bm25")]


# ---- method B ---------------------------------------------------------------------------------

_tokenizer = None
CONTENT_POS = {("名詞", "一般"), ("名詞", "固有名詞"), ("名詞", "サ変接続"), ("動詞", "自立"), ("形容詞", "自立")}
# IPADIC tags these as independent verbs but they carry no topic and would LIKE-match nearly every memory.
STOP_WORDS = {"する", "いる", "ある", "なる"}


def content_words(query: str) -> list[str]:
    """Content words of `query` in order of appearance, de-duplicated (see module docstring, step 1)."""
    global _tokenizer
    if _tokenizer is None:
        try:
            from janome.tokenizer import Tokenizer
        except ImportError:
            raise SystemExit("method B needs Janome: pip install -e '.[eval]'")
        _tokenizer = Tokenizer()
    words = []
    for t in _tokenizer.tokenize(query):
        pos = tuple(t.part_of_speech.split(",")[:2])
        if pos in CONTENT_POS:
            words.append(t.base_form if pos[0] in ("動詞", "形容詞") and t.base_form != "*" else t.surface)
    return [w for w in dict.fromkeys(words) if w not in STOP_WORDS]


@method("B")
def search_b(query: str, db_path: Path) -> list[str]:
    words = content_words(query)
    long_w = [w for w in words if len(w) >= db.FTS_MIN_TERM]
    short_w = [w for w in words if len(w) < db.FTS_MIN_TERM]
    ids: list[str] = []
    with closing(db.connect(db_path)) as conn:
        if long_w:
            weights = ", ".join(str(w) for w in db.BM25_WEIGHTS.values())
            rows = conn.execute(
                f"SELECT m.id FROM memories m JOIN memories_fts ON memories_fts.memory_id = m.id "
                f"WHERE memories_fts MATCH ? AND m.archived = 0 "
                f"ORDER BY bm25(memories_fts, {weights}), m.recorded_at DESC LIMIT ?",
                (" OR ".join(db._fts_term(w) for w in long_w), db.MAX_LIMIT)).fetchall()
            ids = [r[0] for r in rows]
        if short_w:
            hit = ("(m.title LIKE ? ESCAPE '\\' OR m.summary LIKE ? ESCAPE '\\' OR m.transcript LIKE ? ESCAPE '\\' "
                   "OR EXISTS (SELECT 1 FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id "
                   "WHERE mt.memory_id = m.id AND t.name LIKE ? ESCAPE '\\'))")
            params = [p for w in short_w for p in [f"%{db._like_escape(w)}%"] * 4]
            n_hit = " + ".join(f"CASE WHEN {hit} THEN 1 ELSE 0 END" for _ in short_w)
            rows = conn.execute(
                f"SELECT m.id FROM memories m WHERE m.archived = 0 AND ({n_hit}) > 0 "
                f"ORDER BY ({n_hit}) DESC, m.recorded_at DESC LIMIT ?",
                (*params, *params, db.MAX_LIMIT)).fetchall()
            seen = set(ids)
            ids += [r[0] for r in rows if r[0] not in seen]
    return ids[:db.MAX_LIMIT]


# ---- harness ----------------------------------------------------------------------------------

def evaluate(fixture: dict, db_path: Path, fn) -> dict:
    ids = es.build_db(fixture, db_path)
    key_of = {v: k for k, v in ids.items()}
    fn(fixture["queries"][0]["query"], db_path)  # warm-up (Janome dictionary load, SQLite cache)
    per_query, elapsed = [], 0.0
    for q in fixture["queries"]:
        t0 = time.perf_counter()
        ranked = [key_of[i] for i in fn(q["query"], db_path)]
        elapsed += time.perf_counter() - t0
        per_query.append({**q, "returned": len(ranked), "top10": ranked[:10], **es.score(ranked, set(q["relevant"]))})
    scored = [q for q in per_query if not es.is_no_match(q)]
    no_match = [q for q in per_query if es.is_no_match(q)]

    def summarize(c):
        rows = [q for q in per_query if q["category"] == c]
        return es.false_positive_rate(rows) if all(es.is_no_match(q) for q in rows) else es.mean(rows)

    return {
        "overall": es.mean(scored), "no_match": es.false_positive_rate(no_match),
        "by_category": {c: summarize(c) for c in sorted({q["category"] for q in per_query})},
        "avg_ms_per_query": elapsed / len(per_query) * 1000,
        "queries": per_query,
        "missed": [q["id"] for q in scored if q["rr"] == 0.0],
        "false_positives": [q["id"] for q in no_match if q["returned"] > 0],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", default="all", help=f"{'|'.join(METHODS)}|all (default all)")
    ap.add_argument("--json", help="also write the machine-readable result here")
    args = ap.parse_args()
    names = list(METHODS) if args.method == "all" else [args.method.upper()]
    for n in names:
        if n not in METHODS:
            ap.error(f"unknown method {n}; choose from {', '.join(METHODS)} or all")
    results = {}
    with tempfile.TemporaryDirectory() as d:
        for n in names:
            results[n] = evaluate(es.load_fixture(), Path(d) / f"eval_{n}.db", METHODS[n])
    for n, res in results.items():
        print(f"== method={n}  avg {res['avg_ms_per_query']:.2f} ms/query ==")
        print(es.report(res), end="\n\n")
    if args.json:
        out = results[names[0]] if len(names) == 1 else results
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
