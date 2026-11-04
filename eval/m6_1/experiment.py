"""Pre-registration lock for immutable evaluation configurations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import EvalExperiment, sha256_json
from .store import ResultStore, StoreError


REQUIRED_CONFIG_FIELDS = {
    "fixture_version", "grader_version", "judge_version", "retriever_config",
    "knowledge_index_version", "model_versions", "prompt_versions", "tool_permissions",
    "retry_policy", "random_seed", "environment", "budget_limits", "data_version", "fixture_sha256",
}
OPTIONAL_CONFIG_FIELDS = {"runner_version", "registry_version", "rubric_version", "grant_policy_version", "reporting_cohorts"}


@dataclass(frozen=True)
class ExperimentLock:
    experiment: EvalExperiment
    locked_at_utc: str


def preregister_experiment(*, store: ResultStore, experiment_id: str, config: Mapping[str, Any], git_commit: str,
                           strategy: str, scoring_version: str, formal_gate=None, formal_grant=None) -> ExperimentLock:
    missing = REQUIRED_CONFIG_FIELDS - set(config)
    if missing:
        raise ValueError(f"experiment config missing required fields: {', '.join(sorted(missing))}")
    unknown = set(config) - REQUIRED_CONFIG_FIELDS - OPTIONAL_CONFIG_FIELDS
    if unknown:
        raise ValueError(f"experiment config has unknown fields: {', '.join(sorted(unknown))}")
    if "reporting_cohorts" in config:
        cohorts=config["reporting_cohorts"]
        if (formal_grant is None or not isinstance(cohorts,Mapping) or set(cohorts)!={"route","retrieval","outcome"} or
                any(not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids) or
                    not set(ids)<=dict(formal_grant.plan.case_bindings).keys() for ids in cohorts.values())):
            raise ValueError("reporting cohorts must be unique subsets of the signed formal cases")
    fixture_hash = config["fixture_sha256"]
    if not isinstance(fixture_hash, str) or len(fixture_hash) != 64 or any(ch not in "0123456789abcdef" for ch in fixture_hash.lower()):
        raise ValueError("fixture_sha256 must be a SHA-256 hex digest")
    if formal_gate is not None or formal_grant is not None:
        from .execution_modes import ExecutionMode
        if formal_gate is None or formal_grant is None: raise ValueError("formal issuer and grant required together")
        binding=formal_gate.formal_binding(ExecutionMode(strategy))
        formal_gate.formal_authority.verify(formal_grant,**binding)
        if (sha256_json(dict(config))!=formal_grant.plan.config_sha256 or
                experiment_id!=formal_grant.plan.experiment_id or
                config["data_version"]!=formal_grant.plan.asset_version or fixture_hash!=formal_grant.plan.asset_sha256 or
                config["budget_limits"]!=formal_grant.plan.budget.to_dict()):
            raise ValueError("formal experiment config/data/budget differs from signed plan")
    elif config["data_version"] != "synthetic-r1":
        environment = config.get("environment")
        explicitly_authorized_dev_preregistration = (
            strategy == "DEV_SMOKE" and isinstance(environment, Mapping) and
            environment.get("mode") == "DEV_SMOKE" and
            environment.get("kind") == "isolated_dev" and
            environment.get("database") == "shoprelay_m11_test" and
            isinstance(config["data_version"], str) and config["data_version"].startswith("dev-")
        )
        if not explicitly_authorized_dev_preregistration:
            raise ValueError("only synthetic-r1 or explicitly scoped isolated DEV_SMOKE preregistration is supported")
    config_snapshot = dict(config)
    digest = sha256_json(config_snapshot)
    exp = EvalExperiment(
        experiment_id=experiment_id, config_snapshot=config_snapshot, config_sha256=digest,
        git_commit=git_commit, knowledge_index_version=config.get("knowledge_index_version"),
        model_versions=dict(config["model_versions"]), prompt_versions=dict(config["prompt_versions"]),
        strategy=strategy, tool_permissions=tuple(config["tool_permissions"]),
        budget=dict(config["budget_limits"]), data_version=config["data_version"],
        scoring_version=scoring_version,
    )
    locked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        store.lock_experiment(exp, locked_at)
    except StoreError:
        raise
    return ExperimentLock(exp, locked_at)
