from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
import shutil

import pytest

from eval.m6_1.budget import BudgetLedger, BudgetLimits, BudgetStop
from eval.m6_1.aggregate import aggregate
from eval.m6_1.contracts import ExposureStatus, IndependenceStatus, ReviewStatus, Scoreability, Split
from eval.m6_1.experiment import preregister_experiment
from eval.m6_1.grader import CodeGrader
from eval.m6_1.grants import GrantDenied, SyntheticGrantAuthority
from eval.m6_1.judge import JudgeRequest, JudgeRubric, SyntheticJudgeStub
from eval.m6_1.registry import DatasetAsset, DatasetRegistry
from eval.m6_1.reviews import ReviewService
from eval.m6_1.runner import run_synthetic_experiment
from eval.m6_1.store import ResultStore, StoreError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "eval/m6_1/fixtures/m6.1-b-synthetic-cases-r1.json"


def make_asset(path: Path, *, asset_id: str = "m61b-synthetic-cases-r1", split: str = "synthetic") -> DatasetAsset:
    return DatasetAsset(asset_id=asset_id, asset_type="synthetic_fixture", version="synthetic-r1", parent_asset_id=None,
        split=split, case_count=1, source_path=path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "a"*64,
        review_status=ReviewStatus.NOT_REVIEWED.value, reviewer_id=None, reviewed_at_utc=None, review_method=None,
        independence_status=IndependenceStatus.UNKNOWN.value, exposure_status=ExposureStatus.UNKNOWN.value,
        scoreability=Scoreability.NOT_VERIFIED.value, locked_at_utc=None, access_policy="SYNTHETIC_ONLY", limitations=())


def lock_experiment(store: ResultStore, *, experiment_id="exp.synthetic.test", limits=None):
    limits = limits or BudgetLimits(tool_calls=24, max_calls_per_trial=4, max_concurrent_trials=2, max_duration_seconds=30)
    config = {"fixture_version":"synthetic-r1", "grader_version":"code:1", "judge_version":"stub:1",
        "fixture_sha256":"1"*64,
        "retriever_config":{"mode":"not_invoked"}, "knowledge_index_version":None,
        "model_versions":{"executor":"synthetic:1"}, "prompt_versions":{"runner":"none"},
        "tool_permissions":["synthetic_read"], "retry_policy":{"max_retries":0,"automatic_replay":False},
        "random_seed":12, "environment":{"kind":"test"}, "budget_limits":limits.to_dict(), "data_version":"synthetic-r1"}
    exp = preregister_experiment(store=store, experiment_id=experiment_id, config=config, git_commit="0fc9590",
                                strategy="synthetic_deterministic", scoring_version="rubric-r1").experiment
    return exp, BudgetLedger(store, experiment_id, limits)


def test_synthetic_grant_runner_store_grader_judge_and_aggregate_are_integrated(tmp_path: Path):
    store = ResultStore(tmp_path / "results.sqlite3")
    exp, ledger = lock_experiment(store)
    asset = make_asset(FIXTURE)
    registry = DatasetRegistry([asset])
    authority = SyntheticGrantAuthority(registry, repository_root=ROOT, issuer="issuer", trusted_issuers={"issuer"}, signing_key=b"x"*32)
    grant = authority.issue(asset.asset_id, subject="runner")
    cases = authority.read_cases(grant, subject="runner")
    assert len(cases) == 12 and all(case.split == "synthetic" for case in cases)
    report = run_synthetic_experiment(cases=cases, experiment_id=exp.experiment_id, store=store, ledger=ledger)
    assert report["unique_case_count"] == 12 and report["trial_count"] == 12
    assert report["run_reference_count"] > 0
    assert report["lifecycle_status_counts"] == {"BUDGET_STOP":1,"ENV_ERROR":1,"MODEL_UNAVAILABLE":1,"SUCCEEDED":8,"UNKNOWN_RESULT":1}
    assert report["unknown_result_count"] == 1
    assert report["task_success_from_run_status"] is False
    trials = store.trials(exp.experiment_id)
    assert all(store.raw_result(row["trial_id"]) is not None for row in trials)
    assert sum(bool(store.raw_result(row["trial_id"]).get("synthetic_judge")) for row in trials) == 3
    assert report["budget"]["provider_requests"] == report["budget"]["judge_requests"] == 0
    # Replaying the same experiment never re-executes already persisted trials.
    again = run_synthetic_experiment(cases=cases, experiment_id=exp.experiment_id, store=store, ledger=ledger)
    assert again["trial_count"] == 12
    assert store.connect().execute("SELECT count(*) FROM trials").fetchone()[0] == 12


def test_experiment_lock_is_canonical_and_immutable(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    exp, _ = lock_experiment(store)
    store.lock_experiment(exp, "2026-10-24T00:00:00Z")
    changed = dict(exp.config_snapshot)
    changed["random_seed"] = 99
    with pytest.raises(StoreError, match="immutable"):
        lock_experiment(store, experiment_id=exp.experiment_id)
        # A changed snapshot under the same id is rejected by the store lock.
        from eval.m6_1.contracts import EvalExperiment, sha256_json
        conflicting = EvalExperiment(exp.experiment_id, changed, sha256_json(changed), exp.git_commit,
            exp.knowledge_index_version, dict(exp.model_versions), dict(exp.prompt_versions), exp.strategy,
            tuple(exp.tool_permissions), dict(exp.budget), exp.data_version, exp.scoring_version)
        store.lock_experiment(conflicting, "2026-10-24T00:00:00Z")


def test_experiment_preregistration_rejects_unknown_or_missing_config_fields(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    config = {"fixture_version":"synthetic-r1", "fixture_sha256":"1"*64, "grader_version":"code:1", "judge_version":"stub:1",
        "retriever_config":{}, "knowledge_index_version":None, "model_versions":{}, "prompt_versions":{},
        "tool_permissions":[], "retry_policy":{}, "random_seed":1, "environment":{}, "budget_limits":{}, "data_version":"synthetic-r1"}
    with pytest.raises(ValueError, match="unknown fields"):
        preregister_experiment(store=store, experiment_id="exp.bad", config={**config,"unreviewed_option":True}, git_commit="0123456", strategy="SYNTHETIC", scoring_version="r1")
    with pytest.raises(ValueError, match="missing required"):
        preregister_experiment(store=store, experiment_id="exp.bad", config={key:value for key,value in config.items() if key != "fixture_sha256"}, git_commit="0123456", strategy="SYNTHETIC", scoring_version="r1")


def test_budget_reservation_is_atomic_and_unknown_usage_is_not_zero(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    limits = BudgetLimits(provider_calls=2, total_tokens=40, estimated_usd=1.0, max_calls_per_trial=1, max_concurrent_trials=20)
    _, ledger = lock_experiment(store, limits=limits)

    def reserve_and_settle(i: int):
        try:
            reservation = ledger.reserve(trial_id=f"t{i}", category="provider", tokens_reserved=20, estimated_usd_reserved=.5)
        except BudgetStop:
            return False
        ledger.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=None, estimated_usd=None)
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve_and_settle, range(12)))
    assert sum(results) == 2
    snapshot = ledger.snapshot()
    assert snapshot["committed_calls"] == 2
    assert snapshot["tokens"] is None and snapshot["estimated_usd"] is None
    with pytest.raises(BudgetStop):
        ledger.reserve(trial_id="overflow", category="provider")


def test_budget_concurrent_active_reservations_are_bounded(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    limits = BudgetLimits(provider_calls=10, total_tokens=100, estimated_usd=10, max_concurrent_trials=1)
    _, ledger = lock_experiment(store, limits=limits)
    held = ledger.reserve(trial_id="first", category="provider")
    with pytest.raises(BudgetStop, match="concurrent"):
        ledger.reserve(trial_id="second", category="provider")
    ledger.settle(held, status="RELEASE", actual_calls=0, actual_tokens=None, estimated_usd=None)
    assert ledger.reserve(trial_id="second", category="provider")


def test_budget_retry_limits_and_unknown_usage_reconciliation(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    limits = BudgetLimits(provider_calls=3, total_tokens=50, estimated_usd=1, max_retries=1, max_concurrent_trials=2)
    _, ledger = lock_experiment(store, limits=limits)
    reservation = ledger.reserve(trial_id="trial.reconcile", category="provider", tokens_reserved=10, estimated_usd_reserved=.1)
    ledger.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=None, estimated_usd=None)
    assert ledger.snapshot()["tokens"] is None and ledger.snapshot()["estimated_usd"] is None
    ledger.settle(reservation, status="RECONCILE", actual_calls=1, actual_tokens=8, estimated_usd=.08, billed_usd=.08)
    snapshot = ledger.snapshot()
    assert snapshot["tokens"] == 8 and snapshot["estimated_usd"] == .08 and snapshot["billed_usd"] == .08
    retry = ledger.reserve_retry(trial_id="trial.reconcile", retry_index=1, category="provider")
    with pytest.raises(BudgetStop, match="already used"):
        ledger.reserve_retry(trial_id="trial.reconcile", retry_index=1, category="provider")
    with pytest.raises(BudgetStop, match="retry limit"):
        ledger.reserve_retry(trial_id="trial.reconcile", retry_index=2, category="provider")
    ledger.settle(retry, status="RELEASE", actual_calls=0, actual_tokens=None, estimated_usd=None)


def test_sqlite_budget_hard_cap_is_shared_across_processes(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    limits = BudgetLimits(provider_calls=1, total_tokens=10, estimated_usd=1, max_concurrent_trials=4)
    lock_experiment(store, limits=limits)
    code = r'''import sys,time
from pathlib import Path
from eval.m6_1.budget import BudgetLedger,BudgetLimits,BudgetStop
from eval.m6_1.store import ResultStore
s=ResultStore(Path(sys.argv[1])); l=BudgetLedger(s,"exp.synthetic.test",BudgetLimits(provider_calls=1,total_tokens=10,estimated_usd=1,max_concurrent_trials=4))
try:
 r=l.reserve(trial_id="process",category="provider",tokens_reserved=10,estimated_usd_reserved=1); print("RESERVED",flush=True); time.sleep(.15); l.settle(r,status="COMMIT",actual_calls=1,actual_tokens=5,estimated_usd=.5)
except BudgetStop: print("DENIED",flush=True)
'''
    processes = [subprocess.Popen([sys.executable, "-c", code, str(store.path)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    outputs = [proc.communicate(timeout=15) for proc in processes]
    assert all(proc.returncode == 0 for proc in processes), outputs
    assert sorted(stdout.strip() for stdout, _ in outputs) == ["DENIED", "RESERVED"]
    assert BudgetLedger(store, "exp.synthetic.test", limits).snapshot()["committed_calls"] == 1


def test_interrupted_running_trial_is_recovered_as_unknown_without_reexecution(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    exp, ledger = lock_experiment(store)
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]
    from eval.m6_1.contracts import EvalCase
    parsed = EvalCase.from_dict(case)
    trial_id = f"trial.{exp.experiment_id}.{parsed.case_id}.1"
    store.add_trial_event("event.interrupted", trial_id, "RUNNING", {"case_id":parsed.case_id}, "2026-10-24T00:00:00Z")

    class MustNotRun:
        def planned_tools(self, case): return ()
        def planned_status(self, case): return None
        def execute(self, case, trial_id): raise AssertionError("recovery must not replay execution")

    report = run_synthetic_experiment(cases=[parsed], experiment_id=exp.experiment_id, store=store, ledger=ledger, executor=MustNotRun())
    assert report["lifecycle_status_counts"] == {"UNKNOWN_RESULT":1}
    assert store.trial(trial_id)["lifecycle_status"] == "UNKNOWN_RESULT"
    assert store.raw_result(trial_id)["recovery"] == "NO_REPLAY"


def test_grant_rejects_protected_split_expired_tampered_and_revoked(tmp_path: Path):
    synthetic = make_asset(FIXTURE)
    registry = DatasetRegistry([synthetic])
    authority = SyntheticGrantAuthority(registry, repository_root=ROOT, issuer="trusted", trusted_issuers={"trusted"}, signing_key=b"k"*32)
    grant = authority.issue(synthetic.asset_id, subject="runner")
    authority.revoke(grant)
    with pytest.raises(GrantDenied, match="revoked"):
        authority.read_cases(grant, subject="runner")
    candidate = DatasetAsset(**{**synthetic.__dict__, "asset_id":"opaque.synthetic-test", "split":"test_candidate"})
    candidate_registry = DatasetRegistry([candidate])
    candidate_authority = SyntheticGrantAuthority(candidate_registry, repository_root=ROOT, issuer="trusted", trusted_issuers={"trusted"}, signing_key=b"k"*32)
    with pytest.raises(GrantDenied, match="only newly registered"):
        candidate_authority.issue(candidate.asset_id, subject="runner")
    valid_authority = SyntheticGrantAuthority(registry, repository_root=ROOT, issuer="trusted", trusted_issuers={"trusted"}, signing_key=b"k"*32)
    expired = replace(valid_authority.issue(synthetic.asset_id, subject="runner"), expires_at_utc="2000-01-01T00:00:00Z")
    with pytest.raises(GrantDenied, match="signature invalid|expired"):
        valid_authority.read_cases(expired, subject="runner")


def test_raw_trials_are_insert_only_and_same_case_can_have_multiple_trials(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    exp, ledger = lock_experiment(store)
    from eval.m6_1.contracts import EvalTrial
    trial = EvalTrial("trial.a", "case.a", exp.experiment_id, 1, (), "NOT_RUN", (), (), "raw://a", None, {}, {})
    store.append_trial(trial, "2026-10-24T00:00:00Z")
    store.append_raw_result("trial.a", {"raw":"fixture"}, "2026-10-24T00:00:00Z")
    with pytest.raises(Exception):
        store.append_trial(trial, "2026-10-24T00:00:01Z")
    with pytest.raises(Exception):
        with store.transaction() as db:
            db.execute("UPDATE trials SET lifecycle_status='FAILED' WHERE trial_id='trial.a'")
    trial2 = EvalTrial("trial.b", "case.a", exp.experiment_id, 2, ("run.one", "run.two"), "SUCCEEDED", (), (), "raw://b", None, {}, {})
    store.append_trial(trial2, "2026-10-24T00:00:02Z")
    assert len(store.trials(exp.experiment_id)) == 2
    summary = aggregate(store.trials(exp.experiment_id), [], ledger.snapshot())
    assert summary["unique_case_count"] == 1 and summary["trial_count"] == 2


def test_synthetic_executor_supports_one_trial_with_multiple_run_references():
    from eval.m6_1.runner import SyntheticExecutor
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    row = next(case for case in fixture["cases"] if case["expected_outcome"].get("multiple_runs"))
    status, _, payload = SyntheticExecutor().execute(row, "trial.multi")
    assert status == "SUCCEEDED" and len(payload["run_ids"]) == 2


def test_code_grader_separates_run_success_from_task_and_safety(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][5]
    grade = CodeGrader().grade(trial_id="trial.safe", expected=case["expected_outcome"],
        result={"route":"AGENT","tools":[],"schema_valid":True,"ownership_check":"FAIL","unconfirmed_write":False,
                "evidence_ids":[],"business_outcome":"ACCESS_DENIED","task_success_observable":True,"semantic_correctness":"PASS","safety_result":"PASS"},
        lifecycle="SUCCEEDED")
    assert grade.outcome.run_success == "PASS"
    assert grade.outcome.task_success == "PASS"
    assert grade.outcome.safety_result == "PASS"
    unsafe = CodeGrader().grade(trial_id="trial.unsafe", expected=case["expected_outcome"],
        result={"route":"AGENT","tools":["not_allowed"],"schema_valid":True,"ownership_check":"PASS","unconfirmed_write":True,
                "evidence_ids":[],"business_outcome":"WRITTEN","task_success_observable":False,"semantic_correctness":"UNKNOWN","safety_result":"FAIL"},
        lifecycle="SUCCEEDED")
    assert unsafe.outcome.run_success == "PASS" and unsafe.outcome.task_success == "FAIL" and unsafe.outcome.safety_result == "FAIL"
    assert unsafe.outcome.answer_correctness == "UNKNOWN"


def test_human_review_records_are_append_only_and_synthetic_identity_is_not_human(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    exp, _ = lock_experiment(store)
    from eval.m6_1.contracts import EvalTrial
    store.append_trial(EvalTrial("trial.review", "case.review", exp.experiment_id, 1, (), "SUCCEEDED", (), (), None, None, {}, {}), "2026-10-24T00:00:00Z")
    service = ReviewService(store, trusted_human_reviewers={"trusted-reviewer"}, allow_synthetic_decisions=True)
    task = service.open_task("trial.review", "semantic review", [])
    with pytest.raises(ValueError, match="explicit"):
        service.decide(task["review_id"], reviewer_identity="reviewer", reviewer_type="SYNTHETIC_NOT_HUMAN", decision="PASS", rationale="fixture")
    decision = service.decide(task["review_id"], reviewer_identity="SYNTHETIC_TEST_REVIEWER", reviewer_type="SYNTHETIC_NOT_HUMAN", decision="UNKNOWN", rationale="synthetic only")
    assert decision["reviewer_type"] == "SYNTHETIC_NOT_HUMAN"
    assert store.review_status(task["review_id"]) == "REVIEWED"
    with pytest.raises(Exception):
        service.decide(task["review_id"], reviewer_identity="SYNTHETIC_TEST_REVIEWER", reviewer_type="SYNTHETIC_NOT_HUMAN", decision="PASS", rationale="duplicate")


def test_human_review_requires_server_side_reviewer_allowlist(tmp_path: Path):
    store = ResultStore(tmp_path / "result.sqlite3")
    exp, _ = lock_experiment(store)
    from eval.m6_1.contracts import EvalTrial
    store.append_trial(EvalTrial("trial.human-auth", "case.auth", exp.experiment_id, 1, (), "SUCCEEDED", (), (), None, None, {}, {}), "2026-10-24T00:00:00Z")
    service = ReviewService(store, trusted_human_reviewers={"reviewer-1"})
    task = service.open_task("trial.human-auth", "needs review", [])
    with pytest.raises(PermissionError, match="allowlist"):
        service.decide(task["review_id"], reviewer_identity="user-supplied", reviewer_type="HUMAN", decision="PASS", rationale="not trusted")
