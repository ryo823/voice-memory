"""Compare search methods on the eval fixture. Usage: eval_methods.py [--method A|B|B2|C|D|all (comma list ok)] [--json OUT.json]

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
B2 B with 形容動詞語幹 (e.g. 健康, 静か) also treated as a content word. General fix, no per-query tuning.
C  vector only: ruri-v3-30m ONNX int8 (model dir: $VOICE_MEMORY_EVAL_MODEL_DIR, default /tmp/spike_models/ruri;
   see docs/eval/week2-spike.md), query prefix "検索クエリ: ", document prefix "検索文書: ", document text =
   title + summary + transcript. Document embeddings are cached under .eval_cache/ keyed by a hash of the
   fixture texts. Cosine similarity ranks all memories (no cut-off, so a no_match query always returns hits).
D  hybrid: reciprocal rank fusion (k=60) of plain B and C.
(E, rerank, is deliberately not implemented; see docs/eval/week2-comparison-2026-11-28.md.)
"""
import argparse, hashlib, importlib.util, json, os, statistics, sys, tempfile, time
from contextlib import closing

import numpy as np
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
CONTENT_POS_B2 = CONTENT_POS | {("名詞", "形容動詞語幹")}
# IPADIC tags these as independent verbs but they carry no topic and would LIKE-match nearly every memory.
STOP_WORDS = {"する", "いる", "ある", "なる"}


def content_words(query: str, content_pos=CONTENT_POS) -> list[str]:
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
        if pos in content_pos:
            words.append(t.base_form if pos[0] in ("動詞", "形容詞") and t.base_form != "*" else t.surface)
    return [w for w in dict.fromkeys(words) if w not in STOP_WORDS]


def _search_words(words: list[str], db_path: Path) -> list[str]:
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


@method("B")
def search_b(query: str, db_path: Path) -> list[str]:
    return _search_words(content_words(query), db_path)


@method("B2")
def search_b2(query: str, db_path: Path) -> list[str]:
    return _search_words(content_words(query, CONTENT_POS_B2), db_path)


# ---- method C (vector) ------------------------------------------------------------------------

QPREFIX, PPREFIX = "検索クエリ: ", "検索文書: "
MODEL_DIR = Path(os.environ.get("VOICE_MEMORY_EVAL_MODEL_DIR", "/tmp/spike_models/ruri"))
CACHE_DIR = ROOT / ".eval_cache"
_embedder = None
_doc_cache: dict[str, np.ndarray] = {}


def model_available() -> bool:
    return (MODEL_DIR / "onnx" / "model_int8.onnx").exists() and (MODEL_DIR / "tokenizer.json").exists()


def _get_embedder():
    global _embedder
    if _embedder is None:
        if not model_available():
            raise SystemExit(f"method C needs the ruri-v3-30m ONNX files in {MODEL_DIR} (see docs/eval/week2-spike.md)")
        spec = importlib.util.spec_from_file_location("spike_embed", ROOT / "scripts" / "spike_embed.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _embedder = mod.Embedder(str(MODEL_DIR / "onnx" / "model_int8.onnx"), str(MODEL_DIR / "tokenizer.json"))
    return _embedder


def _doc_matrix(db_path: Path) -> tuple[list[str], np.ndarray]:
    """(memory ids, normalized document vectors); cached in memory and on disk by hash of the document texts."""
    with closing(db.connect(db_path)) as conn:
        rows = conn.execute("SELECT id, title, summary, transcript FROM memories WHERE archived = 0 ORDER BY id").fetchall()
    # memory ids are random per DB build: order by text so the cache key and the embedding batches are reproducible
    docs = sorted((PPREFIX + f"{r[1]}\n{r[2]}\n{r[3]}", r[0]) for r in rows)
    texts, ids = [d[0] for d in docs], [d[1] for d in docs]
    key = hashlib.sha256(("ruri-v3-30m-int8\0" + "\0".join(texts)).encode()).hexdigest()[:16]
    if key not in _doc_cache:
        f = CACHE_DIR / f"docs_{key}.npy"
        if f.exists():
            vecs = np.load(f)
        else:
            vecs = _get_embedder().encode(texts)
            CACHE_DIR.mkdir(exist_ok=True)
            np.save(f, vecs)
        _doc_cache[key] = vecs
    return ids, _doc_cache[key]


def vector_scores(query: str, db_path: Path) -> list[tuple[str, float]]:
    """All memories as (id, cosine similarity), best first."""
    ids, D = _doc_matrix(db_path)
    sims = D @ _get_embedder().encode([QPREFIX + query])[0]
    return [(ids[i], float(sims[i])) for i in np.argsort(-sims, kind="stable")]


@method("C")
def search_c(query: str, db_path: Path) -> list[str]:
    return [i for i, _ in vector_scores(query, db_path)]


# ---- method D (hybrid RRF) --------------------------------------------------------------------

RRF_K = 60


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    score: dict[str, float] = {}
    for r in rankings:
        for pos, i in enumerate(r, 1):
            score[i] = score.get(i, 0.0) + 1.0 / (k + pos)
    return sorted(score, key=lambda i: -score[i])


@method("D")
def search_d(query: str, db_path: Path) -> list[str]:
    return rrf(search_b(query, db_path), search_c(query, db_path))


# ---- no_match analysis (exploratory) ----------------------------------------------------------

THRESHOLDS = (0.80, 0.85)


def _dist(xs: list[float]) -> dict:
    return {"n": len(xs), "min": min(xs), "median": statistics.median(xs), "max": max(xs)}


def analyze_c(fixture: dict, work: Path) -> dict:
    """Top1-similarity distributions and fixed-threshold cut-offs for C. Exploratory: thresholds are not tuned."""
    ids = es.build_db(fixture, work / "analysis.db")
    key_of = {v: k for k, v in ids.items()}
    top1_rel, top1_nm, per_q = [], [], []
    for q in fixture["queries"]:
        scored = [(key_of[i], s) for i, s in vector_scores(q["query"], work / "analysis.db")]
        per_q.append((q, scored))
        (top1_nm if es.is_no_match(q) else top1_rel).append(scored[0][1])
    out = {"top1_sim_relevant_queries": _dist(top1_rel), "top1_sim_no_match_queries": _dist(top1_nm), "thresholds": {}}
    for t in THRESHOLDS:
        rows = []
        for q, scored in per_q:
            ranked = [k for k, s in scored if s >= t]
            rows.append({**q, "returned": len(ranked), **es.score(ranked, set(q["relevant"]))})
        scored_rows = [r for r in rows if not es.is_no_match(r)]
        nm_rows = [r for r in rows if es.is_no_match(r)]
        out["thresholds"][f"{t:.2f}"] = {"overall": es.mean(scored_rows), "no_match": es.false_positive_rate(nm_rows),
                                         "zero_result_scored_queries": sum(r["returned"] == 0 for r in scored_rows),
                                         "by_category": {c: es.mean([r for r in scored_rows if r["category"] == c])
                                                         for c in sorted({r["category"] for r in scored_rows})}}
    return out


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
    names = list(METHODS) if args.method == "all" else args.method.upper().split(",")
    for n in names:
        if n not in METHODS:
            ap.error(f"unknown method {n}; choose from {', '.join(METHODS)} or all")
    results = {}
    with tempfile.TemporaryDirectory() as d:
        for n in names:
            results[n] = evaluate(es.load_fixture(), Path(d) / f"eval_{n}.db", METHODS[n])
    analysis = None
    if "C" in results:
        with tempfile.TemporaryDirectory() as d:
            analysis = analyze_c(es.load_fixture(), Path(d))
        print("== C no_match analysis (exploratory) ==\n" + json.dumps(analysis, ensure_ascii=False, indent=1) + "\n")
    for n, res in results.items():
        print(f"== method={n}  avg {res['avg_ms_per_query']:.2f} ms/query ==")
        print(es.report(res), end="\n\n")
    if args.json:
        out = results[names[0]] if len(names) == 1 else {**results, **({"C_no_match_analysis": analysis} if analysis else {})}
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
