"""Unfrozen M0 retrieval ablation: BM25, BGE-small, RRF, and CrossEncoder."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RERANKER_REV = "2cfc18c9415c912f9d8155881c133215df768a70"
SMALL_REV = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_DIR = ROOT / ".cache/huggingface/hub/models--BAAI--bge-reranker-base/snapshots" / RERANKER_REV
SMALL_DIR = ROOT / ".cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots" / SMALL_REV


def wilson(successes: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    z = 1.96
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def metrics(rankings: dict[str, list[str]], golds: dict[str, set[str]]) -> dict:
    hits = 0
    recall3 = []
    recalls = []
    reciprocal = []
    for key, ids in rankings.items():
        gold = golds[key]
        hits += bool(ids and ids[0] in gold)
        recall3.append(len(set(ids[:3]) & gold) / len(gold))
        recalls.append(len(set(ids[:5]) & gold) / len(gold))
        reciprocal.append(next((1 / i for i, item in enumerate(ids, 1) if item in gold), 0.0))
    return {
        "p_at_1": {"numerator": hits, "denominator": len(rankings), "value": hits / len(rankings), "wilson_95": wilson(hits, len(rankings))},
        "mean_recall_at_3": statistics.mean(recall3),
        "mean_recall_at_5": statistics.mean(recalls),
        "mrr": statistics.mean(reciprocal),
    }


def latency(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms_nearest_rank": round(ordered[__import__("math").ceil(0.95 * len(ordered)) - 1], 3),
    }


def load_models():
    import psutil
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    proc = psutil.Process()
    rss_before = proc.memory_info().rss
    started = time.perf_counter()
    encoder = SentenceTransformer(str(SMALL_DIR), device="cpu", local_files_only=True)
    small_load_ms = (time.perf_counter() - started) * 1000
    rss_small = proc.memory_info().rss
    started = time.perf_counter()
    ranker = CrossEncoder(str(MODEL_DIR), device="cpu", local_files_only=True)
    reranker_load_ms = (time.perf_counter() - started) * 1000
    rss_loaded = proc.memory_info().rss
    return encoder, ranker, {
        "device": "cpu",
        "torch_version": torch.__version__,
        "sentence_transformers_version": __import__("sentence_transformers").__version__,
        "small_revision": SMALL_REV,
        "reranker_revision": RERANKER_REV,
        "small_load_ms": round(small_load_ms, 3),
        "reranker_load_ms": round(reranker_load_ms, 3),
        "rss_before_mb": round(rss_before / 1048576, 2),
        "rss_after_small_mb": round(rss_small / 1048576, 2),
        "rss_after_reranker_mb": round(rss_loaded / 1048576, 2),
        "reranker_increment_mb": round((rss_loaded - rss_small) / 1048576, 2),
    }


def prepared_metrics(records, queries):
    """Planned-query denominator; unknown labels never become negatives."""
    by_id = {r['query_id']: r for r in records}
    precision1, precision5, recall3, recall5, reciprocal = [], [], [], [], []
    upper1, upper5, upper_mrr = [], [], []
    unjudged = 0
    failures = 0
    for q in queries:
        labels = {x['chunk_id']: x['grade'] for x in q['qrels']}
        positive = {k for k, v in labels.items() if v is not None and v > 0}
        row = by_id.get(q['query_id'])
        valid = row is not None and row['valid_mode']
        failures += not valid
        ranking = row['ranking'] if valid else []
        if len(ranking) != len(set(ranking)) or any(x not in labels for x in ranking):
            raise ValueError('duplicate or out-of-scope ranking')
        unjudged += sum(labels[x] is None for x in ranking)
        # Bounds retain unjudged slots; lack of any relevant label makes Recall N/A.
        hits = [i + 1 for i, cid in enumerate(ranking[:5]) if cid in positive]
        precision1.append(int(bool(hits and hits[0] == 1)))
        precision5.append(sum(cid in positive for cid in ranking[:5]) / 5)
        recall3.append(len(set(ranking[:3]) & positive) / len(positive) if positive else None)
        recall5.append(len(set(ranking[:5]) & positive) / len(positive) if positive else None)
        reciprocal.append(1 / hits[0] if hits else 0)
        possible = [i+1 for i,cid in enumerate(ranking[:5]) if cid in positive or labels[cid] is None]
        upper1.append(int(bool(possible and possible[0] == 1)))
        upper5.append(sum(cid in positive or labels[cid] is None for cid in ranking[:5])/5)
        upper_mrr.append(1/possible[0] if possible else 0)
    return {'planned_queries': len(queries), 'invalid_or_missing': failures,
            'unjudged_retrieved': unjudged,
            'p_at_1': {'n':sum(precision1),'N':len(queries),'value':statistics.mean(precision1),'wilson_95':wilson(sum(precision1),len(queries))},
            'mean_p_at_5': statistics.mean(precision5),
            'mean_labelled_set_recall_at_3': statistics.mean(x for x in recall3 if x is not None) if any(x is not None for x in recall3) else None,
            'mean_labelled_set_recall_at_5': statistics.mean(x for x in recall5 if x is not None) if any(x is not None for x in recall5) else None,
            'mrr_at_5':statistics.mean(reciprocal),
            'unjudged_bounds': {'p_at_1':[statistics.mean(precision1),statistics.mean(upper1)],
                'p_at_5':[statistics.mean(precision5),statistics.mean(upper5)],
                'mrr_at_5':[statistics.mean(reciprocal),statistics.mean(upper_mrr)]},
            'recall_query_N':sum(x is not None for x in recall5),
            'semantic_gold_metrics': 'N/A: AI development qrels, not independent human references'}


def run_prepared(bundle_path: Path, output: Path):
    """Execute existing production retrievers over explicit, immutable local views.

    Unlike the historical diagnostic mode, each baseline recalls independently.
    A degraded result is retained as a failed planned arm, never relabelled dense.
    """
    import socket
    import psutil
    import torch
    from qdrant_client import QdrantClient
    from app.m2.retrieval import RetrievalConfig, RetrievalService, RetrievalView, index_fingerprint
    bundle_bytes = bundle_path.read_bytes()
    bundle = json.loads(bundle_bytes)
    queries = bundle['queries']
    if len({q['query_id'] for q in queries}) != len(queries):
        raise ValueError('duplicate query')
    for q in queries:
        if index_fingerprint(q['index']) != q['fingerprint']:
            raise ValueError('corpus fingerprint mismatch')
        if {d['chunk_id'] for d in q['index']['chunks']} != {x['chunk_id'] for x in q['qrels']}:
            raise ValueError('qrels do not cover exact corpus')
    attempted = []
    def deny(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError('local retrieval experiment forbids network')
    socket.socket.connect = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HOME=str(ROOT / '.cache/huggingface'), OTEL_SDK_DISABLED='true',
                      LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')
    torch.set_num_threads(4)
    proc = psutil.Process()
    cpu_before = proc.cpu_times()
    total_started = time.perf_counter()
    encoder, ranker, load = load_models()
    client = QdrantClient(location=':memory:')
    config = RetrievalConfig(version='m63-four-arm-local-v1', timeout_seconds=120,
                             sparse_top_k=5, dense_top_k=5, output_top_k=5,
                             rerank_candidate_limit=10, min_vector_score=0.20)
    service = RetrievalService(config=config, encoder=encoder, ranker=ranker,
                               qdrant_client=client, collection_namespace='m63audit')
    records = []
    modes = ('bm25', 'vector', 'rrf', 'rrf_reranker')
    for pass_name in ('cold_index', 'warm'):
        for q in queries:
            view = RetrievalView(q['index'], q['fingerprint'], q['evaluation_at'])
            for mode in modes:
                started = time.perf_counter()
                try:
                    result = service.retrieve(q['query'], mode=mode, view=view)
                    valid = result['mode'] == mode and not result['degraded']
                    if mode != 'bm25': valid = valid and bool(result['models']['embedding'])
                    if mode == 'rrf_reranker': valid = valid and bool(result['models']['reranker'])
                    record = {'query_id':q['query_id'], 'pass':pass_name, 'mode':mode,
                        'valid_mode':valid,'ranking':[x['chunk_id'] for x in result['evidence']], 'result':result}
                except Exception as exc:
                    record = {'query_id':q['query_id'],'pass':pass_name,'mode':mode,
                        'valid_mode':False,'ranking':[],'error':type(exc).__name__+': '+str(exc)}
                record['wall_ms'] = (time.perf_counter()-started)*1000
                records.append(record)
            print(json.dumps({'pass':pass_name,'query_id':q['query_id'],'completed':len(records)}),flush=True)
    cpu_after = proc.cpu_times()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {m:{**prepared_metrics([r for r in records if r['pass']=='warm' and r['mode']==m],queries),
                  **latency([r['wall_ms'] for r in records if r['pass']=='warm' and r['mode']==m])} for m in modes}
    payload = {'schema_version':1,'bundle_sha256':hashlib.sha256(bundle_bytes).hexdigest(),
        'status':'COMPLETED_LOCAL_MODELS', 'models':load,'config':config.__dict__,
        'scope':bundle['corpus_scope'],'external_provider_calls':0,'api_embedding_calls':0,
        'network_attempts':len(attempted),'local_inference':True,'cpu_threads':4,
        'elapsed_seconds':time.perf_counter()-total_started,
        'cpu_seconds':cpu_after.user+cpu_after.system-cpu_before.user-cpu_before.system,
        'rss_end_mb':proc.memory_info().rss/1048576,'summary':summary,'records':records,
        'timing_policy':'cold_index first pass includes per-scenario vector build in vector arm; warm pass reuses vectors, BM25 cache bounded32 across67 scopes; not pure all-in-RAM sparse-cache timing',
        'cost':'local CPU/RAM only; electricity/unamortized hardware USD UNKNOWN, no external API billing'}
    with output.open('x',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False,indent=2)
    client.close()
    print(json.dumps({'output':str(output),'summary':summary,'network_attempts':len(attempted)},ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload-only", action="store_true")
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval/dataset/policy-initial.json")
    parser.add_argument("--mode", choices=("approved", "diagnostic"), default="diagnostic")
    parser.add_argument('--prepared-bundle', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.prepared_bundle:
        if not args.output: parser.error('--output required with --prepared-bundle')
        run_prepared(args.prepared_bundle, args.output)
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(ROOT / ".cache/huggingface")
    if not MODEL_DIR.joinpath("model.safetensors").is_file():
        raise FileNotFoundError(MODEL_DIR / "model.safetensors")
    if not args.reload_only:
        dataset_path = args.dataset.resolve()
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        all_cases = dataset["cases"]
        if args.mode == "approved":
            selected_cases = [c for c in all_cases if c.get("review_status") == "APPROVED" and c.get("scorable", True) is True and c.get("dataset_split") in ("dev", "validation")]
        else:
            selected_cases = [c for c in all_cases if c.get("scorable", True) is True and c.get("scoreability", "SCORABLE_CANDIDATE") not in ("UNSCORABLE", "UNSCORABLE_CANDIDATE") and c.get("dataset_split", "dev") in ("dev", "validation")]
        if not selected_cases:
            if args.mode != "approved":
                raise SystemExit("No eligible diagnostic cases in dev/validation.")
            result = {
                "record_type": "policy_retrieval_ablation", "run_id": str(uuid.uuid4()),
                "timestamp_utc": datetime.now(timezone.utc).isoformat(), "status": "NOT_RUN_NO_APPROVED_CASES",
                "run_mode": "APPROVED_PRELIMINARY", "dataset_version": dataset["dataset_version"],
                "dataset_path": str(dataset_path.relative_to(ROOT)) if dataset_path.is_relative_to(ROOT) else str(dataset_path),
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                "dataset_n_total": len(all_cases), "approved_scoreable_dev_validation_n": 0,
                "reason": "No human-approved, scoreable cases are available in dev/validation. Models were not loaded and no retrieval metrics were computed.",
                "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
            }
            out_dir = ROOT / "docs/probes"
            out = out_dir / f"retrieval-ablation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            with out.open("x", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(json.dumps({"path": str(out), **result}, ensure_ascii=False, indent=2))
            return
    encoder, ranker, load = load_models()
    fixed_pair = ("订单已发货还能取消吗", "订单发货后不支持取消，请联系承运方确认物流。")
    fixed_score = float(ranker.predict([fixed_pair])[0])
    if args.reload_only:
        print(json.dumps({"status": "PASS", "offline": True, "score": fixed_score, "reranker_revision": RERANKER_REV, "load": load}, ensure_ascii=False))
        return

    from app.m0.bm25 import BM25
    from app.m0.data import policy_at

    index = json.loads((ROOT / "data/index.json").read_text(encoding="utf-8"))
    chunks = index["chunks"]
    chunk_by_id = {item["chunk_id"]: item for item in chunks}
    corpus = [c["title"] + " " + c["content"] for c in chunks]
    t0 = time.perf_counter()
    vectors = encoder.encode(corpus, normalize_embeddings=True, convert_to_numpy=True)
    corpus_encode_ms = round((time.perf_counter() - t0) * 1000, 3)
    methods = {name: {} for name in ("bm25", "bge_small", "rrf", "rrf_reranker")}
    times = {name: [] for name in methods}
    cases = []
    category_by_case = {}

    for case in selected_cases:
        q = case["user_query"]
        eligible = policy_at(chunks, case["evaluation_at"])
        eligible_ids = {c["chunk_id"] for c in eligible}
        bm25 = BM25(eligible)
        t0 = time.perf_counter()
        bm25_all = bm25.search(q, min(100, len(eligible)))
        bm25_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        qv = encoder.encode([q], normalize_embeddings=True, convert_to_numpy=True)[0]
        dense_scored = sorted(
            ((float(qv @ vectors[i]), c) for i, c in enumerate(chunks) if c["chunk_id"] in eligible_ids),
            key=lambda x: (-x[0], x[1]["chunk_id"]),
        )
        dense_ms = (time.perf_counter() - t0) * 1000
        bm_ids = [c["chunk_id"] for c in bm25_all[:5]]
        dense_ids = [c["chunk_id"] for _, c in dense_scored[:5]]
        pool_ids = list(dict.fromkeys(bm_ids + dense_ids))
        bm_rank = sorted(pool_ids, key=lambda cid: (-next((float(x["score"]) for x in bm25_all if x["chunk_id"] == cid), 0.0), cid))
        dense_scores = {c["chunk_id"]: score for score, c in dense_scored}
        dense_rank = sorted(pool_ids, key=lambda cid: (-dense_scores.get(cid, 0.0), cid))
        bm_source_rank = {cid: i + 1 for i, cid in enumerate(bm_ids)}
        dense_source_rank = {cid: i + 1 for i, cid in enumerate(dense_ids)}
        rrf_score = {
            cid: (1 / (60 + bm_source_rank[cid]) if cid in bm_source_rank else 0)
            + (1 / (60 + dense_source_rank[cid]) if cid in dense_source_rank else 0)
            for cid in pool_ids
        }
        rrf_rank = sorted(pool_ids, key=lambda cid: (-rrf_score[cid], cid))
        t0 = time.perf_counter()
        pairs = [(q, chunk_by_id[cid]["title"] + " " + chunk_by_id[cid]["content"]) for cid in pool_ids]
        ce_scores = ranker.predict(pairs, batch_size=8)
        rerank_ms = (time.perf_counter() - t0) * 1000
        ce_rank = [cid for _, cid in sorted(zip((float(s) for s in ce_scores), pool_ids), key=lambda pair: (-pair[0], pair[1]))]
        expected = set(case["acceptable_evidence_ids"])
        golds = expected
        for method, ranked in (("bm25", bm_rank), ("bge_small", dense_rank), ("rrf", rrf_rank), ("rrf_reranker", ce_rank)):
            methods[method][case["case_id"]] = ranked[:5]
        category_by_case[case["case_id"]] = case.get("business_scenario", case.get("group_id", "unspecified"))
        times["bm25"].append(bm25_ms)
        times["bge_small"].append(dense_ms)
        times["rrf"].append(bm25_ms + dense_ms)
        times["rrf_reranker"].append(bm25_ms + dense_ms + rerank_ms)
        cases.append({
            "case_id": case["case_id"], "query": q, "gold": sorted(expected), "candidate_pool_ids": pool_ids,
            "dataset_split": case.get("dataset_split", "legacy_unreviewed"), "template_group": case.get("template_group", case.get("group_id")),
            "review_status": case.get("review_status", "UNREVIEWED_LEGACY"),
            "bm25_top5": bm_ids, "bge_small_top5": dense_ids,
            "rankings": {name: ids[case["case_id"]] for name, ids in methods.items()},
            "error_analysis": {
                name: (None if ranked and ranked[0] in expected else "FIRST_STAGE_CANDIDATE_MISS" if not set(pool_ids) & expected else "RANKING_ORDER_MISS")
                for name, ranked in (("bm25", bm_rank), ("bge_small", dense_rank), ("rrf", rrf_rank), ("rrf_reranker", ce_rank))
            },
            "stage_latency_ms": {"bm25": round(bm25_ms, 3), "bge_small_query": round(dense_ms, 3), "cross_encoder": round(rerank_ms, 3)},
        })
    golds_by_id = {case["case_id"]: set(case["acceptable_evidence_ids"]) for case in selected_cases}
    results = {
        name: {**metrics(rankings, golds_by_id), "latency": latency(times[name]), "latency_scope": "per-query strategy execution; excludes one-time model load and corpus vectorization"}
        for name, rankings in methods.items()
    }
    hit_by_method = {name: {cid: bool(ids and ids[0] in golds_by_id[cid]) for cid, ids in ranking.items()} for name, ranking in methods.items()}
    paired_vs_bm25 = {}
    for name in methods:
        if name == "bm25":
            continue
        improved = [cid for cid in hit_by_method[name] if hit_by_method[name][cid] and not hit_by_method["bm25"][cid]]
        regressed = [cid for cid in hit_by_method[name] if hit_by_method["bm25"][cid] and not hit_by_method[name][cid]]
        paired_vs_bm25[name] = {"improved_case_ids": improved, "regressed_case_ids": regressed, "unchanged_case_ids": [cid for cid in hit_by_method[name] if cid not in improved and cid not in regressed]}
    category_results = {}
    for category in sorted(set(category_by_case.values())):
        cids = [cid for cid, value in category_by_case.items() if value == category]
        category_results[category] = {name: metrics({cid: methods[name][cid] for cid in cids}, {cid: golds_by_id[cid] for cid in cids}) for name in methods}
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = env["TRANSFORMERS_OFFLINE"] = "1"
    reload = subprocess.run([sys.executable, str(Path(__file__)), "--reload-only"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=300)
    reload_record = {"returncode": reload.returncode, "stdout": reload.stdout.strip(), "stderr": reload.stderr[-4000:]}
    if reload.returncode == 0:
        reload_record["result"] = json.loads(reload.stdout)
        reload_record["status"] = "PASS"
    else:
        reload_record["status"] = "FAIL"
    weight = MODEL_DIR / "model.safetensors"
    h = hashlib.sha256()
    with weight.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    result = {
        "record_type": "policy_retrieval_ablation",
        "run_id": str(uuid.uuid4()),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": ("PASS_DIAGNOSTIC_ONLY" if args.mode == "diagnostic" else "PASS") if reload.returncode == 0 else "FAIL",
        "run_mode": "APPROVED_PRELIMINARY" if args.mode == "approved" else "UNREVIEWED_DIAGNOSTIC_ONLY",
        "diagnostic_not_formal_score": args.mode != "approved",
        "dataset_path": str(dataset_path.relative_to(ROOT)) if dataset_path.is_relative_to(ROOT) else str(dataset_path),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "dataset_version": dataset["dataset_version"], "dataset_n_total": len(all_cases), "dataset_n_scored": len(selected_cases),
        "dataset_excluded_n": len(all_cases) - len(selected_cases),
        "excluded_case_ids": [c["case_id"] for c in all_cases if c not in selected_cases],
        "dataset_note": dataset.get("note"), "knowledge_version": index["knowledge_version"],
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "methods": ["jieba BM25", "BGE-small dense", "BM25+BGE-small RRF (k=60)", "RRF candidate pool + BGE reranker"],
        "candidate_pool": "union of each query's BM25 top-5 and BGE-small top-5; same pool ranked by all four arms; reranker selects top-5",
        "rrf_note": "RRF source ranks are taken only from each source retriever's top-5; items absent from a source contribute zero.",
        "retrieval_config": {"bm25_tokenizer": "jieba default dictionary, no stopwords", "dense_encoder": "BAAI/bge-small-zh-v1.5", "dense_revision": SMALL_REV, "reranker": "BAAI/bge-reranker-base", "reranker_revision": RERANKER_REV, "first_stage_top_k_each": 5, "output_top_k": 5, "rrf_constant": 60, "device": "cpu", "query_rewrite": False},
        "model": {**load, "reranker_model": "BAAI/bge-reranker-base", "reranker_revision": RERANKER_REV, "weight_bytes": weight.stat().st_size, "weight_sha256": h.hexdigest(), "small_model": "BAAI/bge-small-zh-v1.5", "small_revision": SMALL_REV, "offline_reload": reload_record},
        "corpus_encode_ms_once": corpus_encode_ms, "metrics": results, "category_metrics": category_results, "paired_vs_bm25": paired_vs_bm25, "cases": cases,
        "limitations": (["All scored rows must have human APPROVED status."] if args.mode == "approved" else ["Unreviewed candidate diagnostics only; not an official score and not evidence of approved gold labels."]),
    }
    out = ROOT / "docs/probes" / f"retrieval-ablation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with out.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(out), "metrics": results, "model": result["model"], "status": result["status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
