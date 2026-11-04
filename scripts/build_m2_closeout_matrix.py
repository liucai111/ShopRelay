"""Create an append-only M2 overall matrix revision for closeout findings."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/probes/m2-acceptance-matrix-r3.json"
OUTPUT = ROOT / "docs/probes/m2-acceptance-matrix-r4.json"


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite {OUTPUT.relative_to(ROOT)}")
    matrix = json.loads(SOURCE.read_text(encoding="utf-8"))
    matrix["version"] = "r4"
    matrix["created_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    matrix["supersedes"] = SOURCE.relative_to(ROOT).as_posix()
    matrix["checks"].extend([
        {
            "name": "M2_closeout_hard_negative_route_v2",
            "status": "PASS",
            "reason": "The approved-dev policy-only refund question no longer matches the live-business fallback; explicit live refund-status queries remain on the business-fact branch.",
            "evidence": ["app/m1/routing.py", "tests/test_m2_routing.py"],
            "scope": "deterministic offline routing regression only; original failed Run retained; post-fix real end-to-end NOT_RUN"
        },
        {
            "name": "M2_closeout_review_source_audit",
            "status": "PASS",
            "reason": "52 dev/validation route rows have unique IDs, unique normalized exact questions, valid strategy/safety enums, and no template group crosses splits.",
            "evidence": ["docs/probes/m2-closeout-source-audit-20261021-r1.json", "eval/review/route-m2.1-dev-validation-review-r2.csv"]
        },
        {
            "name": "M2_closeout_answer_disputes_human_adjudication",
            "status": "BLOCKED",
            "reason": "All eight prior answer disputes remain PENDING_HUMAN_REVIEW; no AI/Judge suggestion is treated as approval.",
            "evidence": ["eval/fidelity/m2.4-q2-human-review-r2.csv", "docs/m2-closeout-human-review.md"]
        },
        {
            "name": "M2_closeout_route_labels_human_adjudication",
            "status": "BLOCKED",
            "reason": "All 52 dev/validation route candidates remain PENDING_HUMAN_REVIEW; no formal route metrics can be calculated from candidate labels.",
            "evidence": "eval/review/route-m2.1-dev-validation-review-r2.csv"
        },
        {
            "name": "M2_closeout_post_fix_real_route_end_to_end",
            "status": "NOT_RUN",
            "reason": "No additional DeepSeek or API request was needed for deterministic route-rule repair; original failure remains immutable and original 5/6 selected-set result is unchanged."
        }
    ])
    matrix["m2_status"] = "BLOCKED"
    matrix["m3_started"] = False
    matrix["notes"]["m2_closeout"] = {
        "hard_negative_route": "fixed_offline_route_v2; real end-to-end not rerun",
        "answer_disputes_pending": 8,
        "route_candidates_pending": 52,
        "test_candidate": "SEALED_UNFROZEN_NOT_READ",
        "m2_r_engineering": "PASS",
        "m2_quality": "BLOCKED",
        "formal_release": "BLOCKED"
    }
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(matrix, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(OUTPUT.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
