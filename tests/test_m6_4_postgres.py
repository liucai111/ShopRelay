"""Actual isolated PostgreSQL API owner and concurrent-confirmation proof."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete,or_,select

from app.m1.api import create_app,CSRF_HEADER
from app.m1.models import Base,User,Run,ConversationSession,PendingAction,SimulatedTestAction
from app.m1.services import set_demo_password
from eval.m6_1.personal_dev_database import FormalPostgresDatabase


def test_current_pg_owner_confirmation_concurrency_and_exact_receipt(tmp_path,record_property):
    private=json.loads(os.environ['SHOPRELAY_ENGINEERING_PG_CONNECTION'])
    os.environ['SHOPRELAY_M64_PG_URL']=private['url']
    binding={k:private[k] for k in ('host','port','database','identity_manifest','container_target')}
    binding['url_env']='SHOPRELAY_M64_PG_URL'
    target=FormalPostgresDatabase(__import__('pathlib').Path(private['repository_root']),binding)
    engine,factory=target.open()
    prefix='m64-'+uuid.uuid4().hex[:12]
    # These are the two existing server-granted synthetic identities. Target
    # preflight proves they do not exist; we never widen production grants.
    owner,other='u_demo','u_other'
    password='VIRTUAL_ENGINEERING_PASSWORD_ONLY'
    with factory() as db:
        db.add_all([User(user_id=owner,display_name='fictional owner'),User(user_id=other,display_name='fictional other')]);db.flush()
        for uid in (owner,other):set_demo_password(db,uid,password)
        db.commit()
    app=create_app(database_url=target.database_url,engine=engine,orchestrator_mode='legacy',
        action_writes_enabled=True,simulated_test_actions_enabled=True)
    action_id=None;sid=None;rid=None
    calls=[]
    def login(client,uid):
        csrf=client.get('/api/demo/csrf').json()['csrf_token']
        h={'Origin':'http://testserver',CSRF_HEADER:csrf}
        assert client.post('/api/demo/login',headers=h,json={'username':uid,'password':password}).status_code==200
        h[CSRF_HEADER]=client.cookies.get('shoprelay_csrf')
        return h
    try:
        with TestClient(app) as client,TestClient(app) as foreign:
            h=login(client,owner);f=login(foreign,other)
            sid=client.post('/api/sessions',headers=h,json={'title':'M64 virtual concurrency'}).json()['session']['session_id']
            rid=client.post(f'/api/sessions/{sid}/runs',headers=h,json={'client_request_id':prefix,'request_payload':{'kind':'engineering'}}).json()['run']['run_id']
            assert foreign.get(f'/api/runs/{rid}').status_code==404
            assert foreign.get(f'/api/runs/{rid}/events').status_code==404
            request={'tool_id':'simulated_test_action','session_id':sid,'run_id':rid,'target_resource_id':'M42-SIM-'+prefix,
                'arguments':{'label':'fictional concurrency','value':'no production effect'},'idempotency_key':prefix}
            response=client.post('/api/actions',headers=h,json=request)
            assert response.status_code==200,response.json()
            action=response.json()['action'];action_id=action['action_id']
            with factory() as db:
                assert not list(db.scalars(select(SimulatedTestAction).where(SimulatedTestAction.action_id==action_id)))
                pending=db.get(PendingAction,action_id);arguments_hash=pending.arguments_hash
            path=f'/api/actions/{action_id}/confirm'
            payload={'confirm':True,'arguments_hash':arguments_hash}
            assert foreign.post(path,headers=f,json=payload).status_code==404
            assert client.post(path,headers=h,json={**payload,'arguments_hash':'f'*64}).status_code==409
            # Two independent authenticated HTTP clients, two DB transactions.
            def confirm(_):
                with TestClient(app) as parallel:
                    headers=login(parallel,owner)
                    result=parallel.post(path,headers=headers,json=payload)
                    calls.append({'status_code':result.status_code,'body':result.json()})
                    return result.status_code
            with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(confirm,range(2)))==[200,200]
            with factory() as db:
                effects=list(db.scalars(select(SimulatedTestAction).where(SimulatedTestAction.action_id==action_id)))
                assert len(effects)==1
            # Recreate application, retain database/idempotency state.
            rebuilt=create_app(database_url=target.database_url,engine=engine,orchestrator_mode='legacy',
                action_writes_enabled=True,simulated_test_actions_enabled=True)
            with TestClient(rebuilt) as restored:
                headers=login(restored,owner)
                assert restored.post(path,headers=headers,json=payload).status_code==200
            with factory() as db:assert len(list(db.scalars(select(SimulatedTestAction).where(SimulatedTestAction.action_id==action_id))))==1
            from app.orchestration.persistence import SQLAlchemyObservabilitySink
            from app.orchestration.observability import MetricRecord
            from app.m1.models import ObservabilityRecord
            from app.m3.order_tools import AgentExecutionContext,ToolExecutionError
            from sqlalchemy.exc import SQLAlchemyError
            sink=SQLAlchemyObservabilitySink(factory,user_id=owner,run_id=rid,trace_id=prefix,graph_version='m64')
            execution=AgentExecutionContext(result_store={},sink=sink,run_id=rid,trace_id=prefix,parent_span_id=None)
            def broken_read(_order,_owner):raise SQLAlchemyError('engineering injected dependency failure')
            with pytest.raises(ToolExecutionError,match='TOOL_DATABASE_UNAVAILABLE'):
                execution.execute_read('get_order','M64-ENGINEERING-FAULT',owner,broken_read)
            assert execution.call_records[0]['error_code']=='TOOL_DATABASE_UNAVAILABLE'
            assert execution.result_store=={}
            sink=SQLAlchemyObservabilitySink(factory,user_id=owner,run_id=rid,trace_id=prefix,graph_version='m64')
            with factory() as db:assert db.get(PendingAction,action_id).status=='SUCCEEDED'
            sink.record_metrics(MetricRecord(run_status='SUCCEEDED'))
            with factory() as db:
                alerts=[json.loads(r.payload_json) for r in db.scalars(select(ObservabilityRecord).where(
                    ObservabilityRecord.record_id.like('alert:db:%')).order_by(ObservabilityRecord.timestamp))]
            assert [a['event'] for a in alerts]==['TRIGGERED','RECOVERED']
            record_property('current_pg_alert_roundtrip',json.dumps(alerts))
    finally:
        # Exact rows scoped to our new owners/Run/action only; never truncate.
        receipt={}
        with factory() as db,db.begin():
            if rid and (run:=db.get(Run,rid)):
                run.status='FAILED';db.flush()  # synthetic source Run never executed a model
            for table in reversed(Base.metadata.sorted_tables):
                filters=[]
                for key,value in [('user_id',[owner,other]),('owner_id',[owner,other]),('session_id',[sid]),('run_id',[rid]),('action_id',[action_id])]:
                    if key in table.c and all(v is not None for v in value):filters.append(table.c[key].in_(value))
                if not filters:continue
                condition=or_(*filters)
                ids=[dict(row._mapping) for row in db.execute(select(*table.primary_key).where(condition))]
                if ids:
                    receipt[table.name]=ids
                    db.execute(delete(table).where(condition))
        with factory() as db:
            assert db.get(User,owner) is None and db.get(User,other) is None
            assert not action_id or db.get(PendingAction,action_id) is None
        record_property('current_pg_identity',target.identity)
        record_property('exact_engineering_cleanup_receipt',json.dumps(receipt,default=str))
        record_property('two_concurrent_confirmation_responses',json.dumps(calls))
        engine.dispose()
