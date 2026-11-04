"""Audit/report boundaries; these checks are not a semantic human reviewer."""
import copy
import pytest
from scripts import audit_m63_reliability as audit
from scripts.probe_retrieval_ablation import prepared_metrics


@pytest.fixture(scope='module')
def artifacts():
    return (audit.verified_rows(), *(audit.read(audit.OUT/name) for name in
        ('answer-reviews.json','claim-reviews.json','candidate-reviews.json')))


def test_real_220_answer_and_same_run_claim_bindings(artifacts):
    audit.validate_artifacts(*artifacts)


@pytest.mark.parametrize('field,value', [('run_id','wrong'),('raw_sha256','0'*64),('answer','invented'),('human_decision','PASS')])
def test_answer_binding_drift_rejected(artifacts,field,value):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(answers)
    changed[0][field]=value
    with pytest.raises(ValueError): audit.validate_artifacts(rows,changed,claims,candidates)


def test_claim_span_drift_rejected(artifacts):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(claims);changed[0]['text']='a different statement'
    with pytest.raises(ValueError,match='span'): audit.validate_artifacts(rows,answers,changed,candidates)


def test_cross_run_source_rejected(artifacts):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(claims);changed[0]['evidence'][0]['run_id']='another run'
    with pytest.raises(ValueError,match='cross Run'): audit.validate_artifacts(rows,answers,changed,candidates)


def test_same_run_fake_evidence_rejected(artifacts):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(claims);changed[0]['evidence'][0]['text']='forged source with same Run ID'
    with pytest.raises(ValueError,match='content or locator'): audit.validate_artifacts(rows,answers,changed,candidates)


def test_candidate_business_section_cannot_be_removed(artifacts):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(candidates)
    c=next(c for c in changed if c['sections'])
    c['sections']={}
    with pytest.raises(ValueError,match='section drift'): audit.validate_artifacts(rows,answers,claims,changed)


def test_invalid_citation_cannot_be_whole_false_rejection(artifacts):
    rows,answers,claims,candidates=artifacts
    changed=copy.deepcopy(candidates)
    c=next(c for c in changed if c['candidate']['citation_scope']=='FAIL')
    c['whole_candidate_judgment']='CONFIRMED_FALSE_REJECTION_AI'
    with pytest.raises(ValueError,match='invalid citation'): audit.validate_artifacts(rows,answers,claims,changed)


def test_no_claims_is_na_not_supported_empty_set(artifacts):
    _,answers,_,_=artifacts
    no_fact=next(a for a in answers if a['case_id']=='M63R3-main-A-r1-FC26-PRODUCT-003')
    assert no_fact['claim_ids']==[] and no_fact['claim_status']=='NO_FACTUAL_ASSERTION_NA'


def query(labels):
    return {'query_id':'q','qrels':[{'chunk_id':k,'grade':v} for k,v in labels.items()]}


def test_unknown_qrel_not_a_negative_and_no_fake_recall():
    result=prepared_metrics([{'query_id':'q','valid_mode':True,'ranking':['a']}],[query({'a':None})])
    assert result['unjudged_bounds']['p_at_1']==[0,1]
    assert result['mean_labelled_set_recall_at_5'] is None
    assert result['unjudged_retrieved']==1


def test_missing_and_degraded_arm_retain_planned_denominator():
    q=query({'a':2})
    for rows in ([],[{'query_id':'q','valid_mode':False,'ranking':['a']}]):
        result=prepared_metrics(rows,[q])
        assert result['p_at_1']['N']==1 and result['p_at_1']['n']==0
        assert result['invalid_or_missing']==1


@pytest.mark.parametrize('ranking',[['a','a'],['outside']])
def test_duplicate_or_out_of_scope_ranking_rejected(ranking):
    with pytest.raises(ValueError):
        prepared_metrics([{'query_id':'q','valid_mode':True,'ranking':ranking}],[query({'a':2})])


def test_four_real_modes_and_precommitted_complete_corpus_labels():
    bundle=audit.read(audit.OUT/'retrieval-bundle.json')
    results=audit.read(audit.OUT/'retrieval-results.json')
    assert audit.sha(audit.OUT/'retrieval-bundle.json')==results['bundle_sha256']
    assert len(bundle['queries'])==67 and sum(len(q['qrels']) for q in bundle['queries'])==536
    assert len(results['records'])==536 and results['network_attempts']==0
    for q in bundle['queries']:
        assert {x['chunk_id'] for x in q['qrels']}=={x['chunk_id'] for x in q['index']['chunks']}
        assert all(x['human_grade'] is None for x in q['qrels'])
    for row in results['records']:
        assert row['valid_mode'] and not row['result']['degraded']
        if row['mode']!='bm25': assert row['result']['models']['embedding']
        if row['mode']=='rrf_reranker': assert row['result']['models']['reranker']


def test_atom_locator_retains_negation_date_and_whole_condition():
    text='从交付日起六个月，登记不重置；不是2026年7月1日。'
    pieces=list(audit.fragments(text))
    assert all(text[start:end]==piece for piece,start,end,_ in pieces)
    assert any('登记不重置'==p[0] and '从交付日起' in p[3] for p in pieces)


def test_real_variant_inputs_are_appended_not_falsely_independent_paraphrases(artifacts):
    rows,_,_,candidates=artifacts
    variants=audit.variants(rows,candidates)
    assert len(variants)==20
    assert all(v['question'].startswith(v['original_question']) for v in variants)
    assert any(v['cause']=='PLANNING_STOPPED_AFTER_INCOMPLETE_READ' for v in variants)
    assert any(v['cause']=='VALIDATOR_FALSE_REJECTION_AI' for v in variants)
