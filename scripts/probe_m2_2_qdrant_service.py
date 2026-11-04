"""Compare M2.2 service/local retrieval and verify Qdrant data after restart."""
import argparse,hashlib,json,os,sys,time,uuid
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REV="7999e1d3359715c523056ef9478215996d62a620"
MODEL=ROOT/".cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots"/REV
QUERY="已签收商品申请退款需要满足什么条件？"

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--verify-after-restart",type=Path); args=ap.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_HOME=str(ROOT/".cache/huggingface"))
    url=os.getenv("QDRANT_URL","http://127.0.0.1:6333")
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer
    from app.m2.retrieval import RetrievalConfig,RetrievalService
    encoder=SentenceTransformer(str(MODEL),device="cpu",local_files_only=True)
    client=QdrantClient(url=url,timeout=8); info=client.info()
    service=RetrievalService(config=RetrievalConfig(mode="vector",qdrant_mode="service",qdrant_url=url),encoder=encoder,qdrant_client=client)
    result=service.retrieve(QUERY); service_ids=[x["chunk_id"] for x in result["evidence"]]
    check={"collection_name":"sr_"+result["index_fingerprint"][:24],"service_ids":service_ids,
      "knowledge_version":result["knowledge_version"],"index_fingerprint":result["index_fingerprint"],
      "qdrant_version":getattr(info,"version",None),"embedding_revision":REV,"dimension":encoder.get_sentence_embedding_dimension()}
    check["service_collection_count"]=client.count(check["collection_name"],exact=True).count
    if args.verify_after_restart:
        baseline_path=args.verify_after_restart.resolve()
        base=json.loads(baseline_path.read_text(encoding="utf-8"))
        check["baseline_record"]=str(baseline_path.relative_to(ROOT)); check["reopened_after_restart_ids_match"]=service_ids==base["service_ids"]
        check["status"]="PASS" if check["reopened_after_restart_ids_match"] and check["service_collection_count"]==10 else "FAIL"
        check["local_service_initial_ids_match"]=base.get("local_ids")==base.get("service_ids")
    else:
        local=RetrievalService(config=RetrievalConfig(mode="vector",qdrant_mode="local"),encoder=encoder)
        local_result=local.retrieve(QUERY); check["local_ids"]=[x["chunk_id"] for x in local_result["evidence"]]
        check["status"]="PASS" if check["local_ids"]==service_ids and check["service_collection_count"]==10 else "FAIL"
        check["local_service_initial_ids_match"]=check["local_ids"]==service_ids
    check.update({"record_type":"m2_2_qdrant_service","run_id":str(uuid.uuid4()),"timestamp_utc":datetime.now(timezone.utc).isoformat(),
      "url_scope":"loopback-only" if url.startswith("http://127.0.0.1:") else "configured","query":QUERY,"latency_ms":round(result["latency_ms"],3),
      "mode":"verify_after_restart" if args.verify_after_restart else "initial_service_comparison"})
    outdir=ROOT/"docs/probes"; outdir.mkdir(exist_ok=True); out=outdir/f"m2.2-qdrant-service-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    with out.open("x",encoding="utf-8") as f: json.dump(check,f,ensure_ascii=False,indent=2)
    print(json.dumps({"path":str(out.relative_to(ROOT)),"status":check["status"],"match":check.get("local_service_initial_ids_match"),"collection":check["collection_name"]},ensure_ascii=False))
    client.close()
if __name__=="__main__": main()
