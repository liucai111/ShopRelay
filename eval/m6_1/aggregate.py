"""Small-sample-safe synthetic report aggregation; repeated trials never inflate cases."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    EvalCase,
    EvalOutcome,
    EvalTrial,
    GradingStatus,
    LifecycleStatus,
    canonical_json,
    sha256_json,
)


ROUTE_LABELS = ("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE")
_RUN_STATUSES = {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT", "ENV_ERROR"}
_CASE_SCOREABILITY = {"SYNTHETIC_ONLY", "ELIGIBLE", "PENDING_HUMAN_REVIEW", "UNSCORABLE"}
_CASE_KEYS = {"case", "scoreability"}
_TRIAL_KEYS = {"trial", "raw_result", "outcome", "started", "run_observations"}
_SCOREABILITY_KEYS = {"status", "gold_status", "registry_grant_id", "qualification_ref"}
_TRIAL_STATUS_NOT_STARTED = {"NOT_RUN", "BUDGET_STOP"}
_NON_FAILURE_TERMINALS = {"UNSCORABLE", "PENDING_HUMAN_REVIEW"}


class MetricInputError(ValueError):
    """Raised when raw records violate the evaluation/report contract."""


class ReportMismatchError(ValueError):
    """Raised when a serialized derived report cannot be reproduced."""


class MetricContractMismatchError(ValueError):
    """Raised when a locked metric-contract file is missing or has changed."""


def verify_metric_contract_file(path: str | Path, *, expected_version: str,
                                expected_sha256: str) -> dict[str, Any]:
    """Verify the exact contract bytes and declared version before recomputation."""
    target = Path(path)
    if not target.is_file():
        raise MetricContractMismatchError(f"metric contract file is missing: {target}")
    raw = target.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if not isinstance(expected_sha256, str) or actual_sha != expected_sha256.lower():
        raise MetricContractMismatchError(
            f"metric contract SHA-256 mismatch: expected {expected_sha256!r}, actual {actual_sha}")
    text = raw.decode("utf-8")
    declared = re.search(r"^#\s*M6\.2 指标契约\s+(r[0-9]+)\s*$", text, re.MULTILINE)
    if not declared:
        raise MetricContractMismatchError("metric contract has no recognized M6.2 version heading")
    declared_version = f"m6.2-prep-{declared.group(1)}"
    if expected_version != declared_version:
        raise MetricContractMismatchError(
            f"metric contract version mismatch: expected {expected_version!r}, file declares {declared_version!r}")
    return {"version": declared_version, "sha256": actual_sha, "bytes": len(raw), "path": str(target)}


def cluster_bootstrap_interval(observations: Sequence[Mapping[str, Any]], *, metric: str,
                               seed: int, resamples: int = 2000, confidence_level: float = 0.95,
                               min_groups: int = 10, min_valid_resamples: int | None = None) -> dict[str, Any]:
    """Percentile cluster bootstrap. Sampling units are group_id values, not rows."""
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise MetricInputError("resamples must be a positive integer")
    if isinstance(min_groups, bool) or not isinstance(min_groups, int) or min_groups < 2:
        raise MetricInputError("min_groups must be an integer >= 2")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise MetricInputError("seed must be an integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise MetricInputError("seed must be an integer")
    if not 0 < confidence_level < 1:
        raise MetricInputError("confidence_level must be between 0 and 1")
    if metric not in {"ratio", "mean", "route_accuracy", "route_macro_f1"} and not re.fullmatch(r"route_(precision|recall|f1):(DIRECT|SINGLE|MULTI|AGENT|REFUSE)", metric):
        raise MetricInputError(f"unsupported bootstrap metric: {metric}")
    config = {"method": "group_id percentile cluster bootstrap", "metric": metric,
              "seed": seed, "resamples": resamples, "confidence_level": confidence_level,
              "min_groups": min_groups}
    if not observations:
        return {"status": "NOT_COMPUTABLE_NO_OBSERVATIONS", "lower": None, "upper": None,
                "independent_group_count": 0, "valid_resamples": 0, "config": config}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping):
            raise MetricInputError(f"bootstrap observation {index} must be an object")
        group_id = row.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            return {"status": "NOT_COMPUTABLE_MISSING_GROUP_ID", "lower": None, "upper": None,
                    "independent_group_count": None, "valid_resamples": 0,
                    "reason": f"observation {index} has no group_id", "config": config}
        if metric == "ratio":
            numerator, denominator = row.get("numerator"), row.get("denominator")
            if (isinstance(numerator, bool) or not isinstance(numerator, (int, float)) or
                    isinstance(denominator, bool) or not isinstance(denominator, (int, float)) or
                    not math.isfinite(numerator) or not math.isfinite(denominator) or
                    numerator < 0 or denominator < 0 or numerator > denominator):
                raise MetricInputError(f"invalid bootstrap ratio counts at observation {index}")
        elif metric == "mean":
            value = row.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise MetricInputError(f"invalid bootstrap mean value at observation {index}")
        elif metric.startswith("route_"):
            if row.get("expected") not in ROUTE_LABELS or row.get("actual") not in ROUTE_LABELS:
                raise MetricInputError(f"invalid route labels at bootstrap observation {index}")
        grouped.setdefault(group_id, []).append(row)
    group_ids = sorted(grouped)
    group_count = len(group_ids)
    if group_count < min_groups:
        return {"status": "INSUFFICIENT_INDEPENDENT_GROUPS", "lower": None, "upper": None,
                "independent_group_count": group_count, "required_group_count": min_groups,
                "valid_resamples": 0, "config": config}

    def score(rows: list[Mapping[str, Any]]) -> float | None:
        if metric == "ratio":
            numerator = sum(row.get("numerator", 0) for row in rows)
            denominator = sum(row.get("denominator", 0) for row in rows)
            if denominator == 0:
                return None
            return numerator / denominator
        if metric == "mean":
            values = [row.get("value") for row in rows]
            if not values:
                return None
            return sum(values) / len(values)
        if metric == "route_accuracy":
            if not rows:
                return None
            return sum(row.get("expected") == row.get("actual") for row in rows) / len(rows)
        matrix = {actual: {predicted: 0 for predicted in ROUTE_LABELS} for actual in ROUTE_LABELS}
        for row in rows:
            actual, predicted = row.get("expected"), row.get("actual")
            if actual not in ROUTE_LABELS or predicted not in ROUTE_LABELS:
                return None
            matrix[actual][predicted] += 1
        if metric.startswith("route_") and metric != "route_macro_f1":
            kind, label = metric.split(":", 1)
            tp = matrix[label][label]
            support = sum(matrix[label].values())
            predicted_count = sum(matrix[actual][label] for actual in ROUTE_LABELS)
            denominator = predicted_count if kind == "route_precision" else support
            if kind == "route_f1":
                if support == 0:
                    return None
                precision = tp / predicted_count if predicted_count else 0.0
                recall = tp / support
                return 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            return tp / denominator if denominator else None
        scores = []
        for label in ROUTE_LABELS:
            support = sum(matrix[label].values())
            predictions = sum(matrix[actual][label] for actual in ROUTE_LABELS)
            if support == 0:
                return None
            tp = matrix[label][label]
            precision = tp / predictions if predictions else 0.0
            recall = tp / support
            scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
        return sum(scores) / len(ROUTE_LABELS)

    actual_value = score([row for rows in grouped.values() for row in rows])
    if actual_value is None:
        status = "NOT_COMPUTABLE_ZERO_DENOMINATOR" if metric == "ratio" else "NOT_COMPUTABLE_BASE_SAMPLE"
        return {"status": status, "lower": None, "upper": None,
                "independent_group_count": group_count, "valid_resamples": 0, "config": config}
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(resamples):
        selected = [rng.choice(group_ids) for _ in group_ids]
        # Repeated draws deliberately duplicate the entire group's observations.
        sample = [row for group_id in selected for row in grouped[group_id]]
        value = score(sample)
        if value is not None and math.isfinite(value):
            values.append(value)
    required_valid = min_valid_resamples if min_valid_resamples is not None else max(1, math.ceil(resamples * 0.8))
    if len(values) < required_valid:
        return {"status": "INSUFFICIENT_VALID_RESAMPLES", "lower": None, "upper": None,
                "independent_group_count": group_count, "valid_resamples": len(values),
                "required_valid_resamples": required_valid, "config": config}
    alpha = (1 - confidence_level) / 2
    return {"status": "COMPUTED_GROUP_AWARE_BOOTSTRAP", "estimate": actual_value,
            "lower": percentile(values, alpha), "upper": percentile(values, 1 - alpha),
            "independent_group_count": group_count, "valid_resamples": len(values),
            "required_valid_resamples": required_valid,
            "interval_algorithm": "percentile; linear-interpolated empirical quantiles",
            "config": config}


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, Any]:
    """Wilson score interval; deliberately withholds intervals for n < 2."""
    if isinstance(successes, bool) or isinstance(total, bool) or not isinstance(successes, int) or not isinstance(total, int):
        raise MetricInputError("Wilson counts must be integers")
    if total < 0 or successes < 0 or successes > total:
        raise MetricInputError("Wilson counts must satisfy 0 <= successes <= total")
    if total == 0:
        return {"status": "NOT_COMPUTABLE_ZERO_DENOMINATOR", "level": 0.95, "lower": None, "upper": None}
    if total < 2:
        return {"status": "INSUFFICIENT_SAMPLE", "level": 0.95, "lower": None, "upper": None}
    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    half = z * math.sqrt((p * (1 - p) / total) + z2 / (4 * total * total)) / denom
    return {"status": "COMPUTED_DESCRIPTIVE", "level": 0.95,
            "lower": max(0.0, center - half), "upper": min(1.0, center + half),
            "method": "Wilson score interval; trial-level, not cluster-adjusted"}


def _ratio(successes: int, total: int, *, reason: str | None = None,
           confidence_interval: bool = True) -> dict[str, Any]:
    if total == 0:
        status = "NOT_SCORABLE_ZERO_DENOMINATOR"
        interval = wilson_interval(0, 0) if confidence_interval else {
            "status": "NOT_APPLICABLE_NON_BINOMIAL", "level": None, "lower": None, "upper": None}
        return {"status": status, "numerator": successes, "denominator": total,
                "value": None, "reason": reason or status, "confidence_interval": interval}
    status = "COMPUTED"
    interval = wilson_interval(successes, total) if confidence_interval else {
        "status": "NOT_APPLICABLE_NON_BINOMIAL", "level": None, "lower": None, "upper": None}
    return {"status": status, "numerator": successes, "denominator": total,
            "value": successes / total, "reason": None,
            "confidence_interval": interval}


def _validate_bundle(cases: Sequence[Mapping[str, Any]], trials: Sequence[Mapping[str, Any]],
                     experiment_config: Mapping[str, Any], *, dataset_scope: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if dataset_scope not in {"SYNTHETIC_ONLY", "AUTHORIZED_EVALUATION"}:
        raise MetricInputError("unsupported dataset_scope")
    if not isinstance(experiment_config, Mapping) or not isinstance(experiment_config.get("experiment_id"), str):
        raise MetricInputError("experiment_config requires experiment_id")
    parsed_cases: dict[str, dict[str, Any]] = {}
    for row in cases:
        if not isinstance(row, Mapping) or set(row) != _CASE_KEYS:
            raise MetricInputError("case row must contain exactly case and scoreability")
        try:
            case = EvalCase.from_dict(row["case"])
        except (ContractError, TypeError) as exc:
            raise MetricInputError(f"invalid EvalCase: {exc}") from exc
        scoreability = row["scoreability"]
        if not isinstance(scoreability, Mapping) or not set(scoreability) <= _SCOREABILITY_KEYS or "status" not in scoreability:
            raise MetricInputError(f"invalid scoreability metadata for {case.case_id}")
        state = scoreability["status"]
        if state not in _CASE_SCOREABILITY:
            raise MetricInputError(f"unsupported scoreability status for {case.case_id}: {state}")
        if state == "SYNTHETIC_ONLY" and dataset_scope != "SYNTHETIC_ONLY":
            raise MetricInputError("SYNTHETIC_ONLY case requires SYNTHETIC_ONLY dataset scope")
        if state == "ELIGIBLE":
            if dataset_scope != "AUTHORIZED_EVALUATION" or scoreability.get("gold_status") != "APPROVED":
                raise MetricInputError("ELIGIBLE case requires authorized scope and approved Gold")
            if not scoreability.get("registry_grant_id") or not scoreability.get("qualification_ref"):
                raise MetricInputError("ELIGIBLE case requires Registry grant and qualification reference")
        if case.case_id in parsed_cases:
            raise MetricInputError(f"duplicate case_id: {case.case_id}")
        parsed_cases[case.case_id] = {"model": case, "scoreability": dict(scoreability)}

    parsed_trials: dict[str, dict[str, Any]] = {}
    seen_runs: set[str] = set()
    for row in trials:
        if not isinstance(row, Mapping) or set(row) != _TRIAL_KEYS:
            raise MetricInputError("trial row must contain exactly trial, raw_result, outcome, started, run_observations")
        try:
            trial = EvalTrial.from_dict(row["trial"])
        except (ContractError, TypeError) as exc:
            raise MetricInputError(f"invalid EvalTrial: {exc}") from exc
        if trial.experiment_id != experiment_config["experiment_id"]:
            raise MetricInputError(f"experiment_id mismatch for trial {trial.trial_id}")
        if trial.case_id not in parsed_cases:
            raise MetricInputError(f"unknown case_id {trial.case_id} for trial {trial.trial_id}")
        if trial.trial_id in parsed_trials:
            raise MetricInputError(f"duplicate trial_id: {trial.trial_id}")
        started = row["started"]
        if not isinstance(started, bool):
            raise MetricInputError(f"started must be boolean for {trial.trial_id}")
        if trial.lifecycle_status == "NOT_RUN" and started:
            raise MetricInputError(f"NOT_RUN trial cannot be marked started: {trial.trial_id}")
        if trial.lifecycle_status == "UNKNOWN_RESULT" and not started:
            raise MetricInputError(f"UNKNOWN_RESULT trial must be marked started: {trial.trial_id}")
        raw_result = row["raw_result"]
        if raw_result is not None and not isinstance(raw_result, Mapping):
            raise MetricInputError(f"raw_result must be object or null for {trial.trial_id}")
        outcome_data = row["outcome"]
        try:
            outcome = EvalOutcome.from_dict(outcome_data) if outcome_data is not None else None
        except (ContractError, TypeError) as exc:
            raise MetricInputError(f"invalid EvalOutcome for {trial.trial_id}: {exc}") from exc
        if outcome is not None and outcome.trial_id != trial.trial_id:
            raise MetricInputError(f"outcome trial_id mismatch for {trial.trial_id}")
        observations = row["run_observations"]
        if not isinstance(observations, list):
            raise MetricInputError(f"run_observations must be a list for {trial.trial_id}")
        local_ids: set[str] = set()
        for obs in observations:
            if not isinstance(obs, Mapping) or set(obs) != {"run_id", "status"}:
                raise MetricInputError(f"run observation must contain run_id and status for {trial.trial_id}")
            run_id, status = obs["run_id"], obs["status"]
            if run_id is not None and (not isinstance(run_id, str) or run_id not in trial.run_ids):
                raise MetricInputError(f"run observation is not linked to trial {trial.trial_id}")
            if status not in _RUN_STATUSES:
                raise MetricInputError(f"unsupported run observation status for {trial.trial_id}: {status}")
            if run_id is not None:
                if run_id in local_ids or run_id in seen_runs:
                    raise MetricInputError(f"duplicate run observation: {run_id}")
                local_ids.add(run_id)
                seen_runs.add(run_id)
        if local_ids != set(trial.run_ids):
            raise MetricInputError(f"run observation IDs do not match EvalTrial.run_ids for {trial.trial_id}")
        parsed_trials[trial.trial_id] = {
            "model": trial, "raw_result": deepcopy(raw_result), "outcome": outcome,
            "started": started, "run_observations": deepcopy(observations),
        }
    return parsed_cases, parsed_trials


def _eligible(case_row: Mapping[str, Any], dataset_scope: str) -> tuple[bool, str | None]:
    state = case_row["scoreability"]["status"]
    if state == "SYNTHETIC_ONLY" and dataset_scope == "SYNTHETIC_ONLY":
        return True, None
    if state == "ELIGIBLE" and dataset_scope == "AUTHORIZED_EVALUATION":
        return True, None
    return False, f"CASE_SCOREABILITY_{state}"


def _group_bootstrap_metrics(case_rows: Mapping[str, Mapping[str, Any]],
                             trial_rows: Mapping[str, Mapping[str, Any]],
                             scoreable_trial_ids: set[str], recall_ks: Sequence[int], *,
                             seed: int, resamples: int, confidence_level: float,
                             min_groups: int) -> dict[str, Any]:
    """Build group-level resampling inputs from the same reviewed/eligible records as metrics."""
    route_rows: list[dict[str, Any]] = []
    retrieval: dict[int, list[dict[str, Any]]] = {k: [] for k in recall_ks}
    mrr_rows: list[dict[str, Any]] = []
    mrr_at_k_rows: dict[int, list[dict[str, Any]]] = {k: [] for k in recall_ks}
    evidence_rows: list[dict[str, Any]] = []
    fact_rows: list[dict[str, Any]] = []
    unsupported_rows: list[dict[str, Any]] = []
    answer_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    for trial_id, row in trial_rows.items():
        trial = row["model"]
        case_row = case_rows[trial.case_id]
        case = case_row["model"]
        group = case.group_id
        raw = row["raw_result"] or {}
        eligible, _ = _eligible(case_row, "SYNTHETIC_ONLY" if case_row["scoreability"]["status"] == "SYNTHETIC_ONLY" else "AUTHORIZED_EVALUATION")
        if trial_id in scoreable_trial_ids:
            expected_route = case.expected_outcome.get("route")
            actual_route = raw.get("route")
            if expected_route in ROUTE_LABELS and actual_route in ROUTE_LABELS:
                route_rows.append({"group_id": group, "expected": expected_route, "actual": actual_route})
        for obs in row["run_observations"]:
            if obs.get("run_id") is not None:
                run_rows.append({"group_id": group, "numerator": int(obs["status"] == "SUCCEEDED"), "denominator": 1})
        if row["started"] and trial.lifecycle_status == "UNKNOWN_RESULT" and not row["run_observations"]:
            run_rows.append({"group_id": group, "numerator": 0, "denominator": 1})
        if not eligible or trial.lifecycle_status in _TRIAL_STATUS_NOT_STARTED | _NON_FAILURE_TERMINALS:
            continue
        expected = case.expected_outcome
        retrieval_gold = expected.get("retrieval", {})
        retrieval_raw = raw.get("retrieval")
        if isinstance(retrieval_gold, Mapping) and retrieval_gold.get("relevance_reviewed") is True:
            relevant = retrieval_gold.get("relevant_evidence_ids", [])
            if relevant and isinstance(retrieval_raw, Mapping) and retrieval_raw.get("logs_present") is True:
                ranked = retrieval_raw.get("ranked_evidence_ids", [])
                if isinstance(ranked, list):
                    first = next((index for index, item in enumerate(ranked, 1) if item in set(relevant)), None)
                    mrr_rows.append({"group_id": group, "value": 1 / first if first else 0.0})
                    for k in recall_ks:
                        first_at_k = next((index for index, item in enumerate(ranked[:k], 1) if item in set(relevant)), None)
                        mrr_at_k_rows[k].append({"group_id": group, "value": 1 / first_at_k if first_at_k else 0.0})
                        retrieval[k].append({"group_id": group,
                            "numerator": len(set(ranked[:k]) & set(relevant)), "denominator": len(relevant)})
        evidence_gold = expected.get("evidence", {})
        evidence_raw = raw.get("evidence")
        if isinstance(evidence_gold, Mapping) and evidence_gold.get("reviewed") is True and isinstance(evidence_raw, Mapping) and evidence_raw.get("logs_present") is True:
            required = evidence_gold.get("required_evidence_ids", [])
            authorized = evidence_raw.get("authorized_evidence_ids")
            found = evidence_raw.get("found_evidence_ids", [])
            if required and isinstance(authorized, list) and isinstance(found, list):
                evidence_rows.append({"group_id": group,
                    "numerator": len(set(required) & set(found) & set(authorized)), "denominator": len(required)})
        gold_facts = expected.get("key_facts", [])
        observed_facts = raw.get("key_facts", [])
        if isinstance(gold_facts, (list, tuple)) and isinstance(observed_facts, list):
            by_id = {item.get("fact_id"): item for item in observed_facts if isinstance(item, Mapping)}
            valid = [by_id.get(fact.get("fact_id")) for fact in gold_facts if isinstance(fact, Mapping) and fact.get("reviewed") is True]
            valid = [fact for fact in valid if isinstance(fact, Mapping) and fact.get("adjudicated") is True
                     and fact.get("source_authoritative") is True and isinstance(fact.get("source_ref"), str) and fact.get("source_ref")]
            if valid:
                fact_rows.append({"group_id": group, "numerator": sum(item.get("correct") is True for item in valid), "denominator": len(valid)})
        claims = raw.get("claims", [])
        valid_claims = [claim for claim in claims if isinstance(claim, Mapping) and claim.get("adjudicated") is True
                        and isinstance(claim.get("supported"), bool)] if isinstance(claims, list) else []
        if valid_claims:
            unsupported_rows.append({"group_id": group,
                "numerator": sum(item["supported"] is False for item in valid_claims), "denominator": len(valid_claims)})
        outcome = row["outcome"]
        if outcome is not None and trial.lifecycle_status == "SUCCEEDED":
            if outcome.answer_correctness in {"PASS", "FAIL"}:
                answer_rows.append({"group_id": group, "numerator": int(outcome.answer_correctness == "PASS"), "denominator": 1})
            if outcome.task_success in {"PASS", "FAIL"}:
                task_rows.append({"group_id": group, "numerator": int(outcome.task_success == "PASS"), "denominator": 1})

    config = {"seed": seed, "resamples": resamples, "confidence_level": confidence_level, "min_groups": min_groups}
    result: dict[str, Any] = {"config": config, "router": {
        "accuracy": cluster_bootstrap_interval(route_rows, metric="route_accuracy", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups)
            if route_rows else cluster_bootstrap_interval([], metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "macro_f1": cluster_bootstrap_interval(route_rows, metric="route_macro_f1", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "per_class": {label: {kind: cluster_bootstrap_interval(route_rows, metric=f"route_{kind}:{label}", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups)
                               for kind in ("precision", "recall", "f1")} for label in ROUTE_LABELS}},
        "retrieval": {f"recall_at_{k}": cluster_bootstrap_interval(rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups)
                      for k, rows in retrieval.items()},
        "mrr": cluster_bootstrap_interval(mrr_rows, metric="mean", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "mrr_at_k": {f"mrr_at_{k}": cluster_bootstrap_interval(rows, metric="mean", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups)
                     for k, rows in mrr_at_k_rows.items()},
        "evidence_coverage": cluster_bootstrap_interval(evidence_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "key_fact_correctness": cluster_bootstrap_interval(fact_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "unsupported_assertion_rate": cluster_bootstrap_interval(unsupported_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "run_success_rate": cluster_bootstrap_interval(run_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "answer_correctness": cluster_bootstrap_interval(answer_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups),
        "task_success_rate": cluster_bootstrap_interval(task_rows, metric="ratio", seed=seed, resamples=resamples, confidence_level=confidence_level, min_groups=min_groups)}
    return result


def _kappa(rows: Sequence[tuple[str, str]], *, ordinal: bool) -> dict[str, Any]:
    if not rows:
        return {"status": "NOT_COMPUTABLE_NO_DOUBLE_SCORED_UNITS", "value": None, "unit_count": 0}
    scale = ("FAIL", "PASS")
    observed = sum(left == right for left, right in rows) / len(rows)
    left_counts = Counter(left for left, _ in rows)
    right_counts = Counter(right for _, right in rows)
    expected = sum(left_counts[value] * right_counts[value] for value in scale) / (len(rows) ** 2)
    if ordinal:
        # Quadratic weighted disagreement for the declared binary ordinal scale FAIL < PASS.
        distance = {(a, b): ((scale.index(a) - scale.index(b)) / (len(scale) - 1)) ** 2
                    for a in scale for b in scale}
        observed_disagreement = sum(distance[(left, right)] for left, right in rows) / len(rows)
        expected_disagreement = sum(
            left_counts[a] * right_counts[b] * distance[(a, b)]
            for a in scale for b in scale) / (len(rows) ** 2)
        if expected_disagreement == 0:
            return {"status": "NOT_COMPUTABLE_ZERO_EXPECTED_DISAGREEMENT", "value": None,
                    "unit_count": len(rows), "scale_type": "ordinal", "weighting": "quadratic"}
        return {"status": "COMPUTED", "value": 1 - observed_disagreement / expected_disagreement,
                "unit_count": len(rows), "scale_type": "ordinal", "weighting": "quadratic",
                "rating_order": list(scale)}
    if expected == 1:
        return {"status": "NOT_COMPUTABLE_ZERO_EXPECTED_DISAGREEMENT", "value": None,
                "unit_count": len(rows), "scale_type": "nominal", "weighting": "unweighted"}
    return {"status": "COMPUTED", "value": (observed - expected) / (1 - expected),
            "unit_count": len(rows), "scale_type": "nominal", "weighting": "unweighted"}


def _krippendorff_alpha(units: Sequence[Sequence[str]], *, ordinal: bool) -> dict[str, Any]:
    units = [unit for unit in units if len(unit) >= 2]
    ratings = [rating for unit in units for rating in unit]
    if not ratings:
        return {"status": "NOT_COMPUTABLE_NO_MULTI_RATED_UNITS", "value": None, "unit_count": 0,
                "scale_type": "ordinal" if ordinal else "nominal", "missing_policy": "omit missing ratings; retain units with >=2 ratings"}
    categories = sorted(set(ratings))
    scale = ["FAIL", "PASS"] if set(categories) <= {"FAIL", "PASS"} else categories
    def distance(a: str, b: str) -> float:
        if not ordinal:
            return 0.0 if a == b else 1.0
        return ((scale.index(a) - scale.index(b)) / max(1, len(scale) - 1)) ** 2
    observed_numerator = 0.0
    observed_denominator = 0
    for unit in units:
        for i, a in enumerate(unit):
            for b in unit[i + 1:]:
                observed_numerator += distance(a, b)
                observed_denominator += 1
    do = observed_numerator / observed_denominator if observed_denominator else 0.0
    counts = Counter(ratings)
    total = len(ratings)
    de = sum(counts[a] * counts[b] * distance(a, b) for a in categories for b in categories) / (total * (total - 1)) if total > 1 else 0.0
    if de == 0:
        return {"status": "NOT_COMPUTABLE_ZERO_EXPECTED_DISAGREEMENT", "value": None,
                "unit_count": len(units), "rating_count": total,
                "scale_type": "ordinal" if ordinal else "nominal",
                "weighting": "quadratic" if ordinal else "unit disagreement",
                "missing_policy": "omit missing ratings; retain units with >=2 ratings"}
    return {"status": "COMPUTED", "value": 1 - do / de, "unit_count": len(units),
            "rating_count": total, "scale_type": "ordinal" if ordinal else "nominal",
            "weighting": "quadratic" if ordinal else "unit disagreement",
            "missing_policy": "omit missing ratings; retain units with >=2 ratings"}


def summarize_calibration(records: Sequence[Mapping[str, Any]] | None, *,
                          data_scope: str = "AUTHORIZED_EVALUATION",
                          verified_human_record_ids: Sequence[str] = ()) -> dict[str, Any]:
    """Validate calibration provenance and compute agreement/error summaries.

    Synthetic records may exercise the formulas, but their formal status remains NOT_RUN.
    """
    records = list(records or [])
    if data_scope not in {"SYNTHETIC_ONLY", "AUTHORIZED_EVALUATION"}:
        raise MetricInputError("unsupported calibration data_scope")
    if isinstance(verified_human_record_ids, (str, bytes)) or any(not isinstance(item, str) for item in verified_human_record_ids):
        raise MetricInputError("verified_human_record_ids must be a sequence of record IDs")
    if not records:
        return {"status": "NOT_RUN", "formal_status": "NOT_RUN", "reason": "NO_REAL_HUMAN_CALIBRATION_RECORDS",
                "cohen_kappa_nominal": None, "cohen_kappa_ordinal": None, "krippendorff_alpha": None,
                "grader_confusion_matrix": None, "false_accept_rate": None, "false_reject_rate": None,
                "record_count": 0}
    required = {"record_id", "case_id", "trial_id", "reviewer_id", "reviewer_type", "rating", "independent",
                "independence_evidence_ref", "rubric_version", "rated_at_utc", "sampling_rule_id",
                "adjudication_status", "adjudicator_id", "adjudicated_at_utc", "reference_rating", "grader_prediction"}
    # Historical minimal records are retained as not-run, rather than reinterpreted as verified reviews.
    if any(not isinstance(item, Mapping) or set(item) != required for item in records):
        legacy_keys = {"case_id", "trial_id", "reviewer_id", "reviewer_type", "rating", "adjudicated", "reference_rating", "grader_prediction"}
        if all(isinstance(item, Mapping) and set(item) == legacy_keys for item in records):
            return {"status": "NOT_RUN", "formal_status": "NOT_RUN", "reason": "CALIBRATION_PROVENANCE_SCHEMA_INCOMPLETE",
                    "cohen_kappa_nominal": None, "cohen_kappa_ordinal": None, "krippendorff_alpha": None,
                    "grader_confusion_matrix": None, "false_accept_rate": None, "false_reject_rate": None,
                    "record_count": len(records)}
        raise MetricInputError("calibration record has invalid fields")
    for item in records:
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("case_id", "trial_id", "reviewer_id", "reviewer_type", "rubric_version", "sampling_rule_id")):
            raise MetricInputError("calibration identifiers and rubric/sampling versions must be non-empty strings")
        if item["rating"] not in {"PASS", "FAIL"} or not isinstance(item["independent"], bool):
            raise MetricInputError("calibration ratings must be PASS/FAIL and independent must be boolean")
        try:
            parsed = datetime.fromisoformat(item["rated_at_utc"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise MetricInputError("rated_at_utc must be ISO-8601 with UTC offset") from exc
        if parsed.utcoffset() is None:
            raise MetricInputError("rated_at_utc must include UTC offset")
        if item["adjudication_status"] not in {"PENDING", "NOT_REQUIRED", "ADJUDICATED"}:
            raise MetricInputError("invalid adjudication_status")
        if item["adjudication_status"] == "ADJUDICATED":
            if not item["adjudicator_id"] or not item["adjudicated_at_utc"]:
                raise MetricInputError("adjudicated record requires adjudicator identity and timestamp")
            if item["reference_rating"] not in {"PASS", "FAIL"} or item["grader_prediction"] not in {"PASS", "FAIL"}:
                raise MetricInputError("adjudicated record requires reference_rating and grader_prediction")
            try:
                adjudicated_time = datetime.fromisoformat(item["adjudicated_at_utc"].replace("Z", "+00:00"))
            except (ValueError, AttributeError) as exc:
                raise MetricInputError("adjudicated_at_utc must be ISO-8601 with UTC offset") from exc
            if adjudicated_time.utcoffset() is None:
                raise MetricInputError("adjudicated_at_utc must include UTC offset")
        elif any(item[field] is not None for field in ("adjudicator_id", "adjudicated_at_utc", "reference_rating", "grader_prediction")):
            raise MetricInputError("non-adjudicated record cannot carry adjudication fields")
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in records:
        grouped.setdefault((item["case_id"], item["trial_id"]), []).append(item)
    pair_ratings: list[tuple[str, str]] = []
    alpha_units: list[list[str]] = []
    adjudicated: dict[tuple[str, str], Mapping[str, Any]] = {}
    for key, rows in grouped.items():
        ids = [row["reviewer_id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise MetricInputError(f"duplicate reviewer rating for {key}")
        if any(row["independent"] is not True for row in rows):
            continue
        ratings = [row["rating"] for row in rows]
        if len(ratings) >= 2:
            alpha_units.append(ratings)
        if len(ratings) == 2:
            pair_ratings.append((ratings[0], ratings[1]))
        if len(set(ratings)) > 1 and not any(row["adjudication_status"] == "ADJUDICATED" for row in rows):
            raise MetricInputError(f"disputed independent ratings require adjudication for {key}")
        adjudicated_rows = [row for row in rows if row["adjudication_status"] == "ADJUDICATED"]
        if adjudicated_rows:
            projections = {(row["reference_rating"], row["grader_prediction"], row["adjudicator_id"], row["adjudicated_at_utc"]) for row in adjudicated_rows}
            if len(projections) != 1:
                raise MetricInputError(f"inconsistent adjudication projection for {key}")
            if len(set(ratings)) > 1 and adjudicated_rows[0]["adjudicator_id"] in set(ids):
                raise MetricInputError(f"disputed ratings require a separate adjudicator for {key}")
            adjudicated[key] = adjudicated_rows[0]

    record_ids = [item["record_id"] for item in records]
    if len(record_ids) != len(set(record_ids)):
        raise MetricInputError("duplicate calibration record_id")
    real_independently_verified = all(
        item["reviewer_type"] == "HUMAN" and item["reviewer_id"] not in {"", "UNKNOWN"}
        and item["independent"] is True and item["independence_evidence_ref"]
        and item["record_id"] in set(verified_human_record_ids)
        for item in records)
    if data_scope != "SYNTHETIC_ONLY" and not real_independently_verified:
        return {"status": "NOT_RUN", "formal_status": "NOT_RUN", "reason": "NO_VERIFIED_INDEPENDENT_HUMAN_RATINGS",
                "cohen_kappa_nominal": None, "cohen_kappa_ordinal": None, "krippendorff_alpha": None,
                "grader_confusion_matrix": None, "false_accept_rate": None, "false_reject_rate": None,
                "record_count": len(records)}
    confusion = {actual: {pred: 0 for pred in ("FAIL", "PASS")} for actual in ("FAIL", "PASS")}
    for item in adjudicated.values():
        confusion[item["reference_rating"]][item["grader_prediction"]] += 1
    false_accept = confusion["FAIL"]["PASS"]
    false_reject = confusion["PASS"]["FAIL"]
    total_reference_fail = sum(confusion["FAIL"].values())
    total_reference_pass = sum(confusion["PASS"].values())
    computed = {"status": "SYNTHETIC_VALIDATION_ONLY" if data_scope == "SYNTHETIC_ONLY" else "COMPUTED_FROM_VERIFIED_HUMAN_RECORDS",
        "formal_status": "NOT_RUN" if data_scope == "SYNTHETIC_ONLY" else "COMPUTED",
        "cohen_kappa_nominal": _kappa(pair_ratings, ordinal=False),
        "cohen_kappa_ordinal": _kappa(pair_ratings, ordinal=True),
        "krippendorff_alpha_nominal": _krippendorff_alpha(alpha_units, ordinal=False),
        "krippendorff_alpha_ordinal": _krippendorff_alpha(alpha_units, ordinal=True),
        "grader_confusion_matrix": confusion,
        "false_accept_rate": _ratio(false_accept, total_reference_fail, reason="NO_REFERENCE_FAIL_CASES"),
        "false_reject_rate": _ratio(false_reject, total_reference_pass, reason="NO_REFERENCE_PASS_CASES"),
        "false_accept_denominator": "adjudicated reference FAIL units",
        "false_reject_denominator": "adjudicated reference PASS units",
        "record_count": len(records), "adjudicated_case_count": len(adjudicated),
        "reviewer_count": len({item["reviewer_id"] for item in records}),
        "verified_human_record_count": len(set(verified_human_record_ids) & set(record_ids)) if data_scope != "SYNTHETIC_ONLY" else 0,
        "rubric_versions": sorted({item["rubric_version"] for item in records}),
        "sampling_rules": sorted({item["sampling_rule_id"] for item in records}),
        "note": "Synthetic output validates arithmetic only; reviewer independence and real calibration are not inferred.",
    }
    computed["product_quality_claim"] = False
    return computed


def aggregate_capabilities(*, cases: Sequence[Mapping[str, Any]], trials: Sequence[Mapping[str, Any]],
                           experiment_config: Mapping[str, Any], dataset_scope: str,
                           recall_ks: Sequence[int] = (3, 5),
                           calibration_records: Sequence[Mapping[str, Any]] | None = None,
                           verified_human_record_ids: Sequence[str] = (),
                           metric_contract_version: str = "m6.2-prep-r1",
                           metric_contract_sha256: str | None = None,
                           bootstrap_seed: int = 20261026, bootstrap_resamples: int = 2000,
                           bootstrap_confidence_level: float = 0.95,
                           bootstrap_min_groups: int = 10) -> dict[str, Any]:
    """Compute deterministic M6.2 metrics from validated raw records; never mutates inputs."""
    if not recall_ks or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in recall_ks):
        raise MetricInputError("recall_ks must contain positive integers")
    if len(set(recall_ks)) != len(recall_ks):
        raise MetricInputError("recall_ks must be unique")
    case_rows, trial_rows = _validate_bundle(cases, trials, experiment_config, dataset_scope=dataset_scope)
    if not case_rows:
        raise MetricInputError("at least one planned Case is required")
    if not metric_contract_version:
        raise MetricInputError("metric_contract_version is required")
    if not isinstance(metric_contract_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", metric_contract_sha256):
        raise MetricContractMismatchError("metric_contract_sha256 must be a locked 64-character SHA-256")
    if isinstance(verified_human_record_ids, (str, bytes)) or any(not isinstance(item, str) for item in verified_human_record_ids):
        raise MetricInputError("verified_human_record_ids must be a sequence of record IDs")

    status_counts = Counter(row["model"].lifecycle_status for row in trial_rows.values())
    started_rows = [row for row in trial_rows.values() if row["started"]]
    run_observations = [obs for row in trial_rows.values() for obs in row["run_observations"]]
    missing_run_status = sum(1 for row in started_rows if not row["run_observations"])
    run_status_counts = Counter(obs["status"] for obs in run_observations)
    known_run_ids = {run_id for row in trial_rows.values() for run_id in row["model"].run_ids}
    unique_scoreable_cases = {
        case_id for case_id, case_row in case_rows.items() if _eligible(case_row, dataset_scope)[0]
    }
    scoreable_trial_ids = {
        trial_id for trial_id, row in trial_rows.items()
        if _eligible(case_rows[row["model"].case_id], dataset_scope)[0]
        and row["model"].lifecycle_status not in _TRIAL_STATUS_NOT_STARTED | _NON_FAILURE_TERMINALS
    }

    # Fixed five-label route matrix. Classes are never dropped for zero support.
    matrix = {actual: {predicted: 0 for predicted in ROUTE_LABELS} for actual in ROUTE_LABELS}
    route_missing = 0
    for trial_id in scoreable_trial_ids:
        row = trial_rows[trial_id]
        raw = row["raw_result"] or {}
        expected = case_rows[row["model"].case_id]["model"].expected_outcome.get("route")
        actual = raw.get("route")
        if not isinstance(expected, str) or expected not in ROUTE_LABELS or not isinstance(actual, str) or actual not in ROUTE_LABELS:
            route_missing += 1
            continue
        matrix[expected][actual] += 1
    per_class: dict[str, Any] = {}
    macro_values: list[float] = []
    zero_support: list[str] = []
    total_route = sum(sum(row.values()) for row in matrix.values())
    correct_route = 0
    for label in ROUTE_LABELS:
        tp = matrix[label][label]
        support = sum(matrix[label].values())
        predicted = sum(matrix[actual][label] for actual in ROUTE_LABELS)
        correct_route += tp
        precision = _ratio(tp, predicted, reason="NO_PREDICTIONS_FOR_CLASS")
        recall = _ratio(tp, support, reason="NO_EXPECTED_SUPPORT_FOR_CLASS")
        if support == 0:
            zero_support.append(label)
            f1 = None
        else:
            p = tp / predicted if predicted else 0.0
            r = tp / support
            f1 = 2 * p * r / (p + r) if p + r else 0.0
            macro_values.append(f1)
        per_class[label] = {"support": support, "predicted_count": predicted,
                            "precision": precision, "recall": recall,
                            "f1": f1, "f1_status": "COMPUTED" if support else "NOT_SCORABLE_ZERO_SUPPORT"}
    macro = (sum(macro_values) / len(ROUTE_LABELS)) if not zero_support else None
    route = {
        "status": "COMPUTED" if not zero_support else "NOT_SCORABLE_MISSING_CLASS_SUPPORT",
        "labels": list(ROUTE_LABELS), "confusion_matrix": matrix, "per_class": per_class,
        "macro_f1": macro, "macro_f1_denominator_classes": len(ROUTE_LABELS),
        "macro_f1_reason": "REQUIRED_ROUTE_CLASS_HAS_ZERO_SUPPORT" if zero_support else None,
        "macro_f1_confidence_interval": {"status": "NOT_APPLICABLE_NON_BINOMIAL", "level": None, "lower": None, "upper": None},
        "zero_support_classes": zero_support, "missing_route_observation_count": route_missing,
        "accuracy": _ratio(correct_route, total_route, reason="NO_SCORABLE_ROUTE_OBSERVATIONS"),
    }

    retrieval_counts = {k: {"hits": 0, "relevant": 0, "reciprocal_rank_sum": 0.0, "queries": 0,
                            "missing_logs": 0, "unreviewed_relevance": 0, "no_relevant_reference": 0,
                            "zero_hit_queries": 0, "unqualified_cases": 0} for k in recall_ks}
    mrr_total = {"reciprocal_rank_sum": 0.0, "queries": 0, "missing_logs": 0,
                 "unreviewed_relevance": 0, "no_relevant_reference": 0, "unqualified_cases": 0}
    evidence_found = evidence_required = 0
    evidence_missing_logs = 0
    evidence_unqualified_cases = 0
    fact_correct = fact_total = 0
    fact_unadjudicated = 0
    fact_unqualified_count = 0
    unsupported = claim_total = 0
    claims_unadjudicated = 0
    claims_unqualified_count = 0
    unauthorized_effects = 0
    safety_checked_trials = 0
    unsafe_trials = 0
    answer_pass = answer_total = answer_unknown = 0
    task_pass = task_total = task_unknown = 0
    answer_excluded_reasons: Counter[str] = Counter()
    task_excluded_reasons: Counter[str] = Counter()

    for trial_id, row in trial_rows.items():
        trial = row["model"]
        case_row = case_rows[trial.case_id]
        raw = row["raw_result"] or {}
        eligible, reason = _eligible(case_row, dataset_scope)
        safety = raw.get("safety")
        if isinstance(safety, Mapping) and safety.get("checked") is True:
            count = safety.get("unauthorized_side_effects")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise MetricInputError(f"invalid unauthorized_side_effects for {trial_id}")
            safety_checked_trials += 1
            unauthorized_effects += count
            unsafe_trials += count > 0
        if trial_id not in scoreable_trial_ids and eligible:
            continue
        outcome = row["outcome"]
        if outcome is not None and trial.lifecycle_status == "SUCCEEDED" and eligible:
            if outcome.answer_correctness in {"PASS", "FAIL"}:
                answer_total += 1
                answer_pass += outcome.answer_correctness == "PASS"
            else:
                answer_unknown += 1
                answer_excluded_reasons["ANSWER_VERDICT_UNKNOWN_OR_NOT_APPLICABLE"] += 1
            if outcome.task_success in {"PASS", "FAIL"}:
                task_total += 1
                task_pass += outcome.task_success == "PASS"
            else:
                task_unknown += 1
                task_excluded_reasons["TASK_VERDICT_UNKNOWN_OR_NOT_APPLICABLE"] += 1
        elif trial.lifecycle_status == "SUCCEEDED":
            answer_excluded_reasons[reason or "CASE_NOT_SCOREABLE"] += 1
            task_excluded_reasons[reason or "CASE_NOT_SCOREABLE"] += 1

        expected = case_row["model"].expected_outcome
        retrieval_gold = expected.get("retrieval", {})
        retrieval_raw = raw.get("retrieval")
        if isinstance(retrieval_gold, Mapping) and retrieval_gold.get("relevance_reviewed") is True:
            relevant = retrieval_gold.get("relevant_evidence_ids", [])
            if not isinstance(relevant, (list, tuple)) or len(relevant) != len(set(relevant)):
                raise MetricInputError(f"invalid relevant_evidence_ids for {trial.case_id}")
            if not eligible:
                mrr_total["unqualified_cases"] += 1
                for data in retrieval_counts.values(): data["unqualified_cases"] += 1
            elif not relevant:
                mrr_total["no_relevant_reference"] += 1
                for data in retrieval_counts.values(): data["no_relevant_reference"] += 1
            elif retrieval_raw is None or retrieval_raw.get("logs_present") is not True:
                mrr_total["missing_logs"] += 1
                for data in retrieval_counts.values(): data["missing_logs"] += 1
            elif relevant:
                run_id = retrieval_raw.get("run_id")
                if run_id not in trial.run_ids:
                    raise MetricInputError(f"retrieval record is not linked to trial Run for {trial_id}")
                ranked = retrieval_raw.get("ranked_evidence_ids")
                if not isinstance(ranked, list) or len(ranked) != len(set(ranked)):
                    raise MetricInputError(f"invalid ranked_evidence_ids for {trial_id}")
                for k, data in retrieval_counts.items():
                    top = ranked[:k]
                    hit_ids = set(top) & set(relevant)
                    data["hits"] += len(hit_ids)
                    data["relevant"] += len(relevant)
                    data["zero_hit_queries"] += not hit_ids
                    first_rank = next((i for i, item in enumerate(top, 1) if item in set(relevant)), None)
                    data["reciprocal_rank_sum"] += (1 / first_rank) if first_rank else 0
                    data["queries"] += 1
                first_rank_all = next((i for i, item in enumerate(ranked, 1) if item in set(relevant)), None)
                mrr_total["reciprocal_rank_sum"] += (1 / first_rank_all) if first_rank_all else 0
                mrr_total["queries"] += 1
        elif retrieval_gold:
            mrr_total["unreviewed_relevance"] += 1
            for data in retrieval_counts.values(): data["unreviewed_relevance"] += 1

        evidence_gold = expected.get("evidence", {})
        if isinstance(evidence_gold, Mapping) and evidence_gold.get("reviewed") is True:
            required = evidence_gold.get("required_evidence_ids", [])
            if not isinstance(required, (list, tuple)) or len(required) != len(set(required)):
                raise MetricInputError(f"invalid required_evidence_ids for {trial.case_id}")
            if required:
                if not eligible:
                    evidence_unqualified_cases += len(required)
                else:
                    evidence_raw = raw.get("evidence")
                    if not isinstance(evidence_raw, Mapping) or evidence_raw.get("logs_present") is not True:
                        evidence_missing_logs += 1
                    else:
                        evidence_run_id = evidence_raw.get("run_id")
                        if evidence_run_id not in trial.run_ids:
                            raise MetricInputError(f"evidence record is not linked to trial Run for {trial_id}")
                        found = evidence_raw.get("found_evidence_ids", [])
                        authorized = evidence_raw.get("authorized_evidence_ids")
                        if not isinstance(found, list) or len(found) != len(set(found)):
                            raise MetricInputError(f"invalid found_evidence_ids for {trial_id}")
                        if authorized is None:
                            evidence_missing_logs += 1
                        else:
                            if not isinstance(authorized, list) or len(authorized) != len(set(authorized)) or not set(authorized) <= set(found):
                                raise MetricInputError(f"invalid authorized_evidence_ids for {trial_id}")
                            evidence_required += len(required)
                            evidence_found += len(set(required) & set(found) & set(authorized))

        fact_gold = expected.get("key_facts", [])
        fact_raw = raw.get("key_facts", [])
        if fact_gold:
            if not isinstance(fact_gold, (list, tuple)) or not isinstance(fact_raw, list):
                raise MetricInputError(f"key_facts must be lists for {trial_id}")
            if not eligible:
                fact_unqualified_count += len(fact_gold)
                fact_unadjudicated += len(fact_gold)
                fact_gold = []
            by_id = {x.get("fact_id"): x for x in fact_raw if isinstance(x, Mapping) and isinstance(x.get("fact_id"), str)}
            for fact in fact_gold:
                if not isinstance(fact, Mapping) or not isinstance(fact.get("fact_id"), str):
                    raise MetricInputError(f"invalid key fact rubric for {trial_id}")
                observed = by_id.get(fact["fact_id"])
                if (not fact.get("reviewed") or not observed or observed.get("adjudicated") is not True
                        or observed.get("source_authoritative") is not True
                        or not isinstance(observed.get("source_ref"), str) or not observed.get("source_ref")):
                    fact_unadjudicated += 1
                    continue
                if not isinstance(observed.get("correct"), bool):
                    raise MetricInputError(f"key fact verdict must be boolean for {trial_id}")
                fact_total += 1
                fact_correct += observed["correct"]

        claims = raw.get("claims", [])
        if not isinstance(claims, list):
            raise MetricInputError(f"claims must be a list for {trial_id}")
        if not eligible:
            claims_unqualified_count += len(claims)
            claims_unadjudicated += len(claims)
            claims = []
        for claim in claims:
            if not isinstance(claim, Mapping) or not isinstance(claim.get("adjudicated"), bool):
                claims_unadjudicated += 1
                continue
            if claim["adjudicated"] is not True:
                claims_unadjudicated += 1
                continue
            if not isinstance(claim.get("supported"), bool):
                raise MetricInputError(f"supported claim verdict must be boolean for {trial_id}")
            claim_total += 1
            unsupported += not claim["supported"]


    retrieval_metrics: dict[str, Any] = {}
    for k, data in retrieval_counts.items():
        recall = _ratio(data["hits"], data["relevant"], reason="NO_REVIEWED_RELEVANT_EVIDENCE")
        if data["missing_logs"]:
            recall = {**recall, "status": "INCOMPLETE_TELEMETRY", "value": None,
                      "reason": "RETRIEVAL_LOG_MISSING", "confidence_interval": wilson_interval(0, 0)}
        elif data["unreviewed_relevance"]:
            recall = {**recall, "status": "NOT_SCORABLE_EVIDENCE_SET_UNREVIEWED", "value": None,
                      "reason": "RELEVANCE_SET_NOT_REVIEWED", "confidence_interval": wilson_interval(0, 0)}
        elif data["unqualified_cases"]:
            qualification_status = "PARTIAL_UNSCORABLE_CASE_QUALIFICATION" if data["queries"] else "NOT_SCORABLE_CASE_QUALIFICATION"
            recall = {**recall, "status": qualification_status,
                      "value": recall["value"] if data["queries"] else None,
                      "reason": "CASE_NOT_QUALIFIED_FOR_SCORING",
                      "confidence_interval": recall["confidence_interval"] if data["queries"] else wilson_interval(0, 0)}
        elif data["no_relevant_reference"]:
            recall = {**recall, "status": "PARTIAL_UNSCORABLE_EMPTY_RELEVANCE" if data["queries"] else "NOT_SCORABLE_NO_RELEVANT_EVIDENCE",
                      "value": recall["value"] if data["queries"] else None,
                      "reason": "ONE_OR_MORE_QUERIES_HAVE_NO_REVIEWED_RELEVANT_EVIDENCE",
                      "confidence_interval": recall["confidence_interval"] if data["queries"] else wilson_interval(0, 0)}
        mrr = (data["reciprocal_rank_sum"] / data["queries"]) if data["queries"] else None
        retrieval_metrics[f"recall_at_{k}"] = recall
        retrieval_metrics[f"mrr_at_{k}"] = {"status": "COMPUTED" if data["queries"] else "NOT_SCORABLE_NO_RETRIEVAL_LOGS",
            "value": mrr, "numerator_reciprocal_rank_sum": data["reciprocal_rank_sum"],
            "denominator_queries": data["queries"], "missing_log_queries": data["missing_logs"],
            "unreviewed_relevance_queries": data["unreviewed_relevance"],
            "confidence_interval": {"status": "NOT_APPLICABLE_NON_BINOMIAL", "level": None, "lower": None, "upper": None}}
        retrieval_metrics[f"recall_at_{k}"].update({
            "evaluated_queries": data["queries"], "missing_log_queries": data["missing_logs"],
            "zero_hit_queries": data["zero_hit_queries"],
            "unreviewed_relevance_queries": data["unreviewed_relevance"],
            "no_relevant_reference_queries": data["no_relevant_reference"],
            "unqualified_case_queries": data["unqualified_cases"],
        })
    mrr_status = "COMPUTED"
    mrr_value = mrr_total["reciprocal_rank_sum"] / mrr_total["queries"] if mrr_total["queries"] else None
    mrr_reason = None
    if mrr_total["missing_logs"]:
        mrr_status, mrr_value, mrr_reason = "INCOMPLETE_TELEMETRY", None, "RETRIEVAL_LOG_MISSING"
    elif mrr_total["unreviewed_relevance"]:
        mrr_status, mrr_value, mrr_reason = "NOT_SCORABLE_EVIDENCE_SET_UNREVIEWED", None, "RELEVANCE_SET_NOT_REVIEWED"
    elif mrr_total["unqualified_cases"]:
        mrr_status = "PARTIAL_UNSCORABLE_CASE_QUALIFICATION" if mrr_total["queries"] else "NOT_SCORABLE_CASE_QUALIFICATION"
        mrr_value = mrr_value if mrr_total["queries"] else None
        mrr_reason = "CASE_NOT_QUALIFIED_FOR_SCORING"
    elif not mrr_total["queries"]:
        mrr_status, mrr_reason = "NOT_SCORABLE_NO_RELEVANT_EVIDENCE", "NO_REVIEWED_RELEVANT_EVIDENCE"
    elif mrr_total["no_relevant_reference"]:
        mrr_status, mrr_reason = "PARTIAL_UNSCORABLE_EMPTY_RELEVANCE", "ONE_OR_MORE_QUERIES_HAVE_NO_REVIEWED_RELEVANT_EVIDENCE"
    retrieval_metrics["mrr"] = {
        "status": mrr_status, "value": mrr_value, "reason": mrr_reason,
        "numerator_reciprocal_rank_sum": mrr_total["reciprocal_rank_sum"],
        "denominator_queries": mrr_total["queries"], "missing_log_queries": mrr_total["missing_logs"],
        "unreviewed_relevance_queries": mrr_total["unreviewed_relevance"],
        "no_relevant_reference_queries": mrr_total["no_relevant_reference"],
        "unqualified_case_queries": mrr_total["unqualified_cases"],
        "confidence_interval": {"status": "NOT_APPLICABLE_NON_BINOMIAL", "level": None, "lower": None, "upper": None},
    }

    run_successes = run_status_counts.get("SUCCEEDED", 0)
    run_denominator = len(run_observations) + sum(1 for row in started_rows if row["model"].lifecycle_status == "UNKNOWN_RESULT" and not row["run_observations"])
    run_metric = _ratio(run_successes, run_denominator, reason="NO_RUN_STATUS_OBSERVATIONS")
    if missing_run_status:
        run_metric = {**run_metric, "status": "INCOMPLETE_TELEMETRY", "value": None,
                      "reason": "RUN_STATUS_MISSING_FOR_STARTED_TRIAL", "confidence_interval": wilson_interval(0, 0)}
    answer_metric = _ratio(answer_pass, answer_total, reason="NO_APPROVED_GRADED_ANSWERS")
    task_metric = _ratio(task_pass, task_total, reason="NO_VERIFIABLE_TASK_OUTCOMES")
    if answer_unknown and answer_total == 0:
        answer_metric["status"] = "NOT_SCORABLE_VERDICT_UNKNOWN"
        answer_metric["reason"] = "ANSWER_VERDICT_UNKNOWN_OR_NOT_APPLICABLE"
    if task_unknown and task_total == 0:
        task_metric["status"] = "NOT_SCORABLE_VERDICT_UNKNOWN"
        task_metric["reason"] = "TASK_VERDICT_UNKNOWN_OR_NOT_APPLICABLE"
    evidence_metric = _ratio(evidence_found, evidence_required, reason="NO_REVIEWED_REQUIRED_EVIDENCE")
    if evidence_missing_logs:
        evidence_metric = {**evidence_metric, "status": "INCOMPLETE_TELEMETRY", "value": None,
                           "reason": "EVIDENCE_LOG_MISSING", "confidence_interval": wilson_interval(0, 0)}
    elif evidence_unqualified_cases:
        evidence_metric = {**evidence_metric,
            "status": "PARTIAL_UNSCORABLE_CASE_QUALIFICATION" if evidence_required else "NOT_SCORABLE_CASE_QUALIFICATION",
            "value": evidence_metric["value"] if evidence_required else None,
            "reason": "CASE_NOT_QUALIFIED_FOR_SCORING",
            "confidence_interval": evidence_metric["confidence_interval"] if evidence_required else wilson_interval(0, 0),
            "unqualified_required_evidence_count": evidence_unqualified_cases}
    key_fact_metric = _ratio(fact_correct, fact_total, reason="NO_ADJUDICATED_KEY_FACTS")
    if fact_unqualified_count:
        key_fact_metric = {**key_fact_metric,
            "status": "PARTIAL_UNSCORABLE_CASE_QUALIFICATION" if fact_total else "NOT_SCORABLE_CASE_QUALIFICATION",
            "value": key_fact_metric["value"] if fact_total else None,
            "reason": "CASE_NOT_QUALIFIED_FOR_SCORING",
            "confidence_interval": key_fact_metric["confidence_interval"] if fact_total else wilson_interval(0, 0),
            "unqualified_key_fact_count": fact_unqualified_count}
    unsupported_metric = _ratio(unsupported, claim_total, reason="NO_ADJUDICATED_FACT_CLAIMS")
    if claims_unqualified_count:
        unsupported_metric = {**unsupported_metric,
            "status": "PARTIAL_UNSCORABLE_CASE_QUALIFICATION" if claim_total else "NOT_SCORABLE_CASE_QUALIFICATION",
            "value": unsupported_metric["value"] if claim_total else None,
            "reason": "CASE_NOT_QUALIFIED_FOR_SCORING",
            "confidence_interval": unsupported_metric["confidence_interval"] if claim_total else wilson_interval(0, 0),
            "unqualified_claim_count": claims_unqualified_count}
    calibration = summarize_calibration(calibration_records,
        data_scope="SYNTHETIC_ONLY" if dataset_scope == "SYNTHETIC_ONLY" else "AUTHORIZED_EVALUATION",
        verified_human_record_ids=verified_human_record_ids)
    bootstrap = _group_bootstrap_metrics(case_rows, trial_rows, scoreable_trial_ids, recall_ks,
        seed=bootstrap_seed, resamples=bootstrap_resamples,
        confidence_level=bootstrap_confidence_level, min_groups=bootstrap_min_groups)
    def gated(interval: Mapping[str, Any], primary_status: str) -> dict[str, Any]:
        if primary_status == "COMPUTED":
            return dict(interval)
        return {"status": "NOT_COMPUTABLE_PRIMARY_METRIC", "reason": primary_status,
                "lower": None, "upper": None, "independent_group_count": interval.get("independent_group_count"),
                "valid_resamples": 0, "config": interval.get("config")}
    route["accuracy"]["group_bootstrap_interval"] = gated(bootstrap["router"]["accuracy"],
        "INCOMPLETE_ROUTE_TELEMETRY" if route_missing else route["accuracy"]["status"])
    route["macro_f1_confidence_interval"] = gated(bootstrap["router"]["macro_f1"], route["status"])
    for label in ROUTE_LABELS:
        for kind in ("precision", "recall"):
            route["per_class"][label][kind]["group_bootstrap_interval"] = gated(
                bootstrap["router"]["per_class"][label][kind], route["per_class"][label][kind]["status"])
        route["per_class"][label]["f1_group_bootstrap_interval"] = gated(
            bootstrap["router"]["per_class"][label]["f1"], route["per_class"][label]["f1_status"])
    for k in recall_ks:
        key = f"recall_at_{k}"
        retrieval_metrics[key]["group_bootstrap_interval"] = gated(bootstrap["retrieval"][key], retrieval_metrics[key]["status"])
        mrr_key = f"mrr_at_{k}"
        retrieval_metrics[mrr_key]["group_bootstrap_interval"] = gated(bootstrap["mrr_at_k"][mrr_key], retrieval_metrics[mrr_key]["status"])
    retrieval_metrics["mrr"]["group_bootstrap_interval"] = gated(bootstrap["mrr"], retrieval_metrics["mrr"]["status"])
    evidence_metric["group_bootstrap_interval"] = gated(bootstrap["evidence_coverage"], evidence_metric["status"])
    key_fact_metric["group_bootstrap_interval"] = gated(bootstrap["key_fact_correctness"], key_fact_metric["status"])
    unsupported_metric["group_bootstrap_interval"] = gated(bootstrap["unsupported_assertion_rate"], unsupported_metric["status"])
    run_metric["group_bootstrap_interval"] = gated(bootstrap["run_success_rate"], run_metric["status"])
    answer_metric["group_bootstrap_interval"] = gated(bootstrap["answer_correctness"], answer_metric["status"])
    task_metric["group_bootstrap_interval"] = gated(bootstrap["task_success_rate"], task_metric["status"])
    all_raw = {"cases": [deepcopy(row) for row in cases], "trials": [deepcopy(row) for row in trials],
               "calibration_records": deepcopy(list(calibration_records or [])),
               "verified_human_record_ids": sorted(set(verified_human_record_ids)), "dataset_scope": dataset_scope}
    report = {
        "schema_version": "m6.2-derived-metric-report-v1",
        "report_kind": "SYNTHETIC_DERIVED_REPORT" if dataset_scope == "SYNTHETIC_ONLY" else "AUTHORIZED_EVALUATION_DERIVED_REPORT",
        "product_quality_claim": False if dataset_scope == "SYNTHETIC_ONLY" else None,
        "experiment_id": experiment_config["experiment_id"],
        "provenance": {
            "dataset_scope": dataset_scope,
            "raw_records_sha256": sha256_json(all_raw),
            "experiment_config_sha256": sha256_json(experiment_config),
            "metric_contract_version": metric_contract_version,
            "metric_contract_sha256": metric_contract_sha256,
            "statistical_config": {"recall_ks": list(recall_ks), "group_bootstrap": bootstrap["config"],
                                   "wilson_interval": "95% descriptive trial-level only; not cluster-adjusted"},
            "source_refs": [{"trial_id": row["model"].trial_id, "case_id": row["model"].case_id,
                             "experiment_id": row["model"].experiment_id, "run_ids": list(row["model"].run_ids),
                             "raw_result_ref": row["model"].raw_result_ref}
                            for row in trial_rows.values()],
        },
        "counts": {
            "planned_case_count": len(case_rows), "planned_trial_count": len(trial_rows),
            "independent_case_count": len({row["model"].case_id for row in trial_rows.values()}),
            "independent_group_count": len({row["model"].group_id for row in case_rows.values()}),
            "scoreable_case_count": len(unique_scoreable_cases), "scoreable_trial_count": len(scoreable_trial_ids),
            "started_trial_count": len(started_rows), "run_count": len(known_run_ids),
            "not_run_count": status_counts.get("NOT_RUN", 0), "budget_stop_count": status_counts.get("BUDGET_STOP", 0),
            "environment_failure_count": status_counts.get("ENV_ERROR", 0),
            "model_unavailable_count": status_counts.get("MODEL_UNAVAILABLE", 0),
            "unknown_result_count": status_counts.get("UNKNOWN_RESULT", 0),
            "unscorable_trial_count": status_counts.get("UNSCORABLE", 0),
            "pending_human_review_count": status_counts.get("PENDING_HUMAN_REVIEW", 0),
            "grading_error_count": sum(1 for row in trial_rows.values() if row["outcome"] is not None and row["outcome"].grading_status == GradingStatus.GRADING_ERROR.value),
            "lifecycle_status_counts": dict(sorted(status_counts.items())),
            "run_status_counts": dict(sorted(run_status_counts.items())),
            "started_trials_missing_run_status": missing_run_status,
        },
        "metrics": {
            "router": route,
            "retrieval": retrieval_metrics,
            "evidence_coverage": evidence_metric,
            "key_fact_correctness": key_fact_metric,
            "key_fact_unadjudicated_count": fact_unadjudicated,
            "unsupported_assertion_rate": unsupported_metric,
            "unadjudicated_claim_count": claims_unadjudicated,
            "run_success_rate": run_metric,
            "answer_correctness": {**answer_metric, "unknown_or_not_applicable_count": answer_unknown,
                                   "excluded_reasons": dict(sorted(answer_excluded_reasons.items()))},
            "task_success_rate": {**task_metric, "unknown_or_not_applicable_count": task_unknown,
                                  "excluded_reasons": dict(sorted(task_excluded_reasons.items()))},
            "unauthorized_side_effect_count": {"status": "COMPUTED" if safety_checked_trials else "NOT_RUN",
                "count": unauthorized_effects if safety_checked_trials else None,
                "checked_trial_count": safety_checked_trials,
                "unsafe_trial_rate": _ratio(unsafe_trials, safety_checked_trials, reason="NO_SAFETY_TELEMETRY")},
            "unknown_result_rate": _ratio(status_counts.get("UNKNOWN_RESULT", 0), len(started_rows), reason="NO_STARTED_TRIALS"),
        },
        "grader_calibration": calibration,
        "budget": deepcopy(experiment_config.get("budget", {})),
        "limitations": [
            "Derived from immutable input snapshots; report generation does not mutate raw records.",
            "SYNTHETIC_ONLY reports validate formulas and are not ShopRelay quality results.",
            "Wilson intervals are descriptive and not cluster-adjusted; repeated/grouped Trials are not independent Cases.",
            "Group-aware intervals use group_id cluster bootstrap; results are withheld below the configured independent-group minimum.",
            "Human calibration remains NOT_RUN without verified real human adjudication records.",
        ],
    }
    return report


def serialize_derived_report(report: Mapping[str, Any]) -> str:
    """Canonical JSON serialization for deterministic derived reports."""
    return canonical_json(report)


def recompute_derived_report(serialized_report: str | Mapping[str, Any], *,
                             cases: Sequence[Mapping[str, Any]], trials: Sequence[Mapping[str, Any]],
                             experiment_config: Mapping[str, Any], dataset_scope: str,
                             recall_ks: Sequence[int] = (3, 5),
                             calibration_records: Sequence[Mapping[str, Any]] | None = None,
                             verified_human_record_ids: Sequence[str] = (),
                             metric_contract_version: str = "m6.2-prep-r1",
                             metric_contract_sha256: str | None = None,
                             metric_contract_path: str | Path | None = None,
                             bootstrap_seed: int = 20261026, bootstrap_resamples: int = 2000,
                             bootstrap_confidence_level: float = 0.95,
                             bootstrap_min_groups: int = 10) -> dict[str, Any]:
    """Rebuild a report from raw records and assert it exactly matches the saved projection."""
    try:
        saved = json.loads(serialized_report) if isinstance(serialized_report, str) else dict(serialized_report)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReportMismatchError(f"serialized report is invalid JSON: {exc}") from exc
    if metric_contract_path is not None:
        if metric_contract_sha256 is None:
            raise MetricContractMismatchError("locked metric_contract_sha256 is required when a contract file is supplied")
        verify_metric_contract_file(metric_contract_path, expected_version=metric_contract_version,
                                    expected_sha256=metric_contract_sha256)
    else:
        raise MetricContractMismatchError("metric_contract_path is required for report recomputation")
    rebuilt = aggregate_capabilities(cases=cases, trials=trials, experiment_config=experiment_config,
        dataset_scope=dataset_scope, recall_ks=recall_ks, calibration_records=calibration_records,
        verified_human_record_ids=verified_human_record_ids,
        metric_contract_version=metric_contract_version, metric_contract_sha256=metric_contract_sha256,
        bootstrap_seed=bootstrap_seed, bootstrap_resamples=bootstrap_resamples,
        bootstrap_confidence_level=bootstrap_confidence_level, bootstrap_min_groups=bootstrap_min_groups)
    if canonical_json(saved) != canonical_json(rebuilt):
        raise ReportMismatchError("saved derived report differs from recomputation from raw Trial records")
    return rebuilt


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lo, hi = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def aggregate(trials: list[dict[str, Any]], scores: list[dict[str, Any]], budget: dict[str, Any],
              outcomes: list[dict[str, Any]] | None = None, reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    status = Counter(trial["lifecycle_status"] for trial in trials)
    run_ids = [run_id for trial in trials for run_id in trial["run_ids"]]
    latencies = [float(trial["latency_ms"]["total"]) for trial in trials if trial["latency_ms"].get("total") is not None]
    routes = Counter(row.get("dimension") for row in scores if row.get("dimension") == "route" and row.get("verdict") == "PASS")
    case_ids = {trial["case_id"] for trial in trials}
    outcomes = outcomes or []
    reviews = reviews or []
    run_scored = outcomes
    task_scored = [out for out in outcomes if out["task_success"] in {"PASS", "FAIL"}]
    answer_scored = [out for out in outcomes if out["answer_correctness"] in {"PASS", "FAIL"}]
    safety_scored = [out for out in outcomes if out["safety_result"] in {"PASS", "FAIL"}]
    return {
        "label": "SYNTHETIC_TEST_ONLY_NOT_QUALITY_EVALUATION",
        "unique_case_count": len(case_ids), "trial_count": len(trials), "run_reference_count": len(run_ids),
        "lifecycle_status_counts": dict(sorted(status.items())),
        "unknown_result_count": status.get("UNKNOWN_RESULT", 0),
        "succeeded_run_count": status.get("SUCCEEDED", 0),
        "task_success_from_run_status": False,
        "run_success": {"numerator":sum(out["run_success"] == "PASS" for out in run_scored),"denominator":len(run_scored),"rate":(sum(out["run_success"] == "PASS" for out in run_scored)/len(run_scored) if run_scored else None),"unknown_count":sum(out["run_success"] == "UNKNOWN" for out in outcomes)},
        "task_success": {"numerator":sum(out["task_success"] == "PASS" for out in task_scored),"denominator":len(task_scored),"rate":(sum(out["task_success"] == "PASS" for out in task_scored)/len(task_scored) if task_scored else None),"unknown_count":sum(out["task_success"] == "UNKNOWN" for out in outcomes)},
        "answer_correctness": {"numerator":sum(out["answer_correctness"] == "PASS" for out in answer_scored),"denominator":len(answer_scored),"rate":(sum(out["answer_correctness"] == "PASS" for out in answer_scored)/len(answer_scored) if answer_scored else None),"pending_review_count":sum(out["grading_status"] == "PENDING_HUMAN_REVIEW" for out in outcomes)},
        "safety": {"checked_count":len(safety_scored),"failure_count":sum(out["safety_result"] == "FAIL" for out in safety_scored),"unknown_count":sum(out["safety_result"] == "UNKNOWN" for out in outcomes)},
        "pending_human_review_tasks":sum(review["status"] == "PENDING_HUMAN_REVIEW" for review in reviews),
        "route_structural_pass_count": routes.get("route", 0),
        "latency_ms": {"sample_count": len(latencies), "p50": percentile(latencies, .5), "p95": percentile(latencies, .95), "method": "linear interpolation; synthetic trials only"},
        "budget": budget,
        "score_count": len(scores),
        "limitations": ["Case count is unique case_id, not trial count.", "Rates describe scripted synthetic harness validation only, not model or business quality.", "No real provider, embedding, or judge was invoked.", "Synthetic results do not support M2 quality or production claims."],
    }
