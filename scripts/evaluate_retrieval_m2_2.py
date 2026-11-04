"""Reproducible four-mode M2.2 comparison on APPROVED dev/validation only."""
from __future__ import annotations
import argparse, hashlib, json, os, statistics, subprocess, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATASET=ROOT/"eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
SMALL_REV="7999e1d3359715c523056ef9478215996d62a620"
RERANKER_REV="2cfc18c9415c912f9d8155881c133215df768a70"
SMALL=ROOT/".cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots"/SMALL_REV
RERANKER=ROOT/".cache/huggingface/hub/models--BAAI--bge-reranker-base/snapshots"/RERANKER_REV
MODES=("bm25","vector","rrf","rrf_reranker")

def pct(values,p):
    if not values:return None
    values=sorted(values); import math
    return round(values[max(0,math.ceil(p*len(values))-1)],3)

def score_rows(rows):
    n=len(rows); hits=sum(bool(r["rankings"].get("top") and r["rankings"]["top"][0] in r["gold"]) for r in rows)
    recalls={k:[] for k in (3,5)}; rr=[]; coverage=[]
    for r in rows:
        ids=r["rankings"].get("top",[]); gold=set(r["gold"])
        for k in recalls: recalls[k].append(len(set(ids[:k])&gold)/max(1,len(gold)))
        rr.append(next((1/i for i,x in enumerate(ids,1) if x in gold),0.0))
        req=r.get("required_conditions",[])
        coverage.append(sum(bool(set(ids[:5])&set(condition["evidence_ids"])) for condition in req)/len(req) if req else None)
    vals=[x for x in coverage if x is not None]
    return {"p_at_1":{"numerator":hits,"denominator":n,"value":hits/n if n else None},
      "recall_at_3":statistics.mean(recalls[3]) if n else None,"recall_at_5":statistics.mean(recalls[5]) if n else None,
      "mrr":statistics.mean(rr) if n else None,"required_condition_evidence_coverage":statistics.mean(vals) if vals else None}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--dataset",type=Path,default=DATASET); args=parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_HOME=str(ROOT/".cache/huggingface"))
    dataset_path=args.dataset.resolve(); raw=dataset_path.read_bytes(); dataset=json.loads(raw)
    cases=[c for c in dataset["cases"] if c.get("review_status")=="APPROVED" and c.get("dataset_split") in ("dev","validation")
           and c.get("scorable",True) is True and c.get("scoreability","SCORABLE_CANDIDATE") not in ("UNSCORABLE","UNSCORABLE_CANDIDATE")]
    if not cases: raise SystemExit("No approved scoreable dev/validation samples.")
    dev_validation=[c for c in dataset["cases"] if c.get("dataset_split") in ("dev","validation")]
    excluded_dev_validation=[c for c in dev_validation if c not in cases]
    import psutil, torch
    from sentence_transformers import SentenceTransformer,CrossEncoder
    from qdrant_client import QdrantClient
    from app.m2.retrieval import RetrievalConfig,RetrievalService
    process=psutil.Process(); rss0=process.memory_info().rss; load_t=time.perf_counter()
    encoder=SentenceTransformer(str(SMALL),device="cpu",local_files_only=True)
    emb_load=(time.perf_counter()-load_t)*1000; rss1=process.memory_info().rss
    load_t=time.perf_counter(); ranker=CrossEncoder(str(RERANKER),device="cpu",local_files_only=True)
    rank_load=(time.perf_counter()-load_t)*1000; rss2=process.memory_info().rss
    client=QdrantClient(path=str(ROOT/"data/qdrant_retrieval_m2"),timeout=8)
    services={m:RetrievalService(config=RetrievalConfig(mode=m),encoder=encoder,ranker=ranker,qdrant_client=client) for m in MODES}
    all_rows={m:[] for m in MODES}; lat={m:[] for m in MODES}; first={m:None for m in MODES}; degradations={m:0 for m in MODES}; case_output=[]
    for case in cases:
        judgments={j.get("condition"):j for j in case.get("evidence_judgments",[]) if j.get("relation")=="supports"}
        req=[]
        for condition in case.get("required_conditions",[]):
            if condition.get("required",True):
                judgment=judgments.get(condition.get("text"),{})
                req.append({"condition_id":condition.get("condition_id"),"text":condition.get("text"),"evidence_ids":judgment.get("evidence_ids",[])})
        row={"case_id":case["case_id"],"group_id":case.get("template_group"),"split":case["dataset_split"],
             "query":case["user_query"],"gold":case["acceptable_evidence_ids"],"required_conditions":req}
        per={}
        for mode in MODES:
            start=time.perf_counter(); result=services[mode].retrieve(case["user_query"],evaluation_at=case["evaluation_at"]); ms=(time.perf_counter()-start)*1000
            lat[mode].append(ms); first[mode]=first[mode] or round(ms,3); degradations[mode]+=int(result["degraded"])
            ev=result["evidence"]; ids=[x["chunk_id"] for x in ev]
            item={"top":ids,"scores":[{"chunk_id":x["chunk_id"],"rank":x["rank"],"score":x.get("score"),"score_type":x.get("score_type"),"channel_ranks":x.get("channel_ranks"),"pre_rerank_rank":x.get("pre_rerank_rank")} for x in ev],
                  "latency_ms":round(ms,3),"index_fingerprint":result["index_fingerprint"],"degraded":result["degraded"],"failures":result["failures"],"diagnostics":result["diagnostics"]}
            per[mode]=item; all_rows[mode].append({**row,"rankings":item})
        case_output.append({**row,"methods":per,"paired_top1_vs_bm25":{m:(bool(per[m]["top"] and per[m]["top"][0] in row["gold"]) - bool(per["bm25"]["top"] and per["bm25"]["top"][0] in row["gold"])) for m in MODES if m!="bm25"}})
    metrics={m:{**score_rows(all_rows[m]),"latency_ms":{"p50":pct(lat[m],.5),"p95_nearest_rank":pct(lat[m],.95),"first_query_including_index_build":first[m]},"degraded_queries":degradations[m]} for m in MODES}
    rankmaps={m:{row["case_id"]:row["rankings"]["top"] for row in all_rows[m]} for m in MODES}
    goldmap={row["case_id"]:set(row["gold"]) for row in all_rows["bm25"]}
    paired={}
    bm_hits={cid:bool(ids and ids[0] in goldmap[cid]) for cid,ids in rankmaps["bm25"].items()}
    for method in ("vector","rrf","rrf_reranker"):
        hits={cid:bool(ids and ids[0] in goldmap[cid]) for cid,ids in rankmaps[method].items()}
        paired[method]={"top1_improved_case_ids":[cid for cid in hits if hits[cid] and not bm_hits[cid]],
          "top1_regressed_case_ids":[cid for cid in hits if bm_hits[cid] and not hits[cid]],
          "top1_unchanged_n":sum(hits[cid]==bm_hits[cid] for cid in hits)}
    paired["reranker_vs_rrf"]={"full_top5_order_changed_case_ids":[cid for cid in rankmaps["rrf"] if rankmaps["rrf"][cid]!=rankmaps["rrf_reranker"][cid]],
      "top1_changed_case_ids":[cid for cid in rankmaps["rrf"] if (rankmaps["rrf"][cid][:1]!=rankmaps["rrf_reranker"][cid][:1])],
      "p_at_1_correctness_improved_case_ids":[cid for cid in rankmaps["rrf"] if (bool(rankmaps["rrf_reranker"][cid] and rankmaps["rrf_reranker"][cid][0] in goldmap[cid]) and not bool(rankmaps["rrf"][cid] and rankmaps["rrf"][cid][0] in goldmap[cid]))],
      "p_at_1_correctness_regressed_case_ids":[cid for cid in rankmaps["rrf"] if (bool(rankmaps["rrf"][cid] and rankmaps["rrf"][cid][0] in goldmap[cid]) and not bool(rankmaps["rrf_reranker"][cid] and rankmaps["rrf_reranker"][cid][0] in goldmap[cid]))]}
    result={"record_type":"m2_2_retrieval_ablation","run_id":str(uuid.uuid4()),"timestamp_utc":datetime.now(timezone.utc).isoformat(),"status":"PASS","mode":"APPROVED_DEV_VALIDATION_ONLY",
      "dataset_path":str(dataset_path.relative_to(ROOT)),"dataset_sha256":hashlib.sha256(raw).hexdigest(),"dataset_version":dataset["dataset_version"],"dataset_total":len(dataset["cases"]),"scored_n":len(cases),"scored_split_counts":{s:sum(c["dataset_split"]==s for c in cases) for s in ("dev","validation")},
      "excluded_n":len(dataset["cases"])-len(cases),"excluded_dev_validation_n":len(excluded_dev_validation),
      "excluded_dev_validation_case_ids":[c["case_id"] for c in excluded_dev_validation],
      "test_candidate_count":sum(c.get("dataset_split")=="test_candidate" for c in dataset["cases"]),
      "test_candidate_query_or_gold_scored":False,"test_candidate_used_for_tuning":False,"unscorable_in_denominator":False,
      "knowledge_version":"sim-policy-2026-01","knowledge_index_sha256":hashlib.sha256((ROOT/"data/index.json").read_bytes()).hexdigest(),
      "git_commit":subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip(),"python":sys.version.split()[0],
      "retrieval_config":{"version":"retrieval-m2.2-v1","sparse_top_k":5,"dense_top_k":5,"rrf_constant":60,"rerank_candidate_limit":10,"output_top_k":5,"min_cosine_score":0.20,"device":"cpu","qdrant_mode":"local_persistent","query_rewrite":False},
      "models":{"embedding":{"id":"BAAI/bge-small-zh-v1.5","revision":SMALL_REV,"dimension":encoder.get_sentence_embedding_dimension(),"load_ms":round(emb_load,3)},
                 "reranker":{"id":"BAAI/bge-reranker-base","revision":RERANKER_REV,"load_ms":round(rank_load,3)},"rss_mb":{"before":round(rss0/1048576,2),"after_embedding":round(rss1/1048576,2),"after_reranker":round(rss2/1048576,2),"increment_total":round((rss2-rss0)/1048576,2)},"torch":torch.__version__},
      "metrics":metrics,"paired_effects":paired,"cases":case_output,"limitations":["小样本不声明统计显著；指标仅覆盖批准 dev/validation。","输入 JSON 文件包含 test_candidate 行以应用 split/status 筛选；这些行的 query/gold 未传入检索模型、未评分、未用于调参。","本记录报告当前知识库的10个政策chunk，不代表知识覆盖扩大。"]}
    outdir=ROOT/"eval/raw"; outdir.mkdir(exist_ok=True); out=outdir/f"m2.2-retrieval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{result['run_id'][:8]}.json"
    with out.open("x",encoding="utf-8") as f: json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({"path":str(out.relative_to(ROOT)),"scored_n":len(cases),"metrics":metrics,"models":result["models"],"status":"PASS"},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
