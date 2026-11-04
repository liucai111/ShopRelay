"""Repeatable local-only cold/warm Hybrid retrieval probe for M5.4-B2."""
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import json
import os
import threading
import time
from pathlib import Path


QUERY = "普通商品退货期限和申请流程是什么？"


def memory_snapshot() -> dict | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCountersEx), ctypes.c_ulong)
    get_memory.restype = ctypes.c_int
    handle = get_process()
    ok = get_memory(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return None
    mib = 1024 * 1024
    return {"working_set_mb": round(counters.WorkingSetSize / mib, 1),
            "peak_working_set_mb": round(counters.PeakWorkingSetSize / mib, 1),
            "private_bytes_mb": round(counters.PrivateUsage / mib, 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("lazy", "prewarm", "concurrent"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    import_started = time.perf_counter()
    from app.m0.data import ROOT
    from app.m2.retrieval import RetrievalService, config_from_environment
    module_import_ms = (time.perf_counter() - import_started) * 1000

    config = config_from_environment()
    service = RetrievalService(ROOT / "data" / "index.json", config)
    index, fingerprint = service.registry.current(service.index_path)
    record = {
        "schema_version": "shoprelay.m5.4-b2.retrieval-probe.v1",
        "mode": args.mode,
        "query": QUERY,
        "query_class": "synthetic_public_policy_question",
        "module_import_ms": round(module_import_ms, 3),
        "python_executable": os.path.basename(os.sys.executable),
        "retrieval_config_version": config.version,
        "retrieval_config_hash": config.config_hash,
        "retrieval_mode_configured": config.mode,
        "knowledge_version": index["knowledge_version"],
        "index_fingerprint": fingerprint,
        "index_chunks": len(index["chunks"]),
        "model_revisions": {"embedding": "BAAI/bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620",
                             "reranker": "BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70"},
        "qdrant_mode": config.qdrant_mode,
        "samples": [],
        "memory": [],
    }
    record["memory"].append({"label": "after_python_import", **(memory_snapshot() or {})})

    def retrieve(label: str, mode: str = "rrf_reranker") -> dict:
        started = time.perf_counter()
        result = service.retrieve(QUERY, mode=mode)
        wall_ms = (time.perf_counter() - started) * 1000
        sample = {"label": label, "wall_ms": round(wall_ms, 3),
                  "retrieval_latency_ms": round(float(result["latency_ms"]), 3),
                  "diagnostics": result["diagnostics"], "degraded": result["degraded"],
                  "failures": result["failures"], "executed_mode": result["mode"],
                  "evidence_ids": [row["chunk_id"] for row in result["evidence"]],
                  "evidence_versions": sorted({row["policy_version"] for row in result["evidence"]}),
                  "knowledge_version": result["knowledge_version"]}
        record["samples"].append(sample)
        record["memory"].append({"label": label, **(memory_snapshot() or {})})
        return sample

    try:
        record["bm25_cold"] = retrieve("bm25_cold", mode="bm25")
        record["bm25_hot"] = [retrieve(f"bm25_hot_{i + 1}", mode="bm25") for i in range(3)]

        if args.mode == "prewarm":
            started = time.perf_counter()
            record["warmup"] = service.warmup()
            record["warmup_wall_ms"] = round((time.perf_counter() - started) * 1000, 3)
            record["memory"].append({"label": "after_warmup", **(memory_snapshot() or {})})
            record["hybrid_first_after_warmup"] = retrieve("hybrid_first_after_warmup")
        elif args.mode == "concurrent":
            barrier = threading.Barrier(2)
            def concurrent_call(index: int) -> tuple[dict, float]:
                barrier.wait(timeout=10)
                started = time.perf_counter()
                result = service.retrieve(QUERY, mode="rrf_reranker")
                return ({"index": index, "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                         "diagnostics": result["diagnostics"], "degraded": result["degraded"],
                         "mode": result["mode"], "evidence_ids": [row["chunk_id"] for row in result["evidence"]]},
                        time.perf_counter())
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                record["concurrent_first_requests"] = [future.result()[0] for future in
                    [pool.submit(concurrent_call, i + 1) for i in range(2)]]
            record["memory"].append({"label": "after_concurrent", **(memory_snapshot() or {})})
        else:
            record["hybrid_cold_or_first"] = retrieve("hybrid_cold_or_first")

        if args.mode != "concurrent":
            record["hybrid_hot"] = [retrieve(f"hybrid_hot_{i + 1}") for i in range(3)]
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
    finally:
        if service.client is not None and hasattr(service.client, "close"):
            service.client.close()

    rendered = json.dumps(record, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if "error" not in record else 2


if __name__ == "__main__":
    raise SystemExit(main())
