"""Real BGE load/encode/rerank probe; records missing dependencies as blocked."""
from __future__ import annotations

import json
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".cache/huggingface"))
CONFIGS = [
    ("small", "BAAI/bge-small-zh-v1.5", "BAAI/bge-reranker-base"),
    ("m3", "BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=("small", "m3", "all"), default="all")
    args = parser.parse_args()
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0], "models": {}}
    selected = [config for config in CONFIGS if args.only in ("all", config[0])]
    try:
        import torch
        from sentence_transformers import CrossEncoder, SentenceTransformer
    except Exception as exc:
        for label, embedding, reranker in selected:
            result["models"][label] = {"status": "blocked", "embedding": embedding, "reranker": reranker, "reason": "runtime dependency unavailable", "error_type": type(exc).__name__}
    else:
        result["device"] = "cuda" if torch.cuda.is_available() else "cpu"
        import psutil
        import numpy as np
        from app.m0.data import policy_at

        index = json.loads((ROOT / "data/index.json").read_text(encoding="utf-8"))
        dataset = json.loads((ROOT / "eval/dataset/policy-initial.json").read_text(encoding="utf-8"))
        process = psutil.Process()
        for label, embedding, reranker in selected:
            entry = {"embedding": embedding, "reranker": reranker}
            stage = "embedding_load"
            try:
                rss_before = process.memory_info().rss
                started = time.perf_counter()
                encoder = SentenceTransformer(embedding, device=result["device"])
                entry["load_embedding_ms"] = round((time.perf_counter() - started) * 1000)
                model_ref = ROOT / ".cache/huggingface/hub" / ("models--" + embedding.replace("/", "--")) / "refs/main"
                entry["embedding_revision"] = model_ref.read_text(encoding="utf-8").strip() if model_ref.exists() else None
                stage = "embedding_eval"
                started = time.perf_counter()
                chunks = index["chunks"]
                vectors = encoder.encode([c["title"] + " " + c["content"] for c in chunks], normalize_embeddings=True)
                entry["encode_corpus_ms"] = round((time.perf_counter() - started) * 1000)
                entry["dimension"] = int(vectors.shape[1])
                entry["rss_after_embedding_mb"] = round(process.memory_info().rss / 1048576)
                entry["embedding_increment_mb"] = round((process.memory_info().rss - rss_before) / 1048576)
                query_ms = []
                dense_hits = 0
                dense_recall = 0
                prepared = []
                case_records = []
                for case in dataset["cases"]:
                    started = time.perf_counter()
                    query_vector = encoder.encode([case["user_query"]], normalize_embeddings=True)[0]
                    query_ms.append(round((time.perf_counter() - started) * 1000))
                    eligible = {c["chunk_id"] for c in policy_at(chunks, case["evaluation_at"])}
                    candidates = sorted((c for c in chunks if c["chunk_id"] in eligible), key=lambda c: -float(np.dot(query_vector, vectors[chunks.index(c)])))[:5]
                    gold = set(case["acceptable_evidence_ids"])
                    dense_hits += bool(candidates and candidates[0]["chunk_id"] in gold)
                    dense_recall += bool(any(c["chunk_id"] in gold for c in candidates))
                    prepared.append((case, candidates, gold))
                    case_records.append({"case_id": case["case_id"], "gold": sorted(gold), "dense_ids": [c["chunk_id"] for c in candidates], "query_ms": query_ms[-1]})
                entry["dataset_version"] = dataset["dataset_version"]
                entry["cases"] = case_records
                entry["n"] = len(dataset["cases"])
                entry["dense_p1"] = dense_hits
                entry["dense_recall5"] = dense_recall
                entry["query_p95_ms_nearest_rank"] = sorted(query_ms)[int(np.ceil(0.95 * len(query_ms))) - 1]
                stage = "reranker_load"
                started = time.perf_counter()
                ranker = CrossEncoder(reranker, device=result["device"])
                entry["load_reranker_ms"] = round((time.perf_counter() - started) * 1000)
                rerank_ref = ROOT / ".cache/huggingface/hub" / ("models--" + reranker.replace("/", "--")) / "refs/main"
                entry["reranker_revision"] = rerank_ref.read_text(encoding="utf-8").strip() if rerank_ref.exists() else None
                stage = "reranker_eval"
                entry["rss_after_reranker_mb"] = round(process.memory_info().rss / 1048576)
                rerank_ms = []
                rerank_hits = 0
                rerank_recall = 0
                for case, candidates, gold in prepared:
                    started = time.perf_counter()
                    scores = ranker.predict([(case["user_query"], c["content"]) for c in candidates])
                    rerank_ms.append(round((time.perf_counter() - started) * 1000))
                    ranked = [c for _, c in sorted(zip(scores, candidates), key=lambda pair: -float(pair[0]))]
                    next(record for record in case_records if record["case_id"] == case["case_id"])["rerank_ids"] = [c["chunk_id"] for c in ranked]
                    rerank_hits += bool(ranked and ranked[0]["chunk_id"] in gold)
                    rerank_recall += bool(any(c["chunk_id"] in gold for c in ranked))
                entry["rerank_p1"] = rerank_hits
                entry["rerank_recall5"] = rerank_recall
                entry["query_p95_ms_nearest_rank"] = sorted(query_ms)[int(np.ceil(0.95 * len(query_ms))) - 1]
                entry["rerank_p95_ms_nearest_rank"] = sorted(rerank_ms)[int(np.ceil(0.95 * len(rerank_ms))) - 1]
                entry["status"] = "ok"
            except Exception as exc:
                entry["status"] = "failed"
                entry["error_type"] = type(exc).__name__
                entry["failure_stage"] = stage
            result["models"][label] = entry
    out = ROOT / "docs/probes" / f"embedding-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(out), "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
