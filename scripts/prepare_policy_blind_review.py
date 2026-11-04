"""Create a label/ranking-free test_candidate review packet from reviewed data."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
POLICIES = ROOT / "knowledge/policies.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_packet(dataset_path: Path = DATASET, output_dir: Path | None = None) -> tuple[Path, Path, Path]:
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    ds_sha = sha(dataset_path)
    out = output_dir or ROOT / "eval/review/blind"
    out.mkdir(parents=True, exist_ok=True)
    packet_path = out / f"policy-retrieval-blind-input-{ds_sha[:12]}.csv"
    policies_path = out / f"policy-source-pack-{ds_sha[:12]}.json"
    mapping_path = ROOT / "docs/probes" / f"blind-id-map-{ds_sha[:12]}.json"
    rows, mapping = [], {}
    for case in data["cases"]:
        if case.get("dataset_split") != "test_candidate":
            continue
        blind_id = "blind-" + hashlib.sha256((ds_sha + case["case_id"]).encode()).hexdigest()[:12]
        mapping[blind_id] = {"case_id": case["case_id"], "case_sha256": hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        rows.append({"blind_id": blind_id, "dataset_sha256": ds_sha, "question": case["user_query"],
                     "policy_version": json.dumps(case["policy_version"], ensure_ascii=False), "evaluation_at": case["evaluation_at"],
                     "independent_required_conditions": "", "independent_evidence_ids": "", "independent_conclusion": "",
                     "conclusion_review": "", "review_decision": "", "reviewer": "", "reviewed_at": "", "review_notes": ""})
    if packet_path.exists() or policies_path.exists() or mapping_path.exists():
        raise FileExistsError("refusing to overwrite an existing blind-review artifact")
    fields = list(rows[0]) if rows else []
    with packet_path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    policy_data = json.loads(POLICIES.read_text(encoding="utf-8"))
    policy_pack = {"dataset_sha256": ds_sha, "policies": policy_data["documents"]}
    policies_path.write_text(json.dumps(policy_pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mapping_path.write_text(json.dumps({"dataset_sha256": ds_sha, "blind_id_map": mapping}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return packet_path, policies_path, mapping_path


if __name__ == "__main__":
    print("\n".join(str(p) for p in create_packet()))
