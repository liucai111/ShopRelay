from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_1.smoke_scoring_v2 import score_inputs

PACKAGE = ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028"


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute versioned personal Dev Smoke score projection offline.")
    parser.add_argument("--inputs", default=str(PACKAGE / "scoring-inputs-r2.json"))
    parser.add_argument("--adjudication", default=str(PACKAGE / "provisional-semantic-adjudication-r2.json"))
    parser.add_argument("--output", default=str(PACKAGE / "scores-r2.json"))
    args = parser.parse_args()
    result = score_inputs(ROOT, Path(args.inputs), Path(args.adjudication))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "RECOMPUTED", "output": str(Path(args.output)),
        "attempt_count": result["recomputation"]["attempt_count"],
        "primary_case_count": result["recomputation"]["primary_case_count"],
        "task_success_by_binding": result["metrics"]["task_success_by_binding"],
        "mixed_binding_task_success": result["metrics"]["mixed_binding_task_success"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
