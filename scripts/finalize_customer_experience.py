"""Safe in-container proof export and automated-budget closure, no requests."""
import json
from pathlib import Path
import ssl
import httpx
from sqlalchemy import text
from app.m1.db import make_engine
from scripts.m65_local_delivery import application,database_url

def main():
    app=application()
    metered=app.state.chat_provider.client
    scope,ledger=metered.ledger()
    assert scope=='AUTOMATED_SYNTHETIC'
    snapshot=ledger.snapshot()
    assert snapshot['active_reservations']==0 and snapshot['token_status']=='RECORDED'
    assert snapshot['provider_requests']<=10 and snapshot['estimated_usd']<=1
    transport=metered.client._client._transport_for_url(httpx.URL('https://api.deepseek.com'))
    proxy=transport._pool._proxy_url
    assert proxy.host==b'127.0.0.1' and proxy.port==7892
    assert transport._pool._ssl_context.verify_mode==ssl.CERT_REQUIRED
    engine=make_engine(database_url())
    with engine.connect() as db:
        identity=db.execute(text('SELECT identity FROM public.d1_personal_target')).scalar_one()
        assert identity=='ca3b0b3d027448a09cc58caa49139862'
        assert db.execute(text("SELECT count(*) FROM runs WHERE status='RUNNING'")).scalar_one()==0
    export={'scope':scope,'snapshot':snapshot,'events':metered.store.budget_snapshot(ledger.experiment_id),
        'limits':metered.store.budget_limits(ledger.experiment_id),'database_identity':identity,
        'actual_sdk_proxy':{'host':'127.0.0.1','port':7892},'TLS_verify':True,
        'SDK_max_retries':metered.client.max_retries,'invoice_status':'UNKNOWN'}
    assert export['SDK_max_retries']==0
    Path('/delivery-ledger/automated-closed.json').write_text(json.dumps(export,ensure_ascii=False,indent=2))
    # This enables only subsequent user-initiated requests. Automated test entry
    # points refuse this scope; old conversations still fail the creation cutoff.
    Path('/delivery-ledger/scope').write_text('USER_INITIATED')
    user_scope,user_ledger=metered.ledger()
    assert user_scope=='USER_INITIATED' and user_ledger.snapshot()['provider_requests']==0
    print(json.dumps({'automated':snapshot,'manual_provider_requests':0,
        'runtime_mode':'REAL_MODEL','automated_acceptance':'CLOSED'},ensure_ascii=False))

if __name__=='__main__':main()
