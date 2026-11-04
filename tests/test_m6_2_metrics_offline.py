from __future__ import annotations

from copy import deepcopy
import json
import hashlib
from pathlib import Path

import pytest

from eval.m6_1.aggregate import (
    MetricInputError,
    ReportMismatchError,
    aggregate_capabilities,
    cluster_bootstrap_interval,
    recompute_derived_report,
    serialize_derived_report,
    summarize_calibration,
    verify_metric_contract_file,
    MetricContractMismatchError,
    wilson_interval,
)


EXPERIMENT_ID = "experiment.m62.synthetic"
METRIC_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "docs/eval/m6.2-prep-r1/metric-contracts.md"
METRIC_CONTRACT_SHA256 = hashlib.sha256(METRIC_CONTRACT_PATH.read_bytes()).hexdigest()


def make_case(case_id: str, route: str = "SINGLE", *, scoreability: str = "SYNTHETIC_ONLY",
              retrieval: dict | None = None, evidence: dict | None = None,
              key_facts: list | None = None) -> dict:
    expected = {"route": route}
    if retrieval is not None:
        expected["retrieval"] = retrieval
    if evidence is not None:
        expected["evidence"] = evidence
    if key_facts is not None:
        expected["key_facts"] = key_facts
    row = {
        "case": {
            "schema_version": "1.0", "case_id": case_id, "group_id": f"group.{case_id}",
            "split": "synthetic", "input": {"query": "synthetic-only input"},
            "expected_outcome": expected, "rubric_refs": ["rubric.synthetic.r1"],
            "fixture_ref": "fixture.synthetic.r1", "risk_level": "LOW", "data_version": "synthetic.r1",
        },
        "scoreability": {"status": scoreability},
    }
    return row


def make_trial(case_id: str, index: int, *, lifecycle: str = "SUCCEEDED", actual_route: str | None = None,
               run_ids: list[str] | None = None, started: bool | None = None, raw_result: dict | None = None,
               outcome_values: dict | None = None, run_statuses: list[str] | None = None) -> dict:
    run_ids = list(run_ids if run_ids is not None else [f"run.{index}"] if lifecycle not in {"NOT_RUN", "BUDGET_STOP"} else [])
    trial_id = f"trial.{index}"
    result = {"route": actual_route or "SINGLE"}
    if raw_result is not None:
        result.update(deepcopy(raw_result))
    if "route" not in result:
        result["route"] = actual_route or "SINGLE"
    if run_ids and "retrieval" in result and result["retrieval"].get("run_id") is None:
        result["retrieval"]["run_id"] = run_ids[0]
    if run_ids and "evidence" in result and result["evidence"].get("run_id") is None:
        result["evidence"]["run_id"] = run_ids[0]
    default_verdicts = {"run_success": "PASS" if lifecycle == "SUCCEEDED" else "UNKNOWN",
                        "answer_correctness": "PASS", "task_success": "PASS", "safety_result": "PASS",
                        "grading_status": "GRADED"}
    if outcome_values:
        default_verdicts.update(outcome_values)
    outcome = {
        "schema_version": "1.0", "outcome_id": f"outcome.{index}", "trial_id": trial_id,
        **default_verdicts, "rubric_version": "rubric.synthetic.r1", "score_refs": [],
        "created_at_utc": "2026-10-25T00:00:00Z",
    }
    observations = []
    statuses = run_statuses or ([lifecycle] if run_ids and lifecycle in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT", "ENV_ERROR"} else [])
    if len(statuses) != len(run_ids):
        raise ValueError("run_statuses must correspond to run_ids")
    observations.extend({"run_id": run_id, "status": status} for run_id, status in zip(run_ids, statuses))
    if lifecycle == "UNKNOWN_RESULT" and not run_ids:
        observations.append({"run_id": None, "status": "UNKNOWN_RESULT"})
    is_started = lifecycle not in {"NOT_RUN", "BUDGET_STOP"} if started is None else started
    return {
        "trial": {
            "schema_version": "1.0", "trial_id": trial_id, "case_id": case_id,
            "experiment_id": EXPERIMENT_ID, "trial_index": index, "run_ids": run_ids,
            "lifecycle_status": lifecycle, "model_calls": [], "tool_calls": [],
            "raw_result_ref": f"raw/{trial_id}.json" if raw_result is not None or actual_route else None,
            "error": None, "usage": {}, "latency_ms": {},
        },
        "raw_result": result if is_started else None,
        "outcome": outcome if lifecycle in {"SUCCEEDED", "UNKNOWN_RESULT"} else None,
        "started": is_started,
        "run_observations": observations,
    }


def compute(cases: list[dict], trials: list[dict], *, recall_ks=(3, 5)) -> dict:
    return aggregate_capabilities(
        cases=cases, trials=trials, experiment_config={"experiment_id": EXPERIMENT_ID, "budget": {"scope": "synthetic"}},
        dataset_scope="SYNTHETIC_ONLY", recall_ks=recall_ks,
        metric_contract_version="m6.2-prep-r1", metric_contract_sha256=METRIC_CONTRACT_SHA256,
    )


def test_full_metrics_calculate_from_synthetic_trials_and_separate_outcomes() -> None:
    labels = ["DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"]
    predicted = ["DIRECT", "SINGLE", "SINGLE", "AGENT", "REFUSE"]
    cases = [make_case(f"case.{i}", label,
        retrieval={"relevance_reviewed": True, "relevant_evidence_ids": [f"e{i}"]},
        evidence={"reviewed": True, "required_evidence_ids": [f"e{i}"]},
        key_facts=[{"fact_id": f"f{i}", "reviewed": True}]) for i, label in enumerate(labels, 1)]
    trials = []
    for i, (case, route) in enumerate(zip(cases, predicted), 1):
        raw = {
            "retrieval": {"logs_present": True, "ranked_evidence_ids": [f"e{i}", "other"]},
            "evidence": {"logs_present": True, "found_evidence_ids": [f"e{i}"], "authorized_evidence_ids": [f"e{i}"]},
            "key_facts": [{"fact_id": f"f{i}", "adjudicated": True, "correct": i != 2, "source_authoritative": True, "source_ref": f"synthetic.source.{i}"}],
            "claims": [{"claim_id": f"claim.{i}", "adjudicated": True, "supported": i != 2}],
            "safety": {"checked": True, "unauthorized_side_effects": 1 if i == 5 else 0},
        }
        outcome = {"answer_correctness": "FAIL", "task_success": "FAIL"} if i == 2 else {"answer_correctness": "PASS", "task_success": "PASS"}
        trials.append(make_trial(case["case"]["case_id"], i, actual_route=route, raw_result=raw, outcome_values=outcome))

    report = compute(cases, trials, recall_ks=(2, 3))
    router = report["metrics"]["router"]
    assert router["labels"] == labels
    assert router["confusion_matrix"]["MULTI"]["SINGLE"] == 1
    assert router["per_class"]["SINGLE"]["support"] == 1
    assert router["macro_f1"] == pytest.approx(11 / 15)
    assert router["per_class"]["SINGLE"]["precision"]["numerator"] == 1
    assert router["per_class"]["SINGLE"]["precision"]["denominator"] == 2
    assert report["metrics"]["retrieval"]["recall_at_2"]["value"] == 1.0
    assert report["metrics"]["retrieval"]["mrr_at_2"]["value"] == 1.0
    assert report["metrics"]["evidence_coverage"]["value"] == 1.0
    assert report["metrics"]["key_fact_correctness"]["value"] == 0.8
    assert report["metrics"]["unsupported_assertion_rate"]["value"] == 0.2
    assert report["metrics"]["run_success_rate"]["value"] == 1.0
    assert report["metrics"]["answer_correctness"]["value"] == 0.8
    assert report["metrics"]["task_success_rate"]["value"] == 0.8
    assert report["metrics"]["unauthorized_side_effect_count"]["count"] == 1
    assert report["counts"]["independent_case_count"] == 5
    assert report["metrics"]["unknown_result_rate"]["value"] == 0.0


def test_missing_route_class_is_retained_and_macro_f1_is_not_silently_redefined() -> None:
    cases = [make_case("case.only", "DIRECT")]
    trials = [make_trial("case.only", 1, actual_route="DIRECT")]
    route = compute(cases, trials)["metrics"]["router"]
    assert route["labels"] == list(("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"))
    assert route["zero_support_classes"] == ["SINGLE", "MULTI", "AGENT", "REFUSE"]
    assert route["macro_f1"] is None
    assert route["status"] == "NOT_SCORABLE_MISSING_CLASS_SUPPORT"
    assert route["per_class"]["MULTI"]["support"] == 0


def test_unapproved_case_gold_returns_null_answer_and_task_scores() -> None:
    case = make_case("case.pending", "DIRECT", scoreability="PENDING_HUMAN_REVIEW",
        retrieval={"relevance_reviewed": True, "relevant_evidence_ids": ["pending.evidence"]},
        evidence={"reviewed": True, "required_evidence_ids": ["pending.evidence"]},
        key_facts=[{"fact_id": "pending.fact", "reviewed": True}])
    trial = make_trial("case.pending", 1, actual_route="DIRECT", raw_result={
        "retrieval": {"logs_present": True, "ranked_evidence_ids": ["pending.evidence"]},
        "evidence": {"logs_present": True, "found_evidence_ids": ["pending.evidence"], "authorized_evidence_ids": ["pending.evidence"]},
        "key_facts": [{"fact_id": "pending.fact", "adjudicated": True, "correct": True, "source_authoritative": True, "source_ref": "synthetic.source.pending"}],
        "claims": [{"claim_id": "pending.claim", "adjudicated": True, "supported": True}],
    })
    report = compute([case], [trial])
    assert report["metrics"]["answer_correctness"]["value"] is None
    assert report["metrics"]["answer_correctness"]["reason"] == "NO_APPROVED_GRADED_ANSWERS"
    assert report["metrics"]["task_success_rate"]["value"] is None
    assert report["counts"]["scoreable_case_count"] == 0
    assert report["metrics"]["router"]["accuracy"]["value"] is None
    assert report["metrics"]["retrieval"]["recall_at_3"]["status"] == "NOT_SCORABLE_CASE_QUALIFICATION"
    assert report["metrics"]["retrieval"]["mrr"]["value"] is None
    assert report["metrics"]["evidence_coverage"]["status"] == "NOT_SCORABLE_CASE_QUALIFICATION"
    assert report["metrics"]["key_fact_correctness"]["value"] is None
    assert report["metrics"]["unsupported_assertion_rate"]["value"] is None


def test_unknown_and_other_lifecycle_states_remain_separate() -> None:
    states = ["SUCCEEDED", "UNKNOWN_RESULT", "ENV_ERROR", "MODEL_UNAVAILABLE", "NOT_RUN", "BUDGET_STOP", "UNSCORABLE", "PENDING_HUMAN_REVIEW"]
    cases, trials = [], []
    for i, status in enumerate(states, 1):
        cid = f"case.status.{i}"
        cases.append(make_case(cid))
        run_ids = [f"run.status.{i}"] if status in {"SUCCEEDED", "UNKNOWN_RESULT", "ENV_ERROR", "MODEL_UNAVAILABLE"} else []
        obs_status = "UNKNOWN_RESULT" if status == "UNKNOWN_RESULT" else status
        trials.append(make_trial(cid, i, lifecycle=status, run_ids=run_ids,
                                 run_statuses=[obs_status] if run_ids else None))
    report = compute(cases, trials)
    counts = report["counts"]["lifecycle_status_counts"]
    assert counts == {state: 1 for state in states}
    assert report["counts"]["not_run_count"] == 1
    assert report["counts"]["budget_stop_count"] == 1
    assert report["counts"]["environment_failure_count"] == 1
    assert report["counts"]["model_unavailable_count"] == 1
    assert report["counts"]["unknown_result_count"] == 1
    assert report["metrics"]["run_success_rate"]["numerator"] == 1
    assert report["metrics"]["run_success_rate"]["denominator"] == 4
    assert report["metrics"]["unknown_result_rate"]["numerator"] == 1
    assert report["metrics"]["unknown_result_rate"]["denominator"] == 6


def test_missing_retrieval_telemetry_is_not_a_zero_hit() -> None:
    cases = [
        make_case("case.no_log", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": ["r1"]}),
        make_case("case.zero_hit", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": ["r2"]}),
    ]
    trials = [
        make_trial("case.no_log", 1, raw_result={"retrieval": {"logs_present": False}}),
        make_trial("case.zero_hit", 2, raw_result={"retrieval": {"logs_present": True, "ranked_evidence_ids": []}}),
    ]
    metric = compute(cases, trials)["metrics"]["retrieval"]["recall_at_3"]
    assert metric["status"] == "INCOMPLETE_TELEMETRY"
    assert metric["value"] is None
    assert metric["reason"] == "RETRIEVAL_LOG_MISSING"


def test_empty_relevance_is_not_counted_as_a_zero_hit_or_mrr_query() -> None:
    cases = [
        make_case("case.with_relevant", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": ["e1"]}),
        make_case("case.no_relevant", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": []}),
    ]
    trials = [
        make_trial("case.with_relevant", 1, raw_result={"retrieval": {"logs_present": True, "ranked_evidence_ids": ["e1"]}}),
        make_trial("case.no_relevant", 2, raw_result={"retrieval": {"logs_present": True, "ranked_evidence_ids": []}}),
    ]
    retrieval = compute(cases, trials)["metrics"]["retrieval"]
    assert retrieval["recall_at_3"]["status"] == "PARTIAL_UNSCORABLE_EMPTY_RELEVANCE"
    assert retrieval["recall_at_3"]["value"] == 1.0
    assert retrieval["recall_at_3"]["no_relevant_reference_queries"] == 1
    assert retrieval["mrr"]["status"] == "PARTIAL_UNSCORABLE_EMPTY_RELEVANCE"
    assert retrieval["mrr"]["value"] == 1.0
    assert retrieval["mrr"]["denominator_queries"] == 1
    assert retrieval["mrr"]["no_relevant_reference_queries"] == 1


def test_mrr_zero_ranked_hit_is_zero_but_empty_relevance_is_not_in_denominator() -> None:
    cases = [make_case("case.zero_rank", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": ["e1"]})]
    trials = [make_trial("case.zero_rank", 1, raw_result={"retrieval": {"logs_present": True, "ranked_evidence_ids": []}})]
    retrieval = compute(cases, trials)["metrics"]["retrieval"]
    assert retrieval["mrr"]["status"] == "COMPUTED"
    assert retrieval["mrr"]["value"] == 0.0
    assert retrieval["mrr"]["denominator_queries"] == 1


def test_zero_denominators_and_wilson_small_samples_are_explicit() -> None:
    assert wilson_interval(0, 0)["status"] == "NOT_COMPUTABLE_ZERO_DENOMINATOR"
    assert wilson_interval(1, 1)["status"] == "INSUFFICIENT_SAMPLE"
    interval = wilson_interval(3, 5)
    assert interval["status"] == "COMPUTED_DESCRIPTIVE"
    assert interval["lower"] < 0.6 < interval["upper"]
    case = make_case("case.no_denominator", "DIRECT", evidence={"reviewed": True, "required_evidence_ids": []})
    report = compute([case], [make_trial("case.no_denominator", 1, actual_route="DIRECT")])
    metric = report["metrics"]["evidence_coverage"]
    assert metric["value"] is None and metric["status"] == "NOT_SCORABLE_ZERO_DENOMINATOR"


def test_evidence_coverage_requires_same_run_authorized_ids_and_authoritative_fact_source() -> None:
    case = make_case("case.authorization", "DIRECT",
        evidence={"reviewed": True, "required_evidence_ids": ["allowed", "private"]},
        key_facts=[{"fact_id": "fact", "reviewed": True}])
    trial = make_trial("case.authorization", 1, raw_result={
        "evidence": {"logs_present": True, "found_evidence_ids": ["allowed", "private"], "authorized_evidence_ids": ["allowed"]},
        "key_facts": [{"fact_id": "fact", "adjudicated": True, "correct": True,
                       "source_authoritative": True, "source_ref": "synthetic.authority.r1"}],
    })
    report = compute([case], [trial])
    assert report["metrics"]["evidence_coverage"]["numerator"] == 1
    assert report["metrics"]["evidence_coverage"]["denominator"] == 2
    assert report["metrics"]["key_fact_correctness"]["value"] == 1.0


def test_missing_evidence_authorization_or_fact_source_is_not_scored_as_pass() -> None:
    case = make_case("case.missing_authority", "DIRECT",
        evidence={"reviewed": True, "required_evidence_ids": ["evidence"]},
        key_facts=[{"fact_id": "fact", "reviewed": True}])
    trial = make_trial("case.missing_authority", 1, raw_result={
        "evidence": {"logs_present": True, "found_evidence_ids": ["evidence"]},
        "key_facts": [{"fact_id": "fact", "adjudicated": True, "correct": True}],
    })
    metrics = compute([case], [trial])["metrics"]
    assert metrics["evidence_coverage"]["status"] == "INCOMPLETE_TELEMETRY"
    assert metrics["evidence_coverage"]["value"] is None
    assert metrics["key_fact_correctness"]["value"] is None
    assert metrics["key_fact_unadjudicated_count"] == 1


def test_duplicate_trials_do_not_inflate_case_count_and_multiple_runs_are_counted() -> None:
    case = make_case("case.repeat", "DIRECT")
    one = make_trial("case.repeat", 1, actual_route="DIRECT", run_ids=["run.a", "run.b"], run_statuses=["SUCCEEDED", "FAILED"])
    two = make_trial("case.repeat", 2, actual_route="DIRECT")
    report = compute([case], [one, two])
    assert report["counts"]["planned_case_count"] == 1
    assert report["counts"]["planned_trial_count"] == 2
    assert report["counts"]["independent_case_count"] == 1
    assert report["counts"]["run_count"] == 3
    assert report["metrics"]["run_success_rate"]["numerator"] == 2
    assert report["metrics"]["run_success_rate"]["denominator"] == 3


def test_report_round_trip_recomputes_deterministically_without_mutating_raw_records() -> None:
    cases = [make_case("case.stable", "DIRECT")]
    trials = [make_trial("case.stable", 1, actual_route="DIRECT")]
    before = deepcopy((cases, trials))
    report = compute(cases, trials)
    encoded = serialize_derived_report(report)
    decoded = json.loads(encoded)
    recomputed = recompute_derived_report(encoded, cases=cases, trials=trials,
        experiment_config={"experiment_id": EXPERIMENT_ID, "budget": {"scope": "synthetic"}},
        dataset_scope="SYNTHETIC_ONLY", recall_ks=(3, 5), metric_contract_version="m6.2-prep-r1",
        metric_contract_sha256=METRIC_CONTRACT_SHA256, metric_contract_path=METRIC_CONTRACT_PATH)
    assert decoded == recomputed
    assert serialize_derived_report(compute(cases, trials)) == encoded
    assert (cases, trials) == before
    changed = deepcopy(trials)
    changed[0]["raw_result"]["route"] = "REFUSE"
    changed_report = compute(cases, changed)
    assert changed_report["provenance"]["raw_records_sha256"] != report["provenance"]["raw_records_sha256"]
    assert changed_report["metrics"]["router"]["confusion_matrix"]["DIRECT"]["REFUSE"] == 1
    with pytest.raises(ReportMismatchError):
        recompute_derived_report(encoded, cases=cases, trials=changed,
            experiment_config={"experiment_id": EXPERIMENT_ID, "budget": {"scope": "synthetic"}},
            dataset_scope="SYNTHETIC_ONLY", recall_ks=(3, 5), metric_contract_version="m6.2-prep-r1",
            metric_contract_sha256=METRIC_CONTRACT_SHA256, metric_contract_path=METRIC_CONTRACT_PATH)


def test_schema_links_and_authorized_scoreability_fail_closed() -> None:
    case = make_case("case.link", "DIRECT")
    trial = make_trial("case.link", 1, actual_route="DIRECT")
    bad = deepcopy(trial)
    bad["trial"]["schema_version"] = "9.9"
    with pytest.raises(MetricInputError, match="invalid EvalTrial"):
        compute([case], [bad])
    bad_link = deepcopy(trial)
    bad_link["run_observations"][0]["run_id"] = "run.other"
    with pytest.raises(MetricInputError, match="not linked"):
        compute([case], [bad_link])
    ineligible_official = make_case("case.unapproved", "DIRECT", scoreability="ELIGIBLE")
    with pytest.raises(MetricInputError, match="approved Gold"):
        aggregate_capabilities(cases=[ineligible_official], trials=[make_trial("case.unapproved", 3)],
            experiment_config={"experiment_id": EXPERIMENT_ID}, dataset_scope="AUTHORIZED_EVALUATION")


def test_calibration_has_no_scores_without_real_human_records() -> None:
    empty = summarize_calibration([])
    assert empty["status"] == empty["formal_status"] == "NOT_RUN"
    assert empty["reason"] == "NO_REAL_HUMAN_CALIBRATION_RECORDS"
    assert empty["record_count"] == 0
    synthetic = [{"case_id": "c", "trial_id": "t", "reviewer_id": "SYNTHETIC_TEST_REVIEWER",
        "reviewer_type": "SYNTHETIC_NOT_HUMAN", "rating": "PASS", "adjudicated": True,
        "reference_rating": "FAIL", "grader_prediction": "PASS"}]
    assert summarize_calibration(synthetic)["status"] == "NOT_RUN"
    assert summarize_calibration(synthetic)["false_accept_rate"] is None


def test_group_bootstrap_resamples_whole_groups_and_is_seed_reproducible() -> None:
    rows = []
    for group in range(12):
        # Correlated duplicate trials are deliberately identical within each group.
        outcome = 1 if group < 6 else 0
        rows.extend({"group_id": f"g{group}", "numerator": outcome, "denominator": 1} for _ in range(4))
    first = cluster_bootstrap_interval(rows, metric="ratio", seed=19, resamples=300, min_groups=10)
    second = cluster_bootstrap_interval(rows, metric="ratio", seed=19, resamples=300, min_groups=10)
    assert first == second
    assert first["status"] == "COMPUTED_GROUP_AWARE_BOOTSTRAP"
    assert first["independent_group_count"] == 12
    assert first["valid_resamples"] == 300
    assert "group_id percentile cluster bootstrap" in first["config"]["method"]


def test_group_bootstrap_reports_missing_groups_small_n_zero_denominator_and_invalid_samples() -> None:
    missing = cluster_bootstrap_interval([{"numerator": 1, "denominator": 1}], metric="ratio", seed=1)
    assert missing["status"] == "NOT_COMPUTABLE_MISSING_GROUP_ID"
    small = cluster_bootstrap_interval([{"group_id": "one", "numerator": 1, "denominator": 1}],
        metric="ratio", seed=1, min_groups=2)
    assert small["status"] == "INSUFFICIENT_INDEPENDENT_GROUPS"
    repeated = cluster_bootstrap_interval([{"group_id": "same-case", "numerator": 1, "denominator": 1} for _ in range(50)],
        metric="ratio", seed=1, min_groups=2)
    assert repeated["status"] == "INSUFFICIENT_INDEPENDENT_GROUPS"
    assert repeated["independent_group_count"] == 1
    zero = cluster_bootstrap_interval([{"group_id": f"g{i}", "numerator": 0, "denominator": 0} for i in range(10)],
        metric="ratio", seed=1, min_groups=10)
    assert zero["status"] == "NOT_COMPUTABLE_ZERO_DENOMINATOR"
    # Macro-F1 is undefined in every resample when required fixed labels have no support.
    route_rows = [{"group_id": f"g{i}", "expected": "DIRECT", "actual": "DIRECT"} for i in range(10)]
    invalid = cluster_bootstrap_interval(route_rows, metric="route_macro_f1", seed=1,
        resamples=100, min_groups=10, min_valid_resamples=1)
    assert invalid["status"] == "NOT_COMPUTABLE_BASE_SAMPLE"
    balanced_routes = [{"group_id": f"route-{label}-{i}", "expected": label, "actual": label}
                       for label in ("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE") for i in range(2)]
    invalid_resamples = cluster_bootstrap_interval(balanced_routes, metric="route_macro_f1", seed=2,
        resamples=100, min_groups=10, min_valid_resamples=100)
    assert invalid_resamples["status"] == "INSUFFICIENT_VALID_RESAMPLES"
    assert invalid_resamples["valid_resamples"] < 100


def test_aggregate_exposes_cluster_intervals_separately_from_wilson() -> None:
    cases, trials = [], []
    for i in range(12):
        cid = f"case.group.{i}"
        case = make_case(cid, "DIRECT", retrieval={"relevance_reviewed": True, "relevant_evidence_ids": [f"evidence.{i}" ]})
        case["case"]["group_id"] = f"independent.{i}"
        cases.append(case)
        trials.append(make_trial(cid, 100 + i, actual_route="DIRECT",
            raw_result={"retrieval": {"logs_present": True, "ranked_evidence_ids": [f"evidence.{i}"]}}))
    report = compute(cases, trials)
    metric = report["metrics"]["router"]["accuracy"]
    assert metric["confidence_interval"]["method"].startswith("Wilson")
    assert metric["group_bootstrap_interval"]["status"] == "COMPUTED_GROUP_AWARE_BOOTSTRAP"
    assert metric["group_bootstrap_interval"]["independent_group_count"] == 12
    assert report["metrics"]["answer_correctness"]["group_bootstrap_interval"]["status"] == "COMPUTED_GROUP_AWARE_BOOTSTRAP"
    assert report["metrics"]["task_success_rate"]["group_bootstrap_interval"]["status"] == "COMPUTED_GROUP_AWARE_BOOTSTRAP"
    assert report["metrics"]["retrieval"]["recall_at_3"]["group_bootstrap_interval"]["status"] == "COMPUTED_GROUP_AWARE_BOOTSTRAP"


def _calibration_record(case_id: str, reviewer: str, rating: str, reference: str, prediction: str,
                        *, adjudication_status: str = "ADJUDICATED") -> dict:
    return {"record_id": f"record.{case_id}.{reviewer}", "case_id": case_id, "trial_id": f"trial.{case_id}",
        "reviewer_id": reviewer, "reviewer_type": "SYNTHETIC", "rating": rating, "independent": True,
        "independence_evidence_ref": "synthetic-only", "rubric_version": "rubric.r1",
        "rated_at_utc": "2026-10-26T00:00:00Z", "sampling_rule_id": "sample.synthetic.r1",
        "adjudication_status": adjudication_status,
        "adjudicator_id": "synthetic.adjudicator" if adjudication_status == "ADJUDICATED" else None,
        "adjudicated_at_utc": "2026-10-26T01:00:00Z" if adjudication_status == "ADJUDICATED" else None,
        "reference_rating": reference if adjudication_status == "ADJUDICATED" else None,
        "grader_prediction": prediction if adjudication_status == "ADJUDICATED" else None}


def test_synthetic_calibration_kappa_alpha_and_false_accept_reject_denominators() -> None:
    records = []
    for case_id, ratings, reference, prediction in (
        ("c1", ("PASS", "PASS"), "PASS", "FAIL"),
        ("c2", ("PASS", "FAIL"), "FAIL", "PASS"),
        ("c3", ("FAIL", "FAIL"), "FAIL", "FAIL"),
    ):
        records.extend([_calibration_record(case_id, f"reviewer.{i}", rating, reference, prediction)
                        for i, rating in enumerate(ratings, 1)])
    result = summarize_calibration(records, data_scope="SYNTHETIC_ONLY")
    assert result["status"] == "SYNTHETIC_VALIDATION_ONLY"
    assert result["formal_status"] == "NOT_RUN"
    assert result["cohen_kappa_nominal"]["value"] == pytest.approx(0.4)
    assert result["cohen_kappa_nominal"]["scale_type"] == "nominal"
    assert result["cohen_kappa_ordinal"]["weighting"] == "quadratic"
    assert result["krippendorff_alpha_nominal"]["value"] == pytest.approx(4 / 9)
    assert result["grader_confusion_matrix"] == {"FAIL": {"FAIL": 1, "PASS": 1}, "PASS": {"FAIL": 1, "PASS": 0}}
    assert result["false_accept_rate"]["numerator"] == 1
    assert result["false_accept_rate"]["denominator"] == 2
    assert result["false_reject_rate"]["numerator"] == 1
    assert result["false_reject_rate"]["denominator"] == 1


def test_calibration_dispute_requires_adjudication_and_missing_ratings_are_omitted() -> None:
    pending = [_calibration_record("c", "r1", "PASS", "PASS", "PASS", adjudication_status="PENDING"),
               _calibration_record("c", "r2", "FAIL", "PASS", "PASS", adjudication_status="PENDING")]
    with pytest.raises(MetricInputError, match="require adjudication"):
        summarize_calibration(pending, data_scope="SYNTHETIC_ONLY")
    partial = [_calibration_record("c", "r1", "PASS", "PASS", "PASS"),
               _calibration_record("c", "r2", "PASS", "PASS", "PASS")]
    # A unit with one available independent rating is omitted from alpha's multi-rated units.
    partial = partial[:1]
    result = summarize_calibration(partial, data_scope="SYNTHETIC_ONLY")
    assert result["krippendorff_alpha_nominal"]["status"] == "NOT_COMPUTABLE_NO_MULTI_RATED_UNITS"


def test_metric_contract_file_hash_and_version_are_locked() -> None:
    path = Path(__file__).resolve().parents[1] / "docs/eval/m6.2-prep-r1/metric-contracts.md"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_metric_contract_file(path, expected_version="m6.2-prep-r1", expected_sha256=digest)["sha256"] == digest
    with pytest.raises(MetricContractMismatchError, match="version mismatch"):
        verify_metric_contract_file(path, expected_version="m6.2-prep-r2", expected_sha256=digest)
    with pytest.raises(MetricContractMismatchError, match="SHA-256 mismatch"):
        verify_metric_contract_file(path, expected_version="m6.2-prep-r1", expected_sha256="0" * 64)
    with pytest.raises(MetricContractMismatchError, match="missing"):
        verify_metric_contract_file(path.with_name("missing-contract.md"), expected_version="m6.2-prep-r1", expected_sha256=digest)


def test_report_recompute_rejects_changed_locked_contract() -> None:
    contract = Path(__file__).resolve().parents[1] / "docs/eval/m6.2-prep-r1/metric-contracts.md"
    digest = hashlib.sha256(contract.read_bytes()).hexdigest()
    cases = [make_case("case.lock", "DIRECT")]
    trials = [make_trial("case.lock", 71, actual_route="DIRECT")]
    config = {"experiment_id": EXPERIMENT_ID, "budget": {"scope": "synthetic"}}
    report = aggregate_capabilities(cases=cases, trials=trials, experiment_config=config,
        dataset_scope="SYNTHETIC_ONLY", metric_contract_version="m6.2-prep-r1", metric_contract_sha256=digest)
    encoded = serialize_derived_report(report)
    assert recompute_derived_report(encoded, cases=cases, trials=trials, experiment_config=config,
        dataset_scope="SYNTHETIC_ONLY", metric_contract_version="m6.2-prep-r1",
        metric_contract_sha256=digest, metric_contract_path=contract)["provenance"]["metric_contract_sha256"] == digest
    with pytest.raises(MetricContractMismatchError, match="metric_contract_path is required"):
        recompute_derived_report(encoded, cases=cases, trials=trials, experiment_config=config,
            dataset_scope="SYNTHETIC_ONLY", metric_contract_version="m6.2-prep-r1",
            metric_contract_sha256=digest)
    with pytest.raises(MetricContractMismatchError, match="SHA-256 mismatch"):
        recompute_derived_report(encoded, cases=cases, trials=trials, experiment_config=config,
            dataset_scope="SYNTHETIC_ONLY", metric_contract_version="m6.2-prep-r1",
            metric_contract_sha256="0" * 64, metric_contract_path=contract)
