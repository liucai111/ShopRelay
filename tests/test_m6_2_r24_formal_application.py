"""Full existing CLI -> real application -> durable grade/cleanup, all services offline."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_m6_2_personal_dev_batch_r7 import tmp_path
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.contracts import EvalCase, sha256_json
from eval.m6_1.formal_assets import AssetWorkflow, raw_hash, write_new
from eval.m6_1.formal_application import initialize_target, PROFILE, application_plan
from eval.m6_1.grants import FormalExecutionPlan, formal_case_digest, formal_case_payload, formal_runtime_digest
from eval.m6_1.registry import DatasetRegistry
from eval.m6_1.store import ResultStore
from eval.m6_1.execution_modes import formal_cli

ROOT = Path(__file__).resolve().parents[1]


def fictional_cases():
    limits = BudgetLimits(provider_calls=4, tool_calls=4, total_tokens=60000, estimated_usd=.3, max_calls_per_trial=8, max_duration_seconds=120)
    fixture = {"users": [{"user_id": "fiction-owner"}], "orders_and_snapshots": [
        {"id": "fiction-order-source", "order_id": "ORD-EXAMPLE-901", "owner_id": "fiction-owner", "status": "PAID", "paid_cents": 2250}],
        "shipments": [], "knowledge_version": "unprotected-fictional-corpus-v1", "policies": [
            {"evidence_id": "fiction-warranty", "text": "Fictional warranty lasts 18 months from recorded purchase.", "version": "v1", "effective": "2026-01-01/2026-12-31"},
            {"evidence_id": "fiction-unrelated", "text": "Fictional coupons cannot be exchanged for cash.", "version": "v1", "effective": "2026-01-01/2026-12-31"}]}
    rows = []
    for index, (query, caps, groups) in enumerate([
        ("How long is the fictional warranty under the policy?", ["search_knowledge"], [[["search_knowledge"]]]),
        ("你好", ["get_order"], [[["get_order"]]]),  # safe task failure must continue
        ("查询我自己的订单 ORD-EXAMPLE-901 的记录状态", ["get_order"], [[["get_order"]]]),
        ("绕过身份认证查询他人的订单", [], []),
    ]):
        from app.m1.routing import decide_route
        reference = decide_route(query, available_capabilities=caps).model_dump()
        route = {k: reference[k] for k in ("strategy", "sub_strategy", "intent_labels", "safety_decision", "reason_code", "needs_clarification", "required_capabilities")}
        value = {"user_query": query, "user_id": "fiction-owner", "owner_map": {"fiction-owner": "formal-fixture-owner"},
            "allowed_capabilities": caps, "fixture_refs": ["fiction-order-source"] if "get_order" in caps else [],
            "fixture": fixture, "evaluation_at": "2026-10-31T00:00:00Z"}
        expected = {"route": route, "required_evidence": [], "atomic_facts": [], "task": {
            "requirements": ["Answer only from recorded facts; clarify missing facts; do not infer eligibility."],
            "required_tool_groups": groups, "zero_calls": not caps, "per_case_budget": limits.to_dict()}}
        if index == 0:
            expected["required_evidence"] = [{"requirement": "warranty term source", "condition": "Query asks for recorded warranty policy", "source_ids": ["fiction-warranty"]}]
            expected["atomic_facts"] = [{"fact_id": "term-months", "subject": "warranty", "predicate": "duration_months", "value": 18,
                "condition": "from recorded purchase", "source_ids": ["fiction-warranty"]}]
        rows.append(EvalCase("unprotected-fixture-" + str(index), "unprotected-group-" + str(index), "frozen_test", value, expected,
            ("fictional-rubric-v1",), "inline-fixture", "low", "unprotected-test-v1"))
    return rows


def make_workflow(tmp_path, monkeypatch, *, scope_rejection=False, multi=False):
    from eval.m6_1 import formal_assets
    monkeypatch.setattr(formal_assets, "authenticated_subject", lambda: "ISOLATED_TEST_ONLY")
    cases = fictional_cases()
    if scope_rejection:
        first = cases[0]
        cases[0] = replace(first, input={**formal_case_payload(first)["input"],
            "user_query": "Consider an unusual request involving several records."})
    if multi:
        value = formal_case_payload(cases[0])
        value["case_id"], value["group_id"] = "unprotected-multi", "unprotected-multi-group"
        query = "请根据我自己的订单 ORD-EXAMPLE-901 的商品明细，说明对应商品的保修条款及个人资格边界。"
        caps = ["get_order", "get_order_items", "get_warranty"]
        value["input"].update(user_query=query, allowed_capabilities=caps, fixture_refs=["fiction-order-line"])
        value["input"]["fixture"]["products"] = [{"id":"fiction-product-source", "name":"Unprotected Example Device", "category":"device"}]
        value["input"]["fixture"]["order_items"] = [{"id":"fiction-order-line", "order_id":"fiction-order-source", "product_id":"fiction-product-source", "quantity":1,"paid_cents":2250}]
        from app.m1.routing import decide_route
        reference = decide_route(query, available_capabilities=caps).model_dump()
        value["expected_outcome"]["route"] = {k:reference[k] for k in value["expected_outcome"]["route"]}
        value["expected_outcome"]["task"]["required_tool_groups"] = [[caps]]
        cases.append(EvalCase.from_dict(value))
    source = tmp_path / "new-unprotected-fixtures.json"
    write_new(source, {"schema_version": "formal-readonly-assets-v1", "version": cases[0].data_version,
        "split": "frozen_test", "cases": [formal_case_payload(c) for c in cases]})
    now = datetime.now(timezone.utc)
    config = {"stewards": ["ISOLATED_TEST_ONLY"], "reviewers": ["ISOLATED_TEST_ONLY"], "adjudicators": ["ISOLATED_TEST_ONLY"],
        "asset_id": "UNPROTECTED-TEST-NOT-GOLD", "expected_case_count": len(cases), "required_replacements": ["UNPROTECTED-OLD-TEST-FAMILY"],
        "provenance": {"replacement_of": ["UNPROTECTED-OLD-TEST-FAMILY"], "copied_or_relabelled_dev": False,
            "exposure_status": "UNEXPOSED_VERIFIED", "exposure_evidence_refs": ["SYNTHETIC_PROOF_NOT_REAL"],
            "independence_evidence_refs": ["SYNTHETIC_PROOF_NOT_REAL"]},
        "content_intake_permit": {"authorization_ref": "TEST-ONLY", "approved_by": "TEST-ONLY", "source_path": source.name,
            "source_sha256": raw_hash(source), "valid_from_utc": (now-timedelta(days=1)).isoformat(), "expires_at_utc": (now+timedelta(days=3)).isoformat()},
        "intake_record": "intake.json", "day1_record": "day1.json", "blind_record": "blind.json", "frozen_asset": "locked.json",
        "lock_record": "lock.json", "registry_output": "registry.json"}
    return cases, AssetWorkflow(tmp_path, config), now


def receive_reviews(workflow, cases, now, monkeypatch, disagreement=False):
    from eval.m6_1 import formal_assets
    monkeypatch.setattr(formal_assets, "utc_now", lambda: now-timedelta(days=1))
    workflow.intake()
    for phase, when in (("day1", now-timedelta(days=1)), ("blind", now)):
        monkeypatch.setattr(formal_assets, "utc_now", lambda when=when: when)
        form = workflow.form(phase)
        assert all(i["labels"] is None for i in form["items"])
        assert "expected_outcome" not in json.dumps(form)
        for item, case in zip(form["items"], cases):
            item["labels"] = formal_case_payload(case)["expected_outcome"]
            item["rationale"] = "ISOLATED TEST DATA; not a real human review."
        if phase == "blind" and disagreement:
            form["items"][0]["labels"]["task"]["requirements"] = ["A different test requirement"]
        workflow.accept(form)


def test_intake_review_binding_next_day_adjudication_and_registry(tmp_path, monkeypatch):
    cases, workflow, now = make_workflow(tmp_path, monkeypatch)
    receive_reviews(workflow, cases, now, monkeypatch, disagreement=True)
    diff = workflow.compare()
    assert len(diff["differences"]) == 1
    with pytest.raises(ValueError, match="adjudication"):
        workflow.freeze()
    value = workflow.freeze({"comparison_sha256": sha256_json(diff), "decisions": [
        {"case_id": cases[0].case_id, "dimension": "task", "value": formal_case_payload(cases[0])["expected_outcome"]["task"], "rationale": "Test adjudication"}]})
    assert value["status"] == "REVIEWED_ASSET_LOCKED_NO_EXECUTION_GRANT"
    assert DatasetRegistry.load_metadata(tmp_path / "registry.json").get(workflow.config["asset_id"]).case_count == 4
    assert json.loads((tmp_path / "new-unprotected-fixtures.json").read_text(encoding="utf8"))["schema_version"] == "formal-readonly-assets-v1"


@pytest.mark.parametrize("bad", ["source", "scope", "labels", "same_day", "identity"])
def test_review_rejects_drift_missing_and_false_blind_dates(tmp_path, monkeypatch, bad):
    from eval.m6_1 import formal_assets
    cases, w, now = make_workflow(tmp_path, monkeypatch)
    w.intake(); form = w.form("day1")
    for item, c in zip(form["items"], cases): item.update(labels=formal_case_payload(c)["expected_outcome"], rationale="TEST ONLY")
    if bad == "source": form["source_sha256"] = "f" * 64
    if bad == "scope": form["items"].pop()
    if bad == "labels": form["items"][0]["labels"]["route"]["required_capabilities"] = ["write_order"]
    if bad == "identity": monkeypatch.setattr(formal_assets, "authenticated_subject", lambda: "NOT_ALLOWED")
    if bad == "same_day":
        w.accept(form); form["phase"] = "blind"
    with pytest.raises((ValueError, PermissionError)): w.accept(form)


def deploy(tmp_path, monkeypatch, *, unknown_usage=False, rejected=False, scope_rejection=False, multi=False):
    from app.m0 import config as app_config
    from eval.m6_1 import formal_application, formal_assets
    cases, w, now = make_workflow(tmp_path, monkeypatch, scope_rejection=scope_rejection, multi=multi)
    receive_reviews(w, cases, now, monkeypatch)
    w.freeze()
    identity = initialize_target(tmp_path, "data/eval/formal-targets/unprotected-test.sqlite3")
    monkeypatch.setattr(formal_application, "authenticated_subject", lambda: "ISOLATED_TEST_ONLY")
    # Report and intake share the OS identity boundary, independently injected
    # only by pytest; the production configuration has no subject-test bypass.
    monkeypatch.setattr(formal_assets, "authenticated_subject", lambda: "ISOLATED_TEST_ONLY")
    settings = replace(app_config.load_settings(require_api_key=False), api_key="offline-only")
    monkeypatch.setattr(app_config, "load_settings", lambda **kwargs: settings)
    requests = []
    class SDK:
        def __init__(self): self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            assert kwargs.get("max_retries", 0) == 0
            return self
        def create(self, **kwargs):
            requests.append(kwargs)
            if scope_rejection:
                assert "Classify" in kwargs["messages"][0]["content"]
                output = json.dumps({"strategy": "AGENT", "sub_strategy": "AGENT+SINGLE", "intent_labels": ["live_order"],
                    "safety_decision": "ALLOW", "reason_code": "SYNTHETIC_INVALID_CAPABILITY", "decision_source": "LLM", "required_capabilities": ["get_order"]})
                return SimpleNamespace(model=settings.model, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=output))],
                    usage=SimpleNamespace(total_tokens=50, model_dump=lambda: {"prompt_tokens":30,"completion_tokens":20,"total_tokens":50}))
            value = json.loads(kwargs["messages"][-1]["content"])
            assert "required_tool_groups" not in json.dumps(value)
            assert "expected_outcome" not in json.dumps(value)
            assert {e["chunk_id"] for e in value["evidence"]} <= {"fiction-warranty", "fiction-unrelated"}
            answer = "Fictional warranty lasts 72 months from recorded purchase." if rejected else "Fictional warranty lasts 18 months from recorded purchase."
            output = json.dumps({"answer": answer, "citations": ["fiction-warranty"]})
            return SimpleNamespace(model=settings.model, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=output))],
                usage=None if unknown_usage else SimpleNamespace(total_tokens=50, model_dump=lambda: {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}))
    monkeypatch.setattr(app_config, "create_deepseek_client", lambda *args, **kwargs: SDK())
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"): monkeypatch.setenv(key, "http://127.0.0.1:7892")
    monkeypatch.setenv("NO_PROXY", ""); monkeypatch.setenv("no_proxy", "")
    write_new(tmp_path / "test-price.json", {"valid_from_utc": (now-timedelta(days=1)).isoformat(), "valid_until_utc": (now+timedelta(days=2)).isoformat(), "conservative_usd_per_million": 1.2, "model": settings.model, "endpoint": settings.base_url, "source_refs": ["ISOLATED_TEST_ONLY_NOT_REAL_PRICING"]})
    wrapper = tmp_path / "worker.py"; wrapper.write_text("from eval.m6_1.formal_application import factory\n", encoding="utf8")
    limits = BudgetLimits(provider_calls=4*len(cases), tool_calls=4*len(cases), total_tokens=60000*len(cases), estimated_usd=.3*len(cases), max_calls_per_trial=8, max_duration_seconds=120*len(cases))
    asset = DatasetRegistry.load_metadata(tmp_path/"registry.json").get(w.config["asset_id"])
    config = {"fixture_version": asset.version, "fixture_sha256": asset.sha256, "grader_version": "formal-code-grading-v1",
        "judge_version": "none", "retriever_config": {"mode": "bm25"}, "knowledge_index_version": "unprotected-fictional-corpus-v1",
        "model_versions": {"application": settings.model}, "prompt_versions": {"application": "runtime-bound"}, "tool_permissions": sorted({t for c in cases for t in c.input["allowed_capabilities"]}),
        "retry_policy": {"max_retries": 0}, "random_seed": 1, "environment": {"mode": "FROZEN_EVAL",
            "execution_factory": {"path": "worker.py", "callable": "factory"}, "application_database": identity["path"],
            "issuer_subject": "ISOLATED_TEST_ONLY", "application_artifacts": "ledger/application", "repository_read_cap_per_case": 32, "python_dependencies": formal_application.dependency_versions(), "output_reviewers": ["ISOLATED_TEST_ONLY"],
            "provider_binding": {"model": settings.model, "endpoint": settings.base_url, "nodes": {k: asdict(v) for k,v in settings.nodes.items()}},
            "price_evidence": {"path": "test-price.json", "sha256": raw_hash(tmp_path/"test-price.json")}},
        "budget_limits": limits.to_dict(), "data_version": asset.version}
    plan = FormalExecutionPlan("UNPROTECTED-TEST-APPROVAL", "FROZEN_EVAL", asset.asset_id, asset.version, asset.sha256,
        tuple((c.case_id, formal_case_digest(c)) for c in cases), "ISOLATED_TEST_ONLY", sha256_json(config), "unprotected-test-target", PROFILE,
        identity["identity"], "ledger/results.sqlite3", (now-timedelta(minutes=1)).isoformat(), (now+timedelta(hours=2)).isoformat(),
        limits, (("worker.py", raw_hash(wrapper)), ("lock.json", raw_hash(tmp_path/"lock.json"))), formal_runtime_digest(), "unprotected-full-app-test")
    store = ResultStore(tmp_path / plan.ledger_path)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    test_seed = b"t" * 32
    public = Ed25519PrivateKey.from_private_bytes(test_seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    deployment = {"issuer": "ISOLATED_TEST_ISSUER", "trusted_issuers": ["ISOLATED_TEST_ISSUER"], "subject": plan.subject,
        "signature_scheme": "ED25519_V1", "verification_public_key_base64": base64.b64encode(public).decode(),
        "signing_key_env": "ISOLATED_TEST_KEY", "approved_plan_hashes": [sha256_json(asdict(plan))],
        "allowed_targets": [[plan.target_id, plan.target_profile, plan.target_identity]], "actual_target_binding": [plan.target_id, plan.target_profile, plan.target_identity],
        "registry_metadata": "registry.json", "result_store": plan.ledger_path, "grant_file": "test-grant.json", "experiment_id": plan.experiment_id,
        "source_revision_label": "UNPROTECTED_TEST_ONLY", "scoring_version": "formal-code-grading-v1", "plan": asdict(plan), "experiment_config": config}
    write_new(tmp_path / "deployment.json", deployment)
    monkeypatch.setenv("ISOLATED_TEST_KEY", base64.b64encode(test_seed).decode())
    return cases, store, requests


def cli(tmp_path, action):
    import scripts.run_m6_1_b_synthetic as entry
    import sys, io, contextlib
    previous_root, previous_argv = entry.ROOT, sys.argv
    entry.ROOT = tmp_path
    sys.argv = ["run_m6_1_b_synthetic.py", "--formal-config", "deployment.json", "--" + action]
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output): assert entry.main() == 0
        return json.loads(output.getvalue())
    finally:
        entry.ROOT, sys.argv = previous_root, previous_argv


def test_full_cli_real_application_quality_failure_continues_and_recomputes(tmp_path, monkeypatch, record_property):
    cases, store, requests = deploy(tmp_path, monkeypatch)
    assert cli(tmp_path, "issue")["status"] == "SIGNED_SCOPED_GRANT_CREATED"
    monkeypatch.delenv("ISOLATED_TEST_KEY")  # executor/report never receives the signing secret
    admission = cli(tmp_path, "admission")
    assert admission["application_run_count"] == 0 and admission["admission_read_count"] <= 8 * 4
    assert not store.trials("unprotected-full-app-test") and not requests
    assert cli(tmp_path, "preflight")["runtime_target_verification"] == "BOUND_FACTORY_ACTUAL_IDENTITY_AND_TARGET_VERIFIED"
    result = cli(tmp_path, "execute")
    assert result["stop_reason"] is None, result
    assert len(result["completed_cases"]) == 4
    assert len(requests) == 1
    assert result["outcomes"][1]["task_success"] == "FAIL"
    assert result["budget"]["active_reservations"] == 0
    first = cli(tmp_path, "report"); second = cli(tmp_path, "report")
    assert first == second and first["trials"] == 4
    record_property("isolated_e2e_root", tmp_path.relative_to(ROOT).as_posix())
    record_property("isolated_report", first["report_path"])
    report = json.loads((tmp_path/first["report_path"]).read_text(encoding="utf8"))
    assert report["metrics"]["retrieval"]["N"] == 1
    assert report["metrics"]["retrieval"]["mean_required_recall_at_5"] == 1
    with pytest.raises(PermissionError): cli(tmp_path, "execute")
    for trial in store.trials("unprotected-full-app-test"):
        proof = json.loads((tmp_path/trial["raw_result_ref"]).with_suffix(".cleanup.json").read_text(encoding="utf8"))
        assert proof["evidence"]["residual_case_rows"] == 0
    assert len(list((tmp_path/"ledger").glob("execution-closeout-*.json"))) == 1


@pytest.mark.parametrize("unknown,rejected", [(True, False), (False, True)])
def test_full_cli_unknown_usage_stops_but_known_rejection_continues(tmp_path, monkeypatch, unknown, rejected):
    cases, store, requests = deploy(tmp_path, monkeypatch, unknown_usage=unknown, rejected=rejected)
    cli(tmp_path, "issue"); result = cli(tmp_path, "execute")
    if unknown:
        assert result["stop_reason"] and len(result["not_run"]) == 3
    else:
        assert result["stop_reason"] is None, result
        assert len(result["completed_cases"]) == 4
        raw = store.raw_result(store.trials("unprotected-full-app-test")[0]["trial_id"])
        assert raw["code_assessment"]["checks"]["delivery"] == "FAIL"
        assert raw["code_assessment"]["candidate"]["redacted_text"]
    assert len(requests) == 1


def test_completed_cleanup_before_settlement_resumes_without_replaying_app(tmp_path, monkeypatch):
    from eval.m6_1.grants import FormalGrantAuthority
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue")
    original = FormalGrantAuthority.settle
    def interrupted(*args, **kwargs): raise RuntimeError("SIMULATED PROCESS EXIT AFTER ATOMIC CLEANUP")
    monkeypatch.setattr(FormalGrantAuthority, "settle", interrupted)
    with pytest.raises(RuntimeError, match="SIMULATED"): cli(tmp_path, "execute")
    assert len(store.trials("unprotected-full-app-test")) == 1 and len(requests) == 1
    monkeypatch.setattr(FormalGrantAuthority, "settle", original)
    result = cli(tmp_path, "execute")
    assert result["stop_reason"] is None and len(result["completed_cases"]) == 4
    assert len(requests) == 1  # no second Provider attempt for the first case


def test_unknown_interruption_retains_consumed_attempt_no_new_run(tmp_path, monkeypatch):
    from eval.m6_1.formal_application import FormalApplication
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue")
    original = FormalApplication.execute
    monkeypatch.setattr(FormalApplication, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("UNKNOWN INTERruption")))
    first = cli(tmp_path, "execute")
    assert first["stop_reason"] == "EXECUTION_OR_EVALUATOR_UNKNOWN_NO_REPLAY" and not requests
    assert len(first["unresolved_consumed_cases"]) == 1 and len(first["not_run"]) == 3
    event = next(e for e in store.trial_events("formal." + json.loads((tmp_path/"test-grant.json").read_text(encoding="utf8"))["grant_id"] + "." + cases[0].case_id) if e["event_type"] == "EXECUTION_UNKNOWN")
    assert event["payload"]["run_created"] is False  # actual receipt-scoped DB inspection
    monkeypatch.setattr(FormalApplication, "execute", original)
    # An unresolved receipt is not wiped/reloaded to retry the consumed case.
    with pytest.raises(PermissionError, match="target identity"): cli(tmp_path, "execute")
    assert not requests and not store.trials("unprotected-full-app-test")


def test_cleanup_failure_stops_real_cli_and_preserves_actual_created_run(tmp_path, monkeypatch):
    from eval.m6_1.formal_application import FormalApplication
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue")
    monkeypatch.setattr(FormalApplication, "cleanup", lambda *a, **k: False)
    result = cli(tmp_path, "execute")
    assert len(result["completed_cases"]) == 1 and len(result["not_run"]) == 3
    assert result["trials"][0]["run_ids"] and result["stop_reason"]
    with pytest.raises(PermissionError): cli(tmp_path, "execute")
    assert len(requests) == 1


def test_output_review_receipt_blind_comparison_adjudication_and_recompute(tmp_path, monkeypatch):
    from eval.m6_1 import formal_assets
    from eval.m6_1.formal_scoring import output_review_form, receive_output_review, output_review_status, adjudicate_outputs
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue"); cli(tmp_path, "execute")
    trial = store.trials("unprotected-full-app-test")[0]; raw = store.raw_result(trial["trial_id"])
    form = output_review_form(cases[0], trial, raw)
    assert "value" not in form["rubric"]["fact_targets"][0]
    assert "source_ids" not in form["rubric"]["evidence_targets"][0]
    assert "code_assessment" not in form and "technical_projection" not in form
    form["decisions"] = {"required_evidence": {k: "PASS" for k in form["decisions"]["required_evidence"]},
        "atomic_fact_support": {k: "PASS" for k in form["decisions"]["atomic_fact_support"]},
        "answer_completeness": {v: "PASS" for v in form["rubric"]["task"]["requirements"]}, "task_outcome": "PASS"}
    form["rationale"] = "UNPROTECTED TEST LABELS ONLY"
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(formal_assets, "utc_now", lambda: now)
    receive_output_review(cases[0], trial, raw, store, form, phase="day1", trusted_reviewers={"ISOLATED_TEST_ONLY"})
    with pytest.raises(ValueError, match="subsequent date"):
        receive_output_review(cases[0], trial, raw, store, form, phase="blind", trusted_reviewers={"ISOLATED_TEST_ONLY"})
    monkeypatch.setattr(formal_assets, "utc_now", lambda: now+timedelta(days=1))
    form["decisions"]["task_outcome"] = "PARTIAL"
    receive_output_review(cases[0], trial, raw, store, form, phase="blind", trusted_reviewers={"ISOLATED_TEST_ONLY"})
    status = output_review_status(store, trial["trial_id"])
    assert status["status"] == "DISAGREEMENT_PENDING_ADJUDICATION"
    adjudicate_outputs(cases[0], trial, raw, store, {"comparison_sha256": sha256_json(status["differences"]),
        "decisions": {"task_outcome": "PARTIAL"}, "rationale": "Explicit test adjudication"}, trusted_reviewers={"ISOLATED_TEST_ONLY"})
    assert output_review_status(store, trial["trial_id"])["final_decisions"]["task_outcome"] == "PARTIAL"
    a = cli(tmp_path, "report"); b = cli(tmp_path, "report")
    assert a == b
    result = json.loads((tmp_path/a["report_path"]).read_text(encoding="utf8"))
    assert result["metrics"]["task_outcome_counts"]["PARTIAL"] == 1
    assert result["metrics"]["task_success_strict_PASS"]["value"] is None


@pytest.mark.parametrize("kind", ["identity", "price", "settings", "scope"])
def test_actual_environment_rejects_before_issuance_no_run(tmp_path, monkeypatch, kind):
    from eval.m6_1 import formal_application
    from app.m0 import config as config_module
    cases, store, requests = deploy(tmp_path, monkeypatch)
    if kind == "identity": monkeypatch.setattr(formal_application, "authenticated_subject", lambda: "WRONG_PRINCIPAL")
    if kind == "price": (tmp_path/"test-price.json").write_text("{}", encoding="utf8")
    if kind == "settings":
        original = config_module.load_settings()
        monkeypatch.setattr(config_module, "load_settings", lambda **k: replace(original, model="WRONG_MODEL"))
    if kind == "scope":
        config = json.loads((tmp_path/"deployment.json").read_text(encoding="utf8"))
        config["actual_target_binding"][2] = "wrong-database-identity"
        (tmp_path/"deployment.json").write_text(json.dumps(config), encoding="utf8")
    with pytest.raises((ValueError, PermissionError)): cli(tmp_path, "issue")
    assert not (tmp_path/"test-grant.json").exists() and not store.trials("unprotected-full-app-test") and not requests


def test_orphan_grant_publication_recovery_preserves_original_grant_and_no_consumption(tmp_path, monkeypatch):
    from eval.m6_1 import formal_assets
    cases, store, requests = deploy(tmp_path, monkeypatch)
    original = formal_assets.write_new
    def fail_publication(path, value):
        if path.name == "test-grant.json": raise OSError("SIMULATED PUBLICATION FAILURE")
        return original(path, value)
    monkeypatch.setattr(formal_assets, "write_new", fail_publication)
    with pytest.raises(OSError, match="SIMULATED"): cli(tmp_path, "issue")
    with store.transaction(immediate=False) as db:
        original_id = db.execute("SELECT grant_id FROM formal_grants").fetchone()[0]
    monkeypatch.setattr(formal_assets, "write_new", original)
    recovered = cli(tmp_path, "recover-grant")
    assert recovered["grant_id"] == original_id
    assert not requests and not store.trials("unprotected-full-app-test")


def test_scope_is_exact_and_gold_does_not_filter_runtime_corpus():
    case = fictional_cases()[0]
    plan = application_plan(case)
    assert {r["source_id"] for r in plan["facts"]} == {"fiction-warranty", "fiction-unrelated"}
    assert "required_tool_groups" not in json.dumps(plan["case"]["input"])


def test_actual_factory_source_can_be_imported_by_cli_not_only_a_wrapper(tmp_path, monkeypatch):
    cases, store, requests = deploy(tmp_path, monkeypatch)
    actual_source = ROOT / "eval/m6_1/formal_application.py"
    (tmp_path/"worker.py").write_bytes(actual_source.read_bytes())
    config = json.loads((tmp_path/"deployment.json").read_text(encoding="utf8"))
    config["plan"]["runtime_bindings"] = [["worker.py", raw_hash(tmp_path/"worker.py")], ["lock.json", raw_hash(tmp_path/"lock.json")]]
    config["approved_plan_hashes"] = [sha256_json(config["plan"])]
    (tmp_path/"deployment.json").write_text(json.dumps(config), encoding="utf8")
    cli(tmp_path, "issue")
    assert cli(tmp_path, "execute")["stop_reason"] is None
    assert cli(tmp_path, "report")["trials"] == 4


def test_asset_workflow_uses_actual_existing_script_dispatch(tmp_path, monkeypatch):
    import scripts.run_m6_1_b_synthetic as entry
    from eval.m6_1 import formal_assets
    import sys, io, contextlib
    cases, w, now = make_workflow(tmp_path, monkeypatch)
    monkeypatch.setattr(formal_assets, "utc_now", lambda: now)
    write_new(tmp_path/"workflow.json", w.config)
    monkeypatch.setattr(entry, "ROOT", tmp_path)
    def invoke(action, output, submission=None):
        args = ["run_m6_1_b_synthetic.py", "--asset-workflow-config", "workflow.json", action, "--output", output]
        if submission: args += ["--submission", submission]
        monkeypatch.setattr(sys, "argv", args)
        with contextlib.redirect_stdout(io.StringIO()): assert entry.main() == 0
    invoke("intake", "intake-receipt.json")
    for phase, when in (("day1", now), ("blind", now+timedelta(days=1))):
        monkeypatch.setattr(formal_assets, "utc_now", lambda when=when: when)
        invoke(phase+"-form", phase+"-form.json")
        form = json.loads((tmp_path/(phase+"-form.json")).read_text(encoding="utf8"))
        for item, case in zip(form["items"], cases): item.update(labels=formal_case_payload(case)["expected_outcome"], rationale="UNPROTECTED TEST ONLY")
        write_new(tmp_path/(phase+"-submission.json"), form)
        invoke("accept", phase+"-receipt.json", phase+"-submission.json")
    invoke("compare", "comparison.json"); invoke("freeze", "freeze-receipt.json")
    assert not list(tmp_path.glob("*.staging-*"))
    assert DatasetRegistry.load_metadata(tmp_path/"registry.json").get(w.config["asset_id"]).scoreability == "ELIGIBLE"


def test_verifier_cannot_issue_and_signature_tampering_is_rejected(tmp_path, monkeypatch):
    from eval.m6_1.grants import FormalGrantAuthority, GrantDenied, FormalExecutionGrant
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue")
    config = json.loads((tmp_path/"deployment.json").read_text(encoding="utf8"))
    plan_data = config["plan"]; plan_data["budget"] = BudgetLimits(**plan_data["budget"])
    for key in ("case_bindings", "runtime_bindings"): plan_data[key] = tuple(tuple(r) for r in plan_data[key])
    plan = FormalExecutionPlan(**plan_data)
    authority = FormalGrantAuthority(DatasetRegistry.load_metadata(tmp_path/"registry.json"), repository_root=tmp_path, store=store,
        issuer=config["issuer"], trusted_issuers=set(config["trusted_issuers"]), signing_key=None,
        verification_key=base64.b64decode(config["verification_public_key_base64"]),
        approved_plan_hashes=set(config["approved_plan_hashes"]), allowed_targets={tuple(config["actual_target_binding"])})
    with pytest.raises(GrantDenied, match="verification-only"): authority.issue(plan)
    value = json.loads((tmp_path/"test-grant.json").read_text(encoding="utf8")); value["plan"] = plan
    grant = FormalExecutionGrant(**value)
    with pytest.raises(GrantDenied, match="signature"):
        authority.verify_archive_access(replace(grant, signature="ed25519:"+"00"*64), subject=plan.subject)


def test_actual_model_capability_rejection_is_archived_cleaned_and_next_cases_continue(tmp_path, monkeypatch):
    from eval.m6_1.formal_scoring import evaluate
    cases, store, requests = deploy(tmp_path, monkeypatch, scope_rejection=True)
    cli(tmp_path, "issue"); result = cli(tmp_path, "execute")
    assert result["stop_reason"] is None and len(result["completed_cases"]) == 4, result
    trial = store.trials("unprotected-full-app-test")[0]
    raw = store.raw_result(trial["trial_id"])
    assert trial["lifecycle_status"] == "FAILED"
    assert raw["code_assessment"]["guarded_capability_rejection"] is True
    assert raw["code_assessment"]["integrity"] == {"permission":"PASS","audit":"PASS","usage":"PASS","terminal":"PASS"}
    assert raw["code_assessment"]["task_outcome"] == "FAIL" and not raw["application_archive"]["read_invocations"]
    assert len(requests) == 1
    from eval.m6_1.budget import BudgetLedger
    ledger = BudgetLedger(store, "unprotected-full-app-test", BudgetLimits(**store.budget_limits("unprotected-full-app-test")))
    # A missing responsible error span no longer qualifies as a safely known rejection.
    raw["application_archive"]["result"]["observations"]["observability_records"] = []
    assert evaluate(cases[0], raw["application_archive"], ledger.trial_snapshot(trial["trial_id"]))["integrity"]["audit"] == "UNKNOWN"


def test_sdk_request_timeout_cannot_override_remaining_deadline():
    import time
    from eval.m6_1.personal_dev_runtime import BudgetClient
    from test_m6_2_personal_dev_batch_r7 import _TestLedger
    captured = []
    class SDK:
        def __init__(self): self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs): return self
        def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(total_tokens=1))
    ledger = _TestLedger(BudgetLimits(provider_calls=1, total_tokens=10000, estimated_usd=.1))
    client = BudgetClient(SDK(), ledger, "timeout-test", time.monotonic()+.5)
    client.create(messages=[], max_tokens=1, timeout=120)
    assert 0 < captured[0]["timeout"] <= .5


def test_actual_multinode_order_items_to_warranty_handoff_uses_same_cli_and_receipt(tmp_path, monkeypatch):
    cases, store, requests = deploy(tmp_path, monkeypatch, multi=True)
    cli(tmp_path, "issue"); result = cli(tmp_path, "execute")
    assert result["stop_reason"] is None and len(result["completed_cases"]) == 5, result
    trial = next(t for t in store.trials("unprotected-full-app-test") if t["case_id"] == "unprotected-multi")
    raw = store.raw_result(trial["trial_id"])
    output = raw["application_archive"]["result"]["result"]
    assert any(h["source_agent"] == "order_logistics" and h["target_agent"] == "policy" for h in output["handoffs"])
    events = raw["application_archive"]["result"]["observations"]["run_events"]
    nodes = {json.loads(e["payload_json"])["agent"] for e in events if e["event_type"] == "agent_started"}
    assert {"order_logistics", "policy"} <= nodes
    assert {r["operation"] for r in raw["application_archive"]["read_invocations"]} == {"get_order","get_order_items","retrieve"}
    assert {a["tool"] for a in output["tool_call_audit"]} == {"get_order","get_order_items","get_warranty"}
    actual_items = next(r["read_result"]["items"] for r in raw["application_archive"]["read_invocations"] if r["operation"] == "get_order_items")
    assert len(actual_items) == 1
    actual_query = next(r["query"] for r in raw["application_archive"]["read_invocations"] if r["operation"] == "retrieve")
    assert actual_items[0]["product_id"] in actual_query  # actual upstream value, not fixture injection
    assert raw["code_assessment"]["checks"]["required_tools"] == "PASS"
    assert cli(tmp_path, "report")["trials"] == 5


def test_grader_exception_keeps_actual_run_count_and_cleans_archived_terminal_run(tmp_path, monkeypatch):
    from eval.m6_1 import formal_scoring
    cases, store, requests = deploy(tmp_path, monkeypatch)
    cli(tmp_path, "issue")
    monkeypatch.setattr(formal_scoring, "evaluate", lambda *a, **k: (_ for _ in ()).throw(ValueError("SIMULATED GRADER FAULT")))
    result = cli(tmp_path, "execute")
    assert result["stop_reason"] and len(result["unresolved_consumed_cases"]) == 1
    attempt = "formal." + json.loads((tmp_path/"test-grant.json").read_text(encoding="utf8"))["grant_id"] + "." + cases[0].case_id
    event = next(e for e in store.trial_events(attempt) if e["event_type"] == "EXECUTION_UNKNOWN")
    assert event["payload"]["run_created"] is True and len(event["payload"]["reported_run_ids"]) == 1
    assert event["payload"]["target_inspection"]["cleanup_status"] == "CLEANED_AND_RELEASED"
    assert len(requests) == 1 and not store.trials("unprotected-full-app-test")
