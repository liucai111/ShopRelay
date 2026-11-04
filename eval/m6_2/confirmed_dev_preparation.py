"""Apply an explicit chat confirmation to Dev artifacts; never execute a trial."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from eval.m6_2.dev_review_materials import DATA, DATA_SHA, EvidenceReader, stable_hash
from eval.m6_1.personal_dev import plan_case
from scripts.recompute_m6_2_r9_policy018_closeout import wilson

OUT = Path("docs/eval/m6.2-r19-confirmed-preparation-20261031")
R19 = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030/reports/r19-20261031")
NEW_DATA = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.5.json")
EVENT_ID = "M62-R19-AB-CHAT-USER-CONFIRMED-20261031-R1"
READY_CASES = ("D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022", "D1-POLICY-015",
               "D1-POLICY-016", "D1-CROSSAGENT-029", "D1-AFTERSALES-024")


def revised_dev_bundle(original: dict) -> dict:
    """Apply the approved case-contract edits, preserving business fixture bytes."""
    data = deepcopy(original)
    data["version"] = "m6.1-d1-dev-draft-r3.2.5"
    data["created_at_utc"] = "2026-10-31T05:23:48Z"
    data["derived_from"] = {"path": DATA.as_posix(), "sha256": DATA_SHA,
                            "decision_event": EVENT_ID}
    data["limitations"] = [text for text in data["limitations"]
                           if not text.startswith("Derived from the historical r3.2.3")]
    data["limitations"].append(
        "R19 A/B approved only for personal Dev. Dataset contract edits do not grant execution, "
        "independent review, Gold or Frozen approval. Historical Run scores stay on their original dataset.")
    cases = {case["case_id"]: case for case in data["cases"]}
    for case in cases.values():
        case["data_version"] = data["version"]
    coupon = cases["D1-POLICY-015"]
    coupon["input"]["expected_tool_calls"] = []
    coupon["input"]["acceptable_evidence_ids"] = []
    outcome = coupon["expected_outcome"]
    outcome["business_outcome"] = (
        "Ask for an explicit coupon code. Current validity, entitlement and discount are UNKNOWN. "
        "Make no lookup in this missing-code turn and assert no unobserved catalog facts.")
    outcome["semantic_quality_rubric"]["must_include"] = [
        "Ask for coupon_code; current validity/entitlement/discount is UNKNOWN; do not assert catalog facts without lookup."]
    outcome["seven_dimension_trace"]["retrieval"] = {"required_evidence_ids": [], "strategy": "NOT_APPLICABLE"}
    outcome["seven_dimension_trace"]["tool_agent_outcome"]["conditional_execution"] = {
        "capability": "get_coupon", "requires": "explicit unambiguous coupon_code from a subsequent user input",
        "current_turn_expected_calls": 0, "subsequent_turn_authorized": False}
    warranty = cases["D1-POLICY-016"]
    warranty["input"]["expected_tool_calls"] = [{"tool_id": "search_knowledge", "mode": "read_only"}]
    warranty["expected_outcome"]["seven_dimension_trace"]["tool_agent_outcome"]["allowed_tool_ids"] = ["search_knowledge"]
    warranty["expected_outcome"]["business_outcome"] = (
        "Retrieve general fictional warranty evidence, report its term and actual source version, "
        "request purchase/activation facts, and keep personal eligibility UNKNOWN.")
    cases["D1-CROSSAGENT-029"]["input"]["user_query"] = (
        "The fictional product Fictional Lamp 113 has an untimestamped stock snapshot; "
        "can policy promise delivery tomorrow?")
    # 024's approved clarification is already consistent with its original business_outcome.
    # Preserve the original task and action guards; record the scope interpretation separately.
    return data


def build_confirmed_preparation(root: Path) -> None:
    reader = EvidenceReader(root)
    event = reader.read(OUT / "confirmation-event-r1.json")
    if event["event_id"] != EVENT_ID or event["authorization"]["real_execution"] is not False:
        raise ValueError("EXACT_AB_CONFIRMATION_WITH_NO_EXECUTION_REQUIRED")
    package = reader.read(R19 / "adjudication-package-r19.json", event["source_package_sha256"])
    original = reader.read(DATA, DATA_SHA)
    metrics_source = reader.read(R19 / "metrics-r19.json")
    rows = package["rows"]
    if len(rows) != 32 or len({r["run_id"] for r in rows}) != 32:
        raise ValueError("EXPECTED_32_EXACT_RUNS")
    decisions = []
    for row in rows:
        reader.read(row["archive_path"], row["archive_sha256"])
        decisions.append({key: deepcopy(row[key]) for key in (
            "review_id", "case_id", "run_id", "binding", "archive_path", "archive_sha256",
            "answer_sha256", "proposal_reason", "continuation_compliance")})
        decisions[-1].update({"decision": row["task_outcome_proposal"], "scope": "TASK_OUTCOME_ONLY",
            "actor": event["actor"], "method": "AI_ASSISTED_REVIEW_USER_ADOPTED",
            "confirmation_event_id": EVENT_ID, "recorded_at_utc": event["recorded_at_utc"],
            "independent_review": False, "other_dimensions_confirmed": False})
    by_case = {r["case_id"]: r for r in decisions}
    groups = {}
    for key, group in metrics_source["groups"].items():
        members = [by_case[c] for c in group["case_ids"]]
        eligible = [r for r in members if r["continuation_compliance"] != "POST_LOCKED_STOP_DEVIATION"]
        def counts(items):
            labels = dict(Counter(r["decision"] for r in items))
            return {"counts": labels, "pass_n": labels.get("PASS", 0), "n": len(items),
                    "wilson95": wilson(labels.get("PASS", 0), len(items))}
        groups[key] = {"binding": group["binding"], "case_ids": group["case_ids"],
                       "all_observations": counts(members), "compliant_sequence": counts(eligible)}
    revised = revised_dev_bundle(original)
    # Parse the actual schema and validate every case/version, not a mirror schema.
    for case in revised["cases"]:
        plan_case(revised, case["case_id"])
    assert revised["fixtures"] == original["fixtures"]
    dispositions = []
    for item in package["remaining_eight"]:
        postponed = item["case_id"] == "D1-CROSSAGENT-033"
        dispositions.append({"case_id": item["case_id"], "decision_id": item["decision_id"],
            "decision": "DEFER_KEEP_COVERAGE_GAP" if postponed else "ADOPT_RECOMMENDATION",
            "recommendation": item["recommendation"], "confirmation_event_id": EVENT_ID,
            "actor": event["actor"], "role_scope": "PROJECT_DEV_DATA_AND_BUSINESS_DISPOSITION",
            "specialist_signature": False, "real_execution_authorized": False,
            "status": "DEFERRED_NOT_RUN" if postponed else "APPROVED_FOR_OFFLINE_PREPARATION",
            "paid_at_scope": "024 read-only interpretation only; action guards unchanged" if item["case_id"] == "D1-AFTERSALES-024" else None})
    changes = []
    old_cases = {c["case_id"]: c for c in original["cases"]}
    for case in revised["cases"]:
        before, after = deepcopy(old_cases[case["case_id"]]), deepcopy(case)
        before.pop("data_version"); after.pop("data_version")
        if before != after:
            changes.append({"case_id": case["case_id"], "before": before, "after": after})
    outputs = {
        "confirmed-decisions-r1.json": {"event_id": EVENT_ID, "items": decisions,
            "descriptive_counts_not_success_rate": dict(Counter(r["decision"] for r in decisions)),
            "grading_scope": "USER_CONFIRMED_PERSONAL_DEV_NOT_FORMAL_GOLD"},
        "confirmed-dispositions-r1.json": {"event_id": EVENT_ID, "items": dispositions},
        "confirmed-task-metrics-r1.json": {"event_id": EVENT_ID, "groups": groups,
            "program_metrics_source": (R19 / "metrics-r19.json").as_posix(),
            "program_metrics_not_relabelled_as_human": True, "unified_success_rate": None},
        "contract-changes-r1.json": {"source": DATA.as_posix(), "target": NEW_DATA.as_posix(),
            "all_case_data_versions_updated": True, "fixtures_unchanged": True, "changes": changes,
            "after_sales_024": "Scope interpretation confirmed; original read-only contract retained",
            "other_37_case_contracts_unchanged": True},
        "execution-preparation-r1.json": {"schema_version": "m62-preparation-no-execution-v1",
            "event_id": EVENT_ID, "dataset_path": NEW_DATA.as_posix(),
            "dataset_semantic_sha256": stable_hash(revised), "proposed_case_ids": list(READY_CASES),
            "deferred_case_ids": ["D1-CROSSAGENT-033"], "existing_runs": 32,
            "new_runs": 0, "execution_authorized": False, "judge_authorized": False,
            "authorization_token": None, "proposed_new_run_limit": 7,
            "budget_status": "PROPOSED_ONLY_NOT_A_GRANT",
            "proposed_shared_limits": {"application_runs": 27, "provider_requests": 600,
                "tokens": 10000000, "local_estimated_usd": 30, "runtime_seconds": 14400},
            "previous_shared_runs": 20, "previous_closed_ledger_unchanged": True,
            "proxy": "http://127.0.0.1:7892", "concurrency": 1,
            "remaining_before_real_execution": [
                "Complete final application/CLI admission with the revised contract, including coupon zero-call clarification",
                "Bind current implementation, case allowlists, runtime target and fresh pricing evidence",
                "Obtain exact seven-case execution authorization; preserve old ledgers and consumed attempts"],
            "coupon_boundary": "Current expected reads=[]; conditional get_coupon is not permission for a future turn",
            "formal_gates_changed": False},
    }
    def write(path, value):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if path == NEW_DATA and target.exists() and target.read_text(encoding="utf8") != content:
            raise ValueError("NEW_DATASET_PATH_ALREADY_HAS_DIFFERENT_CONTENT")
        target.write_text(content, encoding="utf8")
    write(NEW_DATA, revised)
    for name, value in outputs.items():
        write(OUT / name, value)
    write(OUT / "source-bindings-r1.json", reader.sources)
    print(json.dumps({"confirmed_runs": len(decisions), "dispositions": len(dispositions),
                      "proposed_first_runs": len(READY_CASES), "real_execution_authorized": False}))
