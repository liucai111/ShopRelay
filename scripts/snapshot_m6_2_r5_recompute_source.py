"""Preserve the R5-bound source needed for historical score recomputation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_1.personal_dev import implementation_digest

PACKAGE = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
SOURCE_FILES = (
    "eval/m6_1/personal_dev.py", "eval/m6_1/personal_dev_database.py",
    "eval/m6_1/personal_dev_runtime.py", "eval/m6_1/shipment_fixture_reset.py",
    "eval/m6_1/shipment_fixture_loader.py", "app/orchestration/runtime.py",
    "app/m1/models.py", "app/m1/services.py", "scripts/run_m6_1_d1_personal_dev.py",
    "app/m0/config.py", "app/m1/chat.py", "app/m1/routing.py", "app/m1/chat_responses.py",
    "app/m2/policy_execution.py", "app/orchestration/adapters.py", "app/orchestration/graph.py",
    "app/orchestration/state.py", "app/orchestration/checkpointer.py",
    "app/orchestration/observability.py", "app/orchestration/persistence.py", "app/m5/memory.py",
    "eval/m6_1/fixture_read_models.py", "app/m3/agents.py", "app/m3/policy_tools.py",
    "app/m3/order_tools.py", "app/m3/product_tools.py", "app/m2/retrieval.py",
    "eval/m6_1/personal_dev_authorization.py", "eval/m6_1/budget.py", "eval/m6_1/store.py",
    "app/m1/chat_execution.py", "eval/m6_1/personal_dev_diagnostic_checks.py",
    "scripts/check_m6_1_d1_diagnostic.py", "scripts/recompute_m6_1_d1_diagnostic_r7.py",
    "eval/m6_1/personal_dev_scoring.py", "eval/m6_1/contracts.py",
    "eval/m6_1/personal_dev_budget_carryforward.py", "app/m1/api.py", "app/m3/graph.py",
    "eval/m6_1/provider_diagnostic.py", "scripts/run_m6_1_d1_provider_diagnostic_r6.py",
    "eval/m6_1/personal_smoke.py", "scripts/run_m6_1_d1_personal_smoke.py",
    "eval/m6_1/personal_dev_batch_m62.py", "scripts/run_m6_2_r324_personal_dev_batch.py",
    "scripts/run_m6_2_r324_personal_dev_batch_r5.py", "scripts/prepare_m6_2_r324_batch_r5.py",
    "app/m3/after_sales_tools.py", "scripts/recompute_m6_2_r5_personal_batch.py",
)
HISTORICAL_DIGEST = "57bee2b1765f0a20a61c9f4a722e4908230fb934ca31236bf32fbad0384010f3"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if PACKAGE.exists():
        raise FileExistsError(f"Refusing to overwrite historical snapshot package: {PACKAGE}")
    current = implementation_digest(ROOT)
    if current != HISTORICAL_DIGEST:
        raise RuntimeError(f"R5 bound implementation changed before snapshot: {current}")
    src_root = PACKAGE / "source-snapshot"
    records = []
    for relative in SOURCE_FILES:
        source = ROOT / relative
        source_sha = sha(source)
        target = src_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha(target) != source_sha:
            raise RuntimeError(f"Snapshot verification failed: {relative}")
        records.append({"path": relative, "sha256": source_sha})
    recompute_script = ROOT / "scripts/recompute_m6_2_r5_personal_batch.py"
    recompute_target = PACKAGE / "historical-recompute-script.py"
    shutil.copyfile(recompute_script, recompute_target)
    manifest = {
        "schema_version": "m6.2-r5-recompute-source-snapshot-r1",
        "created_before_runtime_source_modification": True,
        "r5_batch_id": "M62-R324-REMAINDER-R5-20261029-a5387a6411f2",
        "r5_implementation_digest": current,
        "r5_manifest_sha256": sha(ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/batch-manifest-r5-final8.json"),
        "r5_rules_sha256": sha(ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/locked-rules-r5-final8.json"),
        "r5_dataset_sha256": sha(ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"),
        "historical_recompute_script_sha256": sha(recompute_target),
        "runtime_source_files": records,
        "snapshot_scope": "Runtime/scoring source needed to recompute R5; no credentials, Candidate/Frozen content, database file, or secrets copied.",
    }
    (PACKAGE / "historical-source-snapshot-r1.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "SNAPSHOT_VERIFIED", "implementation_digest": current,
        "files": len(records) + 1, "package": str(PACKAGE.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
