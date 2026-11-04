from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029"


def _json(name: str):
    return json.loads((PKG / name).read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_confirmation_event_and_six_case_decisions_are_source_bound():
    event = _json("confirmation-event-r1.json")
    derived = _json("confirmed-decisions-r1.json")
    assert event["actor_type"] == "CHAT_USER_SELF_CONFIRMED"
    assert event["identity_and_limits"]["independent_or_blind"] is False
    assert event["identity_and_limits"]["gold_approval"] is False
    expected = {
        "D1-PRODUCT-003": "FAIL", "D1-POLICY-017": "PARTIAL", "D1-POLICY-018": "PASS",
        "D1-SAFETY-036": "PARTIAL", "D1-SAFETY-037": "PASS", "D1-CROSSAGENT-030": "PARTIAL",
        "ANSWER-J3": "PARTIAL", "ANSWER-claim-04a": "INSUFFICIENT_EVIDENCE",
    }
    assert {x["item_id"]: x["decision"] for x in event["confirmed_decisions"]} == expected
    assert {x["item_id"]: x["decision"] for x in derived["items"]} == expected
    assert derived["confirmation_event_sha256"] == _sha(PKG / "confirmation-event-r1.json")
    assert event["decision_scope"]["package_sha256"] == _sha(
        ROOT / "docs/eval/m6.2-dev-score-review-closeout-r1-20261029/eight-item-review-package-r1.json")
    for rel, digest in event["decision_scope"]["source_bindings"].items():
        assert _sha(ROOT / rel) == digest
    for item in derived["items"]:
        assert item["dimension_decisions"]["task_outcome_or_review_disposition"] == item["decision"]
        for key in ("answer_completeness", "factual_support", "safety", "execution_quality"):
            assert item["dimension_decisions"][key] == "NOT_SEPARATELY_LABELED"


def test_j3_reason_and_claim_scope_are_corrected_without_reopening_labels():
    derived = _json("confirmed-decisions-r1.json")
    items = {x["item_id"]: x for x in derived["items"]}
    assert items["ANSWER-J3"]["decision"] == "PARTIAL"
    assert "未影响二次销售" in items["ANSWER-J3"]["confirmed_reasoning_scope"]["accepted_rationale_correction"]
    assert "未开封" in items["ANSWER-J3"]["confirmed_reasoning_scope"]["accepted_rationale_correction"]
    assert items["ANSWER-claim-04a"]["decision"] == "INSUFFICIENT_EVIDENCE"
    assert items["ANSWER-claim-04a"]["confirmed_reasoning_scope"]["scope_decision"].startswith("claim_only")


def test_judge_batch_is_two_blinded_full_answer_completion_samples():
    samples = _json("judge-frozen-samples-r1.json")
    manifest = _json("judge-manifest-r1.json")
    assert [x["case_id"] for x in samples] == ["D1-POLICY-018", "D1-PRODUCT-003"]
    assert all(x["payload"]["rubric"]["scope"] == "complete_visible_answer_task_completion_only" for x in samples)
    assert manifest["scoring_target"] == "complete_visible_answer_task_completion_only"
    assert manifest["response_schema_is_sent_in_messages"] is True
    for sample in samples:
        payload = sample["payload"]
        assert set(payload) == {"question", "answer_or_claim", "applicable_sources", "rubric", "scoring_target"}
        assert sample["reference_label_excluded"] is True
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "human_reference" not in serialized.lower()
        assert "AI_ASSISTED_PROPOSAL" not in serialized
        assert "expected_score" not in serialized.lower()
        assert sample["request_summary"]["full_schema_in_system_message"] is True
    state = _json("judge-state-r1.json")
    if (PKG / "judge-results-r1.json").exists():
        assert state["provider_requests_attempted"] == 2
        assert state["attempted_sample_ids"] == manifest["sample_order"]
        assert state["closed"] is True
    else:
        assert state["provider_requests_attempted"] == 0
        assert state["attempted_sample_ids"] == []


def test_route_baseline_is_deterministic_and_model_items_are_not_scored():
    data = _json("route-rules-baseline-r1.json")
    assert data["n"] == 52
    assert data["split_counts"] == {"dev": 28, "validation": 24}
    assert data["execution_mode"] == "decide_route(classifier=None); no network/model calls"
    assert data["classification"] == "DETERMINISTIC_RULE_BASELINE_NOT_CURRENT_LLM_ROUTER_EVALUATION"
    assert data["field_metrics"]["strategy"]["score"] == "44/52"
    assert data["field_metrics"]["required_capabilities"]["score"] == "45/52"
    assert data["field_metrics"]["sub_strategy"]["score"] == "44/46"
    assert data["field_metrics"]["strategy"]["wilson_95"] is not None


def test_retrieval_and_formal_gates_are_not_promoted_by_dev_confirmation():
    retrieval = _json("retrieval-metrics-availability-r1.json")
    assert retrieval["ranking_quality_status"] == "N/A"
    assert all(v == "N/A" for v in retrieval["metrics"].values())
    closeout = _json("closeout-r1.json")
    assert closeout["formal_gates_unchanged"] == {
        "gold": "NOT_APPROVED", "registry": "NOT_AUTHORIZED", "m2": "BLOCKED",
        "formal_dev_smoke": "NOT_AUTHORIZED", "frozen_test": "NOT_AUTHORIZED",
        "m6_1": "COMPLETE", "m6_2": "IN_PROGRESS",
    }


def test_six_case_scores_keep_the_crossagent030_origin_binding_separate():
    score = _json("six-case-derived-score-r1.json")
    assert score["case_count"] == 6
    assert score["confirmed_task_decisions"] == {"FAIL": 1, "PARTIAL": 3, "PASS": 2}
    assert score["binding_groups"] == 2
    rows = {x["item_id"]: x for x in score["items"]}
    assert rows["D1-CROSSAGENT-030"]["decision"] == "PARTIAL"
    assert rows["D1-CROSSAGENT-030"]["originating_batch_binding"]["code_sha256"] == "e8162812550798f6d6aa69a0843c245f5beb9d75a3fc441697652a0a85a3b321"
    assert rows["D1-PRODUCT-003"]["originating_batch_binding"]["code_sha256"] == "3b0ef85c4d725bc46e143562a90d304253778ed61d7a36f5bb4eecd5a3a34194"
    assert rows["D1-CROSSAGENT-030"]["confirmed_reasoning_scope"]["historical_checker_task_outcome"] == "UNKNOWN"
    assert rows["D1-CROSSAGENT-030"]["confirmed_reasoning_scope"]["historical_continue_decision_preserved"] is True


def test_route_recompute_matches_frozen_rule_only_report():
    # A fresh process recomputes the rule-only routes and compares stable outputs;
    # it never constructs or calls an LLM classifier.
    result = subprocess.run(
        [sys.executable, "-c", "import json,pathlib; from app.m1.routing import decide_route; "
         "d=json.loads(pathlib.Path('docs/eval/m6.1-human-review-preflight-60-r1-20261028/confirmed-review-decisions-r1.json').read_text(encoding='utf-8')); "
         "print(json.dumps([{'case_id':r['case_id'],'strategy':decide_route(r['user_query'],classifier=None).strategy,'caps':decide_route(r['user_query'],classifier=None).required_capabilities} for r in d['route_pre_reviews']]))"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    actual = json.loads(result.stdout)
    expected = _json("route-rules-baseline-r1.json")["case_results"]
    assert len(actual) == len(expected) == 52
    assert all(a["case_id"] == e["case_id"] and a["strategy"] == e["fields"]["strategy"]["predicted"]
               and a["caps"] == e["fields"]["required_capabilities"]["predicted"] for a, e in zip(actual, expected))


def test_actual_judge_results_are_valid_bound_and_fully_settled():
    results = json.loads((PKG / "judge-results-r1.json").read_text(encoding="utf-8"))
    manifest = _json("judge-manifest-r1.json")
    state = _json("judge-state-r1.json")
    auth = _json("judge-authorization-r1.json")
    assert [x["sample_id"] for x in results["samples"]] == manifest["sample_order"]
    assert state["status"] == auth["status"] == "CLOSED_COMPLETED"
    assert state["provider_requests_attempted"] == 2
    assert state["settled_tokens"] == sum(x["usage"]["total_tokens"] for x in results["samples"])
    assert abs(state["local_estimated_usd"] - sum(x["local_estimated_usd"] for x in results["samples"])) < 1e-10
    for item in results["samples"]:
        assert item["parse_status"] == "VALID" and item["usage_valid"] is True
        assert item["request_binding"]["automatic_retries"] == 0
        refs = {s["source_ref"] for s in _json("judge-frozen-samples-r1.json")[manifest["sample_order"].index(item["sample_id"])]["payload"]["applicable_sources"]}
        assert item["parsed_judge_output"]["evidence_refs"]
        assert set(item["parsed_judge_output"]["evidence_refs"]).issubset(refs)
