"""Append-only raw M0 retrieval evaluations; no frozen-set tuning."""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .baseline import SingleAgentBaseline
from .data import ROOT, digest


def evaluate(index_path: Path, dataset_path: Path, raw_dir: Path, top_k: int = 5, llm: bool = False) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    agent = SingleAgentBaseline(index_path)
    records = []
    for case in dataset["cases"]:
        result = agent.answer_llm(case["user_query"], case["evaluation_at"], top_k) if llm else agent.answer(case["user_query"], case["evaluation_at"], top_k)
        gold = set(case["acceptable_evidence_ids"])
        retrieved = result.get("retrieved", result["citations"])
        hits = [cid for cid in retrieved if cid in gold]
        records.append({"case_id": case["case_id"], "group_id": case["group_id"], "dataset_split": case["dataset_split"], "query": case["user_query"], "expected": sorted(gold), "retrieved": retrieved, "citations": result["citations"], "hit_at_1": bool(retrieved and retrieved[0] in gold), "recall_at_5": len(hits) / len(gold) if gold else None, "status": result["status"], "answer": result["answer"], "generation_mode": result["generation_mode"], "model": result.get("model"), "usage": result.get("usage"), "latency_ms": result.get("latency_ms"), "error_type": result.get("error_type")})
    scoreable = [r for r in records if r["recall_at_5"] is not None]
    metrics = {"n": len(records), "scoreable_n": len(scoreable), "p_at_1_numerator": sum(r["hit_at_1"] for r in scoreable), "p_at_1_denominator": len(scoreable), "mean_recall_at_5": sum(r["recall_at_5"] for r in scoreable) / len(scoreable) if scoreable else None}
    try:
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_commit = None
    raw = {"run_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(), "method": "single_agent_bm25_jieba_" + ("deepseek" if llm else "extractive"), "tokenizer": "jieba==0.42.1/default_dictionary/no_stopwords", "git_commit": git_commit, "dataset_version": dataset["dataset_version"], "dataset_sha256": digest(dataset), "knowledge_version": agent.index["knowledge_version"], "knowledge_source_sha256": agent.index["source_sha256"], "top_k": top_k, "model_calls": len(records) if llm else 0, "metrics": metrics, "cases": records}
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{raw['run_id']}.json"
    with path.open("x", encoding="utf-8") as out:
        json.dump(raw, out, ensure_ascii=False, indent=2)
    return {"path": str(path), "run_id": raw["run_id"], "metrics": metrics}
