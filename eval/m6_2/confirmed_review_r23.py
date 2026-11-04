"""Apply the explicit R23 chat decisions, keeping Dev and formal review separate."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from eval.m6_2.dev_review_materials import score_relevance, stable_hash
from eval.m6_1.registry import DatasetRegistry

OUT=Path("docs/eval/m6.2-r23-confirmed-calibration-and-formal-wiring-20261031")
R22=Path("docs/eval/m6.2-r21-offline-review-r22-20261031")
EVENT="M62-R23-DEV-TASK-QRELS-CHAT-SELF-CONFIRMED-20261031-R1"
TASKS={"D1-ORDER-008":"PASS","D1-ORDER-010":"PARTIAL","D1-AFTERSALES-022":"FAIL",
    "D1-POLICY-015":"PASS","D1-POLICY-016":"FAIL","D1-CROSSAGENT-029":"FAIL","D1-AFTERSALES-024":"FAIL"}
JUDGE_IDS=("R22-D1-POLICY-018","R22-D1-PRODUCT-003","R22-D1-AFTERSALES-019","R22-D1-SAFETY-037")
QREL_CHANGES={("DEV-R22-FULL-CORPUS-BM25-D1-"+cid,chunk)
    for cid in ("AFTERSALES-019","CROSSAGENT-032") for chunk in ("syn-policy-return-v1","syn-policy-return-v2")}


def sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path: Path): return json.loads(path.read_text(encoding="utf8"))
def save(path: Path, value) -> None:
    text=json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    if path.exists():
        if path.read_text(encoding="utf8")!=text: raise ValueError("IMMUTABLE_DERIVED_RECORD_CHANGED:"+path.name)
    else:
        with path.open("x",encoding="utf8",newline="\n") as stream: stream.write(text)


def build(root: Path) -> dict:
    pkg=root/OUT;pkg.mkdir(exist_ok=True)
    source=root/R22
    # R22 contains source snapshots as well as raw records. Later authorized
    # source edits are expected; validate the exact immutable review inputs here.
    inventory={r["path"]:r["sha256"] for r in read(source/"SHA256SUMS.json")["items"]}
    names=("closeout-r22.md","review-and-metrics.json","retrieval-ai-plan.json","retrieval-review.json",
        "relevance-review-form.json","judge-plan.json","independent-review-forms.json","exit-matrix.json")
    bindings={}
    for name in names:
        path=source/name
        if sha(path)!=inventory[path.relative_to(root).as_posix()]: raise ValueError("R22_REVIEW_INPUT_CHANGED:"+name)
        bindings[path.relative_to(root).as_posix()]=sha(path)
    review=read(source/"review-and-metrics.json");retrieval=read(source/"retrieval-review.json")
    event_path=pkg/"confirmation-event.json"
    if not event_path.exists():
        save(event_path,{"event_id":EVENT,"recorded_at_utc":datetime.now(timezone.utc).isoformat(),
            "actor":"CHAT_USER_SELF_CONFIRMED","method":"AI_ASSISTED_EVIDENCE_REVIEW_EXPLICIT_USER_ADOPTION",
            "source":"Current explicit user message in this chat; original platform message timestamp not exposed",
            "task_decisions":TASKS,"qrel_changes":[{"query_id":q,"chunk_id":c,"relevant":True} for q,c in sorted(QREL_CHANGES)],
            "qrel_scope":"12 explicit queries under candidate relevance: answer basis, applicable conditions or potentially conflicting clauses;28 cells/2 referentially incomplete queries remain unknown",
            "independent_review":False,"blind_review":False,"gold_approved":False,
            "source_bindings":bindings,"Judge_authorization":{"sample_ids":list(JUDGE_IDS),"requests":4,"tokens":48000,
                "estimated_usd":1.0,"request_timeout_seconds":120,"batch_seconds":600,"concurrency":1,"retries":0},
            "formal_wiring_authorized":True,"formal_execution_authorized":False,
            "candidate_content_access_authorized":False,"unchanged_scope":["Original32 exact-Run decisions","39/40 observed;033 deferred","Dev remains exposed Dev"]})
    event=read(event_path)
    if (event["task_decisions"]!=TASKS or event["source_bindings"]!=bindings or event["actor"]!="CHAT_USER_SELF_CONFIRMED" or
            event["qrel_changes"]!=[{"query_id":q,"chunk_id":c,"relevant":True} for q,c in sorted(QREL_CHANGES)] or
            event["independent_review"] or event["blind_review"] or event["gold_approved"] or
            event["Judge_authorization"]!={"sample_ids":list(JUDGE_IDS),"requests":4,"tokens":48000,
                "estimated_usd":1.0,"request_timeout_seconds":120,"batch_seconds":600,"concurrency":1,"retries":0}):
        raise ValueError("CONFIRMATION_EVENT_DRIFT")
    tasks=[]
    for row in review["seven"]:
        archive=root/row["archive_path"]
        if sha(archive)!=row["archive_sha256"] or row["task"]!=TASKS[row["case_id"]]: raise ValueError("CONFIRMED_RUN_BINDING_DRIFT")
        tasks.append({**row,"human_decision":TASKS[row["case_id"]],"reviewer":"CHAT_USER_SELF_CONFIRMED",
            "reviewed_at":event["recorded_at_utc"],"confirmation_event":EVENT,"confirmation_event_sha256":sha(event_path),
            "review_method":event["method"],"scope":"Task Outcome only; not independent Gold or other dimensions"})
    save(pkg/"confirmed-seven-task-decisions.json",{"event_id":EVENT,"items":tasks,"counts":dict(Counter(TASKS.values())),
        "original_R21_retained":"1 PASS/6 FAIL; new confirmed retrospective scope2 PASS/1 PARTIAL/4 FAIL"})
    groups=json.loads(json.dumps(review["groups"]))
    for group in groups.values():
        if "AI_PROPOSAL_PENDING" in group["task_rating_scope"]:
            group["task_rating_scope"]={EVENT:group["task_rating_scope"].pop("AI_PROPOSAL_PENDING")}
    save(pkg/"confirmed-grouped-dev-metrics.json",{"groups":groups,"no_unified_39_success_rate":True,
        "task_confirmation_scope_only":True,"coverage":{"real_cases":39,"total_cases":40,"deferred":"D1-CROSSAGENT-033",
            "post_STOP_observations_excluded":["D1-SAFETY-039","D1-SAFETY-040"]}})
    template=read(source/"relevance-review-form.json")
    immutable=("query_id","chunk_id","query","document_text","document_version")
    source_rows={(r["query_id"],r["chunk_id"]):r for r in retrieval["annotations"]}
    if len(source_rows)!=196 or template["source_experiment_sha256"]!=stable_hash(retrieval["experiment"]): raise ValueError("EXPERIMENT_BINDING_DRIFT")
    qrels=[];changes=[]
    for row in template["rows"]:
        key=(row["query_id"],row["chunk_id"]);original=source_rows[key]
        if any(row[k]!=original[k] for k in immutable): raise ValueError("QREL_SOURCE_DRIFT")
        relevant=original["ai_suggested_relevant"]
        if key in QREL_CHANGES:
            if relevant is not False: raise ValueError("EXPECTED_FOUR_FALSE_TO_TRUE_CHANGES")
            changes.append({"query_id":key[0],"chunk_id":key[1],"old_relevant":False,"new_relevant":True,
                "reason":"Unknown category/opened state cannot exclude potentially applicable sealed-goods conflict clauses."})
            relevant=True
        qrels.append({**row,"relevant":relevant,"confirmed_by":EVENT if relevant is not None else None,
            "AI_original_suggestion":original["ai_suggested_relevant"]})
    if {(r["query_id"],r["chunk_id"]) for r in changes}!=QREL_CHANGES: raise ValueError("QREL_CHANGE_IDS_MISMATCH")
    if Counter(str(r["relevant"]) for r in qrels)!={"True":24,"False":144,"None":28}: raise ValueError("QREL_COUNTS_MISMATCH")
    save(pkg/"confirmed-dev-qrels.json",{"event_id":EVENT,"source_experiment_sha256":template["source_experiment_sha256"],
        "method":event["method"],"independent_annotation":False,"next_day_blind_review":False,
        "relevance_scope":"CANDIDATE_RELEVANCE_NOT_REQUIRED_EVIDENCE_OR_ORDER_ELIGIBILITY","changes":changes,"rows":qrels})
    metrics=score_relevance(retrieval["experiment"],qrels)
    metrics["classification"]="USER_ADOPTED_AI_ASSISTED_DEV_CANDIDATE_RELEVANCE_NOT_FORMAL_REQUIRED_EVIDENCE"
    save(pkg/"confirmed-retrieval-metrics.json",{"new":metrics,"old_AI_only":retrieval["AI_provisional_metrics"],
        "confirmation_event_sha256":sha(event_path),"scoreable_queries":12,"query_universe":14,
        "excluded":[{"case_id":cid,"reason":"No legal referent for this/cited policy in this offline query experiment;no expected-ID inference"} for cid in ("D1-POLICY-017","D1-POLICY-018")]})
    build_asset_metadata(root,pkg)
    save(pkg/"decision-source-bindings.json",bindings)
    return {"confirmed_tasks":dict(Counter(TASKS.values())),"qrels":196,"positive":24,"negative":144,"unknown":28,"retrieval_metrics":metrics["means"]}


def build_asset_metadata(root: Path, pkg: Path) -> None:
    base=root/"eval/registry/m6.1-dataset-registry-r1.json"
    registry=DatasetRegistry.load_metadata(base)  # metadata only; never follow source_path
    overlay_paths=[root/"eval/registry/m6.1-dataset-registry-governance-r2.json",root/"eval/registry/m6.1-a-governance-overlay-r3.json"]
    overlays=[read(p) for p in overlay_paths]
    # Overlay hashes identify the canonical metadata inventory, not file
    # formatting bytes. DatasetRegistry already checked this content digest.
    metadata_digest=registry.to_dict()["registry_sha256"]
    if any(o["base_registry_sha256"]!=metadata_digest for o in overlays): raise ValueError("GOVERNANCE_BASE_REGISTRY_MISMATCH")
    rows=[]
    for asset in registry.assets():
        m=asset.metadata()
        if asset.asset_id=="policy-candidate-family":
            decision=next(r for r in overlays[1]["affected_assets"] if r["asset_id"]==asset.asset_id)
            effective={**decision,"source_decision":overlays[1]["decision_id"]}
        else:
            effective={"exposure_status":m["exposure_status"],"scoreability":m["scoreability"],
                "limitation":"No inference of family exposure or safety;source content not opened. Independent formal eligibility not established."}
        rows.append({"registered_metadata":m,"effective_governance":effective,
            "content_read_or_hashed_this_task":False,"formal_eligible_now":False,
            "reviewer_identity_as_recorded":asset.reviewer_id,"missing":"Verified eligible scope/review/exposure/integrity and formal grant;unknown metadata is not reconstructed from content."})
    save(pkg/"formal-asset-metadata-inventory.json",{"source_bindings":{p.relative_to(root).as_posix():sha(p) for p in [base,*overlay_paths]},
        "base_registry_canonical_metadata_sha256":metadata_digest,"base_registry_file_sha256":sha(base),
        "registered_assets":rows,"registered_asset_count":len(rows),
        "candidate_family_case_count_as_registered":registry.get("policy-candidate-family").case_count,
        "candidate_case_level_inventory":"UNKNOWN: no authorized item-level metadata;family60 is not60 individually verified assets",
        "frozen_eligible_asset_count":0,"new_asset_ids_invented":False,
        "explicit_owner_disposition":"M6.1-A-OWNER-DISPOSITION-20261024-01:known affected family requires replacement;others not inferred exposed/safe",
        "protected_blind_test_isolation":"Owner disposition explicitly requires separate protected storage/ACL denial/steward least privilege and OS auditing before protected blind tests;not a universal requirement invented from SQLite status."})


def prepare_judge(root: Path) -> dict:
    """Freeze a fresh four-object grant only after the caller's offline checks."""
    from datetime import timedelta
    from eval.m6_2.judge_contract import build_messages
    pkg=root/OUT
    if any((pkg/name).exists() for name in ("judge-authorization-r1.json","judge-state-r1.json","judge-manifest-r1.json")):
        raise ValueError("R23_JUDGE_ALREADY_PREPARED_NEVER_RESET_ATTEMPTS")
    build(root)  # restart boundary: revalidate mutable confirmation/source files before freezing
    event=read(pkg/"confirmation-event.json");plan=read(root/R22/"judge-plan.json")
    if tuple(s["sample_id"] for s in plan["samples"])!=JUDGE_IDS: raise ValueError("JUDGE_SCOPE_MISMATCH")
    price=read(pkg/"official-pricing-evidence.json")
    if price["model"]!=plan["model"] or price["endpoint"]!=plan["endpoint"]: raise ValueError("PRICE_MODEL_MISMATCH")
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(price["checked_at_utc"].replace("Z","+00:00"))).total_seconds()
    if not 0<=age<=86400: raise ValueError("PRICE_EVIDENCE_NOT_FRESH")
    rows=read(root/R22/"review-and-metrics.json")["all39_observations"]
    samples=[];references=[];bindings={}
    def wire_hash(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    for row in plan["samples"]:
        raw=next(r for r in rows if r["run_id"]==row["run_id"])
        if sha(root/raw["archive_path"])!=row["archive_sha256"]: raise ValueError("JUDGE_RAW_RUN_DRIFT")
        if build_messages(row["judge_payload"])!=row["actual_messages"]: raise ValueError("PREPARED_JUDGE_MESSAGES_DRIFT")
        samples.append({"sample_id":row["sample_id"],"case_id":raw["case_id"],"run_id":row["run_id"],
            "source_archive_path":raw["archive_path"],"source_archive_sha256":row["archive_sha256"],
            "payload":row["judge_payload"],"payload_sha256":wire_hash(row["judge_payload"]),
            "messages_sha256":wire_hash(row["actual_messages"])})
        references.append({"sample_id":row["sample_id"],"run_id":row["run_id"],"decision":row["reference_task_decision"],
            "event_id":row["reference_event"],"dimension_labels":None,"target":"VISIBLE_ANSWER_TASK_COMPLETENESS_ONLY"})
        bindings[raw["archive_path"]]=row["archive_sha256"]
    for rel in (R22/"judge-plan.json",OUT/"confirmation-event.json",
            Path("docs/eval/m6.2-r19-confirmed-preparation-20261031/confirmation-event-r1.json"),
            Path("docs/eval/m6.2-r19-confirmed-preparation-20261031/confirmed-decisions-r1.json")):
        bindings[rel.as_posix()]=sha(root/rel)
    save(pkg/"judge-frozen-samples-r1.json",samples);save(pkg/"judge-reference-labels.json",references)
    save(pkg/"judge-budget-r1.json",{"limits":{k:v for k,v in event["Judge_authorization"].items() if k!="sample_ids"},
        "rates_usd_per_million":price["rates_usd_per_million"],"expected_response_models":[plan["model"],price["official_model_version"]],
        "actual_supplier_invoice":"UNKNOWN","estimate_is_local_not_supplier_cap":True})
    now=datetime.now(timezone.utc);batch="M62-R23-FOUR-JUDGE-20261031"
    manifest={"batch_id":batch,"execution_profile":"r23-confirmed-four","sample_order":list(JUDGE_IDS),
        "model":{"provider":"DeepSeek","model":plan["model"],"endpoint":plan["endpoint"],"same_model_bias":True},
        "limits":{k:v for k,v in event["Judge_authorization"].items() if k!="sample_ids"},"max_output_tokens":2048,"thinking":"disabled","stream":False,
        "source_bindings":bindings,"runtime_bindings":{p:sha(root/p) for p in (
            "eval/m6_2/confirmed_review_r23.py","eval/m6_2/judge_contract.py","app/m0/config.py","scripts/run_m6_2_user_confirmed_judge_r1.py")},
        "sample_paths_and_sha256":{"judge-frozen-samples-r1.json":sha(pkg/"judge-frozen-samples-r1.json")},
        "pricing":{"path":(OUT/"official-pricing-evidence.json").as_posix(),"sha256":sha(pkg/"official-pricing-evidence.json")},
        "created_at_utc":now.isoformat(),"billing":"New Judge-only ledger;historical application grant closed and not debited",
        "budget_sha256":sha(pkg/"judge-budget-r1.json"),
        "history_links":[{"path":p,"sha256":sha(root/p)} for p in (
            "docs/eval/m6.2-judge-calibration-r4-20261029/budget-r2.json",
            "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/judge-budget-r1.json",
            "docs/eval/m6.2-r325-seven-first-runs-20261031/task-cumulative-ledger.json")],
        "history_scope":"History links are separate, non-reset records; no claim that these selected scopes exhaust lifetime usage."}
    save(pkg/"judge-manifest-r1.json",manifest)
    auth={"authorization_id":"M62-R23-JUDGE-USER-FOUR-20261031","batch_id":batch,"status":"AUTHORIZED_READY",
        "authorized_by":"CHAT_USER_SELF_CONFIRMED","confirmation_event":EVENT,"confirmation_event_sha256":sha(pkg/"confirmation-event.json"),
        "manifest_sha256":sha(pkg/"judge-manifest-r1.json"),"frozen_samples_sha256":sha(pkg/"judge-frozen-samples-r1.json"),
        "expires_after_utc":(now+timedelta(hours=1)).isoformat(),"scope":"ONLY_FOUR_JUDGE_REQUESTS_NO_AGENT_OR_FORMAL_RUN"}
    save(pkg/"judge-authorization-r1.json",auth)
    save(pkg/"judge-state-r1.json",{"batch_id":batch,"status":"READY","closed":False,"usage_unknown":False,
        "manifest_sha256":auth["manifest_sha256"],"authorization_sha256":sha(pkg/"judge-authorization-r1.json"),
        "provider_requests_attempted":0,"attempted_sample_ids":[],"settled_sample_ids":[],"settled_tokens":0,
        "local_estimated_usd":0.0,"reserved_tokens":0,"reserved_usd":0.0})
    return {"batch_id":batch,"sample_count":4,"authorization":"AUTHORIZED_READY"}


def analyze_judge(root: Path) -> dict:
    from eval.m6_2.judge_contract import parse_response
    from scripts.recompute_m6_2_r9_policy018_closeout import wilson
    pkg=root/OUT
    samples=read(pkg/"judge-frozen-samples-r1.json");labels={r["sample_id"]:r for r in read(pkg/"judge-reference-labels.json")}
    results=read(pkg/"judge-results-r1.json")["samples"] if (pkg/"judge-results-r1.json").exists() else []
    by_id={r["sample_id"]:r for r in results};rows=[];matrix={};decisions=[]
    for sample in samples:
        label=labels[sample["sample_id"]];result=by_id.get(sample["sample_id"])
        if result is None:
            rows.append({"sample_id":sample["sample_id"],"run_id":sample["run_id"],"status":"NOT_RUN","reference":label["decision"]});continue
        if (result["request_binding"]["payload_sha256"]!=sample["payload_sha256"] or
                result["request_binding"]["messages_sha256"]!=sample["messages_sha256"]): raise ValueError("JUDGE_ACTUAL_REQUEST_DRIFT")
        status,value,errors=parse_response(result["response"]["content"],{s["source_ref"] for s in sample["payload"]["applicable_sources"]})
        valid=status=="VALID" and result["usage_valid"] and result["model_binding_valid"] and result["response"]["finish_reason"]=="stop"
        decision=value["decision"] if valid else None
        rows.append({"sample_id":sample["sample_id"],"run_id":sample["run_id"],"question":sample["payload"]["question"],
            "visible_answer":sample["payload"]["answer_or_claim"],"sources":sample["payload"]["applicable_sources"],
            "rubric":sample["payload"]["rubric"],"reference":label["decision"],"reference_event":label["event_id"],
            "status":status if valid else result["parse_status"],"judge":decision,"judge_output":value,
            "agreement":decision==label["decision"] if valid else None,"usage":result["usage"],"errors":errors,
            "human_dimension_agreement":"N/A_NO_CONFIRMED_DIMENSION_LABELS","reviewer":None,"reviewed_at":None,"new_human_decision":None})
        if valid:
            matrix.setdefault(label["decision"],Counter())[decision]+=1;decisions.append((label["decision"],decision))
    def rate(n,N): return {"n":n,"N":N,"value":n/N if N else None,"wilson95":wilson(n,N) if N else None}
    output={"target":"VISIBLE_ANSWER_TASK_COMPLETENESS_ONLY","rows":rows,"sample_universe":4,
        "format_valid_comparable":len(decisions),"not_run":sum(r["status"]=="NOT_RUN" for r in rows),"matrix":matrix,
        "agreement":rate(sum(a==b for a,b in decisions),len(decisions)),
        "false_accept_PASS_on_FAIL":rate(sum(a=="FAIL" and b=="PASS" for a,b in decisions),sum(a=="FAIL" for a,b in decisions)),
        "false_reject_FAIL_on_PASS":rate(sum(a=="PASS" and b=="FAIL" for a,b in decisions),sum(a=="PASS" for a,b in decisions)),
        "PARTIAL_kept_separate":True,"same_model_and_reused_Dev_bias":True,"independent_calibration_complete":False,
        "no_pooling_with_R4_content_claim_or_old_two_task_samples":True,
        "reference_dimension_labels_not_inferred_from_task":True}
    save(pkg/"judge-analysis.json",output)
    return output


def finalize(root: Path,*,junit_path: Path | None=None) -> dict:
    """Single additive closeout after the one authorized Judge sequence closes."""
    import xml.etree.ElementTree as ET
    from eval.m6_1.grants import FormalExecutionPlan
    pkg=root/OUT
    closure=read(pkg/"judge-closure-r1.json")
    analysis=analyze_judge(root)
    q=read(pkg/"confirmed-retrieval-metrics.json");inventory=read(pkg/"formal-asset-metadata-inventory.json")
    suites=ET.parse(junit_path or root/"docs/probes/m62-r23-final-verified.xml").getroot().findall("testsuite")
    tests={k:sum(int(s.get(k,"0")) for s in suites) for k in ("tests","failures","errors","skipped")}
    if tests["failures"] or tests["errors"]: raise ValueError("OFFLINE_ACCEPTANCE_NOT_PASSED")
    deployment={"status":"TEMPLATE_NOT_DEPLOYED_NOT_AUTHORIZED","issuer":None,"trusted_issuers":[],"subject":None,
        "signing_key_env":"SHOPRELAY_EVAL_FORMAL_SIGNING_KEY","secret_value":None,
        "approved_plan_hashes":[],"allowed_targets":[],"actual_target_binding":None,"registry_metadata":None,
        "result_store":None,"grant_file":None,"experiment_id":None,"source_revision_label":"Use locked working-tree/runtime hashes, not Git HEAD alone",
        "plan":{key:None for key in FormalExecutionPlan.__dataclass_fields__},"experiment_config":None,"scoring_version":None,
        "factory_required_methods":["verify_execution_subject(expected_subject)->bool from real authentication, not echoed config",
            "verify_execution_target(target)->bool from actual profile/DB/endpoint identity",
            "prepare(case,trial_id)->exact receipt; no unapproved business action",
            "execute(case,trial_id,receipt,ledger,remaining_seconds=...)->FormalTrialResult; request-level reservations and deadline-aware SDK/tool adapters",
            "cleanup(receipt,trial,raw)->True only after exact residual/lease proof"],
        "factory_contract":"Reviewed owner-authenticated implementation from existing application runtime, bound by source hash in plan.runtime_bindings and config.environment.execution_factory. No caller-selected unbound module.",
        "ledger_initialization":"Issuer/environment owner provisions and protects one registered ResultStore path;missing ledger is not silently recreated by formal CLI. Same approval_ref is unique;unknown consumed work is never replayed.",
        "commands":[".venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <reviewed-config.json> --issue",
            ".venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <reviewed-config.json> --preflight",
            ".venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <reviewed-config.json> --execute"]}
    save(pkg/"formal-deployment-input-template.json",deployment)
    items=[
        {"item":"Dev Task decisions","status":"DONE","evidence":"confirmed-seven-task-decisions.json plus original32 R19 event;Task-only, exact Run scope"},
        {"item":"Dev relevance metrics","status":"DONE_WITH_EXPLICIT_EXCLUSIONS","evidence":"196 cells:24 relevant144 irrelevant28 unknown;12/14 queries;candidate relevance,not required-evidence Gold"},
        {"item":"Limited Judge batch","status":closure["status"],"evidence":"judge-analysis.json;4 frozen objects at most once;no dimension labels inferred","remaining":"Review any disagreements, no resampling;not universal/independent calibration"},
        {"item":"Formal execution engineering","status":"OFFLINE_VERIFIED_NOT_DEPLOYED","evidence":"grants.py,execution_modes.py,experiment.py,runner.py,shoprelay_adapter.py and actual CLI isolated tests","remaining":"Real issuer/key permissions,executor identity resolver,registered durable ledger,target factory,exact approved plan"},
        {"item":"Formal eligible asset","status":"BLOCKED_REAL_REVIEW_AND_REPLACEMENT","evidence":"metadata inventory:11 registered assets;0 eligible formal assets. policy-candidate-family60 known family quarantined/replacement required;case-level IDs not available in allowed metadata","remaining":"Steward records replacement metadata and lawful review scope/access;uncertain other assets remain unknown,not inferred exposed"},
        {"item":"Independent annotation/next-day blind","status":"NOT_DONE","evidence":"§3.4 and evaluation-spec;AI-adopted Dev decisions remain valid but are not blind/independent Gold","remaining":"Actual reviewer identity,initial decisions,real later-date blind decisions and disagreement resolution;one-person process allowed where applicable"},
        {"item":"Protected blind-test isolation","status":"NOT_VERIFIED_FOR_FORMAL_TARGET","evidence":"M6.1-A-OWNER-DISPOSITION-20261024-01 expressly requires protected storage/ACL denial/steward privilege and OS auditing","remaining":"Environment owner supplies actual signed/dated isolation and recovery evidence;do not infer from SQLite Dev receipts"},
        {"item":"033 and prior integrity incidents","status":"HELD_AS_CONFIRMED","evidence":"033 remains1/40 not run;R6 overwritten offline report not restored;raw Run evidence separate","remaining":"Exclude electrical-conflict coverage explicitly in proposed formal scope,or obtain genuine case-specific safety/business reviews if included. Do not generalize expert signature to all cases."},
        {"item":"Quality targets and calibration","status":"FORMAL_INPUTS_MISSING","evidence":"§10.1:Router macro F1>=.85;necessary-policy Recall@5>=.85;human critical-fact accuracy>=.90","remaining":"Version-matched formal predictions/reference classes,necessary-evidence qrels and atomised reviewed fact labels;Dev candidate Recall and Task labels are different objects"},
        {"item":"Exact formal permit/Frozen Test","status":"NOT_AUTHORIZED_NOT_EXECUTED","evidence":"Current instruction expressly excludes Gold approval,content unsealing and real Frozen execution","remaining":"After concrete scope/review/target/config/N/budget/stop rules are reviewable,obtain exact access/execution permit,then formal trials and final report. Not currently an authorization-only gap."},
    ]
    matrix={"M6.1":"COMPLETE","M6.2":"IN_PROGRESS","tasks":[
        {"task":1,"status":"PARTIAL","done":"39/40 observed;all39 Task reviewed under separate bindings;Dev qrels12/14 and grouped metrics","remaining":"Formal route/retrieval/factual support objects,eligible reviewed data and actual authorized formal evidence"},
        {"task":2,"status":"LIMITED_DEV_CALIBRATION_DONE" if closure["status"]=="CLOSED_COMPLETED" else "LIMITED_DEV_CALIBRATION_STOPPED","done":"New4-object Task batch and prior content/claim targets kept separate","remaining":"Independent/dimension-level labels,coverage gaps and actual disagreement adjudication;no general reliability claim"},
        {"task":3,"status":"ENGINEERING_WIRED_FORMAL_NOT_DONE","done":"Separate formal signed issue/verify/claim/recovery/cleanup entry verified offline","remaining":"Real asset/review/governance,isolation,deployment key/target/factory,precise permit,Frozen Test"},
        {"task":4,"status":"DEV_REPORT_DONE_FORMAL_PENDING","done":"Per-binding n/N,Wilson,costs,negative outcomes and replay","remaining":"Formal results,reviewed metrics and final acceptance report"}],"intake":items,
        "not_new_gates":["All tasks PASS","Rerun39 on latest code","M2 must first PASS"],
        "formal_scope":{"actual_N":None,"actual_asset_ids":[],"case_attempts":None,"budget":None,
            "033":"Deferred as confirmed;no electrical conflict capability claim","candidate_content_read":False},
        "applicable_hard_gates":"§10.2/evaluation-spec:zero serious unauthorized read/leak/unconfirmed or duplicate write;15-tool contracts,MCP two transports,current online retrieval boundaries,memory/SSE/recovery,audit/alerts/cleanup. Bind actual applicable test evidence to chosen formal profile;M2-R supersedes historical online3hop requirement.",
        "only_precise_authorization_remaining":[],"why_empty":"Formal asset eligibility,actual reviews and deployed target/issuer inputs remain;do not disguise them as mere permission."}
    save(pkg/"formal-freeze-intake-and-exit-matrix.json",matrix)
    lines=["# R23 五项决定落地、有限Judge及正式执行接线", "",
        "M6.1 COMPLETE；M6.2 IN_PROGRESS。本轮没有真实Agent/application Run、Embedding、业务工具或业务写入；仅授权Judge清单的请求。旧应用Run授权与27次共享消费记录不改、不重新打开。", "",
        "## 已确认七案", "", "| Case | Task |", "|---|---|"]
    lines += [f"| {cid} | {decision} |" for cid,decision in TASKS.items()]
    lines += ["",f"确认事件{EVENT}，Actor CHAT_USER_SELF_CONFIRMED，实际归档时间见事件；AI辅助用户采纳，仅对应R22精确Run Task，不替代独立Gold。原32事件不重复。R21旧1P/6F、R22改判依据和未交付历史保留；新2P/1Partial/4F。各组指标不混合。", "",
        "## 修订Dev候选相关性", "", "24相关/144不相关/28未知；12/14查询可评。四格修改有query_id/chunk_id/来源绑定，其他明确查询按本消息采纳。未知28格不从expected反推，017/018若补指代须新实验。相关不证明适用于订单，也不等于正式必需证据。", "",
        "| 指标 | 新值 | 原AI口径 | 可评查询/全部 |", "|---|---|---|---|"]
    for name,value in q["new"]["means"].items():
        lines.append(f"| {name} | {value['value']:.6f} | {q['old_AI_only']['means'][name]['value']:.6f} | {value['n']}/{value['N']} |")
    lines += ["", "P@1=8/12，Wilson95%39.06%–86.19%；其他宏平均非二项比例，不硬套Wilson。MRR为截断MRR@5；新Recall@5=.756944，不沿用旧.777778。MRR@5重新计算后数值恰好不变。逐查询全部结果在confirmed-retrieval-metrics.json。", "",
        "## 本轮Judge实际结果", "", "| 对象 | 参考Task | Judge | 状态 |", "|---|---|---|---|"]
    for row in analysis["rows"]: lines.append(f"| {row['sample_id']} | {row['reference']} | {row.get('judge') or 'N/A'} | {row['status']} |")
    lines += ["",f"一致率{analysis['agreement']['n']}/{analysis['agreement']['N']}；误放行(PASS on FAIL){analysis['false_accept_PASS_on_FAIL']['n']}/{analysis['false_accept_PASS_on_FAIL']['N']}；误拒(FAIL on PASS){analysis['false_reject_FAIL_on_PASS']['n']}/{analysis['false_reject_FAIL_on_PASS']['N']}。各区间/混淆矩阵见judge-analysis.json，PARTIAL独立；维度无人工标签则一致率N/A。", "",
        f"授权{closure['status']}；尝试{closure['provider_requests_attempted']}，已知tokens {closure['settled_tokens']}，本地峰值保守估算USD{closure['local_estimated_usd']}，usage_unknown={closure['usage_unknown']}。发票UNKNOWN；不是供应商硬封顶。新独立Judge账本关联既有财务记录，未重置旧账。", "",
        "实际messages含完整schema、目标和允许来源，人工标签/AI建议未发送；thinking disabled、max_tokens2048、重试0、并发1、单请求<=120秒/批<=600秒、token<=48000/USD1。六代理变量与HTTPX实际均127.0.0.1:7892，NO_PROXY清空且TLS开启。首样本通过格式/引用/usage结算才继续；没有独立探针。只是一轮4样本开发检查，同模型及重复Dev参考偏差保留，不能与旧6/8、1/2不同批次/维度混算。", "",
        "## 正式执行代码与离线验收", "",f"最终受影响完整文件{tests['tests']} passed，failures/errors/skipped={tests['failures']}/{tests['errors']}/{tests['skipped']}。初轮失败和修复日志保留。隔离测试资产不是真实Frozen或真实审核记录。", "",
        "复用grants.py的HMAC机制，但正式Grant/Plan为不同类型、不同签名kind；Registry ELIGIBLE条件不放宽。签发必须有信任issuer/key、已批准精确plan hash和目标allowlist。签名绑定模式/资产/精确case和顺序、实现与配置、主体/实际目标、时限/预算和固定账本路径。approval_ref持久唯一，撤销/关闭/claim/settle追加记录；换目录/进程不复位。", "",
        "现有run_m6_1_b_synthetic.py新增受控formal-config入口；原合成入口不变。实际formal入口在读取资产前验证签名/主体/目标/配置，由绑定工厂核验真实主体和目标；case claim在Fixture/Run之前持久化。复用ResultStore、BudgetLedger、CodeGrader/EvalTrial和ShopRelay API收集/清理。完整CLI测试覆盖质量FAIL后继续、清理失败停止和关闭后拒绝；重启后已终态原档只清理收口、不执行，未知中断保留现场。模型子请求由已绑定工厂复用实际应用预算适配器按步骤预留，接收剩余deadline，不能由未审工厂跳过。", "",
        "尚无正式生产签发者/key/执行主体/目标工厂/合格资产，未启用正式执行。可填写模板formal-deployment-input-template.json；不得拿测试TEST_ONLY身份或合成Grant改名。", "",
        "## 正式资产与冻结准入", "",
        f"仅盘点获准登记元数据：{inventory['registered_asset_count']}个资产记录；policy-candidate-family登记60案，不存在获准的60条逐题ID清单。现有合格正式资产0。没有打开、搜索、复制、散列受保护题目内容。Registry canonical metadata hash与文件字节hash分别保存，旧overlay引用前者，不误报为损坏。", "",
        "已确认所有者决定M6.1-A-OWNER-DISPOSITION-20261024-01：受影响家族隔离/不可用于独立Frozen/需要替换；其他资产暴露未知不一概认定已暴露。40案仍Dev，033延期39/40，不悄悄删除。R6被覆盖的离线报告未恢复，真实Run单独作证。", ""]
    for item in items: lines.append(f"- {item['item']}：{item['status']}。{item['evidence']} "+item.get('remaining',''))
    lines += ["", "最短后续顺序：记录真实Reviewer与独立初标→真实后续日期盲审/分歧处理→按既有决定准备未用于调参的替换资产元数据/暴露证据→实际保护存储/ACL/访问审计与目标恢复验证→填写可信issuer/主体/批准plan和固定账本/工厂→提出具体资产范围、N、预算及停止规则的精确访问/执行许可→正式Frozen Test和最终报告。不是再次请求本次七案或qrels确认；不默认重跑Dev。033如拟纳入仍需该案真实专案审核。", "",
        "M6.2四任务见唯一formal-freeze-intake-and-exit-matrix.json；本轮未正式Gold批准、未解封或执行Frozen，未改变M2/正式Gate。无全案PASS或M2必须先PASS的新条件。", ""]
    path=pkg/"closeout-r23.md";text="\n".join(lines)
    if path.exists() and path.read_text(encoding='utf8')!=text: raise ValueError("FINAL_CLOSEOUT_DRIFT")
    if not path.exists(): path.write_text(text,encoding='utf8')
    return {"tests":tests,"judge_status":closure["status"],"agreement":analysis["agreement"],"M6.2":"IN_PROGRESS"}
