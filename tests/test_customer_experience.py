import json
from types import SimpleNamespace as NS
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.m0.config import load_settings
from app.m0.data import ingest, seed
from app.m1.api import create_app, CSRF_HEADER, CSRF_COOKIE
from app.m1.chat import DeepSeekProvider
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import Message, Run
from app.m1.services import set_demo_password
from app.m1.delivery_provider import MeteredChatClient
from eval.m6_1.budget import BudgetStop


class SDK:
    def __init__(self, outputs):
        self.outputs, self.requests = list(outputs), []
        self.chat=NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        value=self.outputs.pop(0)
        if isinstance(value,Exception):raise value
        usage=NS(prompt_tokens=50,completion_tokens=20,total_tokens=70,
            model_dump=lambda: {'prompt_tokens':50,'completion_tokens':20,'total_tokens':70})
        return NS(id='offline-response',model='deepseek-flash',usage=usage,
            choices=[NS(finish_reason='stop',message=NS(content=json.dumps(value,ensure_ascii=False)))])


def test_delivery_factory_uses_shared_business_contract_in_actual_api(customer_app):
    from dataclasses import replace
    from scripts.m65_local_delivery import delivery_services
    app,factory,sdk=customer_app
    app.state.settings=replace(app.state.settings,m3_supervisor_enabled=True)
    app.state.graph_services=delivery_services(app.state.chat_provider,app.state.retrieval_service,None)
    assert app.state.graph_services.business_composer is not None
    assert app.state.graph_services.agent_single is None  # normal supervisor, not forced flat A
    sdk.outputs=[{'business_answer':'订单记录状态仅为存储快照，币种未提供时不推断。',
        'policy_answer':'','citations':[],'business_result_refs':[]}]
    with TestClient(app) as client:
        from test_m3_2_order_logistics import _login
        headers=_login(client,'u_demo','synthetic-test-password')
        sid=client.post('/api/sessions',headers=headers,json={'title':'SYNTHETIC'}).json()['session']['session_id']
        from app.m1.models import Order
        with factory() as db:order_id=db.scalars(select(Order.order_id).where(Order.user_id=='u_demo')).first()
        assert order_id
        response=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':f'查询我的订单 {order_id} 的状态','client_request_id':'shared-contract'})
        assert response.status_code==200,response.json()
    assert sdk.requests
    system=sdk.requests[-1]['messages'][0]['content']
    assert '完整响应契约' in system and '目录AVAILABLE' in system and '不同渠道' in system
    payload=json.loads(sdk.requests[-1]['messages'][-1]['content'])
    assert payload['tool_results'] and payload['tool_results'][0]['result_ref']
    assert 'expected_outcome' not in payload


def test_bounded_delivery_cli_persists_attempt_before_http_and_separate_account(tmp_path,monkeypatch):
    from pathlib import Path
    from uuid import uuid4
    from scripts import verify_customer_experience as cli
    root=tmp_path/'ledger';root.mkdir();sid=str(uuid4());calls=[]
    (root/'demo-password').write_text('synthetic')
    config={'questions':['查看我的订单','查询本人订单 ORD-SYN-A 的状态'],
        'batch_id':'synthetic-followup','approval_event':'SYNTHETIC_TEST_ONLY',
        'budget':{'provider_calls':20,'total_tokens':200000,'estimated_usd':0.5}}
    plan=root/'plan.json';plan.write_text(json.dumps(config))
    def path(value):
        if str(value)=='/delivery-ledger':return root
        if str(value).startswith('/delivery-ledger/'):return root/Path(value).name
        if str(value)=='/delivery-state/demo-password':return root/'demo-password'
        return Path(value)
    class Response:
        status_code=200
        def __init__(self,value):self.value=value
        def json(self):return self.value
        def raise_for_status(self):pass
    class Client:
        cookies={'shoprelay_csrf':'synthetic'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url):return Response({'csrf_token':'synthetic'})
        def post(self,url,**kwargs):
            if url=='/api/demo/login':return Response({})
            if url=='/api/sessions':return Response({'session':{'session_id':sid}})
            saved=json.loads((root/'results.json').read_text())
            assert saved['rows'][-1]['status']=='ENTERED'
            assert json.loads((root/'acceptance-account.json').read_text())['session_ids']==[sid]
            calls.append(kwargs['json']['message']);return Response({'answer':'synthetic','route':{}})
    monkeypatch.setattr(cli,'Path',path);monkeypatch.setattr(cli.httpx,'Client',lambda **kw:Client())
    monkeypatch.setattr(cli.sys,'argv',['verify','--plan',str(plan)])
    cli.main();assert calls==config['questions']
    with pytest.raises(RuntimeError):cli.main()


def test_metered_sdk_durable_limit_no_reset_and_unknown_stops(tmp_path):
    (tmp_path/'scope').write_text('AUTOMATED_SYNTHETIC')
    binding={'model':'deepseek-flash','endpoint_host':'api.deepseek.com',
        'knowledge_version':'synthetic','price_source':'official-peak'}
    sdk=SDK([{'ok':True}]*10)
    kwargs={'model':'deepseek-flash','messages':[{'role':'user','content':'synthetic'}], 'max_tokens':128}
    client=MeteredChatClient(sdk,tmp_path,binding)
    for _ in range(10):client.chat.completions.create(**kwargs)
    restarted=MeteredChatClient(sdk,tmp_path,binding)
    with pytest.raises(BudgetStop):restarted.chat.completions.create(**kwargs)
    assert len(sdk.requests)==10
    assert restarted.ledger()[1].snapshot()['active_reservations']==0
    (tmp_path/'scope').write_text('USER_INITIATED')
    failure=SDK([TimeoutError('do not log raw error')])
    other=MeteredChatClient(failure,tmp_path,binding)
    with pytest.raises(TimeoutError):other.chat.completions.create(**kwargs)
    with pytest.raises(BudgetStop):other.chat.completions.create(**kwargs)
    assert len(failure.requests)==1
    assert 'do not log raw error' not in (tmp_path/'provider-events.jsonl').read_text()


def test_manual_continuation_preserves_prefix_and_cumulative_cap(tmp_path):
    from app.m1.delivery_provider import continue_manual_account, DemoBudgetExhausted
    (tmp_path/'scope').write_text('USER_INITIATED')
    binding={'model':'deepseek-flash','endpoint_host':'api.deepseek.com',
        'knowledge_version':'synthetic','price_source':'official-peak'}
    sdk=SDK([{'ok':True}]*100)
    kwargs={'model':'deepseek-flash','messages':[{'role':'user','content':'synthetic'}],'max_tokens':128}
    client=MeteredChatClient(sdk,tmp_path,binding)
    for _ in range(9):client.create(**kwargs)
    before=client.store.budget_snapshot('delivery-user_initiated')
    receipt=continue_manual_account(client,'USER-APPROVED-DELIVERY-CLOSEOUT')
    assert receipt['predecessor']['provider_requests']==9
    restarted=MeteredChatClient(sdk,tmp_path,binding)
    assert restarted.ledger()[1].limits.provider_calls==91
    for _ in range(91):restarted.create(**kwargs)
    with pytest.raises(DemoBudgetExhausted):restarted.create(**kwargs)
    assert len(sdk.requests)==100
    assert before==client.store.budget_snapshot('delivery-user_initiated')
    with pytest.raises(BudgetStop):continue_manual_account(restarted,'duplicate')


def test_manual_continuation_rejects_unknown_and_modified_prefix(tmp_path):
    from app.m1.delivery_provider import continue_manual_account
    from eval.m6_1.budget import BudgetLedger, BudgetLimits
    (tmp_path/'scope').write_text('USER_INITIATED')
    binding={'model':'deepseek-flash','endpoint_host':'api.deepseek.com',
        'knowledge_version':'synthetic','price_source':'official-peak'}
    client=MeteredChatClient(SDK([{'ok':True}]),tmp_path,binding)
    continue_manual_account(client,'USER-APPROVED-DELIVERY-CLOSEOUT')
    # An independently running old process cannot silently alter the sealed prefix.
    old=BudgetLedger(client.store,'delivery-user_initiated',BudgetLimits(provider_calls=10,
        total_tokens=200000,estimated_usd=1,max_concurrent_trials=1,max_duration_seconds=120,max_retries=0))
    reservation=old.reserve(trial_id='visible-chat',category='provider',tokens_reserved=100,estimated_usd_reserved=.01)
    old.settle(reservation,status='COMMIT',actual_calls=1,actual_tokens=None,estimated_usd=None)
    with pytest.raises(BudgetStop,match='predecessor changed'):client.ledger()


@pytest.fixture
def customer_app(tmp_path, monkeypatch):
    monkeypatch.setenv('SHOPRELAY_M3_SUPERVISOR_ENABLED','false')
    path=tmp_path/'app.sqlite'
    seed(path)
    url='sqlite:///'+path.as_posix()
    upgrade_database(url)
    engine=make_engine(url)
    factory=make_session_factory(engine)
    with factory() as db:set_demo_password(db,'u_demo','synthetic-test-password')
    index=tmp_path/'index.json'
    ingest(index)
    cid='policies/return-standard@2026-07#no-reason'
    sdk=SDK([
        {'answer':'可以先核对商品状态，再提交退货申请，由人工审核，不保证批准。你退货的原因是什么？','citations':[cid]},
        {'strategy':'SINGLE','sub_strategy':'BM25','intent_labels':['policy'],'safety_decision':'ALLOW',
         'reason_code':'POLICY_EXPLANATION_FOLLOWUP','needs_clarification':False,
         'clarification_question':None,'required_capabilities':['search_knowledge'],
         'retrieval_query':'普通商品退货申请流程与条件'},
        {'answer':'意思是先说明退货原因，再核对商品状态；提交申请后还要人工审核，不是马上获批。','citations':[cid]},
        {'answer':'无依据的期限。','citations':['unretrieved-source']},
    ])
    settings=load_settings(environ={'DEEPSEEK_API_KEY':'offline'},local_values={})
    provider=DeepSeekProvider(settings,client=sdk)
    saver=MemorySaver()
    @asynccontextmanager
    async def checkpoint(*args,**kwargs):yield saver
    app=create_app(database_url=url,engine=engine,chat_provider=provider,index_path=index,
        orchestrator_mode='langgraph',retrieval_evaluation_at='2026-11-01T00:00:00Z',
        checkpointer_factory=checkpoint,checkpointer_readiness=lambda *args:True)
    yield app,factory,sdk
    engine.dispose()


def test_actual_api_multiturn_provider_payload_and_rejected_candidate(customer_app):
    app,factory,sdk=customer_app
    with TestClient(app) as client:
        token=client.get('/api/demo/csrf').json()['csrf_token']
        headers={'Origin':'http://testserver',CSRF_HEADER:token}
        assert client.post('/api/demo/login',headers=headers,
            json={'username':'u_demo','password':'synthetic-test-password'}).status_code==200
        token=client.cookies.get(CSRF_COOKIE)
        headers[CSRF_HEADER]=token
        response=client.post('/api/sessions',headers=headers,json={'title':'SYNTHETIC'})
        sid=response.json()['session']['session_id']
        first=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':'商品要退货，怎么退','client_request_id':'first'})
        assert first.status_code==200,first.json()
        second=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':'请通俗解释上一段','client_request_id':'followup'})
        assert second.status_code==200,second.json()
        route_payload=sdk.requests[1]['messages'][1]['content']
        assert '你退货的原因是什么' in route_payload
        answer_payload=json.loads(sdk.requests[2]['messages'][1]['content'])
        assert answer_payload['question']=='请通俗解释上一段'
        assert any('退货' in row['content'] for row in answer_payload['evidence'])
        assert all('43天' not in row['content'] for row in answer_payload['evidence'])
        assert '自然' in sdk.requests[2]['messages'][0]['content']
        rejected=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':'普通商品退货条件是什么','client_request_id':'bad-citation'})
        assert rejected.status_code!=200
        with factory() as db:
            runs=list(db.scalars(select(Run).where(Run.session_id==sid)))
            assert sorted(r.status for r in runs)==['FAILED','SUCCEEDED','SUCCEEDED']
            assert not any('无依据的期限' in m.content for m in db.scalars(select(Message).where(Message.session_id==sid,Message.role=='assistant')))
        other=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':'查询别人的订单','client_request_id':'privacy'})
        assert other.status_code==200
        assert len(sdk.requests)==4  # REFUSE performs no model request.


def test_classifier_retrieval_hint_is_not_permission():
    from app.m1.routing import decide_route
    class Classifier:
        def classify_route(self,*args,**kwargs):
            return {'strategy':'AGENT','sub_strategy':'AGENT+SINGLE','intent_labels':['policy'],
                'safety_decision':'ALLOW','reason_code':'FOLLOWUP','decision_source':'LLM',
                'retrieval_query':'退货条件','required_capabilities':['get_order']}
    from app.m1.routing import RouteCapabilityScopeError
    with pytest.raises(RouteCapabilityScopeError):
        # A search hint does not grant an unavailable business capability.
        decide_route('请解释刚才那段',Classifier(),available_capabilities=['search_knowledge'])


@pytest.mark.parametrize('query', ['普通商品的无理由退货有哪些条件？', '产品退款有什么规则？'])
def test_policy_conditions_are_not_mistaken_for_product_catalog_search(query):
    from app.m1.routing import decide_route
    route=decide_route(query)
    assert route.strategy=='SINGLE'
    assert 'search_products' not in route.required_capabilities


def test_one_window_list_keeps_explicit_scope_and_rejects_wrong_scope():
    from pathlib import Path
    from app.m2.self_rag import _answer_semantic_issues
    root=Path(__file__).resolve().parents[1]
    saved=json.loads((root/'docs/probes/customer-experience-r1-20261101/real-conversation-check.json').read_text(encoding='utf8'))
    audit=saved['rows'][1]['run']['error']['details']['answer_rejection_audit']
    index=json.loads((root/'data/delivery-index.json').read_text(encoding='utf8'))
    evidence=[r for r in index['chunks'] if r['chunk_id'] in audit['candidate_citation_ids']]
    text=audit['candidate_answer_redacted']
    assert not _answer_semantic_issues(text,evidence,'请通俗解释刚才的退货条件')
    for old,new in [('7×24','8×24'),('仅普通商品','仅食品'),('未影响二次销售','已影响二次销售')]:
        assert old in text
        assert _answer_semantic_issues(text.replace(old,new),evidence,'请通俗解释刚才的退货条件')


def test_real_requirement_list_through_actual_api(customer_app):
    from pathlib import Path
    app,factory,sdk=customer_app
    saved=json.loads((Path(__file__).resolve().parents[1]/'docs/probes/customer-experience-r1-20261101/real/real-browser-results.json').read_text(encoding='utf8'))
    audit=saved['rows'][0]['run']['error']['details']['answer_rejection_audit']
    sdk.outputs[0]={'answer':audit['candidate_answer_redacted'],'citations':audit['candidate_citation_ids']}
    with TestClient(app) as client:
        token=client.get('/api/demo/csrf').json()['csrf_token']
        headers={'Origin':'http://testserver',CSRF_HEADER:token}
        client.post('/api/demo/login',headers=headers,json={'username':'u_demo','password':'synthetic-test-password'}).raise_for_status()
        headers[CSRF_HEADER]=client.cookies.get(CSRF_COOKIE)
        sid=client.post('/api/sessions',headers=headers,json={'title':'SYNTHETIC'}).json()['session']['session_id']
        response=client.post('/api/chat',headers=headers,json={'session_id':sid,
            'message':'请说明普通商品无理由退货的申请条件。','client_request_id':'saved-requirements'})
        assert response.status_code==200,response.json()
        with factory() as db:
            run=db.get(Run,response.json()['run_id'])
            assert run.status=='SUCCEEDED'
            assert any(m.content==audit['candidate_answer_redacted'] for m in db.scalars(select(Message).where(Message.session_id==sid,Message.role=='assistant')))


def test_real_bullets_retain_explicit_product_state():
    from pathlib import Path
    from app.m2.self_rag import _answer_semantic_issues
    root=Path(__file__).resolve().parents[1]
    saved=json.loads((root/'docs/probes/customer-experience-r1-20261101/real-final/real-browser-results.json').read_text(encoding='utf8'))
    audit=saved['rows'][0]['run']['error']['details']['answer_rejection_audit']
    index=json.loads((root/'data/delivery-index.json').read_text(encoding='utf8'))
    evidence=[r for r in index['chunks'] if r['chunk_id'] in audit['candidate_citation_ids']]
    text=audit['candidate_answer_redacted']
    assert not _answer_semantic_issues(text,evidence,'请说明普通商品无理由退货的申请条件。')
    assert _answer_semantic_issues(text.replace('未影响二次销售','已影响二次销售'),evidence)


def test_real_profile_route_error_is_not_successful_generic_greeting(customer_app):
    from app.orchestration.adapters import make_legacy_services
    app,factory,sdk=customer_app
    app.state.graph_services=make_legacy_services(provider=app.state.chat_provider,
        classifier=app.state.chat_provider,retrieval_service=app.state.retrieval_service,
        index_path=app.state.policy_index_path,require_model_route_success=True)
    # The SDK returns an answer-shaped object where the route schema is needed.
    # A real response with known usage must still be rejected as a route failure.
    with TestClient(app) as client:
        token=client.get('/api/demo/csrf').json()['csrf_token']
        headers={'Origin':'http://testserver',CSRF_HEADER:token}
        client.post('/api/demo/login',headers=headers,json={'username':'u_demo','password':'synthetic-test-password'}).raise_for_status()
        headers[CSRF_HEADER]=client.cookies.get(CSRF_COOKIE)
        response=client.post('/api/chat',headers=headers,json={'message':'请解释上一次',
            'client_request_id':'route-contract-invalid'})
        assert response.status_code!=200
        assert response.json()['code']=='ROUTE_CLASSIFICATION_FAILED'
        with factory() as db:
            run=db.get(Run,response.json()['details']['run_id'])
            assert run.status=='FAILED' and run.result_json is None
            diagnostic=json.loads(run.error_detail_json)['route']
            assert diagnostic['decision_source']=='FALLBACK'
            assert diagnostic['provider_diagnostic']['provider_response_received'] is True
            assert diagnostic['provider_diagnostic']['response_summary']['finish_reason']=='stop'
            assert diagnostic['usage']['total_tokens']==70


def test_cleanup_refuses_protected_user_session_before_opening_database(tmp_path):
    from scripts.cleanup_m65_delivery import cleanup_exact_receipt
    path=tmp_path/'invalid-receipt.json'
    path.write_text(json.dumps({'session_ids':['71b78efe-6eb9-4659-b92e-e4c19a489f4f'],'run_ids':[]}))
    with pytest.raises(ValueError,match='EXACT_SYNTHETIC_SESSION_RECEIPT_REQUIRED'):
        cleanup_exact_receipt(path)


def test_cleanup_evidence_cannot_escape_ledger_before_opening_database(tmp_path):
    from scripts.cleanup_m65_delivery import cleanup_exact_receipt
    with pytest.raises(ValueError,match='CLEANUP_EVIDENCE_DIRECTORY_OUTSIDE_LEDGER'):
        cleanup_exact_receipt(tmp_path/'unused.json', output_directory=tmp_path)


@pytest.mark.parametrize('window,valid', [('7天',True),('168小时',True),('7×24小时',True),
    ('7*24小时',True),('7 days',True),('8天',False),('24小时',False),('7个月',False)])
def test_elapsed_window_equivalence_preserves_exact_amount(window,valid):
    from app.m2.self_rag import _answer_semantic_issues
    source={'chunk_id':'current','content':'签收后7×24小时内可提交退货申请，由人工审核。'}
    issues=_answer_semantic_issues(f'签收后{window}内可提交退货申请，由人工审核。',[source],'退货期限')
    assert bool(issues) is not valid, issues


def test_elapsed_conversion_never_changes_clock_or_calendar_months():
    from app.m2.self_rag import _answer_semantic_issues
    source={'chunk_id':'current','content':'签收后7×24小时内可提交退货申请，由人工审核。'}
    assert _answer_semantic_issues('付款后7天内可提交退货申请，由人工审核。',[source],'退货期限')
    month={'chunk_id':'month','content':'签收后1个月内可提交退货申请，由人工审核。'}
    assert _answer_semantic_issues('签收后30天内可提交退货申请，由人工审核。',[month],'退货期限')


@pytest.mark.parametrize('archive,question',[
    ('real-acceptance.json','商品要退货，怎么退'),
    ('real-acceptance-after-fix.json','我想了解普通商品退货的申请条件')])
def test_saved_full_real_candidate_has_local_windows_not_global_boundary(archive,question):
    from pathlib import Path
    from app.m2.self_rag import _answer_semantic_issues
    root=Path(__file__).resolve().parents[1]
    saved=json.loads((root/'docs/probes/customer-experience-r1-20261101'/archive).read_text(encoding='utf8'))
    audit=saved['rows'][0]['run']['error']['details']['answer_rejection_audit']
    index=json.loads((root/'data/delivery-index.json').read_text(encoding='utf8'))
    evidence=[r for r in index['chunks'] if r['chunk_id'] in audit['candidate_citation_ids']]
    candidate=audit['candidate_answer_redacted']
    assert _answer_semantic_issues(candidate,evidence,question)==[]
    # Mutate the relevant local assertion, not an unrelated sentence.
    mutations=(
        candidate.replace('7×24 小时','8×24 小时'),
        candidate.replace('如果是普通商品','如果是食品'),
        candidate.replace('没有影响二次销售','已经影响二次销售'),
        candidate.replace('签收后 7','付款后 7'),
        candidate.replace('超过 30 天需转人工核对保修','超过 30 天仍可提交退货申请'),
    )
    for altered in mutations:
        if altered!=candidate:
            assert _answer_semantic_issues(altered,evidence,question)


def test_acceptance_receipt_budget_is_independent_durable_and_closed(tmp_path):
    from app.m1.delivery_provider import acceptance_request
    sid='06704467-79ef-49cd-aa1f-738946c724ea'
    account={'batch_id':'customer-multiturn-r1','status':'OPEN','approval_event':'synthetic-test-only',
        'session_ids':[sid],'provider_calls':20,'total_tokens':400000,'estimated_usd':1}
    (tmp_path/'scope').write_text('USER_INITIATED')
    (tmp_path/'acceptance-account.json').write_text(json.dumps(account))
    binding={'model':'deepseek-flash','endpoint_host':'api.deepseek.com','knowledge_version':'synthetic','price_source':'official-peak'}
    sdk=SDK([{'ok':True}]*20)
    client=MeteredChatClient(sdk,tmp_path,binding)
    token=acceptance_request.set((account['batch_id'],sid,'synthetic-request'))
    kwargs={'model':'deepseek-flash','messages':[{'role':'user','content':'synthetic'}],'max_tokens':128}
    try:
        for _ in range(20):client.chat.completions.create(**kwargs)
        restarted=MeteredChatClient(sdk,tmp_path,binding)
        with pytest.raises(BudgetStop):restarted.chat.completions.create(**kwargs)
        assert restarted.ledger()[1].snapshot()['active_reservations']==0
        account['status']='CLOSED';(tmp_path/'acceptance-account.json').write_text(json.dumps(account))
        with pytest.raises(BudgetStop):restarted.chat.completions.create(**kwargs)
    finally:acceptance_request.reset(token)
    assert len(sdk.requests)==20
    assert client.ledger()[0]=='USER_INITIATED'
    assert client.ledger()[1].snapshot()['calls_by_category'].get('provider',0)==0


def test_sse_uses_only_receipt_account_and_closed_session_cannot_fall_into_manual(customer_app,tmp_path):
    from app.m1.delivery_provider import install_acceptance_accounting
    app,factory,sdk=customer_app
    directory=tmp_path/'account';directory.mkdir();(directory/'scope').write_text('USER_INITIATED')
    binding={'model':'deepseek-flash','endpoint_host':'api.deepseek.com','knowledge_version':'synthetic','price_source':'official-peak'}
    metered=MeteredChatClient(sdk,directory,binding)
    app.state.chat_provider.client=metered
    install_acceptance_accounting(app,directory)
    with TestClient(app) as client:
        headers={'Origin':'http://testserver',CSRF_HEADER:client.get('/api/demo/csrf').json()['csrf_token']}
        client.post('/api/demo/login',headers=headers,json={'username':'u_demo','password':'synthetic-test-password'}).raise_for_status()
        headers[CSRF_HEADER]=client.cookies.get(CSRF_COOKIE)
        sid=client.post('/api/sessions',headers=headers,json={'title':'SYNTHETIC_ACCEPTANCE'}).json()['session']['session_id']
        account={'batch_id':'customer-multiturn-r1','status':'OPEN','approval_event':'synthetic-test-only',
            'session_ids':[sid],'provider_calls':20,'total_tokens':400000,'estimated_usd':1}
        (directory/'acceptance-account.json').write_text(json.dumps(account))
        response=client.post('/api/chat/stream',headers=headers,json={'session_id':sid,'message':'商品要退货，怎么退','client_request_id':'scoped'})
        assert response.status_code==200 and 'answer' in response.text,response.text
        event=json.loads((directory/'provider-events.jsonl').read_text().splitlines()[0])
        assert event['scope']=='customer-multiturn-r1' and event['session_id']==sid
        assert metered.ledger()[1].snapshot()['calls_by_category'].get('provider',0)==0
        before=len(sdk.requests)
        account['status']='CLOSED';(directory/'acceptance-account.json').write_text(json.dumps(account))
        closed=client.post('/api/chat',headers=headers,json={'session_id':sid,'message':'继续','client_request_id':'closed'})
        assert closed.status_code==409 and len(sdk.requests)==before


def test_unknown_operational_fact_answer_and_followup_deliver_without_invented_phase(customer_app):
    app,factory,sdk=customer_app
    cid='policies/return-standard@2026-07#no-reason'
    route={'strategy':'SINGLE','sub_strategy':'BM25','intent_labels':['policy'],'safety_decision':'ALLOW',
        'reason_code':'POLICY_EXPLANATION','needs_clarification':False,'clarification_question':None,
        'required_capabilities':['search_knowledge'],'retrieval_query':'退货申请政策'}
    answers=['当前可核验的政策没有给出退货地址，暂时不能提供准确地址。建议先向商家核实收件信息，确认前先不要寄出。',
        '建议先向商家说明退货需求，并核实收件地址；目前没有可核验的地址，先不要自行寄出。']
    sdk.outputs=[route,{'answer':answers[0],'citations':[cid]},route,{'answer':answers[1],'citations':[cid]}]
    with TestClient(app) as client:
        headers={'Origin':'http://testserver',CSRF_HEADER:client.get('/api/demo/csrf').json()['csrf_token']}
        client.post('/api/demo/login',headers=headers,json={'username':'u_demo','password':'synthetic-test-password'}).raise_for_status()
        headers[CSRF_HEADER]=client.cookies.get(CSRF_COOKIE)
        sid=client.post('/api/sessions',headers=headers,json={'title':'SYNTHETIC_ADDRESS'}).json()['session']['session_id']
        for index,question in enumerate(['退货寄到哪个地址','那我接下来怎么办']):
            response=client.post('/api/chat',headers=headers,json={'session_id':sid,'message':question,'client_request_id':f'address-{index}'})
            assert response.status_code==200,response.json()
            assert response.json()['answer']==answers[index]
        assert answers[0] in sdk.requests[2]['messages'][1]['content']
        payload=json.loads(sdk.requests[3]['messages'][1]['content'])
        assert payload['question']=='那我接下来怎么办'
        assert '下一步' in sdk.requests[3]['messages'][0]['content']
        with factory() as db:
            assert len(list(db.scalars(select(Message).where(Message.session_id==sid,Message.role=='assistant'))))==2


def test_archived_unknown_address_phase_was_not_a_blanket_validator_false_reject():
    from pathlib import Path
    from app.m2.self_rag import _answer_semantic_issues
    root=Path(__file__).resolve().parents[1]
    archived=json.loads((root/'docs/probes/customer-experience-r1-20261101/real-final/real-browser-results.json').read_text(encoding='utf8'))
    audit=next(row['run']['error']['details']['answer_rejection_audit'] for row in archived['rows']
        if row['question']=='退货应该寄到哪个地址？')
    index=json.loads((root/'data/delivery-index.json').read_text(encoding='utf8'))
    evidence=[r for r in index['chunks'] if r['chunk_id'] in audit['candidate_citation_ids']]
    assert any(i['code']=='ANSWER_UNSUPPORTED_POLICY_PHASE' for i in _answer_semantic_issues(audit['candidate_answer_redacted'],evidence,'退货应该寄到哪个地址？'))


def test_missing_resource_clarification_is_delivered_with_zero_business_reads(customer_app):
    app,factory,sdk=customer_app
    sdk.outputs=[{'strategy':'DIRECT','sub_strategy':None,'intent_labels':['clarification'],
        'safety_decision':'ALLOW','reason_code':'ORDER_IDENTIFIER_REQUIRED','needs_clarification':True,
        'clarification_question':'请提供需要核对的订单号。','required_capabilities':[],'retrieval_query':None}]
    with TestClient(app) as client:
        headers={'Origin':'http://testserver',CSRF_HEADER:client.get('/api/demo/csrf').json()['csrf_token']}
        client.post('/api/demo/login',headers=headers,json={'username':'u_demo','password':'synthetic-test-password'}).raise_for_status()
        headers[CSRF_HEADER]=client.cookies.get(CSRF_COOKIE)
        response=client.post('/api/chat',headers=headers,json={'message':'我付钱了吗？','client_request_id':'missing-id'})
        assert response.status_code==200,response.json()
        assert response.json()['answer']=='请提供需要核对的订单号。'
        assert response.json()['route']['required_capabilities']==[]
        assert len(sdk.requests)==1
        assert 'DIRECT and REFUSE must always have required_capabilities: []' in sdk.requests[0]['messages'][0]['content']
