"""Local delivery profiles over the real API and isolated fictional PG.

This module supplies deployment configuration, not another Runner. Bootstrap is
explicit. Normal startup never migrates, seeds, issues permits or invokes a model.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/delivery-state')
SCHEMA = 'm65_delivery'
CHECKPOINT = 'm65_delivery_checkpoint'


def database_url() -> str:
    password = Path('/run/db-secret/password').read_text().strip()
    return URL.create('postgresql+psycopg', username='evalapp', password=password,
        host='m62-eval-postgres', port=5432, database='shoprelay_m11_test',
        query={'options': '-csearch_path=' + SCHEMA}).render_as_string(hide_password=False)


def bootstrap() -> dict:
    from app.m1.db import make_engine, upgrade_database, make_session_factory
    from app.m1.models import User, Product, Order, OrderItem
    from app.m1.services import set_demo_password
    STATE.mkdir(exist_ok=True)
    engine = make_engine(database_url())
    with engine.begin() as db:
        assert db.execute(text('SELECT current_database()')).scalar_one() == 'shoprelay_m11_test'
        marker = db.execute(text('SELECT identity FROM public.d1_personal_target')).scalar_one()
        if marker != 'ca3b0b3d027448a09cc58caa49139862':
            raise RuntimeError('ISOLATED_PARENT_TARGET_CHANGED')
        db.execute(text('CREATE SCHEMA IF NOT EXISTS ' + SCHEMA))
        db.execute(text('CREATE SCHEMA IF NOT EXISTS ' + CHECKPOINT))
        exists = inspect(db).get_table_names(schema=SCHEMA)
        if not exists:
            # The original M0 DDL is portable after its SQLite-only PRAGMA.
            # Actual Alembic upgrades follow; no fake head stamping.
            for statement in (ROOT/'migrations/0001_m0.sql').read_text().split(';'):
                if statement.strip() and not statement.strip().startswith('PRAGMA'):
                    db.execute(text(statement))
    upgrade_database(database_url())
    from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async
    async def setup():
        async with open_checkpointer(database_url(), CHECKPOINT) as saver:
            await saver.setup()
    run_checkpointer_async(setup())
    factory = make_session_factory(engine)
    credential = STATE/'demo-password'
    if not credential.exists():
        credential.write_text(secrets.token_urlsafe(24))
        credential.chmod(0o600)
    with factory() as db:
        if db.get(User, 'm65_owner') is None:
            db.add(User(user_id='m65_owner', display_name='M6.5 fictional owner'))
            db.add(User(user_id='m65_other', display_name='M6.5 fictional other'))
            db.add(Product(product_id='M65-CUP', name='M6.5虚构保温杯', category='cup', price_cents=2500))
            db.flush()
            db.add(Order(order_id='ORD-M65-901', user_id='m65_owner', status='PAID', paid_at=None,
                delivered_at=None, paid_cents=2500, version=1))
            db.flush()
            db.add(OrderItem(order_id='ORD-M65-901', product_id='M65-CUP', quantity=1, paid_cents=2500))
            db.flush()
            for uid in ('m65_owner','m65_other'):
                set_demo_password(db, uid, credential.read_text())
            db.commit()
    index={'knowledge_version':'m65-fictional-engineering-v1','source_sha256':'0'*64,'chunks':[
        {'chunk_id':'m65-policy-window','policy_id':'m65-channel','version':'engineering-v1',
            'title':'虚构商业渠道申请期限','clause_id':'window','effective_from':'2026-01-01T00:00:00Z',
            'effective_to':'2028-01-01T00:00:00Z',
            'content':'独立渠道条款以登记事件起算43天，仅适用于商业使用且具有有效服务凭证的商品。'}]}
    from app.m0.data import digest
    index['source_sha256']=digest(index['chunks'])
    (STATE/'index.json').write_text(json.dumps(index,ensure_ascii=False),encoding='utf8')
    receipt={'profile':'M65_DETERMINISTIC_LOCAL_DELIVERY','parent_identity':marker,'schema':SCHEMA,
        'checkpoint_schema':CHECKPOINT,'retained_fixture':{'users':['m65_owner','m65_other'],
            'orders':['ORD-M65-901'],'products':['M65-CUP']},'Provider':0,'production_writes':False}
    (STATE/'fixture-receipt.json').write_text(json.dumps(receipt,indent=2))
    engine.dispose()
    return receipt


def delivery_services(provider, retrieval_service, index_path):
    """Use the shared source-bound answer contract, keeping normal supervision."""
    from app.orchestration.adapters import make_legacy_services
    from app.m3.single_agent import bind_agent_controls
    services=make_legacy_services(provider=provider,classifier=provider,
        retrieval_service=retrieval_service,index_path=index_path,
        evaluation_at='2026-11-01T00:00:00Z',require_model_route_success=True)
    # MULTI binds only the composer. Per-Run repositories, owner and tools are
    # still supplied by the actual runtime, not a deployment-side replacement.
    bind_agent_controls(services,provider,{},'MULTI',[])
    return services


def application():
    real = os.getenv('SHOPRELAY_DELIVERY_MODE') == 'real'
    os.environ.update(DATABASE_URL=database_url(),SHOPRELAY_ORCHESTRATOR='langgraph',
        SHOPRELAY_M3_SUPERVISOR_ENABLED='true',SHOPRELAY_LANGGRAPH_CHECKPOINT_SCHEMA=CHECKPOINT,
        SHOPRELAY_ALLOWED_ORIGINS='http://127.0.0.1:18065,http://127.0.0.1:18066',
        OTEL_SDK_DISABLED='true',LANGSMITH_TRACING='false')
    if not real:
        os.environ['DEEPSEEK_API_KEY']=''
    # This image deliberately has neither .env nor Provider credential mounts.
    from app.m1.api import create_app
    from scripts.probe_m5_4_a_browser_server import DeterministicEvidenceStub
    class DeliveryEvidenceStub(DeterministicEvidenceStub):
        def answer_for_policy_aspects(self,*args,**kwargs):
            value=super().answer_for_policy_aspects(*args,**kwargs)
            value['answer']='本轮虚构条款说明：'+value['answer']
            if os.getenv('M65_ENGINEERING_INVALID_ANSWER')=='true':
                import re
                value['answer']=re.sub(r'(\d+)天',r'\1个月',value['answer'],count=1)
            return value
    if real:
        from app.m0.config import load_settings, create_deepseek_client
        from app.m1.chat import DeepSeekProvider
        from app.m1.delivery_provider import MeteredChatClient
        from urllib.parse import urlparse
        os.environ['DEEPSEEK_API_KEY']=Path('/run/provider-secret/api-key').read_text().strip()
        settings=load_settings(require_api_key=True)
        if urlparse(settings.base_url).hostname != 'api.deepseek.com' or settings.model != 'deepseek-flash':
            raise RuntimeError('DELIVERY_PROVIDER_BINDING_MISMATCH')
        index_path=ROOT/'data/delivery-index.json'
        index=json.loads(index_path.read_text())
        binding={'model':settings.model,'endpoint_host':'api.deepseek.com',
            'knowledge_version':index['knowledge_version'],
            'index_sha256':__import__('hashlib').sha256(index_path.read_bytes()).hexdigest(),
            'price_source':'https://api-docs.deepseek.com/quick_start/pricing/'}
        client=MeteredChatClient(create_deepseek_client(settings,settings.nodes['policy_answer']),
            Path('/delivery-ledger'),binding)
        provider=DeepSeekProvider(settings,client=client)
    else:
        index_path=STATE/'index.json'
        provider=DeliveryEvidenceStub()
    app=create_app(database_url=database_url(),chat_provider=provider,
        index_path=index_path,retrieval_evaluation_at='2026-11-01T00:00:00Z',
        orchestrator_mode='langgraph',observability_admin_user_ids=['m65_owner'])
    if real:
        from app.m1.delivery_provider import install_acceptance_accounting
        install_acceptance_accounting(app, Path('/delivery-ledger'))
        app.state.graph_services=delivery_services(provider,app.state.retrieval_service,index_path)
        # Never replay a pre-existing user's transcript into a newly enabled
        # external provider. Old sessions remain readable, but require a new chat.
        from fastapi.responses import JSONResponse
        from app.m1.models import ConversationSession
        from datetime import datetime, timezone
        cutoff=datetime.fromisoformat((Path('/delivery-ledger')/'enabled-at').read_text().strip())
        @app.middleware('http')
        async def new_sessions_only(request,call_next):
            if request.method=='POST' and request.url.path in {'/api/chat','/api/chat/stream'}:
                try:
                    payload=await request.json()
                except (ValueError,UnicodeDecodeError):
                    return await call_next(request)
                sid=payload.get('session_id') if isinstance(payload,dict) else None
                if sid:
                    with app.state.session_factory() as db:
                        session=db.get(ConversationSession,sid)
                        if session:
                            created=session.created_at
                            if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
                            if created < cutoff:
                                return JSONResponse(status_code=409,content={'code':'NEW_MODEL_SESSION_REQUIRED',
                                    'message':'旧会话已保留。请新建会话后使用真实模型，旧消息不会自动发送。'})
            return await call_next(request)
    @app.get('/api/runtime',include_in_schema=False)
    def runtime_info():
        budget = provider.client.manual_budget_status() if real else None
        budget_notice = (f" 本地演示剩余{budget['remaining_provider_requests']}次模型请求（刷新时统计）。"
                        if budget and budget['status'] in {'AVAILABLE', 'EXHAUSTED'} else
                        ' 演示用量待对账。' if budget else '')
        return {'mode':'REAL_MODEL' if real else 'OFFLINE_ACCEPTANCE',
            'manual_budget': budget,
            'provider':'DeepSeek' if real else None,'model':'deepseek-flash' if real else None,
            'endpoint_host':'api.deepseek.com' if real else None,
            'notice':('合成数据演示 · 发送的新会话消息及相关政策将交由 DeepSeek 处理。请勿输入真实个人或支付信息。'+budget_notice) if real
                else '离线验收模式 · 回答来自测试替身，不代表真实模型。',
            'suggestions':['查询我的订单 ORD-M65-901 的记录状态','看看有哪些杯子','商品要退货，怎么退','退货申请和退款有什么区别？']}
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    dist=ROOT/'frontend/dist'
    app.mount('/assets',StaticFiles(directory=dist/'assets'),name='web-assets')
    @app.get('/',include_in_schema=False)
    @app.get('/chat',include_in_schema=False)
    @app.get('/chat/{session_id}',include_in_schema=False)
    @app.get('/admin/observability',include_in_schema=False)
    def web():return FileResponse(dist/'index.html')
    return app


if __name__=='__main__':
    if sys.argv[1:]==['bootstrap']:print(json.dumps(bootstrap()))
    else:raise SystemExit('supported operation: bootstrap; serve with uvicorn --factory')
