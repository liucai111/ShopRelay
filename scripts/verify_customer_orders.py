"""Controller-only synthetic fixture and closeout, run in the isolated delivery container.

No model client, production fallback or broad owner cleanup. The browser and
existing Agent executor remain the only real acceptance execution path.
"""
import hashlib
import json
import secrets
import os
import re
import sys
from pathlib import Path

from sqlalchemy import delete, func, select, text

from app.m1.db import make_engine, make_session_factory
from app.m1.models import (AuthSession, ConversationSession, DemoCredential, Order,
                          OrderItem, Product, Run, User)
from app.m1.services import set_demo_password
from scripts.m65_local_delivery import SCHEMA, database_url

DIRECTORY = Path('/delivery-ledger/orders-task')
RECEIPT = Path('/delivery-ledger/orders-task-fixture.json')
OWNER = 'acceptance_orders_r1'
OTHER = 'acceptance_orders_other_r1'
BATCH = 'customer-orders-r1-20261101'
suffix = os.environ.get('SHOPRELAY_ORDER_ACCEPTANCE_SUFFIX')
if suffix:
    if not re.fullmatch(r'[a-z][a-z0-9-]{3,30}', suffix):
        raise ValueError('INVALID_SYNTHETIC_ACCEPTANCE_SUFFIX')
    DIRECTORY = Path('/delivery-ledger/orders-task-' + suffix)
    RECEIPT = Path('/delivery-ledger/orders-task-fixture-' + suffix + '.json')
    OWNER = 'acceptance_orders_' + suffix.replace('-', '_')
    OTHER = OWNER + '_other'
    BATCH = 'customer-orders-' + suffix


def baseline(db):
    # Existing sessions are preserved by IDs/counts, without reading their text.
    return {row.session_id: {
        'owner': row.user_id,
        'runs': db.scalar(select(func.count()).select_from(Run).where(Run.session_id == row.session_id)),
        'messages': db.execute(text('SELECT count(*) FROM messages WHERE session_id=:s'),
                               {'s': row.session_id}).scalar_one(),
    } for row in db.scalars(select(ConversationSession).where(ConversationSession.user_id.notin_([OWNER, OTHER])))}


def prepare(factory):
    prior = None
    if RECEIPT.exists():
        prior = json.loads(RECEIPT.read_text())
        with factory() as db:
            assert prior['state'] == 'PREPARED'
            assert db.get(Product, 'P-ACCEPT-CUP') is None
            assert not db.scalar(select(func.count()).select_from(Order).where(
                Order.order_id.in_(prior['orders'] + prior['other_orders'])))
            assert not db.scalar(select(func.count()).select_from(Run).where(Run.user_id.in_([OWNER, OTHER])))
            assert not db.scalar(select(func.count()).select_from(ConversationSession).where(ConversationSession.user_id.in_([OWNER, OTHER])))
            actual_users = [uid for uid in (OWNER, OTHER) if db.get(User, uid) is not None]
            assert actual_users in ([], [OWNER, OTHER])
        (DIRECTORY/'preparation-reconciliation.json').write_text(json.dumps({
            'prior_receipt': prior, 'created_users': actual_users, 'orders': 0,
            'products': 0, 'Run': 0, 'Provider': 0,
            'cause': 'credential service committed users before enclosing transaction closed',
            'recovery': 'finish the exact receipted fixture; do not recreate users or consume a trial'}))
    DIRECTORY.mkdir(exist_ok=True)
    password = (DIRECTORY/'password').read_text() if prior else secrets.token_urlsafe(24)
    receipt = {'batch': BATCH, 'owner': OWNER, 'other': OTHER, 'state': 'PREPARED',
        'orders': [f'ORD-ACCEPT-{index}' for index in range(101, 107)],
        'other_orders': ['ORD-OTHER-ACCEPT-901'], 'products': ['P-ACCEPT-CUP'],
        'database_marker': 'ca3b0b3d027448a09cc58caa49139862', 'schema': SCHEMA}
    with factory() as db, db.begin():
        assert db.execute(text('SELECT current_schema()')).scalar_one() == SCHEMA
        assert db.execute(text('SELECT identity FROM public.d1_personal_target')).scalar_one() == receipt['database_marker']
        assert not db.scalar(select(func.count()).select_from(Run).where(Run.status.in_(['CREATED', 'RUNNING'])))
        assert db.execute(text('SELECT count(*) FROM controlled_pending_actions')).scalar_one() == 0
        if prior is None:
            assert db.get(User, OWNER) is None and db.get(User, OTHER) is None
        receipt['preserved_sessions'] = baseline(db)
        receipt['existing_users'] = prior['existing_users'] if prior else list(db.scalars(select(User.user_id).order_by(User.user_id)))
        receipt['existing_orders'] = prior['existing_orders'] if prior else list(db.scalars(select(Order.order_id).order_by(Order.order_id)))
        RECEIPT.write_text(json.dumps(receipt), encoding='utf8')
        (DIRECTORY/'password').write_text(password, encoding='utf8')
        (DIRECTORY/'password').chmod(0o600)
        if db.get(User, OWNER) is None:
            db.add_all([User(user_id=OWNER, display_name='合成订单验收'), User(user_id=OTHER, display_name='合成他人边界')])
        db.flush()
        db.add(Product(product_id='P-ACCEPT-CUP', name='合成日用水杯', category='general', price_cents=3200))
        db.flush()
        for oid in receipt['orders'] + receipt['other_orders']:
            db.add(Order(order_id=oid, user_id=OTHER if oid in receipt['other_orders'] else OWNER,
                status='PAID', paid_at=None, delivered_at=None, paid_cents=3200, version=1))
        db.flush()
        for oid in receipt['orders'] + receipt['other_orders']:
            db.add(OrderItem(order_id=oid, product_id='P-ACCEPT-CUP', quantity=1, paid_cents=3200))
    # Existing credential service commits its own transaction.
    with factory() as db:
        set_demo_password(db, OWNER, password)
    receipt['state'] = 'CREATED'
    RECEIPT.write_text(json.dumps(receipt), encoding='utf8')
    return {'fixture_created': True, 'users': 2, 'orders': 7, 'products': 1,
            'preserved_sessions': len(receipt['preserved_sessions']), 'Provider': 0}


def close_account():
    from eval.m6_1.budget import BudgetLedger, BudgetLimits
    from eval.m6_1.store import ResultStore
    path = Path('/delivery-ledger/acceptance-account.json')
    account = json.loads(path.read_text())
    assert account['batch_id'] == BATCH and account['status'] == 'OPEN'
    ledger = BudgetLedger(ResultStore(Path('/delivery-ledger/requests.sqlite')), 'delivery-' + BATCH,
        BudgetLimits(provider_calls=20, total_tokens=400000, estimated_usd=1,
                     max_concurrent_trials=1, max_duration_seconds=120, max_retries=0))
    snapshot = ledger.snapshot()
    assert snapshot['active_reservations'] == 0 and snapshot['token_status'] != 'UNKNOWN_USAGE'
    account['status'] = 'CLOSED'
    path.write_text(json.dumps(account))
    value = {'account': account, 'snapshot': snapshot}
    target = DIRECTORY/'closed-account.json'
    assert not target.exists()
    target.write_text(json.dumps(value))
    return value


def cleanup(factory):
    receipt = json.loads(RECEIPT.read_text())
    assert receipt['state'] == 'CREATED'
    with factory() as db, db.begin():
        assert baseline(db) == receipt['preserved_sessions']
        assert not db.scalar(select(func.count()).select_from(Run).where(Run.user_id.in_([OWNER, OTHER])))
        assert not db.scalar(select(func.count()).select_from(ConversationSession).where(ConversationSession.user_id.in_([OWNER, OTHER])))
        order_ids = receipt['orders'] + receipt['other_orders']
        assert set(db.scalars(select(Order.order_id).where(Order.user_id.in_([OWNER, OTHER])))) == set(order_ids)
        db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        db.execute(delete(Order).where(Order.order_id.in_(order_ids)))
        db.execute(delete(Product).where(Product.product_id.in_(receipt['products'])))
        db.execute(delete(AuthSession).where(AuthSession.user_id.in_([OWNER, OTHER])))
        db.execute(delete(DemoCredential).where(DemoCredential.user_id.in_([OWNER, OTHER])))
        db.execute(delete(User).where(User.user_id.in_([OWNER, OTHER])))
        assert list(db.scalars(select(User.user_id).order_by(User.user_id))) == receipt['existing_users']
        assert list(db.scalars(select(Order.order_id).order_by(Order.order_id))) == receipt['existing_orders']
        assert baseline(db) == receipt['preserved_sessions']
    receipt['state'] = 'CLEANED'
    RECEIPT.write_text(json.dumps(receipt))
    (DIRECTORY/'password').unlink()
    return {'exact_fixture_cleanup': True, 'original_sessions_preserved': True,
            'original_users_and_orders_preserved': True, 'new_owner_residual': 0}


if __name__ == '__main__':
    engine = make_engine(database_url())
    factory = make_session_factory(engine)
    try:
        mode = sys.argv[1]
        output = prepare(factory) if mode == 'prepare' else close_account() if mode == 'close' else cleanup(factory) if mode == 'cleanup' else None
        if output is None:
            raise ValueError('UNKNOWN_OPERATION')
        print(json.dumps(output, ensure_ascii=True))
    finally:
        engine.dispose()
