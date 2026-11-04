"""One bounded real-model acceptance over synthetic sessions and public policies.

The durable plan is written before HTTP. Re-entering this command reads prior
results; it never replays a message whose outcome is unknown. Uses normal APIs.
"""
import json
import os
from pathlib import Path
import uuid
import sys
import httpx

OUT=Path('/delivery-ledger/customer-acceptance.json')
PRIVATE_SESSION='71b78efe-6eb9-4659-b92e-e4c19a489f4f'

def save(value):
    temp=OUT.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    temp.replace(OUT)

def main():
    global OUT
    custom=None
    if len(sys.argv)==3 and sys.argv[1]=='--plan':
        source=Path(sys.argv[2]).resolve()
        if not source.is_relative_to(Path('/delivery-ledger')):raise ValueError('acceptance plan outside dedicated ledger')
        custom=json.loads(source.read_text(encoding='utf8'))
        questions=custom['questions']
        if (not isinstance(questions,list) or not 1<=len(questions)<=8 or
                any(not isinstance(q,str) or not q.strip() or len(q)>2000 for q in questions)):
            raise ValueError('invalid bounded acceptance questions')
        OUT=source.with_name('results.json')
    continuation=sys.argv[1:]==['--after-offline-fix']
    conversation=sys.argv[1:]==['--conversation-check']
    if sys.argv[1:] and not (continuation or conversation or custom):
        raise SystemExit('unknown operation')
    if continuation:
        OUT=OUT.with_name('customer-acceptance-after-fix.json')
    if conversation:
        OUT=OUT.with_name('customer-conversation-check.json')
    if OUT.exists():
        raise RuntimeError('Acceptance already entered; inspect persistent results, do not replay')
    # These are purpose-written generic synthetic inputs, not old transcripts.
    plan={'purpose':'AUTOMATED_SYNTHETIC_REAL_PROVIDER_ACCEPTANCE',
        'existing_user_session_not_used':PRIVATE_SESSION,'rows':[],
        'requests_max':10,'estimated_usd_max':1,'automatic_retries':0}
    save(plan)
    with httpx.Client(base_url='http://127.0.0.1:8080',trust_env=False,timeout=110) as client:
        csrf=client.get('/api/demo/csrf').json()['csrf_token']
        headers={'Origin':'http://127.0.0.1:18065','X-CSRF-Token':csrf}
        login=client.post('/api/demo/login',headers=headers,json={'username':'m65_owner',
            'password':Path('/delivery-state/demo-password').read_text()})
        login.raise_for_status()
        headers['X-CSRF-Token']=client.cookies.get('shoprelay_csrf')
        session=client.post('/api/sessions',headers=headers,json={'title':'合成验收 · 退货与追问'}).json()['session']['session_id']
        plan['session_id']=session
        save(plan)
        if custom:
            from app.m1.delivery_provider import AcceptanceAccount
            account=AcceptanceAccount(batch_id=custom['batch_id'],status='OPEN',approval_event=custom['approval_event'],
                session_ids=[session],**custom['budget'])
            path=Path('/delivery-ledger/acceptance-account.json')
            if path.exists():
                previous=AcceptanceAccount.model_validate_json(path.read_text())
                if previous.status!='CLOSED':raise ValueError('another acceptance account is open')
                saved=OUT.parent/'predecessor-account.json'
                if saved.exists():raise ValueError('acceptance already prepared')
                saved.write_bytes(path.read_bytes())
            path.write_text(account.model_dump_json(),encoding='utf8')
            plan.update(batch_id=account.batch_id,approval_event=account.approval_event,
                requests_max=account.provider_calls,estimated_usd_max=account.estimated_usd)
            save(plan)
        questions=custom['questions'] if custom else (('普通商品的无理由退货有哪些条件？','请通俗解释刚才的退货条件',
            '查询我的订单 ORD-M65-901 的记录状态') if conversation else
            ('我想了解普通商品退货的申请条件','什么意思，请通俗解释',
            '查询我的订单 ORD-M65-901 的记录状态') if continuation else
            ('商品要退货，怎么退','什么意思，请通俗解释','查询我的订单 ORD-M65-901 的记录状态'))
        for question in questions:
            row={'question':question,'client_request_id':'experience-'+uuid.uuid4().hex,'status':'ENTERED'}
            plan['rows'].append(row);save(plan)
            response=client.post('/api/chat',headers=headers,json={'session_id':session,
                'message':question,'client_request_id':row['client_request_id']})
            row['http_status']=response.status_code
            payload=response.json();row['response']=payload
            run_id=payload.get('run_id') or payload.get('details',{}).get('run_id')
            if run_id:
                row['run']=client.get('/api/runs/'+run_id).json()['run']
                row['events']=client.get('/api/runs/'+run_id+'/events').json()['events']
                row['execution']=client.get('/api/runs/'+run_id+'/execution').json()['execution']
            row['status']='RECORDED';save(plan)
            # Do not send another step after unknown usage, a failed model route,
            # or a transport/application failure. No retry and no answer sampling.
            if response.status_code!=200 or (payload.get('route') or {}).get('fallback_error'):
                plan['stop_reason']='APPLICATION_OR_PROVIDER_FAILURE';save(plan);break
    print(json.dumps({'artifact':str(OUT),'synthetic_session':plan['session_id'],
        'recorded_runs':len(plan['rows']),'stop_reason':plan.get('stop_reason')},ensure_ascii=False))

if __name__=='__main__':main()
