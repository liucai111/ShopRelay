"""Write a scoped SHA-256 inventory for the R5 closeout package."""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_REL = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029")
PKG = ROOT / PKG_REL
OUTPUT_REL = PKG_REL / "SHA256SUMS-r5-final.md"

EXTRA_FILES = [
    "docs/progress.md",
    "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json",
    "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json",
    "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/batch-manifest-r4.json",
    "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/authorization-r2.json",
    "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/SHA256SUMS-r4-final.md",
    "docs/eval/personal-dev-runs/08420baa7f3e4b10b85d75ca7d4de5b6.json",
    "app/m3/after_sales_tools.py",
    "app/m3/policy_tools.py",
    "app/m3/order_tools.py",
    "eval/m6_1/personal_dev.py",
    "eval/m6_1/personal_dev_batch_m62.py",
    "scripts/run_m6_2_r324_personal_dev_batch_r5.py",
    "scripts/prepare_m6_2_r324_batch_r5.py",
    "scripts/recompute_m6_2_r5_personal_batch.py",
    "scripts/render_m6_2_r5_closeout.py",
    "scripts/write_m6_2_r5_sha256.py",
    "tests/test_m6_2_personal_dev_batch_r1.py",
    "tests/test_m3_3_product_agent.py",
    "tests/test_m3_2_order_logistics.py",
    "tests/test_m3_4_policy_agent.py",
    "tests/test_m3_5_after_sales.py",
    "tests/test_langgraph_g2.py",
]


def description(relative: str) -> str:
    if relative.startswith(str(PKG_REL).replace("\\", "/") + "/"):
        return "R5 versioned batch evidence/artifact"
    if relative == "docs/progress.md":
        return "Current progress log including R5 append"
    if relative.startswith("eval/m6_1/drafts/"):
        return "Bound r3.2.4 Dev dataset"
    if "batch-r1-20261029" in relative:
        return "Original 40-case source admission matrix"
    if "batch-r4-20261029" in relative or "personal-dev-runs/08420" in relative:
        return "Preserved R4 historical binding/evidence"
    if relative.startswith("tests/"):
        return "Complete targeted regression source"
    return "Changed runtime/offline implementation source"


def main() -> None:
    files: set[Path] = set()
    for path in PKG.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == OUTPUT_REL.as_posix() or "__pycache__" in path.parts or any(
            part.startswith("pytest-tmp") for part in path.parts
        ):
            continue
        files.add(path)
    for relative in EXTRA_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(relative)
        files.add(path)

    entries = []
    for path in sorted(files, key=lambda item: item.relative_to(ROOT).as_posix().casefold()):
        relative = path.relative_to(ROOT).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, relative, description(relative)))

    lines = [
        "# M6.2 R5 final SHA-256 inventory",
        "",
        "Hashes bind the files at closeout time. Paths and descriptions are separate fields. The inventory covers this R5 evidence package (excluding transient pytest temp fixtures and this self-referential inventory), the exact dataset/admission source, preserved R4 predecessor evidence, changed runtime/offline implementation and targeted regression sources, and the appended progress log.",
        "",
        "| SHA-256 | Path | Description |",
        "|---|---|---|",
    ]
    lines.extend(f"| `{digest}` | `{relative}` | {desc} |" for digest, relative, desc in entries)
    lines.append("")
    (ROOT / OUTPUT_REL).write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_REL.as_posix()} with {len(entries)} file hashes")


if __name__ == "__main__":
    main()
