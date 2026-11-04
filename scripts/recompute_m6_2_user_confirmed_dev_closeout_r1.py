"""Verify and summarize the confirmed M6.2 Dev review and two-sample Judge supplement."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029"
sys.path.insert(0, str(ROOT))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def wilson(k: int, n: int):
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def main() -> None:
    manifest_p, auth_p, state_p, results_p = (PKG / n for n in (
        "judge-manifest-r1.json", "judge-authorization-r1.json", "judge-state-r1.json", "judge-results-r1.json"))
    manifest, auth, state, results = (read(p) for p in (manifest_p, auth_p, state_p, results_p))
    event = read(PKG / "confirmation-event-r1.json")
    decisions = read(PKG / "confirmed-decisions-r1.json")
    samples = read(PKG / "judge-frozen-samples-r1.json")
    closure = read(PKG / "judge-closure-r1.json")
    if auth["manifest_sha256"] != sha(manifest_p) or state["manifest_sha256"] != sha(manifest_p):
        raise RuntimeError("manifest binding mismatch")
    for rel, expected_hash in manifest["source_bindings"].items():
        source_path = ROOT / rel
        if not source_path.is_file() or sha(source_path) != expected_hash:
            raise RuntimeError(f"frozen source binding mismatch: {rel}")
    for rel, expected_hash in manifest["runtime_bindings"].items():
        runtime_path = ROOT / rel
        if not runtime_path.is_file() or sha(runtime_path) != expected_hash:
            raise RuntimeError(f"frozen runtime binding mismatch: {rel}")
    if event["event_id"] != manifest["confirmation_event_id"] or sha(PKG / "confirmation-event-r1.json") != manifest["confirmation_event_sha256"]:
        raise RuntimeError("confirmation event mismatch")
    if sha(PKG / "confirmed-decisions-r1.json") != manifest["confirmed_decisions_sha256"]:
        raise RuntimeError("confirmed decision binding mismatch")
    if results["batch_id"] != manifest["batch_id"] or [x["sample_id"] for x in results["samples"]] != manifest["sample_order"]:
        raise RuntimeError("result sample order mismatch")
    if auth["status"] != "CLOSED_COMPLETED" or state["status"] != "CLOSED_COMPLETED" or not state["closed"]:
        raise RuntimeError("authorization was not closed successfully")
    if state["provider_requests_attempted"] != 2 or state["attempted_sample_ids"] != manifest["sample_order"]:
        raise RuntimeError("attempt ledger differs from the authorized two requests")
    if state["reserved_tokens"] != 0 or state["reserved_usd"] != 0 or state["usage_unknown"]:
        raise RuntimeError("unsettled/unknown reservation remains")
    from eval.m6_2.judge_contract import parse_response
    expected_confirmed = {x["item_id"]: x["decision"] for x in decisions["items"]}
    observed = {}
    total_tokens = 0
    total_usd = 0.0
    per_sample = []
    for result, frozen in zip(results["samples"], samples):
        assert result["sample_id"] == frozen["sample_id"]
        if result["parse_status"] != "VALID" or not result["usage_valid"] or result["response"]["finish_reason"] != "stop":
            raise RuntimeError(f"Judge result invalid for {result['sample_id']}")
        if result["request_binding"]["messages_sha256"] != frozen["messages_sha256"]:
            raise RuntimeError("sent request does not match frozen messages")
        valid, parsed, errors = parse_response(result["response"]["content"], {s["source_ref"] for s in frozen["payload"]["applicable_sources"]})
        if not valid or errors or parsed != result["parsed_judge_output"]:
            raise RuntimeError(f"independent response parse mismatch: {result['sample_id']}")
        if not parsed["evidence_refs"]:
            raise RuntimeError("empty evidence refs")
        total_tokens += result["usage"]["total_tokens"]
        total_usd += result["local_estimated_usd"]
        observed[result["case_id"]] = parsed["decision"]
        per_sample.append({"sample_id": result["sample_id"], "case_id": result["case_id"],
                           "decision": parsed["decision"], "dimensions": parsed["dimension_scores"],
                           "evidence_refs": parsed["evidence_refs"], "usage": result["usage"],
                           "local_estimated_usd": result["local_estimated_usd"], "format": "VALID"})
    if total_tokens != state["settled_tokens"] or round(total_usd, 10) != round(state["local_estimated_usd"], 10):
        raise RuntimeError("settled ledger does not match raw result usage")
    reference = {"D1-POLICY-018": "PASS", "D1-PRODUCT-003": "FAIL"}
    if set(observed) != set(reference):
        raise RuntimeError("the two declared calibration references are not present")
    confusion = {ref: {judge: 0 for judge in ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE", "UNSCORABLE")} for ref in ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE", "UNSCORABLE")}
    for case_id, label in reference.items():
        confusion[label][observed[case_id]] += 1
    agreement = sum(reference[c] == observed[c] for c in reference)
    pos = [c for c in reference if reference[c] == "PASS"]
    neg = [c for c in reference if reference[c] == "FAIL"]
    judge_pass_on_neg = sum(observed[c] == "PASS" for c in neg)
    judge_fail_on_pos = sum(observed[c] == "FAIL" for c in pos)
    analysis = {
        "schema_version": "m6.2-user-confirmed-two-sample-judge-analysis-r1",
        "scoring_target": "complete_visible_answer_task_completion_only",
        "confirmation_event_id": event["event_id"],
        "reference_scope": "six Dev task decisions confirmed in this turn; Judge sample uses only POLICY-018 PASS and PRODUCT-003 task-completion FAIL.",
        "per_sample": per_sample,
        "agreement": {"exact": f"{agreement}/{len(reference)}", "count": agreement, "denominator": len(reference),
                      "wilson_95_descriptive_only": wilson(agreement, len(reference))},
        "confusion_matrix_rows_reference_columns_judge": confusion,
        "false_accept": {"definition": "Judge PASS among confirmed task-completion FAIL references", "observed": f"{judge_pass_on_neg}/{len(neg)}" if neg else "N/A", "n": len(neg), "wilson_95_descriptive_only": wilson(judge_pass_on_neg, len(neg))},
        "false_reject": {"definition": "Judge FAIL among confirmed PASS references", "observed": f"{judge_fail_on_pos}/{len(pos)}" if pos else "N/A", "n": len(pos), "wilson_95_descriptive_only": wilson(judge_fail_on_pos, len(pos))},
        "partial_preserved_as_its_own_category": True,
        "dimension_level_human_comparison": "N/A; no user-confirmed dimension labels were added.",
        "limitations": ["Only two samples; intervals are descriptive and do not estimate general performance.", "The Judge and Dev generation use the same DeepSeek model; correlated bias remains.", "This reuses development references and is not independent or held-out validation.", "Do not combine with R4's eight items: that batch used mixed full-answer and claim-only scoring objects."],
    }
    (PKG / "judge-analysis-r1.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    six = read(PKG / "confirmed-decisions-r1.json")
    six_rows = [x for x in six["items"] if x["item_id"].startswith("D1-")]
    by_binding = Counter()
    for x in six_rows:
        archive = read(ROOT / x["original_archive_path"])
        result = archive["result"]
        source_item = next(item for item in read(ROOT / "docs/eval/m6.2-dev-score-review-closeout-r1-20261029/eight-item-review-package-r1.json")["items"] if item["item_id"] == x["item_id"])
        run_id = result["run_id"]
        x["originating_batch_binding"] = source_item.get("originating_batch_binding")
        if x["item_id"] == "D1-CROSSAGENT-030":
            auth_path = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/authorization.json"
            rules_path = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/locked-check-rules.json"
            auth = read(auth_path)
            if auth.get("execution_outcome", {}).get("run_id") != run_id:
                raise RuntimeError("CROSSAGENT-030 authorization/run binding mismatch")
            if auth.get("check_rules_sha256") != sha(rules_path) or auth.get("asset_sha256") != x.get("dataset_sha256"):
                raise RuntimeError("CROSSAGENT-030 rules/dataset binding mismatch")
            x["originating_batch_binding"] = {
                "authorization_path": str(auth_path.relative_to(ROOT)).replace("\\", "/"),
                "authorization_sha256": sha(auth_path),
                "code_sha256": auth["code_sha256"],
                "code_revision": auth["code_revision"],
                "rules_path": str(rules_path.relative_to(ROOT)).replace("\\", "/"),
                "rules_sha256": sha(rules_path),
                "dataset_sha256": auth["asset_sha256"],
                "model": auth["model"],
                "provider_endpoint": auth["provider_endpoint"],
                "pricing_sha256": auth["pricing_sha256"],
                "budget_sha256": auth["budget_sha256"],
            }
        x["run_id"] = run_id
        x["archive_lifecycle_status"] = result["status"]
        visible = result.get("result", {}).get("answer")
        x["answer_present"] = isinstance(visible, str) and bool(visible.strip())
        x["actual_answer_sha256"] = hashlib.sha256(visible.encode("utf-8")).hexdigest() if x["answer_present"] else None
        x["execution_and_delivery"] = "derived from this exact archived Run; not inferred from the newly confirmed task label"
        x["non_outcome_dimensions"] = {"fact_support": "evidence-linked observation only; no new human label",
                                        "safety": "evidence-linked observation only; no new human label",
                                        "answer_completeness": "covered only insofar as the confirmed task-outcome rationale says so"}
        binding = x.get("originating_batch_binding")
        if binding is None:
            binding = {"data_version": x.get("data_version"), "dataset_sha256": x.get("dataset_sha256"),
                       "run_archive_sha256": x.get("original_archive_sha256")}
        key = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        by_binding[key] += 1
    scoring = {
        "schema_version": "m6.2-six-case-confirmed-dev-score-summary-r1",
        "status": "DERIVED_DEV_REVIEW_LAYER; NOT_FORMAL_GRADE",
        "confirmation_event_id": event["event_id"],
        "case_count": 6,
        "confirmed_task_decisions": dict(Counter(x["decision"] for x in six_rows)),
        "task_success_binary_rate": "N/A; PARTIAL is distinct and the six Run/implementation bindings are heterogeneous.",
        "binding_groups": len(by_binding),
        "binding_group_case_counts": dict(by_binding),
        "run_status_is_not_task_success": True,
        "dimension_labels_created": False,
        "items": six_rows,
        "limitations": ["Do not merge unlike run/code/rules bindings into one frozen-config rate.", "Only the user-confirmed item-level decision is labeled; other dimensions are not auto-derived as human labels."],
    }
    (PKG / "six-case-derived-score-r1.json").write_text(json.dumps(scoring, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    state_final = read(state_p)
    junit_path = ROOT / "docs/probes/m62-user-confirmed-review-r5.xml"
    junit_root = ET.parse(junit_path).getroot()
    junit = junit_root.attrib if "tests" in junit_root.attrib else junit_root.find("testsuite").attrib
    test_counts = {key: int(junit.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
    if test_counts != {"tests": 8, "failures": 0, "errors": 0, "skipped": 0}:
        raise RuntimeError(f"final offline test result is not clean: {test_counts}")
    replay_denial = read(PKG / "judge-replay-denial-r1.json")
    if replay_denial["status"] != "REPLAY_DENIED_CLOSED_AUTHORIZATION" or replay_denial["provider_requests_added"] != 0:
        raise RuntimeError("closed-batch replay denial evidence is invalid")
    closeout = {
        "schema_version": "m6.2-user-confirmed-dev-review-closeout-r1",
        "confirmation": {"event_id": event["event_id"], "actor": event["actor_type"], "confirmed_at_utc": event["confirmed_at_utc"],
                         "scope_items": 8, "independent_or_blind": False, "gold_approval": False},
        "six_case_decisions": {"count": 6, "outcomes": dict(Counter(x["decision"] for x in six_rows)), "case_bindings": len(by_binding),
                               "formal_grade": False},
        "judge_calibration": {"batch_id": manifest["batch_id"], "requests": len(per_sample), "results": {x["case_id"]: x["decision"] for x in per_sample},
                              "total_tokens": total_tokens, "local_estimated_usd": round(total_usd, 10), "provider_invoice": "UNKNOWN",
                              "closure_status": closure["status"], "agent_runs": 0, "business_tool_calls": 0, "embedding_requests": 0,
                              "pending_actions": 0, "writes": 0},
        "route_rule_baseline": read(PKG / "route-rules-baseline-r1.json")["field_metrics"],
        "retrieval_metrics": read(PKG / "retrieval-metrics-availability-r1.json")["metrics"],
        "formal_gates_unchanged": {"m6_1": "COMPLETE", "m6_2": "IN_PROGRESS", "gold": "NOT_APPROVED", "registry": "NOT_AUTHORIZED", "m2": "BLOCKED", "formal_dev_smoke": "NOT_AUTHORIZED", "frozen_test": "NOT_AUTHORIZED"},
        "run_attempt_ledger": {"attempted_ids": state_final["attempted_sample_ids"], "provider_requests": state_final["provider_requests_attempted"],
                               "settled_tokens": state_final["settled_tokens"], "local_estimated_usd": state_final["local_estimated_usd"],
                               "reserved_tokens": state_final["reserved_tokens"], "reserved_usd": state_final["reserved_usd"],
                               "usage_unknown": state_final["usage_unknown"]},
        "tests": {"command": ".\\.venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests/test_m6_2_user_confirmed_review_r2.py --junitxml=docs/probes/m62-user-confirmed-review-r5.xml",
                  **test_counts, "exit_code": 0, "log": "docs/probes/m62-user-confirmed-review-r5.log", "junit": "docs/probes/m62-user-confirmed-review-r5.xml",
                  "failed_iterations_preserved": ["docs/probes/m62-user-confirmed-review-iteration1.log", "docs/probes/m62-user-confirmed-review-iteration2.log", "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/recompute-iteration-1-failure.json", "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/setup-iteration-1-failure.json"]},
        "closed_replay_check": replay_denial,
        "report_path": "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/closeout-r1.md",
        "not_done": ["No formal Dev Smoke or Frozen Test", "No independent/next-day blind review for Frozen", "No Registry/grant or Gold approval", "No comprehensive Judge validation"],
    }
    (PKG / "closeout-r1.json").write_text(json.dumps(closeout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "RECOMPUTE_OK", "judge_samples": len(per_sample), "tokens": total_tokens,
                      "local_estimated_usd": round(total_usd, 10), "exact_agreement": f"{agreement}/2",
                      "six_case_binding_groups": len(by_binding)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
