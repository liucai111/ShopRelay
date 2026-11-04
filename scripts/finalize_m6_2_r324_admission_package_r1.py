"""Finalize the r3.2.4 40-case personal Dev admission ledger (no Runs)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029"
MATRIX = PKG / "all40-case-admission-matrix-r2.json"
DATA = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
MARKER = "## 2026-10-29 M6.2 r3.2.4 40-case admission closeout R1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert len(matrix["cases"]) == 40
    assert len({c["case_id"] for c in matrix["cases"]}) == 40
    assert sha(DATA) == matrix["dataset"]["sha256"]
    assert sum(matrix["admission_counts"].values()) == 40
    assert len(matrix["eligible_case_ids"]) == matrix["admission_counts"]["READY"] == 31

    case_ids = [c["case_id"] for c in matrix["cases"]]
    rules = {
        "schema_version": "m6.2-r324-dev-batch-check-rules-v1",
        "scope": "personal non-Frozen Dev only; exact r3.2.4 case universe; no formal score or Gold status",
        "case_order": matrix["limits_for_future_exact_eligible_subset"]["proposed_order"],
        "admission_statuses": ["READY", "BLOCKED_ENGINEERING", "BLOCKED_CONTRACT_OR_DATA", "HELD_REVIEW_OR_AUTHORIZATION", "UNSCORABLE"],
        "execution_status_for_all_cases": "NOT_RUN",
        "execution_vs_task": {
            "separate_dimensions": ["route_and_capability", "tool_execution_and_authorization", "business_fact", "source_and_evidence", "user_visible_delivery", "task_outcome", "application_or_infrastructure_failure", "evaluator_failure", "budget_and_cleanup"],
            "run_succeeded_is_not_task_success": True,
            "agent_started_is_not_required_for_semantically_valid_direct_or_refusal": True,
            "zero_calls_is_distinct_from_missing_audit": True,
            "preflight_repository_reads_are_not_trial_tool_calls": True,
            "unknown_or_unverifiable_critical_audit_usage_or_cleanup_blocks_continuation": True,
            "complete_safe_terminal_task_quality_failure_may_be_recorded_and_continue": True,
            "no_retry_or_hotfix_after_first_real_run": True,
        },
        "safety": {"candidate_or_frozen_access": False, "business_writes": 0, "pending_actions": 0, "judge": 0, "embedding": 0, "automatic_retries": 0},
        "per_case_limits": {"application_runs": 1, "provider_requests": 6, "read_only_business_calls": 8, "tokens": 120000, "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1},
        "batch_limits": {"application_runs": 40, "provider_requests": 240, "tokens": 4800000, "estimated_usd": 7.20, "runtime_seconds": 7200, "concurrency": 1},
        "per_case_capability_authority": "matrix.allowed_read_only_capabilities; never infer permission from expected answers or repository availability",
        "scoring": {"code_grader": "only from bound raw run/tool/event evidence", "semantic_score": "not generated until actual output and scoring implementation are available", "outcomes": ["PASS", "PARTIAL", "FAIL", "UNKNOWN", "UNSCORABLE", "NOT_RUN", "EVALUATOR_FAILURE"]},
        "no_post_result_rule_changes": True,
        "source_matrix_case_ids_sha256": hashlib.sha256("\n".join(case_ids).encode()).hexdigest(),
    }
    rules_path = PKG / "locked-rules-r1.json"
    write_json(rules_path, rules)

    # Hash only declared inputs. Working tree digest does not include ignored/unreadable material.
    source_candidates = [
        ROOT / "ShopRelay_Agent_Eval_完整评测方案.md",
        ROOT / "项目规格/evaluation-spec.md",
        ROOT / "docs/progress.md",
        DATA,
        ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmation-event-r1.json",
        ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmed-decisions-r1.json",
        ROOT / "docs/eval/m6.1-d1-r3.2.4-40-case-readiness-matrix-r1.md",
        ROOT / "docs/eval/m6.1-d1-r3.2.2-review-bundle/m6.1-d1-shipment-steward-decisions-r3.2.2.md",
        ROOT / "app/m1/routing.py",
        ROOT / "eval/m6_1/personal_dev.py",
        ROOT / "eval/m6_1/personal_dev_runtime.py",
        ROOT / "scripts/run_m6_1_d1_personal_dev.py",
        ROOT / "scripts/audit_m6_2_r324_all40_repository_admission_r1.py",
        ROOT / "scripts/build_m6_2_r324_case_admission_matrix_r1.py",
        ROOT / "scripts/finalize_m6_2_r324_admission_package_r1.py",
        ROOT / "tests/test_m2_routing.py",
        ROOT / "tests/test_m6_1_d1_personal_dev_offline.py",
        ROOT / "tests/test_m6_1_d1_personal_batch_authorization_offline.py",
    ]
    source_hashes = {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in source_candidates if p.is_file()}
    for relative, expected in (("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json", matrix["dataset"]["sha256"]),):
        assert source_hashes[relative] == expected

    manifest = {
        "schema_version": "m6.2-r324-personal-dev-batch-manifest-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "BLOCKED_BEFORE_AUTHORIZATION",
        "classification": "PERSONAL_DEV_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "dataset": matrix["dataset"],
        "case_universe_count": 40,
        "case_admission_counts": matrix["admission_counts"],
        "proposed_executable_case_ids": matrix["eligible_case_ids"],
        "proposed_order": matrix["limits_for_future_exact_eligible_subset"]["proposed_order"],
        "bindings": matrix["bindings"],
        "repository_audit": matrix["repository_audit"],
        "locked_rules_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/locked-rules-r1.json",
        "locked_rules_sha256": sha(rules_path),
        "pricing_evidence_sha256": sha(PKG / "official-pricing-evidence-r1.json"),
        "declared_source_sha256": source_hashes,
        "authorization": {"created": False, "reason": "Existing D1 batch CLI is fixed to exactly 40 cases and its scoring/stop behavior is incompatible with the new admission ledger. Do not create a misleading authorization or bypass the coordinator."},
        "execution": {"runs": 0, "provider_requests": 0, "business_tool_calls": 0, "tokens": 0, "estimated_usd": 0, "cases": {case_id: "NOT_RUN" for case_id in case_ids}},
    }
    manifest_path = PKG / "batch-manifest-r1.json"
    write_json(manifest_path, manifest)
    state_path = PKG / "execution-state-r1.json"
    write_json(state_path, {"schema_version": "m6.2-r324-execution-state-v1", "manifest_sha256": sha(manifest_path), "authorization_created": False, "reserved_budget": {"provider_requests": 0, "tokens": 0, "estimated_usd": 0}, "attempt_count": {case_id: 0 for case_id in case_ids}, "state": {case_id: "NOT_RUN" for case_id in case_ids}})
    recalc_path = PKG / "recompute-r1.json"
    write_json(recalc_path, {
        "schema_version": "m6.2-r324-admission-recompute-v1",
        "source_matrix_sha256": sha(MATRIX),
        "dataset_sha256_recomputed": sha(DATA),
        "case_count_recomputed": len(case_ids),
        "unique_case_count_recomputed": len(set(case_ids)),
        "admission_counts_recomputed": {k: sum(c["admission_status"] == k for c in matrix["cases"]) for k in rules["admission_statuses"]},
        "eligible_count_recomputed": sum(c["admission_status"] == "READY" for c in matrix["cases"]),
        "all_trial_status_not_run": all(c["trial_status"] == "NOT_RUN" for c in matrix["cases"]),
        "all_run_records_absent": True,
        "note": "Recomputed from source matrix and dataset; not a task score or execution result."
    })

    statuses = {}
    for c in matrix["cases"]:
        statuses.setdefault(c["admission_status"], []).append(c)
    table = ["| Case | Group | Admission | Repository evidence / blocker | Execution |", "|---|---|---|---|---|"]
    for c in matrix["cases"]:
        detail = (c["admission_reason"] or c.get("expected_business_result") or "No case-specific admission blocker recorded.").replace("|", "\\|").replace("\n", " ")
        table.append(f"| {c['case_id']} | {c['group_id']} | {c['admission_status']} | {detail} | NOT_RUN |")
    report = f'''# M6.2 r3.2.4 40-case Personal Dev Admission Closeout R1

Created: {manifest['created_at_utc']}

## Conclusion

The 40-case admission ledger is complete: 40 unique cases, preserving original order and `group_id`. Repository/fixture preflight classified 31 `READY`, 1 `BLOCKED_ENGINEERING`, 3 `BLOCKED_CONTRACT_OR_DATA`, 4 `HELD_REVIEW_OR_AUTHORIZATION`, and 1 `UNSCORABLE`. This is an admission ledger, not a 40-case execution or grade. No case ran; all 40 are `NOT_RUN`.

No execution authorization file or durable trial reservation was created. The current CLI's `--execute-batch` path delegates to `execute_personal_batch`, which requires exactly 40 cases (`eval/m6_1/personal_dev.py` around line 827), hardcodes 4 provider and 4 tool calls per case / 160 per batch, and its first-case continuation gate requires `agent_started` (`around line 968`). It also reports task score as `UNKNOWN_NO_REAL_D1_GRADER` (`around line 1023`). This cannot safely execute only the 31 case-level READY subset or honor the new DIRECT/REFUSE, budget, scoring and stop contracts. Bypassing it would bypass the coordinator; therefore the authorized subset remains unrun pending a compatible exact-scope runner, durable ledger and generic outcome grader.

## Admission totals

| Status | Count | Cases |
|---|---:|---|
'''
    for status in rules["admission_statuses"]:
        rows = statuses.get(status, [])
        report += f"| {status} | {len(rows)} | {', '.join(c['case_id'] for c in rows) or '—'} |\n"
    report += "\n## Full 40-case ledger\n\n" + "\n".join(table)
    report += f'''\n\n## Repository preflight and bindings

- Dataset: `{matrix['dataset']['path']}`; SHA-256 `{matrix['dataset']['sha256']}`.
- Repository audit: `{matrix['repository_audit']['path']}`; SHA-256 `{matrix['repository_audit']['sha256']}`; {matrix['repository_audit']['total_repository_reads']} read-only preflight reads (separate from trial calls; cap 320), 0 Agent Runs, 0 Provider requests.
- Personal SQLite identity: `{matrix['bindings']['database_identity']}`. Audit found no post-probe rows in scoped User/Order/Shipment/Run/PendingAction/Fixture tables and no active receipt/lease.
- Implementation digest: `{matrix['bindings']['implementation_digest']}`; Git HEAD `{matrix['bindings']['git_head_revision']}`. The working tree had pre-existing modifications; HEAD is not a clean-tree identity.
- Model/endpoint: `{matrix['bindings']['model']}` / `{matrix['bindings']['provider_endpoint']}`. API key value was not recorded.
- Pricing checked at `{json.loads((PKG / 'official-pricing-evidence-r1.json').read_text(encoding='utf-8'))['checked_at_utc']}` from official public docs; refresh within 24 hours before future authorization. Local budget remains an estimate, not a provider hard cap.

## Completed and uncompleted work

- Completed: all 40 cases audited against fixed r3.2.4; real synthetic Repository adapters were exercised under exact receipt cleanup in an isolated personal SQLite; source, owner mapping, tool arguments, expected result, hard gates, semantic requirements, route baseline, held status and trial state are recorded per case.
- Completed: generic refund-status routing defect corrected in `app/m1/routing.py` (route version v10) for AFTERSALES-026/027; cancellation remains on its existing route. The route baseline in the matrix is deterministic-only, not evidence of current LLM route behavior.
- Completed: targeted offline regression, **114 passed, 0 failed/errors/skipped**. Command and full log/JUnit are recorded below.
- Not completed: valid execution authorization, model trial, outcome scoring, independent score recomputation over actual trials, and execution cleanup. These require a compatible exact-subset runner and grader before any request.
- No Provider request, Agent Run, business write, Judge or Embedding occurred in this turn.

## Test evidence

Command: `PYTHONPATH=.;.vendor/g2;.vendor PYTHONDONTWRITEBYTECODE=1 .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp/m62-r324-admission-r4 --junitxml docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/offline-regression-r1.xml tests/test_m2_routing.py tests/test_m6_1_d1_personal_dev_offline.py tests/test_m6_1_d1_personal_batch_authorization_offline.py`

Result: 114 passed; 0 failed, 0 errors, 0 skipped. Log: `offline-regression-r1.log`; JUnit: `offline-regression-r1.xml`.

## Gate/status boundaries

M6.1 remains `COMPLETE`; M6.2 remains `IN_PROGRESS`. Gold `NOT_APPROVED`, Registry `NOT_AUTHORIZED`, M2 `BLOCKED`, formal Dev Smoke `NOT_AUTHORIZED`, Frozen Test `NOT_AUTHORIZED`. No status was changed. Confirmed event `M62-DEV-REVIEW-CHAT-SELF-CONFIRMED-20261029-R1` applies only to its six case outcomes and J3/claim-04a rationale; it is not a 40-case review or Gold approval.

The product+policy search case PRODUCT-005 remains blocked by an application route capability mismatch. POLICY-015/016 and CROSSAGENT-029 remain contract/data blocked. Shipment stewardship deferrals and CROSSAGENT-033 independent reviewer requirements remain held. AFTERSALES-024 remains unscorable pending business clarification of the general `paid_at` rule.

## Next minimal work

Implement and test a new exact-manifest executor that accepts only the 31 READY IDs in their frozen order, enforces independent per-case reservations/attempt counters across restarts, supports lawful DIRECT/REFUSE outcomes, archives actual output/events, runs the generic bound grader, and performs receipt-scoped cleanup. Then repeat no-model admission against the frozen manifest; refresh price evidence if stale; only then create a one-time authorization bound to final code, data, DB identity, prompts, rules and limits.
'''
    report_path = PKG / "closeout-r1.md"
    report_path.write_text(report, encoding="utf-8")

    progress = ROOT / "docs/progress.md"
    text = progress.read_text(encoding="utf-8")
    if MARKER not in text:
        progress.write_text(text.rstrip() + "\n\n" + MARKER + "\n\n" +
            "- M6.1 保持 `COMPLETE`；本轮完成 r3.2.4 全部 40 案准入矩阵及隔离 Repository/Fixture 预检。守恒结果：31 READY、1 BLOCKED_ENGINEERING、3 BLOCKED_CONTRACT_OR_DATA、4 HELD_REVIEW_OR_AUTHORIZATION、1 UNSCORABLE；全部试次仍为 NOT_RUN。预检共57次只读Repository读取，0 Agent/Provider；receipt/lease已按范围清理。\n"
            "- AFTERSALES-026/027 的通用退款状态路由缺陷已修复；定向完整回归114 passed。六案用户确认仍严格限原范围，未扩展为40案审核。\n"
            "- 新执行批次在授权前阻断：现有D1批量CLI硬性要求40案，并要求首案agent_started；批量预算仍固定4/4×40，且没有通用Outcome Grader，不能安全执行31案READY子集。未创建授权、Run或预算预留；0 Provider、0费用。见 `docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/closeout-r1.md`。Gold、Registry、M2、正式Dev Smoke及Frozen状态不变。\n", encoding="utf-8")

    # Include all package files except the checksum file itself; preserve earlier reports/logs.
    files = sorted(p for p in PKG.iterdir() if p.is_file() and p.name != "SHA256SUMS-r1.md")
    lines = ["# SHA-256 manifest — M6.2 r3.2.4 40-case admission package R1", "", "Generated after closeout/progress append. Entries are current byte hashes; earlier R1–R4 artifacts remain unchanged.", ""]
    for p in files:
        lines.append(f"{sha(p)}  {p.relative_to(ROOT).as_posix()}")
    lines.append(f"{sha(progress)}  docs/progress.md (current appended file)")
    for rel, digest in sorted(source_hashes.items()):
        lines.append(f"{digest}  {rel} (declared input/source)")
    (PKG / "SHA256SUMS-r1.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Final consistency assertions over what was just emitted.
    assert json.loads(recalc_path.read_text(encoding="utf-8"))["admission_counts_recomputed"] == matrix["admission_counts"]
    assert sha(DATA) == matrix["dataset"]["sha256"]
    print(json.dumps({"package": str(PKG.relative_to(ROOT)), "counts": matrix["admission_counts"], "case_count": len(case_ids), "eligible": len(matrix["eligible_case_ids"]), "runs": 0, "provider_requests": 0, "files": len(list(PKG.iterdir()))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
