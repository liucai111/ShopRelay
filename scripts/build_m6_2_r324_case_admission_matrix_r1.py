"""Build the version-bound r3.2.4 40-case personal Dev admission ledger.

This is a deterministic projector over the authorized Dev dataset and the
receipt-scoped Repository audit. It never reads Candidate/Frozen assets and
never executes a Run or calls a model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from app.m0.config import load_settings
from app.m1.chat import ANSWER_PROMPT_VERSION
from app.m1.routing import PROMPT_VERSION, ROUTE_VERSION
from eval.m6_1.personal_dev import implementation_digest


ROOT = Path(__file__).resolve().parents[1]
PKG = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029")
DATA = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
DATA_SHA = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
REPO_AUDIT = PKG / "repository-admission-r4.json"
CONFIRMED = Path("docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmed-decisions-r1.json")
CONFIRM_EVENT = Path("docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmation-event-r1.json")

STATUS = {
    "D1-POLICY-015": ("BLOCKED_CONTRACT_OR_DATA", "No coupon_code or catalog association can be derived from the user input; do not guess one.", "m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md"),
    "D1-POLICY-016": ("BLOCKED_CONTRACT_OR_DATA", "The allowed warranty lookup needs a unique product_id, but neither the query nor an allowed upstream order-item result supplies one.", "m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md"),
    "D1-CROSSAGENT-029": ("BLOCKED_CONTRACT_OR_DATA", "No product identifier is available from the query or an allowed upstream result; existing capability scope is not expanded.", "m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md; user-confirmed D1-029 hold"),
    "D1-PRODUCT-005": ("BLOCKED_ENGINEERING", "The current deterministic route returns PRODUCT_SEARCH_NOT_ALLOWED for a product-plus-policy comparison when only get_product_detail and search_knowledge are allowed; the real repository can resolve the named item, but the application route cannot currently express the combined path. A clarification is safe but does not meet the stated outcome.", "m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md; repository-admission-r3.json"),
    "D1-AFTERSALES-024": ("UNSCORABLE", "The read-only case can expose stored facts and preserve unknown payment facts, but whether paid_at is a universal return-eligibility prerequisite remains an unresolved business decision; do not convert current code into a universal rule.", "docs/progress.md (M6.1 D1 business-decision history); m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md"),
    "D1-ORDER-008": ("HELD_REVIEW_OR_AUTHORIZATION", "Existing Shipment steward disposition recommends deferring this case pending supported source/disposition; preserve the empty Shipment fixture as UNKNOWN.", "docs/eval/m6.1-d1-r3.2.2-review-bundle/m6.1-d1-shipment-steward-decisions-r3.2.2.md"),
    "D1-ORDER-010": ("HELD_REVIEW_OR_AUTHORIZATION", "Existing Shipment steward disposition recommends deferring this case pending supported source/disposition; preserve the empty Shipment fixture as UNKNOWN.", "docs/eval/m6.1-d1-r3.2.2-review-bundle/m6.1-d1-shipment-steward-decisions-r3.2.2.md"),
    "D1-AFTERSALES-022": ("HELD_REVIEW_OR_AUTHORIZATION", "Existing Shipment steward disposition recommends deferring this case pending supported source/disposition; preserve the empty Shipment fixture as UNKNOWN.", "docs/eval/m6.1-d1-r3.2.2-review-bundle/m6.1-d1-shipment-steward-decisions-r3.2.2.md"),
    "D1-CROSSAGENT-033": ("HELD_REVIEW_OR_AUTHORIZATION", "Case-specific safety hold remains: genuine independent electrical-safety reviewer and independent business/policy reviewer signatures are required before this high-risk conflict case can run.", "m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md; prior explicit D1-CROSSAGENT-033 user instruction"),
}


def sha(path: Path) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def read(path: Path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def build() -> dict:
    data_bytes = (ROOT / DATA).read_bytes()
    if hashlib.sha256(data_bytes).hexdigest() != DATA_SHA:
        raise ValueError("R324_DATASET_SHA_MISMATCH")
    bundle = json.loads(data_bytes.decode("utf-8"))
    audit = read(REPO_AUDIT)
    if audit.get("dataset", {}).get("sha256") != DATA_SHA or audit.get("database", {}).get("lease_or_receipt_after"):
        raise ValueError("REPOSITORY_AUDIT_BINDING_OR_CLEANUP_INVALID")
    if audit.get("limits", {}).get("provider_requests") != 0 or audit.get("limits", {}).get("agent_runs") != 0:
        raise ValueError("OFFLINE_AUDIT_CONTAINS_EXTERNAL_EXECUTION")
    confirmed = read(CONFIRMED)
    confirmed_ids = {item.get("item_id") for item in confirmed.get("items", [])
                     if item.get("item_id", "").startswith("D1-")}
    event = read(CONFIRM_EVENT)
    event_id = event.get("event_id")
    if confirmed.get("confirmation_event_id") != event_id or not event_id:
        raise ValueError("CONFIRMED_REFERENCE_EVENT_BINDING_MISMATCH")
    audit_cases = {row["case_id"]: row for row in audit.get("cases", [])}
    if len(bundle.get("cases", [])) != 40 or len(audit_cases) != 40:
        raise ValueError("CASE_COUNT_NOT_40")
    rows = []
    for case in bundle["cases"]:
        cid = case["case_id"]
        repo = audit_cases.get(cid)
        if not repo or repo.get("group_id") != case.get("group_id") or repo.get("query") != case.get("input", {}).get("user_query"):
            raise ValueError(f"CASE_SOURCE_BINDING_MISMATCH:{cid}")
        status, reason, source = STATUS.get(cid, ("READY", None, "r3.2.4 dataset + repository-admission-r3.json"))
        if status == "READY" and (repo.get("readiness") != "READY" or repo.get("cleanup") not in {"CLEANED_AND_RELEASED", "NO_FIXTURE_RECEIPT_REQUIRED"}):
            raise ValueError(f"READY_CASE_REPOSITORY_OR_CLEANUP_NOT_PASS:{cid}")
        tool_ids = repo.get("allowed_capabilities", [])
        if any(row.get("mode") != "read_only" for row in case.get("input", {}).get("expected_tool_calls", [])):
            raise ValueError(f"NON_READ_ONLY_CASE:{cid}")
        if cid in {"D1-PRODUCT-003", "D1-POLICY-017", "D1-POLICY-018", "D1-SAFETY-036", "D1-SAFETY-037", "D1-CROSSAGENT-030"}:
            review_scope = "USER_CONFIRMED_DEV_OUTCOME_ONLY"
        elif cid in confirmed_ids:
            review_scope = "USER_CONFIRMED_DEV_REVIEW_EVENT_BOUND"
        else:
            review_scope = "NO_CASE_SPECIFIC_CONFIRMATION_IN_BOUND_EVENT"
        nodes = []
        if any(t in tool_ids for t in ("search_products", "get_product_detail", "recommend_products", "check_inventory")):
            nodes.append("ProductAgent/product read adapter (not executed in this audit)")
        if any(t in tool_ids for t in ("get_order", "get_order_items", "track_shipment", "get_refund_status")):
            nodes.append("Order/AfterSales read adapter (not executed in this audit)")
        if any(t in tool_ids for t in ("search_knowledge", "get_warranty", "get_coupon")):
            nodes.append("Policy/BM25 adapter (not executed in this audit)")
        if not nodes:
            nodes.append("DIRECT/REFUSE response path; no business tool expected")
        rows.append({
            "case_id": cid, "group_id": case.get("group_id"), "split": case.get("split"),
            "query": case["input"]["user_query"], "scenario": case["expected_outcome"]["outcome_type"],
            "fixture_ref": case.get("fixture_ref"), "fixture_refs": case["input"].get("fixture_refs", []),
            "owner_alias": repo.get("owner_alias"), "mapped_owner_id": repo.get("owner_id"),
            "allowed_read_only_capabilities": tool_ids,
            "tool_parameter_sources_and_repository_evidence": repo.get("repository_checks", []),
            "repository_read_count_preflight_only": repo.get("repository_reads"),
            "expected_business_result": case["expected_outcome"].get("business_outcome"),
            "hard_gates": case["expected_outcome"].get("hard_gates", []),
            "semantic_requirements": case["expected_outcome"].get("semantic_quality_rubric", {}),
            "task_success_predicate": case["expected_outcome"].get("task_success_predicate"),
            "declared_expected_route": case["input"].get("expected_strategy"),
            "deterministic_route_baseline_without_classifier": repo.get("rule_only_route"),
            "deterministic_route_is_not_llm_evidence": True,
            "expected_nodes_not_yet_verified": nodes,
            "review_status_in_source": case["input"].get("review_status"),
            "review_event_scope": review_scope,
            "batch_authorization_scope": "CURRENT_USER_MESSAGE_EXPLICITLY_AUTHORIZES_PERSONAL_DEV_FOR_40_CASE_DATASET; NO NEW AUTH FILE CREATED",
            "admission_status": status, "admission_reason": reason,
            "blocking_source": source if reason else None,
            "fixture_repository_status": repo.get("readiness"),
            "fixture_receipt_cleanup": repo.get("cleanup"),
            "trial_status": "NOT_RUN",
            "trial_scoreability": "NOT_GRADED_NO_NEW_TRIAL; semantic rubric remains provisional and no formal human score is inferred",
            "technical_requirements_for_trial": ["new exact-scope M6.2 runner/profile", "durable per-case attempt and budget ledger", "per-case diagnostic checks and independent recompute", "runtime route scope check", "safe terminal cleanup"],
        })
    counts = {s: sum(r["admission_status"] == s for r in rows) for s in
              ("READY", "BLOCKED_ENGINEERING", "BLOCKED_CONTRACT_OR_DATA", "HELD_REVIEW_OR_AUTHORIZATION", "UNSCORABLE")}
    eligible = [r["case_id"] for r in rows if r["admission_status"] == "READY"]
    if sum(counts.values()) != 40 or len(eligible) != 31:
        raise ValueError(f"ADMISSION_CONSERVATION_OR_EXPECTED_SCOPE_MISMATCH:{counts}")
    settings = load_settings(require_api_key=False)
    api_key_present = bool(settings.api_key.strip())
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                                  capture_output=True, text=True).stdout.strip()
    except Exception:
        revision = "UNKNOWN"
    return {
        "schema_version": "m6.2-r324-all40-substantive-admission-r2",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "classification": "PERSONAL_DEV_ADMISSION_LEDGER_NOT_AN_EXECUTION_OR_GRADE",
        "dataset": {"path": DATA.as_posix(), "version": bundle["version"], "sha256": DATA_SHA,
                    "case_count": 40, "stable_order_and_group_ids_preserved": True},
        "bindings": {"implementation_digest": implementation_digest(ROOT), "git_head_revision": revision,
            "working_tree_note": "Existing working-tree changes were observed; this revision is only HEAD and does not represent a clean working tree.",
            "route_version": ROUTE_VERSION, "route_prompt_version": PROMPT_VERSION,
            "answer_prompt_version": ANSWER_PROMPT_VERSION,
            "model": settings.model, "provider_endpoint": settings.base_url,
            "api_key_configured": api_key_present, "api_key_value_recorded": False,
            "database_identity": audit["database"]["identity"],
            "database_profile": audit["database"]["profile"],
            "pricing_evidence_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/official-pricing-evidence-r1.json"},
        "repository_audit": {"path": REPO_AUDIT.as_posix(), "sha256": sha(REPO_AUDIT),
            "total_repository_reads": audit["limits"]["actual_repository_reads"],
            "per_case_cap": audit["limits"]["per_case_reads"], "batch_cap": audit["limits"]["total_reads"],
            "post_probe_rows": audit["database"]["post_probe_rows"],
            "lease_or_receipt_after": audit["database"]["lease_or_receipt_after"],
            "agent_runs": 0, "provider_requests": 0},
        "review_binding": {"confirmation_event_id": event_id, "confirmation_event_sha256": sha(CONFIRM_EVENT),
            "confirmed_decisions_sha256": sha(CONFIRMED),
            "scope_note": "The bound event covers only its six case outcomes plus J3 and claim-04a rationale handling; it is not a 40-case review, Gold, independent blind review, or formal approval."},
        "admission_counts": counts, "eligible_case_ids": eligible,
        "execution_status": "BLOCKED_BEFORE_AUTHORIZATION",
        "batch_execution_blockers": [
            {"code": "NO_EXACT_SUBSET_RUNNER", "evidence": "scripts/run_m6_1_d1_personal_dev.py --execute-batch routes to execute_personal_batch, which hard-requires exactly 40 cases and closed-batch carryforward; it cannot execute only the 31 admitted cases."},
            {"code": "BUDGET_PROFILE_MISMATCH", "evidence": "Current batch validator hardcodes 4 provider/4 tool calls per case and batch 160/160; the authorized task requires a new frozen profile up to 6 provider and 8 tool calls per case and a durable distinct batch ledger."},
            {"code": "NO_GENERIC_CASE_OUTCOME_GRADER", "evidence": "Current execute_personal_batch leaves task outcome and quality UNKNOWN_NO_REAL_D1_GRADER; existing M6.2 scoring is bound to reviewed historical references and does not score all r3.2.4 cases from new archives."},
            {"code": "CASE_SPECIFIC_PRETRIAL_CLASSIFICATION", "evidence": "The execution path stops the batch when the first case does not start an Agent; legitimate DIRECT/REFUSE and quality failures must be distinguished from technical stop conditions under this task."},
        ],
        "limits_for_future_exact_eligible_subset": {"case_count": len(eligible),
            "per_case": {"provider_calls": 6, "read_only_tool_calls": 8, "total_tokens": 120000,
                "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1,
                "automatic_retries": 0, "judge": 0, "embedding": 0, "writes": 0, "pending_actions": 0},
            "batch": {"provider_calls_max": min(240, 6 * len(eligible)),
                "total_tokens_max": min(4800000, 120000 * len(eligible)),
                "estimated_usd_max": round(min(7.20, 0.18 * len(eligible)), 2),
                "max_run_seconds": 7200, "preflight_repository_reads_actual": audit["limits"]["actual_repository_reads"],
                "preflight_repository_reads_cap": 320},
            "first_model_needed_case": "D1-POLICY-013", "proposed_order": ["D1-POLICY-013"] +
                [cid for cid in eligible if cid != "D1-POLICY-013"],
            "note": "This is a proposed exact subset schedule only; no authorization, reservation, Run or provider request was created."},
        "cases": rows,
        "limitations": ["No current LLM route or answer was executed; deterministic route rows are rule-only baselines.",
            "Repository success proves only that synthetic facts are available through the adapters, not that the model can call or answer correctly.",
            "No real task score exists because there was no new output and no generic archive-based 40-case Outcome grader.",
            "Formal Gold/M2/Registry/Frozen states are out of scope and unchanged."],
    }


def main() -> None:
    out = ROOT / PKG / "all40-case-admission-matrix-r2.json"
    if out.exists():
        raise SystemExit("OUTPUT_ALREADY_EXISTS")
    result = build()
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"output": out.relative_to(ROOT).as_posix(), "sha256": sha(PKG / out.name),
        "counts": result["admission_counts"], "ready": len(result["eligible_case_ids"]),
        "execution_status": result["execution_status"], "implementation_digest": result["bindings"]["implementation_digest"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
