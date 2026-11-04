"""Frozen experiment scope, first-trial reuse and shared-budget regression."""
import json
from pathlib import Path
from copy import deepcopy
import pytest
from scripts import prepare_m63_retest as prep
from eval.m6_1.grants import formal_runtime_digest

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'docs/eval/m6.3-repair-ab-r3-20261102/task-config.json'

@pytest.fixture
def package(tmp_path,monkeypatch):
    cfg=prep.read(CONFIG)
    monkeypatch.setattr(prep,'OUT',tmp_path);monkeypatch.setattr(prep,'TASK_CONFIG',cfg)
    monkeypatch.setattr(prep,'TASK',cfg['task_id']);monkeypatch.setattr(prep,'LEDGER',cfg['ledger_directory'])
    prep.design()
    return prep.read(tmp_path/'design.json')

def test_same_100_for_both_arms_and_interleaved_order(package):
    main=package['phases']['main'];metadata={r['case_id']:r for r in package['trials']}
    assert len(main)==200
    for offset in range(0,200,2):
        a,b=main[offset:offset+2];x,y=metadata[a['case_id']],metadata[b['case_id']]
        assert {x['variant'],y['variant']}=={'A','B'} and x['source_case_id']==y['source_case_id']
        assert {**a,'case_id':'same'}=={**b,'case_id':'same'}
        assert x['architecture']!=y['architecture']

def test_prespecified_24_three_trials_have_identical_inputs(package):
    meta={x['case_id']:x for x in package['trials']};groups={}
    for phase in ('main','repeats'):
        for c in package['phases'][phase]:
            m=meta[c['case_id']]
            if m['source_case_id'] in package['selected_24']:
                groups.setdefault((m['source_case_id'],m['variant']),[]).append((m['round'],{**c,'case_id':'same'}))
    assert len(groups)==48
    for values in groups.values():
        assert sorted(n for n,c in values)==[1,2,3]
        assert values[0][1]==values[1][1]==values[2][1]

def test_200_case_request_uses_new_task_caps_not_legacy_140(package,tmp_path):
    prep.put(tmp_path/'health.json',{'runtime':formal_runtime_digest(),'active_runs':0,'pending_actions':0,'lease_free':True,'python_dependencies':{}})
    prep.put(tmp_path/'price.json',prep.read(ROOT/'docs/eval/m6.3-repair-retest-r2-20261102/price.json'))
    prep.put(tmp_path/'accounting.json',{'known_settled':True,'runs':8,'provider':9,'tokens':2000,'estimated_usd':0.02,'tools':12,'consumed_case_ids':[]})
    prep.put(tmp_path/'isolation.json',{'test_only':True})
    prep.request('main',str(tmp_path/'health.json'),str(tmp_path/'price.json'),'unit',str(tmp_path/'accounting.json'),str(tmp_path/'isolation.json'))
    request=prep.read(tmp_path/'request-main-unit.json')['config']
    assert len(request['plan']['case_bindings'])==200
    assert request['plan']['budget']['provider_calls']==1991
    assert request['plan']['budget']['estimated_usd']==pytest.approx(9.98)
    arms=request['experiment_config']['environment']['trial_architectures']
    assert list(arms.values()).count('SINGLE')==list(arms.values()).count('MULTI')==100
