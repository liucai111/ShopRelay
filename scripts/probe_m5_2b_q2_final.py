"""Write sanitized M5.2-B-Q2 retrieval and memory diagnostic evidence."""
from __future__ import annotations

import importlib.metadata
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.m1.db import make_engine, make_session_factory
from app.m1.models import ObservabilityRecord, Run, RunEvent, Trace
from app.m2.retrieval import RERANKER_DIR, SMALL_DIR
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]
RUN_IDS = (
    "835c719a-3204-4555-a5da-11ebbce15e39",
    "c33aa6ab-af3a-4f57-85da-ba4999fa58b4",
)


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def directory_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def run_projection(db, run_id: str) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        return {"run_id": run_id, "found": False}
    result = json.loads(run.result_json) if run.result_json else {}
    retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
    policy = result.get("policy_retrieval") if isinstance(result.get("policy_retrieval"), dict) else {}
    aspects = []
    for item in policy.get("retrieval_calls", []):
        if not isinstance(item, dict):
            continue
        diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
        aspects.append({
            "aspect_id": item.get("aspect_id"),
            "strategy_selected": item.get("strategy_selected"),
            "strategy_executed": item.get("strategy_executed"),
            "degraded": item.get("degraded"),
            "failure_codes": [f.get("error") for f in item.get("failures", []) if isinstance(f, dict)],
            "knowledge_version": item.get("knowledge_version"),
            "index_fingerprint": item.get("index_fingerprint"),
            "evidence_ids": item.get("evidence_ids", []),
            "diagnostics_ms": {key: diagnostics.get(key) for key in
                ("bm25_ms", "vector_ms", "reranker_ms", "rrf_ms") if key in diagnostics},
            "total_retrieval_ms": item.get("latency_ms"),
            "models": item.get("models"),
        })
    citations = []
    for item in result.get("citations", []):
        if isinstance(item, dict):
            citations.append({key: item.get(key) for key in
                ("chunk_id", "version", "effective_from", "effective_to", "knowledge_version") if key in item})
        elif isinstance(item, str):
            citations.append({"chunk_id": item})
    evidence_ids = [item.get("chunk_id") for item in result.get("evidence", [])
                    if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)]
    events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                        .order_by(RunEvent.seq)).scalars().all()
    spans = []
    for row in db.execute(select(ObservabilityRecord).where(
            ObservabilityRecord.run_id == run_id, ObservabilityRecord.record_type == "span")
            .order_by(ObservabilityRecord.timestamp)).scalars():
        payload = json.loads(row.payload_json)
        spans.append({key: payload.get(key) for key in
            ("node_name", "strategy", "duration_ms", "status", "error_code")})
    trace_types = sorted({row.event_type for row in db.execute(
        select(Trace).where(Trace.run_id == run_id)).scalars() if row.event_type})
    failed_run = run.status != "SUCCEEDED"
    return {
        "run_id": run_id,
        "found": True,
        "status": run.status,
        "error_code": run.error_code,
        "latency_ms": run.latency_ms,
        "strategy": result.get("strategy"),
        "sub_strategy": result.get("sub_strategy"),
        "answer_persisted": bool(result.get("answer")),
        "usage": result.get("usage"),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        "retrieval_strategy_selected": retrieval.get("strategy_selected") or policy.get("strategy_selected"),
        "retrieval_strategy_executed": retrieval.get("strategy_executed") or policy.get("strategy_executed"),
        "degraded": retrieval.get("degraded", policy.get("degraded")),
        "knowledge_version": policy.get("knowledge_version") or result.get("knowledge_version"),
        "index_fingerprint": policy.get("index_fingerprint") or retrieval.get("index_fingerprint"),
        "retrieval_aspects": aspects,
        "citations": citations,
        "evidence_ids": evidence_ids,
        "citations_subset_of_run_evidence": all(row.get("chunk_id") in evidence_ids for row in citations),
        "events": [{"seq": row.seq, "type": row.event_type} for row in events],
        "spans": spans,
        "trace_event_types": trace_types,
        "failed_without_answer_or_done": failed_run and not result.get("answer") and
            not any(row.event_type in {"answer", "done"} for row in events),
    }


def main() -> None:
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Q2 evidence collection is restricted to loopback shoprelay_m11_test")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            runs = [run_projection(db, run_id) for run_id in RUN_IDS]
        output = {
            "schema_version": "shoprelay.m5.2-b-q2-final.v1",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "database": url.database,
            "postgresql_target": "existing isolated ShopRelay database; no schema or business writes by this collector",
            "runtime": {
                "python": platform.python_version(),
                "executable": str(Path(__import__("sys").executable).resolve()),
                "sys_path_has_project_vendor": any(str(ROOT / ".vendor") == p for p in __import__("sys").path),
                "packages": {name: package_version(name) for name in
                    ("sentence-transformers", "torch", "transformers", "qdrant-client")},
                "model_cache": {
                    "bge_small_snapshot_present": (SMALL_DIR / "modules.json").exists(),
                    "bge_small_revision": SMALL_DIR.name,
                    "bge_small_bytes": directory_bytes(SMALL_DIR),
                    "reranker_snapshot_present": (RERANKER_DIR / "model.safetensors").exists(),
                    "reranker_revision": RERANKER_DIR.name,
                    "reranker_bytes": directory_bytes(RERANKER_DIR),
                },
            },
            "hybrid_execution": {
                "expected_chain": ["BM25", "BGE-small vector retrieval", "RRF fusion", "BGE reranker"],
                "cold_start_timing_caveat": "First vector_ms includes model load and query inference; implementation does not separately instrument model load.",
                "reranker_timing_caveat": "reranker_ms measures predict after model initialization; load duration is not separately instrumented.",
            },
            "runs": runs,
            "external_deepseek_http_requests": 0,
            "production_business_writes": 0,
            "mcp_writes": 0,
            "payment_calls": 0,
            "browser": {
                "owner_a_run_and_evidence_panel": "actual in-app browser displayed MULTI, HYBRID_RERANK, citations and same-run evidence; browser refresh restored the run",
                "owner_b_account_switch": "actual in-app browser showed zero sessions and did not display owner A history when opened at A's session URL",
                "console": "observed browser log contained Vite debug connection entries only; no error/warning entries",
                "network_export": "NOT_VERIFIABLE; browser interface provides no exportable Network panel",
                "screenshot_file": "NOT_VERIFIABLE; no supported project-file export in current browser interface",
            },
            "memory_diagnostics": {
                "q1_observation": {
                    "summary_versions_1_to_4_source_ids": [],
                    "summary_versions_1_to_4_source_watermark": 0,
                    "summary_version_5_source_message_count": 2,
                    "estimated_tokens_before": 10828,
                    "estimated_tokens_after": 4962,
                    "effective_test_budget": 2500,
                    "token_measurement": "UTF-8 heuristic estimate; not provider tokenizer usage",
                },
                "interpretation": "Versions 1–4 were empty no-op summaries, not valid incrementally sourced versions; no evidence showed useful new source data. New code avoids advancing the durable version when source IDs/watermark have no delta.",
                "post_compaction_over_budget_handling": "untrusted historical excerpts are transiently pruned; protected verified-result references and Pending Action handles are separately retained; if the current query plus required context still exceeds budget, return CONTEXT_BUDGET_EXCEEDED before provider invocation.",
                "diagnostic_tests": [
                    "AUTO_COMPACT no-op delta does not advance version",
                    "transient history trimming preserves Pending Action handle and signals irreducible overage",
                    "provider not called when required context remains above budget",
                ],
                "thresholds_unchanged": {"soft": 0.50, "hard": 0.70, "auto_compact": 0.85},
            },
            "owner_http_evidence": "docs/probes/m5.2-b-q2-owner-http-r1.json",
            "cleanup": {"status": "PENDING", "note": "Exact probe rows are removed after evidence generation; final residual counts will be appended."},
        }
        path = ROOT / "docs" / "probes" / "m5.2-b-q2-final-r1.json"
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"written": str(path), "runs": runs, "model_cache": output["runtime"]["model_cache"]},
                         ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
