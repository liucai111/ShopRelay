import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_r_probe", ROOT / "scripts" / "probe_m2_r_real.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_m2_r_manifest_is_fixed_human_approved_dev_validation_only():
    cases, digest = probe._load_manifest()
    assert set(cases) == probe.FIXED_CASE_IDS
    assert digest == "c2da6cf144cdd5c760cf92fa126042438907c48e7d2c61f87fba42eb930913ea"
    assert all(row["dataset_split"] in {"dev", "validation"} for row in cases.values())
    assert all(row["review_status"] == "APPROVED" for row in cases.values())
    assert sum(row.get("scoreability") == "SCORABLE_CANDIDATE" for row in cases.values()) == 6
    assert sum(row.get("scoreability") != "SCORABLE_CANDIDATE" for row in cases.values()) == 1


def test_m2_r_manifest_builder_does_not_include_test_candidate_rows():
    from scripts.build_m2_r_manifest import build

    manifest = build()
    assert manifest["test_candidate_rows_included"] is False
    assert len(manifest["cases"]) == 7
    assert {row["dataset_split"] for row in manifest["cases"]} <= {"dev", "validation"}


def test_m2_r_failed_sse_can_have_error_as_terminal_event():
    events = probe._parse_sse(
        'event: started\ndata: {"run_id":"r1"}\n\n'
        'event: route_decided\ndata: {"run_id":"r1"}\n\n'
        'event: error\ndata: {"run_id":"r1"}\n\n'
    )
    types = [row["type"] for row in events]
    assert types == ["started", "route_decided", "error"]
    assert types[-1] == "error"
    assert probe._sse_order_valid(types)


def test_m2_r_success_sse_requires_answer_before_done():
    assert probe._sse_order_valid(["started", "route_decided", "agent_started", "retrieval_hop", "answer", "done"])
    assert not probe._sse_order_valid(["started", "route_decided", "done", "answer"])


def test_m2_r_verification_requires_exclusion_of_test_candidate_rows():
    required = {"database_ok": True, "sse_ok": True}
    assert probe._verification_passes(required | {"test_candidate_rows_included": False})
    assert not probe._verification_passes(required | {"test_candidate_rows_included": True})
    assert not probe._verification_passes(required | {"sse_ok": False,
                                                      "test_candidate_rows_included": False})


def test_reconciliation_produces_new_summary_and_never_overwrites_probe(tmp_path):
    from scripts.reconcile_m2_r_probe import reconcile
    source = ROOT / "docs/probes/m2-r-real-20261021T004953976793Z.json"
    output = tmp_path / "reconciled.json"
    result = reconcile(source, output)
    assert result["engineering_status"] == "PASS"
    assert result["quality_status"] == "BLOCKED"
    # The source probe recorded the original 21-request M2-R stage; the later
    # targeted hard-negative rerun adds two unique requests to the derived
    # cumulative ledger. The immutable stage record itself remains unchanged.
    assert result["model_ledger"]["unique_request_count"] == 23
    assert result["model_ledger"]["requests_with_usage"] == 23
    assert result["test_candidate_rows_included"] is False
    original = source.read_text(encoding="utf-8")
    assert '"status": "FAIL"' in original
    try:
        reconcile(source, output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("reconciliation must not overwrite a prior report")


def test_m2_r_cost_uses_cached_input_and_peak_output_rates():
    cost = probe._request_cost({
        "prompt_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 500_000},
    })
    assert cost == 1.353


def test_m2_r_explicit_rerun_rejects_validation_cases_before_execution():
    try:
        probe._execute_real(ROOT / "docs/probes/must-not-be-created.json",
                            rerun_dev_case="pr2-return_old_version-01")
    except ValueError as exc:
        assert "fixed approved dev regression" in str(exc)
    else:
        raise AssertionError("validation case must not be rerun through the dev-only option")
