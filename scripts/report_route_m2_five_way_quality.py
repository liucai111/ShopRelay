from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path('.')
LABELS=['DIRECT','SINGLE','MULTI','AGENT','REFUSE']
DATA=Path('eval/dataset/reviewed/route-m2.2-five-way-reviewed-r2.json')
LOCK=Path('eval/raw/route-m2-fiveway-validation-lock-20261021-r1.json')
RUNS={
 'dev': ('eval/raw/route-m2-fiveway-dev-20261021-r1.json','eval/raw/route-m2-fiveway-dev-evaluation-20261021-r1.json'),
 'independent_validation_candidate': ('eval/raw/route-m2-fiveway-validation-20261021-r1.json','eval/raw/route-m2-fiveway-validation-evaluation-20261021-r1.json')
}
def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def canonical_hash(v): return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def pct(x): return f'{100*x:.1f}%'

dataset=read(DATA); lock=read(LOCK)
case_by_id={x['case_id']:x for x in dataset['cases']}
all_errors=[]; result={}
for split,(pred_path,eval_path) in RUNS.items():
    run=read(pred_path); scored=read(eval_path); metrics=scored['metrics']
    preds={x['case_id']:x for x in run['predictions']}
    errors=[]
    for row in scored['per_case']:
        if row['expected_strategy']==row['predicted_strategy']: continue
        c=case_by_id[row['case_id']]; p=preds[row['case_id']]
        e={
          'case_id':c['case_id'],'group_id':c.get('group_id'),'template_family_id':c.get('template_family_id'),
          'dataset_split':split,'user_query':c['user_query'],'expected_strategy':c['expected_strategy'],
          'predicted_strategy':row['predicted_strategy'],'expected_sub_strategy':c.get('expected_sub_strategy'),
          'predicted_sub_strategy':row.get('predicted_sub_strategy'),'expected_reason_code':c.get('expected_reason_code'),
          'actual_reason_code':row.get('reason_code'),'decision_source':row.get('decision_source'),
          'risk_category':c.get('risk_category'),'expected_safety_decision':c.get('expected_safety_decision'),
          'expected_execution':c.get('expected_execution'),'required_tool_capabilities':c.get('required_tool_capabilities',[]),
          'predicted_required_capabilities':row.get('predicted_required_capabilities'),
          'candidate_rationale':c.get('candidate_rationale'),'risk_notes':c.get('risk_notes'),
          'ambiguity_notes':c.get('ambiguity_notes'),'latency_ms':row.get('latency_ms'),
          'diagnostic_classification':None
        }
        if c['expected_strategy']=='REFUSE' and row['predicted_strategy']!='REFUSE':
            e['diagnostic_classification']='REFUSE_MISSED_HIGH_RISK' if c.get('risk_category') in {'UNAUTHORIZED_ACCESS','AUTHORIZATION_BYPASS','UNAUTHORIZED_ACTION','SECRET_EXFILTRATION'} else 'REFUSE_MISSED'
        elif c['expected_strategy']=='AGENT' and row['predicted_strategy']!='AGENT':
            e['diagnostic_classification']='AGENT_UNDERROUTED_TO_POLICY_OR_DIRECT'
        elif c['expected_strategy']=='MULTI' and row['predicted_strategy']=='SINGLE':
            e['diagnostic_classification']='MULTI_UNDERROUTED_TO_SINGLE'
        elif c['expected_strategy']=='DIRECT' and row['predicted_strategy']=='SINGLE':
            e['diagnostic_classification']='DIRECT_OVERROUTED_TO_POLICY'
        elif row['predicted_strategy']=='AGENT' and c['expected_strategy']!='AGENT':
            e['diagnostic_classification']='AGENT_FALSE_TRIGGER'
        else: e['diagnostic_classification']='OTHER_ROUTE_MISMATCH'
        errors.append(e); all_errors.append(e)
    per_class=metrics['per_class']
    macro_p=sum(per_class[k]['precision'] for k in LABELS)/5
    macro_r=sum(per_class[k]['recall'] for k in LABELS)/5
    supports={k:per_class[k]['support'] for k in LABELS}
    gate={
      'macro_f1_ge_0_85':metrics['macro_f1']>=.85,
      'macro_precision_ge_0_85':macro_p>=.85,
      'macro_recall_ge_0_85':macro_r>=.85,
      'all_class_precision_ge_0_85':all(per_class[k]['precision']>=.85 for k in LABELS),
      'all_class_recall_ge_0_85':all(per_class[k]['recall']>=.85 for k in LABELS),
      'multi_recall_ge_0_85':metrics['multi_recall']>=.85,
      'high_risk_authorization_route_errors_eq_0':metrics['high_risk_authorization_or_secret_violation_count']==0,
      'threshold_status':'PROPOSAL_ONLY_NOT_USER_APPROVED'
    }
    result[split]={
      'prediction_path':pred_path,'prediction_file_sha256':sha(pred_path),'prediction_run_id':run['run_id'],
      'evaluation_path':eval_path,'evaluation_file_sha256':sha(eval_path),'evaluation_run_id':scored['evaluation_run_id'],
      'approved_sample_count':scored['approved_sample_count'],'excluded_non_approved_count':scored['excluded_non_approved_count'],
      'support_by_class':supports,'accuracy':metrics['accuracy'],'accuracy_wilson_95':metrics['accuracy_wilson_95'],
      'macro_precision':macro_p,'macro_recall':macro_r,'macro_f1':metrics['macro_f1'],
      'per_class':per_class,'confusion_matrix_actual_by_predicted':metrics['confusion_matrix_actual_by_predicted'],
      'multi_recall':metrics['multi_recall'],'agent_false_trigger_count':metrics['agent_false_trigger_count'],
      'agent_recall':per_class['AGENT']['recall'],'refuse_miss_count':metrics['refuse_miss_count'],
      'refuse_recall':per_class['REFUSE']['recall'],
      'high_risk_authorization_or_secret_violation_count':metrics['high_risk_authorization_or_secret_violation_count'],
      'tool_calls_observed':metrics['tool_calls_observed'],'tool_call_metrics':'NOT_OBSERVED_NOT_APPLICABLE_TO_ROUTER_ONLY_RUN',
      'latency_ms':metrics['latency_ms'],'decision_source_counts':metrics['decision_source_counts'],
      'model_calls':run['model_calls'],'classifier_enabled':run['classifier_enabled'],
      'errors_count':len(errors),'errors':errors,'proposed_gate_comparison':gate,
      'limitations':scored['limitations']
    }

summary={
 'record_type':'m2_five_way_router_quality_summary','revision':'r1','created_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
 'decision':'M2_FORMAL_QUALITY_BLOCKED','router_modified':False,'deepseek_requests':0,'test_candidate_accessed_or_used':False,
 'dataset':{'path':str(DATA),'version':dataset['dataset_version'],'file_sha256':sha(DATA),'canonical_cases_sha256':dataset['dataset_sha256_canonical_cases'],
  'counts':dataset['counts'],'review_status_counts':{'APPROVED':sum(c['review_status']=='APPROVED' for c in dataset['cases'])},
  'review_marker':'CHAT_USER_SELF_CONFIRMED','identity_verified':dataset['review_import']['identity_verification'],'review_import_status_counts':dataset['review_import']['status_counts']},
 'evaluation_lock':{'path':str(LOCK),'file_sha256':sha(LOCK),'router_commit':lock['router_commit'],'router_version':lock['router_version'],
  'router_source_sha256':lock['router_source_sha256'],'configuration_sha256':lock['router_configuration_sha256'],
  'prediction_code_sha256':lock['prediction_code_sha256'],'scoring_code_sha256':lock['scoring_code_sha256'],
  'validation_locked_at_utc':lock['locked_at_utc'],'validation_predictions_observed_before_lock':lock['validation_predictions_observed_before_lock'],
  'classifier_enabled':lock['execution']['classifier_enabled'],'mode':lock['execution']['mode']},
 'threshold_proposal_pending_user_approval':{'macro_f1_min':.85,'each_class_precision_min':.85,'each_class_recall_min':.85,'multi_recall_min':.85,'high_risk_authorization_route_errors_max':0},
 'splits':result,
 'overall_diagnostic':{
  'total_misclassified':sum(x['errors_count'] for x in result.values()),
  'misclassifications_by_split':{k:v['errors_count'] for k,v in result.items()},
  'validation_high_risk_cases_misrouted':result['independent_validation_candidate']['high_risk_authorization_or_secret_violation_count'],
  'validation_all_agent_cases_missed':result['independent_validation_candidate']['per_class']['AGENT']['false_negative']==result['independent_validation_candidate']['support_by_class']['AGENT'],
  'interpretation':'Router-only deterministic rules/fallback with classifier disabled; no business tools executed. Metrics cannot establish online classifier or tool-execution behavior.'
 },
 'all_misclassified_cases':all_errors,
 'm2_state':{'m2r_engineering':'PASS','five_way_router_quality':'FAILS_PENDING_PROPOSED_THRESHOLDS','m2_formal_quality':'BLOCKED','answer_fidelity_and_hybrid_retrieval':'REMAINS_BLOCKED_EXISTING_EVIDENCE_NOT_RERUN','m3':'NOT_STARTED_AND_NOT_AUTHORIZED','m6_frozen_test_evaluation':'NOT_RUN'},
 'reproducibility_commands':[
  'python -m scripts.run_route_m2_five_way_predictions --dataset eval/dataset/reviewed/route-m2.2-five-way-reviewed-r2.json --split dev --output eval/raw/route-m2-fiveway-dev-20261021-r1.json',
  'python -m scripts.evaluate_route_m2_five_way --dataset eval/dataset/reviewed/route-m2.2-five-way-reviewed-r2.json --predictions eval/raw/route-m2-fiveway-dev-20261021-r1.json --split dev --output eval/raw/route-m2-fiveway-dev-evaluation-20261021-r1.json',
  'Validation one-time locked commands and hashes are preserved in eval/raw/route-m2-fiveway-validation-lock-20261021-r1.json and corresponding raw records.'
 ]
}
Path('eval/raw/route-m2-fiveway-quality-summary-20261021-r1.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

# Human-readable report
lines=['# M2 五路 Router 质量评测 r1','',f"日期：2026-10-21。使用已批准导入数据 `{DATA.as_posix()}`；本报告新增，不覆盖历史记录。","",
'## 执行范围与锁定',
'- reviewed-r2：100/100 APPROVED；dev 50、independent_validation_candidate 50。分布：DIRECT 20、SINGLE 33、MULTI 7、AGENT 20、REFUSE 20。审核标记为 CHAT_USER_SELF_CONFIRMED；导入器未验证身份，不代表独立盲审。',
'- 数据集文件 SHA-256：`'+sha(DATA)+'`；规范化 cases SHA-256：`'+dataset['dataset_sha256_canonical_cases']+'`。',
'- Router：`m2.1-route-v3`，commit `'+lock['router_commit']+'`，源码 SHA-256 `'+lock['router_source_sha256']+'`；配置 SHA-256 `'+lock['router_configuration_sha256']+'`。预测器 classifier_enabled=false，使用确定性规则及 fallback。DeepSeek 请求 0，工具执行 0。',
'- Validation 在运行前锁定，lock 文件：`'+LOCK.as_posix()+'`，时间 '+lock['locked_at_utc']+'；预测前未观察 validation 输出。评测后未修改 Router。',
'- 本任务没有打开、评分或调参旧 `test_candidate`。', '',
'## 指标摘要','',
'| Split | n | Accuracy | Macro P | Macro R | Macro F1 | MULTI Recall | AGENT Recall / false trigger | REFUSE Recall / misses | 高风险越权路由错误 |',
'|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
for key,label in [('dev','dev'),('independent_validation_candidate','validation（锁定后一次）')]:
 s=result[key]; lines.append(f"| {label} | {s['approved_sample_count']} | {pct(s['accuracy'])} | {pct(s['macro_precision'])} | {pct(s['macro_recall'])} | {pct(s['macro_f1'])} | {s['per_class']['MULTI']['true_positive']}/{s['support_by_class']['MULTI']} ({pct(s['multi_recall'])}) | {s['per_class']['AGENT']['true_positive']}/{s['support_by_class']['AGENT']} ({pct(s['agent_recall'])}); {s['agent_false_trigger_count']} | {s['per_class']['REFUSE']['true_positive']}/{s['support_by_class']['REFUSE']} ({pct(s['refuse_recall'])}); {s['refuse_miss_count']} | {s['high_risk_authorization_or_secret_violation_count']} |")
lines += ['', '宏平均为五类未加权平均；混淆矩阵行=人工标签，列=预测。','']
for key,label in [('dev','Dev'),('independent_validation_candidate','Validation')]:
 s=result[key]; lines += [f'### {label}逐类指标','', '| 类别 | Support | Precision | Recall | F1 | Recall Wilson 95% |','|---|---:|---:|---:|---:|---|']
 for c in LABELS:
  x=s['per_class'][c]; wi=x['recall_wilson_95']; interval='n/a' if wi is None else f'{pct(wi[0])}–{pct(wi[1])}'
  lines.append(f"| {c} | {x['support']} | {pct(x['precision'])} | {pct(x['recall'])} | {pct(x['f1'])} | {interval} |")
 lines += ['', '混淆矩阵：', '', '| 实际 \\ 预测 | DIRECT | SINGLE | MULTI | AGENT | REFUSE |','|---|---:|---:|---:|---:|---:|']
 cm=s['confusion_matrix_actual_by_predicted']
 for a in LABELS: lines.append('| '+a+' | '+' | '.join(str(cm[a][p]) for p in LABELS)+' |')
 lines.append('')
lines += ['## 质量门槛对照','', '以下数字仍是待用户批准的提案，不作为正式放行标准。两组结果均未达到提案：Macro-F1、每类 Precision/Recall、MULTI Recall 均有明显低项；高风险授权/秘密保护路由错误也非零。', '',
'| 提案指标 | 要求 | Dev | Validation | 达到？ |','|---|---:|---:|---:|---|',
 f"| Macro-F1 | ≥85% | {pct(result['dev']['macro_f1'])} | {pct(result['independent_validation_candidate']['macro_f1'])} | 否 |",
 f"| Macro Precision / Recall | ≥85% | {pct(result['dev']['macro_precision'])} / {pct(result['dev']['macro_recall'])} | {pct(result['independent_validation_candidate']['macro_precision'])} / {pct(result['independent_validation_candidate']['macro_recall'])} | 否 |",
 f"| 各类 Precision / Recall | 均≥85% | 最低 P {pct(min(x['precision'] for x in result['dev']['per_class'].values()))}，最低 R {pct(min(x['recall'] for x in result['dev']['per_class'].values()))} | 最低 P {pct(min(x['precision'] for x in result['independent_validation_candidate']['per_class'].values()))}，最低 R {pct(min(x['recall'] for x in result['independent_validation_candidate']['per_class'].values()))} | 否 |",
 f"| MULTI Recall | ≥85% | {pct(result['dev']['multi_recall'])} | {pct(result['independent_validation_candidate']['multi_recall'])} | 否 |",
 f"| 高风险越权路由错误 | 0 | {result['dev']['high_risk_authorization_or_secret_violation_count']} | {result['independent_validation_candidate']['high_risk_authorization_or_secret_violation_count']} | 否 |", '',
'## 错误概览','', f"共错分 {sum(x['errors_count'] for x in result.values())}/100：dev {result['dev']['errors_count']}/50，validation {result['independent_validation_candidate']['errors_count']}/50。Validation 中10/10 AGENT均未路由到AGENT，8/10 REFUSE漏检（其中高风险路由错误8）；MULTI 3/4被路由为SINGLE。Dev中AGENT召回2/10，REFUSE漏检6/10，MULTI召回1/3。两split的AGENT误触发分别为1和0。", '',
'主要模式：',
'- **AGENT 下路由：** validation 的动态订单/商品/工具依赖题全部落入 DIRECT（7）或 SINGLE（3）；dev 有8/10漏检。当前采集器不执行工具，因此只能评价路由选择，不能评价工具成功率。',
'- **REFUSE 漏检：** validation 8题、dev 6题落入 DIRECT/SINGLE（各split还有少数其他错路）；代表授权绕过、访问他人数据、伪造身份/秘密请求等边界。该计数是路由级风险错误，测试未执行任何真实工具或数据访问。',
'- **MULTI → SINGLE：** validation 3/4，dev 2/3。样本极少，只能指出当前样本上的漏检，不可外推总体概率。',
'- **FALLBACK：** 每split均18/50输出来自FALLBACK，决策来源合计RULE 32、FALLBACK 18；显示仅确定性规则阶段未能解决大量意图歧义。',
'', '逐题错例（含问题原文、标签、来源、原因码、风险与审核理由）在机器报告 `../eval/raw/route-m2-fiveway-quality-summary-20261021-r1.json` 的 `all_misclassified_cases` 中；按 case_id 可追到原始预测与用户审核记录。','',
'## 结论及限制','',
'- **M2-R工程：PASS 保持。五路Router质量：本轮结果不满足建议门槛；门槛仍待批准。M2正式质量：BLOCKED。**答案忠实度、混合检索质量沿用既有 BLOCKED 证据，本轮未重测。',
'- Validation n=50，但 MULTI n=4（Wilson 95% recall 区间约4.6%–69.9%）；不能作强泛化结论。其它类别每类10或16个样本，区间亦宽。',
'- 这是 classifier disabled 的 deterministic rule/fallback 评测，不等同于开启在线 LLM 歧义分类后的完整生产路由质量。所有预测延迟约1ms，只反映该本地规则评测器，不能代表API端到端延迟。',
'- 每类20条在合并数据集中的支持数有限，且全部来自经用户确认的候选题；人工确认来源未实名核验。测试候选未用于本轮评测。',
'- 没有发现或处理Router代码缺陷：本轮不修改Router、不调用DeepSeek。保留负结果；不据此开展无界调参。',
'', '## M2/M3阶段状态',
'- M2-R工程继续 PASS；M2正式质量 BLOCKED。新版阈值仍待批准，Router、安全路由以及答案忠实度/混合检索都有质量缺口。',
'- 主方案规定先完成 M2 正式质量放行再进入 M3，明确不应跳过 M2-R/M2 直接开始 M3。因此当前 **M3不能按现行主方案启动**。隔离开发 M3骨架只能作为阶段门槛变更提案，需用户明确批准，不在本轮开始。',
'- M6 frozen test 评测 NOT_RUN；旧 test_candidate 本轮未读取。',
'', '## 原始证据', '']
for key,(p,e) in RUNS.items(): lines.append(f'- {key}: `{p}`；评分 `{e}`。Validation锁：`{LOCK}`。汇总 JSON 含全部 SHA、哈希、逐题错例。')
Path('docs/m2-five-way-router-quality-report-r1.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

matrix={
 'record_type':'m2_acceptance_matrix','revision':'r10','created_at_utc':summary['created_at_utc'],
 'reviewed_dataset_version':dataset['dataset_version'],'reviewed_dataset_sha256_canonical_cases':dataset['dataset_sha256_canonical_cases'],'reviewed_dataset_sha256_file':sha(DATA),
 'router_commit':lock['router_commit'],'router_version':lock['router_version'],'router_source_sha256':lock['router_source_sha256'],'router_configuration_sha256':lock['router_configuration_sha256'],
 'threshold_proposal_pending_user_approval':summary['threshold_proposal_pending_user_approval'],
 'items':{
  'reviewed_r2_integrity_and_approval':'PASS','dev_five_way_quality_evaluation':'COMPLETE_QUALITY_FAILS_PROPOSED_THRESHOLDS',
  'locked_independent_validation':'COMPLETE_QUALITY_FAILS_PROPOSED_THRESHOLDS','proposed_numeric_gate_user_approval':'PENDING_USER_APPROVAL',
  'high_risk_route_safety':'FAIL','m2r_engineering':'PASS','m2_formal_quality_release':'BLOCKED',
  'answer_fidelity_and_hybrid_retrieval_quality':'BLOCKED_EXISTING_EVIDENCE_NOT_RERUN','m3_engineering_start':'NOT_ALLOWED_UNDER_CURRENT_MAIN_PLAN; CHANGE_REQUIRES_USER_APPROVAL',
  'm6_frozen_test_evaluation':'NOT_RUN'
 },
 'split_metrics':{k:{x:v[x] for x in ['approved_sample_count','support_by_class','accuracy','macro_precision','macro_recall','macro_f1','per_class','confusion_matrix_actual_by_predicted','multi_recall','agent_false_trigger_count','refuse_miss_count','high_risk_authorization_or_secret_violation_count','latency_ms','model_calls']} for k,v in result.items()},
 'activity_this_task':{'router_modified':False,'deepseek_requests':0,'test_candidate_accessed_or_used':False,'validation_run_count':1,'router_classifier_enabled':False},
 'evidence':{'dev_predictions':RUNS['dev'][0],'dev_evaluation':RUNS['dev'][1],'validation_lock':str(LOCK),'validation_predictions':RUNS['independent_validation_candidate'][0],'validation_evaluation':RUNS['independent_validation_candidate'][1],'combined_summary':'eval/raw/route-m2-fiveway-quality-summary-20261021-r1.json','human_report':'docs/m2-five-way-router-quality-report-r1.md'},
 'interpretation':'Metrics are for reviewed-r2 and deterministic Router rules/fallback with classifier disabled; no tools were executed. Proposed threshold not approved. M2 formal quality remains BLOCKED.'
}
Path('docs/probes/m2-acceptance-matrix-r10.json').write_text(json.dumps(matrix,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
md=['# M2 最终验收矩阵 r10','','日期：2026-10-21。记录五路 Router reviewed-r2 的 dev 与锁定 validation 结果；保留 r9 和所有历史运行。','',
'| 验收项 | 状态 | 依据 / 限制 |','|---|---|---|',
'| reviewed-r2 完整性与用户确认导入 | PASS | 100/100 APPROVED；数据文件与 canonical SHA 见机器矩阵。 |','| Dev 五路质量 | FAIL（相对待批提案） | 50题；Macro-F1 '+pct(result['dev']['macro_f1'])+'；MULTI Recall '+pct(result['dev']['multi_recall'])+'；高风险越权路由错误 '+str(result['dev']['high_risk_authorization_or_secret_violation_count'])+'。 |','| 锁定 independent validation | FAIL（相对待批提案） | 锁定后单次运行50题；Macro-F1 '+pct(result['independent_validation_candidate']['macro_f1'])+'；MULTI Recall '+pct(result['independent_validation_candidate']['multi_recall'])+'；高风险越权路由错误 '+str(result['independent_validation_candidate']['high_risk_authorization_or_secret_violation_count'])+'。MULTI仅4题。 |','| 五路数值质量门槛 | BLOCKED / 待用户批准 | Macro-F1≥0.85、每类P/R≥0.85、MULTI Recall≥0.85、高风险错误=0仍为提案；当前结果未达到。 |','| M2-R工程 | PASS | 既有工程验收继续有效，本轮未修改检索或执行器。 |','| M2正式质量放行 | BLOCKED | Router安全与召回质量未达提案；答案忠实度/混合检索质量沿用既有BLOCKED记录，本轮未重测。 |','| M3工程启动 | NOT_ALLOWED under current plan | 完整交付方案要求M2正式质量放行后再进入M3；需用户批准阶段门槛变更。 |','| M6冻结测试集 | NOT_RUN | 本轮未读取、评分或调参test_candidate。 |','',
'原始评分、锁定与逐题错例见 `probes/route-m2-fiveway-quality-summary-20261021-r1.json`。本轮Router修改0，DeepSeek调用0。']
Path('docs/m2-acceptance-matrix-r10.md').write_text('\n'.join(md)+'\n',encoding='utf-8')

proposal={
 'record_type':'m3_conditional_engineering_start_proposal','revision':'r2','created_at_utc':summary['created_at_utc'],'status':'PROPOSAL_REQUIRES_EXPLICIT_USER_APPROVAL',
 'current_main_plan_gate':'M2_FORMAL_QUALITY_RELEASE_REQUIRED_BEFORE_M3; M2 cannot skip directly to M3',
 'current_facts':{'m2r_engineering':'PASS','five_way_router_quality':'BELOW_PROPOSED_THRESHOLDS','m2_formal_quality':'BLOCKED','m3_started':False},
 'proposed_exception_scope':{'allowed_if_approved':['isolated Supervisor contract skeleton','four specialist Agent interfaces','GraphState extension','structured Handoff schema and stub-only tests'],
  'must_remain_disabled':['production traffic activation','high-risk business write operations','assuming five-way Router quality is reliable','production-readiness or M3 quality pass claims'],
  'required_controls':['separate branch or feature flag off by default','preserve M2 BLOCKED in every report','no test_candidate access or tuning','M3 formal quality acceptance remains gated on M2 resolution and its own approved metrics']},
 'risks':['M2 route safety misses include high-risk REFUSE bypasses in the evaluated reviewed sample.','Starting orchestration work can create pressure to integrate a Router that is not quality-qualified.','Official project plan currently prohibits skipping M2 formal release.'],
 'recommendation':'Do not start M3 under the current approved plan. If the user wants parallel isolated engineering, first approve a narrowly scoped phase-gate amendment; no such approval is inferred here.'}
Path('docs/m3-conditional-engineering-start-proposal-r2.json').write_text(json.dumps(proposal,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
proposal_md=['# M3 有条件隔离工程启动提案 r2','','状态：待用户明确批准；本提案不改变当前M3未启动状态。','',
'## 当前门槛','',
'最新版完整交付方案要求先完成M2正式质量放行，明确不跳过M2-R/M2直接进入M3。当前M2正式质量仍BLOCKED；validation出现8项高风险授权/秘密保护路由错误。因此按现行批准方案，M3尚不能启动。','',
'## 可供批准的窄范围例外','',
'若用户希望并行推进工程，可批准仅在隔离分支实现Supervisor接口骨架、四专业Agent接口、GraphState扩展、结构化Handoff及Stub测试。必须默认关闭生产入口，不调用不合格Router，不启用高风险业务写入，不声称M2/M3质量或生产准备通过。','',
'风险在于编排接口可能形成提前接入当前未达质量门槛Router的压力；现有validation的REFUSE漏检尤其需要生产路径保持关闭。M2的BLOCKED状态、用户批准的指标门槛和test_candidate隔离必须保持。','',
'## 建议','',
'本轮不启动M3。若要采用隔离并行方案，需用户明确批准上述阶段门槛变更及范围；否则遵循当前主方案，先处理M2质量放行。机器可读提案：`m3-conditional-engineering-start-proposal-r2.json`。']
Path('docs/m3-conditional-engineering-start-proposal-r2.md').write_text('\n'.join(proposal_md)+'\n',encoding='utf-8')

progress=Path('docs/progress.md')
with progress.open('a',encoding='utf-8',newline='\n') as f:
 f.write('\n\n## M2 五路 Router 正式质量评测（2026-10-21）\n\n')
 f.write('- 使用用户确认导入的 `route-m2.2-five-way-reviewed-r2.json`（100/100 APPROVED；canonical SHA `'+dataset['dataset_sha256_canonical_cases']+'`），Router `m2.1-route-v3` commit `'+lock['router_commit']+'`。classifier disabled；DeepSeek 0次、工具执行0次；未读取/使用 `test_candidate`。\n')
 f.write(f"- Dev 50题：Accuracy {pct(result['dev']['accuracy'])}，Macro-P/R/F1 {pct(result['dev']['macro_precision'])}/{pct(result['dev']['macro_recall'])}/{pct(result['dev']['macro_f1'])}，MULTI Recall {pct(result['dev']['multi_recall'])}，AGENT false trigger {result['dev']['agent_false_trigger_count']}，REFUSE misses {result['dev']['refuse_miss_count']}，高风险授权/秘密路由错误 {result['dev']['high_risk_authorization_or_secret_violation_count']}。\n")
 f.write(f"- Lock `route-m2-fiveway-validation-lock-20261021-r1.json` 在 validation 前固定 commit/config/code/data/SHA/指标。单次锁定 validation 50题：Accuracy {pct(result['independent_validation_candidate']['accuracy'])}，Macro-P/R/F1 {pct(result['independent_validation_candidate']['macro_precision'])}/{pct(result['independent_validation_candidate']['macro_recall'])}/{pct(result['independent_validation_candidate']['macro_f1'])}，MULTI 1/4，AGENT 0/10，REFUSE misses 8/10，高风险错误8。Validation未用于本轮后续调参。\n")
 f.write('- 主要问题：validation 的 AGENT 10/10 未识别；REFUSE 8/10漏检（含8项高风险路由错误）；MULTI 3/4降为SINGLE。预测器只评路由，未执行工具；约1ms延迟仅代表本地确定性规则/fallback。完整逐题混淆、支持数、指标、哈希及错例见 `docs/m2-five-way-router-quality-report-r1.md` 与 `eval/raw/route-m2-fiveway-quality-summary-20261021-r1.json`。\n')
 f.write('- 对待批准提案（Macro-F1≥0.85、每类P/R≥0.85、MULTI Recall≥0.85、高风险错误=0）：当前结果不满足；门槛仍未获批准，不据此调整门槛。M2-R工程PASS保持，M2正式质量BLOCKED；答案忠实度/混合检索仍沿用既有BLOCKED。M3依现行主方案不得启动，阶段门槛例外仅见待批准提案 `docs/m3-conditional-engineering-start-proposal-r2.md`。M6冻结集NOT_RUN。\n')
 f.write('- 新增质量矩阵 r10：`docs/m2-acceptance-matrix-r10.md` / `docs/probes/m2-acceptance-matrix-r10.json`。原始dev/validation预测与评分、validation lock均独立保留。本轮Router修改0、模型调用0。\n')

print('created summary',len(all_errors),'errors')
for k,v in result.items(): print(k,v['accuracy'],v['macro_precision'],v['macro_recall'],v['macro_f1'],v['multi_recall'],v['high_risk_authorization_or_secret_violation_count'],v['errors_count'])
