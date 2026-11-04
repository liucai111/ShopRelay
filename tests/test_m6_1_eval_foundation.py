from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from m6_1.contracts import (  # noqa: E402
    ContractError,
    EvalCase,
    EvalExperiment,
    EvalOutcome,
    EvalTrial,
    sha256_json,
    validate_unique_ids,
)
from m6_1.registry import (  # noqa: E402
    AccessDenied,
    AccessGrant,
    AccessGuard,
    DatasetAsset,
    DatasetRegistry,
    RegistryError,
)
from m6_1.result_log import AppendOnlyError, append_outcome, append_raw_trial, validate_trial_links  # noqa: E402


@pytest.fixture
def work_dir(tmp_path: Path):
    # Reuse the project-local tmp_path provider on Windows. TemporaryDirectory's
    # restrictive creation mode/cleanup was failing before the test boundary.
    return tmp_path


def case_data(case_id: str = "synthetic.case.1", split: str = "synthetic", version: str = "fixture-r1") -> dict:
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "group_id": "synthetic.group.1",
        "split": split,
        "input": {"query": "合成夹具政策流程"},
        "expected_outcome": {"route": "SINGLE"},
        "rubric_refs": ["rubric.synthetic.v1"],
        "fixture_ref": "fixture.synthetic.1",
        "risk_level": "LOW",
        "data_version": version,
    }


def case(case_id: str = "synthetic.case.1") -> EvalCase:
    return EvalCase.from_dict(case_data(case_id))


def trial(trial_id: str = "trial.synthetic.1", run_ids: list[str] | None = None) -> EvalTrial:
    return EvalTrial.from_dict({
        "schema_version": "1.0",
        "trial_id": trial_id,
        "case_id": "synthetic.case.1",
        "experiment_id": "experiment.synthetic.1",
        "trial_index": 1,
        "run_ids": run_ids or ["run-1", "run-2"],
        "lifecycle_status": "SUCCEEDED",
        "model_calls": [{"provider": "stub", "usage": None}],
        "tool_calls": [],
        "raw_result_ref": "raw/synthetic/trial-1.json",
        "error": None,
        "usage": {"kind": "STUB", "prompt_tokens": None, "completion_tokens": None},
        "latency_ms": {"end_to_end": 120, "provider": None},
    })


def experiment() -> EvalExperiment:
    snapshot = {"provider": "stub", "strategy": "DIRECT", "retry": 0}
    return EvalExperiment.from_dict({
        "schema_version": "1.0",
        "experiment_id": "experiment.synthetic.1",
        "config_snapshot": snapshot,
        "config_sha256": sha256_json(snapshot),
        "git_commit": "0123456789abcdef",
        "knowledge_index_version": None,
        "model_versions": {"answer": "deterministic-stub"},
        "prompt_versions": {"system": "synthetic-v1"},
        "strategy": "DIRECT",
        "tool_permissions": [],
        "budget": {"provider_calls": 0},
        "data_version": "fixture-r1",
        "scoring_version": "synthetic-rubric-v1",
    })


def asset_for(path: Path, *, split: str = "dev", scoreability: str = "ELIGIBLE", digest: str | None = None) -> DatasetAsset:
    return DatasetAsset(
        asset_id=f"asset.{split}", asset_type="SYNTHETIC_TEST_FIXTURE", version="r1", parent_asset_id=None,
        split=split, case_count=1, source_path=path.name, sha256=digest,
        review_status="INDEPENDENT_REVIEWED" if scoreability == "ELIGIBLE" else "CANDIDATE",
        reviewer_id="synthetic-reviewer", reviewed_at_utc="2026-10-24T00:00:00Z",
        review_method="SYNTHETIC_FIXTURE", independence_status="INDEPENDENT" if scoreability == "ELIGIBLE" else "NOT_VERIFIED",
        exposure_status="UNEXPOSED_VERIFIED" if scoreability == "ELIGIBLE" else "NOT_VERIFIED",
        scoreability=scoreability, locked_at_utc="2026-10-24T00:00:00Z" if scoreability == "ELIGIBLE" else None,
        access_policy="TEST_ONLY", limitations=("synthetic fixture only",), integrity_status="VERIFIED" if digest else "NOT_VERIFIED",
    )


def test_case_trial_experiment_outcome_round_trip_and_separation() -> None:
    c = case()
    t = trial()
    e = experiment()
    out = EvalOutcome.from_dict({
        "schema_version": "1.0", "outcome_id": "outcome.synthetic.1", "trial_id": t.trial_id,
        "run_success": "PASS", "answer_correctness": "UNKNOWN", "task_success": "FAIL",
        "safety_result": "PASS", "grading_status": "GRADED", "rubric_version": "rubric.v1",
        "score_refs": ["score.synthetic.1"], "created_at_utc": "2026-10-24T10:00:00+08:00",
    })
    assert EvalCase.from_dict(c.to_dict()) == c
    assert EvalTrial.from_dict(t.to_dict()) == t
    assert EvalExperiment.from_dict(e.to_dict()) == e
    assert EvalOutcome.from_dict(out.to_dict()).created_at_utc == "2026-10-24T02:00:00Z"
    assert t.lifecycle_status == "SUCCEEDED" and out.task_success == "FAIL"
    assert t.run_ids == ("run-1", "run-2")
    assert validate_trial_links(t, known_case_ids={c.case_id}, known_experiment_ids={e.experiment_id}) is None
    redacted = c.to_dict()
    redacted["input"]["api_key"] = "do-not-export"
    assert EvalCase.from_dict(redacted).to_dict()["input"]["api_key"] == "[REDACTED]"


@pytest.mark.parametrize("mutator", [
    lambda d: d.pop("group_id"),
    lambda d: d.update(unknown_field=True),
    lambda d: d.update(case_id=""),
    lambda d: d.update(split="not_a_split"),
    lambda d: d.update(schema_version="9.0"),
])
def test_case_rejects_missing_unknown_invalid_and_incompatible_fields(mutator) -> None:
    data = case().to_dict()
    mutator(data)
    with pytest.raises(ContractError):
        EvalCase.from_dict(data)


def test_json_schemas_are_valid_versioned_closed_objects() -> None:
    schema_dir = Path(__file__).resolve().parents[1] / "eval" / "m6_1" / "schemas"
    expected = {
        "eval-case-v1.schema.json", "eval-trial-v1.schema.json", "eval-experiment-v1.schema.json",
        "eval-outcome-v1.schema.json", "dataset-registry-v1.schema.json",
        "budget-limits-v1.schema.json", "code-score-v1.schema.json", "judge-request-v1.schema.json",
        "judge-response-v1.schema.json", "human-review-record-v1.schema.json",
    }
    loaded = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in schema_dir.glob("*.json")}
    assert set(loaded) == expected
    for schema in loaded.values():
        assert schema["$schema"].endswith("2020-12/schema")
        if "oneOf" in schema:
            assert all(item["type"] == "object" and item["additionalProperties"] is False for item in schema["oneOf"])
        else:
            assert schema["type"] == "object" and schema["additionalProperties"] is False


def test_duplicate_case_ids_fail_but_multiple_trials_and_multiple_runs_are_valid() -> None:
    with pytest.raises(ContractError, match="duplicate case_id"):
        validate_unique_ids([case(), case()], "case_id")
    t1 = trial("trial.synthetic.1", ["run-a", "run-b"])
    t2 = trial("trial.synthetic.2", ["run-c"])
    validate_unique_ids([t1, t2], "trial_id")
    assert len({t1.case_id, t2.case_id}) == 1


def test_config_hash_and_trial_links_are_enforced() -> None:
    data = experiment().to_dict()
    data["config_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="hash mismatch"):
        EvalExperiment.from_dict(data)
    with pytest.raises(ContractError, match="unknown case_id"):
        validate_trial_links(trial(), known_case_ids=set(), known_experiment_ids={"experiment.synthetic.1"})


def test_append_only_raw_trial_and_outcome_records(work_dir: Path) -> None:
    raw_path = work_dir / "raw.jsonl"
    score_path = work_dir / "scores.jsonl"
    t = trial()
    append_raw_trial(raw_path, t)
    with pytest.raises(AppendOnlyError):
        append_raw_trial(raw_path, t)
    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 1
    out = EvalOutcome.from_dict({
        "outcome_id": "outcome.synthetic.1", "trial_id": t.trial_id, "run_success": "PASS",
        "answer_correctness": "UNKNOWN", "task_success": "UNKNOWN", "safety_result": "PASS",
        "grading_status": "NOT_GRADED", "rubric_version": "r1", "score_refs": [],
        "created_at_utc": "2026-10-24T00:00:00Z",
    })
    append_outcome(score_path, out)
    assert len(score_path.read_text(encoding="utf-8").splitlines()) == 1


def test_historical_registry_is_metadata_only_and_invalid_manifest_is_not_repaired() -> None:
    root = Path(__file__).resolve().parents[1]
    registry_path = root / "eval/registry/m6.1-dataset-registry-r1.json"
    before = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    reg = DatasetRegistry.load_metadata(registry_path)
    assert reg.get("route-reviewed-100").review_status == "BATCH_USER_CONFIRMED"
    assert reg.get("route-reviewed-100").scoreability == "HISTORICAL_ONLY"
    assert reg.get("policy-candidate-family").scoreability == "NOT_SCOREABLE"
    assert reg.get("route-r2-invalid-manifest").integrity_status == "INVALID_MANIFEST"
    assert reg.get("route-user-confirmed-52").reviewer_id is None
    assert reg.get("route-user-confirmed-52").sha256 is None
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == before
    metadata = reg.metadata("route-reviewed-100")
    assert set(metadata) == {
        "asset_id", "file_path", "split", "version", "sha256",
        "review_status", "exposure_status", "scoreability",
    }
    assert len(reg.safe_metadata()) == 11


def test_locked_asset_cannot_be_overwritten_or_duplicate_id_reused(work_dir: Path) -> None:
    source = work_dir / "cases.json"
    digest = hashlib.sha256(b'{"cases":[]}').hexdigest()
    asset = asset_for(source, digest=digest)
    registry = DatasetRegistry([asset])
    with pytest.raises(RegistryError, match="duplicate"):
        registry.add(asset)
    replacement = DatasetAsset(**{**asset.__dict__, "version": "r2"})
    with pytest.raises(RegistryError, match="immutable"):
        registry.add(replacement)


def test_default_deny_candidate_content_and_audit_reason(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    content = json.dumps({"cases": [case_data("synthetic-only", split="test_candidate", version="r1")]}).encode()
    source = root / "candidate-synthetic-fixture.json"
    source.write_bytes(content)
    asset = asset_for(source, split="test_candidate", scoreability="NOT_SCOREABLE", digest=hashlib.sha256(content).hexdigest())
    registry = DatasetRegistry([asset])
    guard = AccessGuard(registry, root, work_dir / "audit" / "access.jsonl")
    assert guard.metadata(asset.asset_id)["split"] == "test_candidate"
    with pytest.raises(AccessDenied, match="NO_EXPLICIT_AUTHORIZATION|PROTECTED_SPLIT"):
        guard.read_cases(asset.asset_id, principal="ordinary-runner")
    audit = json.loads((work_dir / "audit/access.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert audit["decision"] == "DENIED"
    assert audit["reason"] == "PROTECTED_SPLIT_NO_EXPLICIT_AUTHORIZATION"
    assert "case_id" not in audit


def test_unregistered_path_alias_and_hash_mismatch_are_rejected(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    content = json.dumps({"cases": [case_data("safe", split="dev", version="r1")]}).encode()
    source = root / "cases.json"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    asset = asset_for(source, digest=digest)
    grant = AccessGrant("auth-1", "steward-1", "2026-10-24T00:00:00Z", (asset.asset_id,), ("dev",), "synthetic contract test", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    guard = AccessGuard(DatasetRegistry([asset]), root, work_dir / "access.jsonl")
    alias = root / "renamed-copy.json"
    alias.write_bytes(content)
    with pytest.raises(AccessDenied, match="PATH_ALIAS_OR_UNREGISTERED_COPY"):
        guard.read_cases(asset.asset_id, principal="authorized-test", grant=grant, requested_path=alias)
    source.write_text('{"cases":[]} ', encoding="utf-8")
    with pytest.raises(AccessDenied, match="ASSET_HASH_MISMATCH"):
        guard.read_cases(asset.asset_id, principal="authorized-test", grant=grant)


def test_authorized_synthetic_dev_read_requires_explicit_grant_and_valid_hash(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    content = json.dumps({"cases": [case_data("safe-synthetic", split="dev", version="r1")]}).encode()
    source = root / "cases.json"
    source.write_bytes(content)
    asset = asset_for(source, digest=hashlib.sha256(content).hexdigest())
    registry = DatasetRegistry([asset])
    guard = AccessGuard(registry, root, work_dir / "access.jsonl")
    with pytest.raises(AccessDenied):
        guard.read_cases(asset.asset_id, principal="ordinary-runner")
    grant = AccessGrant("auth-1", "reviewer-1", "2026-10-24T00:00:00Z", (asset.asset_id,), ("dev",), "synthetic test only", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    result = guard.read_cases(asset.asset_id, principal="synthetic-test", grant=grant)
    assert len(result) == 1 and result[0].case_id == "safe-synthetic"


def test_candidate_csv_and_mixed_split_are_denied_without_disclosing_fixture_content(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    fake_candidate_row = "synthetic-only-row,test_candidate,NEVER_REAL_CONTENT"
    csv_path = root / "synthetic-candidate.csv"
    csv_path.write_text("case_id,split,input\n" + fake_candidate_row + "\n", encoding="utf-8")
    mixed_content = json.dumps({"cases": [case_data("synthetic-mixed", split="dev", version="r1")]})
    mixed_path = root / "mixed-container.json"
    mixed_path.write_text(mixed_content, encoding="utf-8")
    candidate = asset_for(
        csv_path, split="test_candidate", scoreability="NOT_SCOREABLE",
        digest=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    )
    mixed = DatasetAsset(**{
        **asset_for(mixed_path, split="dev", digest=hashlib.sha256(mixed_content.encode()).hexdigest()).__dict__,
        "asset_id": "asset.mixed", "split": "MIXED", "scoreability": "HISTORICAL_ONLY",
        "review_status": "UNKNOWN", "independence_status": "NOT_VERIFIED",
        "exposure_status": "NOT_VERIFIED", "locked_at_utc": None,
    })
    registry = DatasetRegistry([candidate, mixed])
    guard = AccessGuard(registry, root, work_dir / "audit.jsonl")
    assert fake_candidate_row not in json.dumps(guard.metadata_only())
    assert set(guard.metadata(candidate.asset_id)) == {
        "asset_id", "file_path", "split", "version", "sha256",
        "review_status", "exposure_status", "scoreability",
    }
    for asset in (candidate, mixed):
        with pytest.raises(AccessDenied):
            guard.read_cases(asset.asset_id, principal="synthetic-runner")
    audit = [json.loads(line) for line in (work_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["decision"] for entry in audit] == ["DENIED", "DENIED"]
    assert "MIXED_SPLIT_CONTENT_READ_FORBIDDEN" in {entry["reason"] for entry in audit}
    assert fake_candidate_row not in json.dumps(audit)


def test_protected_split_is_blocked_even_when_filename_is_unmarked(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    content = json.dumps({"cases": [case_data("synthetic-protected", split="test_candidate", version="r1")]})
    source = root / "opaque-batch-17.json"
    source.write_text(content, encoding="utf-8")
    asset = asset_for(source, split="test_candidate", scoreability="NOT_SCOREABLE", digest=hashlib.sha256(content.encode()).hexdigest())
    guard = AccessGuard(DatasetRegistry([asset]), root, work_dir / "audit.jsonl")
    assert "candidate" not in source.name.lower()
    with pytest.raises(AccessDenied, match="PROTECTED_SPLIT_NO_EXPLICIT_AUTHORIZATION"):
        guard.read_cases(asset.asset_id, principal="ordinary-runner")


def test_unregistered_path_traversal_expired_grant_and_missing_hash_are_denied_and_audited(work_dir: Path) -> None:
    root = work_dir / "repo"
    root.mkdir()
    outside = work_dir / "outside.json"
    outside.write_text(json.dumps({"cases": [case_data("synthetic-outside")] }), encoding="utf-8")
    outside_asset = DatasetAsset(**{
        **asset_for(outside, digest=hashlib.sha256(outside.read_bytes()).hexdigest()).__dict__,
        "asset_id": "asset.outside",
        "source_path": "../outside.json",
    })
    no_hash_path = root / "no-hash.json"
    no_hash_path.write_text('{"cases":[]}', encoding="utf-8")
    no_hash_asset = DatasetAsset(**{
        **asset_for(no_hash_path, digest=None, scoreability="NOT_SCOREABLE").__dict__,
        "asset_id": "asset.no-hash",
    })
    regular_path = root / "regular.json"
    regular_content = json.dumps({"cases": [case_data("synthetic-expired")]})
    regular_path.write_text(regular_content, encoding="utf-8")
    regular_asset = DatasetAsset(**{
        **asset_for(regular_path, digest=hashlib.sha256(regular_content.encode()).hexdigest()).__dict__,
        "asset_id": "asset.expired-grant",
    })
    registry = DatasetRegistry([outside_asset, no_hash_asset, regular_asset])
    audit_path = work_dir / "audit.jsonl"
    guard = AccessGuard(registry, root, audit_path)

    with pytest.raises(AccessDenied, match="PATH_OUTSIDE_REGISTERED_REPOSITORY"):
        guard.read_cases(outside_asset.asset_id, principal="synthetic-runner")
    with pytest.raises(AccessDenied, match="ASSET_INTEGRITY_NOT_VERIFIED"):
        guard.read_cases(no_hash_asset.asset_id, principal="synthetic-runner")
    expired = AccessGrant(
        "grant-expired", "synthetic-steward", "2026-10-23T00:00:00Z",
        (regular_asset.asset_id,), ("dev",), "synthetic test", "2026-10-23T01:00:00Z",
    )
    with pytest.raises(AccessDenied, match="NO_VALID_ASSET_SPLIT_GRANT"):
        guard.read_cases(regular_asset.asset_id, principal="synthetic-runner", grant=expired)
    with pytest.raises(AccessDenied, match="UNREGISTERED_ASSET"):
        guard.read_cases("asset.not-registered", principal="synthetic-runner")

    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(audit) == 4
    assert all(entry["decision"] == "DENIED" for entry in audit)
    assert {entry["reason"] for entry in audit} >= {
        "PATH_OUTSIDE_REGISTERED_REPOSITORY", "ASSET_INTEGRITY_NOT_VERIFIED",
        "NO_VALID_ASSET_SPLIT_GRANT_OR_NOT_SCOREABLE", "UNREGISTERED_ASSET_CONTENT_READ",
    }
    assert "synthetic-expired" not in json.dumps(audit)


def test_metadata_only_script_is_allowlisted_and_has_no_content_search_option() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "m6_1_asset_metadata.py"
    source = script.read_text(encoding="utf-8")
    assert '"registry" / "m6.1-dataset-registry-r1.json"' in source
    assert "argparse" not in source
    assert ".rglob(" not in source and "os.walk(" not in source
    assert "safe_metadata()" in source
