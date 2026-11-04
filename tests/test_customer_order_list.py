"""Owner list and delivered-reference regression through the real API/graph/repository."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.m1.models import Order, Run, Message
from app.m1.routing import decide_route
from app.m3.order_tools import SqlAlchemyOrderReadRepository, ToolExecutionError
from app.orchestration.runtime import delivered_order_query
from test_m3_2_order_logistics import _build, _login


def test_list_selection_real_application_owner_and_pagination(tmp_path, monkeypatch):
    app, factory, engine, saver, password, other = _build(tmp_path, monkeypatch,
        lambda q: 'list_orders' if '有哪些' in q or '下一页' in q else 'get_order')
    try:
        with factory() as db, db.begin():
            for index in range(8):
                db.add(Order(order_id=f'ORD-LIST-{index}', user_id='u_demo', status='PAID',
                    paid_cents=1500, version=1, paid_at=None, delivered_at=None))
            db.add(Order(order_id='ORD-ZZ-PRIVATE', user_id='u_other', status='PAID', paid_cents=999,version=1))
        with TestClient(app) as client:
            headers=_login(client,'u_demo',password)
            page=client.get('/api/orders').json()
            assert len(page['orders'])==5 and page['has_more']
            assert all(r['order_id']!='ORD-ZZ-PRIVATE' for r in page['orders'])
            following=client.get('/api/orders',params={'before':page['next_before']}).json()
            assert not set(r['order_id'] for r in page['orders']) & set(r['order_id'] for r in following['orders'])
            assert client.get('/api/orders',params={'before':'bad cursor'}).status_code==400
            first=client.post('/api/chat',headers=headers,json={'message':'我的订单有哪些','client_request_id':'list-one'}).json()
            assert first['business_results']['list_orders']['orders']==page['orders']
            sid=first['session_id']; selected=page['orders'][0]['order_id']
            with factory() as db:
                assert delivered_order_query(db,owner_id='u_demo',session_id=sid,current_run_id='none',query='这笔')=='这笔'
                assert delivered_order_query(db,owner_id='u_demo',session_id='another-session',current_run_id='none',query='第一个')=='第一个'
            next_page=client.post('/api/chat',headers=headers,json={'session_id':sid,'message':'列出我的订单下一页','client_request_id':'page-next'}).json()
            assert next_page['business_results']['list_orders']['orders']==following['orders']
            selected=following['orders'][0]['order_id']
            second=client.post('/api/chat',headers=headers,json={'session_id':sid,'message':'第一个','client_request_id':'select-one'}).json()
            assert second['business_results']['get_order']['order_id']==selected
            assert second['business_results']['get_order']['delivered_at'] is None
            assert '未发货' not in second['answer'] and '未送达' not in second['answer']
            with factory() as db:
                events=client.get(f"/api/runs/{first['run_id']}/events").json()['events']
                assert [e['type'] for e in events].count('tool_started')==1
                assert [e['type'] for e in events].count('tool_finished')==1
                assert delivered_order_query(db,owner_id='u_other',session_id=sid,current_run_id='none',query='这笔能退吗')=='这笔能退吗'
                # A failed/rejected candidate cannot become a delivered resource hint.
                runs=list(db.scalars(select(Run).where(Run.session_id==sid)))
                for run in runs:run.status='FAILED'
                db.commit()
                assert delivered_order_query(db,owner_id='u_demo',session_id=sid,current_run_id='none',query='第一个')=='第一个'
    finally: engine.dispose()


def test_empty_and_dependency_failure_are_distinct(tmp_path,monkeypatch):
    app,factory,engine,*_=_build(tmp_path,monkeypatch)
    try:
        repo=SqlAlchemyOrderReadRepository(factory)
        assert repo.list_orders('', 'no-orders')['orders']==[]
        from sqlalchemy.exc import OperationalError
        def broken():raise OperationalError('offline',{},Exception())
        with pytest.raises(OperationalError):SqlAlchemyOrderReadRepository(broken).list_orders('', 'u_demo')
        with pytest.raises(ToolExecutionError):repo.list_orders('invalid','u_demo')
    finally:engine.dispose()


def test_natural_routes_list_and_owned_item_return():
    assert decide_route('我的订单有哪些').required_capabilities==['list_orders']
    route=decide_route('这件商品能退吗？ 当前问题关联的本人订单：ORD-LIST-1。')
    assert route.required_capabilities==['get_order','get_order_items','get_product_detail','search_knowledge']
    assert decide_route('我的订单有哪些',available_capabilities={'get_order'}).required_capabilities==[]


from test_customer_experience import customer_app

def test_delivered_order_to_return_uses_real_items_and_serialized_sources(customer_app):
    from dataclasses import replace
    from app.m1.api import CSRF_HEADER, CSRF_COOKIE
    app,factory,sdk=customer_app
    app.state.settings=replace(app.state.settings,m3_supervisor_enabled=True)
    sdk.outputs=[{'answer':'我已核对订单商品。申请退货仍需核实签收时间和退货原因；是否批准需人工审核。请问何时签收？',
        'citations':['policies/return-standard@2026-07#no-reason']}]
    with TestClient(app) as client:
        headers=_login(client,'u_demo','synthetic-test-password')
        first=client.post('/api/chat',headers=headers,json={'message':'查询本人订单 ORD-PAID 的状态','client_request_id':'fact-first'})
        assert first.status_code==200,first.json()
        second=client.post('/api/chat',headers=headers,json={'session_id':first.json()['session_id'],
            'message':'这件商品能退吗？','client_request_id':'return-followup'})
        assert second.status_code==200,second.json()
        body=second.json()
        assert body['strategy']=='AGENT'
        events=client.get(f"/api/runs/{body['run_id']}/events").json()['events']
        tools=[e['payload']['tool'] for e in events if e['type']=='tool_started']
        assert 'get_order' in tools and 'get_order_items' in tools and 'get_product_detail' in tools
        assert len(sdk.requests)==0  # Audited business composition is rule driven.
        fourth=client.post('/api/chat',headers=headers,json={'session_id':first.json()['session_id'],
            'message':'普通商品无理由退货有哪些条件？','client_request_id':'policy-explain'})
        assert fourth.status_code==200,fourth.json()
        assert len(sdk.requests)==1  # The normal policy path serializes delivered context.
        serialized=json.dumps(sdk.requests[0]['messages'],ensure_ascii=False)
        assert 'evidence' in serialized and '普通商品无理由退货有哪些条件' in serialized
        assert body['business_results']['order_logistics']['get_order']['order_id']=='ORD-PAID'
        assert '签收' in body['answer'] and '未送达' not in body['answer']
        assert '请问' in body['answer'] and '商品类型' not in body['answer']


def test_list_database_failure_is_persisted_not_empty_success(tmp_path,monkeypatch):
    from sqlalchemy.exc import OperationalError
    app,factory,engine,saver,password,_=_build(tmp_path,monkeypatch,lambda q:'list_orders')
    def fail(self,before,owner):raise OperationalError('offline',{},Exception())
    monkeypatch.setattr(SqlAlchemyOrderReadRepository,'list_orders',fail)
    try:
        with TestClient(app) as client:
            assert client.get('/api/orders').status_code==401
            headers=_login(client,'u_demo',password)
            response=client.post('/api/chat',headers=headers,json={'message':'我的订单有哪些','client_request_id':'database-failure'})
            assert response.status_code!=200
            with factory() as db:
                run=db.scalar(select(Run).where(Run.client_request_id=='database-failure'))
                assert run.status=='FAILED'
                events=client.get(f'/api/runs/{run.run_id}/events').json()['events']
                finished=[e['payload'] for e in events if e['type']=='tool_finished']
                assert len(finished)==1 and finished[0]['status']=='FAILED'
                assert finished[0]['error_code']=='TOOL_DATABASE_UNAVAILABLE'
                assert not any(e['type']=='answer' for e in events)
    finally:engine.dispose()
