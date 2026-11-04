"""Exercise immutable retrieval snapshots, failed activation, deletion and rollback."""
import json,uuid
from datetime import datetime,timezone
from pathlib import Path
from app.m2.retrieval import IndexRegistry,RetrievalConfig,RetrievalService
from app.m0.data import ROOT

def main():
    root=ROOT/"data"/"retrieval_lifecycle_probe"; registry=IndexRegistry(root)
    original=ROOT/"knowledge"/"policies.json"; base=json.loads(original.read_text(encoding="utf-8"))
    first=registry.build(original)
    changed=json.loads(json.dumps(base,ensure_ascii=False)); changed["knowledge_version"] += "-lifecycle-probe"
    changed["documents"]=[doc for doc in changed["documents"] if doc["policy_id"]!="delivery"]
    edited=ROOT/"data"/"retrieval_lifecycle_probe_source.json"; edited.write_text(json.dumps(changed,ensure_ascii=False),encoding="utf-8")
    second=registry.build(edited)
    active_after_build=registry.current()[1]
    updated_chunk_count=len(registry.current()[0]["chunks"])
    failed=json.loads(json.dumps(changed,ensure_ascii=False)); failed["knowledge_version"] += "-failed"
    failed_path=ROOT/"data"/"retrieval_lifecycle_probe_failed_source.json"; failed_path.write_text(json.dumps(failed,ensure_ascii=False),encoding="utf-8")
    try: registry.build(failed_path,fail_before_switch=True); failure_atomic=False
    except RuntimeError: failure_atomic=registry.current()[1]==second
    service=RetrievalService(config=RetrievalConfig(mode="bm25"),registry=registry)
    after_delete=service.retrieve("物流政策的运单事件是什么",evaluation_at="2026-10-19T00:00:00Z")
    delivery_id="policies/delivery@2026-01#tracking"
    delivery_hidden=delivery_id not in [x["chunk_id"] for x in after_delete["evidence"]]
    registry.activate(first)
    after_rollback=service.retrieve("物流政策的运单事件是什么",evaluation_at="2026-10-19T00:00:00Z")
    delivery_restored=delivery_id in [x["chunk_id"] for x in after_rollback["evidence"]]
    result={"record_type":"m2_2_index_lifecycle","run_id":str(uuid.uuid4()),"timestamp_utc":datetime.now(timezone.utc).isoformat(),
      "status":"PASS" if first!=second and active_after_build==second and failure_atomic and delivery_hidden and delivery_restored else "FAIL",
      "base_fingerprint":first,"updated_fingerprint":second,"active_after_build":active_after_build,"failure_does_not_switch":failure_atomic,
      "base_chunk_count":10,"updated_chunk_count":updated_chunk_count,"deleted_policy_chunk_not_retrieved":delivery_hidden,
      "rollback_fingerprint":registry.current()[1],"deleted_chunk_available_after_rollback":delivery_restored,
      "source_policy_file_modified":False,"temporary_source_files":[str(edited.relative_to(ROOT)),str(failed_path.relative_to(ROOT))],
      "limitations":["This isolated lifecycle registry does not change the application registry or tracked policy source.","Vector update uses the existing cached BGE-small revision and ShopRelay local Qdrant path."]}
    out=ROOT/"docs"/"probes"/f"m2.2-index-lifecycle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    with out.open("x",encoding="utf-8") as f: json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({"path":str(out.relative_to(ROOT)),"status":result["status"],"failure_atomic":failure_atomic,"delete":delivery_hidden,"rollback":delivery_restored},ensure_ascii=False))
if __name__=="__main__": main()
