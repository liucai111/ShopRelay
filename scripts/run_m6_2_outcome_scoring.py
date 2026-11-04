from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_2.outcome_scoring import build_confirmed_references, score_retrospective_smoke


PACKAGE = ROOT / "docs/eval/m6.2-outcome-pilot-preparation-r1-20261028"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build confirmed Dev references and recompute retrospective M6.2 Outcome scores offline.")
    parser.add_argument("--references", action="store_true")
    parser.add_argument("--score", action="store_true")
    parser.add_argument("--output-dir", default=str(PACKAGE))
    args = parser.parse_args()
    if not (args.references or args.score):
        parser.error("select --references and/or --score")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"external_judge_called": False, "provider_called": False}
    if args.references:
        result = build_confirmed_references(ROOT)
        path = out / "confirmed-dev-reference-bundle-r1.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["references"] = {"path": str(path), "counts": result["counts"]}
    if args.score:
        result = score_retrospective_smoke(ROOT)
        path = out / "retrospective-outcome-scores-r1.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["scores"] = {"path": str(path), "case_count": len(result["rows"]),
            "bindings": result["summary_by_binding"]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
