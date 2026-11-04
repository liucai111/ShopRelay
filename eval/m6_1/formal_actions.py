"""Owner-scripted API steps within the existing formal Trial, never model consent.

The signed environment separately grants the simulated tools. All proposals,
CSRF/authentication, owner checks, confirmation hashes and transactions go through
the real FastAPI/M4 implementation. No production switch or remote API is used.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import select, delete

from app.m1 import models as m
from .contracts import sha256_json
from .formal_assets import write_new
from .grants import formal_case_payload
from .fixture_read_models import product_id

ACTION_TOOLS = {"create_return_request", "create_refund_request", "cancel_order", "escalate_to_human"}


def business_effect_check(tool, decision, state, before, after, *, paid_cancel=False):
    expected={key:0 for key in before}
    if decision=="CONFIRM" and state=="SUCCEEDED":
        if tool=="cancel_order":expected.update(order_events=1,refunds=int(paid_cancel))
        else:expected[{"create_return_request":"returns","create_refund_request":"refunds","escalate_to_human":"support_tickets"}[tool]]=1
    return "PASS" if {key:after[key]-before[key] for key in before}==expected else "FAIL"


def assess_interaction(case,evidence):
    """Recompute from visible API receipts and actual rows, not saved PASS flags."""
    command=case.input["owner_interaction"];snapshot=evidence["snapshot"];pending=snapshot["controlled_pending_actions"]
    calls=evidence["calls"]
    if evidence['status']=='NOT_EXECUTED_CORE_FACTS_INCOMPLETE':
        value=formal_case_payload(case)['input']
        source=next(r for r in value['fixture']['orders_and_snapshots'] if r['order_id']==command['target_resource_id'])
        missing=[k for k in ('status','paid_cents') if source.get(k) is None]
        verified=bool(missing) and missing==evidence['missing_core_fields'] and evidence['core_order_present'] is False and not calls and not evidence['policy_reads'] and not any(snapshot.values())
        return {'permission':'PASS' if verified else 'UNKNOWN','audit':'PASS' if verified else 'UNKNOWN',
            'state_check':'FAIL','business_effect_check':'NOT_APPLICABLE','task':'FAIL'}
    if evidence["status"]=="SKIPPED_SOURCE_RUN_NOT_SUCCEEDED":
        return {"permission":"PASS" if not calls and not pending else "UNKNOWN","audit":"PASS" if not calls and not pending else "UNKNOWN",
            "state_check":"FAIL","business_effect_check":"NOT_APPLICABLE","task":"FAIL"}
    before=evidence["before_business_counts"];after={key:len(snapshot[key]) for key in before}
    expected={"LEAVE_PENDING":"PENDING_CONFIRMATION","CONFIRM":"SUCCEEDED","REJECT":"REJECTED"}[command["decision"]]
    state=pending[0]["status"] if len(pending)==1 else "NO_UNIQUE_ACTION"
    args=json.loads(pending[0]["sanitized_arguments_json"]) if len(pending)==1 else {}
    effect=business_effect_check(command["tool_id"],command["decision"],state,before,after,
        paid_cancel=command["tool_id"]=="cancel_order" and len(pending)==1 and args["order_snapshot"]["status"]=="PAID")
    owner=case.input["owner_map"][case.input["user_id"]]
    zero_denial=bool(calls) and not pending and not evidence["run_events"] and all(c["status_code"]>=400 for c in calls) and before==after
    if pending:
        permission="PASS" if all(r["owner_id"]==owner and r["tool_id"]==command["tool_id"] and r["run_id"]==evidence["source_run_id"] for r in pending) else "FAIL"
    else:permission="PASS" if zero_denial else "UNKNOWN"
    if effect=="FAIL" or (evidence["after_proposal_business_counts"] is not None and before!=evidence["after_proposal_business_counts"]):permission="FAIL"
    ids={row["action_id"] for row in pending}
    linked_events=bool(evidence["run_events"]) and all(row["run_id"]==evidence["source_run_id"] and row["trace_id"] and
        json.loads(row["payload_json"])["action_id"] in ids for row in evidence["run_events"])
    audit=bool(calls) and all(c["response"].get("trace_id") for c in calls) and ((bool(snapshot["action_audit"]) and linked_events) or zero_denial)
    state_check="PASS" if len(pending)==1 and state==expected else "FAIL"
    return {"permission":permission,"audit":"PASS" if audit else "UNKNOWN","state_check":state_check,"business_effect_check":effect,
        "task":"FAIL" if state_check=="FAIL" or effect=="FAIL" or any(c["status_code"]!=200 for c in calls) else "UNKNOWN"}


def validate_interaction(value):
    from app.m1.api import CreatePendingActionInput
    if set(value) != {"tool_id","target_resource_id","arguments","decision"} or value["decision"] not in {"LEAVE_PENDING","CONFIRM","REJECT"}:
        raise ValueError("owner interaction must explicitly select a bounded action and consent step")
    if value["tool_id"] not in ACTION_TOOLS: raise ValueError("unsupported simulated owner action")
    CreatePendingActionInput.model_validate({k:v for k,v in value.items() if k!="decision"} |
        {"session_id":"boundary-validation","run_id":"boundary-validation","idempotency_key":"boundary-validation"})


def incomplete_core_action_fields(case,receipt):
    value=formal_case_payload(case)['input'];target=value['owner_interaction']['target_resource_id']
    source=next((r for r in value['fixture']['orders_and_snapshots'] if r['order_id']==target),None)
    if source is None:raise ValueError('action target lacks an explicit fixture source')
    if value['owner_map'][source['owner_id']]!=value['owner_map'][value['user_id']]:raise PermissionError('action target belongs to another fixture owner')
    missing=[k for k in ('status','paid_cents') if source.get(k) is None]
    if target not in receipt.core_order_ids:
        if not missing:raise ValueError('complete source order was not materialized')
        return missing
    return []


def materialize_action_facts(worker, case, receipt):
    """Load only explicitly supplied normalized business facts; never fill unknowns."""
    if worker.target.profile != "M6.2-FORMAL-POSTGRES":
        raise ValueError("simulated actions require the bound isolated PostgreSQL target")
    interaction=formal_case_payload(case)["input"]["owner_interaction"]
    validate_interaction(interaction)
    if interaction["tool_id"] not in worker.env["simulated_action_tools"]:
        raise PermissionError("action absent from signed environment scope")
    fixture=formal_case_payload(case)["input"]["fixture"]
    orders={r["id"]:r["order_id"] for r in fixture["orders_and_snapshots"]}
    with worker.session_factory() as db, db.begin():
        worker.coordinator._check_receipt(db,receipt)
        if incomplete_core_action_fields(case,receipt):
            # The real core Order requires a non-null payment amount. Sparse
            # read facts remain usable;never invent0 merely to satisfy the FK.
            return
        for row in fixture.get("products",[]):
            db.add(m.Product(product_id=product_id(row["id"]),name=row["name"],category=row["category"],price_cents=row["price_cents"]))
        db.flush()
        for row in fixture.get("inventory",[]):
            db.add(m.Inventory(product_id=product_id(row["product_id"]),available_qty=row["available_qty"],reserved_qty=row["reserved_qty"],version=row["version"]))
        for row in fixture.get("order_items",[]):
            db.add(m.OrderItem(order_id=orders[row["order_id"]],product_id=product_id(row["product_id"]),quantity=row["quantity"],paid_cents=row["paid_cents"]))
        db.flush()
        for row in fixture.get("order_inventory_reservations",[]):
            db.add(m.OrderInventoryReservation(reservation_id=row["id"],order_id=orders[row["order_id"]],
                product_id=product_id(row["product_id"]),quantity=row["quantity"],status=row["status"],
                source_type="SIMULATED_TEST_DATA",source_ref=row["source_ref"],created_at=datetime.fromisoformat(row["created_at"].replace("Z","+00:00"))))
        def source_values(model,row,identifier):
            values={k:v for k,v in row.items() if k not in {"id","events"}}
            if identifier in values and values[identifier]!=row["id"]:raise ValueError("fixture source and business identifier conflict")
            values[identifier]=row["id"]
            values["order_id"]=orders[row["order_id"]]
            allowed={c.name for c in model.__table__.columns}
            if not set(values)<=allowed:raise ValueError("unknown normalized after-sales fixture fields")
            for column in model.__table__.columns:
                if isinstance(column.type,m.UTCDateTime) and column.name in values and values[column.name] is not None:
                    values[column.name]=datetime.fromisoformat(values[column.name].replace("Z","+00:00"))
            return values
        for collection,model,identifier in (("returns",m.ReturnRequest,"return_id"),("refunds",m.Refund,"refund_id")):
            for row in fixture.get(collection,[]):db.add(model(**source_values(model,row,identifier)))
            db.flush()
        for collection in ("returns","refunds"):
            for row in fixture.get(collection,[]):
                for event in row.get("events",[]):
                    event={**event,"id":event["event_id"]}
                    db.add(m.AfterSalesEvent(**source_values(m.AfterSalesEvent,event,"event_id")))


def action_snapshot(worker, receipt, product_ids):
    """Exact owned scope, including audit and credential IDs needed for teardown."""
    with worker.session_factory() as db:
        worker.coordinator._check_receipt(db,receipt)
        tickets=db.scalars(select(m.SupportTicket.ticket_id).where(m.SupportTicket.source_run_id==receipt.run_id)).all() if receipt.run_id else []
        actions=db.scalars(select(m.PendingAction.action_id).where(m.PendingAction.run_id==receipt.run_id)).all() if receipt.run_id else []
        selections=[(m.ActionAudit,m.ActionAudit.action_id.in_(actions)),
            (m.SupportTicketEvent,m.SupportTicketEvent.ticket_id.in_(tickets)),(m.SupportTicket,m.SupportTicket.ticket_id.in_(tickets)),
            (m.AfterSalesEvent,m.AfterSalesEvent.order_id.in_(receipt.order_ids)),
            (m.OrderEvent,m.OrderEvent.order_id.in_(receipt.order_ids)),
            (m.SimulatedTestAction,m.SimulatedTestAction.action_id.in_(actions)),
            (m.Refund,m.Refund.order_id.in_(receipt.order_ids)),(m.ReturnRequest,m.ReturnRequest.order_id.in_(receipt.order_ids)),
            (m.PendingAction,m.PendingAction.action_id.in_(actions)),
            (m.AuthSession,m.AuthSession.user_id.in_(receipt.user_ids)),(m.DemoCredential,m.DemoCredential.user_id.in_(receipt.user_ids)),
            (m.OrderInventoryReservation,m.OrderInventoryReservation.order_id.in_(receipt.order_ids)),
            (m.OrderItem,m.OrderItem.order_id.in_(receipt.order_ids)),
            (m.Inventory,m.Inventory.product_id.in_(product_ids)),(m.Product,m.Product.product_id.in_(product_ids))]
        return {model.__tablename__: [json.loads(json.dumps({c.name:getattr(r,c.name) for c in model.__table__.columns},default=str))
            for r in db.scalars(select(model).where(condition).order_by(*model.__table__.primary_key.columns)).all()]
            for model,condition in selections}


def execute_owner_interaction(worker,case,receipt,result,ledger,trial_id,*,remaining_seconds):
    import asyncio
    import httpx
    from app.m1.api import create_app,CSRF_HEADER
    from app.m1.services import set_demo_password
    from app.orchestration.checkpointer import run_checkpointer_async
    from .fixture_read_models import fixture_retrieval
    value=formal_case_payload(case)["input"]; command=value["owner_interaction"]
    password=secrets.token_urlsafe(32)
    owner=value["owner_map"][value["user_id"]]
    products=[product_id(p["id"]) for p in value["fixture"].get("products",[])]
    before=action_snapshot(worker,receipt,products)
    if result["status"]!="SUCCEEDED":
        return {"status":"SKIPPED_SOURCE_RUN_NOT_SUCCEEDED","calls":[],"source_run_id":receipt.run_id,
            "products":products,"snapshot":before,"permission":"PASS","audit":"PASS","task":"FAIL"}
    missing=incomplete_core_action_fields(case,receipt)
    if missing:
        with worker.session_factory() as db:present=db.get(m.Order,command['target_resource_id']) is not None
        return {'status':'NOT_EXECUTED_CORE_FACTS_INCOMPLETE','calls':[],'policy_reads':[],
            'source_run_id':receipt.run_id,'products':products,'snapshot':before,'missing_core_fields':missing,'core_order_present':present,
            'reason':'Sparse source cannot satisfy the real core Order schema without inventing payment facts;action not attempted,not a policy eligibility ruling',
            'user_delivery':'No action API response delivered;main Run answer is separate'}
    with worker.session_factory() as db: set_demo_password(db,owner,password)
    tool=command["tool_id"]
    app=create_app(database_url=worker.target.database_url,engine=worker.engine,orchestrator_mode="legacy",
        action_writes_enabled=True,return_actions_enabled=tool=="create_return_request",refund_actions_enabled=tool=="create_refund_request",
        cancel_actions_enabled=tool=="cancel_order",ticket_actions_enabled=tool=="escalate_to_human")
    from .formal_application import application_plan
    plan=application_plan(case)
    store=worker.coordinator.repositories(plan,receipt,maximum=worker.env["repository_read_cap_per_case"])["store"]
    from .formal_application import MeteredReads
    retrieval_calls=[]
    app.state.retrieval_service=MeteredReads(fixture_retrieval(store,plan["knowledge_version"],plan["evaluation_at"]),ledger,trial_id,retrieval_calls,retrieval=True)
    calls=[]
    proposal_counts=None
    async def interact():
        nonlocal proposal_counts
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
                csrf=(await client.get("/api/demo/csrf")).json()["csrf_token"]
                headers={"Origin":"http://testserver",CSRF_HEADER:csrf}
                login=await client.post("/api/demo/login",headers=headers,json={"username":owner,"password":password})
                if login.status_code!=200: raise PermissionError("isolated owner authentication failed")
                headers[CSRF_HEADER]=client.cookies.get("shoprelay_csrf")
                async def send(path,payload):
                    held=ledger.reserve(trial_id=trial_id,category="tool")
                    try:
                        response=await client.post(path,headers=headers,json=payload)
                        body=response.json()
                        calls.append({"path":path,"status_code":response.status_code,"response":body})
                        return response,body
                    finally: ledger.settle(held,status="COMMIT",actual_calls=1,actual_tokens=0,estimated_usd=0)
                response,body=await send("/api/actions",{k:v for k,v in command.items() if k!="decision"} |
                    {"session_id":receipt.session_id,"run_id":receipt.run_id,"idempotency_key":trial_id+":owner-action"})
                if response.status_code!=200:return
                action=body["action"]
                pending_snapshot=action_snapshot(worker,receipt,products)
                proposal_counts={t:len(pending_snapshot[t]) for t in ("refunds","returns","support_tickets","order_events")}
                if command["decision"]=="CONFIRM":
                    await send("/api/actions/"+action["action_id"]+"/confirm",{"confirm":True,"arguments_hash":action["arguments_hash"]})
                elif command["decision"]=="REJECT":
                    await send("/api/actions/"+action["action_id"]+"/reject",{"acknowledge":True})
    async def bounded():
        async with asyncio.timeout(remaining_seconds):await interact()
    try: run_checkpointer_async(bounded())
    finally:
        # Ephemeral API login material never enters evaluation archives.
        with worker.session_factory() as db,db.begin():
            db.execute(delete(m.AuthSession).where(m.AuthSession.user_id==owner))
            db.execute(delete(m.DemoCredential).where(m.DemoCredential.user_id==owner))
    after=action_snapshot(worker,receipt,products)
    baseline={t:len(before[t]) for t in ("refunds","returns","support_tickets","order_events")}
    expected_status={"LEAVE_PENDING":"PENDING_CONFIRMATION","CONFIRM":"SUCCEEDED","REJECT":"REJECTED"}[command["decision"]]
    counts={t:len(after[t]) for t in baseline}
    with worker.session_factory() as db:
        action_events=[{c.name:getattr(row,c.name) for c in m.RunEvent.__table__.columns}
            for row in db.scalars(select(m.RunEvent).where(m.RunEvent.run_id==receipt.run_id).order_by(m.RunEvent.seq)).all()
            if "action_id" in json.loads(row.payload_json)]
    evidence={"status":"OBSERVED_OWNER_SCRIPT_NOT_AGENT_AUTONOMY","source_run_id":receipt.run_id,"products":products,
        "decision":command["decision"],"calls":calls,"policy_reads":retrieval_calls,"before_business_counts":baseline,"after_proposal_business_counts":proposal_counts,
        "run_events":json.loads(json.dumps(action_events,default=str)),"required_action_state":expected_status,
        "after_business_counts":counts,"snapshot":after,"snapshot_sha256":sha256_json(after)}
    return {**evidence,**assess_interaction(case,evidence)}


def cleanup_action_facts(worker,receipt,evidence):
    # All rows are checked against the archived exact snapshot before deletion;
    # a newly appeared action/result is not silently erased by fixture cleanup.
    current=action_snapshot(worker,receipt,evidence["products"])
    proof=worker.artifacts/((receipt.run_id or receipt.lease_id)+".actions-cleanup.json")
    if proof.exists():
        if any(current.values()):raise ValueError("action rows reappeared after exact cleanup")
        return
    if current!=evidence["snapshot"]:raise ValueError("action cleanup snapshot changed; preserve scene")
    models={model.__tablename__:model for model in (m.ActionAudit,m.SupportTicketEvent,m.SupportTicket,m.AfterSalesEvent,m.OrderEvent,
        m.SimulatedTestAction,m.Refund,m.ReturnRequest,m.PendingAction,m.AuthSession,m.DemoCredential,m.OrderInventoryReservation,m.OrderItem,m.Inventory,m.Product)}
    with worker.session_factory() as db,db.begin():
        worker.coordinator._check_receipt(db,receipt)
        for table,rows in current.items():
            model=models[table]
            for row in rows:
                db.execute(delete(model).where(*(column==row[column.name] for column in model.__table__.primary_key.columns)))
    if any(action_snapshot(worker,receipt,evidence["products"]).values()):raise ValueError("action cleanup residual")
    write_new(proof,{"source_run_id":receipt.run_id,"archived_snapshot_sha256":sha256_json(current),
        "cleanup":"EXACT_SIMULATED_FIXTURE_TEARDOWN_NOT_A_USER_DECISION","residual_rows":0})
