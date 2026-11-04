"""HTTP delivery smoke against the deployed application, never a model probe.

Run inside the isolated delivery container. Output contains no passwords,
cookies, DSNs or hidden reasoning. Production APIs and persistent records are
used; the separately configured server uses an explicit answer substitute.
"""
import hashlib
import json
from pathlib import Path
import sys
import uuid
from datetime import datetime,timedelta,timezone

import httpx

STATE=Path('/delivery-state')
QUERY='请说明虚构政策中独立商业渠道的申请期限与适用条件。'


def login(base='http://127.0.0.1:8080',owner='m65_owner'):
    client=httpx.Client(base_url=base,trust_env=False,timeout=40)
    csrf=client.get('/api/demo/csrf').json()['csrf_token']
    origin='http://127.0.0.1:18066' if 'negative' in base else 'http://127.0.0.1:18065'
    headers={'Origin':origin,'X-CSRF-Token':csrf}
    response=client.post('/api/demo/login',headers=headers,json={
        'username':owner,'password':(STATE/'demo-password').read_text()})
    assert response.status_code==200,(response.status_code,response.json())
    headers['X-CSRF-Token']=client.cookies.get('shoprelay_csrf')
    return client,headers


def execute():
    client,headers=login()
    assert client.get('/').status_code==200
    assert client.get('/health/ready').json()['status']=='ready'
    rows=[]
    for query in (QUERY,'查询我自己的订单 ORD-M65-901 的记录状态'):
        response=client.post('/api/chat',headers=headers,json={'message':query,'client_request_id':'m65-'+uuid.uuid4().hex})
        value=response.json()
        assert response.status_code==200,(response.status_code,value)
        run_id=value['run_id']
        run=client.get('/api/runs/'+run_id).json()['run']
        events=client.get('/api/runs/'+run_id+'/events').json()['events']
        execution=client.get('/api/runs/'+run_id+'/execution').json()['execution']
        assert run['status']=='SUCCEEDED' and events and execution['spans']
        if query==QUERY:
            assert value['citations'] and '43天' in value['answer']
        else:
            assert execution['tool_references'] and 'PAID' in value['answer']
        rows.append({'question':query,'response':value,'run':run,'events':events,'execution':execution})
    negative,h=login('http://delivery-negative:8080')
    response=negative.post('/api/chat',headers=h,json={'message':QUERY,'client_request_id':'m65-negative-'+uuid.uuid4().hex})
    value=response.json()
    assert response.status_code==502,value
    run_id=value['error']['details']['run_id'] if 'error' in value else value['details']['run_id']
    run=negative.get('/api/runs/'+run_id).json()['run']
    assert run['status']=='FAILED' and run['answer'] is None
    assert 'ANSWER_UNSUPPORTED_POLICY_WINDOW' in json.dumps(run)
    rows.append({'question':QUERY,'negative_engineering_mutation':'duration_unit',
        'http_status':response.status_code,'run':run,
        'events':negative.get('/api/runs/'+run_id+'/events').json()['events']})
    other,_=login(owner='m65_other')
    assert other.get('/api/runs/'+rows[0]['run']['run_id']).status_code==404
    assert other.get('/api/orders/ORD-M65-901').status_code==404
    now=datetime.now(timezone.utc)
    overview=client.get('/api/admin/observability/runs',params={
        'from':(now-timedelta(days=1)).isoformat(),'to':(now+timedelta(days=1)).isoformat()})
    assert overview.status_code==200,(overview.status_code,overview.json())
    result={'mode':'DETERMINISTIC_REPLAY_OVER_DEPLOYED_HTTP_API','Provider':0,'Judge':0,'Embedding':0,
        'checks':{'login':True,'static_frontend':True,'policy_answer_and_citation':True,'read_only_tool':True,
            'Run_Trace_projection':True,'wrong_duration_rejected_without_delivery':True,'owner_boundary':True},'rows':rows}
    print(json.dumps(result,ensure_ascii=False))


def recover(path):
    proof=json.loads(Path(path).read_text())
    client,_=login()
    result=[]
    for row in proof['rows']:
        prior=row['run'];current=client.get('/api/runs/'+prior['run_id']).json()['run']
        assert current['run_id']==prior['run_id'] and current['status']==prior['status']
        assert current['answer']==prior['answer']
        events=client.get('/api/runs/'+prior['run_id']+'/events').json()['events']
        assert events
        result.append({'run_id':prior['run_id'],'status':current['status'],'answer_unchanged':True,'events':len(events)})
    print(json.dumps({'recovered_after_service_restart':result,'Provider':0}))


if __name__=='__main__':
    if sys.argv[1:] == ['execute']:execute()
    elif len(sys.argv)==3 and sys.argv[1]=='recover':recover(sys.argv[2])
    else:raise SystemExit('execute | recover <prior-safe-json>')
