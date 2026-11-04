"""Current application behavior and complete supported/rejected policy candidates."""
import json
from pathlib import Path
import pytest
from app.m1.routing import decide_route
from app.m2.self_rag import _answer_semantic_issues
from test_m63_repair_retest import short_project_tmp as tmp_path

ROOT=Path(__file__).resolve().parents[1]
ROWS=json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/scores-and-evidence.json').read_text(encoding='utf8'))['rows']
MAIN={r['source_case_id'].removeprefix('FC26-'):r for r in ROWS if r['phase']=='main'}
SUPPORTED=['POLICY-030','POLICY-043','EXTRA-067','EXTRA-073','EXTRA-079','EXTRA-090','EXTRA-094']
BASELINE=json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/baseline-protection.json').read_text())
ORIGINAL_PASS={k.split('-r1-',1)[1].removeprefix('FC26-') for k,v in BASELINE['all_original_labels'].items() if v=='PASS'}
PROTECTED=sorted({n for n,r in MAIN.items() if r['ai_review']['task_outcome']=='PASS'}|ORIGINAL_PASS)
assert len(PROTECTED)==82

def selected(row):
    return [s for s in row['policy_sources'] if s['chunk_id'] in row['candidate']['citation_ids']]

@pytest.mark.parametrize('name',SUPPORTED)
def test_complete_actual_supported_candidate(name):
    row=MAIN[name]
    assert not _answer_semantic_issues(row['candidate']['redacted_text'],selected(row),row['question'])

@pytest.mark.parametrize('name',['PRODUCT-007','PRODUCT-008','EXTRA-098','EXTRA-100'])
def test_natural_missing_inputs_do_not_read(name):
    row=MAIN[name]
    case=json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/main-cases.json').read_text())['cases']
    inp=next(c['input'] for c in case if c['case_id']==row['case_id'])
    route=decide_route(row['question'],available_capabilities=inp['allowed_capabilities'])
    assert route.strategy=='DIRECT' and not route.required_capabilities
    required={'PRODUCT-007':['安装','设备','重量'],'PRODUCT-008':['接口','输入','输出','电压','功率'],
              'EXTRA-098':['正文','附录','排序'],'EXTRA-100':['审批记录','订单']}[name]
    assert all(s in route.clarification_question for s in required),route

def test_return_application_status_is_real_status_read():
    row=MAIN['AFTER_SALES-028']
    route=decide_route(row['question'],available_capabilities=['get_order','get_refund_status','search_knowledge'])
    assert 'get_refund_status' in route.required_capabilities

@pytest.mark.parametrize('name,old,new',[
    ('POLICY-030','十二小时','十三小时'),('POLICY-030','付款后','送达后'),
    ('POLICY-030','确认图稿前','确认图稿后'),('POLICY-043','七天','八天'),
    ('EXTRA-067','41天','41个月'),('EXTRA-073','商业使用','个人使用'),
    ('EXTRA-079','六个月','七个月'),('EXTRA-094','电池期限','食品期限'),
    ('EXTRA-094','购买后','激活后')])
def test_supported_candidate_mutations_still_rejected(name,old,new):
    row=MAIN[name];text=row['candidate']['redacted_text'];assert old in text
    assert _answer_semantic_issues(text.replace(old,new),selected(row),row['question'])

@pytest.mark.parametrize('name',PROTECTED)
def test_current_application_regression(name,tmp_path,monkeypatch):
    exercise_current_application(name,tmp_path,monkeypatch)


def test_m3_rejection_keeps_both_sections_but_never_delivers(tmp_path,monkeypatch):
    exercise_current_application('CROSS_DOMAIN-050',tmp_path,monkeypatch,reject=True)


def test_m3_schema_failure_preserves_completed_handoffs(tmp_path,monkeypatch):
    exercise_current_application('CROSS_DOMAIN-050',tmp_path,monkeypatch,schema_failure=True)


def test_publisher_uses_signed_ledger_name_not_report_name(tmp_path,monkeypatch):
    from scripts import manage_m63_retest as manager
    out=tmp_path/'different-report-name';out.mkdir()
    ledger='data/eval/formal-ledgers/exact-ledger-name'
    for name in ('authorization.json','acceptance-cases.json','main-cases.json','variants-cases.json','price.json','proof.json'):
        (out/name).write_text('{}')
    request={'config':{'experiment_id':'unit-acceptance','plan':{'ledger_path':ledger+'/results.sqlite3','execution_implementation_sha256':'0'*64}},
             'registry':{},'isolation_evidence_file':'proof.json'}
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    commands=[]
    monkeypatch.setattr(manager,'OUT',out);monkeypatch.setattr(manager,'LEDGER',ledger)
    monkeypatch.setattr(manager.ops,'admin',lambda code,*args:commands.append(code) or '{}')
    monkeypatch.setattr(manager,'control',lambda code:{})
    monkeypatch.setattr(manager.ops,'write_trust',lambda *args:None)
    manager.publish(path)
    assert "Path('/results/exact-ledger-name')" in commands[0]
    assert '/results/different-report-name' not in commands[0]
    request['config']['plan']['ledger_path']='data/eval/formal-ledgers/wrong/results.sqlite3'
    path.write_text(json.dumps(request))
    with pytest.raises(PermissionError,match='differs'):manager.publish(path)
    assert len(commands)==1


@pytest.mark.parametrize('activated',[True,False])
def test_real_execute_endpoint_activates_before_run(tmp_path,monkeypatch,activated):
    from types import SimpleNamespace
    from datetime import datetime,timezone,timedelta
    from fastapi.testclient import TestClient
    from eval.m6_1 import container_boundary as boundary,container_startup
    from eval.m6_1.store import ResultStore
    trust=tmp_path/'trust';trust.mkdir();(trust/'mode.json').write_text(json.dumps({'engineering_only':False,'real_provider_allowed':activated}))
    store=ResultStore(tmp_path/'ledger.sqlite3')
    with store.transaction() as db:
        db.execute('CREATE TABLE container_attempts(trial_id TEXT PRIMARY KEY,payload_json TEXT,stage TEXT,receipt_json TEXT,result_json TEXT)')
        db.execute('INSERT INTO container_attempts VALUES(?,?,?,?,?)',('attempt','{}','PREPARED','{}',None))
    calls=[]
    def activate(root,mode):
        calls.append('activation')
        if not mode['real_provider_allowed']:raise PermissionError('not activated')
        return 'FORMAL_SIGNED_ACTIVATION'
    def execute(*args,**kwargs):
        assert calls==['activation'];calls.append('execute')
        return {'archive':{'synthetic_engineering_only':True}}
    worker=SimpleNamespace(config={'engineering_only':False},execute=execute,engine=SimpleNamespace(dispose=lambda:None))
    grant=SimpleNamespace(plan=SimpleNamespace(expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    case=SimpleNamespace(expected_outcome={'task':{'per_case_budget':{'max_duration_seconds':60}}})
    monkeypatch.setattr(boundary,'ROOT',tmp_path);monkeypatch.setattr(boundary,'TRUST',trust)
    monkeypatch.setattr(boundary,'worker_setup',lambda *args,**kwargs:(worker,store,None,grant,None,None,case))
    monkeypatch.setattr(container_startup,'configure_execution',activate)
    with TestClient(boundary.app) as client:
        response=client.post('/boundary/execute',json={'grant':{},'payload':{},'trial_id':'attempt','remaining_seconds':60})
    with store.transaction(immediate=False) as db:stage=db.execute('SELECT stage FROM container_attempts').fetchone()[0]
    assert response.status_code==(200 if activated else 403)
    assert stage==('ARCHIVED' if activated else 'PREPARED')
    assert calls==(['activation','execute'] if activated else ['activation'])


def exercise_current_application(name,tmp_path,monkeypatch,*,reject=False,schema_failure=False,architecture='MULTI',controller_response=None,expected_candidate_rejection=False):
    """Actual CLI/scope/Repository/Provider/validator/archive/cleanup, no forced route."""
    from dataclasses import replace
    from types import SimpleNamespace
    from app.m0 import config
    from eval.m6_1.contracts import EvalCase,sha256_json
    import test_m6_2_r24_formal_application as support
    from test_m6_2_r30_personal_real import personal
    original=support.fictional_cases
    row=MAIN[name]
    source=next(c for c in json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/main-cases.json').read_text())['cases'] if c['case_id']==row['case_id'])
    def cases():
        values=original();old=values[2];case=EvalCase.from_dict(source)
        # This SQLite harness exercises the actual chat/draft boundary. Seven
        # scripted owner follow-ups are separate PG/API scenarios, protected
        # by the existing action suite; never fake those as completed here.
        if 'owner_interaction' in case.input:
            case=replace(case,input={k:v for k,v in case.input.items() if k!='owner_interaction'})
        values[2]=replace(case,case_id=old.case_id,group_id=old.group_id,split=old.split,data_version=old.data_version)
        return values
    monkeypatch.setattr(support,'fictional_cases',cases)
    values,store,requests=personal(tmp_path,monkeypatch)
    path=tmp_path/'deployment.json';cfg=json.loads(path.read_text())
    cfg['experiment_config']['environment']['trial_architectures']={c.case_id:architecture for c in values}
    limits=cfg['experiment_config']['budget_limits']
    for key in ('provider_calls','tool_calls','total_tokens','estimated_usd','max_duration_seconds'):
        limits[key]=sum(c.expected_outcome['task']['per_case_budget'][key] for c in values)
    limits['max_calls_per_trial']=20;cfg['plan']['budget']=dict(limits)
    cfg['plan']['config_sha256']=sha256_json(cfg['experiment_config']);cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
    path.write_text(json.dumps(cfg))
    sdk=config.create_deepseek_client();prior=sdk.chat.completions.create
    seen=[]
    def response(**kwargs):
        # Only external inference is substituted; the actual application chooses
        # its route and calls scoped adapters. No expected tool plan is passed.
        message=kwargs['messages'][-1]['content']
        if 'Classify' in kwargs['messages'][0]['content'] and message==row['question']:
            fields=('strategy','sub_strategy','intent_labels','safety_decision','reason_code','needs_clarification','clarification_question','required_capabilities')
            out={k:row['route'][k] for k in fields}
            requests.append(kwargs)
            return SimpleNamespace(model='deepseek-flash',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(out)))],usage=SimpleNamespace(total_tokens=50,model_dump=lambda:{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}))
        try:payload=json.loads(message)
        except json.JSONDecodeError:return prior(**kwargs)
        if payload.get('question')!=row['question']:return prior(**kwargs)
        assert 'expected_outcome' not in json.dumps(payload)
        seen.append(payload);requests.append(kwargs)
        if 'tool_results' in payload and architecture=='SINGLE':
            assert controller_response is not None
            out=controller_response(payload)
        elif 'tool_results' in payload:
            assert '库存available/reserved/quantity是数量' in kwargs['messages'][0]['content']
            text=payload['verified_business_summary']
            out={'business_answer':text,'policy_answer':'','citations':[]}
            if reject:
                source=payload['evidence'][0]
                out['policy_answer']='商品送达后八天内可申请退货。'
                out['citations']=[source['chunk_id']]
            if schema_failure:out['unexpected_field']='not a valid BusinessAnswer'
        else:
            if row['candidate']:
                out={'answer':row['candidate']['redacted_text'],'citations':row['candidate']['citation_ids']}
                if expected_candidate_rejection:
                    # Isolate semantic rejection using only this invocation's
                    # actual returned IDs; separate tests retain bad-citation
                    # rejection. This is the offline model, not application filtering.
                    returned={e['chunk_id'] for e in payload['evidence']}
                    out['citations']=[cid for cid in out['citations'] if cid in returned]
            else:
                # Source-derived offline answer, not an expected label. This
                # proves serialization/validation, not new model Task quality.
                out={'answer':'；'.join(e['content'] for e in payload['evidence']),
                     'citations':[e['chunk_id'] for e in payload['evidence']]}
        return SimpleNamespace(model='deepseek-flash',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(out)))],usage=SimpleNamespace(total_tokens=50,model_dump=lambda:{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}))
    sdk.chat.completions.create=response;monkeypatch.setattr(config,'create_deepseek_client',lambda *a,**k:sdk)
    support.cli(tmp_path,'issue');result=support.cli(tmp_path,'execute')
    assert result['stop_reason'] is None,result
    trial=store.trials('unprotected-full-app-test')[2];raw=store.raw_result(trial['trial_id'])
    assessment=raw['code_assessment']
    if reject:
        assert assessment['checks']['delivery']=='FAIL'
        audit=json.loads(raw['application_archive']['result']['observations']['runs'][0]['error_detail_json'])['answer_rejection_audit']
        assert audit['candidate_answer_scope']=='M3_POLICY_SEGMENT_WITH_STRUCTURED_SECTIONS'
        assert audit['candidate_sections']['business_answer']['text_redacted']
        assert audit['candidate_sections']['policy_answer']['text_redacted']=='商品送达后八天内可申请退货。'
    elif expected_candidate_rejection:
        assert assessment['checks']['delivery']=='FAIL',assessment
        audit=json.loads(raw['application_archive']['result']['observations']['runs'][0]['error_detail_json'])['answer_rejection_audit']
        assert audit['candidate_answer_redacted'] and audit['validation_findings']
    else:assert assessment['checks']['delivery']=='PASS',assessment
    assert assessment['checks']['required_tools']=='PASS',assessment
    if name in {'POLICY-030','POLICY-043','EXTRA-094'}:assert seen
    if name=='PRODUCT-005':assert '分值' not in assessment['delivered_answer']
    if name=='ORDER-012':assert '无法确认' in assessment['delivered_answer']
    if name=='ORDER-012':assert 'business_database' in assessment['delivered_answer'] and '观测时刻' in assessment['delivered_answer']
    if name=='AFTER_SALES-028':assert '退货申请' in assessment['delivered_answer'] and 'UNDER_REVIEW' in assessment['delivered_answer']
    if name.startswith('CROSS_DOMAIN') and not reject and architecture=='MULTI':
        handoffs=raw['application_archive']['result']['result']['handoffs']
        assert len(handoffs)>=2
    for t in store.trials('unprotected-full-app-test'):
        cleanup=json.loads((tmp_path/t['raw_result_ref']).with_suffix('.cleanup.json').read_text())
        assert cleanup['evidence']['residual_case_rows']==0
    assert result['budget']['active_reservations']==0
    return raw
