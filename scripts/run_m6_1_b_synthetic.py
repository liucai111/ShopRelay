"""Run the M6.1-B deterministic synthetic-only harness; no service/provider is contacted."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.m6_1.budget import BudgetLedger, BudgetLimits
from eval.m6_1.contracts import ExposureStatus, IndependenceStatus, ReviewStatus, Scoreability, Split
from eval.m6_1.experiment import preregister_experiment
from eval.m6_1.grants import SyntheticGrantAuthority
from eval.m6_1.registry import DatasetAsset, DatasetRegistry
from eval.m6_1.runner import run_synthetic_experiment
from eval.m6_1.store import ResultStore


def main() -> int:
    if sys.argv[1:2] == ["--formal-postgres-target-init"]:
        from eval.m6_1.formal_assets import confined
        from eval.m6_1.personal_dev_database import FormalPostgresDatabase
        binding = json.loads(confined(ROOT, sys.argv[2]).read_text(encoding="utf8"))
        target = FormalPostgresDatabase(ROOT, binding)
        engine, _ = target.open(initialize=True)
        from app.orchestration.checkpointer import open_checkpointer,run_checkpointer_async
        async def setup():
            async with open_checkpointer(target.database_url, binding["checkpoint_schema"]) as saver:
                await saver.setup()
        from sqlalchemy import text
        with engine.begin() as db:
            import re
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}",binding["checkpoint_schema"]): raise ValueError("invalid checkpoint schema")
            db.execute(text('CREATE SCHEMA "'+binding["checkpoint_schema"]+'"'))
        run_checkpointer_async(setup())
        engine.dispose()
        print(json.dumps({"profile":target.profile,"identity":target.identity,"manifest":binding["identity_manifest"],"model_requests":0}))
        return 0
    if sys.argv[1:2] == ["--asset-workflow-config"]:
        from eval.m6_1.formal_assets import workflow_cli
        print(json.dumps(workflow_cli(sys.argv[1:], repository_root=ROOT), ensure_ascii=False))
        return 0
    if sys.argv[1:2] in (["--formal-target-init"], ["--formal-ledger-init"]):
        from eval.m6_1.formal_assets import confined
        if len(sys.argv) != 3:
            raise ValueError("one explicit relative provisioning path required")
        if sys.argv[1] == "--formal-target-init":
            from eval.m6_1.formal_application import initialize_target
            result = initialize_target(ROOT, sys.argv[2])
        else:
            path = confined(ROOT, sys.argv[2])
            if not sys.argv[2].replace("\\", "/").startswith("data/eval/formal-ledgers/") or path.exists():
                raise ValueError("new dedicated formal ledger path required")
            ResultStore(path)
            result = {"status": "DEDICATED_LEDGER_INITIALIZED_NO_GRANT", "path": sys.argv[2]}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if sys.argv[1:2]==["--formal-config"]:
        from eval.m6_1.execution_modes import formal_cli
        result=formal_cli(sys.argv[1:],repository_root=ROOT)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    fixture = ROOT / "eval/m6_1/fixtures/m6.1-b-synthetic-cases-r1.json"
    fixture_bytes = fixture.read_bytes()
    fixture_hash = hashlib.sha256(fixture_bytes).hexdigest()
    version = "synthetic-r1"
    asset = DatasetAsset(
        asset_id="m61b-synthetic-cases-r1", asset_type="eval_case_fixture", version=version,
        parent_asset_id=None, split=Split.SYNTHETIC.value, case_count=12,
        source_path=fixture.relative_to(ROOT).as_posix(), sha256=fixture_hash,
        review_status=ReviewStatus.NOT_REVIEWED.value, reviewer_id=None, reviewed_at_utc=None,
        review_method=None, independence_status=IndependenceStatus.UNKNOWN.value,
        exposure_status=ExposureStatus.UNKNOWN.value, scoreability=Scoreability.NOT_VERIFIED.value,
        locked_at_utc=None, access_policy="SYNTHETIC_ONLY_EXPLICIT_GRANT", limitations=("Fresh synthetic fixtures; not an independent quality set",),
    )
    registry = DatasetRegistry([asset])
    authority = SyntheticGrantAuthority(registry, repository_root=ROOT, issuer="m61b-local-synthetic-issuer",
        trusted_issuers={"m61b-local-synthetic-issuer"}, signing_key=secrets.token_bytes(32))
    grant = authority.issue(asset.asset_id, subject="synthetic-runner")
    cases = authority.read_cases(grant, subject="synthetic-runner")

    output = ROOT / "docs/probes/m6.1-b-synthetic-experiment-r4"
    output.mkdir(parents=True, exist_ok=True)
    store = ResultStore(output / "results.sqlite3")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    limits = BudgetLimits(provider_calls=0, embedding_calls=0, judge_calls=0, total_tokens=0,
                          estimated_usd=0.0, tool_calls=24, max_calls_per_trial=4, max_concurrent_trials=1,
                          max_duration_seconds=300, max_retries=0)
    config = {
        "fixture_version": version, "fixture_sha256": fixture_hash,
        "runner_version":"1.2", "registry_version":"synthetic-memory-registry-r1",
        "grader_version": "m6.1-b-code-grader:1.0", "judge_version": "synthetic-stub:r1",
        "retriever_config": {"mode": "not_invoked_synthetic_only"},
        "knowledge_index_version": None, "model_versions": {"executor": "synthetic-deterministic:r1"},
        "prompt_versions": {"runner": "none"}, "tool_permissions": ["synthetic_order_lookup"],
        "retry_policy": {"max_retries": 0, "automatic_replay": False}, "random_seed": 6101,
        "environment": {"python": sys.version.split()[0], "platform": sys.platform, "runner": "synthetic-only"},
        "budget_limits": limits.to_dict(), "data_version": version,
    }
    locked = preregister_experiment(store=store, experiment_id="m61b.synthetic.exp.r4", config=config,
                                   git_commit=commit, strategy="synthetic_deterministic", scoring_version="synthetic-rubric-r1")
    ledger = BudgetLedger(store, locked.experiment.experiment_id, limits)
    summary = run_synthetic_experiment(cases=cases, experiment_id=locked.experiment.experiment_id, store=store, ledger=ledger)
    trials = store.trials(locked.experiment.experiment_id)
    outcomes = store.outcomes_for_experiment(locked.experiment.experiment_id)
    reviews = store.reviews_for_experiment(locked.experiment.experiment_id)
    scores = store.scores(locked.experiment.experiment_id)
    raw_results = [{"trial_id": trial["trial_id"], "raw_result": store.raw_result(trial["trial_id"])} for trial in trials]
    event_history = [{"trial_id": trial["trial_id"], "events": store.trial_events(trial["trial_id"])} for trial in trials]
    judge_stub_calls = sum(1 for row in raw_results if row["raw_result"].get("synthetic_judge"))
    payload = {
        "schema_version": "1.0", "experiment": locked.experiment.to_dict(),
        "locked_at_utc": locked.locked_at_utc, "fixture_asset": asset.metadata(),
        "grant": {"grant_id": grant.grant_id, "asset_id": grant.asset_id, "split": grant.split,
                  "subject": grant.subject, "expires_at_utc": grant.expires_at_utc, "signature": "[OMITTED]"},
        "summary": summary, "trials": trials, "raw_results": raw_results, "trial_events": event_history,
        "outcomes": outcomes, "scores": scores, "human_review_records": reviews,
        "counts": {"cases": len(cases), "trials": len(trials), "unique_cases": len({c.case_id for c in cases}),
                   "synthetic_judge_stub_calls": judge_stub_calls, "external_provider_calls": 0,
                   "embedding_calls": 0, "external_judge_calls": 0, "business_writes": 0,
                   "pending_human_review_tasks": sum(record["status"] == "PENDING_HUMAN_REVIEW" for record in reviews),
                   "real_human_review_decisions": sum(bool(record["decision"] and record["decision"]["reviewer_type"] == "HUMAN") for record in reviews)},
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (output / "experiment-evidence.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"fixture_sha256":fixture_hash,
                "experiment_evidence_sha256":hashlib.sha256((output / "experiment-evidence.json").read_bytes()).hexdigest(),
                "summary_sha256":hashlib.sha256((output / "summary.json").read_bytes()).hexdigest(),
                "sqlite_results_sha256":hashlib.sha256((output / "results.sqlite3").read_bytes()).hexdigest(),
                "manifest_scope":"new synthetic assets only; no historical evaluation content"}
    (output / "evidence-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.relative_to(ROOT)), "fixture_sha256": fixture_hash,
                      "case_count": len(cases), "trial_count": len(trials), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
