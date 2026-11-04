"""Real CLI and application adapters for the bounded Single/Multi comparison."""
import json
from types import SimpleNamespace
from dataclasses import replace
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from test_m6_2_r30_personal_real import personal
from test_m6_2_r24_formal_application import cli
from eval.m6_1.contracts import sha256_json

@pytest.mark.parametrize('architecture',['SINGLE','MULTI'])
@pytest.mark.parametrize('mode',['valid','scope','wrong_policy'])
def test_actual_cli_architecture_and_guarded_failure_continue(tmp_path,monkeypatch,architecture,mode):
    import test_m6_2_r24_formal_application as support
    from app.m0 import config
    base=support.fictional_cases
    def inputs():
        cases=base()
        c=cases[2]
        value=dict(c.input)
        value.update(user_query='请根据我自己的订单 ORD-EXAMPLE-901 的明细与退货政策，说明记录状态和政策条款。',allowed_capabilities=['get_order','get_order_items','search_knowledge'])
        cases[2]=replace(c,input=value)
        return cases
    monkeypatch.setattr(support,'fictional_cases',inputs)
    cases,store,requests=personal(tmp_path,monkeypatch)
    path=tmp_path/'deployment.json';cfg=json.loads(path.read_text())
    cfg['experiment_config']['environment']['trial_architectures']={c.case_id:architecture for c in cases}
    cfg['plan']['config_sha256']=sha256_json(cfg['experiment_config']);cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
    path.write_text(json.dumps(cfg))
    sdk=config.create_deepseek_client();original=sdk.create
    def response(**kwargs):
        payload=json.loads(kwargs['messages'][-1]['content'])
        if 'tool_results' not in payload:return original(**kwargs)
        requests.append(kwargs)
        assert 'expected_outcome' not in json.dumps(payload) and 'required_tool_groups' not in json.dumps(payload)
        data={'business_answer':'订单记录状态为PAID。','policy_answer':'','citations':[]}
        if architecture=='SINGLE':
            if not payload['tool_results']:
                data={'business_answer':'','policy_answer':'','citations':[],'kind':'tool','tool':'get_order' if mode!='scope' else 'cancel_order','arguments':{'order_id':'ORD-EXAMPLE-901'}}
            else:data.update(kind='finish',tool=None,arguments={})
        if mode=='wrong_policy' and (architecture=='MULTI' or payload['tool_results']):
            data.update(policy_answer='Fictional warranty lasts 72 months from recorded purchase.',citations=['fiction-warranty'])
        return SimpleNamespace(model='deepseek-flash',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(data)))],usage=SimpleNamespace(total_tokens=50,model_dump=lambda:{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}))
    sdk.chat.completions.create=response
    monkeypatch.setattr(config,'create_deepseek_client',lambda *a,**k:sdk)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert result['stop_reason'] is None,result
    assert len(result['completed_cases'])==4 and result['budget']['active_reservations']==0
    trial=next(t for t in store.trials('unprotected-full-app-test') if t['case_id']==cases[2].case_id)
    raw=store.raw_result(trial['trial_id']);archive=raw['application_archive'];events=archive['result']['observations']['run_events']
    assert raw['code_assessment']['integrity']['audit']=='PASS'
    if architecture=='SINGLE':
        assert not any(e['event_type']=='agent_handoff' for e in events)
        if mode=='scope':
            assert archive['read_invocations']==[] and raw['code_assessment']['task_outcome']=='FAIL'
        else:assert [r['operation'] for r in archive['read_invocations']]==['get_order']
    else:
        assert any(r['operation']=='get_order' for r in archive['read_invocations'])
    if mode=='wrong_policy':
        assert raw['code_assessment']['checks']['delivery']=='FAIL'
    for t in store.trials('unprotected-full-app-test'):
        proof=json.loads((tmp_path/t['raw_result_ref']).with_suffix('.cleanup.json').read_text())
        assert proof['evidence']['residual_case_rows']==0

@pytest.mark.parametrize('pooled',[False,True])
def test_shared_cost_pool_uses_actual_reservations_not_sum_of_case_ceilings(tmp_path,monkeypatch,pooled):
    cases,store,requests=personal(tmp_path,monkeypatch)
    path=tmp_path/'deployment.json';cfg=json.loads(path.read_text())
    cfg['experiment_config']['budget_limits']['estimated_usd']=.31
    cfg['plan']['budget']['estimated_usd']=.31
    if pooled:cfg['experiment_config']['environment']['budget_pool_policy']='ACTUAL_STEP_RESERVATIONS_SHARED_COST_V1'
    cfg['plan']['config_sha256']=sha256_json(cfg['experiment_config']);cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
    path.write_text(json.dumps(cfg));cli(tmp_path,'issue')
    if pooled:
        result=cli(tmp_path,'execute');assert result['stop_reason'] is None and len(result['completed_cases'])==4
        assert result['budget']['estimated_usd']<.31 and result['budget']['active_reservations']==0
    else:
        with pytest.raises(ValueError,match='batch caps'):cli(tmp_path,'execute')
        assert not requests and not store.trials(cfg['experiment_id'])


def test_complete_read_aggregation_failure_continues_but_missing_audit_stops(tmp_path,monkeypatch):
    from copy import deepcopy
    from eval.m6_1.formal_scoring import evaluate
    cases,store,requests=personal(tmp_path,monkeypatch)
    for module,name in [('app.m3.order_tools','build_order_business_result'),
            ('app.m3.product_tools','build_product_business_result'),
            ('app.m3.policy_tools','build_policy_business_result'),
            ('app.m3.after_sales_tools','build_after_sales_business_result'),
            ('app.m3.composition','build_multi_agent_business_result')]:
        monkeypatch.setattr(module+'.'+name,lambda *a,**k:None)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert result['stop_reason'] is None and len(result['completed_cases'])==4
    trial=next(t for t in store.trials('unprotected-full-app-test') if t['case_id']==cases[2].case_id)
    raw=store.raw_result(trial['trial_id']);grade=raw['code_assessment']
    assert grade['task_outcome']=='FAIL' and grade['checks']['delivery']=='FAIL'
    assert grade['blocking_items']==[] and grade['integrity']['audit']=='PASS'
    assert 'COMPLETED_READS_WITHOUT_USER_ANSWER_APPLICATION_FAILURE' in grade['failure_categories']
    damaged=deepcopy(raw['application_archive']);damaged['read_invocations'][0].pop('tool_audit')
    reassessed=evaluate(cases[2],damaged,trial['usage'])
    assert 'FAILED_STAGE_NOT_CLASSIFIED' in reassessed['blocking_items']
    assert result['budget']['active_reservations']==0
