import json
from collections import defaultdict

from app.m0.data import ROOT


def test_route_candidate_is_review_pending_diverse_and_group_isolated():
    path = ROOT / "eval" / "dataset" / "route-m2.1-candidate-r1.json"
    dataset = json.loads(path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    assert dataset["status"] == "CANDIDATE_NOT_FROZEN"
    assert len(cases) == dataset["case_count"] == 132
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert {case["expected_strategy"] for case in cases} == {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}
    assert all(case["review_status"] == "PENDING_REVIEW" for case in cases)
    split_by_group = defaultdict(set)
    for case in cases:
        split_by_group[case["group_id"]].add(case["dataset_split"])
        assert case["expected_strategy"] in {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}
        if case["expected_strategy"] == "SINGLE":
            assert case["expected_sub_strategy"] in (None, "BM25")
    assert all(len(splits) == 1 for splits in split_by_group.values())
    assert sum(case["dataset_split"] == "test_candidate" for case in cases) == 60
