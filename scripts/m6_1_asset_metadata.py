"""Print the allowlisted M6.1 asset metadata projection only.

This command reads the versioned registry JSON and never opens any asset's
source_path. It deliberately has no arbitrary path, query, or content-search
arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from m6_1.registry import DatasetRegistry  # noqa: E402


def main() -> int:
    registry_path = ROOT / "eval" / "registry" / "m6.1-dataset-registry-r1.json"
    registry = DatasetRegistry.load_metadata(registry_path)
    print(json.dumps(registry.safe_metadata(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
