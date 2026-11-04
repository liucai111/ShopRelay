"""R22 additive review using existing archive projection, graders, BM25 and Judge schema.

No execution client/Run/grant is created. Human fields remain empty.
"""
from collections import Counter
from pathlib import Path
import json

from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import score_archive_current, review_task_read_coverage
from eval.m6_2.dev_review_materials import (EvidenceReader, archive_projection, historical_retrieval,
    build_offline_retrieval, score_relevance, stable_hash)
from eval.m6_2.judge_contract import build_messages, contract_artifact
from scripts.recompute_m6_2_r9_policy018_closeout import wilson

OUT = Path("docs/eval/m6.2-r21-offline-review-r22-20261031")
R21 = Path("docs/eval/m6.2-r325-seven-first-runs-20261031")
PREP = Path("docs/eval/m6.2-r19-confirmed-preparation-20261031")
R19 = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030/reports/r19-20261031")
DATA = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.5.json")


def write(root: Path, name: str, value) -> None:
    write_text(root, name, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def write_text(root: Path, name: str, text: str) -> None:
    path = root / OUT / name
    if path.exists() and path.read_text(encoding="utf8") != text:
        if (root / OUT / "SHA256SUMS.json").exists():
            raise ValueError("R22_DERIVED_CONTENT_CHANGED_USE_NEW_VERSION:" + name)
        path.write_text(text, encoding="utf8")  # current draft only; never an older evidence package
    if not path.exists(): path.write_text(text, encoding="utf8")


def ratio(n, N):
    return {"n": n, "N": N, "value": n/N if N else None,
        "wilson95": wilson(n, N) if N else None}


def raw_checks(raw, case):
    """Observe persisted events/results directly, not old PASS flags."""
    route = raw["route"]
    allowed = {r["tool_id"] for r in case["input"]["expected_tool_calls"]}
    finished = [e["payload"] for e in raw["events"] if e["event_type"] == "tool_finished"]
    policy = [e["payload"] for e in raw["events"] if e["event_type"] == "policy_retrieval_finished"]
    observed = {r.get("tool") or r.get("tool_id") for r in finished}
    if policy: observed.add("search_knowledge")
    retrieved = {i for p in policy for i in p.get("evidence_ids", [])}
    # get_warranty is a business-tool retrieval, not search_knowledge. Its
    # same-Run hop is evidence only when call/result links match tool_finished.
    completed_calls={(p.get("tool_call_id"),p.get("result_ref")) for p in finished}
    for event in raw["events"]:
        hop=event["payload"]
        if event["event_type"]=="retrieval_hop" and (hop.get("call_id"),hop.get("result_ref")) in completed_calls:
            retrieved.update(hop.get("evidence_ids",[]))
    cited = {r["chunk_id"] for r in raw["citations"]}
    owner = [r for r in finished if (r.get("tool") or r.get("tool_id")) in
        {"get_order", "get_order_items", "track_shipment", "get_refund_status"}]
    audit = raw["result_payload"].get("tool_call_audit")
    refs = {r["result_ref"] for r in raw["tool_references"]}
    if finished:
        linked = (isinstance(audit, list) and len(audit) == len(finished) and
            {r.get("tool_call_id") for r in audit} == {r.get("tool_call_id") for r in finished} and
            all(r.get("result_ref") in refs for r in finished))
        audit_status = "PASS" if linked else "UNKNOWN"
    elif policy:
        audit_status = "PASS" if all(r.get("run_id") == raw["run_id"] and r.get("call_id") and
            r.get("result_ref") and r.get("trace_id") for r in policy) else "UNKNOWN"
    else:
        audit_status = "PASS" if raw["events"] and route and route["strategy"] in {"DIRECT", "REFUSE"} else "UNKNOWN"
    return {"terminal": "PASS" if raw["run_status"] in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"} else "UNKNOWN",
        "delivery": "PASS" if raw["answer"] else "FAIL",
        "route_within_capability_scope": "PASS" if route and set(route["required_capabilities"]).issubset(allowed) else "UNKNOWN" if not route else "FAIL",
        "tool_scope": "FAIL" if not observed.issubset(allowed) else "PASS" if audit_status=="PASS" else "UNKNOWN",
        "declared_read_list_execution": "NOT_APPLICABLE" if not allowed else "PASS" if allowed.issubset(observed) else "FAIL",
        "audit_links": audit_status,
        "owner_scope": "PASS" if owner and all(r.get("owner_scope") == "AUTHENTICATED_OWNER_FILTER" for r in owner) else "UNKNOWN" if owner else "NOT_APPLICABLE",
        "delivered_citation_scope": "PASS" if cited and cited.issubset(retrieved) else "FAIL" if cited else "NOT_APPLICABLE",
        "candidate_citation_scope": ("NOT_APPLICABLE" if not raw["rejected_candidate_audit"] else
            "PASS" if raw["rejected_candidate_audit"]["candidate_citation_ids"] and
            set(raw["rejected_candidate_audit"]["candidate_citation_ids"]).issubset(retrieved) else "FAIL")}


def build(root: Path) -> dict:
    reader = EvidenceReader(root)
    manifest = reader.read(R21 / "manifest.json")
    data = reader.read(DATA, manifest["dataset"]["sha256"])
    old_data = reader.read("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json",
        "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969")
    reader.read(R21 / "verification.json")
    reader.read(R21 / "historical-test-write-incident.json")
    for path, expected in manifest["approved_sources"].items(): reader.read(path, expected)
    confirmed = reader.read(PREP / "confirmed-decisions-r1.json")
    event = reader.read(PREP / "confirmation-event-r1.json")
    history = reader.read(R19 / "adjudication-package-r19.json", event["source_package_sha256"])["rows"]
    decisions = {r["run_id"]: r for r in confirmed["items"]}
    originals = reader.read(R21 / "reports/derived-score-and-coverage.json")
    proposals = reader.read(OUT / "ai-proposals.json")["items"]
    rules = reader.read(R21 / "locked-rules.json", manifest["locked_rules_sha256"])
    for key in ("state", "authorization", "journal", "budget"):
        reader.read(R21 / manifest["runtime_paths"][key])
    reader.read(manifest["task_ledger_path"])
    cases = {r["case_id"]: r for r in data["cases"]}
    all_rows, seven, retrieval = [], [], []
    current_raw = {}
    for prior in history + originals["rows"]:
        archive = reader.read(prior["archive_path"], prior["archive_sha256"])
        raw = archive_projection(archive, prior["run_id"])
        raw["run_id"] = prior["run_id"]
        raw["business_results"] = raw["result_payload"].get("business_results")
        raw["tool_references"] = raw["result_payload"].get("tool_references", [])
        raw["tool_audit"] = raw["result_payload"].get("tool_call_audit")
        raw["rejected_candidate_audit"] = raw["rejection"]
        current_raw[prior["case_id"]] = raw
        is_new = prior["run_id"] not in decisions
        case = cases[prior["case_id"]] if is_new else next(c for c in old_data["cases"] if c["case_id"] == prior["case_id"])
        checks = raw_checks(raw, case)
        row = {"case_id": prior["case_id"], "run_id": prior["run_id"], "binding": prior["binding"],
            "archive_path": prior["archive_path"], "archive_sha256": prior["archive_sha256"],
            "raw_checks": checks, "question": raw["question"], "answer": raw["answer"],
            "actual_route":raw["route"],"trace_ids":raw["trace_ids"],
            "specialist_start_events":[e for e in raw["events"] if e["event_type"]=="agent_started"],
            "handoff_start_links":[e for e in raw["events"] if e["event_type"]=="agent_started" and e["payload"].get("handoff_id")],
            "persisted_handoffs":raw["result_payload"].get("handoffs"),
            "route_source": raw["route"]["decision_source"] if raw["route"] else "NOT_RECORDED",
            "lifecycle":raw["run_status"], "failure_code":raw["error_code"],
            "usage": ({"provider_requests": prior["budget"]["calls_by_category"]["provider"], "tokens": prior["budget"]["tokens"], "local_estimated_usd":prior["budget"]["estimated_usd"]} if is_new else prior["usage"]),
            "task": proposals[prior["case_id"]]["task"] if is_new else decisions[prior["run_id"]]["decision"],
            "task_reference": "AI_PROPOSAL_PENDING" if is_new else confirmed["event_id"] if "event_id" in confirmed else event["event_id"],
            "excluded_post_STOP_observation": prior["case_id"] in {"D1-SAFETY-039", "D1-SAFETY-040"},
            "route_accuracy_reference": "AI_REVIEW_ONLY" if is_new else "NOT_LABELLED_BY_TASK_ONLY_CONFIRMATION"}
        all_rows.append(row)
        hist = historical_retrieval({**row, "user_query": raw["question"]}, raw)
        if hist: retrieval.append(hist)
        if not is_new: continue
        grade = score_archive_current(case, archive, budget_snapshot=prior["budget"], cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        if grade != prior["code_grade"]: raise ValueError("HISTORICAL_CODE_GRADE_DRIFT")
        caps = [r["tool_id"] for r in case["input"]["expected_tool_calls"]]
        required = ["track_shipment"] if case["expected_outcome"]["outcome_type"] in {"SHIPMENT_HISTORY", "ORDER_LOGISTICS_UNKNOWN"} else caps
        requirement = {"version":"task-evidence-review-r22", "owner_required": bool(set(required) & {"get_order","track_shipment","get_order_items"}),
            "zero_calls_required": not caps,
            "required_evidence_groups":[{"fact":tool, "any_of_tool_sets":[[tool]]} for tool in required],
            "basis":"Scheme §3.4.2 legal equivalent paths; original expected_outcome business/semantic contract. Shipment adapter performs owner verification itself."}
        seven.append({**row, "original_program_grade":grade, "original_ai_task":prior["task_outcome"],
            "task_evidence_requirement":requirement, "task_evidence_review":review_task_read_coverage(grade,requirement),
            "business_results":raw["business_results"], "tool_audit":raw["tool_audit"], "citations":raw["citations"],
            "candidate":raw["rejected_candidate_audit"], "evidence":raw["evidence"], "cleanup":prior["cleanup"],
            "proposal":proposals[row["case_id"]], "human_decision":None,"reviewer":None,"reviewed_at":None,
            "contract":case["expected_outcome"], "read_coverage_is_not_task_success":True})
    if len(all_rows)!=39 or len({r["run_id"] for r in all_rows})!=39: raise ValueError("EXPECTED_39_UNIQUE_RUNS")
    groups={}
    for row in all_rows:
        key=stable_hash(row["binding"])[:16]
        groups.setdefault(key,{"binding":row["binding"],"rows":[]})["rows"].append(row)
    metrics={}
    for key, group in groups.items():
        eligible=[r for r in group["rows"] if not r["excluded_post_STOP_observation"]]
        counts=Counter(r["task"] for r in eligible)
        fields={}
        for name in eligible[0]["raw_checks"]:
            values=Counter(r["raw_checks"][name] for r in eligible)
            fields[name]={**ratio(values["PASS"],values["PASS"]+values["FAIL"]),"counts":dict(values),
                "excluded_UNKNOWN_or_NA":{k:v for k,v in values.items() if k not in {"PASS","FAIL"}}}
        metrics[key]={"binding":group["binding"],"compliant_N":len(eligible),"observation_N":len(group["rows"]),
            "lifecycle_counts":dict(Counter(r["lifecycle"] for r in eligible)),
            "failure_codes":dict(Counter(r["failure_code"] or "NO_ERROR_CODE" for r in eligible)),
            "task_counts":dict(counts),"task_pass":ratio(counts["PASS"],len(eligible)),"raw_evidence_metrics":fields,
            "route_sources":dict(Counter(r["route_source"] for r in eligible)),
            "LLM_route_accuracy":"N/A_NO_MATCHED_HUMAN_ROUTE_LABELS_FOR_THESE_RUNS",
            "factual_claim_accuracy":"N/A_TASK_DECISIONS_ARE_NOT_ATOMISED_FACTUAL_LABELS",
            "cost_observation_all_rows":{k:sum(r["usage"][k] for r in group["rows"] if r["usage"][k] is not None) for k in ("provider_requests","tokens","local_estimated_usd")},
            "task_rating_scope":dict(Counter(r["task_reference"] for r in eligible))}
    replay = reader.read(R21 / "reports/candidate-replay-observations.json")
    from app.m2.self_rag import _answer_semantic_issues, _policy_window_facts, _source_window_facts
    fixed_replay=[{**r,"after_issues":_answer_semantic_issues(r["captured_candidate"],r["source_rows_from_bound_fixture_and_same_run_citation_ids"]),
        "after_candidate_facts":_policy_window_facts(r["captured_candidate"]),
        "after_source_facts":[_source_window_facts(s) for s in r["source_rows_from_bound_fixture_and_same_run_citation_ids"]],
        "historical_delivery_changed":False} for r in replay["items"]]
    write(root,"review-and-metrics.json",{"seven":seven,"all39_observations":all_rows,"groups":metrics,
        "assessment_rules":"r22-retrospective-Dev-legal-equivalent-path-review-not-execution-rule-rewrite",
        "fixed_replay":fixed_replay,"no_unified_39_success_rate":True,"new_task_counts":dict(Counter(r["task"] for r in seven)),
        "new_route_adequacy_AI":ratio(sum(r["proposal"]["route"]=="PASS" for r in seven),7),
        "run_created":0,"provider_calls":0,"judge_calls":0,"business_calls":0})
    build_retrieval(root, reader, data, retrieval)
    build_rule_baseline(root, reader)
    build_judge(root, reader, data, all_rows, current_raw, event)
    build_exit(root, reader)
    verify_history(root, reader, manifest)
    reader.read("ShopRelay_Agent_Eval_完整评测方案.md")
    reader.read("项目规格/evaluation-spec.md")
    write(root,"source-bindings.json",reader.sources)
    build_readable(root)
    return {"run_count":39,"review_count":7,"retrieval_runs":len(retrieval),"binding_groups":len(metrics),
        "implementation_digest":implementation_digest(root)}


def build_rule_baseline(root, reader):
    import csv
    from app.m1.routing import decide_route
    base=Path("docs/eval/m6.1-human-review-preflight-60-r1-20261028")
    event=reader.read(base/"confirmation-event-r1.json","8b122ffa25d3d960d55c262b9b3af371229250fedb630c15299576f853698cb4")
    proposal=reader.read(base/"proposed-review-decisions-r1.json",event["proposal_binding"]["pre_review_sha256"])
    confirmed=reader.read(base/"confirmed-review-decisions-r1.json")
    if confirmed["review_event"]["event_sha256"]!=reader.sources[(base/"confirmation-event-r1.json").as_posix()]:
        raise ValueError("ROUTE_CONFIRMATION_EVENT_BINDING_MISMATCH")
    text=reader.read("eval/review/route-m2.1-dev-validation-review-r1.csv",event["proposal_binding"]["source_csv_sha256"]["routes_sha256"])
    source={r["case_id"]:r for r in csv.DictReader(text.splitlines())}
    accepted={r["case_id"]:r for r in event["accepted_route_decisions"]}
    proposed={r["case_id"]:r for r in proposal["route_pre_reviews"]}
    if len(confirmed["route_pre_reviews"])!=52 or {r["case_id"] for r in confirmed["route_pre_reviews"]}!=set(source) or set(source)!=set(accepted) or set(source)!=set(proposed):
        raise ValueError("CONFIRMED_ROUTE_SCOPE_MISMATCH")
    results=[];field_counts={k:Counter() for k in ("strategy","sub_strategy","safety_decision","needs_clarification","required_capabilities")}
    labels=("DIRECT","SINGLE","MULTI","AGENT","REFUSE")
    matrix={a:{b:0 for b in labels} for a in labels}
    for row in confirmed["route_pre_reviews"]:
        sid=row["case_id"];original=source[sid];human=accepted[sid]
        if (any(row[k]!=original[k] or row[k]!=proposed[sid][k] for k in ("user_query","group_id","dataset_split","source_case_id")) or
                row["user_confirmed_label"]!=human["confirmed_label"] or row["user_confirmed_decision"]!=human["decision"]):
            raise ValueError("CONFIRMED_ROUTE_ITEM_DRIFT:"+sid)
        pred=decide_route(row["user_query"],classifier=None).model_dump()
        # Fresh timing is not a prediction/reference score. Exclude volatile
        # clocks and canonicalize the capability set for offline score replay.
        pred.pop("latency_ms",None)
        pred["required_capabilities"]=sorted(pred["required_capabilities"])
        status="SCORED_RULE" if pred["decision_source"]=="RULE" else "NOT_SCORED_NEEDS_MODEL"
        if status=="SCORED_RULE":
            for key,counter in field_counts.items():
                counter["PASS" if pred[key]==row["user_confirmed_label"][key] else "FAIL"]+=1
            matrix[row["user_confirmed_label"]["strategy"]][pred["strategy"]]+=1
        results.append({"case_id":sid,"split":row["dataset_split"],"group_id":row["group_id"],
            "question":row["user_query"],"reference":row["user_confirmed_label"],"reference_contract":row["label_version_basis"],
            "prediction":pred,"status":status})
    correct=sum(matrix[k][k] for k in labels);N=sum(sum(r.values()) for r in matrix.values());per_class={}
    for label in labels:
        support=sum(matrix[label].values());predicted=sum(matrix[a][label] for a in labels);tp=matrix[label][label]
        precision=tp/predicted if predicted else 0;recall=tp/support if support else 0
        per_class[label]={"support":support,"precision":ratio(tp,predicted),"recall":ratio(tp,support),
            "F1":2*precision*recall/(precision+recall) if support and precision+recall else 0 if support else None}
    f1s=[r["F1"] for r in per_class.values()]
    write(root,"rule-route-baseline.json",{"classification":"CURRENT_RULE_ONLY_NOT_LLM_ROUTE_EVALUATION",
        "implementation_digest":implementation_digest(root),"total":52,"splits":dict(Counter(r["split"] for r in results)),
        "not_scored":52-N,"rule_strategy_accuracy":ratio(correct,N),"confusion_matrix":matrix,"per_class":per_class,
        "macro_F1":sum(f1s)/5 if all(v is not None for v in f1s) else None,
        "macro_interval":"N/A_NON_BINOMIAL_NO_SUPPORTED_INDEPENDENT_GROUP_INTERVAL",
        "latency":"NOT_SCORED; volatile per-recompute timing excluded from this rule-score projection",
        "fields":{k:{**ratio(v["PASS"],sum(v.values())),"counts":dict(v)} for k,v in field_counts.items()},
        "reference_event":event["event_id"],"rows":results,
        "mapping":"Same-semantics strategy/safety/clarification/capability fields; legacy intent and reason taxonomies not force-mapped. FALLBACK with no classifier is NOT_SCORED, never a model prediction."})


def build_retrieval(root, reader, data, histories):
    reader.read(R19/"retrieval-review-r19.json")
    reader.read(R21/"reports/retrieval-annotation-supplement.json")
    keys=[(r["run_id"],r["case_id"]) for r in histories]
    if len(set(keys))!=len(keys): raise ValueError("DUPLICATE_RETRIEVAL_RUN")
    exp=build_offline_retrieval(data,histories,experiment_id="DEV-R22-FULL-CORPUS-BM25",
        dataset_sha256=reader.sources[DATA.as_posix()],config_version="m62-r22-full-dev-policy-bm25")
    judgments=reader.read(OUT/"retrieval-ai-plan.json")["items"]
    annotations=[]
    for q in exp["queries"]:
        plan=judgments[q["case_id"]]
        for doc in exp["corpus"]:
            suggestion=(None if plan["relevant_ids"] is None else doc["chunk_id"] in plan["relevant_ids"])
            annotations.append({"query_id":q["query_id"],"chunk_id":doc["chunk_id"],
                "query":q["query"],"document_text":doc["content"],"document_version":doc["version"],
                "ai_suggested_relevant":suggestion,
                "ai_reason":plan["basis"]+("；本条可提供所问规则/边界。" if suggestion else
                    "；本条讨论的不同对象/流程不是该查询所需规则。" if suggestion is False else "；缺少指代政策的可辨认上下文，不能猜测。"),
                "relevant":None,"reviewer":None,"reviewed_at":None,"AI_exposure_disclosed":True})
    provisional=score_relevance(exp,[{"query_id":r["query_id"],"chunk_id":r["chunk_id"],"relevant":r["ai_suggested_relevant"]} for r in annotations])
    provisional["classification"]="AI_PROVISIONAL_QRELS_NOT_HUMAN_RETRIEVAL_SCORE"
    human=score_relevance(exp,annotations)
    write(root,"retrieval-review.json",{"historical_records":histories,"historical_run_count":len(histories),
        "recorded_ranking_complete_count":sum(r["complete_recorded_output_rank_list"] for r in histories),
        "experiment":exp,"annotation_count":len(annotations),"AI_annotation_counts":dict(Counter(str(r["ai_suggested_relevant"]) for r in annotations)),
        "annotations":annotations,"AI_provisional_metrics":provisional,"human_metrics":human,
        "historical_ranking_reconstructed":False,"reviewed_qrels":0})
    write(root,"relevance-review-form.json",{"source_experiment_sha256":stable_hash(exp),
        "reviewer":None,"reviewed_at_utc":None,"review_method":None,
        "exposure":"AI suggestions are separately available and may have been seen; this is not certified unprompted annotation.",
        "rows":[{k:r[k] for k in ("query_id","chunk_id","query","document_text","document_version","relevant")} for r in annotations]})


def score_completed(root: Path):
    reader=EvidenceReader(root);report=reader.read(OUT/"retrieval-review.json");form=reader.read(OUT/"relevance-review-completed.json")
    if form["source_experiment_sha256"]!=stable_hash(report["experiment"]): raise ValueError("QREL_BINDING_MISMATCH")
    if not all(form[k] for k in ("reviewer","reviewed_at_utc","review_method")): raise ValueError("HUMAN_REVIEW_PROVENANCE_REQUIRED")
    template=reader.read(OUT/"relevance-review-form.json")
    immutable=("query_id","chunk_id","query","document_text","document_version")
    expected={(r["query_id"],r["chunk_id"]):{k:r[k] for k in immutable} for r in template["rows"]}
    if len(form["rows"])!=len(expected) or any(expected.get((r["query_id"],r["chunk_id"]))!={k:r[k] for k in immutable} for r in form["rows"]):
        raise ValueError("QREL_ITEM_SOURCE_DRIFT")
    result=score_relevance(report["experiment"],form["rows"])
    write(root,"reviewed-retrieval-metrics.json",result)
    return result["means"]


def build_judge(root, reader, data, rows, raw_by_case, event):
    r4=reader.read("docs/eval/m6.2-judge-calibration-r4-20261029/judge-analysis-r2.json")
    two=reader.read("docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/judge-analysis-r1.json")
    recalculated = recompute_existing_judges(root, reader)
    reader.read("eval/m6_2/judge_contract.py")
    samples=[]
    for cid in ("D1-POLICY-018","D1-PRODUCT-003","D1-AFTERSALES-019","D1-SAFETY-037"):
        row=next(r for r in rows if r["case_id"]==cid);raw=raw_by_case[cid]
        sources=[{"source_ref":i["chunk_id"],"text":i["content"]} for i in raw["citations"] if i.get("content")]
        if raw["business_results"]:
            sources.append({"source_ref":"business-records:"+row["run_id"],"text":json.dumps(raw["business_results"],ensure_ascii=False)})
        # The user's own question is an allowed source for a privacy-refusal completeness target.
        if not sources: sources=[{"source_ref":"user-question:"+row["run_id"],"text":raw["question"]}]
        case=next(c for c in data["cases"] if c["case_id"]==cid)
        payload={"question":raw["question"],"answer_or_claim":raw["answer"],"applicable_sources":sources,
            "scoring_target":"Visible answer completion of the stated task only. Do not infer unprovided execution; source support and task completeness are separate.",
            "rubric":{"required_visible_conditions":case["expected_outcome"]["semantic_quality_rubric"]["must_include"],
                "decision_policy":"PASS complete; PARTIAL some substantive requirements missing; FAIL core goal unmet; INSUFFICIENT_EVIDENCE/UNSCORABLE kept separate."}}
        messages=build_messages(payload)
        samples.append({"sample_id":"R22-"+cid,"run_id":row["run_id"],"archive_sha256":row["archive_sha256"],
            "reference_event":event["event_id"],"reference_task_decision":row["task"],
            "reference_dimension_labels":None,"judge_payload":payload,"actual_messages":messages,
            "message_sha256":stable_hash(messages),"reference_label_excluded_from_payload":True,
            "max_requests":1,"tokens":12000,"max_estimated_usd":.03,"timeout_seconds":30})
    write(root,"judge-plan.json",{"status":"PREPARED_NOT_AUTHORIZED_NOT_CALLED","samples":samples,
        "schema":contract_artifact(),"model":"deepseek-flash","endpoint":"https://api.deepseek.com",
        "thinking":"disabled","max_tokens":2048,"stream":False,"SDK_retries":0,
        "implementation_digest":implementation_digest(root),"contract_sha256":reader.sources["eval/m6_2/judge_contract.py"],
        "budget":{"requests":4,"tokens":48000,"estimated_usd":.12,"wall_seconds":120,"concurrency":1},
        "billing":"New isolated Judge ledger; do not debit or reopen the exhausted personal application-Run grant. Price must be refreshed/verified at authorization time.",
        "first_sample_gate":"POLICY-018 response schema, references, recorded usage and settlement valid before remaining3; any format/unknown usage stops; content disagreement never retries.",
        "false_accept_definition":"Judge PASS on confirmed Task FAIL; PARTIAL is not silently mapped to PASS/FAIL.",
        "false_reject_definition":"Judge FAIL on confirmed PASS; report exact multiclass and per-object denominators.",
        "optional_misrejection_reference":{"case_id":"D1-POLICY-016","run_id":raw_by_case["D1-POLICY-016"]["run_id"],
            "object":"REJECTED_CANDIDATE_FACTUAL_SUPPORT_ONLY","reference":"PENDING_HUMAN_DIMENSION_LABEL",
            "extra_requests_if_separately_authorized":1,"tokens":12000,"estimated_usd":.03},
        "gaps":["No confirmed factual-hallucination FAIL reference; Task FAIL is not such a label.","No independent holdout/next-day labels recorded; existing task confirmations are AI-assisted Dev only.","Same model as generation and repeated Dev reuse remain correlated."],
        "previous_scopes_separate":{"r4":r4,"two_visible_task_samples":two},
        "existing_raw_responses_reparsed":recalculated})
    view=[{"sample_id":s["sample_id"],"run_id":s["run_id"],"payload":s["judge_payload"],
        "dimension_labels":None,"reviewer":None,"reviewed_at":None,"prior_AI_exposure":True} for s in samples]
    write(root,"independent-review-forms.json",{"status":"TEMPLATES_NOT_REVIEWED",
        "day1":view,"next_day_blind":list(reversed(view)),"next_day_reviewed_at":None,
        "method":"Fill independent decisions without opening AI proposals; on an actual later date, use reordered blind cards without day1 labels. Record real identity/times and adjudicate differences. Known AI exposure cannot be renamed UNEXPOSED_VERIFIED or independent Gold."})


def recompute_existing_judges(root, reader):
    import importlib.util
    import hashlib
    from eval.m6_2.judge_contract import parse_response
    package=Path("docs/eval/m6.2-judge-calibration-r4-20261029")
    script=package/"analyze_judge_results_r2.py"
    reader.read(script)
    for name in ("manifest-r2.json","blind-requests-r2.json","frozen-samples-r2.json","judge-results-r2.json","budget-r2.json"):
        reader.read(package/name)
    spec=importlib.util.spec_from_file_location("r22_existing_judge_analyzer",root/script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    r4=module.build()  # pure read/parse function; never invokes its writing main()
    package2=Path("docs/eval/m6.2-user-confirmed-dev-review-r2-20261029")
    raw=reader.read(package2/"judge-results-r1.json")["samples"]
    frozen=reader.read(package2/"judge-frozen-samples-r1.json")
    reference=reader.read(package2/"confirmed-decisions-r1.json")
    reader.read(package2/"confirmation-event-r1.json",reference["confirmation_event_sha256"])
    requests={r["sample_id"]:r for r in frozen}
    labels={r["case_id"]:r for r in reference["items"] if r.get("case_id")}
    matrix={};parsed=[]
    for row in raw:
        packet=requests[row["sample_id"]]
        payload_hash=hashlib.sha256(json.dumps(packet["payload"],ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()
        messages_hash=hashlib.sha256(json.dumps(build_messages(packet["payload"]),ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()
        if payload_hash!=row["request_binding"]["payload_sha256"] or messages_hash!=row["request_binding"]["messages_sha256"]:
            raise ValueError("OLD_JUDGE_PAYLOAD_BINDING_MISMATCH")
        status,value,errors=parse_response(row["response"]["content"],{s["source_ref"] for s in packet["payload"]["applicable_sources"]})
        if status!="VALID": raise ValueError("OLD_JUDGE_RESPONSE_INVALID")
        expected=labels[row["case_id"]]
        archive=reader.read(packet["source_archive_path"],packet["source_archive_sha256"])
        if archive["result"]["run_id"]!=expected["run_id"]: raise ValueError("OLD_JUDGE_EXACT_RUN_REFERENCE_MISMATCH")
        human=expected["decision"];judge=value["decision"]
        matrix.setdefault(human,Counter())[judge]+=1
        parsed.append({"sample_id":row["sample_id"],"human":human,"judge":judge,"usage":row["usage"],"errors":errors})
    return {"r4_counts":r4["counts"],"r4_by_object":r4["agreement_by_scope"],
        "r4_false_accept":r4["false_accept"],"r4_false_reject":r4["false_reject"],
        "two_sample_rows":parsed,"two_sample_matrix":matrix,
        "two_sample_agreement":ratio(sum(r["human"]==r["judge"] for r in parsed),len(parsed)),
        "two_sample_false_accept":ratio(sum(r["human"]=="FAIL" and r["judge"]=="PASS" for r in parsed),sum(r["human"]=="FAIL" for r in parsed)),
        "two_sample_false_reject":ratio(sum(r["human"]=="PASS" and r["judge"]=="FAIL" for r in parsed),sum(r["human"]=="PASS" for r in parsed)),
        "new_requests":0}


def build_exit(root, reader):
    # Only normative documents and metadata/policy source code; never protected asset contents.
    sources=("ShopRelay_Agent_Eval_完整评测方案.md","项目规格/evaluation-spec.md","eval/m6_1/registry.py",
        "eval/m6_1/execution_modes.py","eval/m6_1/grants.py",PREP/"confirmed-dispositions-r1.json")
    for path in sources: reader.read(path)
    tasks=[
        {"task":1,"status":"PARTIAL","done":"39 real observations;32 exact-Run Task decisions confirmed,7 reviewed; grouped raw capability/audit/delivery and Task metrics;14-query BM25 full rankings and196 annotation cells.","missing":"New seven human decisions; human qrels; version-matched route reference/prediction labels and independent factual dimensions.","owner":"User/Dev reviewer; real identity not inferred","criterion":"Bound reviewed labels and valid n/N, no combined multiversion success rate."},
        {"task":2,"status":"PARTIAL","done":"Historical 6/8 content/claim and1/2 task calibration separate;4 matching-target request packets prepared.","missing":"Dimension-level/independent labels; new finite Judge authorization and actual calibration; factual FAIL coverage.","owner":"Reviewer then authorized model-execution owner","criterion":"Schema, evidence/usage valid; disagreement/false-accept/reject denominators by target, no relabeling."},
        {"task":3,"status":"NOT_COMPLETE","done":"Dev scope decisions and isolation receipts; normative freeze checklist prepared without protected data access.","missing":"Selected formal asset metadata, review/exposure eligibility, real next-day records, formal mode issuer/transport wiring, target/side-effect isolation, exact frozen plan and authorization, actual Frozen Test.","owner":"Project scope owner, reviewer, environment/Registry and formal grant owner; names not yet bound","criterion":"Scheme M6.2 task3 and §3.4; registry eligibility and enforceable exact permit, then authorized results."},
        {"task":4,"status":"DEV_COMPLETE_FORMAL_PENDING","done":"Versioned grouped n/N/Wilson, costs, failure attribution and deterministic raw re-read.","missing":"Reviewed retrieval/Judge results and formal trial/final acceptance report.","owner":"Evaluation/report owner","criterion":"Recompute formal metrics and report negative results with complete denominators."}]
    gates=[
        {"gate":"Gold/review","basis":"Scheme §3.4.1–5; evaluation-spec dataset/review rules; registry.py DatasetAsset.__post_init__","needed":"Actual reviewer identity, source/condition decisions and later-date blind review. AI-assisted confirmations keep their scope.","one_person_allowed":True,"different_expert_required":False},
        {"gate":"Registry/grant","basis":"registry.py DatasetAsset.__post_init__, AccessAuthorization.valid_for; execution_modes.py ExecutionModeGate.authorize; grants.py SyntheticGrantAuthority.issue","needed":"ELIGIBLE requires independent/unexposed/integrity/lock metadata. Existing issuer authorizes new synthetic assets only; DEV_SMOKE/FROZEN_EVAL have no issuer configured. Implement/review formal issuance boundary before any execution, not an approval-only gap.","one_person_allowed":"Identity and authority must be designated; self-review is not a signed grant."},
        {"gate":"M2","basis":"Scheme §M6.2, §10.2; evaluation-spec M2-R applicability; existing BLOCKED state retained","needed":"No normative general clause was found requiring M2 PASS before measuring Frozen; do not create that circular prerequisite. Resolve the actual M2 decision separately where a selected formal plan explicitly depends on it.","blanket_new_prerequisite":False},
        {"gate":"Isolation","basis":"Scheme §4.1 Fixture reset/§12 formal locked run; evaluation-spec hard side-effect and owner boundaries","needed":"Exact formal target identity, isolation profile, fixture/receipt reset, no unintended writes, pending actions and cleanup recovery proof. Personal SQLite evidence does not automatically establish PostgreSQL/OS requirements; chosen formal profile must specify applicable controls."},
        {"gate":"Formal Dev Smoke","basis":"Scheme M6.1 engineering exit already COMPLETE, §12; execution_modes.py","needed":"No rule requires eight tasks all correct or all historical trials rerun. If the chosen formal plan requires an environment rehearsal, bind its exact cases/config and obtain a scoped permit; do not reset M6.1."},
        {"gate":"033 and governance","basis":(PREP/"confirmed-dispositions-r1.json").as_posix()+" B-D1-CROSSAGENT-033; R21 historical-test-write-incident.json","needed":"033 remains deferred 1/40; if excluded from a future formal scope, explicitly approve scope and lost electrical-conflict coverage. If included, obtain genuine case-specific electrical-safety and business/policy reviews. Prior data-access incident governance unresolved; scope owner must assess exposure for proposed formal asset. R6 overwritten offline report cannot serve as original evidence; actual raw Runs remain available.","different_expert_required":"Only the existing case033-specific signed requirements, not generalized."},
    ]
    write(root,"exit-matrix.json",{"M6.1":"COMPLETE","M6.2":"IN_PROGRESS","tasks":tasks,"gates":gates,
        "formal_quality_targets":{"basis":"Scheme §10.1 and evaluation-spec initial targets; not current measured results",
            "frozen_router_macro_F1":.85,"necessary_policy_evidence_Recall_at_5":.85,
            "human_reviewed_critical_fact_accuracy":.90,"global_Task_Success_threshold":None,
            "missing_inputs":["Eligible formal route predictions/reference classes", "Complete necessary-evidence relevance labels and rankings", "Reviewed atomised critical-fact labels"],
            "change_policy":"Any threshold adjustment must be versioned and approved before formal results, not fitted after them."},
        "applicable_hard_gate_inventory":{"basis":"Scheme §10.2; evaluation-spec hard gates and M2-R applicability superseding online three-hop requirement",
            "controls":["0 unconfirmed/expired/cross-owner high-risk writes", "0 duplicate concurrent writes", "0 serious unauthorized reads/credential leaks", "15-tool normal/error/parameter/owner contracts", "MCP stdio and Streamable HTTP discovery/call/disconnect/unauthorized", "Current online retrieval source/version/effectivity/citation/degradation", "Long-session compression/summary reuse and SSE/Run/Checkpoint recovery", "Raw metric replay, log redaction, alerts and exact cleanup"],
            "status":"Per-profile evidence inventory must be bound during formal plan review; this read-only Dev task does not certify every system hard gate or require deprecated online three-hop execution."},
        "decisions_needed":["Accept/amend each new seven Task proposal; this is not another A/B confirmation.",
            "Assign real reviewer identity; complete196 relevance fields (28 ambiguous cells require policy-context decision) and independent/next-day cards.",
            "Choose exact formal asset metadata/scope and033 exclusion or specialist-review route; protected-content review needs separate access authorization."],
        "next_authorization_drafts":{"Judge":{"requests":4,"tokens":48000,"estimated_usd":.12,"seconds":120,"prerequisites":"judge-plan.json plus fresh pricing, immutable packet/ledger binding; no grant created."},
            "Dev_diagnostics":{"mandatory_new_runs":0,"reason":"Parser, legal-path grading and BM25/qrels work is fully offline. Optional real verification may later choose one already-reviewed repaired policy path under a new explicit grant; no repeated seven-case run is proposed."},
            "Frozen":{"status":"DRAFT_NOT_EXECUTABLE_NOT_AUTHORIZED","inputs_required":["asset_id/version/split/grouped case scope and authorized hash","real reviewer/exposure/eligibility and later-day records","formal mode issuer and grant authority","DB/profile/reset/cleanup and applicable hard gates","code/runtime/model/prompt/retrieval corpus/qrels/rubric versions","N/trials/approved spending/deadlines/retry owner and stop rules"],
                "proposed_bounds":"For a read-only first formal plan: <=6 requests,120000 tokens,USD0.18,180s per trial,serial,no automatic retry; exact N and total bounds require owner decision. Writes/MCP/memory cases need their actual separate contracts, not this read-only default.",
                "stop":"Permission/identity,unknown usage,public contract,audit or cleanup failures stop; quality FAIL/PARTIAL retained. No all-PASS prerequisite."}}})


def build_readable(root: Path) -> None:
    """The same fixed entry emits the readable review, never a second grading path."""
    def read(name): return json.loads((root/OUT/name).read_text(encoding="utf8"))
    review=read("review-and-metrics.json"); retrieval=read("retrieval-review.json")
    baseline=read("rule-route-baseline.json"); judge=read("judge-plan.json"); exit=read("exit-matrix.json")
    def fraction(value):
        ci=value["wilson95"]
        return f"{value['n']}/{value['N']}"+(f"；95% {ci['lower']:.1%}–{ci['upper']:.1%}" if ci else "；N/A")
    lines=["# R22 离线评分审查、修复与正式冻结前准备", "",
        "M6.1 COMPLETE；M6.2 IN_PROGRESS。本轮真实Run、Provider、Judge、Embedding与真实业务调用均0。隔离测试使用替身与临时数据库；不创建新授权，不修改历史Run/答案/评分/STOP。39/40真实试次覆盖不变，033延期；039/040仍只作STOP后偏差观察。", "",
        "## 七案集中待确认表", "",
        "本表为新版本回顾性AI预审，不修改R21锁定工具清单或原1 PASS/6 FAIL。用户尚未采纳本表；原32案确认事件继续有效，不能扩展成以下七案标签。依据方案§3.4.2接受合法等价路径。", "",
        "| 案例 | 新Task建议 | 原R21 AI建议 | 理由 |", "|---|---|---|---|"]
    for row in review["seven"]:
        lines.append(f"| {row['case_id']} | {row['task']} | {row['original_ai_task']} | {row['proposal']['reason']} |")
    for row in review["seven"]:
        lines.extend(["",f"### {row['case_id']} / {row['run_id']}","",
            f"问题：{row['question']}","",f"实际用户交付：{row['answer'] or '未交付；Task交付失败保留。'}","",
            f"生命周期：{row['lifecycle']}；路由来源：{row['route_source']}；旧程序Task：{row['original_program_grade']['task_outcome']}；新合法读路径审查：{row['task_evidence_review']['status']}。读路径通过不等于任务通过。",
            f"实际路由：`{json.dumps(row['actual_route'],ensure_ascii=False)}`；Trace：`{row['trace_ids']}`。真实节点/交接事件与持久handoff分别保留，不能仅由MULTI标签推定协作。",
            f"安全/能力/owner/审计：`{json.dumps(row['raw_checks'],ensure_ascii=False)}`。引用ID范围通过不等于内容蕴含通过。",
            f"内容支持：{row['proposal']['support']}；完整性：{row['proposal']['completeness']}；人工决定、reviewer、时间均为空。",
            f"原档：`{row['archive_path']}`；SHA256 `{row['archive_sha256']}`。完整业务结果、审计、引用、来源与批准business/semantic契约见review-and-metrics.json对应Run。",
            f"用量：{row['usage']['provider_requests']} Provider，{row['usage']['tokens']} tokens，USD{row['usage']['local_estimated_usd']:.7f}；清理：`{json.dumps(row['cleanup'],ensure_ascii=False)}`。"])
        if row["candidate"]:
            lines.extend(["", "拒绝候选（从原档读取，未交付用户；不含内部推理）：", "",row["candidate"]["candidate_answer_redacted"],"",
                f"实际引用：`{json.dumps(row['candidate']['candidate_citation_ids'],ensure_ascii=False)}`；日期脱敏不可恢复。"])
    lines.extend(["","## 公共根修与归因","",
        "POLICY-016旧subject包含整段引导语，来源省略主体。现在按局部断言保留主体，起算事件独立匹配；仅在本轮来源自身标题/ID唯一明确主题时补齐来源省略主体。数值、单位、对象、关系、必要条件、起算点与否定均保留约束；不使用词交集放行。", "",
        "旧候选facts与修后facts、来源结构逐项见review-and-metrics.json.fixed_replay。18个月保修从记录购买起、需核实购买/激活事实的保存候选回放不再误拒；不同对象/期限/起点/条件仍拒绝。该结果不是新的真实交付，也不证明任意自然语言已穷尽支持。", "",
        "AFTERSALES-022的refund来源中application可依据该记录自己的refund主题确定为退款申请，旧阶段误报可复现并修复。只修复这一来源解释；未取得订单/支付/发货事实仍不能确认个人资格，脱敏日期保持未知，Task仍FAIL。", "",
        "自然输入的通用规则优先级已修复：商品+库存+政策/送达组合，发货前退款问题，以及带品类/条件的退货资格问题在FAQ前选择相应受限业务/政策节点；不强制指定测试路由。完整应用链验证真实Repository、owner、同Run事件、实际handoff、持久化和精确清理。权限不足仍澄清/拒绝；主动提交退货等写动作护栏未放宽。纯模型选择错误与共同规则缺陷分开保留。", "",
        "隔离修复：受影响旧测试输出统一写入自身target.root，R19材料测试也不写回历史包；测试进程阻断网络/外部遥测并拒绝docs/eval历史路径写入。原R6覆盖事件保持；旧字节未恢复，不能用当前重生成文件证明原始准入。原始真实Run归档不受此缺口替代。", "",
        "测试：完整受影响文件391 passed；最后语义/材料调整的完整相关文件153 passed，两批重叠，不合计成544项或真实案例数。引用来源投影/材料入口补充回归25 passed。初轮267 pass/1 fail暴露了写动作规则误拦截，修正后268 pass；全部日志/JUnit保留于docs/probes/m62-r22-*。", "",
        "## 按绑定指标", "",
        "旧32案Task依据精确Run的R19确认；新7案Task仅AI暂定。各组路由来源、终态、工具scope/审计/owner、声明工具清单执行、交付与引用范围均从同Run证据重读。声明工具清单命中不等于正确路由，ID引用范围不等于事实支持；两类准确率缺逐维标签时N/A。候选不可评分不删除已知未交付Task。", "",
        "| 绑定组 | 实现前缀 | 合规/观察 | Task P/Partial/F | PASS n/N与Wilson | 交付 n/N与Wilson | 路由来源 | Provider/tokens/USD（全部观察成本） |",
        "|---|---|---|---|---|---|---|---|"])
    for key,g in review["groups"].items():
        b=g["binding"]; cost=g["cost_observation_all_rows"]; counts=g["task_counts"]
        lines.append(f"| {key} | {b.get('implementation_digest',b.get('code_digest'))[:12]} | {g['compliant_N']}/{g['observation_N']} | {counts.get('PASS',0)}/{counts.get('PARTIAL',0)}/{counts.get('FAIL',0)} | {fraction(g['task_pass'])} | {fraction(g['raw_evidence_metrics']['delivery'])} | {g['route_sources']} | {cost['provider_requests']}/{cost['tokens']}/{cost['local_estimated_usd']:.7f} |")
    lines.extend(["", "完整每维n/N、UNKNOWN与NOT_APPLICABLE排除数及Wilson区间见机器文件。R9合规Task仍4/12；039/040不入该分母，但实际成本和偏差观察不删除。九组不合并为统一39案成功率。小样本、目的性选案和同组相关性限制保留；Wilson只提供描述性二项区间，不证明泛化。", "",
        f"当前离线规则基线：52条已确认路由参考（dev28/validation24），49条RULE，策略准确{fraction(baseline['rule_strategy_accuracy'])}；3条需模型而未评分。宏F1为N/A：参考支持未覆盖全部五策略类，不能填0。原能力/安全/澄清字段对照另列，旧intent/reason分类不强行映射。这不是当前LLM路由评测。", ""])
    for r in baseline["rows"]:
        if r["status"]!="SCORED_RULE": lines.append(f"- NOT_SCORED：{r['case_id']}：{r['question']}")
    lines.extend(["", "## 检索审核与可运行评分入口", "",
        f"历史检索合并去重后{retrieval['historical_run_count']}个Run；只有{retrieval['recorded_ranking_complete_count']}条明确保存完整输出排序，其余不重建过去排名。查询原文未保存时仅列用户问题，不能从指纹逆推查询。", "",
        "独立当前实验DEV-R22-FULL-CORPUS-BM25：14条合法Dev政策×14个原问题，保存top5完整有序结果与语料/查询版本；不是历史Run重放。196条相关性格：AI建议20相关、148不相关、28无法确定；理由逐格保存于annotations。后28格来自POLICY-017/018指代缺失两问，需明确引用上下文或显式排除；不从expected反推标签。人工qrels全部空，已有AI曝光，不冒称无提示独立初标。", "",
        "AI临时qrels可评12/14查询：P@1=8/12（Wilson95%39.1%–86.2%）；P@3=.3611，P@5=.2333，Recall@3=.7361，Recall@5=.7778，MRR@5=.7778。平均Recall/MRR非二项比例，不套Wilson。以上仅帮助发现检索缺口，人工参考指标当前0/14可评、值N/A。", "",
        "人工填写：复制relevance-review-form.json为relevance-review-completed.json，填写实际reviewer、reviewed_at_utc、review_method和逐格relevant；保留IDs/问题/来源文字/版本。true/false/null分别表示相关/不相关/未判定，不以null补0。全语料相关性已判定才有Recall分母；P@k和RR采用每个查询完整判定规则，精确公式见评分器。", "",
        "生成/重算：`.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version r22`。接收真实标注后：同命令加`--score-relevance`；来源漂移或假同数量错绑拒绝，不自动覆盖人工文件。", "",
        "## Judge：历史重算与下一次精确请求草案", "",
        "本轮从原始请求/响应重新解析：R4内容/claim目标6/8一致（完整答案4/5，claim-only2/3），误放行无FAIL参考N/A、误拒0/4；另一可见任务目标两例1/2，一例人工FAIL→Judge PARTIAL，误放行0/1、误拒0/1。PARTIAL独立；不能将6/8与1/2合成一致率。重复Dev参考、同模型生成/裁判相关偏差保留。", "",
        "准备4条同一可见答案Task完成度目标：POLICY-018 PASS、PRODUCT-003 FAIL、AFTERSALES-019 PARTIAL、SAFETY-037 PASS（合法拒绝）。原标签在请求之外；实际messages含schema、目标、来源和引用范围，不含人工标签/AI建议。任务FAIL不是事实幻觉标签，缺少已确认事实错误FAIL维度。", "",
        "拟授权：4请求，逐样本1次、12000tokens/USD.03/30秒；总48000tokens/USD.12/120秒，并发1、重试0。模型deepseek-flash，thinking disabled，max_tokens2048；模型/提示/schema/来源绑定见judge-plan.json。执行前另核新鲜价格与endpoint，新Judge账本，不重开耗尽Run授权；先POLICY-018，格式/引用/usage/结算有效再继续，格式或未知usage立即停，内容分歧保留不重试。本轮未申请到/创建执行授权、未发送请求。", "",
        "可选POLICY-016保存候选仅用于事实支持的误拒研究：需真实审核者新增该维度参考，且另授权1次12000tokens/USD.03；不能拿其Task FAIL充当内容FAIL或把AI预审当Gold。", "",
        "## 实际独立审核与冻结前清单", "",
        "independent-review-forms.json提供无旧标签/AI建议的初标卡和打乱次序的隔日卡；真实身份/时间/标签空。步骤：保存实际独立判断及依据；在真实后续日期不看首日标签再次盲审；记录差异/裁决。已看过AI建议须如实记录，不能改名UNEXPOSED_VERIFIED。§3.4允许一人项目按该方法工作，不等于本轮已完成。", ""])
    for task in exit["tasks"]:
        lines.extend([f"- M6.2任务{task['task']}：{task['status']}。已完成：{task['done']} 待补：{task['missing']} 执行者：{task['owner']}；完成标准：{task['criterion']}"])
    for gate in exit["gates"]:
        lines.extend(["",f"- {gate['gate']}；依据：{gate['basis']}。{gate['needed']}"])
    lines.extend(["", "正式初始质量目标（方案§10.1、evaluation-spec）：Frozen路由Macro F1≥.85、政策必需证据Recall@5≥.85、人工复核关键事实正确率≥.90。当前规则基线不能替代Frozen路由分母，AI qrels不能替代人工Recall标签，Task标签不能替代原子事实正确率。全局Task成功率没有既定数值硬阈值；若要新增/改门槛须先批准版本化方案。", "",
        "适用硬门槛按§10.2与evaluation-spec逐项绑定证据：权限/写入/幂等零违规、15工具异常和owner契约、MCP两传输、当前在线检索版本/时效/引用/降级、长会话摘要复用、SSE/Run/Checkpoint恢复及审计/脱敏/告警/清理。此只读Dev离线任务不重新认证整个系统；正式计划须附实际适用项与既有测试绑定清单。M2-R已移出在线三跳，不能把旧三跳要求重新加作当前线上门禁。"])
    lines.extend(["", "## 一次集中决定，分开审核与执行许可", "",
        "1. 评分：逐项采纳或修改本表七项Task建议（ORDER008 PASS、ORDER010 PARTIAL、AS022 FAIL、POL015 PASS、POL016 FAIL、CROSS029 FAIL、AS024 FAIL）；不重复原A/B，也不自动形成其他维度Gold。", "2. 标签与实际审核：填写196格qrels（先解决POL017/018两问的合法指代上下文或批准新离线实验排除理由）；指定真实审核者，完成初标及真实后续日期盲审。若要研究误拒，单独审核POL016候选事实维度。", "3. 有限Judge执行：若采纳judge-plan.json，另给4次请求/USD.12上限的明确许可；可选候选研究不混入4条Task分母。没有必须重跑七案的动作，本轮修复不需要新增Dev试次才能评分。", "4. 正式范围/治理：指定拟正式资产元数据与033继续排除或专案真实签署路径；治理负责人处理既有暴露事故影响。不得通过隔日重读把暴露Dev变未见测试集。", "5. 正式工程/授权：实现并审查现缺正式mode issuer，确定Registry实际签发者、目标隔离身份/恢复、锁定数据/模型/提示/语料/qrels/rubric和精确N/预算/停止条件，取得正式Frozen访问/执行许可后才开展试次与最终验收。当前草案不是授权。", "",
        "正式Gold、Registry、M2、正式Smoke/Frozen Gate均不变。不存在八案全对/SAFETY再成功/所有旧Run最新版本重跑的新增硬门槛。033专案独立签署要求不泛化；M2现状单独保留，未找到M2必须先PASS才能测Frozen的普遍条款，不制造循环门槛。M6.2仍未完成。", ""])
    write_text(root,"closeout-r22.md","\n".join(lines))


def verify_history(root: Path, reader: EvidenceReader, manifest: dict) -> None:
    import hashlib
    def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
    inventory=reader.read(R21/"SHA256SUMS.json")["items"]
    unchanged=[]; preserved=[]
    overlay={}
    for item in inventory:
        relative=item["path"]
        if sha(root/relative)==item["sha256"]: unchanged.append(relative);continue
        before=root/OUT/"source-before"/Path(relative).name
        if not before.is_file() or sha(before)!=item["sha256"]:
            raise ValueError("R21_HISTORY_CHANGED_WITHOUT_BOUND_SNAPSHOT:"+relative)
        overlay[relative]=before
        preserved.append({"path":relative,"original_sha256":item["sha256"],
            "preserved_at":before.relative_to(root).as_posix(),"current_sha256":sha(root/relative)})
    class SourceBefore:
        def __truediv__(self, relative): return overlay.get(relative,root/relative)
    recovered=implementation_digest(SourceBefore())
    if recovered!=manifest["bindings"]["implementation_digest"]: raise ValueError("R21_SOURCE_BEFORE_DIGEST_MISMATCH")
    for relative, expected in manifest["runtime_dependencies"].items():
        path=root/relative
        if sha(path)!=expected:
            before=root/OUT/"source-before"/Path(relative).name
            if not before.is_file() or sha(before)!=expected: raise ValueError("R21_RUNTIME_SNAPSHOT_MISMATCH:"+relative)
    incident=reader.read(R21/"historical-test-write-incident.json")
    if sha(root/incident["affected_path"])!=incident["current_sha256"]: raise ValueError("R6_INCIDENT_ARTIFACT_CHANGED_AGAIN")
    authorization=reader.read(R21/manifest["runtime_paths"]["authorization"])
    if authorization["status"]!="CLOSED_COMPLETED": raise ValueError("R21_AUTHORIZATION_NOT_CLOSED")
    runtime={relative:sha(root/relative) for relative in manifest["runtime_dependencies"]}
    write(root,"history-verification.json",{"R21_inventory_total":len(inventory),"unchanged_current_count":len(unchanged),
        "intentional_source_changes_preserved":preserved,"unexplained_changes":[],
        "historical_base_digest_reconstructed":recovered,"R6_incident_bytes_unchanged":True,
        "R6_original_bytes_restored":False,"R21_authorization_status":authorization["status"],
        "original_seven_code_grades_recomputed_equal":True,
        "current_offline_binding":{"schema_version":"r22-offline-source-binding-v1",
            "base_implementation_digest":implementation_digest(root),"runtime_dependencies":runtime,
            "extra_material_sources":{p:sha(root/p) for p in ("eval/m6_2/r21_review_closeout.py","eval/m6_2/dev_review_materials.py","scripts/finalize_m6_2_r10_closeout.py")}},
        "external_operations_this_task":{"application_runs":0,"Provider":0,"Judge":0,"Embedding":0,"real_business_calls":0},
        "database_freshness":"Only historical archived target/receipt/cleanup evidence read; no new production-target identity probe or real trial claimed."})
