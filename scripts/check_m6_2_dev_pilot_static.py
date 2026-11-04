from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_2.pilot import build_static_pilot_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="No-model static check for the fixed M6.2 non-Frozen Dev Pilot.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_static_pilot_preflight(ROOT)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "case_count": result["case_count"],
        "digest": result["code_binding"]["digest"], "provider_called": False,
        "run_created": False, "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["status"] == "STATIC_PREFLIGHT_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
