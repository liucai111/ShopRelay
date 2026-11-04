"""Offline M6.3 experiment aggregation over the existing trial/archive loader.

No execution or model calls. Source-case mappings come from the frozen design;
AI output judgments are separately bound to each actual Run and raw hash.
"""
from pathlib import Path
from collections import Counter
from statistics import median
import json,random,hashlib
from . import personal_closeout_r30 as existing
from eval.m6_1.aggregate import wilson_interval

OUT=Path('docs/eval/m6.3-personal-execution-20261101')

def read(p):return json.loads(p.read_text(encoding='utf8'))

def candidate_observations(rows):
    from app.m2.self_rag import _answer_semantic_issues,_policy_window_facts
    items=[]
    for r in rows:
        c=r['raw']['code_assessment']['candidate']
        if c is None:continue
        evidence={e['chunk_id']:e for x in r['raw']['application_archive']['read_invocations']
            if x['category']=='policy_retrieval' and x['status']=='RETURNED'
            for e in x['retrieval_result']['evidence']}
        selected=[evidence[x] for x in c['citation_ids'] if x in evidence]
        items.append({'case_id':r['case']['case_id'],'run_id':r['raw']['application_archive']['result']['run_id'],
            'raw_sha256':r['raw_sha256'],'candidate':c,'citation_sources':selected,
            'replay_findings':_answer_semantic_issues(c['redacted_text'],selected,r['case']['input']['user_query']),
            'candidate_assertions':_policy_window_facts(c['redacted_text'],cited_ids=c['citation_ids']),
            'redaction_limits':'Historical redacted dates/identifiers cannot be recovered' if '[REDACTED' in c['redacted_text'] else None,
            'is_delivered':False})
    return {'mode':'OFFLINE_ONLY_CURRENT_STAGE_ACTUAL_CANDIDATES','model_calls':0,'items':items,
        'scope':'Policy candidate content only;not actual delivery,complete business answer,permission or action effects'}

def metric(labels,*,repeated=False):
    n=labels.count('PASS');N=len(labels)
    return {'n':n,'N':N,'value':n/N if N else None,
        'wilson_95':{'status':'NOT_APPLICABLE_REPEATED_CASES_USE_CASE_CLUSTER_BOOTSTRAP','lower':None,'upper':None} if repeated else wilson_interval(n,N),
        'counts':dict(Counter(labels)),'scope':'AI_ASSISTED_PERSONAL_DEV_PROVISIONAL_NOT_HUMAN'}

def performance(rows):
    values=sorted(r['trial']['latency_ms']['total_ms']/1000 for r in rows)
    passed=sum(r['ai_review']['task_outcome']=='PASS' for r in rows)
    cost=sum(r['trial']['usage']['estimated_usd'] for r in rows)
    return {'N':len(rows),'provider':sum(r['trial']['usage']['calls_by_category']['provider'] for r in rows),
        'tools':sum(r['trial']['usage']['calls_by_category']['tool'] for r in rows),
        'tokens':sum(r['trial']['usage']['tokens'] for r in rows),'estimated_usd':cost,
        'usd_per_PASS':cost/passed if passed else None,'p50_seconds':median(values) if values else None,
        'p95_seconds':values[max(0,__import__('math').ceil(.95*len(values))-1)] if values else None,
        'timeout_count':sum('TIMEOUT' in str(r['raw']['code_assessment']['failure_categories']) for r in rows),
        'supplier_invoice':'UNKNOWN'}

def build(root):
    base=root/OUT;existing.OUT=OUT
    rows=existing.load_trials(root)
    design=read(base/'experiment-plan.json');by={t['case_id']:t for t in design['trials']}
    ai=existing.validate_ai(rows,read(base/'ai-output-review.json'))
    full=[]
    for r in rows:
        a=r['raw']['code_assessment'];c=r['case'];item=by[c['case_id']]
        archive=r['raw']['application_archive'];outer=archive['result']
        events=outer['observations']['run_events']
        starts=[json.loads(e['payload_json']) for e in events if e['event_type']=='agent_started']
        full.append({**r,'design':item,'question':c['input']['user_query'],
            'requirements':c['expected_outcome']['task']['requirements'],
            'delivered_answer':a['delivered_answer'],'candidate':a['candidate'],
            'actual_agents':starts,'handoff_events':[json.loads(e['payload_json']) for e in events if e['event_type'] in {'handoff','agent_handoff'}],
            'ai_review':ai[c['case_id']]})
    planned=set(by);actual={r['case']['case_id'] for r in rows}
    if len(actual)!=len(rows) or not actual<=planned:raise ValueError('trial consumption differs from frozen design')
    groups=[]
    for key in sorted({(r['experiment_id'],r['design']['phase'],r['design']['variant']) for r in full}):
        current=[r for r in full if (r['experiment_id'],r['design']['phase'],r['design']['variant'])==key]
        groups.append({'experiment_id':key[0],'phase':key[1],'variant':key[2],
            'runtime':sorted({r['raw']['scoring_runtime_sha256'] for r in current}),
            'task':metric([r['ai_review']['task_outcome'] for r in current],repeated=key[1]=='paired'),'performance':performance(current),
            'delivery':metric([r['raw']['code_assessment']['checks']['delivery'] for r in current],repeated=key[1]=='paired'),
            'route_sources':dict(Counter((r['raw']['code_assessment']['route'] or {}).get('decision_source','NOT_RECORDED') for r in current)),
            'failure_categories':dict(Counter(v for r in current for v in r['raw']['code_assessment']['failure_categories']))})
    pairs=[];stability=[]
    repeated=[r for r in full if r['design']['phase']=='paired']
    # Some designs use a more descriptive phase name; exact fixed design is the
    # source of truth rather than assuming all nonbenchmark rows are repeats.
    if not repeated:repeated=[r for r in full if r['design']['phase'] not in {'smoke','benchmark'}]
    for source in [c for values in design['selected_24'].values() for c in values]:
        current=[r for r in repeated if r['design']['source_case_id']==source]
        for variant in ('A','B'):
            selected=sorted([r for r in current if r['design']['variant']==variant],key=lambda r:r['design']['round'])
            labels=[r['ai_review']['task_outcome'] for r in selected]
            stability.append({'source_case_id':source,'variant':variant,'rounds':[r['design']['round'] for r in selected],
                'labels':labels,'complete_three':len(labels)==3,'pass_all_three':len(labels)==3 and labels.count('PASS')==3,
                'pass_at_least_once':'PASS' in labels,'runtime_bindings':sorted({r['raw']['scoring_runtime_sha256'] for r in selected})})
        for round_no in (1,2,3):
            matched={r['design']['variant']:r for r in current if r['design']['round']==round_no}
            if set(matched)!={'A','B'}:continue
            labels={v:matched[v]['ai_review']['task_outcome'] for v in ('A','B')}
            ordinal={'FAIL':0,'PARTIAL':.5,'PASS':1}
            winner=('UNSCORABLE' if not set(labels.values())<=set(ordinal) else
                'TIE' if labels['A']==labels['B'] else 'B' if ordinal[labels['B']]>ordinal[labels['A']] else 'A')
            pairs.append({'source_case_id':source,'round':round_no,'labels':labels,'winner':winner,
                'same_runtime':matched['A']['raw']['scoring_runtime_sha256']==matched['B']['raw']['scoring_runtime_sha256'],
                'provider':{v:matched[v]['trial']['usage']['calls_by_category']['provider'] for v in ('A','B')},
                'estimated_usd':{v:matched[v]['trial']['usage']['estimated_usd'] for v in ('A','B')}})
    case_deltas=[]
    for source in sorted({p['source_case_id'] for p in pairs}):
        p=[p for p in pairs if p['source_case_id']==source and p['same_runtime'] and p['winner']!='UNSCORABLE']
        if len(p)==3:case_deltas.append(sum((x['labels']['B']=='PASS')-(x['labels']['A']=='PASS') for x in p)/3)
    rng=random.Random(73);boot=sorted(sum(rng.choices(case_deltas,k=len(case_deltas)))/len(case_deltas) for _ in range(4000)) if case_deltas else []
    repeat_summary={v:{'N_cases':24,'complete_cases':sum(s['complete_three'] for s in stability if s['variant']==v),
        'pass_all_three_n':sum(s['pass_all_three'] for s in stability if s['variant']==v),
        'pass_at_least_once_n':sum(s['pass_at_least_once'] for s in stability if s['variant']==v),
        'performance':performance([r for r in repeated if r['design']['variant']==v])} for v in ('A','B')}
    benchmark=[r for r in full if r['design']['phase']=='benchmark']
    cohorts=read(root/'data/eval/new-assets-r26/v7/case-metadata.json')['cohorts']
    summary={'schema':'m63-personal-core-summary-v1','planned_trials':250,'actual_trials':len(rows),
        'not_run':[t for t in design['trials'] if t['case_id'] not in actual],'groups':groups,
        'budget':performance(full),'pairs':pairs,'stability':stability,'repeat_summary':repeat_summary,
        'benchmark_B_same_application_descriptive':{
            'all_100':metric([r['ai_review']['task_outcome'] for r in benchmark]),
            'primary_outcome_60':metric([r['ai_review']['task_outcome'] for r in benchmark if r['design']['source_case_id'] in cohorts['outcome']]),
            'performance':performance(benchmark),
            'runtime_segments':sorted({r['raw']['scoring_runtime_sha256'] for r in benchmark}),
            'binding_scope':'Identical application/data/model/prompts;19-trial stop followed by scorer-only successor. Full execution runtime bindings remain distinct;see segment-repair.json.'},
        'paired_case_bootstrap':{'N_cases':len(case_deltas),'mean_PASS_B_minus_A':sum(case_deltas)/len(case_deltas) if case_deltas else None,
            'lower_95':boot[100] if boot else None,'upper_95':boot[3899] if boot else None,'seed':73,'resamples':4000,
            'scope':'Purposive24 disclosed Dev cases;case-cluster descriptive comparison,not72 independent questions'},
        'limitations':['New output reviews are AI-assisted and pending user acceptance','Different segment bindings remain separate',
            'B is the existing hybrid rules/Supervisor/specialist implementation;A is a flat model tool loop only where actual AGENT path runs',
            'Same source data has been used in development;not unseen Frozen','Scripted owner effects are separate from Agent draft completion'],
        'human_acceptance':None}
    # Reference existing raw files rather than duplicating whole durable traces.
    projected=[]
    for r in full:
        archive=r['raw']['application_archive'];assessment=r['raw']['code_assessment']
        projected.append({'case_id':r['case']['case_id'],'design':r['design'],
            'run_id':archive['result']['run_id'],'trial_id':r['trial']['trial_id'],
            'experiment_id':r['experiment_id'],'runtime':r['raw']['scoring_runtime_sha256'],
            'raw_sha256':r['raw_sha256'],'archive_path':r['archive_path'],
            'question':r['question'],'requirements':r['requirements'],
            'lifecycle_status':archive['result']['status'],
            'delivered_answer':r['delivered_answer'],'candidate':r['candidate'],
            'route':assessment['route'],'original_code_assessment':assessment,
            'actual_agents':r['actual_agents'],'handoff_events':r['handoff_events'],
            'returned_handoffs':(archive['result']['result'] or {}).get('handoffs',[]),
            'read_calls':[{'operation':x['operation'],'arguments':x['arguments'],
                'status':x['status'],'tool_audit':x.get('tool_audit')} for x in archive['read_invocations']],
            'owner_interaction_ref':'application_archive.owner_interaction' if archive.get('owner_interaction') else None,
            'usage':r['trial']['usage'],'latency_ms':r['trial']['latency_ms'],
            'cleanup':r['cleanup'],'ai_review':r['ai_review']})
    existing.put(base/'scores-and-evidence.json',{'rows':projected})
    existing.put(base/'metrics-and-budget.json',summary)
    existing.put(base/'candidate-ablation-observations-final.json',candidate_observations(rows))
    lines=['# M6.3逐Trial输出审核','', 'AI辅助预审，真人字段未填；每个重复试次独立绑定。','',
        '| Trial | Source | 组/轮 | Task建议 | 理由 |','|---|---|---|---|---|']
    for r in full:
        d=r['design'];a=r['ai_review']
        lines.append('|'+ '|'.join([r['case']['case_id'],d['source_case_id'],d['variant']+'/'+str(d['round']),a['task_outcome'],a['rationale'].replace('|','/').replace('\n',' ')])+'|')
    text='\n'.join(lines)+'\n';p=base/'review-table.md'
    if p.exists() and p.read_text(encoding='utf8')!=text:raise ValueError('review table drift')
    p.write_text(text,encoding='utf8')
    if (base/'verification.json').exists() and (base/'candidate-ablation-review.json').exists():
        closeout(root,summary,projected)
    return summary

def closeout(root,summary,rows):
    base=root/OUT;verification=read(base/'verification.json');ablation=read(base/'candidate-ablation-review.json')
    repair=read(base/'segment-repair.json');budget=summary['budget']
    if verification['planned_and_actual_trials']!=len(rows):raise ValueError('verified actual coverage differs')
    participation=[]
    for phase,variant in sorted({(r['design']['phase'],r['design']['variant']) for r in rows}):
        current=[r for r in rows if (r['design']['phase'],r['design']['variant'])==(phase,variant)]
        participation.append({'phase':phase,'variant':variant,'trials':len(current),
            'flat_controller_entered':sum(any(a['agent']=='single_controller' for a in r['actual_agents']) for r in current),
            'multiple_specialist_nodes_observed':sum(len({a['agent'] for a in r['actual_agents']})>1 for r in current),
            'provider_participating_trials':sum(r['usage']['calls_by_category']['provider']>0 for r in current)})
    total_latency=sum(r['latency_ms']['total_ms'] for r in rows)/1000
    final={'schema':'m63-personal-core-closeout-v1','authorization_event':'M63-PERSONAL-CORE-EXECUTION-APPROVED-20261101',
        'execution':'COMPLETE' if len(rows)==250 else 'INCOMPLETE','analysis':'COMPLETE_AI_ASSISTED_PENDING_USER_ACCEPTANCE',
        'M6_1':'COMPLETE','M6_2_PERSONAL':'COMPLETE','M6_3':'EXECUTION_AND_ANALYSIS_COMPLETE_PENDING_OUTPUT_ACCEPTANCE',
        'quality':'KNOWN_NEGATIVE_RESULTS_RETAINED;NO_ALL_PASS_REQUIREMENT',
        'planned_trials':250,'actual_trials':len(rows),'new_paid_judge_or_embedding':0,
        'trial_lifecycle':dict(Counter(r['lifecycle_status'] for r in rows)),
        'delivered_n':sum(bool(r['delivered_answer']) for r in rows),
        'ai_task_counts_coverage_only':dict(Counter(r['ai_review']['task_outcome'] for r in rows)),
        'budget':{**budget,'trial_latency_seconds_sum':total_latency,
            'remaining_provider':2000-budget['provider'],'remaining_tokens':30000000-budget['tokens'],
            'remaining_estimate_usd':50-budget['estimated_usd'],'remaining_application_trials':250-len(rows)},
        'participation':participation,'whole_execution_binding_segments':repair,
        'ablation_counts':ablation['counts'],'ablation_scope':ablation['limitations'],
        'frozen_image_recompute_evidence':['recompute-s1-verification.json','recompute-s2-verification.json'],
        'scoring_recompute_entry':' .venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version m63',
        'review_scope':'250 new exact Runs;AI Task and candidate policy judgments remain unsigned. Original source-reference confirmations do not approve new outputs.',
        'next_action':'Concentrated user review of new Task judgments,paired outcomes and policy-rejection analysis;no automatic new trial or M6.4',
        'strict_unseen_gold_frozen_claim':False,'human_decision':None}
    existing.put(base/'closeout.json',final)
    matrix={'M6_3':final['M6_3'],'items':[
        {'requirement':'真实Single/现有Supervisor及公平性','status':'COMPLETE_WITH_HYBRID_LIMITATION_DISCLOSED','evidence':'app/m3/single_agent.py; participation; tests/test_m6_3_personal_core.py'},
        {'requirement':'固定6+100+144试次与独立会话','status':'COMPLETE','evidence':'experiment-plan.json;verification.json'},
        {'requirement':'统一B应用基准','status':'COMPLETE_SAME_APPLICATION_TWO_SCORER_BINDINGS','evidence':'segment-repair.json;metrics-and-budget.json'},
        {'requirement':'24题配对及三轮稳定性','status':'COMPLETE_ONE_S2_BINDING','evidence':'metrics-and-budget.json:pairs/stability'},
        {'requirement':'校验离线消融','status':'COMPLETE_AI_ASSISTED_LIMITED_SCOPE','evidence':'candidate-ablation-review.json;candidate-ablation-observations-final.json'},
        {'requirement':'原始评分重算、累计用量及精确清理','status':'COMPLETE','evidence':'verification.json;recompute-s1-verification.json;recompute-s2-verification.json;final-health.json'},
        {'requirement':'新输出及争议集中接受','status':'PENDING_REAL_USER_ACCEPTANCE','evidence':'review-table.md;ai-output-review.json;candidate-ablation-review.json'},
        {'requirement':'M6.4或额外试次','status':'NOT_STARTED_NOT_AUTHORIZED','evidence':'user-authorization.json'}]}
    existing.put(base/'exit-matrix.json',matrix)
    benchmark=summary['benchmark_B_same_application_descriptive'];A=summary['repeat_summary']['A'];B=summary['repeat_summary']['B'];delta=summary['paired_case_bootstrap']
    winner=Counter(p['winner'] for p in summary['pairs'])
    lines=['# M6.3个人项目核心评测收口','',
        '**250/250个真实应用试次已完成：Smoke6、B基准100、配对三轮144。执行、代码评分、AI预审、重算和清理完成；新输出集中接受尚待用户决定。**','',
        'M6.1与M6.2个人范围保持COMPLETE；本轮不启动M6.4。不同阶段的历史Run未混入本批分母。全部新输出为AI辅助Dev预审，不是新的人类/Gold标签。','',
        '## 结果','',
        '| 范围 | PASS | PARTIAL | FAIL |','|---|---:|---:|---:|']
    for title,labels in [('B主集100',benchmark['all_100']['counts']),('配对A：Single72',next(g['task']['counts'] for g in summary['groups'] if g['phase']=='paired' and g['variant']=='A')),
            ('配对B：现有Multi72',next(g['task']['counts'] for g in summary['groups'] if g['phase']=='paired' and g['variant']=='B'))]:
        lines.append(f"|{title}|{labels.get('PASS',0)}|{labels.get('PARTIAL',0)}|{labels.get('FAIL',0)}|")
    lines+=['',f"B主集严格PASS为{benchmark['all_100']['n']}/100；主要Outcome队列{benchmark['primary_outcome_60']['n']}/60。100题应用/数据/模型/提示相同，执行评分器跨两段：首段13个基准题、后继87个。完整runtime绑定并非同一值，不宣称单一完整绑定成绩；仅改分类与剩余费用计算，实际应用字节相同。原段第19次STOP仍保留，未重跑。",'',
        f"72组同题同轮配对：B胜{winner['B']}、A胜{winner['A']}、同分{winner['TIE']}。严格PASS差值B−A为{delta['mean_PASS_B_minus_A']*100:.2f}个百分点；按24题重采样的描述性95%区间为[{delta['lower_95']*100:.2f}, {delta['upper_95']*100:.2f}]个百分点。不是72个独立题的区间，也不是总体泛化结论。",'',
        f"三次全PASS：A {A['pass_all_three_n']}/24，B {B['pass_all_three_n']}/24；至少一次PASS：A {A['pass_at_least_once_n']}/24，B {B['pass_at_least_once_n']}/24。不择优替换失败轮。",'',
        '| 配对成本/延迟 | A | B |','|---|---:|---:|',
        f"|Provider请求|{A['performance']['provider']}|{B['performance']['provider']}|",
        f"|tokens|{A['performance']['tokens']}|{B['performance']['tokens']}|",
        f"|估算USD|{A['performance']['estimated_usd']:.7f}|{B['performance']['estimated_usd']:.7f}|",
        f"|P50秒（各72）|{A['performance']['p50_seconds']:.3f}|{B['performance']['p50_seconds']:.3f}|",
        f"|P95秒（各72）|{A['performance']['p95_seconds']:.3f}|{B['performance']['p95_seconds']:.3f}|",'',
        'B在这组数据上请求更少、延迟更低，但tokens及保守费用略高；不能称所有维度均更省。复合048两组均三次FAIL，复合050仍缺完整交付，动作018的B三次被范围护栏拒绝。政策030的差异出现在两组共同政策链，不能归功于Multi。', '',
        '## 实现与公平性边界','',
        'A是实际LLM下一工具/结束/澄清循环，复用同一守门工具适配层，没有Supervisor节点；B是当前规则/Supervisor/专业节点混合实现，增加共同的工具后LLM汇总。两组使用相同模型、业务答案节点配置、允许能力、原始输入、初始Fixture、固定政策时点及owner脚本。仅真实AGENT路径改变协作结构；共同SINGLE政策、DIRECT和REFUSE路径不伪称架构差异。', '',
        '配对A有47/72次实际进入flat controller，B有22/72次观察到多个专业节点；两组各63/72有实际模型调用。规则路由不等于无LLM回答，也不等于LLM自主路由。源数据已用于开发，24题针对历史问题选择，存在明确选择偏差。owner/API精确效果与Agent草案完整性分开。', '',
        '## 修复与负结果','',
        '- 执行前：通用订单明细/复合能力选择、明确商品名解析、电压比较、动作草案读取路线、真实Single基线及业务结果模型汇总；沿用既有政策守门，不注入期望答案。',
        '- 第19次读取完整但业务结果无法汇总，原检查器误停为未分类。保留Run与原STOP，以离线完整CLI正反例修复分类后继续231个未消费试次；不修改决策代码、数据、提示或Task阈值。',
        '- 35份实际拒绝候选：31份有据政策断言被拒的AI建议、1份当前引用不支持所述期限、2份无政策来源的字段契约拒绝、1份一般/具体主体冲突争议。日期/标识脱敏部分不可恢复。完整用户Task仍按实际交付评分。',
        '- 漏放行率缺独立完整断言标签，记N/A；已发现额外币种、泛化澄清、错配目标拒绝等业务答案缺陷，不能用政策引用通过掩盖。校验分析没有新增模型请求，也没有关闭真实安全护栏。', '',
        '## 用量、验证与清理','',
        f"Provider {budget['provider']}，只读/owner模拟调用合计{budget['tools']}，tokens {budget['tokens']}，本地保守估算USD {budget['estimated_usd']:.5f}；供应商账单UNKNOWN。Judge/Embedding0。累计Trial延迟{total_latency:.3f}秒，全部P50 {budget['p50_seconds']:.3f}秒、P95 {budget['p95_seconds']:.3f}秒（n250、超时0）；不含离线修复/评分耗时。",'',
        f"剩余额度：Provider {2000-budget['provider']}、tokens {30000000-budget['tokens']}、估算USD {50-budget['estimated_usd']:.5f}；应用试次250名额已用尽，未授权自动补跑。250个独立会话及精确receipt均核验，零残留/Pending/活动Run/lease、两许可关闭、预留0、Provider停用。",'',
        '受影响回归153 passed，后继分类/CLI三文件52 passed；二者有重叠，不相加为案例数。早期失败迭代JUnit保留。两个原冻结镜像各自断网重算两遍，逐项重核原档、案例和原代码评分，不只比较输出哈希。新评分汇总从精确Run/原档及独立AI记录重建。', '',
        '## 集中待审及入口','',
        '- `review-table.md`和`ai-output-review.json`：250个精确Run的Task建议及具体理由；全部完整交付/候选、来源与程序检查见`scores-and-evidence.json`及原始`evidence/application/`。',
        '- `candidate-ablation-review.json`：35项政策候选判断；尤其074一般/具体对象冲突争议、空政策来源契约与有据期限误拒分开接受。',
        '- `metrics-and-budget.json`：分段/分组、24题每轮、配对胜负、成本及不确定性。新输出未填reviewer、时间或确认事件。',
        '- 离线重建：`.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version m63`，网络阻断验证为0次尝试；不需要调用模型。',
        '- 工程与分析工作已完成。下一步仅集中接受/修改本批Task与校验预审；不因低分开启重跑。后续可改进Single计划字段稳定性、复合工具覆盖、政策主体/条件解析及币种/澄清完整性，另行规划，未启动M6.4。','']
    text='\n'.join(lines);p=base/'closeout.md'
    if p.exists() and p.read_text(encoding='utf8')!=text:raise ValueError('closeout drift')
    p.write_text(text,encoding='utf8')

def finalize_accepted_scope(root):
    """Apply the recorded actual user's exact acceptance, preserving old layers."""
    from decimal import Decimal
    base=root/OUT;event=read(base/'final-user-acceptance-r1.json')
    for binding in event['source_bindings']:
        if hashlib.sha256((root/binding['path']).read_bytes()).hexdigest()!=binding['sha256']:
            raise ValueError('accepted source changed:'+binding['path'])
    existing.OUT=OUT;rows=existing.load_trials(root)
    ai=existing.validate_ai(rows,read(base/'ai-output-review.json'))
    accepted={x['case_id']:x for x in event['items']}
    if len(event['items'])!=250 or set(accepted)!={r['case']['case_id'] for r in rows}:raise ValueError('acceptance coverage differs')
    design={t['case_id']:t for t in read(base/'experiment-plan.json')['trials']}
    for r in rows:
        x=accepted[r['case']['case_id']]
        actual=(r['raw']['application_archive']['result']['run_id'],r['trial']['trial_id'],r['raw_sha256'],
            r['raw']['scoring_runtime_sha256'],r['experiment_id'],ai[x['case_id']]['task_outcome'])
        if actual!=tuple(x[k] for k in ('run_id','trial_id','raw_sha256','runtime','experiment_id','accepted_task_outcome')):
            raise ValueError('acceptance exact Run/score binding differs')
    counts=dict(Counter(x['accepted_task_outcome'] for x in accepted.values()))
    if counts!=event['accepted_task_counts']:raise ValueError('accepted Task totals differ')
    groups=[]
    for phase,variant in [('smoke',None),('benchmark','B'),('paired','A'),('paired','B')]:
        chosen=[x for x in accepted.values() if design[x['case_id']]['phase']==phase and (variant is None or design[x['case_id']]['variant']==variant)]
        groups.append({'phase':phase,'variant':variant,'N':len(chosen),
            'accepted_task_counts':dict(Counter(x['accepted_task_outcome'] for x in chosen))})
    expected=[{'PARTIAL':3,'FAIL':3},{'PASS':51,'PARTIAL':10,'FAIL':39},
        {'PASS':27,'PARTIAL':19,'FAIL':26},{'PASS':38,'PARTIAL':22,'FAIL':12}]
    if [g['accepted_task_counts'] for g in groups]!=expected:raise ValueError('accepted phase counts differ')
    pairs={};rounds={};ordinal={'FAIL':0,'PARTIAL':.5,'PASS':1}
    for x in accepted.values():
        t=design[x['case_id']]
        if t['phase']!='paired':continue
        pairs.setdefault((t['source_case_id'],t['round']),{})[t['variant']]=x['accepted_task_outcome']
        rounds.setdefault((t['source_case_id'],t['variant']),[]).append((t['round'],x['accepted_task_outcome']))
    if len(pairs)!=72 or any(set(p)!={'A','B'} for p in pairs.values()):raise ValueError('paired coverage differs')
    wins=dict(Counter('TIE' if p['A']==p['B'] else 'A' if ordinal[p['A']]>ordinal[p['B']] else 'B' for p in pairs.values()))
    if wins!={'TIE':45,'A':6,'B':21}:raise ValueError('accepted paired wins differ')
    stability={}
    for v in ('A','B'):
        items=[p for (_,variant),p in rounds.items() if variant==v]
        if len(items)!=24 or any(sorted(n for n,_ in p)!=[1,2,3] for p in items):raise ValueError('repeat round coverage differs')
        stability[v]={'N_cases':24,'all_three_PASS':sum(all(s=='PASS' for _,s in p) for p in items),
            'any_PASS':sum(any(s=='PASS' for _,s in p) for p in items)}
    if stability['A']['all_three_PASS']!=4 or stability['B']['all_three_PASS']!=9:raise ValueError('accepted stability differs')
    original_candidates=read(base/'candidate-ablation-review.json')['items'];candidate_items=[]
    original_observations={x['case_id']:x for x in read(base/'candidate-ablation-observations-final.json')['items']}
    for x in original_candidates:
        bound=accepted[x['case_id']];o=original_observations[x['case_id']]
        if (x['run_id'],x['raw_sha256'])!=(bound['run_id'],bound['raw_sha256']) or x['candidate_citation_ids']!=o['candidate']['citation_ids']:
            raise ValueError('accepted candidate binding differs')
        label=x['policy_guard_ai_judgment']
        if x['case_id']=='M63-benchmark-B-r1-FC26-EXTRA-074':label='SCOPE_CONFLATION_NOT_CLEAN_FALSE_REJECTION'
        elif label=='SUPPORTED_POLICY_ASSERTIONS_REJECTED':label='REJECTED_RELEVANT_DURATION_ASSERTIONS_SUPPORTED_ONLY'
        candidate_items.append({'case_id':x['case_id'],'run_id':x['run_id'],'raw_sha256':x['raw_sha256'],
            'accepted_diagnostic':label,'acceptance_event':event['event_id']})
    candidate_counts=dict(Counter(x['accepted_diagnostic'] for x in candidate_items))
    if candidate_counts!={'REJECTED_RELEVANT_DURATION_ASSERTIONS_SUPPORTED_ONLY':31,'SCOPE_CONFLATION_NOT_CLEAN_FALSE_REJECTION':1,
            'NO_POLICY_EVIDENCE_CONTRACT_REJECTION':2,'VALID_CITATION_SCOPE_BLOCK':1}:raise ValueError('accepted candidate categories differ')
    cost=sum(Decimal(str(r['trial']['usage']['estimated_usd'])) for r in rows)
    provider=sum(r['trial']['usage']['calls_by_category']['provider'] for r in rows);tokens=sum(r['trial']['usage']['tokens'] for r in rows)
    if provider!=309 or tokens!=421275 or cost.quantize(Decimal('.00000001'))!=Decimal('.50553000'):raise ValueError('original accounting differs')
    prior=read(base/'closeout.json');verification=read(base/'verification.json')
    if verification['grants']!={'CLAIMED':250,'CLOSED':2,'SETTLED':250,'STOPPED':1}:raise ValueError('historical closure evidence differs')
    backlog=['Single步骤/字段契约稳定性与工具计划完成，失败不靠重采样刷分',
        '复合任务必要读取、逐资源/订单行及用户完整交付，owner/API效果不替代Agent任务',
        '政策期限主体、事件、条件和引用范围解析；074一般/具体范围不得混淆',
        '币种来源、精准澄清、目标拒绝、申请/审核/批准/到账措辞',
        '若后续要研究纯结构因果收益或真实校验器消融，应另行设计，不冒用本批系统比较或离线候选诊断']
    final={'schema':'m63-personal-final-closeout-v1','stage_status':{'M6_1':'COMPLETE','M6_2_PERSONAL':'COMPLETE','M6_3_PERSONAL':'COMPLETE'},
        'acceptance_event':event['event_id'],'actor':event['actor'],'method':event['method'],
        'actual_trials':250,'accepted_task_counts_coverage_only':counts,'groups':groups,
        'paired_wins':wins,'stability':stability,'candidate_items':candidate_items,'candidate_counts':candidate_counts,
        'candidate_scope':'31 only confirms support for rejected relevant duration assertions,not complete-answer correctness;074 scope conflation,not clean false rejection. Offline diagnostic,not a live guard-disabled end-to-end ablation.',
        'overall_false_rejection_rate':None,'overall_false_acceptance_rate':None,
        'rate_limitation':'Selected rejected candidates and incomplete independent assertion labels do not support overall rates.',
        'comparison_scope':'This batch/current Single and hybrid Multi implementation;not a general pure-architecture causal claim.100 benchmark spans two scoring runtimes with identical application bytes;144 paired trials share s2.',
        'budget':prior['budget'],'closure_evidence':'verification.json;final-health.json;original frozen recompute evidence reused,not re-queried',
        'quality_status':'KNOWN_FAILURES_RETAINED_FOR_DEVELOPMENT_NOT_ALL_QUALITY_GATES_PASS',
        'backlog':backlog,'strict_Gold_Registry_independent_Frozen':'UNCHANGED_NOT_GRANTED',
        'new_calls':{'application_Run':0,'Provider':0,'Judge':0,'Embedding':0},'M6_4':'NOT_STARTED'}
    existing.put(base/'final-stage-closeout-r1.json',final)
    existing.put(base/'exit-matrix-final-r1.json',{'stage_status':final['stage_status'],'acceptance_event':event['event_id'],
        'items':[{'requirement':r['requirement'],'status':'COMPLETE_USER_ACCEPTED_AI_ASSISTED' if r['status']=='PENDING_REAL_USER_ACCEPTANCE' else r['status'],
            'evidence':r['evidence']} for r in read(base/'exit-matrix.json')['items']]})
    existing.put(base/'final-closeout-verification-r1.json',{'exact_accepted_runs':250,'Task_counts':counts,'groups':groups,
        'paired_wins_recomputed':wins,'stability_recomputed':stability,'candidate_bindings':len(candidate_items),'candidate_counts':candidate_counts,
        'raw_archive_case_receipt_bindings_checked':True,'source_bindings':event['source_bindings'],
        'ledger_provider_tokens_cost_checked':True,'historical_report_and_authorization_unchanged':True,
        'closure_evidence_reused_not_live_requeried':True,'new_calls':final['new_calls'],'full_regression_or_deployment_performed':False})
    lines=['# M6.3个人项目范围最终收口','', '**M6.3个人范围COMPLETE，质量问题保留进入后续开发。** M6.1及M6.2个人范围保持COMPLETE；M6.4未启动。','',
        f"接受事件：`{event['event_id']}`。已直接核验审核聊天用户“{event['actual_user_statement']}”，方式为AI辅助证据复核后用户集中采纳，Actor为CHAT_USER_SELF_CONFIRMED。250个精确Run/原档/Task标签绑定，不冒充独立逐题或逐原子事实标注、盲审。",'',
        '| 范围 | 试次 | PASS | PARTIAL | FAIL |','|---|---:|---:|---:|---:|']
    for g in groups:
        c=g['accepted_task_counts'];lines.append('|'+ '|'.join([g['phase']+('/'+g['variant'] if g['variant'] else ''),str(g['N']),str(c.get('PASS',0)),str(c.get('PARTIAL',0)),str(c.get('FAIL',0))])+'|')
    lines+=['','合计116 PASS/54 PARTIAL/80 FAIL为覆盖汇总；208交付、42未交付的历史事实不改。安全、Task完成、候选内容及owner脚本效果继续分层。未交付不因候选有据改成成功。','',
        '72对B胜21/A胜6/同分45；三轮全PASS A4/24、B9/24，至少一次A14/24、B17/24。只表述本批选题/当前实现的系统比较；共同政策链、规则路径及开发数据暴露不支持纯多Agent结构普遍优越的因果结论。100基准应用字节相同但评分runtime跨s1/s2，144配对同s2。原STOP及所有失败保留，不拼成单一完整绑定成绩。','',
        '35份候选的最终接受：31份仅确认被拒的相关期限断言有当前引用支持，不认定完整答案全正确；EXTRA-074为`SCOPE_CONFLATION_NOT_CLEAN_FALSE_REJECTION`，不同适用范围被混为期限冲突，Task仍FAIL。另1份所选引用范围问题、2份无政策证据的字段契约拒绝保持分列。已脱敏日期/标识不可恢复。此为离线候选诊断，不是真实关闭校验器的端到端消融；总体误拒/漏放行率均N/A。','',
        'Provider309、tokens421275、本地保守估算USD0.50553，供应商账单UNKNOWN。复用250个精确receipt、两份关闭许可、预留0及Provider停用证据，本次不重新查询实时环境、不启用Provider、不执行模型、案例、部署或全量回归。','',
        '当前有效索引为本最终报告、`final-user-acceptance-r1.json`、`final-stage-closeout-r1.json`和`exit-matrix-final-r1.json`。旧closeout/AI预审/PENDING字段是确认前历史，不覆写；最终接受层供当前阶段判断。原SHA清单绑定当时progress和报告生成器快照，本次追加及入口扩展造成其快照差异，不改写旧SHA或增加哈希审批层。','',
        '离线复算本次最终接受：`.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version m63 --finalize`。验证逐个Run/标签/原档及35候选绑定，重算分组、配对、三轮及原账本用量，不只比较输出哈希。','',
        '## 后续开发清单（不自动执行）','']+['- '+x for x in backlog]+['',
        '严格Gold/Registry/独立Frozen及033专项缺口保持原状态。阶段完成不等于全部质量门槛通过；本轮不启动M6.4或新的修复重跑循环。','']
    text='\n'.join(lines);p=base/'final-stage-closeout-r1.md'
    if p.exists() and p.read_text(encoding='utf8')!=text:raise ValueError('final report drift')
    p.write_text(text,encoding='utf8')
    return final

if __name__=='__main__':
    value=build(Path.cwd());print(json.dumps({'actual_trials':value['actual_trials'],'not_run':len(value['not_run']),'budget':value['budget']}))
