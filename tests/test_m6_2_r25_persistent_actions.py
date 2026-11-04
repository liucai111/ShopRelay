"""New fictional PostgreSQL only: actual formal CLI, M4 API and durable saver.

No Provider HTTP is permitted. Virtual labels/clock are test injections; no real
asset is imported or reviewed. The external test-container owner removes it.
"""
import json
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timezone

import pytest
from sqlalchemy import text

from test_m6_2_personal_dev_batch_r7 import tmp_path
from eval.m6_1.formal_assets import raw_hash,write_new,AssetWorkflow
from eval.m6_1.grants import formal_case_payload,formal_case_digest,formal_runtime_digest
from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_dev_database import FormalPostgresDatabase


@pytest.mark.parametrize("cohorts",[{"route":["outside"],"retrieval":[],"outcome":[]},
    {"route":["one","one"],"retrieval":[],"outcome":[]},{"route":["one"],"retrieval":[]}])
def test_reporting_cohort_is_validated_before_preregistration_and_claim(tmp_path,cohorts):
    from types import SimpleNamespace
    from eval.m6_1.experiment import preregister_experiment,REQUIRED_CONFIG_FIELDS
    from eval.m6_1.store import ResultStore
    config={k:None for k in REQUIRED_CONFIG_FIELDS};config["reporting_cohorts"]=cohorts
    with pytest.raises(ValueError,match="reporting cohorts"):
        preregister_experiment(store=ResultStore(tmp_path/"results.sqlite3"),experiment_id="not-claimed",config=config,
            git_commit="not-used",strategy="FROZEN_EVAL",scoring_version="virtual",formal_grant=SimpleNamespace(plan=SimpleNamespace(case_bindings=(("one","bound"),))))


@pytest.mark.parametrize("change",[{"host":"example.invalid"},{"database":"shoprelay"},{"identity_manifest":"../escape.json"}])
def test_formal_postgres_refuses_wrong_target_before_connect(tmp_path,monkeypatch,change):
    binding={"host":"127.0.0.1","port":55432,"database":"shoprelay_m11_test","url_env":"VIRTUAL_PG_URL","identity_manifest":"data/eval/formal-targets/new.json"}
    monkeypatch.setenv("VIRTUAL_PG_URL","postgresql+psycopg://test:test@127.0.0.1:55432/shoprelay_m11_test")
    binding.update(change)
    with pytest.raises(ValueError):FormalPostgresDatabase(tmp_path,binding)


@pytest.mark.parametrize("field,value",[("decision","AUTOMATIC_MODEL_CONSENT"),("tool_id","unknown_write"),("arguments",{"order_id":"ORD-1","amount_cents":1})])
def test_owner_input_does_not_authorize_arbitrary_or_server_derived_fields(field,value):
    from eval.m6_1.formal_actions import validate_interaction
    command={"tool_id":"create_refund_request","target_resource_id":"ORD-1","arguments":{"order_id":"ORD-1","reason_code":"OTHER","reason_summary":"fictional user claim"},"decision":"CONFIRM"}
    command[field]=value
    with pytest.raises(ValueError):validate_interaction(command)


def test_nested_action_and_policy_reservations_share_the_same_per_case_cap(tmp_path):
    from test_m6_1_budget_snapshot_contract import ledger
    from eval.m6_1.budget import BudgetLimits,BudgetStop
    from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger
    _,batch,_=ledger(tmp_path,limits=BudgetLimits(tool_calls=10,max_calls_per_trial=10))
    scoped=PerCaseBudgetLedger(batch,"nested",BudgetLimits(tool_calls=1,max_calls_per_trial=1))
    first=scoped.reserve(trial_id="nested",category="tool")
    with pytest.raises(BudgetStop):scoped.reserve(trial_id="nested",category="tool")
    scoped.settle(first,status="COMMIT",actual_calls=1,actual_tokens=0,estimated_usd=0)
    assert batch.trial_snapshot("nested")["calls_by_category"]["tool"]==1


@pytest.mark.parametrize("count,expected",[(0,"FAIL"),(1,"PASS"),(2,"FAIL")])
def test_action_status_alone_does_not_prove_exactly_one_business_effect(count,expected):
    from eval.m6_1.formal_actions import business_effect_check
    before={"returns":0,"refunds":0,"support_tickets":0,"order_events":0}
    assert business_effect_check("create_refund_request","CONFIRM","SUCCEEDED",before,{**before,"refunds":count})==expected


def test_actual_archived_action_blind_form_hides_code_labels_and_keeps_user_output():
    import xml.etree.ElementTree as ET
    from eval.m6_1.store import ResultStore
    from eval.m6_1.contracts import EvalCase
    from eval.m6_1.formal_scoring import output_review_form
    xml=Path("docs/probes/m62-r25-release-pg.xml")
    if not xml.exists():pytest.skip("requires prior explicitly isolated application proof")
    props={e.attrib["name"]:e.attrib["value"] for e in ET.parse(xml).getroot().iter("property")}
    root=Path(props["r25_proof_root"]);store=ResultStore(root/"ledger/results.sqlite3")
    trial=next(t for t in store.trials("unprotected-full-app-test") if t["case_id"].startswith("virtual-action-create_refund_request-CONFIRM"))
    raw=store.raw_result(trial["trial_id"])
    case=EvalCase.from_dict(json.loads((root/raw["case_path"]).read_text(encoding="utf8")))
    view=output_review_form(case,trial,raw)["scripted_owner_api_output"]
    assert not {"permission","audit","task","state_check","business_effect_check","required_action_state"}.intersection(view)
    assert view["calls"]==raw["application_archive"]["owner_interaction"]["calls"]
    assert view["snapshot"]==raw["application_archive"]["owner_interaction"]["snapshot"]
    from copy import deepcopy
    from eval.m6_1.formal_actions import assess_interaction
    changed=deepcopy(raw["application_archive"]["owner_interaction"])
    changed["snapshot"]["controlled_pending_actions"][0]["status"]="PENDING_CONFIRMATION"
    assert changed["state_check"]=="PASS"  # old producer flag must not win over actual rows
    assert assess_interaction(case,changed)["state_check"]=="FAIL"
    from eval.m6_1.budget import BudgetLedger,BudgetLimits
    from eval.m6_1.formal_scoring import evaluate
    actual=deepcopy(raw["application_archive"]);actual["owner_interaction"]["calls"][-1]["status_code"]=503
    ledger=BudgetLedger(store,"unprotected-full-app-test",BudgetLimits(**store.budget_limits("unprotected-full-app-test")))
    score=evaluate(case,actual,ledger.trial_snapshot(trial["trial_id"]))
    assert "OWNER_ACTION_API_PUBLIC_FAILURE" in score["blocking_items"] and score["task_outcome"]=="FAIL"


def test_actual_formal_cli_postgres_actions_persistent_checkpoint_and_rebuild(tmp_path,monkeypatch,record_property):
    import test_m6_2_r24_formal_application as previous
    connection=Path("data/eval/offline-tests/r25-postgres-isolated/private-test-connection.json")
    injected=os.environ.get("SHOPRELAY_ENGINEERING_PG_CONNECTION")
    if injected:
        private=json.loads(injected)
    else:
        if not connection.exists():pytest.skip("explicit newly provisioned fictional PostgreSQL target required")
        private=json.loads(connection.read_text(encoding="utf8"))
    monkeypatch.setenv("SHOPRELAY_R25_TEST_DATABASE_URL",private["url"])
    base_cases=previous.fictional_cases()
    actions=[]
    for ordinal,(tool,decision) in enumerate([("create_refund_request","CONFIRM"),("create_return_request","CONFIRM"),
            ("cancel_order","CONFIRM"),("escalate_to_human","CONFIRM"),
            ("create_refund_request","LEAVE_PENDING"),("create_refund_request","REJECT"),("create_refund_request","CONFIRM")]):
        original=base_cases[2]
        inp=formal_case_payload(original)["input"]
        inp["owner_map"]={"fiction-owner":"u_demo"}
        order=inp["fixture"]["orders_and_snapshots"][0]
        order.update(paid_at="2026-10-01T10:00:00Z",paid_cents=2250)
        if tool=="create_return_request":order.update(status="DELIVERED",delivered_at=datetime.now(timezone.utc).isoformat())
        if ordinal==6:
            at=datetime.now(timezone.utc).isoformat()
            order.update(status="DELIVERED",delivered_at=at)
            inp["fixture"]["returns"]=[{"id":"approved-fixture-return","order_id":order["id"],"status":"APPROVED","reason":"fictional evidence",
                "created_at":at,"submitted_at":at,"approved_at":at,"source_type":"SIMULATED_TEST_DATA","source_ref":"fictional:return-approval",
                "events":[{"event_id":"approved-fixture-event","order_id":order["id"],"return_id":"approved-fixture-return","refund_id":None,
                    "event_type":"RETURN_APPROVED","occurred_at":at,"recorded_at":at,"actor_type":"SIMULATED_TEST_DATA","source_type":"SIMULATED_TEST_DATA","source_ref":"fictional:approval-event","details_json":"{}"}]}]
        inp["fixture"]["policies"]=[{"evidence_id":policy+"-"+clause,"policy_id":policy,"clause_id":clause,
            "title":"Isolated fictional "+policy,"text":content,"version":"fiction-v1","effective":"2026-01-01/2026-12-31"}
            for policy,clause,content in [("refund","before-shipping","未发货已付款订单允许申请整单退款，退款申请待审核，不代表到账。"),
                ("refund","uniqueness","同订单只允许一个开放退款申请。"),("cancel","eligible","未发货订单可以申请取消，已付款订单关联退款申请。"),
                ("return-standard","window","商品送达后七天内可以申请退货，不影响二次销售，退货申请待审核。")]]
        inp["fixture"]["products"]=[{"id":"action-product","name":"Fictional action fixture","category":"ACCESSORY","price_cents":2250}]
        inp["fixture"]["inventory"]=[{"id":"action-inventory","product_id":"action-product","available_qty":10,"reserved_qty":1,"version":1}]
        inp["fixture"]["order_items"]=[{"id":"action-item","order_id":order["id"],"product_id":"action-product","quantity":1,"paid_cents":2250}]
        inp["fixture"]["order_inventory_reservations"]=[{"id":"action-reservation","order_id":order["id"],"product_id":"action-product", "quantity":1,"status":"RESERVED","source_ref":"isolated:reservation","created_at":"2026-10-01T10:00:00Z"}]
        args={"order_id":order["order_id"],"reason_code":"OTHER","reason_summary":"用户请求受控模拟申请"}
        if tool=="escalate_to_human":args={"order_id":order["order_id"],"issue_category":"ORDER_STATUS","user_description":"请人工核实订单问题","issue_summary":"核实订单记录","reason_code":"OTHER"}
        inp["owner_interaction"]={"tool_id":tool,"target_resource_id":order["order_id"],"arguments":args,"decision":decision}
        expected=formal_case_payload(original)["expected_outcome"]
        expected["task"]["per_case_budget"].update(tool_calls=12,max_calls_per_trial=20)
        suffix="-approved-return" if ordinal==6 else ""
        actions.append(replace(original,case_id="virtual-action-"+tool+"-"+decision+suffix,group_id="virtual-action-group-"+tool+"-"+decision+suffix,input=inp,expected_outcome=expected))
    clarification=formal_case_payload(base_cases[3])
    clarification.update(case_id="virtual-missing-coupon-code",group_id="virtual-missing-coupon-code-group")
    clarification["input"].update(user_query="我的优惠券今天能用吗？我还没有提供券码。",allowed_capabilities=["get_coupon"])
    from app.m1.routing import decide_route
    reference=decide_route(clarification["input"]["user_query"],available_capabilities=["get_coupon"]).model_dump()
    clarification["expected_outcome"]["route"]={k:reference[k] for k in clarification["expected_outcome"]["route"]}
    monkeypatch.setattr(previous,"fictional_cases",lambda:base_cases+actions+[__import__("eval.m6_1.contracts",fromlist=["EvalCase"]).EvalCase.from_dict(clarification)])
    original_workflow=previous.make_workflow
    def workflow(*a,**k):
        cases,w,now=original_workflow(*a,**k)
        path=w.root/w.config["content_intake_permit"]["source_path"]
        data=json.loads(path.read_text(encoding="utf8"));data["schema_version"]="formal-owner-action-assets-v2"
        path.write_text(json.dumps(data),encoding="utf8")
        config=w.config.copy();config["content_intake_permit"]={**config["content_intake_permit"],"source_sha256":raw_hash(path)}
        return cases,AssetWorkflow(w.root,config),now
    monkeypatch.setattr(previous,"make_workflow",workflow)
    cases,store,requests=previous.deploy(tmp_path,monkeypatch,multi=True)
    binding={"url_env":"SHOPRELAY_R25_TEST_DATABASE_URL","host":private["host"],"port":private["port"],"database":private["database"],
        "identity_manifest":"data/eval/formal-targets/r25-pg.json","checkpoint_schema":"shoprelay_langgraph"}
    if injected:
        # Test-only dependency injection of the existing, immutable target root.
        # Do not reinitialize, relabel or loosen FormalPostgresDatabase checks.
        binding.update(identity_manifest=private["identity_manifest"],container_target=private["container_target"])
        original_init=FormalPostgresDatabase.__init__
        def bound_init(self,repository_root,provided_binding):
            original_init(self,Path(private["repository_root"]),provided_binding)
        monkeypatch.setattr(FormalPostgresDatabase,"__init__",bound_init)
    write_new(tmp_path/"pg.json",binding)
    import scripts.run_m6_1_b_synthetic as entry
    import sys,contextlib,io
    monkeypatch.setattr(entry,"ROOT",tmp_path);monkeypatch.setattr(sys,"argv",["run_m6_1_b_synthetic.py","--formal-postgres-target-init","pg.json"])
    if not injected:
        with contextlib.redirect_stdout(io.StringIO()):assert entry.main()==0
    identity_root=Path(private["repository_root"]) if injected else tmp_path
    identity=json.loads((identity_root/binding["identity_manifest"]).read_text(encoding="utf8"))
    path=tmp_path/"deployment.json";config=json.loads(path.read_text(encoding="utf8"))
    env=config["experiment_config"]["environment"]
    env["application_postgres"]=binding;env["simulated_action_tools"]=sorted({r.input["owner_interaction"]["tool_id"] for r in actions})
    env["retrieval_evaluation_mode"]="FORMAL_FULL_QUERY_BM25"
    config["experiment_config"]["reporting_cohorts"]={"route":[c.case_id for c in cases],"retrieval":[cases[0].case_id],"outcome":[c.case_id for c in cases]}
    config["experiment_config"]["budget_limits"].update(tool_calls=sum(c.expected_outcome["task"]["per_case_budget"]["tool_calls"] for c in cases),max_calls_per_trial=20)
    config["plan"]["budget"]=config["experiment_config"]["budget_limits"]
    config["plan"].update(target_profile=FormalPostgresDatabase.profile,target_identity=identity["identity"],config_sha256=sha256_json(config["experiment_config"]))
    config["actual_target_binding"][1:]=[FormalPostgresDatabase.profile,identity["identity"]]
    config["allowed_targets"]=[config["actual_target_binding"]]
    config["approved_plan_hashes"]=[sha256_json(config["plan"])]
    path.write_text(json.dumps(config),encoding="utf8")
    from eval.m6_1.formal_application import FormalApplication
    from app.orchestration.checkpointer import open_checkpointer,run_checkpointer_async
    from app.m0.config import load_settings
    original_cleanup=FormalApplication.cleanup
    recoveries=[]
    def verify_before_cleanup(worker,receipt,trial,raw):
        held=__import__("eval.m6_1.formal_application",fromlist=["restore_receipt"]).restore_receipt(receipt["fixture"])
        async def reopen():
            async with open_checkpointer(worker.target.database_url,load_settings(require_api_key=False).checkpoint_schema) as saver:
                saved=await saver.aget_tuple({"configurable":{"thread_id":held.run_id,"checkpoint_ns":""}})
                assert saved is not None
                return saved.config["configurable"]["checkpoint_id"]
        recoveries.append({"run_id":held.run_id,"checkpoint_id":run_checkpointer_async(reopen()),"new_connection":True})
        if injected:
            from app.m1.models import Order
            from app.m3.order_tools import SqlAlchemyOrderReadRepository,ToolExecutionError
            from sqlalchemy import select
            with worker.session_factory() as db:order=db.scalar(select(Order))
            if order is not None:
                with pytest.raises(ToolExecutionError,match='ORDER_NOT_FOUND'):
                    SqlAlchemyOrderReadRepository(worker.session_factory).get_order(order.order_id,'not-the-owner')
                recoveries[-1]['foreign_order_read_refused']=True
        if trial["case_id"]==cases[0].case_id:
            from app.m1.api import create_app,CSRF_HEADER
            from app.m1.services import set_demo_password
            from app.m1.models import DemoCredential,AuthSession
            from sqlalchemy import delete
            from fastapi.testclient import TestClient
            password="VIRTUAL_TEST_ONLY_NOT_A_REAL_CREDENTIAL"
            owner=cases[0].input["owner_map"][cases[0].input["user_id"]]
            with worker.session_factory() as db:set_demo_password(db,owner,password)
            row=raw["application_archive"]["result"]["observations"]["runs"][0]
            payload={"message":cases[0].input["user_query"],"session_id":held.session_id,"client_request_id":row["client_request_id"]}
            all_events=[]
            for n in range(2):
                api=create_app(database_url=worker.target.database_url,engine=worker.engine,orchestrator_mode="langgraph")
                with TestClient(api) as client:
                    csrf=client.get("/api/demo/csrf").json()["csrf_token"]
                    h={"Origin":"http://testserver",CSRF_HEADER:csrf}
                    assert client.post("/api/demo/login",headers=h,json={"username":owner,"password":password}).status_code==200
                    h[CSRF_HEADER]=client.cookies.get("shoprelay_csrf")
                    if n:h["Last-Event-ID"]=all_events[1]["event_id"]
                    stream=client.post("/api/chat/stream",headers=h,json=payload)
                    assert stream.status_code==200 and stream.headers["x-run-id"]==held.run_id
                    events=[json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
                    if not n:all_events=events
                    else:assert [e["event_id"] for e in events]==[e["event_id"] for e in all_events[2:]]
            with worker.session_factory() as db,db.begin():
                db.execute(delete(AuthSession).where(AuthSession.user_id==owner));db.execute(delete(DemoCredential).where(DemoCredential.user_id==owner))
            recoveries[-1]["actual_SSE_app_rebuild_cursor_replay_no_new_Run"]=True
        # Reconfirm only an already committed simulated action: no new Run,
        # proposal, model sample or business effect may appear after reconnect.
        ev=raw["application_archive"].get("owner_interaction")
        if ev and ev.get("required_action_state")=="SUCCEEDED" and ev["state_check"]=="PASS":
            from app.m4.actions import confirm_pending_action,ActionError
            action=ev["snapshot"]["controlled_pending_actions"][0]
            with worker.session_factory() as db:
                row,replayed=confirm_pending_action(db,owner_id=action["owner_id"],action_id=action["action_id"],
                    simulated_enabled=False,environment_name="shoprelay_m11_test",confirmed_arguments_hash=action["arguments_hash"])
                assert replayed and row.status=="SUCCEEDED"
            with worker.session_factory() as db,pytest.raises(ActionError):
                confirm_pending_action(db,owner_id="not-the-owner",action_id=action["action_id"],simulated_enabled=False,
                    environment_name="shoprelay_m11_test",confirmed_arguments_hash=action["arguments_hash"])
        result=original_cleanup(worker,receipt,trial,raw)
        assert original_cleanup(worker,receipt,trial,raw) is True  # settlement recovery must not query an already removed action lease
        return result
    monkeypatch.setattr(FormalApplication,"cleanup",verify_before_cleanup)
    previous.cli(tmp_path,"issue")
    result=previous.cli(tmp_path,"execute")
    assert result["stop_reason"] is None,result
    assert len(result["completed_cases"])==len(cases)
    assert len(requests)==1
    action_rows=[]
    for trial in store.trials(config["experiment_id"]):
        raw=store.raw_result(trial["trial_id"])
        assert raw["application_archive"]["result"]["checkpoint_mode"]=="POSTGRES_DURABLE_NO_AUTOMATIC_REPLAY"
        if trial["case_id"]=="unprotected-multi":
            output=raw["application_archive"]["result"]["result"]
            assert any(h["source_agent"]=="order_logistics" and h["target_agent"]=="policy" for h in output["handoffs"])
            assert {a["tool"] for a in output["tool_call_audit"]}=={"get_order","get_order_items","get_warranty"}
        if trial["case_id"]==cases[0].case_id:
            evaluation=raw["application_archive"]["retrieval_evaluation"]
            assert evaluation["status"]=="RECORDED" and len(evaluation["calls"])==1
            assert len(raw["application_archive"]["read_invocations"])==1  # separate from module measurement
        if trial["case_id"]=="virtual-missing-coupon-code":
            assert raw["application_archive"]["read_invocations"]==[]
            assert raw["code_assessment"]["integrity"]["permission"]=="PASS"
        if "owner_interaction" in raw["application_archive"]:
            ev=raw["application_archive"]["owner_interaction"]
            assert ev["state_check"]=="PASS",ev
            assert ev["before_business_counts"]==ev["after_proposal_business_counts"]
            assert ev["permission"]==ev["audit"]=="PASS"
            assert ev["business_effect_check"]=="PASS"
            assert raw["code_assessment"]["checks"]["simulated_owner_action"]=="PASS"
            action_rows.append({"case_id":trial["case_id"],"decision":ev["decision"],"state":ev["required_action_state"],"before":ev["before_business_counts"],"proposal":ev["after_proposal_business_counts"],"after":{k:len(ev["snapshot"][k]) for k in ev["before_business_counts"]}})
    response=previous.cli(tmp_path,"report");assert previous.cli(tmp_path,"report")==response
    first=json.loads((tmp_path/response["report_path"]).read_text(encoding="utf8"))
    assert first["metrics"]["retrieval"]["N"]==1
    assert first["metrics"]["retrieval"]["measurement_objects"]=={"FORMAL_RETRIEVER_MODULE_FULL_QUERY":1}
    target=FormalPostgresDatabase(tmp_path,binding);engine,factory=target.open()
    with factory() as db:
        assert db.execute(text("SELECT count(*) FROM controlled_pending_actions")).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM users")).scalar_one()==0
    write_new(tmp_path/"r25-proof.json",{"fictional_cases":len(cases),"substitute_requests":len(requests),"model_requests":0,"action_rows":action_rows,"persistent_reopens":recoveries,"result":result,"report":first})
    record_property("r25_proof_root",tmp_path.relative_to(Path.cwd()).as_posix())
    if injected:
        # tmpfs disappears when the engineering container exits. Export the
        # fictional raw archives, scoped ledger and cleanup proofs first.
        import shutil,uuid
        destination=Path('/results')/('pg-cli-evidence-'+uuid.uuid4().hex[:12])
        shutil.copytree(tmp_path,destination)
        record_property('persisted_engineering_evidence',str(destination))
    engine.dispose()
