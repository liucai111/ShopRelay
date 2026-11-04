from __future__ import annotations
import argparse, hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.m0.config import load_settings
from app.m1.chat import ChatFailure, DeepSeekProvider
from app.m1.routing import ROUTE_VERSION, decide_route
from scripts.run_route_m2_five_way_predictions import (
    ROOT, REVIEWED_DIR, RAW_DIR, approved_cases_for_split, load_reviewed_dataset,
)
from scripts.evaluate_route_m2_five_way import score_rows

LABELS=("DIRECT","SINGLE","MULTI","AGENT","REFUSE")

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()

class BoundedRouteClassifier:
    """One provider call per ambiguous request, global cap shared across splits."""
    def __init__(self, provider, max_requests: int):
        self.provider=provider
        self.max_requests=max_requests
        self.request_attempts=0
        self.rows=[]
    def classify_route(self, query: str) -> dict:
        if self.request_attempts >= self.max_requests:
            raise RuntimeError("ROUTE_MODEL_REQUEST_BUDGET_EXHAUSTED")
        self.request_attempts += 1
        started=time.perf_counter()
        row={"request_index":self.request_attempts,"query_sha256":hashlib.sha256(query.encode("utf-8")).hexdigest()}
        try:
            value=self.provider.classify_route(query)
            row.update({"status":"RESPONSE","elapsed_ms":round((time.perf_counter()-started)*1000),
                        "model_id":value.get("model_id"),"usage":value.get("usage"),
                        "finish_reason_validation":"provider accepted only finish_reason=stop"})
            self.rows.append(row)
            return value
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            cause = exc.__cause__
            row.update({"status":"ERROR","elapsed_ms":round((time.perf_counter()-started)*1000),
                        "exception_type":type(exc).__name__,"root_exception_type":type(cause).__name__ if cause else None,
                        "provider_exception_type":diagnostic.get("provider_exception") if isinstance(diagnostic,dict) else None,
                        "provider_status":diagnostic.get("provider_status") if isinstance(diagnostic,dict) else None,
                        "error_code":getattr(exc,"code",None)})
            self.rows.append(row)
            raise

def run_split(data, split, classifier, remaining_cap):
    cases=approved_cases_for_split(data,split)
    predictions=[]
    for case in cases:
        before=classifier.request_attempts
        route=decide_route(case["user_query"],classifier if classifier.request_attempts<remaining_cap else None)
        used=classifier.request_attempts>before
        predictions.append({"case_id":case["case_id"],"predicted_strategy":route.strategy,
          "sub_strategy":route.sub_strategy,"decision_source":route.decision_source,
          "reason_code":route.reason_code,"safety_decision":route.safety_decision,
          "needs_clarification":route.needs_clarification,"required_capabilities":route.required_capabilities,
          "latency_ms":route.latency_ms,"model_id":route.model_id,"prompt_version":route.prompt_version,
          "usage":route.usage,"fallback_error":route.fallback_error,
          "classifier_request_attempted":used})
    return cases,predictions

def main():
    ap=argparse.ArgumentParser(description="Bounded, full two-layer Router run over approved dev and seen validation")
    ap.add_argument("--dataset",default="eval/dataset/reviewed/route-m2.2-five-way-reviewed-r2.json")
    ap.add_argument("--dev-output",required=True)
    ap.add_argument("--validation-output",required=True)
    ap.add_argument("--max-requests",type=int,default=40)
    a=ap.parse_args()
    if not (1<=a.max_requests<=40): raise SystemExit("--max-requests must be in [1,40]")
    data_path=(ROOT/a.dataset).resolve()
    outputs={"dev":(ROOT/a.dev_output).resolve(),"independent_validation_candidate":(ROOT/a.validation_output).resolve()}
    for path in outputs.values():
        if path.parent!=RAW_DIR or "test_candidate" in str(path).casefold(): raise SystemExit("outputs must be new files under eval/raw and outside test_candidate")
        if path.exists(): raise SystemExit(f"refusing to overwrite existing record: {path}")
    data=load_reviewed_dataset(data_path)
    try:
        settings=load_settings(require_api_key=True)
        provider=DeepSeekProvider(settings=settings)
    except Exception as exc:
        raise SystemExit(f"ROUTE_CLASSIFIER_NOT_CONFIGURED: {type(exc).__name__}") from exc
    tracker=BoundedRouteClassifier(provider,a.max_requests)
    split_records={}
    for split,out in outputs.items():
        cases,preds=run_split(data,split,tracker,a.max_requests)
        now=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
        record={"record_type":"five_way_route_predictions_full_two_layer","run_id":f"route-m2-five-way-full-{split}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}",
          "created_at_utc":now,"dataset_path":data_path.relative_to(ROOT).as_posix(),"dataset_version":data["dataset_version"],
          "dataset_sha256_canonical_cases":data["dataset_sha256_canonical_cases"],"split":split,"approved_sample_count":len(cases),
          "router_commit":__import__("subprocess").run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip(),
          "router_version":ROUTE_VERSION,"router_source_sha256":sha(ROOT/"app/m1/routing.py"),
          "classifier_enabled":True,"classifier_policy":"fallback-only; max one request per ambiguous case; SDK max_retries=0",
          "classifier_model":settings.model,"route_node":{"thinking":settings.nodes["route"].thinking,"max_tokens":settings.nodes["route"].max_tokens,
             "timeout_seconds":settings.nodes["route"].timeout_seconds,"max_cost_usd":settings.nodes["route"].max_cost_usd},
          "max_requests_shared_stage":a.max_requests,"request_attempts_so_far":tracker.request_attempts,
          "test_candidate_used":False,"predictions":preds}
        out.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        split_records[split]={"prediction_path":out.relative_to(ROOT).as_posix(),"prediction_sha256":sha(out),
           "evaluation":score_rows(cases,preds),"run_id":record["run_id"],"prediction_count":len(preds)}
    usage_rows=[r.get("usage") for r in tracker.rows if isinstance(r.get("usage"),dict)]
    prompt_tokens=sum(int(x.get("prompt_tokens") or 0) for x in usage_rows)
    completion_tokens=sum(int(x.get("completion_tokens") or 0) for x in usage_rows)
    costs=[float(x["cost_estimate_usd_peak_rate"]) for x in usage_rows if x.get("cost_estimate_usd_peak_rate") is not None]
    ledger={"record_type":"m2_router_full_two_layer_ledger","revision":"r1","created_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
      "router_version":ROUTE_VERSION,"source_sha256":sha(ROOT/"app/m1/routing.py"),"dataset_sha256_canonical_cases":data["dataset_sha256_canonical_cases"],
      "classifier_enabled":True,"sdk_automatic_retries":0,"request_budget":a.max_requests,"request_attempts":tracker.request_attempts,
      "successful_or_failed_provider_invocations":len(tracker.rows),"usage_available_for":len(usage_rows),"prompt_tokens":prompt_tokens,
      "completion_tokens":completion_tokens,"estimated_cost_usd_peak_rate":round(sum(costs),9) if costs else None,
      "cost_is_estimate_not_invoice":True,"request_ledger":tracker.rows,"splits":split_records,
      "validation_status":"SEEN_VALIDATION_REGRESSION_NOT_INDEPENDENT_VALIDATION","test_candidate_used":False}
    ledger_path=ROOT/"eval/raw/route-m2-fiveway-full-router-ledger-20261021-r1.json"
    if ledger_path.exists(): raise SystemExit(f"refusing to overwrite {ledger_path}")
    ledger_path.write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"full-router requests={tracker.request_attempts}/{a.max_requests} dev_f1={split_records['dev']['evaluation']['macro_f1']:.4f} seen_validation_f1={split_records['independent_validation_candidate']['evaluation']['macro_f1']:.4f}")
    print(ledger_path.relative_to(ROOT).as_posix())

if __name__=="__main__": main()
