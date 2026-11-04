"""Trusted image entry: explicit isolated PG only; no production fallback."""
from pathlib import Path
import json,os

def configure_execution(root:Path,mode:dict):
    """Engineering,prepared-offline,and signed formal activation are distinct.

    This routine never issues a permit or creates a Run. A prepared container
    can expose only identity/health until a separately signed permit is active.
    """
    if set(mode)!={'engineering_only','real_provider_allowed'} or any(type(v) is not bool for v in mode.values()):
        raise PermissionError('invalid trusted execution mode')
    if mode['engineering_only']:
        if mode['real_provider_allowed']:raise PermissionError('engineering mode cannot use a real provider')
        os.environ['DEEPSEEK_API_KEY']='ENGINEERING_SUBSTITUTE_NOT_A_PROVIDER_CREDENTIAL'
        return 'ENGINEERING'
    trust=root/'data/eval/formal-trust';config=json.loads((trust/'deployment.json').read_text())
    if config['engineering_only'] is not False or config['issuer']=='R26_ENGINEERING_ONLY':
        raise PermissionError('engineering trust cannot be renamed formal')
    if not mode['real_provider_allowed']:return 'PREPARED_NO_EXECUTION'
    from .container_boundary import read_grant
    from .grants import FormalGrantAuthority,formal_runtime_digest
    from .registry import DatasetRegistry
    from .store import ResultStore
    from .governance import load_governance,verify_isolation_evidence
    from .contracts import canonical_json,sha256_json
    from dataclasses import asdict
    from datetime import datetime,timezone
    import base64
    load_governance(root,config['governance_binding'])
    verify_isolation_evidence(root,config['isolation_evidence_binding'],formal_runtime_digest())
    grant=read_grant(json.loads((root/config['grant_file']).read_text()))
    if grant.plan.mode not in {'DEV_SMOKE','FROZEN_EVAL','PERSONAL_REAL_EVAL'}:raise PermissionError('a real exact mode permit is required')
    store=ResultStore(root/grant.plan.ledger_path)
    authority=FormalGrantAuthority(DatasetRegistry.load_metadata(trust/'registry.json'),repository_root=root,store=store,
        issuer=config['issuer'],trusted_issuers=set(config['trusted_issuers']),signing_key=None,
        verification_key=base64.b64decode(config['verification_public_key_base64']),
        approved_plan_hashes=set(config['approved_plan_hashes']),allowed_targets={tuple(r) for r in config['allowed_targets']})
    bindings={'subject':config['subject'],'mode':grant.plan.mode,'config_sha256':sha256_json(config['experiment_config']),
        'target':tuple(config['actual_target_binding'])}
    authority._verify_bindings(grant,**bindings)
    with store.transaction() as db:
        row=db.execute('SELECT payload_json FROM formal_grants WHERE grant_id=?',(grant.grant_id,)).fetchone()
        encoded=canonical_json(asdict(grant))
        if row is None:db.execute('INSERT INTO formal_grants VALUES(?,?,?)',(grant.grant_id,grant.plan.approval_ref,encoded))
        elif row[0]!=encoded:raise PermissionError('persisted activation permit changed')
    authority.verify(grant,**bindings)
    from .formal_assets import confined,raw_hash
    env=config['experiment_config']['environment'];price=env['price_evidence'];path=confined(root,price['path'])
    if raw_hash(path)!=price['sha256']:raise PermissionError('activation price binding changed')
    value=json.loads(path.read_text());now=datetime.now(timezone.utc)
    if not datetime.fromisoformat(value['valid_from_utc'])<=now<datetime.fromisoformat(value['valid_until_utc']):
        raise PermissionError('activation price expired')
    from app.m0.config import load_settings
    settings=load_settings(require_api_key=False)
    if (settings.model,settings.base_url)!=(value['model'],value['endpoint']):raise PermissionError('activation provider differs')
    key=Path('/run/provider-secret/key').read_text().strip()
    if not key or 'ENGINEERING' in key:raise PermissionError('real provider secret is absent')
    os.environ['DEEPSEEK_API_KEY']=key
    return 'FORMAL_SIGNED_ACTIVATION'

def start():
    from .governance import verify_container_process
    verify_container_process(10002)
    root=Path(__file__).resolve().parents[2]
    trust=root/'data/eval/formal-trust'
    binding=json.loads((trust/'target-binding.json').read_text())
    from urllib.parse import quote
    password=Path('/run/db-secret/password').read_text().strip()
    os.environ[binding['url_env']]=f'postgresql+psycopg://evalapp:{quote(password,safe="")}@m62-eval-postgres:5432/shoprelay_m11_test'
    # Only test placeholder material is present in this deployment. The SDK
    # substitute is selected by readonly trusted deployment configuration.
    mode=json.loads((trust/'mode.json').read_text())
    phase=configure_execution(root,mode)
    os.environ['DATABASE_URL']=os.environ[binding['url_env']]
    from .personal_dev_database import FormalPostgresDatabase
    target=FormalPostgresDatabase(root,binding)
    if not target.path.exists():
        engine,_=target.open(initialize=True)
        from sqlalchemy import text
        from app.orchestration.checkpointer import open_checkpointer,run_checkpointer_async
        with engine.begin() as db:db.execute(text('CREATE SCHEMA shoprelay_langgraph'))
        async def setup():
            async with open_checkpointer(target.database_url,'shoprelay_langgraph') as saver:await saver.setup()
        run_checkpointer_async(setup());engine.dispose()
    if phase=='ENGINEERING':
        from .container_canary_sdk import install
        install()
    import uvicorn
    uvicorn.run('eval.m6_1.container_boundary:app',host='0.0.0.0',port=8080,log_level='warning')

if __name__=='__main__':start()
