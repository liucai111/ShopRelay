"""Export then precisely remove M6.5 HTTP/browser engineering records.

Retains the named demo users/catalog/order and schemas for subsequent use.
Never truncates, drops schemas or touches public evaluation history.
"""
import json
from sqlalchemy import delete,func,or_,select,text
from scripts.m65_local_delivery import database_url,SCHEMA,CHECKPOINT
from app.m1.db import make_engine,make_session_factory
from app.m1.models import Base,Run,ConversationSession,AuthSession,RunEvent,Trace
from scripts.probe_m5_4_a_browser_server import _checkpoint_delete


def cleanup():
    engine=make_engine(database_url());factory=make_session_factory(engine)
    owners=('m65_owner','m65_other')
    with factory() as db:
        assert db.execute(text('SELECT current_schema()')).scalar_one()==SCHEMA
        runs=list(db.scalars(select(Run).where(Run.user_id.in_(owners))))
        assert all(r.status in {'SUCCEEDED','FAILED','MODEL_UNAVAILABLE','CANCELLED'} for r in runs)
        run_ids=[r.run_id for r in runs]
        sessions=list(db.scalars(select(ConversationSession.session_id).where(ConversationSession.user_id.in_(owners))))
        archive=[]
        for run in runs:
            archive.append({'run_id':run.run_id,'session_id':run.session_id,'status':run.status,
                'client_request_id':run.client_request_id,'result':json.loads(run.result_json) if run.result_json else None,
                'error_code':run.error_code,'error_detail':json.loads(run.error_detail_json) if run.error_detail_json else None,
                'events':[{'seq':r.seq,'event_type':r.event_type,'payload':json.loads(r.payload_json)}
                    for r in db.scalars(select(RunEvent).where(RunEvent.run_id==run.run_id).order_by(RunEvent.seq))]})
    for run_id in run_ids:_checkpoint_delete(database_url(),CHECKPOINT,run_id)
    removed={}
    keep={'users','demo_credentials','orders','order_items','products','inventory'}
    with factory() as db,db.begin():
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in keep:continue
            scope=[]
            if 'run_id' in table.c and run_ids:scope.append(table.c.run_id.in_(run_ids))
            if 'session_id' in table.c and sessions:scope.append(table.c.session_id.in_(sessions))
            if table.name=='auth_sessions':scope.append(table.c.user_id.in_(owners))
            if not scope:continue
            result=db.execute(delete(table).where(or_(*scope)))
            if result.rowcount:removed[table.name]=result.rowcount
    with engine.connect() as db:
        residual={name:db.execute(text('SELECT count(*) FROM '+name)).scalar_one()
            for name in ('runs','sessions','messages','run_events','traces','observability_records',
                'controlled_pending_actions','auth_sessions')}
        public={name:db.execute(text('SELECT count(*) FROM public.'+name)).scalar_one()
            for name in ('users','runs','controlled_pending_actions')}
        checkpoints={name:db.execute(text('SELECT count(*) FROM '+CHECKPOINT+'.'+name)).scalar_one()
            for name in ('checkpoints','checkpoint_blobs','checkpoint_writes')}
        assert not any(residual.values()) and not any(checkpoints.values()) and not any(public.values())
        assert db.execute(text('SELECT count(*) FROM orders')).scalar_one()==1
        assert db.execute(text('SELECT count(*) FROM users')).scalar_one()==2
    print(json.dumps({'engineering_archive':archive,'receipt':{'run_ids':run_ids,'session_ids':sessions,
        'deleted_rows':removed,'residual':residual,'checkpoints':checkpoints,'public_evaluation_tables_unchanged_empty':public,
        'retained_fictional_baseline':{'users':2,'orders':1,'products':1,'schemas_retained':True}},
        'Provider':0,'Judge':0,'Embedding':0},ensure_ascii=False))
    engine.dispose()


def cleanup_exact_receipt(path, *, output_directory=None):
    """Remove only named synthetic chat records, retaining every other session.

    Login cookies are not selected by user_id: those may belong to the user.
    The original M6.5 broad acceptance cleanup above is historical only.
    """
    from pathlib import Path
    directory=Path(output_directory or '/delivery-ledger').resolve()
    if not directory.is_relative_to(Path('/delivery-ledger').resolve()):
        raise ValueError('CLEANUP_EVIDENCE_DIRECTORY_OUTSIDE_LEDGER')
    destination=directory/'exact-cleanup-archive.json'
    if destination.exists():
        raise ValueError('CLEANUP_ARCHIVE_ALREADY_EXISTS')
    receipt=json.loads(Path(path).read_text())
    sessions=receipt['session_ids']
    owner = receipt.get('synthetic_owner', 'm65_owner')
    if owner != 'm65_owner':
        # A browser-supplied owner is not sufficient. Bind it to the trusted
        # controller's newly-created fixture receipt on this isolated target.
        fixture_path = Path(receipt.get('fixture_receipt', '/delivery-ledger/orders-task-fixture.json'))
        if (fixture_path.parent != Path('/delivery-ledger') or
                not fixture_path.name.startswith('orders-task-fixture') or fixture_path.suffix != '.json'):
            raise ValueError('INVALID_SYNTHETIC_FIXTURE_RECEIPT')
        fixture = json.loads(fixture_path.read_text())
        if owner != fixture['owner'] or not owner.startswith('acceptance_orders_'):
            raise ValueError('SYNTHETIC_OWNER_RECEIPT_MISMATCH')
    if (not sessions or len(sessions)!=len(set(sessions)) or
            '71b78efe-6eb9-4659-b92e-e4c19a489f4f' in sessions):
        raise ValueError('EXACT_SYNTHETIC_SESSION_RECEIPT_REQUIRED')
    engine=make_engine(database_url());factory=make_session_factory(engine)
    protected='71b78efe-6eb9-4659-b92e-e4c19a489f4f'
    def kept(db):
        return {table:db.execute(text('SELECT count(*) FROM '+table+' WHERE session_id=:s'),
            {'s':protected}).scalar_one() for table in ('sessions','runs','messages')}
    with factory() as db:
        assert db.execute(text('SELECT current_schema()')).scalar_one()==SCHEMA
        before=kept(db)
        rows=list(db.scalars(select(ConversationSession).where(ConversationSession.session_id.in_(sessions))))
        assert len(rows)==len(sessions) and all(r.user_id==owner for r in rows)
        runs=list(db.scalars(select(Run).where(Run.session_id.in_(sessions))))
        run_ids=[r.run_id for r in runs]
        assert set(run_ids)==set(receipt['run_ids'])
        assert all(r.status in {'SUCCEEDED','FAILED','MODEL_UNAVAILABLE','CANCELLED'} for r in runs)
        archive=[{'run_id':r.run_id,'session_id':r.session_id,'status':r.status,
            'result':json.loads(r.result_json) if r.result_json else None,
            'error_code':r.error_code,'error_detail':json.loads(r.error_detail_json) if r.error_detail_json else None,
            'events':[{'seq':e.seq,'type':e.event_type,'payload':json.loads(e.payload_json)}
                for e in db.scalars(select(RunEvent).where(RunEvent.run_id==r.run_id).order_by(RunEvent.seq))]}
            for r in runs]
    # Persist the archive before any deletion, including checkpoint cleanup.
    directory.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps({'runs':archive,'receipt':receipt},ensure_ascii=False),encoding='utf8')
    for rid in run_ids:_checkpoint_delete(database_url(),CHECKPOINT,rid)
    removed={}
    with factory() as db,db.begin():
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in {'users','demo_credentials','orders','order_items','products','inventory','auth_sessions'}:continue
            scopes=[]
            if 'run_id' in table.c and run_ids:scopes.append(table.c.run_id.in_(run_ids))
            if 'session_id' in table.c:scopes.append(table.c.session_id.in_(sessions))
            if scopes:
                count=db.execute(delete(table).where(or_(*scopes))).rowcount
                if count:removed[table.name]=count
    with factory() as db:
        after=kept(db)
        assert before==after and before['sessions']==1
        remaining_runs=db.scalar(select(func.count()).select_from(Run).where(Run.run_id.in_(run_ids)))
        remaining_sessions=db.scalar(select(func.count()).select_from(ConversationSession).where(ConversationSession.session_id.in_(sessions)))
        remaining_checkpoints={name:db.execute(text('SELECT count(*) FROM '+CHECKPOINT+'.'+name+
            ' WHERE thread_id = ANY(:ids)'),{'ids':run_ids}).scalar_one()
            for name in ('checkpoints','checkpoint_blobs','checkpoint_writes')}
        assert not remaining_runs and not remaining_sessions and not any(remaining_checkpoints.values())
    proof={'removed':removed,'run_ids':run_ids,'session_ids':sessions,'remaining_runs':remaining_runs,
        'remaining_sessions':remaining_sessions,'remaining_checkpoints':remaining_checkpoints,
        'protected_session_before':before,'protected_session_after':after,
        'other_user_sessions_or_credentials_selected':False}
    (directory/'exact-cleanup-proof.json').write_text(json.dumps(proof,indent=2))
    print(json.dumps(proof))
    engine.dispose()


if __name__=='__main__':
    import sys
    if len(sys.argv)==3 and sys.argv[1]=='--receipt':cleanup_exact_receipt(sys.argv[2])
    else:raise SystemExit('An exact synthetic-session receipt is required; broad owner cleanup is disabled.')
