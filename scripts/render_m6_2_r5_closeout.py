"""Render the R5 M6.2 Dev batch closeout from bound evidence files."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import sys

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029"
sys.path.insert(0, str(ROOT))


def read(name: str) -> dict:
    return json.loads((PKG / name).read_text(encoding="utf-8"))


def main() -> None:
    app = read("application-admission-offline-r1.json")
    repo = read("application-admission-r5.json")
    results = read("reports/case-results-r5-r1.json")
    ledger = read("reports/40-case-coverage-ledger-r5-r1.json")
    closeout = read("reports/batch-closeout-r5-r1.json")
    consistency = read("reports/recompute-consistency-r5-r1.json")
    state = read("execution-state-r5.json")
    auth = read("authorization-r5.json")

    # Recheck the dedicated personal DB after closeout using the same read-only
    # idle assertion as the runner. This is not a cleanup operation.
    from run_m6_2_r324_personal_dev_batch_r5 import _assert_idle_personal_database
    from eval.m6_1.personal_dev_database import PersonalDevDatabase
    target = PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    try:
        db_idle = _assert_idle_personal_database(factory, target)
    finally:
        engine.dispose()
    journal_lock = PKG / "execution-journal-r5.jsonl.lock"
    post_run = {
        "schema_version": "m6.2-r324-r5-post-run-safety-r1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "database": db_idle,
        "authorization_status": auth.get("status"),
        "active_budget_reservations": closeout["r5_actuals"]["active_reservations"],
        "budget_settled": closeout["r5_actuals"]["budget_snapshot"]["settled"],
        "journal_lock_present": journal_lock.exists(),
        "all_five_receipts_cleaned": all(
            (r.get("cleanup") or {}).get("cleanup_status") == "CLEANED_AND_RELEASED"
            and (r.get("cleanup") or {}).get("pending_actions") == 0
            and (r.get("cleanup") or {}).get("residual_case_rows") == 0
            for r in results["cases"]
        ),
    }
    (PKG / "reports/post-run-safety-r5-r1.json").write_text(
        json.dumps(post_run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    final_closeout = {
        "schema_version": "m6.2-r324-r5-final-closeout-r1",
        "classification": closeout["classification"],
        "grading_status": "NOT_GRADED",
        "reference_status": "reference_unreviewed",
        "batch_id": closeout["batch_id"],
        "authorization_id": auth.get("authorization_id"),
        "authorization_final_status": auth.get("status"),
        "dataset_sha256": closeout["dataset_sha256"],
        "implementation_digest": closeout["implementation_digest"],
        "final_manifest_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/batch-manifest-r5-final8.json",
        "final_rules_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/locked-rules-r5-final8.json",
        "source_ready_application_admission": {
            "candidate_count": app["case_count"],
            "application_ready": app["application_ready_count"],
            "application_blocked": app["blocked_count"],
            "external_calls": app["external_calls"],
            "intercepted_provider_requests": app["provider_requests"],
            "final_repository_preflight_cases": len(repo["case_ids"]),
            "final_repository_preflight_reads": repo["read_only_preflight_calls"],
            "final_admission": repo["admission"],
        },
        "execution": {
            "planned": closeout["planned_case_count"],
            "executed": closeout["executed_case_count"],
            "not_run_after_critical_stop": closeout["not_run_selected_count"],
            "stop_case": closeout["stop"]["case_id"],
            "stop_reason": closeout["stop"]["reason"],
            "case_results_path": closeout["case_results_path"],
        },
        "usage": closeout["r5_actuals"],
        "coverage_40_case": closeout["original_40_case_conservation"],
        "historical_r4_excluded_from_r5": closeout["r4_policy013_separate_historical_record"],
        "post_run_safety_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/post-run-safety-r5-r1.json",
        "recompute_consistency": consistency,
        "targeted_test": {
            "test_files": [
                "tests/test_m6_2_personal_dev_batch_r1.py",
                "tests/test_m3_3_product_agent.py",
                "tests/test_m3_2_order_logistics.py",
                "tests/test_m3_4_policy_agent.py",
                "tests/test_m3_5_after_sales.py",
                "tests/test_langgraph_g2.py",
            ],
            "invocation_note": "Project virtualenv; pytest.main over the listed complete test files with a project-local temporary-path plugin to avoid the observed Windows temp-directory ACL failure. No ACL changes.",
            "passed": 76,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "junit_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/test-targeted-final-20261029-173300.xml",
            "log_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/test-targeted-final-20261029-173300.log",
        },
        "formal_gates_changed": False,
        "candidate_frozen_access": False,
        "human_review": "AI-assisted prescreen only; no human confirmation recorded in this batch",
    }
    (PKG / "reports/closeout-r5-final.json").write_text(
        json.dumps(final_closeout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    lines = [
        "# M6.2 r3.2.4 remaining READY cases — R5 closeout",
        "",
        "Classification: `PERSONAL_DEV_AGENT_EVAL` / `PROVISIONAL_DEV_CHECKS` / `reference_unreviewed` / `NOT_GRADED`.",
        "This is a 40-case coverage ledger plus a stopped 5-case subset; it is not a completed 40-case evaluation, formal Smoke, Gold, Frozen, M2, or M6.2 exit result.",
        "",
        "## Scope and binding",
        "",
        f"- Batch: `{closeout['batch_id']}`; authorization `{closeout['authorization_id']}` ended `{closeout['authorization_final_status']}`.",
        f"- Dataset: `m6.1-d1-dev-draft-r3.2.4`, SHA-256 `{closeout['dataset_sha256']}`.",
        f"- Working-tree implementation digest bound before execution: `{closeout['implementation_digest']}`; Git HEAD is provenance only.",
        f"- Personal SQLite identity: `{auth['database_identity']}`. Pre-run scoped tables were empty; policy retrieval event schema was present.",
        f"- Locked final manifest/rules: `{closeout['batch_id']}` uses `batch-manifest-r5-final8.json` and `locked-rules-r5-final8.json`. Earlier final/final2/final3/final5/final6/final7 profiles are retained as superseded pre-authorization artifacts; only final8 was authorized and executed.",
        "- No Candidate/Frozen data was accessed. No Judge, Embedding, business write, Pending Action, or Git write occurred.",
        "",
        "## Application admission of all 30 remaining source-matrix READY cases",
        "",
        f"The full actual-application offline sweep processed {app['case_count']} cases. It used an intercepted model client and reports `{app['external_calls']}` external calls; `provider_requests={app['provider_requests']}` is intercepted-request activity, not live Provider traffic. It classified {app['application_ready_count']} as application-ready and {app['blocked_count']} as blocked.",
        "",
        "| Case | Application admission | Route observed in intercepted app | Result |",
        "|---|---|---|---|",
    ]
    for row in app["records"]:
        route = row.get("route") or {}
        result = row.get("reason_code") or row.get("blocked_reason")
        if not result and row["application_admission"] != "APPLICATION_READY":
            result = "actual route omitted a required allowed tool; contract/data mismatch"
        result = result or "actual app entry, scope, repository, event/reference and cleanup checks satisfied in isolated sweep"
        lines.append(f"| `{row['case_id']}` | `{row['application_admission']}` | `{route.get('strategy', 'UNKNOWN')}` / `{route.get('decision_source', 'UNKNOWN')}` | {result} |")
    lines += [
        "",
        "The two explicit app-level blockers are AFTERSALES-019 and AFTERSALES-020: the offline actual route produced `SINGLE` with `search_knowledge`, omitting the required `get_order`; the preflight did not claim those cases ready. The 30-case sweep is separate from repository-read preflight and from live Trials.",
        "",
        "The follow-up real personal-database repository preflight passed for the exact final 27-case profile: 30 bounded reads total (within 8 per case; zero-read refusal/DIRECT cases remained zero-read). CROSSAGENT-032 was excluded because it needs a product ID that cannot be derived from the user/upstream result while `search_products` is outside its allowlist. The final CLI admission was `PASS`, `run_created=false`, `provider_requests=0`, with exact dataset/code/database/manifest/rules/pricing bindings. The 27-case list excludes that blocked case and the two app-level blockers.",
        "",
        "## R5 actual execution and provisional evidence review",
        "",
        "One attempt per case was durably reserved. The first selected case was PRODUCT-002, an LLM-route path. Cases ran in the frozen manifest order. The fifth case reached the prelocked critical stop; no hot fix, retry, second batch, or further case execution occurred.",
        "",
        "| Case | Run / Trace | Question | Route, nodes, reads and evidence refs | Delivered answer | Program result | AI-assisted provisional review | Usage / cleanup |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in results["cases"]:
        route = row.get("route") or {}
        route_text = (f"{route.get('strategy')} / {route.get('decision_source')} / "
                      f"{route.get('reason_code', 'no-reason')} / route `{route.get('route_version', 'UNKNOWN')}` / "
                      f"prompt `{route.get('prompt_version', 'UNKNOWN')}`")
        observed = row.get("observed_tools") or [x.get("tool", "?") for x in (row.get("tool_references") or [])]
        tools = ", ".join(observed) or "none"
        score = row.get("program_score") or {}
        checks = score.get("program_checks", {})
        program = ", ".join(f"{k}={v}" for k, v in checks.items() if v in {"FAIL", "UNKNOWN"}) or "all applicable program checks PASS/NA"
        review = row.get("ai_semantic_review") or {}
        review_text = f"{review.get('suggestion', 'UNKNOWN')}: {review.get('reason', 'no provisional review recorded')} (unconfirmed)"
        clean = row.get("cleanup") or {}
        # In this batch each nonzero token usage is tied to one request in the
        # per-case settled ledger; the route may show FALLBACK after that call.
        provider_count = 1 if (row.get("token_usage") or 0) > 0 else 0
        usage = f"P{provider_count} / T{row.get('token_usage')} / ${row.get('local_estimated_usd'):.7f}; reads {row.get('business_read_calls')}; {clean.get('cleanup_status', 'UNKNOWN')}"
        answer = (row.get("delivered_answer") or "MISSING").replace("|", "\\|").replace("\n", " ")
        question = (row.get("question") or "MISSING").replace("|", "\\|").replace("\n", " ")
        refs = ", ".join(f"{x.get('tool')}:{x.get('result_ref')} ({x.get('source')})" for x in (row.get("tool_references") or [])) or "none"
        nodes = ", ".join(sorted({x.get("node", "?") for x in (row.get("node_and_tool_events") or []) if x.get("node")})) or "none"
        lines.append(f"| `{row['case_id']}` | `{row.get('run_id')}` / `{row.get('trace_id')}` | {question} | {route_text}; nodes `{nodes}`; tools `{tools}`; refs {refs} | {answer} | {program}; task `{score.get('task_outcome', 'UNKNOWN')}` | {review_text} | {usage} |")

    lines += [
        "",
        "## User review checklist",
        "",
        "The following are AI-assisted recommendations only. No reviewer, decision time, or human decision has been recorded for this R5 batch. Confirm, revise, or leave each item unconfirmed based on the linked raw archive and same-Run evidence.",
        "",
        "| Case | AI recommendation | Review point | Human decision |",
        "|---|---|---|---|",
    ]
    for row in results["cases"]:
        review = row.get("ai_semantic_review") or {}
        reason = (review.get("reason") or "Review the raw archive and sources.").replace("|", "\\|")
        lines.append(f"| `{row['case_id']}` | `{review.get('suggestion', 'UNKNOWN')}` | {reason} | `PENDING_USER_REVIEW` |")

    lines += [
        "",
        "### Stop analysis",
        "",
        "PRODUCT-006 is a technical route/provider-contract failure, not a factual claim failure. A model request was attempted and usage was returned, but route classification fell back with `ValueError`; no allowed `get_product_detail` call or result audit link was produced. The application returned a generic clarification that did not address the explicit 65 W comparison. Locked program checks recorded `provider_execution=FAIL`, `expected_business_tool_execution=FAIL`, `tool_result_audit_linkage=UNKNOWN`; the batch stopped with `CRITICAL_EXECUTION_EVIDENCE_OR_DELIVERY_FAILURE`. This is a system/evidence stop, not an ordinary quality-only failure.",
        "",
        "For PRODUCT-002, PRODUCT-001, PRODUCT-003 and PRODUCT-004, the Run reached `SUCCEEDED`, applicable route/tool/audit checks passed, budgets settled, and receipts were exactly cleaned. `Run SUCCEEDED` is lifecycle status only. Each actual answer and its bound source references are preserved in `case-results-r5-r1.json` and the raw archives. The AI suggestions above are provisional and not human-confirmed; no formal task score was assigned.",
        "",
        "## All 40-case conservation ledger",
        "",
        "| Final status | Count |",
        "|---|---:|",
    ]
    for status, count in closeout["original_40_case_conservation"]["counts"].items():
        lines.append(f"| `{status}` | {count} |")
    lines += [
        "| **Total** | **40** |",
        "",
        "| Case | Group | Original source status | Final coverage status | Record detail |",
        "|---|---|---|---|---|",
    ]
    for row in ledger["rows"]:
        detail = row.get("detail") or {}
        explanation = (detail.get("blocked_reason") or detail.get("reason") or detail.get("classification") or "No live R5 attempt; status follows bound admission/stop record")
        lines.append(f"| `{row['case_id']}` | `{row.get('group_id', 'UNKNOWN')}` | `{row.get('original_admission_status', 'UNKNOWN')}` | `{row.get('final_status', 'UNKNOWN')}` | {explanation} |")
    lines += [
        "",
        "R4 POLICY-013 (Run `02650693-694f-4e1d-94ba-eb5463c4de81`) is a separate historical binding and is not counted in R5 execution or success denominators. The remaining 22 final-profile cases are NOT_RUN after the stop; the original nine non-READY cases retain their source classifications. No cross-version success rate is reported.",
        "",
        "## Batch usage, budget and cleanup",
        "",
        f"- Actual R5 totals: Provider requests {closeout['r5_actuals']['provider_requests']}; business read calls {closeout['r5_actuals']['business_read_calls']}; tokens {closeout['r5_actuals']['tokens']}; local estimate USD {closeout['r5_actuals']['local_estimated_usd']:.6f}; supplier invoice `{closeout['r5_actuals']['vendor_invoice']}`.",
        f"- Judge {closeout['r5_actuals']['judge']}; Embedding {closeout['r5_actuals']['embedding']}; active reservations {closeout['r5_actuals']['active_reservations']}; ledger complete/settled `{closeout['r5_actuals']['budget_snapshot']['complete']}`/`{closeout['r5_actuals']['budget_snapshot']['settled']}`; integrity errors `{closeout['r5_actuals']['budget_snapshot']['integrity_errors']}`.",
        "- All five Runs have terminal lifecycle records and their exact receipts were cleaned; no Pending Actions or residual scoped fixture rows remain. A post-run read-only check reconfirmed the dedicated SQLite is idle and policy event schema is present.",
        "- The detailed post-run database, auth, budget, receipt and lock check is `reports/post-run-safety-r5-r1.json`.",
        "- Local estimates are budget accounting, not a provider-side hard cap. Billing remains UNKNOWN.",
        "",
        "## Verification and limits",
        "",
        "- Targeted regression: 76 passed, 0 failed, 0 errors, 0 skipped across the complete six listed files (`test_m6_2_personal_dev_batch_r1.py`, `test_m3_3_product_agent.py`, `test_m3_2_order_logistics.py`, `test_m3_4_policy_agent.py`, `test_m3_5_after_sales.py`, `test_langgraph_g2.py`). It ran in the project virtualenv through `pytest.main` with a project-local temporary-path plugin after Windows temp-path ACL errors; no ACL was changed. Complete log/JUnit are `test-targeted-final-20261029-173300.log/.xml`; earlier iterations remain preserved.",
        f"- Raw-archive score recomputation: {len(consistency['recomputes'])} cases recomputed twice; each result matched the stored code score (`all_match={consistency['all_match']}`). This demonstrates reproducibility, not grader validity.",
        "- M6.2 remains incomplete. This batch is an interrupted personal Dev subset run, not a unified 40-case score, official Dev Smoke, Gold, Frozen, M2, or formal M6.2 completion.",
        "- Remaining next work: offline root-cause/contract repair for PRODUCT-006, then separately authorized new frozen work if desired; no case from this closed batch may be retried. Continue human review of the five delivered outputs; add future model-route evidence across non-product domains; execute Judge calibration and required formal freeze reviews/authorizations under their own process.",
        "- R4 POLICY-013 remains prior-binding-only. R5 does not revise its provisional PARTIAL, and no prior human reference is copied into this batch.",
        "",
        "## Bound evidence index",
        "",
        "See `reports/closeout-r5-final.json`, `application-admission-offline-r1.json`, `application-admission-r5.json`, `batch-manifest-r5-final8.json`, `locked-rules-r5-final8.json`, `authorization-r5.json`, `budget-r5.json`, `execution-state-r5.json`, `execution-journal-r5.jsonl`, `reports/`, `scores/`, and each raw archive path in the journal. `SHA256SUMS-r5-final.md` binds the final package contents.",
        "",
    ]
    out = PKG / "reports/closeout-r5-final.md"
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
