"""Build a path/hash-only inventory for the R6 closeout package."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    explicit = [
        "app/m1/chat.py", "app/m1/routing.py", "app/orchestration/runtime.py",
        "eval/m6_1/personal_dev_batch_m62.py", "scripts/run_m6_2_r324_personal_dev_batch_r6.py",
        "scripts/recompute_m6_2_r6_closeout.py", "scripts/write_m6_2_r6_narrative.py",
        "scripts/build_m6_2_r6_sha256_manifest.py", "tests/test_m1_chat_provider.py",
        "tests/test_m2_routing.py", "tests/test_m6_2_personal_dev_batch_r1.py",
        "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/batch-manifest-r5-final8.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/locked-rules-r5-final8.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/authorization-r5.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/execution-state-r5.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/execution-journal-r5.jsonl",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/closeout-r5-final.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/40-case-coverage-ledger-r5-r1.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/case-results-r5-r1.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/application-admission-offline-r1.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/batch-manifest-r4.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/reports/closeout-r4.json",
        "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/official-pricing-evidence-r4.json",
        "docs/eval/m6.2-dev-score-review-closeout-r1-20261029/eight-item-review-package-r1.json",
        "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmation-event-r1.json",
        "docs/eval/personal-dev-runs/08420baa7f3e4b10b85d75ca7d4de5b6.json",
        "docs/progress.md",
    ]
    paths = {p.resolve() for p in PKG.rglob("*") if p.is_file()}
    paths.update((ROOT / rel).resolve(strict=True) for rel in explicit)
    r5_results = json.loads((ROOT / explicit[20]).read_text(encoding="utf-8"))
    r6_results = json.loads((PKG / "reports/case-results-r6-r3.json").read_text(encoding="utf-8"))
    for row in r5_results["cases"]:
        if row["case_id"] in {"D1-PRODUCT-003", "D1-PRODUCT-006"}:
            paths.add((ROOT / row["archive_path"]).resolve(strict=True))
    for row in r6_results["cases"]:
        paths.add((ROOT / row["archive_path"]).resolve(strict=True))
    items = [{"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)}
             for path in sorted(paths, key=lambda x: x.relative_to(ROOT).as_posix().casefold())]
    json_path = PKG / "SHA256SUMS-r6-r3.json"
    md_path = PKG / "SHA256SUMS-r6-r3.md"
    document = {"schema_version": "m6.2-r324-r6-sha256-inventory-v1",
        "algorithm": "SHA-256", "path_and_hash_separate_fields": True,
        "self_referential_manifests_excluded": True, "items": items}
    if json_path.exists() or md_path.exists():
        raise SystemExit("REFUSE_OVERWRITE_SHA_MANIFEST")
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = ["# M6.2 R6 SHA-256 inventory", "", "SHA-256 is computed over the exact file bytes. Path and digest are separate columns. This inventory excludes itself to avoid recursive hashing.", "", "| Path | SHA-256 |", "|---|---|"]
    lines.extend(f"| `{item['path']}` | `{item['sha256']}` |" for item in items)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"files_hashed": len(items), "json": json_path.relative_to(ROOT).as_posix(),
                      "markdown": md_path.relative_to(ROOT).as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
