"""Actual saved statements, not value-only or expected-answer acceptance."""
import json
from pathlib import Path
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from app.m2.self_rag import _policy_window_facts,_source_window_facts,_answer_semantic_issues

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT/'docs/eval/m6.3-personal-execution-20261101'
SAVED=json.loads((OLD/'candidate-ablation-observations-final.json').read_text(encoding='utf8'))['items']
ACCEPTED={r['run_id'] for r in json.loads((OLD/'final-stage-closeout-r1.json').read_text(encoding='utf8'))['candidate_items']
    if r['accepted_diagnostic']=='REJECTED_RELEVANT_DURATION_ASSERTIONS_SUPPORTED_ONLY'}
ROWS=[r for r in SAVED if r['run_id'] in ACCEPTED]


@pytest.mark.parametrize('row',ROWS,ids=[r['case_id'] for r in ROWS])
def test_accepted_local_window_assertions_are_not_rejected(row):
    original={f['window'] for f in row['candidate']['validation_findings'] if 'window' in f}
    issues=_answer_semantic_issues(row['candidate']['redacted_text'],row['citation_sources'])
    assert not [i for i in issues if i.get('window') in original],issues


def test_real_narrative_clause_retains_event_and_both_conditions():
    row=ROWS[0]
    answer=_policy_window_facts(row['candidate']['redacted_text'])
    source=[f for s in row['citation_sources'] for f in _source_window_facts(s)]
    claim=next(f for f in answer if f['amount']==37)
    rule=next(f for f in source if f['amount']==37)
    assert claim['subject']==rule['subject'] and claim['subject']
    assert claim['relation']==rule['relation']!='UNSCOPED'
    assert claim['start_event']==rule['start_event']==('registration',)
    assert set(claim['condition'])==set(rule['condition'])=={'commercial_use','valid_service_evidence'}


@pytest.mark.parametrize('old,new',[
    ('37天','38天'),('37天','37个月'),('登记事件','激活事件'),
    ('独立渠道条款','普通退货期限'),('商业使用','个人使用'),
    ('有效服务凭证','无效服务凭证'),
    ('，但它仅适用于商业使用且具有有效服务凭证的商品',''),
    ('37天','37天后过期'),('37天','37天内可申请退货'),
])
def test_real_local_statement_mutations_are_not_supported(old,new):
    row=ROWS[0];text=row['candidate']['redacted_text']
    assert old in text
    target='38天' if new=='38天' else '37个月' if new=='37个月' else '37天'
    findings=_answer_semantic_issues(text.replace(old,new),row['citation_sources'])
    # Other, untouched assertions may have independent findings. They cannot
    # serve as evidence that this mutation was detected.
    assert any(i.get('window')==target for i in findings),findings


def test_source_attribution_cannot_swap_a_and_b():
    evidence=[{'chunk_id':'rule-a','content':'Gift card expires after 12 months.'},
        {'chunk_id':'rule-b','content':'Gift card does not expire.'}]
    correct='Rule A says gift card expires after 12 months; Rule B says gift card does not expire. The rules conflict.'
    bad='Rule B says gift card expires after 12 months; Rule A says gift card does not expire. The rules conflict.'
    assert not _answer_semantic_issues(correct,evidence)
    assert _answer_semantic_issues(bad,evidence)
    one_wrong='Rule A says gift card expires after 12 months; Rule A says gift card does not expire. The rules conflict.'
    assert any(i['code']=='ANSWER_UNSUPPORTED_POLICY_ATTRIBUTION'
        for i in _answer_semantic_issues(one_wrong,evidence))


def test_referencing_a_window_is_distinct_from_asserting_an_unqualified_rule():
    row=ROWS[0]
    refs='另有独立渠道条款涉及以登记事件起算37天等内容；不能据此判断个人资格。'
    rule='独立渠道条款规定以登记事件起算37天。'
    assert not _answer_semantic_issues(refs,row['citation_sources'])
    assert _answer_semantic_issues(rule,row['citation_sources'])


@pytest.mark.parametrize('lead',[
    '条款说明：', '本轮来源另有规定：', '两类条款不相同。另一条款指出：', '',
])
@pytest.mark.parametrize('punctuation',[',','，','；','\n'])
def test_narrative_and_conditional_tails_preserve_the_complete_proposition(lead,punctuation):
    sources=[{'chunk_id':'live-source','content':
        '另一独立渠道条款以登记事件起算43天，仅适用于商业使用且具有有效服务凭证的商品。'}]
    text=lead+'独立渠道条款从登记事件起算43天'+punctuation+'仅适用于商业使用且具有有效服务凭证的商品。'
    assert not _answer_semantic_issues(text,sources)
    for old,new in [('43天','44天'),('登记','激活'),('商业使用','个人使用'),
            ('有效服务凭证','无效服务凭证')]:
        findings=_answer_semantic_issues(text.replace(old,new),sources)
        assert any(i.get('window')==('44天' if old=='43天' else '43天') for i in findings)


@pytest.mark.parametrize('field,old,new',[
    ('resource','P-SYN-956E3FE733148B22','OTHER-PRODUCT'),
    ('clock','购买日期','激活日期'),
    ('value','12 个月','19 个月'),
    ('condition','非耗材主体','耗材主体'),
])
def test_actual_specific_warranty_scope_cannot_borrow_another_resource(field,old,new):
    row=ROWS[2];text=row['candidate']['redacted_text']
    assert old in text
    issues=_answer_semantic_issues(text.replace(old,new),row['citation_sources'])
    assert any(i.get('window')==('19个月' if field=='value' else '12个月') for i in issues),issues


@pytest.mark.parametrize('text',[
    'The independent channel rule runs for 43 days from registration only for commercial use and valid service evidence.',
    'The independent channel clause runs for 43 days after registration only if commercial use and valid service evidence.',
])
def test_bilingual_local_rule_preserves_the_same_event_and_conditions(text):
    source=[{'chunk_id':'selected','content':'独立渠道条款以登记事件起算43天，仅适用于商业使用且具有有效服务凭证的商品。'}]
    assert not _answer_semantic_issues(text,source)
    for old,new in [('43 days','44 days'),('registration','activation'),('commercial use','personal use'),
            ('valid service evidence','invalid service evidence'),('only for','without')]:
        if old in text:
            assert any(i.get('window')==('44days' if old=='43 days' else '43days')
                for i in _answer_semantic_issues(text.replace(old,new),source))


def test_additional_condition_and_reference_negation_are_not_dropped():
    row=ROWS[0]
    text='独立渠道条款以登记事件起算37天，仅适用于商业使用且具有有效服务凭证且年满18岁的商品。'
    assert any(i.get('window')=='37天' for i in _answer_semantic_issues(text,row['citation_sources']))
    text='独立渠道条款以登记事件起算37天，无需商业使用或有效服务凭证。'
    assert any(i.get('window')=='37天' for i in _answer_semantic_issues(text,row['citation_sources']))


@pytest.mark.parametrize('row_index,old,new',[
    (0,'登记事件','激活事件'),
    (2,'P-SYN-956E3FE733148B22','OTHER-PRODUCT'),
    (26,'确认图稿前','确认图稿后'),
])
def test_saved_full_candidates_and_wrong_scope_through_actual_cli(row_index,old,new,tmp_path,monkeypatch):
    from test_m6_4_core import exercise_complete_policy_pair_cli
    row=ROWS[row_index]
    rows=[{'evidence_id':s['chunk_id'],'title':'虚构政策条款','text':s['content'],
        'version':'engineering-real-candidate-replay','effective':'2026-01-01/2026-12-31'}
        for s in row['citation_sources']]
    exercise_complete_policy_pair_cli(tmp_path,monkeypatch,rows,row['candidate']['redacted_text'],old,new)
