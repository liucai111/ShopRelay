"""Offline report projection of original personal trials plus separately bound AI review.

No execution, permit issuance, Provider or automatic human label ingestion.
"""
from pathlib import Path
import hashlib,json,sqlite3
from collections import Counter

OUT=Path('docs/eval/m6.2-personal-real-evaluation-r30-20261101')

def read(path):return json.loads(path.read_text(encoding='utf8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def put(path,value):
    text=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('existing derived report differs:'+path.name)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf8')

def load_trials(root):
    from eval.m6_1.contracts import sha256_json,EvalCase
    from eval.m6_1.grants import formal_case_digest
    base=root/OUT/'evidence';db=sqlite3.connect('file:'+str(base/'results.sqlite3')+'?mode=ro',uri=True)
    rows=[]
    for exp,t,r,payload_sha in db.execute('select t.experiment_id,t.payload_json,r.payload_json,r.payload_sha256 from trials t join raw_results r using(trial_id) order by t.created_at_utc'):
        trial=json.loads(t);raw=json.loads(r)
        if sha256_json(raw)!=payload_sha:raise ValueError('raw persisted hash differs')
        case=read(base/'application'/Path(raw['case_path']).name)
        if formal_case_digest(EvalCase.from_dict(case))!=raw['case_sha256']:raise ValueError('case binding differs')
        archive=base/'application'/Path(raw['receipt']['archive_path']).name
        if sha(archive)!=raw['archive_sha256']:raise ValueError('archive bytes differ')
        proof_path=base/'application'/(archive.stem+'.cleanup.json')
        if proof_path.exists():proof=read(proof_path)
        else:
            proofs=[read(p)['proof'] for p in base.glob('post-stop-cleanup-*.json')]
            match=[p for p in proofs if p['receipt']==raw['receipt']]
            if len(match)!=1:raise ValueError('missing unique exact cleanup proof:'+case['case_id'])
            proof=match[0]
        if proof['receipt']!=raw['receipt'] or proof['archive_sha256']!=raw['archive_sha256']:raise ValueError('cleanup proof binding differs')
        e=proof['evidence']
        if not e['lease_released'] or e['residual_case_rows'] or e['pending_actions']:raise ValueError('cleanup incomplete')
        rows.append({'experiment_id':exp,'trial':trial,'raw':raw,'case':case,'raw_sha256':payload_sha,'archive_path':archive.relative_to(root).as_posix(),
            'cleanup':e})
    db.close()
    ids=[r['case']['case_id'] for r in rows]
    if len(ids)!=len(set(ids)):raise ValueError('same case has multiple real trials')
    return rows

def validate_ai(rows,review):
    if review['method']!='AI_ASSISTED_POSTHOC_OUTPUT_REVIEW_NOT_HUMAN':raise ValueError('wrong review method')
    by={r['case']['case_id']:r for r in rows};items=review['items']
    if len(items)!=len(by) or {i['case_id'] for i in items}!=set(by):raise ValueError('AI review exact actual coverage differs')
    for item in items:
        row=by[item['case_id']]
        if item['run_id']!=row['raw']['application_archive']['result']['run_id'] or item['raw_sha256']!=row['raw_sha256']:raise ValueError('AI review wrong Run/raw binding')
        if item['task_outcome'] not in {'PASS','PARTIAL','FAIL','UNSCORABLE'} or not item['rationale']:raise ValueError('AI review verdict/rationale missing')
        if row['raw']['code_assessment']['task_outcome']=='FAIL' and item['task_outcome']!='FAIL':raise ValueError('candidate quality cannot override known delivery/required read failure')
        if item['human_decision'] is not None or item['reviewer'] is not None or item['reviewed_at'] is not None:raise ValueError('AI cannot sign human decisions')
    return {i['case_id']:i for i in items}

def build(root):
    from eval.m6_1.aggregate import wilson_interval
    base=root/OUT;rows=load_trials(root);ai=validate_ai(rows,read(base/'ai-output-review.json'))
    groups=[];full=[]
    for path in sorted((base/'evidence').glob('recomputed-*.json')):
        report=read(path)
        if not report['rows']:continue
        current=[r for r in rows if r['experiment_id']==report['experiment_id']]
        if {r['raw_sha256'] for r in current}!={r['raw_sha256'] for r in report['rows']}:raise ValueError('recompute group coverage/raw binding differs')
        def metric(values):
            p=values.count('PASS');n=len(values)
            return {'n':p,'N':n,'value':p/n if n else None,'wilson_95':wilson_interval(p,n),'counts':dict(Counter(values))}
        labels=[ai[r['case']['case_id']]['task_outcome'] for r in current]
        outcome_scope=set(report['metrics']['reporting_cohorts']['outcome'])
        groups.append({'experiment_id':report['experiment_id'],'runtime':report['scoring_runtime_sha256'],'report_binding':report['binding_sha256'],
            'actual_trials':len(current),'original_code_metrics':report['metrics'],
            'ai_task_all_actual':metric(labels),'ai_primary_outcome':metric([ai[r['case']['case_id']]['task_outcome'] for r in current if r['case']['case_id'] in outcome_scope]),
            'route_sources':dict(Counter((r['raw']['code_assessment']['route'] or {}).get('decision_source','NO_ROUTE_PERSISTED') for r in current)),
            'lifecycle':dict(Counter(r['raw']['application_archive']['result']['status'] for r in current)),
            'budget':report['budget'],'quality_status':'AI_ASSISTED_PERSONAL_PROVISIONAL;NOT_INDEPENDENT_GOLD'})
    for r in rows:
        a=r['raw']['code_assessment'];archive=r['raw']['application_archive'];outer=archive['result'];events=outer['observations']['run_events']
        full.append({'case_id':r['case']['case_id'],'group_id':r['case']['group_id'],'experiment_id':r['experiment_id'],
            'run_id':outer['run_id'],'runtime':r['raw']['scoring_runtime_sha256'],'data_version':r['case']['data_version'],
            'raw_sha256':r['raw_sha256'],'archive_path':r['archive_path'],'question':r['case']['input']['user_query'],
            'requirements':r['case']['expected_outcome']['task']['requirements'],'lifecycle':outer['status'],'delivered_answer':a['delivered_answer'],
            'candidate':a['candidate'],'route':a['route'],'model_calls':r['trial']['usage']['calls_by_category']['provider'],
            'read_invocations':archive['read_invocations'],'policy_sources':r['case']['input']['fixture']['policies'],
            'citations':(outer['result'] or {}).get('citations',[]),'node_and_handoff_events':[e for e in events if e['event_type'] in {'agent_started','handoff','agent_handoff','agent_finished','node_started','node_finished'}],
            'scripted_owner_interaction':archive.get('owner_interaction'),'original_code_assessment':a,'ai_review':ai[r['case']['case_id']],
            'usage':r['trial']['usage'],'latency_ms':r['trial']['latency_ms'],'cleanup':r['cleanup'],
            'current_output_human_confirmation':'NOT_YET_CONFIRMED;source-reference user confirmation does not confirm new generated outputs'})
    budget={'provider':sum(g['budget']['calls_by_category']['provider'] for g in groups),'tools':sum(g['budget']['calls_by_category']['tool'] for g in groups),
        'tokens':sum(g['budget']['tokens'] for g in groups),'estimated_usd':sum(g['budget']['estimated_usd'] for g in groups),
        'supplier_invoice':'UNKNOWN','judge':0,'embedding':0,'active_reservations':sum(g['budget']['active_reservations'] for g in groups),
        'latency_seconds_sum':sum(r['trial']['latency_ms']['total_ms'] for r in rows)/1000,'scope':'CONTROLLER_COMMITTED_EVENTS_ONLY;app mirror not added'}
    summary={'method':'USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1','actual_trials':len(rows),'dev':sum(r['case']['case_id'].startswith('DV') for r in rows),
        'main':sum(r['case']['case_id'].startswith('FC') for r in rows),'task_counts_coverage_only_not_uniform_accuracy':dict(Counter(i['task_outcome'] for i in ai.values())),
        'budget':budget,'groups':groups,'blind_status':'CANCELLED_BY_OWNER;NOT_PASSED','independent_unseen_status':'NOT_VERIFIED',
        'scoped_real_execution':'COMPLETE' if len(rows)==108 else 'INCOMPLETE','M6_1':'COMPLETE','M6_2':'EXECUTION_AND_CODE_SCORING_CLOSED;OUTPUT_DISPUTES_AND_QUALITY_ACCEPTANCE_OPEN',
        'do_not_claim':'Not an independent unseen Frozen result;different frozen segments are not one common-version accuracy'}
    put(base/'scores-and-evidence.json',{'schema':'r30-complete-review-v1','rows':full})
    put(base/'metrics-and-budget.json',summary)
    table=['# R30 实际输出集中预审','', 'AI辅助、事后个人评测预审；不是人工决定。未交付和必需读取失败保留Task FAIL。','',
        '| Case | Run | 交付 | Task建议 | 依据 |','|---|---|---|---|---|']
    for r in full:table.append('|'+ '|'.join([r['case_id'],r['run_id'],'已交付' if r['delivered_answer'] else '未交付',r['ai_review']['task_outcome'],r['ai_review']['rationale'].replace('|','/').replace('\n',' ')])+'|')
    text='\n'.join(table)+'\n';path=base/'review-table.md'
    if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('review table drift')
    path.write_text(text,encoding='utf8')
    supplementary(root,rows)
    return summary

def supplementary(root,rows):
    """Typed observations from actual archives,not copied task verdicts."""
    from eval.m6_1.aggregate import wilson_interval
    result=[];nodes=[]
    for experiment in sorted({r['experiment_id'] for r in rows}):
        current=[r for r in rows if r['experiment_id']==experiment]
        totals=Counter();sources=Counter();required_hits=required_total=valid_citations=citation_count=0
        for r in current:
            archive=r['raw']['application_archive'];outer=archive['result'];payload=outer['result'] or {};a=r['raw']['code_assessment']
            route=a['route'];source=route['decision_source'] if route else 'NO_ROUTE_PERSISTED'
            if 'LOCAL_RULE_CAPABILITY_CHOICE_BLOCKED_ZERO_EXECUTION' in a['failure_categories']:source='RULE_REJECTED_BY_SCOPE'
            elif a['guarded_capability_rejection']:source='LLM_REJECTED_BY_SCOPE'
            sources[source]+=1
            totals['provider_participating_trials']+=r['trial']['usage']['calls_by_category']['provider']>0
            totals['provider_zero_trials']+=r['trial']['usage']['calls_by_category']['provider']==0
            totals['application_reads']+=len(archive['read_invocations'])
            interaction=archive.get('owner_interaction')
            if interaction:
                totals['scripted_owner_api_calls']+=len(interaction['calls'])
                totals['scripted_owner_policy_reads']+=len(interaction.get('policy_reads',[]))
                totals['scripted_owner_succeeded']+=interaction.get('state_check')=='PASS'
            module=archive.get('retrieval_evaluation')
            if module:totals['separate_retrieval_measurements']+=len(module['calls'])
            retrieved={}
            for call in archive['read_invocations']:
                if call['category']=='policy_retrieval' and call['status']=='RETURNED':
                    for evidence in call['retrieval_result']['evidence']:retrieved[evidence['chunk_id']]=evidence
            ids=set()
            for c in payload.get('citations',[]):
                citation_count+=1;original=retrieved.get(c['chunk_id'])
                if original is not None and all(c.get(k)==original.get(k) for k in ('content','knowledge_version','index_fingerprint','version','effective_from','effective_to')):
                    valid_citations+=1;ids.add(c['chunk_id'])
            required={s for item in r['case']['expected_outcome']['required_evidence'] for s in item['source_ids']}
            required_total+=len(required);required_hits+=len(required & ids)
            events=outer['observations']['run_events'];starts=[json.loads(e['payload_json']) for e in events if e['event_type']=='agent_started']
            handoffs=payload.get('handoffs',[])
            nodes.append({'case_id':r['case']['case_id'],'run_id':outer['run_id'],'experiment_id':experiment,'node_start_events':starts,'returned_handoffs':handoffs,
                'classification':'ACTUAL_MULTINODE_RULE_ORCHESTRATION' if len({s['agent'] for s in starts})>1 else 'SINGLE_NODE_OR_NO_AGENT',
                'provider_requests':r['trial']['usage']['calls_by_category']['provider'],'not_a_multi_vs_single_comparison':True})
        def ratio(n,N):return {'n':n,'N':N,'value':n/N if N else None,'wilson_95':wilson_interval(n,N)}
        counted=totals['application_reads']+totals['scripted_owner_api_calls']+totals['scripted_owner_policy_reads']+totals['separate_retrieval_measurements']
        actual=sum(r['trial']['usage']['calls_by_category']['tool'] for r in current)
        if counted!=actual:raise ValueError('separate tool accounting does not match actual ledger')
        result.append({'experiment_id':experiment,'actual_trials':len(current),'route_sources_corrected':dict(sources),
            'participation_and_separate_tool_accounting':dict(totals),'counted_tools':actual,
            'actual_delivered_policy_citation_lineage':ratio(valid_citations,citation_count),
            'delivered_required_source_coverage':ratio(required_hits,required_total),
            'citation_metric_scope':'Same-Run actual retrieval result,matching text/version/time/index;not semantic entailment or retriever Recall;candidate citations excluded'})
    put(root/OUT/'supplementary-metrics.json',{'groups':result,'scope':'No cross-version quality rate','supplier_invoice':'UNKNOWN'})
    put(root/OUT/'actual-node-and-handoff-index.json',{'rows':nodes,'scope':'Actual events and returned handoffs retained separately;not LLM autonomy inferred from AGENT/MULTI labels'})


def finalize_accepted_scope(root):
    """Project the recorded real user's bounded acceptance,without changing old scores."""
    from decimal import Decimal
    base=root/OUT;event=read(base/'final-user-acceptance-r1.json')
    for binding in event['source_bindings']:
        if sha(root/binding['path'])!=binding['sha256']:raise ValueError('accepted source changed')
    rows=load_trials(root);ai=validate_ai(rows,read(base/'ai-output-review.json'))
    accepted={i['case_id']:i for i in event['items']}
    if len(event['items'])!=108 or set(accepted)!={r['case']['case_id'] for r in rows}:raise ValueError('acceptance case scope differs')
    for r in rows:
        item=accepted[r['case']['case_id']]
        actual=(r['raw']['application_archive']['result']['run_id'],r['trial']['trial_id'],r['experiment_id'],r['raw']['scoring_runtime_sha256'],r['raw_sha256'],ai[item['case_id']]['task_outcome'])
        if actual!=tuple(item[k] for k in ('run_id','trial_id','experiment_id','runtime','raw_sha256','accepted_task_outcome')):raise ValueError('accepted Run or score binding differs')
    counts=dict(Counter(i['accepted_task_outcome'] for i in accepted.values()))
    if counts!=event['accepted_task_counts']:raise ValueError('accepted counts differ')
    scopes=[]
    for prefix,name in [('DV','Dev8'),('FC','Main100')]:
        selected=[r for r in rows if r['case']['case_id'].startswith(prefix)]
        scopes.append({'scope':name,'trials':len(selected),'delivered':sum(bool(r['raw']['code_assessment']['delivered_answer']) for r in selected),
            'not_delivered':sum(not r['raw']['code_assessment']['delivered_answer'] for r in selected),
            'accepted_task_counts':dict(Counter(accepted[r['case']['case_id']]['accepted_task_outcome'] for r in selected))})
    segments=[]
    for experiment in sorted({r['experiment_id'] for r in rows}):
        selected=[r for r in rows if r['experiment_id']==experiment];runtime={r['raw']['scoring_runtime_sha256'] for r in selected}
        if len(runtime)!=1:raise ValueError('one segment has multiple runtimes')
        segments.append({'experiment_id':experiment,'runtime':runtime.pop(),'trials':len(selected),
            'accepted_task_counts':dict(Counter(accepted[r['case']['case_id']]['accepted_task_outcome'] for r in selected))})
    cost=sum(Decimal(str(r['trial']['usage']['estimated_usd'])) for r in rows)
    if cost.quantize(Decimal('.000000001'))!=Decimal('.141540000'):raise ValueError('known ledger amount differs')
    if len(segments)!=6 or len({s['runtime'] for s in segments})!=5:raise ValueError('segment/runtime counts differ')
    partial={i['case_id'] for i in accepted.values() if i['accepted_task_outcome']=='PARTIAL'}
    if partial!={'FC26-ORDER-012','FC26-ORDER-013','FC26-CROSS_DOMAIN-047','FC26-SAFETY-055'}:raise ValueError('accepted partial scope differs')
    for r in rows:
        if not r['raw']['code_assessment']['delivered_answer'] and accepted[r['case']['case_id']]['accepted_task_outcome']!='FAIL':raise ValueError('undelivered outcome changed')
    old=read(base/'metrics-and-budget.json');budget=old['budget']
    if sum(r['trial']['usage']['calls_by_category']['provider'] for r in rows)!=budget['provider'] or sum(r['trial']['usage']['tokens'] for r in rows)!=budget['tokens']:raise ValueError('row usage/aggregate differs')
    backlog=[
        {'priority':'P1','issue':'订单明细和复合任务遗漏','evidence_cases':['FC26-ORDER-010','FC26-CROSS_DOMAIN-049','FC26-CROSS_DOMAIN-050','FC26-CROSS_DOMAIN-053','FC26-CROSS_DOMAIN-054'],'acceptance_direction':'自然输入下完成必要读取、逐行数量及政策/业务来源组合；不能用订单快照替代明细。'},
        {'priority':'P1','issue':'商品搜索参数与属性覆盖','evidence_cases':['FC26-PRODUCT-003','FC26-PRODUCT-004','FC26-PRODUCT-006'],'acceptance_direction':'保留合法参数来源，避免整段问题作keyword；实际取得目标属性并回答筛选/电压/材质要求。'},
        {'priority':'P1','issue':'动作任务仅回快照或只有限制说明','evidence_cases':['FC26-AFTER_SALES-017','FC26-AFTER_SALES-018','FC26-AFTER_SALES-020','FC26-AFTER_SALES-023','FC26-AFTER_SALES-026','DV26-AFTER_SALES-008'],'acceptance_direction':'分开验收Agent准备和用户交付、确认前零效果、确认后精确一次；脚本API成功不能替代Agent任务。'},
        {'priority':'P1','issue':'答案误拒及剩余语义作用域限制','evidence_cases':['DV26-POLICY-003','FC26-POLICY-029','FC26-POLICY-032','FC26-EXTRA-061'],'acceptance_direction':'引用编号期限误识别已有离线修复，保留34候选/17仍有限制的证据；扩大合法等价/错误对象条件回归，不回填历史未交付。'},
        {'priority':'P2','issue':'说明、补证与安全拒绝完整性','evidence_cases':sorted(partial),'acceptance_direction':'补足来源/观测未知、审核中事实、明确补证请求及对准目标的拒绝；安全与Task分列。'},
        {'priority':'LATER_NOT_STARTED','issue':'最终版本统一回归、消融与稳定性','evidence_cases':[],'acceptance_direction':'放入后续开发/阶段；本轮不执行，不以历史分段台账冒充同配置成绩或虚报M6.3已启动。'}]
    final={'schema':'m62-personal-final-closeout-v1','acceptance_event':event['event_id'],'acceptance_actor':event['actor'],'acceptance_method':event['method'],
        'stage_status':{'M6_1':'COMPLETE','M6_2_PERSONAL_PROJECT':'COMPLETE'},'stage_statement':'M6.2个人项目范围评测完成，质量问题保留待改进',
        'quality_status':'KNOWN_ISSUES_RETAINED;UNMEASURED_OR_UNMET_GATES_NOT_CHANGED_TO_PASS','strict_independent_Frozen':'NOT_CLAIMED;original identity and governance states retained',
        'scopes':scopes,'segments':segments,'runtime_count':5,'accepted_task_counts_batch_description':counts,'budget':budget,
        'metrics_source':'metrics-and-budget.json; supplementary-metrics.json','new_calls_this_closeout':{'Agent_Run':0,'Provider':0,'Judge':0,'Embedding':0},
        'unmeasured_not_required_for_approved_personal_exit':['Human atom-by-atom fact accuracy','This batch Judge-Human agreement/false-accept/false-reject'],
        'limitations':['Full-query retriever Recall is not application evidence use','Scripted owner action is not Agent autonomous completion','Offline candidate repair cannot reverse historical undelivered FAIL','6 segments/5 runtimes;40/108 and36/100 are batch descriptions,not final-version accuracy','Not independent blind review or unseen Gold/Frozen approval'],
        'backlog':backlog,'old_reports_unchanged':True,'future_execution_automatically_authorized':False}
    put(base/'final-stage-closeout-r1.json',final)
    matrix={'stage_status':final['stage_status'],'acceptance_event':event['event_id'],'items':[
        {'requirement':'参考/配置与本批方法锁定','status':'COMPLETE','evidence':'method-and-scope-change.json; implementation-bindings.json'},
        {'requirement':'Dev8+主集100实际执行、归档、结算、清理','status':'COMPLETE','evidence':'verification.json; shared-ledger-audit.json'},
        {'requirement':'路由100/检索67/主要Outcome60及全部输出评分','status':'COMPLETE_WITH_KNOWN_NEGATIVE_RESULTS','evidence':'metrics-and-budget.json; final-user-acceptance-r1.json'},
        {'requirement':'用户接受本批Task口径及争议处置','status':'COMPLETE_BULK_AI_ASSISTED_USER_ACCEPTANCE','evidence':'final-user-acceptance-r1.json'},
        {'requirement':'人工逐事实准确率/本批Judge-Human一致率','status':'NOT_MEASURED_NOT_REQUIRED_THIS_ACCEPTED_PERSONAL_SCOPE','evidence':event['event_id']},
        {'requirement':'严格独立未见Frozen/正式Gold等Gate','status':'NOT_CLAIMED_NOT_CHANGED','evidence':'method-and-scope-change.json'},
        {'requirement':'质量问题与后续阶段','status':'RETAINED_FOR_FUTURE_DEVELOPMENT_NOT_EXIT_BLOCKER','evidence':'final-stage-closeout-r1.json:backlog'}]}
    put(base/'exit-matrix-final-r1.json',matrix)
    verification={'case_count':len(rows),'unique_runs':len({r['raw']['application_archive']['result']['run_id'] for r in rows}),
        'accepted_task_counts':counts,'scopes':scopes,'execution_segments':6,'distinct_runtime_versions':5,'row_usage_equals_original_budget':True,
        'exact_acceptance_source_and_run_bindings_checked':True,'undelivered_fail_preserved':True,'historical_documents_rewritten':False,
        'new_calls':0,'new_deployment_or_full_regression':False,'source_bindings':event['source_bindings']}
    put(base/'final-closeout-verification-r1.json',verification)
    lines=['# M6.2个人项目范围最终收口','', '**M6.2个人项目范围 COMPLETE，质量问题保留待改进。** M6.1保持COMPLETE；本轮没有启动M6.3或新增评测。','',
        '用户接受事件：`'+event['event_id']+'`。Actor为CHAT_USER_SELF_CONFIRMED，方式为AI辅助Task评分与争议处理的批量接受。108个精确Run、原评分文件和原始证据哈希已绑定；不是108题独立人工重标注或逐事实审核。历史报告、原checker、失败、STOP及许可不改。','',
        '| 范围 | 真实Run | 已交付/未交付 | 已接受Task PASS/PARTIAL/FAIL |','|---|---:|---:|---:|']
    for s in scopes:
        c=s['accepted_task_counts'];lines.append(f"|{s['scope']}|{s['trials']}|{s['delivered']}/{s['not_delivered']}|{c.get('PASS',0)}/{c.get('PARTIAL',0)}/{c.get('FAIL',0)}|")
    lines+=['','合计40 PASS、4 PARTIAL、64 FAIL仅为本次分段批次描述统计。实际6个执行段、5个不同runtime，不能称6个不同实现，也不构成最终版本同配置准确率。原分段路由、检索、交付、Task指标及区间继续见metrics-and-budget.json。','',
        '实际用量：Provider83次、117950 tokens，本地保守估算USD0.14154，供应商账单UNKNOWN。原7许可关闭、预算预留0、108 receipt清理完成证据复用；本次新增Agent/Provider/Judge/Embedding均0。','',
        '四个PARTIAL仍为ORDER-012/013、CROSS_DOMAIN-047和SAFETY-055。所有未交付保留Task FAIL；候选离线修复不能翻转历史交付。用户接受的是任务评分口径，不为候选另造事实金标。','',
        '质量限制：模块检索Recall@5不等于应用实际使用证据；脚本动作不等于Agent自主完成。人工逐事实正确率及本批Judge-Human一致率未测，在本次批准的个人范围中不要求补测，不作质量Gate通过宣告。严格独立未见Frozen身份及Gold/Registry/M2等正式状态不改变。','',
        '## 后续开发（本轮不执行）','']
    lines.extend('- '+b['issue']+'：'+b['acceptance_direction']+' 证据：'+(', '.join(b['evidence_cases']) or '本轮版本分组与负结果台账')+'。' for b in backlog)
    lines+=['','## 可复用说明与入口','',
        '可对外准确表述：在自建合成业务数据上完成108题真实Agent应用评测，跨5个运行版本保留失败、用量和清理证据；Task评分为AI辅助、用户批量接受。不得表述为独立未见Frozen、统一配置40/108准确率或全部质量达标。','',
        '离线重建最终派生文件：`.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version r30 --finalize`。原报告入口不变，旧结果保持历史含义。新一致性核对见final-closeout-verification-r1.json；本次未重跑模型、全量回归或部署。','',
        '旧SHA清单仍绑定当时progress/方案/生成器快照。此次后继接受和追加内容导致这些可变文件与旧快照不同是已记录变更，不覆写旧清单，不新增签名或哈希审批层。']
    path=base/'final-stage-closeout-r1.md';text='\n'.join(lines)+'\n'
    if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('final closeout differs')
    path.write_text(text,encoding='utf8')
    return final
