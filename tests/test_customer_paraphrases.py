"""Natural paraphrases go through routing, owner reads and response validation."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.m1.models import Message, Run
from app.m1.routing import decide_route
from app.m2.self_rag import _answer_semantic_issues, _policy_window_facts
from app.orchestration.runtime import delivered_order_query
from test_customer_experience import customer_app
from test_m3_2_order_logistics import _login

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT/'docs/probes/customer-paraphrase-20261101'


def saved_candidate():
    fixture = json.loads((ROOT/'tests/fixtures/customer-policy-candidates.json').read_text(encoding='utf8'))
    evidence = [row for row in fixture['evidence'] if row['chunk_id'] in fixture['citations']]
    return fixture['answer'], evidence, fixture['citations']


@pytest.mark.parametrize('question', [
    '查看我的订单', '我有哪些订单', '我的订单有哪些？', '帮我看一下订单',
    '查下我的订单', '查一下本人订单', '我想看看我的订单', '把我的订单列出来',
    '显示我的订单列表', '打开我的购买记录', '看看我买过什么', '我下过哪些单',
    '我的订单', '我的订单列表', '查看订单清单', 'list my orders', 'show my orders', 'view my purchases',
])
def test_order_collection_paraphrases(question):
    route = decide_route(question)
    assert route.strategy == 'AGENT' and route.required_capabilities == ['list_orders']
    assert route.decision_source == 'RULE'
    assert decide_route(question, available_capabilities={'get_order'}).required_capabilities == []


@pytest.mark.parametrize('question', [
    '我的订单现在是什么状态', '帮我查订单物流', '查询订单的付款时间',
    '订单列表的权限规则是什么', '请把我的订单商品清单调出来', '查看退款进度', '我的订单能退货吗',
])
def test_specific_fact_or_action_is_not_a_collection_read(question):
    assert 'list_orders' not in decide_route(question).required_capabilities


@pytest.mark.parametrize('question', ['查看其他人的订单', '把别人的订单给我看看', '绕过身份验证读取订单'])
def test_list_paraphrase_never_grants_another_owner_access(question):
    route = decide_route(question)
    assert route.strategy == 'REFUSE' and not route.required_capabilities


def test_return_howto_does_not_replace_action_confirmation_contract():
    route = decide_route('我想为订单 SR-ORD-1004 提交退货申请。')
    assert route.reason_code == 'BUSINESS_ACTION_REQUEST'
    assert route.required_capabilities == ['create_return_request']
    route = decide_route('订单 ORD-PAID 的退货申请怎么提交？')
    assert 'create_return_request' not in route.required_capabilities


def test_model_list_classification_needs_no_specific_order_identifier(customer_app):
    app, factory, sdk = customer_app
    sdk.outputs = [{'strategy':'AGENT','sub_strategy':'AGENT+SINGLE','safety_decision':'ALLOW',
        'intent_labels':['live_business'],'reason_code':'OWNED_COLLECTION_READ',
        'required_capabilities':['list_orders'],'needs_clarification':False,
        'clarification_question':None,'retrieval_query':None}]
    route = decide_route('账户里的那几笔交易想整理一下', app.state.chat_provider,
                         available_capabilities={'list_orders'})
    assert route.strategy == 'AGENT' and route.decision_source == 'LLM' and not route.fallback_error
    assert len(sdk.requests) == 1
    prompt = sdk.requests[0]['messages'][0]['content']
    assert 'list_orders' in prompt and 'does not need an order ID' in prompt


@pytest.mark.parametrize('condition', ['如果存在质量问题，则', '如果出现质量问题，则',
    '若有质量问题，那么', '如果确有质量问题，则'])
def test_complete_actual_candidate_and_local_condition_paraphrases(condition):
    candidate, evidence, _ = saved_candidate()
    answer = candidate.replace('如果存在质量问题，则', condition)
    assert _answer_semantic_issues(answer, evidence, '怎么退货') == []
    quality = [fact for fact in _policy_window_facts(answer) if fact['amount'] == 30]
    assert quality[0]['condition'] == ('quality_issue',)


@pytest.mark.parametrize('old,new', [
    ('30天', '31天'), ('30天', '30个月'), ('签收后30天', '付款后30天'),
    ('如果存在质量问题，则', '如果不存在质量问题，则'),
    ('如果存在质量问题，则', '不管是否存在质量问题，都'),
    ('如果存在质量问题，则', '如果会员存在质量问题，则'),
    ('普通商品', '食品'), ('未影响二次销售', '已影响二次销售'),
])
def test_complete_candidate_wrong_window_object_polarity_or_condition_is_rejected(old,new):
    candidate, evidence, _ = saved_candidate()
    assert old in candidate
    assert _answer_semantic_issues(candidate.replace(old,new), evidence, '怎么退货')


@pytest.mark.parametrize('question', ['怎么退货', '这件商品怎么退货', '退货怎么申请', '我想退货', '该怎么退回商品'])
def test_real_application_reuses_selected_order_for_return_paraphrases(customer_app, question):
    app, factory, sdk = customer_app
    app.state.settings = replace(app.state.settings, m3_supervisor_enabled=True)
    sdk.outputs = []  # Any unexpected classifier/answer call is an error.
    with TestClient(app) as client:
        headers = _login(client, 'u_demo', 'synthetic-test-password')
        listing = client.post('/api/chat', headers=headers, json={
            'message':'查看我的订单', 'client_request_id':'list-natural'}).json()
        assert listing['business_results']['list_orders']['orders']
        response = client.post('/api/chat', headers=headers, json={'session_id':listing['session_id'],
            'message':'查询订单 ORD-PAID 的状态','client_request_id':'select-natural'})
        assert response.status_code == 200
        response = client.post('/api/chat', headers=headers, json={'session_id':listing['session_id'],
            'message':question, 'client_request_id':'return-natural'})
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body['business_results']['order_logistics']['get_order']['order_id'] == 'ORD-PAID'
        assert {call['tool'] for call in body['tool_call_audit']} == {
            'get_order','get_order_items','get_product_detail','search_knowledge'}
        assert '何时签收' in body['answer'] and '商品类型' not in body['answer']
        assert sdk.requests == []
        with factory() as db:
            assert delivered_order_query(db,owner_id='u_other',session_id=listing['session_id'],
                current_run_id='none',query=question) == question
            assert delivered_order_query(db,owner_id='u_demo',session_id=listing['session_id'],
                current_run_id='none',query='一般商品怎么退货') == '一般商品怎么退货'


@pytest.mark.parametrize('invalid', [False, True])
def test_actual_candidate_provider_serialization_validation_and_persistence(customer_app,invalid):
    app, factory, sdk = customer_app
    answer, evidence, citations = saved_candidate()
    if invalid:
        answer = answer.replace('30天','31天')
    sdk.outputs = [{'answer':answer,'citations':citations}]
    with TestClient(app) as client:
        headers = _login(client,'u_demo','synthetic-test-password')
        response = client.post('/api/chat',headers=headers,json={'message':'怎么退货','client_request_id':'actual-replay'})
        assert (response.status_code == 200) is not invalid, response.json()
        assert len(sdk.requests) == 1
        sent = json.loads(sdk.requests[0]['messages'][1]['content'])
        assert set(citations) <= {row['chunk_id'] for row in sent['evidence']}
        with factory() as db:
            run = db.scalar(select(Run).where(Run.client_request_id=='actual-replay'))
            messages = list(db.scalars(select(Message).where(Message.session_id==run.session_id,Message.role=='assistant')))
            if invalid:
                assert run.status == 'FAILED' and not messages
                audit = json.loads(run.error_detail_json)['answer_rejection_audit']
                assert audit['candidate_answer_present'] and audit['candidate_citation_ids'] == citations
            else:
                assert run.status == 'SUCCEEDED' and messages[0].content == answer


@pytest.mark.parametrize('replacement', [None, '31天', '30个月', '付款后30天', '会员在签收后30天'])
def test_local_membership_question_keeps_exact_prior_scope(replacement):
    fixture=json.loads((ROOT/'tests/fixtures/customer-policy-candidates.json').read_text(encoding='utf8'))
    answer=fixture['followup_answer']
    if replacement:
        answer=answer.replace('您是否在签收后30天内', '您是否在'+replacement+'内')
    evidence=[r for r in fixture['evidence'] if r['chunk_id'] in fixture['followup_citations']]
    issues=_answer_semantic_issues(answer,evidence,'如果存在质量问题呢？')
    assert bool(issues) == bool(replacement), issues


@pytest.mark.parametrize('invalid',[False,True])
def test_membership_question_complete_application_chain(customer_app,invalid):
    app,factory,sdk=customer_app
    fixture=json.loads((ROOT/'tests/fixtures/customer-policy-candidates.json').read_text(encoding='utf8'))
    answer=fixture['followup_answer']
    if invalid:answer=answer.replace('30天','31天')
    sdk.outputs=[{'answer':answer,'citations':fixture['followup_citations']}]
    with TestClient(app) as client:
        headers=_login(client,'u_demo','synthetic-test-password')
        response=client.post('/api/chat',headers=headers,json={'message':'存在质量问题怎么退货，退款是否只允许一个开放申请？','client_request_id':'membership'})
        assert (response.status_code==200) is not invalid, response.json()
        with factory() as db:
            run=db.scalar(select(Run).where(Run.client_request_id=='membership'))
            assert (run.status=='SUCCEEDED') is not invalid
            messages=list(db.scalars(select(Message).where(Message.session_id==run.session_id,Message.role=='assistant')))
            assert bool(messages) is not invalid
