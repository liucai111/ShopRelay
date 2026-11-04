"""Method amendment is distinct from human blind review and asset independence."""
from copy import deepcopy
from dataclasses import replace, asdict
from datetime import timedelta
import json

import pytest

from test_m6_2_personal_dev_batch_r7 import tmp_path
from test_m6_2_r24_formal_application import make_workflow
from eval.m6_1 import formal_assets as fa
from eval.m6_1.contracts import sha256_json
from eval.m6_1.grants import formal_case_payload
from eval.m6_1.registry import DatasetAsset, RegistryError


def amended(tmp_path, monkeypatch):
    cases, legacy, now = make_workflow(tmp_path, monkeypatch)
    config=deepcopy(legacy.config)
    event={'event_id':'TEST_USER_ATTESTATION_NOT_REAL', 'recorded_at_utc':now.isoformat(),
        'human_review_status':'USER_ATTESTED_ACTUAL_REVIEW_AND_CONFIRMATION_OF_AI_ASSISTED_REFERENCES',
        'bound_files':[{'path':config['content_intake_permit']['source_path'],
                        'sha256':config['content_intake_permit']['source_sha256']}]}
    fa.write_new(tmp_path/'confirmation.json',event)
    amendment={'method_id':fa.AI_REVIEW_METHOD,'status':'OWNER_APPROVED_METHOD_AMENDMENT',
        'confirmation':{'path':'confirmation.json','sha256':fa.raw_hash(tmp_path/'confirmation.json')}}
    fa.write_new(tmp_path/'method.json',amendment)
    config.update(review_method=fa.AI_REVIEW_METHOD,method_amendment='method.json',method_amendment_sha256=fa.raw_hash(tmp_path/'method.json'))
    w=fa.AssetWorkflow(tmp_path,config)
    monkeypatch.setattr(fa,'utc_now',lambda:now)
    w.intake();w.confirm_ai_review()
    return cases,w,now


def blind_envelope(w,cases,now):
    form=w.form('blind')
    package=w.blind_package()
    path=w.root/'blind-package.json';fa.write_new(path,package)
    for r,c in zip(form['items'],cases):
        r.update(labels=formal_case_payload(c)['expected_outcome'],rationale='OFFLINE TEST AI label, not actual blind assessment')
    return {'submission':form,'context_receipt':{'assessment_context_id':'TEST_NEW_CONTEXT',
        'fresh_context':True,'prior_conversation_loaded':False,'reference_labels_loaded':False,
        'package_path':path.name,'package_sha256':fa.raw_hash(path),
        'started_at_utc':(now+timedelta(days=1)).isoformat(),
        'completed_at_utc':(now+timedelta(days=1,minutes=1)).isoformat(),'assessor_type':'CODEX_AI'}}


def test_real_confirmation_and_ai_blind_are_distinct_with_adjudication(tmp_path,monkeypatch):
    cases,w,now=amended(tmp_path,monkeypatch)
    first=json.loads((tmp_path/'day1.json').read_text(encoding='utf8'))
    assert first['reviewer']=='CHAT_USER_SELF_CONFIRMED'
    assert first['method']==fa.AI_REVIEW_METHOD
    assert 'ACTUALLY_REVIEWED' in first['reviewer_type']
    assert [r['labels'] for r in first['submission']['items']]==[formal_case_payload(c)['expected_outcome'] for c in cases]
    with pytest.raises(ValueError,match='never records AI as human'):w.accept(w.form('blind'))
    envelope=blind_envelope(w,cases,now)
    envelope['submission']['items'][0]['labels']['task']['requirements']=['Independent test disagreement']
    monkeypatch.setattr(fa,'utc_now',lambda:now+timedelta(days=1,hours=1))
    receipt=w.accept_ai_blind(envelope)
    assert receipt['reviewer_type']=='CODEX_AI_NOT_HUMAN'
    # A new workflow instance validates the stored source/context on recovery.
    w=fa.AssetWorkflow(tmp_path,w.config)
    comparison=w.compare();assert len(comparison['differences'])==1
    with pytest.raises(ValueError,match='adjudication'):w.freeze()
    result=w.freeze({'comparison_sha256':sha256_json(comparison),'decisions':[
        {'case_id':cases[0].case_id,'dimension':'task','value':formal_case_payload(cases[0])['expected_outcome']['task'],'rationale':'Test operator adjudication only'}]})
    assert result['asset']['review_status']=='HUMAN_CONFIRMED_AI_BLIND_REVIEWED'
    assert result['asset']['review_method']==fa.AI_REVIEW_METHOD
    assert 'INDEPENDENT_REVIEWED' != result['asset']['review_status']


@pytest.mark.parametrize('bad',['same_day','loaded_history','loaded_labels','package','leaked_field','scope','future','human_type'])
def test_blind_context_date_source_and_scope_rejected(tmp_path,monkeypatch,bad):
    cases,w,now=amended(tmp_path,monkeypatch);e=blind_envelope(w,cases,now)
    monkeypatch.setattr(fa,'utc_now',lambda:now+timedelta(days=1,hours=1))
    c=e['context_receipt']
    if bad=='same_day':c.update(started_at_utc=now.isoformat(),completed_at_utc=(now+timedelta(minutes=1)).isoformat())
    if bad=='loaded_history':c['prior_conversation_loaded']=True
    if bad=='loaded_labels':c['reference_labels_loaded']=True
    if bad=='package':c['package_sha256']='f'*64
    if bad=='leaked_field':
        p=tmp_path/'blind-package.json';d=json.loads(p.read_text(encoding='utf8'));d['previous_labels']=['PASS']
        p.write_text(json.dumps(d),encoding='utf8');c['package_sha256']=fa.raw_hash(p)
    if bad=='scope':e['submission']['items'].pop()
    if bad=='future':c['completed_at_utc']=(now+timedelta(days=4)).isoformat()
    if bad=='human_type':c['assessor_type']='HUMAN'
    with pytest.raises(ValueError):w.accept_ai_blind(e)
    assert not (tmp_path/'blind.json').exists()


def test_method_binding_and_recovery_label_drift_rejected(tmp_path,monkeypatch):
    cases,w,now=amended(tmp_path,monkeypatch)
    bad=deepcopy(w.config);bad['content_intake_permit']['source_sha256']='e'*64
    with pytest.raises(ValueError,match='exact asset'):fa.AssetWorkflow(tmp_path,bad)
    bad=deepcopy(w.config);bad['method_amendment_sha256']='d'*64
    with pytest.raises(ValueError,match='hash mismatch'):fa.AssetWorkflow(tmp_path,bad)
    e=blind_envelope(w,cases,now);monkeypatch.setattr(fa,'utc_now',lambda:now+timedelta(days=1,hours=1));w.accept_ai_blind(e)
    p=tmp_path/'day1.json';d=json.loads(p.read_text(encoding='utf8'));d['submission']['items'][0]['labels']['task']['requirements']=['drift']
    p.write_text(json.dumps(d),encoding='utf8')
    with pytest.raises(ValueError,match='per-item labels drifted'):w.compare()


def test_amended_review_does_not_confer_unseen_eligibility(tmp_path,monkeypatch):
    cases,w,now=amended(tmp_path,monkeypatch)
    e=blind_envelope(w,cases,now);monkeypatch.setattr(fa,'utc_now',lambda:now+timedelta(days=1,hours=1));w.accept_ai_blind(e)
    value=w.freeze()['asset'];value['independence_status']='NOT_VERIFIED'
    with pytest.raises(RegistryError,match='ELIGIBLE'):DatasetAsset(**value)
    cfg=deepcopy(w.config);cfg['intake_stage']='CANDIDATE_DRAFT'
    draft=fa.AssetWorkflow(tmp_path,cfg)
    with pytest.raises(PermissionError,match='candidate draft'):draft.freeze()


def test_actual_workflow_cli_confirmation_export_and_no_early_blind_vote(tmp_path,monkeypatch):
    cases,w,now=amended(tmp_path,monkeypatch)
    cfg=deepcopy(w.config)
    for key in ('intake_record','day1_record','blind_record'):cfg[key]='cli-'+cfg[key]
    fa.write_new(tmp_path/'workflow.json',cfg)
    def cli(action,output):
        return fa.workflow_cli(['--asset-workflow-config','workflow.json',action,'--output',output],repository_root=tmp_path)
    cli('intake','cli-intake-result.json');cli('confirm-ai-review','cli-human-result.json');cli('blind-package','cli-package.json')
    package=json.loads((tmp_path/'cli-package.json').read_text(encoding='utf8'))
    assert len(package['form']['items'])==len(cases)
    assert all(r['labels'] is None and r['rationale'] is None for r in package['form']['items'])
    assert 'ISOLATE_INJECTION' in package['rubric']['schema']['route']['properties']['safety_decision']['enum']
    assert 'expected_outcome' not in json.dumps(package)
    assert not (tmp_path/'cli-blind.json').exists()


def test_historical_legacy_config_binding_keeps_pre_amendment_meaning(tmp_path,monkeypatch):
    cases,w,now=make_workflow(tmp_path,monkeypatch)
    old_config={k:v for k,v in w.config.items() if k not in ('review_method','method_amendment','method_amendment_sha256')}
    w.intake()
    receipt=json.loads((tmp_path/'intake.json').read_text(encoding='utf8'))
    assert receipt['workflow_configuration_sha256']==sha256_json(old_config)
    recovered=fa.AssetWorkflow(tmp_path,old_config)
    assert len(recovered.form('day1')['items'])==len(cases)
