"""Source-bound offline review preparation; never starts an application Run."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/eval/m6.3-repair-ab-r3-20261102'
OUT = ROOT / 'docs/eval/m6.3-faithfulness-and-retrieval-r1-20261102'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def verified_rows():
    from scripts import report_m63_retest as report
    report.configure(SOURCE / 'task-config.json')
    report.REPORT_REVISION = 'continuation-r1'
    rows, attempts, records = report.load()
    report.existing.validate_ai(records, read(SOURCE/'ai-output-review-continuation-r1.json'))
    assert len(rows) == 324 and not attempts
    return rows


def relevance_grades(source):
    """AI content review fixed before rankings, scoped to each lawful fixture.

    IDs identify reviewed documents, not a production routing/answer rule.
    2=direct support, 1=useful applicability/condition/conflict, 0=unrelated.
    """
    family, number = source.removeprefix('FC26-').rsplit('-', 1)
    n = int(number)
    grades = {i: 0 for i in range(1, 9)}
    grades[5] = 2
    if family == 'AFTER_SALES':
        grades[5] = 1
        if n in (17, 18): grades[4] = 2
        elif n in (19, 20, 26): grades[1], grades[2] = 2, 1
        elif n in (21, 22):
            grades[3] = 2
            grades[1] = 2 if n == 22 else 1
            grades[2] = 1
    elif family == 'POLICY' and n in (40, 41, 42, 43):
        grades[6] = 2
    elif family == 'CROSS_DOMAIN':
        if n in (47, 51): grades[5], grades[7] = 1, 2
        if n == 49: grades[1], grades[2], grades[5] = 2, 1, 1
        if n == 50: grades[4] = 1
        if n == 54: grades[2] = 1
    elif family == 'EXTRA':
        if 61 <= n <= 68: grades[6] = 2
        if 61 <= n <= 65 or n in (69, 70, 71, 72, 95): grades[4] = 1
        if n in (67, 72): grades[1] = 1
        if n == 72: grades[3] = 1
        if 73 <= n <= 80: grades[7] = 1
        if n == 78: grades[6] = 1
    return grades


def prepare():
    from eval.m6_1.fixture_read_models import fixture_retrieval
    rows = verified_rows()
    design = read(SOURCE / 'design.json')
    cases = {c['case_id']: c for c in design['phases']['main']}
    selected = [r for r in rows if r['phase'] == 'main' and r['design']['variant'] == 'B'
                and r['source_case_id'] in design['cohorts']['retrieval']]
    assert len(selected) == 67
    queries = []
    for row in selected:
        case = cases[row['case_id']]
        fixture = case['input']['fixture']
        class Store:
            def rows(self, table, charge=False):
                assert table == 'policies' and charge is False
                return [{'fact': p} for p in fixture['policies']]
        at = case['evaluation_at'] if 'evaluation_at' in case else case['input'].get('evaluation_at', '2026-10-31T00:00:00Z')
        view = fixture_retrieval(Store(), fixture['knowledge_version'], at).pin_view()
        grades = relevance_grades(row['source_case_id'])
        labels = []
        for doc in view.index['chunks']:
            grade = grades[int(doc['chunk_id'].rsplit('-', 1)[1])]
            labels.append({'chunk_id': doc['chunk_id'], 'grade': grade,
                'reason': ('直接回答问题的对象/关系或明确要求比较的条款' if grade == 2 else
                           '同场景可核对的申请条件、阶段或相关保修范围；不证明个人适用资格' if grade == 1 else
                           '对象/动作/条件与本问题无直接或必要适用关系，不能仅凭相同政策词判相关'),
                'source_text': doc['content'], 'human_grade': None})
        query = row['retrieval_queries'][0]
        queries.append({'query_id': row['source_case_id'], 'query': query['query'],
            'question': row['question'], 'source_run': row['run_id'], 'archive_path': row['archive_path'],
            'archive_sha256': row['raw_sha256'], 'invocation_id': query['invocation_id'],
            'evaluation_at': view.evaluation_at, 'index': view.index,
            'fingerprint': view.fingerprint, 'qrels': labels})
    bundle = {'schema_version': 1, 'frozen_at': datetime.now(timezone.utc).isoformat(),
        'status': 'AI_REVIEWED_DEV_PENDING_HUMAN', 'confirmation_source': {
            'thread_id': '01a1803e-e8da-7ac1-921a-741806cfda36',
            'message_id': '01a19c42-ab3d-72e0-b083-14c767d95346',
            'scope': '83%目标可接受；授权离线集中审核，不是逐项标签确认'},
        'corpus_scope': '67 independent original scenario views, eight documents each; not a global 536-document store',
        'query_selection': 'first actual B-main retrieval query, fixed identically for four arms',
        'label_policy': 'grade 2 direct, 1 related conditions/conflicts, 0 unrelated; no labels inferred from arm ranks or unique expected IDs',
        'metrics_scope': 'candidate relevance against complete AI-labelled per-scenario corpus, not independent gold/required-evidence Recall',
        'source_bindings': {p.name: sha(p) for p in (SOURCE/'design.json', SOURCE/'scores-and-evidence-continuation-r1.json', SOURCE/'frozen-manifest.json')},
        'queries': queries}
    destination = OUT / 'retrieval-bundle.json'
    if destination.exists():
        raise FileExistsError('Do not relabel a frozen bundle after observing ranks')
    write(destination, bundle)
    print(json.dumps({'queries':len(queries), 'qrels':sum(len(q['qrels']) for q in queries), 'bundle_sha256':sha(destination)}, ensure_ascii=False))


def fragments(text):
    """Local assertions retain their whole sentence as subject/condition context.

    Punctuation segmentation only locates spans. It does not judge entailment.
    Dates, decimals, identifiers and quoted negation are left intact.
    """
    for sentence in re.finditer(r'[^。！？\n]+[。！？]?', text):
        for part in re.finditer(r'[^，；;]+', sentence.group()):
            raw = part.group()
            piece = raw.strip(' \r\n-。')
            if not piece: continue
            start = sentence.start() + part.start() + raw.find(piece)
            yield piece, start, start+len(piece), sentence.group()


def source_units(row):
    units = []
    for i, item in enumerate(row['reads']):
        if 'result' not in item: continue  # Retrieval is owned by policy_sources.
        def visit(value, path):
            if isinstance(value, dict):
                # A field group preserves ID/value association for human inspection.
                scalars = {k:v for k,v in value.items() if not isinstance(v,(dict,list))}
                if scalars:
                    units.append({'locator':f'/reads/{i}/result'+path,
                        'text':json.dumps(scalars,ensure_ascii=False), 'result_ref':item['result_ref'],
                        'run_id':item['run_id'], 'kind':'business_record'})
                for k,v in value.items():
                    if isinstance(v,(dict,list)): visit(v,path+'/'+k)
            elif isinstance(value, list):
                for j,v in enumerate(value): visit(v,path+'/'+str(j))
        visit(item['result'],'')
    for i, policy in enumerate(row['policy_sources']):
        units.append({'locator':f'/policy_sources/{i}', 'kind':'policy',
            'chunk_id':policy['chunk_id'], 'version':policy['version'],
            'effective_from':policy['effective_from'],'effective_to':policy['effective_to'],
            'text':policy['content'], 'run_id':row['run_id']})
    # Proof of "not queried" and utterance attribution is distinct from DB absence.
    units.append({'locator':'/reads','kind':'execution_scope','run_id':row['run_id'],
                  'text':json.dumps([{'capability':x['capability'],'status':x.get('status'),
                      'result_ref':x.get('result_ref')} for x in row['reads']],ensure_ascii=False)})
    units.append({'locator':'/question','kind':'user_statement_not_verified_business_fact',
                  'text':row['question'],'run_id':row['run_id']})
    return units


def locate_evidence(text, units):
    """Find display locators only. Similarity is NOT the verdict algorithm."""
    def grams(s):
        t = re.sub(r'\s+', '', s.lower())
        return {t[i:i+2] for i in range(len(t)-1)}
    terms=grams(text)
    def rank(unit):
        other=grams(unit['text'])
        return len(terms & other)/max(1,len(terms))
    ordered=sorted(units,key=rank,reverse=True)
    return ordered[:3]


def annotate():
    """Materialize this session's AI evidence review, not a generic auto-grader.

    The reviewer read all 200 main outputs and 20 variants with original sources.
    Per-span exceptions below are documented evidence annotations, never imported
    by production routing or answer validation. They do not change old Task labels.
    """
    rows=verified_rows()
    selected=[r for r in rows if r['phase'] in ('main','variants')]
    claims=[]; answers=[]; candidates=[]
    no_facts={'PRODUCT-003','PRODUCT-007','PRODUCT-008','ORDER-015','ORDER-016',
        'AFTER_SALES-025','AFTER_SALES-027','POLICY-044','POLICY-045','POLICY-046',
        'SAFETY-055','SAFETY-056','SAFETY-057','SAFETY-058','SAFETY-059',
        'EXTRA-098','EXTRA-099','EXTRA-100'}
    # Explicit surface-level AI findings. Missing required content is reported in
    # completeness, rather than invented as a contradicted factual assertion.
    exceptions=[
        ('main','A','PRODUCT-006','可售状态为 AVAILABLE','UNSUPPORTED','目录AVAILABLE证明结果可读，不是库存可售状态；本Run无check_inventory。'),
        ('main','B','PRODUCT-006','库存状态 AVAILABLE','UNSUPPORTED','get_product_detail未返回库存事实，不能将目录availability转成库存状态。'),
        ('main','B','PRODUCT-004','价格在目录中均为 3222','INDETERMINATE','原结果price_cents=3222；正文未写最小金额单位，不能判断读者采用何种单位。'),
        ('main','B','CROSS_DOMAIN-054','金额单位/币种未知','CONTRADICTED','币种未知，但paid_cents契约已确定最小金额分值，二者不能一起称未知。'),
        ('main','A','CROSS_DOMAIN-048','数量含义为件','CONTRADICTED','availability是枚举可读状态，不是件数，目录结果没有此数量字段。'),
        ('main','A','AFTER_SALES-021','该归属与未付款状态尚未独立核验','INDETERMINATE','本人owner已在get_order实际边界核验；未付款状态仍不能由null全部证明，此复合句范围不清。'),
        ('main','A','EXTRA-064','也不适用于个人消费者资格判断','INDETERMINATE','来源限制无渠道事实时不得判断资格，绝对排除个人消费者是更强的表述。'),
        ('main','B','EXTRA-062','不能套用于个人','INDETERMINATE','不能移用到个人退货，不等于所有个人商业服务申请均被排除，正文范围省略。'),
        ('main','A','EXTRA-065','不能套用于个人','INDETERMINATE','来源要求渠道事实，不能由其推出对所有个人情形的绝对禁止。'),
    ]
    # Review of the complete refused candidate, including saved business section.
    uncertain={'A-POLICY-038':'29天另渠道缺登记起点和商业/凭证条件，不能仅凭4个月正确认定整份误拒。',
        'B-POLICY-043':'核心7天/包装条件有据，但新增有效期止端是否排除未说明，整份仍有独立时间表达歧义。',
        'A-EXTRA-068':'将通用手续费实体条件与独立商业窗口并用，跨渠道适用关系仍需人工核定。',
        'B-EXTRA-068':'两条费用条件可分别说明，但是否属于同一申请的实体条件/窗口关系未被来源明确解决。',
        'A-EXTRA-074':'18/24个月分别有文，但主体映射与脱敏商品标识限制阻止整份确定性判断。',
        'B-EXTRA-074':'两份主体期限的对象关系和脱敏ID未完全可重建，不能把局部支持提升为整份误拒。'}
    matched=set()
    for row in selected:
        short=row['source_case_id'].removeprefix('FC26-'); arm=row['design']['variant']
        units=source_units(row)
        key=f'{arm}-{short}'
        state={'case_id':row['case_id'],'run_id':row['run_id'],'phase':row['phase'],'arm':arm,
            'source_case_id':row['source_case_id'],'archive_path':row['archive_path'],
            'raw_sha256':row['raw_sha256'],'runtime':row['runtime'],
            'delivered':row['answer'] is not None,'answer':row['answer'],
            'semantic_reviewer':None,'human_decision':None,'review_method':'AI-assisted evidence review, not independent gold'}
        answer_claims=[]
        if row['answer'] is not None and short not in no_facts:
            for text,start,end,context in fragments(row['answer']):
                # Speech acts and headings are not factual business assertions.
                if (len(text)<5 or text.startswith(('请','建议','如需','若需','如果你','如果您','待补充','待确认字段',
                    '需您','退款草案','退货申请草案','人工处理草案','关于您','以下为','可给出的待确认草案',
                    '【','1）','2）','3）','4）')) or text.endswith(('：',':','？','?'))):
                    continue
                label='SUPPORTED'; reason='对照同Run实读/政策原文，主体、数值/关系及句内条件成立；不等于完整Task已完成。'
                if '2027-01-01' in text and not any(s in context for s in ('排除','不含','零点','不得包含')):
                    label='INDETERMINATE'; reason='来源有效止端为2027-01-01T00:00:00Z排除；“至”未明确包含性/时区，不能自动判等价或矛盾。'
                for phase,a,c,needle,verdict,why in exceptions:
                    if (phase,a,c)==(row['phase'],arm,short) and needle in text:
                        label,reason=verdict,why;matched.add((phase,a,c,needle))
                cid=f"{row['case_id']}:delivered:{len(answer_claims)+1}"
                claim={'claim_id':cid,'case_id':row['case_id'],'run_id':row['run_id'],
                    'layer':'delivered','text':text,'span':[start,end],'sentence_context':context,
                    'claim_kind':('attributed_user_statement_not_verified_business_fact' if any(s in text for s in ('自述','您所述','你指定','用户主张')) else
                                  'source_or_execution_assertion'),
                    'subject_and_conditions':'inherit only from displayed sentence and same-row question, never another Run',
                    'judgment':label,'reason':reason,'evidence':locate_evidence(context,units),
                    'all_source_scope':'answer-reviews.json same Run; all original reads and policy_sources remain bound',
                    'human_judgment':None}
                claims.append(claim);answer_claims.append(claim)
        state['claim_ids']=[c['claim_id'] for c in answer_claims]
        state['claim_counts']=dict(Counter(c['judgment'] for c in answer_claims))
        state['claim_status']='NOT_DELIVERED' if row['answer'] is None else ('NO_FACTUAL_ASSERTION_NA' if not answer_claims else 'AI_REVIEWED')
        state['reads']=row['reads'];state['policy_sources']=row['policy_sources'];state['requirements']=row['requirements']
        state['source_locator_inventory']=units
        state['post_confirmation_output']=row.get('post_confirmation_output',[])
        state['post_confirmation_scope']='separate scripted owner/API outputs, excluded from initial-chat claim denominator'
        state['completeness_note']= ('缺登记必要前提；购买起点本身有据，遗漏不等于虚构事实。' if arm=='B' and short=='EXTRA-073' else
            '忠实度不替代必需工具、动作草案或完整任务评分；旧Task保持原值。')
        answers.append(state)
        if row['candidate']:
            candidate=row['candidate'];sections=row['candidate_sections'] or {}
            full='\n'.join(s['text_redacted'] for s in sections.values()) if sections else candidate['redacted_text']
            result='CONFIRMED_FALSE_REJECTION_AI'; reason='完整候选及引用支持本次断言，保留条件/未知资格；未发现独立安全或内容阻塞。'
            if key in uncertain and row['phase']=='main': result,reason='INDETERMINATE_WHOLE_CANDIDATE',uncertain[key]
            if candidate['citation_scope']!='PASS': result,reason='INDEPENDENT_BLOCKER','原拒绝包含当前证据外引用；现存合法子集/合理文本不能证明原全部引用有效，且缺必需检索。'
            if row['phase']=='variants' and key=='B-EXTRA-062':
                result,reason='INDETERMINATE_WHOLE_CANDIDATE','七天作为禁止移用的反向比较，但只引用05/06；实际7天来源04未引，不能用局部36天支持认定整份误拒。'
            if row['phase']=='variants' and key=='A-EXTRA-079':
                result,reason='INDEPENDENT_COMPLETENESS_BLOCKER','六个月及交付起点有据，但变体新增全新18个月起点问题未明确答未知；门禁对6个月局部可能误判，不代表整份完成请求。'
            obj={**{k:state[k] for k in ('case_id','run_id','phase','arm','source_case_id','archive_path','raw_sha256')},
                'candidate':candidate,'sections':sections,'full_candidate':full,
                'is_delivered':False,'whole_candidate_judgment':result,'reason':reason,
                'scope_limit':'standalone policy candidate is complete for SINGLE; AGENT includes saved business+policy sections',
                'source_scope':units,'human_judgment':None}
            candidates.append(obj)
            for text,start,end,context in fragments(full):
                label='SUPPORTED';why='局部断言与所引同Run条款/业务记录吻合；整体拒绝判定另查独立阻塞。'
                ambiguous = (('2027-01-01' in text and not any(s in context for s in ('排除','不含','零点')))
                    or (key=='A-POLICY-038' and '29天' in text)
                    or (short=='EXTRA-074' and ('REDACTED' in text or '24个月' in text))
                    or (short=='EXTRA-068' and any(s in text for s in ('同时满足','实体条件','两条条件')))
                    or (row['phase']=='variants' and key=='B-EXTRA-062' and '七天' in text))
                if ambiguous: label='INDETERMINATE';why=reason
                claims.append({'claim_id':f"{row['case_id']}:candidate:{start}",'case_id':row['case_id'],
                    'run_id':row['run_id'],'layer':'rejected_candidate','text':text,'span':[start,end],
                    'sentence_context':context,'judgment':label,'reason':why,
                    'evidence':locate_evidence(context,units),'human_judgment':None})
    # A missing span match signals annotation drift, not silent exception omission.
    expected={(p,a,c,n) for p,a,c,n,_,_ in exceptions}
    if matched != expected: raise ValueError(f'annotation surface drift: {expected-matched}')
    write(OUT/'claim-reviews.json',claims)
    write(OUT/'answer-reviews.json',answers)
    write(OUT/'candidate-reviews.json',candidates)
    print(json.dumps({'answers':len(answers),'claims':len(claims),'rejected_candidates':len(candidates)}))


def ratio(n, N):
    from scripts.probe_retrieval_ablation import wilson
    return {'n':n,'N':N,'value':n/N if N else None,'wilson_95_descriptive':wilson(n,N) if N else None}


def audit_metrics(answers, claims, candidates, phase, arm):
    selected=[a for a in answers if a['phase']==phase and a['arm']==arm]
    ids={a['case_id'] for a in selected}
    cs=[c for c in claims if c['case_id'] in ids and c['layer']=='delivered']
    counts=Counter(c['judgment'] for c in cs)
    fact_answers=[a for a in selected if a['claim_ids']]
    delivered=[a for a in selected if a['delivered']]
    bad=[a for a in fact_answers if a['claim_counts'].get('UNSUPPORTED',0)+a['claim_counts'].get('CONTRADICTED',0)>0]
    all_supported=[a for a in fact_answers if set(a['claim_counts'])=={'SUPPORTED'}]
    rejected=[c for c in candidates if c['case_id'] in ids]
    rc=Counter(c['whole_candidate_judgment'] for c in rejected)
    return {'planned_N':len(selected),'delivery':ratio(len(delivered),len(selected)),
        'factual_answer_N':len(fact_answers),'delivered_no_fact_NA':len(delivered)-len(fact_answers),
        'claims':dict(counts),'micro_support_all_reviewed':ratio(counts['SUPPORTED'],len(cs)),
        'micro_support_determinate':ratio(counts['SUPPORTED'],len(cs)-counts['INDETERMINATE']),
        'macro_per_answer_support':sum(a['claim_counts'].get('SUPPORTED',0)/sum(a['claim_counts'].values()) for a in fact_answers)/len(fact_answers) if fact_answers else None,
        'fully_supported_factual_answers':ratio(len(all_supported),len(fact_answers)),
        'answers_with_unsupported_or_contradicted_among_delivered':ratio(len(bad),len(delivered)),
        'answers_with_unsupported_or_contradicted_among_factual':ratio(len(bad),len(fact_answers)),
        'rejected_candidate_counts':dict(rc),
        'confirmed_false_reject_fraction_among_rejected_AI':ratio(rc['CONFIRMED_FALSE_REJECTION_AI'],len(rejected)),
        'false_rejection_rate_all_legal_candidates':None,
        'false_rejection_rate_reason':'全体完整合法候选分母未建立；局部事实支持不等于任务完整/全部引用合法。',
        'counterfactual_fixed_candidate_content_table':{
            'accepted_all_claims_supported':len(all_supported),
            'accepted_unsupported_or_contradicted':len(bad),
            'accepted_indeterminate_without_known_bad':len(fact_answers)-len(all_supported)-len(bad),
            'accepted_no_factual_claim_NA':len(delivered)-len(fact_answers),
            'rejected_whole_valid_AI':rc['CONFIRMED_FALSE_REJECTION_AI'],
            'rejected_independent_blocker':rc['INDEPENDENT_BLOCKER']+rc['INDEPENDENT_COMPLETENESS_BLOCKER'],
            'rejected_indeterminate':rc['INDETERMINATE_WHOLE_CANDIDATE']}}


def variants(rows, candidates):
    base={(r['source_case_id'],r['design']['variant']):r for r in rows if r['phase']=='main'}
    reviews={x['case_id']:x for x in candidates}
    result=[]
    for r in rows:
        if r['phase']!='variants': continue
        original=base[r['source_case_id'],r['design']['variant']]
        short=r['source_case_id'].removeprefix('FC26-'); arm=r['design']['variant']
        if not r['answer']:
            c=reviews[r['case_id']]
            cause='VALIDATOR_FALSE_REJECTION_AI' if c['whole_candidate_judgment']=='CONFIRMED_FALSE_REJECTION_AI' else (
                'PLANNING_MISSING_RETRIEVAL_AND_CITATION_GUARD' if c['whole_candidate_judgment']=='INDEPENDENT_BLOCKER' else
                'LOCAL_VALIDATION_SUSPECT_WITH_INDEPENDENT_COMPLETENESS_OR_SCOPE_LIMIT')
            rationale=c['reason']
        elif arm=='A' and short in ('PRODUCT-001','ORDER-012','AFTER_SALES-028'):
            cause='PLANNING_STOPPED_AFTER_INCOMPLETE_READ'
            rationale='有真实首项读取，但未完成另一商品/物流/售后阶段的必需读取；诚实说明未知不等于完成明确请求。'
        else:
            cause='NO_TASK_FAILURE_OBSERVED'
            rationale='本次所需实读或合法澄清完成；未因RULE路径或无事实断言就宣称模型自主成功。'
        result.append({'case_id':r['case_id'],'run_id':r['run_id'],'source_case_id':r['source_case_id'],
            'arm':arm,'question':r['question'],'answer':r['answer'],
            'original_question':original['question'],'original_case_id':original['case_id'],
            'original_run_id':original['run_id'],'original_answer':original['answer'],
            'input_change':'原题追加表达/要求，不是独立替换式纯同义问法；EXTRA079特别增加全新产品起点要求',
            'cause':cause,'reason':rationale,'actual_capabilities':[x['capability'] for x in r['reads']],
            'retrieval_state':('RETRIEVED_SAME_RUN' if r['policy_sources'] else 'NOT_ATTEMPTED_OR_NO_EVIDENCE'),
            'candidate_audit_ref':r['case_id'] if r['candidate'] else None,
            'format_issue':'literal backslash-n visible in business section' if r['answer'] and '\\n' in r['answer'] else None,
            'human_decision':None})
    return result


def validate_artifacts(rows, answers, claims, candidates):
    expected={r['case_id']:r for r in rows if r['phase'] in ('main','variants')}
    if len(answers)!=220 or {a['case_id'] for a in answers}!=set(expected): raise ValueError('answer set mismatch')
    for a in answers:
        r=expected[a['case_id']]
        if (a['run_id'],a['raw_sha256'],a['answer'])!=(r['run_id'],r['raw_sha256'],r['answer']): raise ValueError('Run/archive/answer binding drift')
        if a['human_decision'] is not None: raise ValueError('new human decision not authorized')
        if a['reads']!=r['reads'] or a['policy_sources']!=r['policy_sources']:
            raise ValueError('projected original evidence drift')
    by_id={c['case_id']:c for c in candidates}
    original_units={key:{unit['locator']:unit for unit in source_units(r)} for key,r in expected.items()}
    for c in claims:
        r=expected[c['case_id']]
        text=r['answer'] if c['layer']=='delivered' else by_id[c['case_id']]['full_candidate']
        start,end=c['span']
        if c['run_id']!=r['run_id'] or text[start:end]!=c['text']: raise ValueError('claim span binding drift')
        for unit in c['evidence']:
            if unit['run_id']!=r['run_id']: raise ValueError('cross Run evidence')
            if original_units[c['case_id']].get(unit['locator'])!=unit:
                raise ValueError('same Run evidence content or locator drift')
        if c['judgment'] not in {'SUPPORTED','CONTRADICTED','UNSUPPORTED','INDETERMINATE'}: raise ValueError('invalid claim judgment')
    for c in candidates:
        r=expected[c['case_id']]
        if c['candidate']!=r['candidate'] or c['is_delivered'] is not False: raise ValueError('candidate projection drift')
        sections=r['candidate_sections'] or {}
        full='\n'.join(s['text_redacted'] for s in sections.values()) if sections else r['candidate']['redacted_text']
        if c['sections']!=sections or c['full_candidate']!=full: raise ValueError('candidate section drift')
        if c['whole_candidate_judgment']=='CONFIRMED_FALSE_REJECTION_AI' and r['candidate']['citation_scope']!='PASS':
            raise ValueError('invalid citation cannot be classified whole false rejection')


def report(check=False):
    from scripts.probe_retrieval_ablation import prepared_metrics
    rows=verified_rows()
    answers=read(OUT/'answer-reviews.json');claims=read(OUT/'claim-reviews.json');candidates=read(OUT/'candidate-reviews.json')
    validate_artifacts(rows,answers,claims,candidates)
    bundle=read(OUT/'retrieval-bundle.json');retrieval=read(OUT/'retrieval-results.json')
    if sha(OUT/'retrieval-bundle.json')!=retrieval['bundle_sha256']: raise ValueError('qrels changed after retrieval')
    if retrieval['network_attempts'] or any(not r['valid_mode'] for r in retrieval['records']): raise ValueError('mode degraded or network attempted')
    for mode, summary in retrieval['summary'].items():
        calculated=prepared_metrics([r for r in retrieval['records'] if r['pass']=='warm' and r['mode']==mode],bundle['queries'])
        if any(summary[k]!=v for k,v in calculated.items()): raise ValueError('retrieval metrics inconsistent with actual rankings and frozen labels')
    summary={'schema_version':1,'status':'AI_ASSISTED_DEV_REVIEW_PENDING_HUMAN',
        'scope':{'main':200,'variants':20,'repeats':'8 rejected policy candidates corroborate causes, not pooled into main'},
        'old_task_labels_unchanged':{arm:dict(Counter(item['task_outcome'] for item in read(SOURCE/'ai-output-review-continuation-r1.json')['items']
            if item['case_id'].startswith(f'M63R3-main-{arm}-'))) for arm in 'AB'},
        'groups':{f'{phase}-{arm}':audit_metrics(answers,claims,candidates,phase,arm) for phase in ('main','variants') for arm in 'AB'},
        'retrieval':retrieval['summary'],'external_calls':{'Provider':0,'Judge':0,'API_Embedding':0,'application_Run':0},
        'local_BGE_inference':True,'old_usage_UNKNOWN_preserved':True,
        'semantic_labels':'AI-assisted, not new Human decisions or Gold',
        'claim_metric_limit':'punctuation-local claims with full sentence scope; correlated claims/families, stylistic verbosity changes micro weights; Wilson descriptive only',
        'counterfactual_limit':'saved-candidate content audit, not a new end-to-end no-validator A/B; accepted task completeness remains separate'}
    if check:
        if read(OUT/'summary.json')!=summary: raise ValueError('summary recomputation mismatch')
        if read(OUT/'variant-reviews.json')!=variants(rows,candidates): raise ValueError('variant projection mismatch')
        print(json.dumps({'status':'MATCH','raw_trials_verified':len(rows),'answers_verified':len(answers),'claims_verified':len(claims),'qrels':536,'actual_rankings':len(retrieval['records']),'network_attempts':0}))
        return
    write(OUT/'summary.json',summary)
    write(OUT/'variant-reviews.json',variants(rows,candidates))
    # Repeat evidence is observational corroboration, never added to main N.
    write(OUT/'repeat-corroboration.json',[{k:r[k] for k in ('case_id','run_id','raw_sha256','archive_path','candidate','candidate_sections','policy_sources','reads','execution_diagnostics','usage','cleanup')}
        for r in rows if r['phase']=='repeats' and (r['candidate'] or r['source_case_id']=='FC26-ORDER-010' and r['answer'] is None)])
    print(json.dumps({'status':'BUILT','groups':{k:{'delivery':v['delivery'],'claims':v['claims']} for k,v in summary['groups'].items()}},ensure_ascii=False))


def publish():
    from scripts.probe_retrieval_ablation import prepared_metrics, latency
    summary=read(OUT/'summary.json'); answers=read(OUT/'answer-reviews.json')
    claims=read(OUT/'claim-reviews.json'); candidates=read(OUT/'candidate-reviews.json')
    bundle=read(OUT/'retrieval-bundle.json'); retrieval=read(OUT/'retrieval-results.json')
    variant=read(OUT/'variant-reviews.json')
    per_query=[]
    for q in bundle['queries']:
        for arm in retrieval['summary']:
            item=next(r for r in retrieval['records'] if r['query_id']==q['query_id'] and r['mode']==arm and r['pass']=='warm')
            per_query.append({'query_id':q['query_id'],'mode':arm,**prepared_metrics([item],[q])})
    direct_queries=[{**q,'qrels':[{**x,'grade':2 if x['grade']==2 else 0} for x in q['qrels']]} for q in bundle['queries']]
    analysis={'per_query':per_query,'direct_support_only_sensitivity':{
        arm:prepared_metrics([r for r in retrieval['records'] if r['mode']==arm and r['pass']=='warm'],direct_queries) for arm in retrieval['summary']},
        'cold_index_pass_latency':{arm:latency([r['wall_ms'] for r in retrieval['records'] if r['mode']==arm and r['pass']=='cold_index']) for arm in retrieval['summary']},
        'label_counts':dict(Counter(str(x['grade']) for q in bundle['queries'] for x in q['qrels'])),
        'model_load':retrieval['models'],'cpu_seconds':retrieval['cpu_seconds'],'elapsed_seconds':retrieval['elapsed_seconds'],
        'rss_end_mb':retrieval['rss_end_mb'],'network_attempts':retrieval['network_attempts']}
    write(OUT/'retrieval-analysis.json',analysis)
    priorities=[
        {'priority':1,'cause':'局部语义门禁错误拒绝有据的期限、起点、否定对照和申请阶段',
         'evidence':['B-main-POLICY-030','B-main-POLICY-036','A/B-main-EXTRA-094','A/B-main-EXTRA-096','A/B-variant-POLICY-030/043'],
         'strength':'原完整候选+引用+同Run来源强；主集14/21建议整份误拒，其余不强行归入',
         'cost':'中；需局部断言结构而非继续添原句关键词；本轮不实施',
         'minimal_regression':'保留原完整句；正例付款后12小时且图稿前、两个主体12个月/90天、日历日期否定；反例错对象/期限/起点/条件/否定翻转/未引来源，必须继续拒绝或明确无法判定'},
        {'priority':2,'cause':'已获授权的多步骤读取提前结束；不是检索器漏召回',
         'evidence':['A-variant-PRODUCT-001仅一详情','A-variant-ORDER-012仅get_order','A-variant-AFTER_SALES-028仅get_order','A-main-CROSS_DOMAIN-047/048/050/051/052'],
         'strength':'实际工具序列与最终自述未读取一致；失败不需重复生成才能确认',
         'cost':'中；让当前计划检查未完成任务需求，不注入预期工具或扩大权限',
         'minimal_regression':'自然输入两商品比较/本人物流/售后阶段分别完成合法读取；缺ID先澄清、无权限零调用；不能把诚实说未知当完整成功'},
        {'priority':3,'cause':'来源类型、金额/库存单位在可见回答中被混同',
         'evidence':['A/B-main-PRODUCT-006目录AVAILABLE转库存/可售','A-main-CROSS_DOMAIN-048 availability当数量','B-main-CROSS_DOMAIN-054分值单位和币种一起称未知','B-main-PRODUCT-004价格3222漏分值'],
         'strength':'同Run原字段可核对；确定错误与单位歧义已分列，未统称模型幻觉',
         'cost':'低至中；沿现有结果类型和格式契约保留语义，不添加并行校验层',
         'minimal_regression':'目录AVAILABLE不证明库存；有观测/缺观测正反例；paid_cents只转换数量级且币种仍未知；明确非金额数量'},
        {'priority':4,'cause':'条件完整性与跨渠道适用范围表达不稳',
         'evidence':['B-main-EXTRA-073漏先登记','A-main-AFTER_SALES-017草案漏附件/完好','A-main-POLICY-038另一渠道漏起点和商业/凭证','A/B-main-EXTRA-068适用关系待核'],
         'strength':'来源必需条件可直接核对；适用关系证据不足部分保留UNKNOWN',
         'cost':'中；不要把独立渠道限制写成一般消费者绝对禁止',
         'minimal_regression':'原完整条件齐全及本轮同义改写；删除登记/附件/商业凭证应降完整性；不能因单个期限匹配就PASS整答'}]
    write(OUT/'repair-priorities.json',priorities)
    lines=['# 第三轮答案忠实度、误拒、变体与四路检索审核','',
        '用户已接受83%作为本轮目标，不再为85%追加采样。该接受不是1961条局部断言或qrels的人工签署。审核来源为聊天消息01a19c42-ab3d-72e0-b083-14c767d95346。',
        '', '本轮新增应用Run、Provider、Judge、API Embedding均0；运行真实本地BGE向量与CrossEncoder，不等于没有模型计算。生产回答/路由/校验、演示与正式Gate不变。原324记录、旧Task79/83、关闭许可及唯一usage UNKNOWN均保持。',
        '', '## 主集200份实际结果','',
        '| 指标 | A | B |','|---|---:|---:|']
    ga,gb=(summary['groups']['main-'+a] for a in 'AB')
    def rate(x): return f"{x['n']}/{x['N']} = {x['value']:.2%}" if x['N'] else 'N/A'
    for name,key in [('实际交付','delivery'),('局部断言支持率（含无法判定分母）','micro_support_all_reviewed'),
                     ('含无依据/矛盾的已交付回答','answers_with_unsupported_or_contradicted_among_delivered'),
                     ('全部局部断言有据的事实回答','fully_supported_factual_answers'),
                     ('被拒候选中建议整份误拒','confirmed_false_reject_fraction_among_rejected_AI')]:
        lines.append(f'|{name}|{rate(ga[key])}|{rate(gb[key])}|')
    lines += ['|旧完整Task严格PASS（不改）|79/100|83/100|',
        f"|有事实断言的已交付回答|{ga['factual_answer_N']}|{gb['factual_answer_N']}|",
        f"|已交付但无可核实业务断言（N/A）|{ga['delivered_no_fact_NA']}|{gb['delivered_no_fact_NA']}|",'',
        'A的689条局部断言：680支持、1无依据、1矛盾、7无法判定。B的790条：778支持、1无依据、1矛盾、10无法判定。全部事实回答逐答平均支持率A98.71%、B97.76%；不能从微观率推断B更忠实。其他20变体单列，剩余候选部分构成总1961条局部断言。',
        '', '局部断言按逗号/分号与句末定位，并保留整句主体、条件和否定上下文；不是每个字符串片段都独立无条件成立。标题/请求、纯澄清/拒绝不靠空集合PASS进入支持率；用户自述不是核实的业务事实。每条保存原文跨度、同Run来源定位和理由。source定位相似度仅帮助展示，不是语义判分算法。新文件是本次AI证据审核，不宣称实现了通用自动语义评分器。',
        '', '支持率受措辞长度和断言划分影响。Wilson区间随各指标存summary.json，仅描述性；断言同答相关、100题来自24语义族，不当独立随机样本泛化。1−支持率包含无法判定，不能叫幻觉率。缺条件的完整性问题单列，未补造为“错误事实”。',
        '', '需要优先复核的可见问题：双方PRODUCT-006把目录AVAILABLE称库存/可售；A CROSS_DOMAIN-048给availability附件数语义；B CROSS_DOMAIN-054把已知分值单位和未知币种混称未知。B PRODUCT-004价格漏单位、若干“至2027-01-01”未说明半开边界，以及个人/商业适用范围绝对化均为INDETERMINATE，未强判矛盾。全部条目和完整问题/答案见answer-reviews.json、claim-reviews.json。',
        '', '## 拒绝候选及固定候选机制分析','',
        '主集21份未交付均存在受控候选：14份建议整份误拒、6份整体无法确定、1份有独立引用/检索阻塞；A分别4/3/1，B10/3/0。整份检查含已保存business_answer与policy_answer，非仅挑有据政策句。',
        '', '| 实际门禁 | 全部已审事实有据/候选整体合理 | 有独立无据/矛盾或阻塞 | 无法确定 | 无事实断言N/A |',
        '|---|---:|---:|---:|---:|','|接受并交付|122|4|17|36|','|拒绝未交付|14|1|6|0|',
        '', '接受行评价可见断言支持，不代表所有Task完整；拒绝行增加整份候选与引用阻塞检查。因此这不是合法候选的统一2×2准确率，更不是关闭校验后的真实端到端实验。已拒中建议误拒14/21=66.67%；全体合法完整候选分母未建立，真正false-rejection rate保持N/A。不能把14份自动改成历史Task PASS。',
        '', '6份不强判误拒：A POLICY-038的29天缺独立渠道条件；B POLICY-043有止端表达歧义；A/B EXTRA-068跨渠道实体条件/窗口关系未解决；A/B EXTRA-074有主体归属与脱敏标识限制。A CROSS_DOMAIN-053原引用越界且未检索，现存合理文字不能消除独立阻塞。各候选完整原文、来源和逐项理由见candidate-reviews.json。',
        '', '## 20个追加表达与重复证据','',
        '旧Task结果A2/10、B6/10不改。实际交付A5/10、B6/10。9份拒绝中6份建议整份误拒，1份引用/检索独立阻塞，1份新增要求未答完整，1份否定参照7天的来源范围未确定。A另外3份已交付FAIL均是缺必需读取，非“检索找不到”。',
        '', '重要设计限制：10个变体全部是在原问题后追加表达，不是替换式纯同义问法；EXTRA-079新增全新产品起算问题，改变了显式任务完整性要求。故2/10、6/10不是无条件的纯paraphrase鲁棒性估计。成功变体也核对；B CROSS_DOMAIN-050虽保留事实和补问，但显示字面\\n，另记格式缺陷。',
        '', '| 变体 | A主要归因 | B主要归因 |','|---|---|---|']
    for source in dict.fromkeys(v['source_case_id'] for v in variant):
        pair={v['arm']:v for v in variant if v['source_case_id']==source}
        lines.append(f"|{source}|{pair['A']['cause']}|{pair['B']['cause']}|")
    lines += ['', '8份重复集政策拒绝再次出现12小时、7天、保修与否定阶段边界，支持公共解析问题；两份无正文/引用越界保留独立阻塞。唯一旧未知Provider和一个已知零工具schema失败仍分列，不归成检索/表达问题。重复证据用于共因佐证，不混主集分母。',
        '', '## 同范围本地检索四路对照','',
        '67题固定使用B-main第一条真实应用query（不是为各臂优化的问题）。每题原合法语料8条、相同评测时间和时效过滤；总536个query-doc格。保持67个场景隔离，不能把它称为一个536条全库辨别或大规模性能试验。AI先逐题核对内容后冻结：直接支持80、相关条件/冲突41、不相关415，0个未标注；等价条款允许多个ID。标签为AI开发参考，不是用户确认qrels或独立Gold。',
        '', '冻结bundle SHA256：`'+sha(OUT/'retrieval-bundle.json')+'`。实际结果绑定此哈希，排名产生后未改标签。两轮共536次本地retrieval invocation（67×4×冷索引/热查询），非536个应用Run或外部模型请求。',
        '', '| 方法 | P@1 | P@5 | 标注集合Recall@3 | Recall@5 | MRR@5 | P50/P95 ms |','|---|---:|---:|---:|---:|---:|---:|']
    for arm,m in retrieval['summary'].items():
        lines.append(f"|{arm}|{m['p_at_1']['n']}/67={m['p_at_1']['value']:.2%}|{m['mean_p_at_5']:.2%}|{m['mean_labelled_set_recall_at_3']:.2%}|{m['mean_labelled_set_recall_at_5']:.2%}|{m['mrr_at_5']:.4f}|{m['p50_ms']:.2f}/{m['p95_ms_nearest_rank']:.2f}|")
    lines += ['', '每臂67/67真实模式有效，0降级、0缺失。BM25和vector各自独立召回top5，RRF仅融合各自top5（k=60），reranker重排融合候选至多10条、输出5；没有把融合池偷偷用于独立基线。min_cosine=.20；有效期采用原Fixture转换的UTC半开区间。评分计划分母67；失败保留为失败，未标注槽位支持区间而不当负例。P@5按5槽分母；Recall为当前AI标注的候选相关性，不等于正式必需证据Recall。MRR截断5。逐query分数和仅grade2敏感性见retrieval-analysis.json。',
        '', '重排序Recall@5从BM25的93.91%升至97.51%，P@1仍62/67；RRF P@1为63/67。因8条语料/场景小样本，不能宣称普遍最优。变体拒绝对应的12小时、包装7天、36天与翻新6个月条款在原Run已检索并出现在候选，主因不支持先加Query Rewrite。',
        '', f"模型仅使用缓存BGE-small@{retrieval['models']['small_revision']}及reranker@{retrieval['models']['reranker_revision']}，CPU4线程，Qdrant内存隔离，无活动索引写入。模型加载{retrieval['models']['small_load_ms']:.1f}/{retrieval['models']['reranker_load_ms']:.1f}ms；实验墙钟{retrieval['elapsed_seconds']:.2f}s、进程CPU{retrieval['cpu_seconds']:.2f}s，结束RSS{retrieval['rss_end_mb']:.1f}MiB（非峰值）。电费/硬件摊销USD UNKNOWN。Python/torch导入不含在该墙钟内。",
        '', '冷索引首轮的vector含每场景文档建向量；后续模式可复用该索引。热查询第二轮无向量建库，BM25缓存上限32小于67场景，仍含实际重建，不冒称全缓存最佳延迟。首轮各臂延迟分布、实际模型诊断和通道排名完整保留。',
        '', '## 真正优先的修复项（本轮不实施）','']
    for p in priorities:
        lines += [f"{p['priority']}. **{p['cause']}**。证据：{'；'.join(p['evidence'])}。{p['strength']}。成本：{p['cost']}。最小回归：{p['minimal_regression']}。",'']
    lines += ['可保留不足：不为83%再次提分、不进行Agent自进化或Query Rewrite堆叠、不把候选修成PASS后追溯原Task。新付费采样或部署不在本轮范围。',
        '', '## 可见输出的集中裁决项','',
        '| Run（均main） | 来源case / arm | 局部断言建议 | 具体依据 |',
        '|---|---|---|---|']
    by_case={a['case_id']:a for a in answers}
    for claim in claims:
        if claim['layer']=='delivered' and claim['judgment']!='SUPPORTED':
            answer=by_case[claim['case_id']]
            lines.append('|'+answer['run_id']+'|'+answer['source_case_id']+'/'+answer['arm']+'|'+claim['judgment']+'：'+claim['text'].replace('|','/')+'|'+claim['reason']+'|')
    lines += ['', '## 全部拒绝候选的集中建议','',
        '| 范围 / Case / arm | Run | 整份候选建议 | 原因 |',
        '|---|---|---|---|']
    for c in candidates:
        lines.append('|'+c['phase']+'/'+c['source_case_id']+'/'+c['arm']+'|'+c['run_id']+'|'+c['whole_candidate_judgment']+'|'+c['reason']+'|')
    lines += [
        '', '## 集中待审与重算','',
        '待用户集中裁决：14份主集＋6份变体整份误拒建议；6份主集＋1份变体整体不确定与1份变体完整性独立缺口；可见4份无据/矛盾及17份不确定回答；536格AI qrels是否用于后续正式报告。已有83%目标接受继续有效，不重复确认。所有新human字段空，不填reviewer/时间。',
        '', '固定入口（无外部调用）：',
        '```powershell',
        '$env:PYTHONPATH="$PWD\\.vendor\\g2;$PWD\\.vendor;$PWD"',
        'python scripts/audit_m63_reliability.py --check',
        '```','',
        '检索结果离线重算读取实际排名和冻结标签，不重复BGE计算。新检索实验才使用现有probe_retrieval_ablation.py的--prepared-bundle/--output入口；原legacy旧48题实现与报告保留，旧模式的共享融合池方法不混入新独立基线。',
        '', '完整来源验证沿既有loader对324原始payload、同Run消息/实读/政策/账本/receipt和冻结绑定核验；新审核再验证220份交付/候选、原文跨度、证据Run、引用范围和人工字段。重算一致不是独立语义审核。测试、双进程重算及历史字节保持见verification.json；SHA清单路径和说明分列。',
        '']
    (OUT/'closeout.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--annotate', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if args.prepare: prepare()
    if args.annotate: annotate()
    if args.report or args.check: report(args.check)
    if args.publish: publish()
