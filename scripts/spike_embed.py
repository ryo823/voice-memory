"""Embedding-model spike (week 2, Run2 prep). Usage: spike_embed.py NAME MODEL.onnx TOKENIZER.json QPREFIX PPREFIX [--json OUT]

Runs one ONNX embedding model with onnxruntime + tokenizers only (no torch) over the synthetic fixture:
load time, RSS, time to embed 120 memories + 64 queries, and cosine similarity of paraphrase pairs
vs unrelated pairs. Models are downloaded by hand into a scratch dir; nothing here touches src/.
"""
import argparse, json, resource, sys, time
from pathlib import Path
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "search_eval.json"
# (query, memory key that should match, unrelated memory key)
PAIRS = [("転居", None), ("アレルギーの薬", None), ("パソコン購入", None), ("ご近所トラブル", None),
         ("食器を自動で洗う機械", None), ("ネット回線が遅い", None)]


class Embedder:
    def __init__(self, model: str, tok: str, max_len: int = 512):
        self.tok = Tokenizer.from_file(tok)
        self.tok.enable_truncation(max_len)
        self.tok.enable_padding()
        so = ort.SessionOptions()
        self.sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
        self.names = {i.name for i in self.sess.get_inputs()}
        self.outs = [o.name for o in self.sess.get_outputs()]

    def encode(self, texts: list[str], bs: int = 16) -> np.ndarray:
        res = []
        for i in range(0, len(texts), bs):
            enc = self.tok.encode_batch(texts[i:i + bs])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.names:
                feed["token_type_ids"] = np.zeros_like(ids)
            out = self.sess.run(self.outs, feed)
            if "sentence_embedding" in self.outs:
                v = out[self.outs.index("sentence_embedding")]
            else:  # mean pooling over the attention mask
                h = out[0]
                m = mask[..., None].astype(np.float32)
                v = (h * m).sum(1) / m.sum(1)
            res.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.vstack(res)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("model"); ap.add_argument("tokenizer")
    ap.add_argument("qprefix"); ap.add_argument("pprefix"); ap.add_argument("--json")
    a = ap.parse_args()
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    t0 = time.perf_counter()
    emb = Embedder(a.model, a.tokenizer)
    load_s = time.perf_counter() - t0
    emb.encode(["warmup"])
    docs = [f"{m['title']}\n{m['summary']}\n{m['transcript']}" for m in fx["memories"]]
    qs = [q["query"] for q in fx["queries"]]
    t0 = time.perf_counter(); D = emb.encode([a.pprefix + d for d in docs]); doc_s = time.perf_counter() - t0
    t0 = time.perf_counter(); Q = emb.encode([a.qprefix + q for q in qs]); q_s = time.perf_counter() - t0
    keys = [m["key"] for m in fx["memories"]]
    # retrieval sanity over the whole fixture: pure vector top-k (info only; Run2 will do the real eval)
    sims = Q @ D.T
    rel_rows = [(i, q) for i, q in enumerate(fx["queries"]) if q["relevant"]]
    def rec(k, cats=None):
        r = [len(set(q["relevant"]) & {keys[j] for j in np.argsort(-sims[i])[:k]}) / len(q["relevant"])
             for i, q in rel_rows if cats is None or q["category"] in cats]
        return float(np.mean(r))
    # paraphrase pairs: query vs its first relevant memory, and vs the mean of every other memory
    pairs = []
    qi = {q["query"]: i for i, q in enumerate(fx["queries"])}
    for qtext, _ in PAIRS:
        i = qi[qtext]; q = fx["queries"][i]
        rel = [keys.index(k) for k in q["relevant"]]
        unrel = [j for j in range(len(keys)) if j not in rel]
        worst_rel = float(min(sims[i][rel])); best_unrel = float(max(sims[i][unrel]))
        pairs.append({"query": qtext, "relevant_mean": float(np.mean(sims[i][rel])), "worst_relevant": worst_rel,
                      "unrelated_mean": float(np.mean(sims[i][unrel])), "best_unrelated": best_unrel})
    sep = float(np.mean([p["relevant_mean"] - p["unrelated_mean"] for p in pairs]))
    # no_match queries: top1 similarity (higher = harder to threshold)
    nm = [float(sims[i].max()) for i, q in enumerate(fx["queries"]) if not q["relevant"]]
    para = float(np.mean([sims[i][[keys.index(k) for k in q["relevant"]]].mean()
                          for i, q in rel_rows if q["category"] == "paraphrase"]))
    out = {"name": a.name, "model_mb": round(Path(a.model).stat().st_size / 1e6, 1), "load_s": round(load_s, 2),
           "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 0),
           "docs_s": round(doc_s, 2), "queries_s": round(q_s, 2), "ms_per_query": round(q_s / len(qs) * 1000, 1),
           "dim": int(D.shape[1]), "pairs": pairs, "mean_separation": round(sep, 3),
           "paraphrase_pure_vector": {"R@5": round(rec(5, {"paraphrase"}), 3), "R@10": round(rec(10, {"paraphrase"}), 3)},
           "all_pure_vector": {"R@5": round(rec(5), 3), "R@10": round(rec(10), 3)},
           "no_match_top1_sim": {"min": round(min(nm), 3), "max": round(max(nm), 3), "mean": round(float(np.mean(nm)), 3)},
           "paraphrase_relevant_sim_mean": round(para, 3)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
