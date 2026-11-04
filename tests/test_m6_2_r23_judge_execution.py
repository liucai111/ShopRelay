"""Actual existing Judge CLI functions with SDK interception and durable files."""
from pathlib import Path
from types import SimpleNamespace
import importlib,json
import pytest
import openai

from test_m6_2_personal_dev_batch_r7 import tmp_path
from eval.m6_2 import confirmed_review_r23 as materials
from eval.m6_2.judge_contract import DIMENSIONS
from app.m0 import config as app_config

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def judge(tmp_path,monkeypatch):
    monkeypatch.setattr(materials,'OUT',tmp_path.relative_to(ROOT))
    price=json.loads((ROOT/'docs/eval/m6.2-r23-confirmed-calibration-and-formal-wiring-20261031/official-pricing-evidence.json').read_text(encoding='utf8'))
    (tmp_path/'official-pricing-evidence.json').write_text(json.dumps(price),encoding='utf8')
    materials.build(ROOT);materials.prepare_judge(ROOT)
    from scripts import run_m6_2_user_confirmed_judge_r1 as runner
    importlib.reload(runner);runner.select_profile('r23-confirmed-four')
    monkeypatch.setattr(runner,'PKG',tmp_path)
    for attr in ('SAMPLES','MANIFEST','AUTH','STATE','BUDGET','RESULTS','JOURNAL'):
        monkeypatch.setattr(runner,attr,tmp_path/getattr(runner,attr).name)
    monkeypatch.setattr(app_config,'load_settings',lambda:SimpleNamespace(api_key='OFFLINE_ONLY',model='deepseek-flash',base_url='https://api.deepseek.com'))
    yield runner
    importlib.reload(runner)


def sdk(monkeypatch,contents='valid',usage=(30,20,50),finish='stop',response_model='deepseek-flash'):
    calls=[];constructors=[]
    class Client:
        def __init__(self,**kwargs):
            constructors.append({k:v for k,v in kwargs.items() if k!='api_key'})
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def create(self,**kwargs):
            calls.append(kwargs);payload=json.loads(kwargs['messages'][1]['content'])
            answer={'decision':'PARTIAL','dimension_scores':{k:'PARTIAL' for k in DIMENSIONS},'rationale':'Offline fixture judgement',
                'evidence_refs':[payload['applicable_sources'][0]['source_ref']],'confidence':.5}
            text=json.dumps(answer) if contents=='valid' else contents
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),finish_reason=finish)],
                usage=SimpleNamespace(prompt_tokens=usage[0],completion_tokens=usage[1],total_tokens=usage[2]),model=response_model)
    monkeypatch.setattr(openai,'OpenAI',Client)
    return calls,constructors


def test_actual_request_path_four_once_partial_disagreements_continue_and_close(judge,monkeypatch):
    calls,constructors=sdk(monkeypatch)
    monkeypatch.setenv('NO_PROXY','.com,api.deepseek.com,*')
    for i in range(4):
        result=judge.send_next()
        assert result['parse_status']=='VALID' and result['parsed_judge_output']['decision']=='PARTIAL'
        state=judge.read(judge.STATE)
        assert state['provider_requests_attempted']==i+1 and state['reserved_tokens']==0 and state['reserved_usd']==0
    assert len(calls)==4 and judge.read(judge.AUTH)['status']=='CLOSED_COMPLETED'
    for call,options in zip(calls,constructors):
        assert call['max_tokens']==2048 and call['stream'] is False and call['extra_body']=={'thinking':{'type':'disabled'}}
        assert options['max_retries']==0 and 0<options['timeout']<=120
        assert 'dimension_scores' in call['messages'][0]['content']
        payload=json.loads(call['messages'][1]['content'])
        assert set(payload)=={'question','answer_or_claim','applicable_sources','rubric','scoring_target'}
    with pytest.raises(RuntimeError,match='not active'): judge.send_next()
    assert len(calls)==4


@pytest.mark.parametrize('text,finish',[('{"score":1}','stop'),('{"decision":"PASS"','stop'),('valid','length')])
def test_format_failure_is_not_truthy_valid_string_and_stops_after_first(judge,monkeypatch,text,finish):
    calls,_=sdk(monkeypatch,contents=text,finish=finish)
    row=judge.send_next()
    assert row['parse_status']=='FORMAT_FAILURE' and len(calls)==1
    assert judge.read(judge.AUTH)['status']=='CLOSED_STOPPED'
    assert judge.read(judge.STATE)['reserved_tokens']==0  # usage known despite invalid content
    with pytest.raises(RuntimeError): judge.send_next()
    assert len(calls)==1


def test_unknown_usage_retains_conservative_reservation_and_never_retries(judge,monkeypatch):
    calls,_=sdk(monkeypatch,usage=(30,20,999))
    row=judge.send_next();state=judge.read(judge.STATE)
    assert row['usage']=='UNKNOWN' and state['usage_unknown'] and state['reserved_tokens']==12000
    assert state['provider_requests_attempted']==1 and judge.read(judge.AUTH)['status']=='CLOSED_STOPPED'
    with pytest.raises(RuntimeError): judge.send_next()
    assert len(calls)==1


def test_mutated_rates_same_counts_rejected_and_unknown_billing_keeps_known_tokens(judge,monkeypatch):
    calls,_=sdk(monkeypatch,response_model='unrecognized-model')
    budget=judge.read(judge.BUDGET);budget['rates_usd_per_million']['output_peak']=0
    judge.atomic(judge.BUDGET,budget)
    with pytest.raises(RuntimeError,match='budget/pricing'): judge.preflight()
    budget['rates_usd_per_million']['output_peak']=1.2;judge.atomic(judge.BUDGET,budget)
    row=judge.send_next();state=judge.read(judge.STATE)
    assert row['usage_valid'] and row['local_estimated_usd'] is None
    assert state['pricing_unknown'] and state['settled_tokens']==50 and state['reserved_tokens']==0
    assert judge.read(judge.PKG/'judge-closure-r1.json')['local_estimated_usd'] is None
    assert len(calls)==1 and judge.read(judge.AUTH)['status']=='CLOSED_STOPPED'


def test_restart_with_consumed_unsettled_sample_and_changed_scope_are_denied(judge,monkeypatch):
    calls,_=sdk(monkeypatch)
    state=judge.read(judge.STATE);state.update(status='RUNNING',provider_requests_attempted=1,
        attempted_sample_ids=['R22-D1-POLICY-018'],reserved_tokens=12000,reserved_usd=.0144)
    judge.atomic(judge.STATE,state)
    with pytest.raises(RuntimeError,match='unsettled'): judge.preflight()
    with pytest.raises(ValueError,match='ALREADY_PREPARED'): materials.prepare_judge(ROOT)
    assert calls==[]


def test_response_empty_reference_list_matches_prompt_schema():
    from eval.m6_2.judge_contract import parse_response
    value={'decision':'PASS','dimension_scores':{k:'PASS' for k in DIMENSIONS},'rationale':'x','evidence_refs':[],'confidence':.5}
    assert parse_response(json.dumps(value),{'source-a'})[0]=='INVALID_SCHEMA'


def test_offline_actual_finalizer_keeps_dimensions_unlabelled_and_formal_gate_pending(judge,monkeypatch):
    sdk(monkeypatch)
    for _ in range(4): judge.send_next()
    xml=judge.PKG/'isolated-report-test.xml'
    xml.write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>',encoding='utf8')
    result=materials.finalize(ROOT,junit_path=xml)
    analysis=judge.read(judge.PKG/'judge-analysis.json')
    assert result['M6.2']=='IN_PROGRESS' and analysis['agreement']['n']==1 and analysis['agreement']['N']==4
    assert analysis['false_accept_PASS_on_FAIL']['N']==1 and analysis['false_reject_FAIL_on_PASS']['N']==2
    assert all(r['human_dimension_agreement'].startswith('N/A') for r in analysis['rows'])
    matrix=judge.read(judge.PKG/'formal-freeze-intake-and-exit-matrix.json')
    assert matrix['only_precise_authorization_remaining']==[]
    assert matrix['formal_scope']['actual_N'] is None and matrix['formal_scope']['candidate_content_read'] is False
    assert materials.finalize(ROOT,junit_path=xml)==result
