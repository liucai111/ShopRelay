"""Independent miniature regressions,not prompts tuned to candidate answers."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from test_m6_2_r24_formal_application import fictional_cases
from eval.m6_1.formal_reference_rules import V2,route_reference,resource_coverage
from eval.m6_1.grants import formal_case_payload
from eval.m6_1.contracts import EvalCase

def sample():
    v=formal_case_payload(fictional_cases()[0]);v['rubric_refs'].append(V2)
    return EvalCase.from_dict(v)

def test_route_explanatory_text_not_an_exact_label_but_legacy_preserved():
    c=sample();route={**c.expected_outcome['route'],'intent_labels':['different_valid_explanation'],'reason_code':'ANOTHER_REASON'}
    assert route_reference(c,route)=='PASS'
    assert route_reference(replace(c,rubric_refs=('old-v1',)),route)=='FAIL'

@pytest.mark.parametrize('changed',[{'safety_decision':'REFUSE'},{'needs_clarification':True},
    {'required_capabilities':['write_order']},{'strategy':'REFUSE','sub_strategy':None}])
def test_stable_route_or_scope_mismatch_rejected(changed):
    c=sample();assert route_reference(c,{**c.expected_outcome['route'],**changed})=='FAIL'

def test_only_explicit_legal_equivalent_path_is_accepted():
    v=formal_case_payload(sample());route=v['expected_outcome']['route']
    v['expected_outcome']['task']['route_equivalents']=[{k:(('MULTI' if k in {'strategy','sub_strategy'} else val))
        for k,val in route.items() if k not in {'intent_labels','reason_code'}}]
    c=EvalCase.from_dict(v)
    assert route_reference(c,{**route,'strategy':'MULTI','sub_strategy':'MULTI'})=='PASS'
    assert route_reference(c,{**route,'strategy':'AGENT','sub_strategy':'AGENT+SINGLE'})=='FAIL'

@pytest.mark.parametrize('values,expected',[(['one'],'FAIL'),(['one','one'],'FAIL'),(['one','two'],'PASS'),(['other','two'],'FAIL'),([],'FAIL')])
def test_two_resources_not_one_tool_name(values,expected):
    v=formal_case_payload(sample());v['expected_outcome']['task']['required_resources']=[{'tool':'get_product_detail','argument':'product_id','values':['one','two']}]
    c=EvalCase.from_dict(v)
    calls=[{'operation':'get_product_detail','status':'RETURNED','arguments':{'product_id':x}} for x in values]
    assert resource_coverage(c,calls)==expected

def test_missing_argument_audit_is_unknown_not_zero_or_success():
    v=formal_case_payload(sample());v['expected_outcome']['task']['required_resources']=[{'tool':'get_product_detail','argument':'product_id','values':['one','two']}]
    c=EvalCase.from_dict(v)
    assert resource_coverage(c,[{'operation':'get_product_detail','status':'RETURNED'}])=='UNKNOWN'
    assert resource_coverage(c,[{'operation':'get_product_detail','status':'FAILED','arguments':{'product_id':'one'}}])=='FAIL'

def test_complete_cli_records_real_arguments_and_ignores_free_text_reference_drift(tmp_path,monkeypatch):
    import test_m6_2_r24_formal_application as helpers
    originals=fictional_cases();revised=[]
    for c in originals:
        v=formal_case_payload(c);v['rubric_refs'].append(V2)
        v['expected_outcome']['route'].update(reason_code='REFERENCE_PROSE_NOT_MODEL_OUTPUT',intent_labels=['human-defined-purpose'])
        if v['case_id']=='unprotected-fixture-2':
            v['expected_outcome']['task']['required_resources']=[{'tool':'get_order','argument':'order_id','values':['ORD-EXAMPLE-901']}]
        revised.append(EvalCase.from_dict(v))
    monkeypatch.setattr(helpers,'fictional_cases',lambda:revised)
    cases,store,requests=helpers.deploy(tmp_path,monkeypatch)
    helpers.cli(tmp_path,'issue');result=helpers.cli(tmp_path,'execute')
    assert len(result['completed_cases'])==4 and result['stop_reason'] is None
    trials=store.trials('unprotected-full-app-test')
    owned=next(t for t in trials if t['case_id']=='unprotected-fixture-2')
    raw=store.raw_result(owned['trial_id'])
    assert raw['application_archive']['read_invocations'][0]['arguments']['order_id']=='ORD-EXAMPLE-901'
    assert raw['code_assessment']['checks']['resource_coverage']=='PASS'
    assert raw['code_assessment']['checks']['route_reference']=='PASS'
    assert store.raw_result(next(t['trial_id'] for t in trials if t['case_id']=='unprotected-fixture-1'))['code_assessment']['task_outcome']=='FAIL'

def test_synthetic_after_sales_history_transition_and_source_mismatch():
    from eval.m6_2.candidate_revision_r27 import _history
    from app.m3.after_sales_tools import AfterSalesReadRepository
    from app.m1.models import ReturnRequest,AfterSalesEvent
    from datetime import datetime
    row={'id':'mini-return','order_id':'mini-order','source_type':'SIMULATED_TEST_DATA','source_ref':'mini-source',
        'status':'APPROVED','submitted_at':'2026-10-20T08:00:00Z','approved_at':'2026-10-20T10:00:00Z','created_at':'2026-10-20T08:00:00Z'}
    _history(row)
    values={k:v for k,v in row.items() if k not in {'events','id'}}
    for k in ['created_at','submitted_at','review_started_at','approved_at']:values[k]=datetime.fromisoformat(values[k].replace('Z','+00:00'))
    record=ReturnRequest(return_id=row['id'],**values)
    events=[]
    for v in row['events']:
        e=deepcopy(v)
        for k in ['occurred_at','recorded_at']:e[k]=datetime.fromisoformat(e[k].replace('Z','+00:00'))
        events.append(AfterSalesEvent(**e))
    assert AfterSalesReadRepository._verified_history('return',record,events)[0:2]==(True,'APPROVED')
    assert AfterSalesReadRepository._verified_history('return',record,events[1:])[0] is False
    events[0].source_ref='wrong-source'
    assert AfterSalesReadRepository._verified_history('return',record,events)[0] is False

def test_bound_adoption_rejects_unchanged_count_single_label_drift(tmp_path):
    from eval.m6_2.candidate_revision_r27 import bind_confirmation,REVIEW
    from eval.m6_1.formal_assets import write_new
    import hashlib
    original={'items':[{'case_id':'one','label':'SINGLE'}]}
    write_new(tmp_path/'review.json',original)
    write_new(tmp_path/REVIEW/'user-confirmation.json',{'bound_review_artifacts':[{'path':'review.json','sha256':hashlib.sha256((tmp_path/'review.json').read_bytes()).hexdigest()}],'source_bindings':[]})
    bind_confirmation(tmp_path)
    (tmp_path/'review.json').write_text(json.dumps({'items':[{'case_id':'one','label':'REFUSE'}]}),encoding='utf8')
    with pytest.raises(ValueError,match='source drift'):bind_confirmation(tmp_path)

@pytest.mark.parametrize('mode',[{'engineering_only':True,'real_provider_allowed':True},
    {'engineering_only':'false','real_provider_allowed':False},{'real_provider_allowed':True}])
def test_mode_cannot_implicitly_activate_real_provider(tmp_path,mode):
    from eval.m6_1.container_startup import configure_execution
    with pytest.raises(PermissionError):configure_execution(tmp_path,mode)

def test_prepared_mode_reads_no_provider_secret_and_cannot_rename_test_trust(tmp_path):
    from eval.m6_1.container_startup import configure_execution
    from eval.m6_1.formal_assets import write_new
    config=tmp_path/'data/eval/formal-trust/deployment.json'
    write_new(config,{'engineering_only':False,'issuer':'TEST_FORMAL_SUBJECT_NOT_ACTIVATED'})
    assert configure_execution(tmp_path,{'engineering_only':False,'real_provider_allowed':False})=='PREPARED_NO_EXECUTION'
    config.write_text(json.dumps({'engineering_only':False,'issuer':'R26_ENGINEERING_ONLY'}),encoding='utf8')
    with pytest.raises(PermissionError,match='renamed formal'):
        configure_execution(tmp_path,{'engineering_only':False,'real_provider_allowed':True})
