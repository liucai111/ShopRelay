"""Offline, source-bound reporting for the existing personal execution ledger.

No model client, Runner or mutable business connection is created here. New AI
reviews are separate from original accepted labels and deterministic code facts.
"""
from pathlib import Path
from collections import Counter
import argparse,hashlib,json,sqlite3,socket
from eval.m6_1.contracts import EvalCase,sha256_json
from eval.m6_1.grants import formal_case_digest
from eval.m6_1.formal_scoring import evaluate
from eval.m6_2.personal_core_m63 import metric
from eval.m6_2 import personal_closeout_r30 as existing
from scripts.prepare_m63_retest import OUT,ROOT,read
TASK_CONFIG=None
REPORT_REVISION=None


def report_artifact(name):
    path=Path(name)
    return OUT/(path.stem+'-'+REPORT_REVISION+path.suffix if REPORT_REVISION else name)

def configure(path):
    global OUT,TASK_CONFIG
    from scripts import prepare_m63_retest as prep
    prep.configure(path);OUT,TASK_CONFIG=prep.OUT,prep.TASK_CONFIG


def write_round2(output):
    summary=output['summary'];budget=summary['task_budget'];comparison=summary['previous_round']
    main=next(g for g in summary['groups'] if g['phase']=='main')
    lines=['# 第二轮通用修复与固定100题重测','',
        '本报告为个人Dev已暴露资料的AI辅助事后预审，不是用户确认或独立Gold。历史51/100与前轮74/100原件均不变。','',
        '| 对象 | Run | PASS | PARTIAL | FAIL | Provider | tokens | 本地估算USD |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for group in summary['groups']:
        counts=group['task']['counts']
        lines.append(f"|{group['phase']}|{group['N']}|{counts.get('PASS',0)}|{counts.get('PARTIAL',0)}|{counts.get('FAIL',0)}|{group['provider_calls']}|{group['tokens']}|{group['estimated_usd']:.7f}|")
    lines += ['',f"前轮74 PASS保留{comparison['retained_PASS']}/74；前轮26未通过恢复{comparison['recovered_PASS']}/26；净变化{summary['main_strict_PASS']-74:+d}个百分点。原51 PASS保留{summary['original_51']['retained_PASS']}/51，原49未通过恢复{summary['original_49']['new_PASS']}/49。不同审核层/小验收绑定不合并，主集与变体各按本轮实际runtime分组。",
        f"主集严格PASS {main['task']['n']}/{main['task']['N']}，描述性Wilson95%区间{main['task']['wilson_95']['lower']:.2%}–{main['task']['wilson_95']['upper']:.2%}；未达85/90目标，不把PARTIAL合并成PASS。",
        f"主集规则路由{main['route_sources'].get('RULE',0)}、LLM路由{main['route_sources'].get('LLM',0)}；{main['actual_model_participation_n']}/100有真实模型请求。专业节点启动与真实handoff均单列，不把RULE取数写成LLM自主规划。最终序列化handoff被覆盖为空的本轮主集数量{main['handoff_projection']['empty_final_list_despite_m3_starts']}。",
        f"前轮13个未交付案例，本轮实际交付{comparison['previous_13_rejections_now_delivered']}/13；交付不等于Task正确。主集未运行{len(summary['main_not_run'])}。",'',
        '## 本次真实负结果与退化','',
        '- 主集9案未交付：AFTER_SALES-018、POLICY-038、CROSS_DOMAIN-048/049、EXTRA-064/073/074/079/096。候选均有受控文本；048另有被拒引用ID哈希，显示合法引用子集不证明原全部引用合法。其余局部期限/条件拒绝需按候选逐断言复核，不能全算模型幻觉或全算校验器误拒。',
        '- PRODUCT-001/004真实返回AGENT_CITATIONS_WITHOUT_ASSERTION；PRODUCT-002为回答schema额外type字段。工具已读但最终只有失败说明，Task FAIL；不是网络故障，不重采样。AFTER_SALES-021仍因缺paid_cents而未执行取消，Task FAIL；保留未知，不填零。',
        '- PARTIAL为AFTER_SALES-022确认后双状态未回接、024缺具体转人工草案/反馈、025未补问问题事实。独立owner/API效果与Agent交付分开，不能只凭API成功升级。',
        '- 原51退化3案为PRODUCT-001/004及AFTER_SALES-024；前轮74中退化7案，详见previous_round.regressions及逐案理由。前轮26未通过本轮恢复17，不隐藏新退化。',
        '- 新变体3 FAIL为PRODUCT-004的引用断言契约、POLICY-034候选否定性阶段说明被拒、EXTRA-061普通/商业并列窗口范围未解析。换问法仍可能失败，7/10不能宣称语义泛化已解决。', '',
        '## 修复与证据边界','',
        '- 通用局部期限解析保留明确主体、起算事件、条件与引用：支持前置/后置事件、名词标题、并列独立断言、具有/有的语法归一及禁止移用的独立关系。错误对象、数值、单位、起点、条件、否定和错引用仍以反例保护。没有移除护栏或按case ID放行。',
        '- 退货/退款申请阶段采用实际状态读取；缺设备/安装条件或审批/正文附录资料时先有针对性澄清、零读取。简单库存/物流/申请快照复用既有确定性渲染；复杂组合仍由真实模型生成，不能统称LLM自主成功。',
        '- 库存件数与金额分值、已有来源与新鲜度、未查询与不存在在生成契约中分开。新拒绝审计分存business_answer/policy_answer受控正文；合成回答不再覆盖真实handoff。旧缺失段/事件不反造。',
        '- 82题保护并集为前轮74与原51，包含8个退化目标的实际CLI、scope、Repository、生成边界、校验、持久化和清理。外部推理为离线替身，不是82次模型验收；7个动作题该矩阵只验证聊天/草案，外部确认效果由原有隔离动作回归保护。质量表现最终由本次真实试次报告，离线通过不是Task全通过证明。',
        '- 旧10变体已用于回归，不称未见。本轮10个新变体在真实输出前锁定、未用于修复断言；作者/来源与调试上下文已暴露，不称独立盲测。',
        '- 本轮集中P1–P3，未扩展至P4稀疏金额与确认后反馈的业务契约修改，不伪造零金额、资格、确认、幂等或外部真实交易。', '',
        '## 离线验证与负例','',
        '- 首轮真实旧句复现12 FAIL/9 PASS，所有失败迭代及JUnit保留。完整受影响核心483项中482通过、1个引用替身测试失败；以实际首条返回来源纠正该测试后，其拒绝与schema两条均通过。后续51项覆盖变更后的商品/售后/汇总及真实CLI；物流完整10项通过；最终共享报告与本轮边界14项通过。',
        '- 全目录JUnit按测试稳定ID、各自最后一次执行去重，当前558个均PASS；这是多个受影响执行的去重覆盖，不是一次558项运行，也不是558个模型案例。完整记录在test-verification.json；未重跑无变化的历史全套。',
        '- 当前回放实际核验117个独立payload、同Run消息/路由/工具/政策、病例摘要与冻结输入、119文件运行依赖、逐案usage及receipt；两独立离线进程均MATCH，网络尝试0。错Run、漏项、已知未交付伪PASS、代填人工决定及健康快照覆盖负例均拒绝/保留。相同重算器一致不冒称独立人工验证。', '',
        '## 用量、绑定与清理','',
        f"全任务{budget['runs']} Run、Provider{budget['provider_calls']}、账本工具{budget['tool_calls']}、tokens{budget['tokens']}、本地保守估算USD{budget['estimated_usd']:.7f}；供应商账单UNKNOWN，Judge/Embedding0。独立账本不借手动聊天额度。",
        f"应用实读{budget['application_read_invocations']}，隔离owner/API及政策{budget['owner_API_and_policy_calls']}，分账不重叠。运行时长和分组模型参与见scores-and-evidence.json。",
        '- 小验收计划8案、实际7案：POLICY-030因启动前凭据激活/本地准备错误，已消费设置尝试但0 Run/0 Provider/0工具；精确清理后不重放小验收。其原题在主集正常执行一次。故总计117真实Run与118次已结算claim分别核验，不制造第118个Run。',
        '- 启动前账本组权限、公开签名文件可读性、控制端路径及进程凭据激活修复均保留旧错误和零执行对账；未改系统全局权限/密钥/既有信任。本轮后续健康快照的模式字段变化保留版本，不覆盖初次证明。',
        '- 授权、每案次数/预留/结算、原始payload、独立application文件与精确receipt由既有Loader及Code Grader重算。全部闭合情况见final-permit-closure.json、post-execution-health.json和verification.json。',
        '- 容器六代理为http://proxy:7892，经既有中继转宿主127.0.0.1:7892，NO_PROXY空、TLS开启、SDK retries0，无额外模型探针；工程联网不算Provider。',
        '- 定价依据[DeepSeek官方文档](https://api-docs.deepseek.com/quick_start/pricing/)，本地按全部token最高输出费率USD1.20/百万保守估算。当前演示18065未替换，无本轮Git写入、正式Gate变更或受保护内容访问。', '',
        '## 逐案新AI预审','',
        '完整问题、实际交付/受控候选、同Run工具和政策、节点/handoff、使用量、清理、具体绑定见scores-and-evidence.json。人工字段为空。','',
        '| Phase / Case | Run | Task建议 | 依据 |','|---|---|---|---|']
    for row in output['rows']:
        lines.append('|'+row['phase']+' / '+row['case_id']+'|'+row['run_id']+'|'+row['ai_review']['task_outcome']+'|'+row['ai_review']['rationale'].replace('|','/').replace('\n',' ')+'|')
    lines += ['', '## 重算与停止位置','',
        '离线入口：`python scripts/report_m63_retest.py --task-config docs/eval/m6.3-repair-retest-r2-20261102/task-config.json --check`。实际命令/绑定见request-*.json、frozen-manifest.json及docs/probes/m63-retest-round2-20261102。',
        '所有已授权试次完成后停止，不按剩余额度追加采样。新AI建议不覆盖旧人工或旧AI分数；未通过题、退化与证据限制保留，不为85%目标放宽评分。', '']
    (OUT/'closeout.md').write_text('\n'.join(lines),encoding='utf8')


def load():
    denied=[]
    def block(*a,**k):denied.append(True);raise RuntimeError('report is offline only')
    socket.socket.connect=block;socket.create_connection=block;socket.getaddrinfo=block
    design=read(OUT/'design.json');cases={c['case_id']:(phase,c) for phase,items in design['phases'].items() for c in items}
    trial_design={t['case_id']:t for t in design['trials']} if 'trials' in design else None
    original=read(ROOT/'docs/eval/m6.3-personal-execution-20261101/evidence/trial-cases.json')['cases']
    original_by_source={c['case_id'].split('-r1-',1)[1]:c for c in original if c['case_id'].startswith('M63-benchmark-B-r1-')}
    frozen_runtime=read(OUT/'frozen-manifest.json')['execution_runtime'] if (OUT/'frozen-manifest.json').exists() else None
    segment_runtime=read(OUT/'frozen-segments.json')['experiment_runtime'] if (OUT/'frozen-segments.json').exists() else None
    existing.OUT=OUT.relative_to(ROOT)
    loaded=existing.load_trials(ROOT)
    if TASK_CONFIG and TASK_CONFIG.get('comparison_design') and REPORT_REVISION is None and (OUT/'execution-stop.json').exists():
        # Recompute the original stopped report after later append-only trials;
        # its immutable recorded scope must not silently expand on re-export.
        original_stop=read(OUT/'execution-stop.json')
        not_yet_run={v['case_id'] for v in original_stop['not_run']}
        loaded=[r for r in loaded if r['case']['case_id'] not in not_yet_run]
    rows=[]
    for record in loaded:
        trial,raw=record['trial'],record['raw'];cid=trial['case_id']
        phase,payload=cases[cid];case=EvalCase.from_dict(payload)
        if formal_case_digest(case)!=raw['case_sha256']:raise ValueError('frozen design differs from actual case')
        archive=raw['application_archive'];assessment=raw['code_assessment']
        if archive['runtime_binding']['approved']!=archive['runtime_binding']['observed_at_end'] or raw['scoring_runtime_sha256']!=archive['runtime_binding']['approved']:
            raise ValueError('actual application/control runtime drift')
        approved_runtime=segment_runtime[record['experiment_id']] if segment_runtime else frozen_runtime
        if phase in {'main','repeats','variants'} and raw['scoring_runtime_sha256']!=approved_runtime:raise ValueError('final phase is not the one frozen runtime')
        if phase in {'main','repeats'}:
            source=original_by_source[trial_design[cid]['source_case_id'] if trial_design else cid.removeprefix('RETEST-main-')]
            same={**payload,'case_id':source['case_id']}
            if same!=source:raise ValueError('main input/fixture/reference changed from original personal snapshot')
        if read(ROOT/record['archive_path'])!=archive:raise ValueError('archive differs from persisted payload')
        recalculated=evaluate(case,archive,trial['usage'],allow_known_classifier_rejection=True)
        if recalculated!=assessment:raise ValueError('deterministic code assessment does not recompute: '+cid)
        outer=archive['result'];events=sorted(outer['observations']['run_events'],key=lambda e:e['seq'])
        messages=outer['observations']['messages']
        answer=assessment['delivered_answer'];delivered=[m for m in messages if m['role']=='assistant']
        if answer is not None and not any(m['content']==answer for m in delivered):raise ValueError('answer not an actual delivered message')
        route=assessment['route'];reads=[];sources={}
        for inv in archive['read_invocations']:
            audit=inv.get('tool_audit')
            if inv['category']=='policy_retrieval':
                values=inv['retrieval_result']['evidence']
                for value in values:sources[value['chunk_id']]=value
                reads.append({'operation':inv['operation'],'capability':audit['tool'] if audit else 'search_knowledge',
                    'evidence_ids':[v['chunk_id'] for v in values],'status':inv['status']})
            else:
                reads.append({'operation':inv['operation'],'capability':audit['tool'],'arguments':inv['arguments'],
                    'result_ref':audit['result_ref'],'run_id':audit['run_id'],'trace_id':audit['trace_id'],
                    'status':inv['status'],'result':inv['read_result']})
        source=trial_design[cid]['source_case_id'].removeprefix('FC26-') if trial_design else cid.split('FC26-',1)[-1] if phase=='main' else None
        runs=outer['observations']['runs']
        rejection=json.loads(runs[0]['error_detail_json']) if isinstance(runs[0]['error_detail_json'],str) else runs[0]['error_detail_json']
        candidate_sections=(rejection or {}).get('answer_rejection_audit',{}).get('candidate_sections')
        rows.append({'case_id':cid,'phase':phase,'source_case_id':'FC26-'+source if source else None,
            'trial_id':trial['trial_id'],'archive_path':record['archive_path'],'run_id':outer['run_id'],'raw_sha256':record['raw_sha256'],
            **({'design':trial_design[cid]} if trial_design else {}),
            **({'retrieval_queries':[{'invocation_id':inv['invocation_id'],'query':inv['query'],
                'retrieval_result':inv['retrieval_result']} for inv in archive['read_invocations']
                if inv['category']=='policy_retrieval' and inv['status']=='RETURNED']} if trial_design else {}),
            **({'post_confirmation_output':[
                {'path':c['path'],'answer':c['response']['answer'],'status_code':c['status_code']}
                for c in archive['owner_interaction']['calls']
                if c['path'].endswith('/confirm') and c['status_code']==200 and 'answer' in c['response']],
                'owner_delivery_scope':'Actual simulated owner/API response, separate from initial chat and LLM autonomy'}
                if trial_design and 'owner_interaction' in archive else {}),
            'runtime':raw['scoring_runtime_sha256'],'runtime_binding':archive['runtime_binding'],
            'question':case.input['user_query'],'requirements':payload['expected_outcome']['task']['requirements'],
            'expected_task':payload['expected_outcome'],'lifecycle':outer['status'],'answer':answer,'candidate':assessment['candidate'],
            **({'candidate_sections':candidate_sections} if TASK_CONFIG is not None else {}),
            **({'execution_diagnostics':[json.loads(e['payload_json']) for e in events if e['event_type']=='error'],
                'rejection_audit':(rejection or {}).get('answer_rejection_audit')} if TASK_CONFIG is not None else {}),
            'route':route,'actual_agents':[{**json.loads(e['payload_json']),'seq':e['seq']} for e in events if e['event_type']=='agent_started'],
            'handoff_projection':{'final_serialized_handoffs':outer['result']['handoffs'] if outer['result'] is not None and 'handoffs' in outer['result'] else None,
                'm3_start_records':[json.loads(e['payload_json']) for e in events if e['event_type']=='agent_started' and 'handoff_id' in json.loads(e['payload_json'])],
                'scope':'Start records prove real specialist tasks; an empty final handoff list does not reconstruct full completion payloads'},
            'handoffs':[json.loads(e['payload_json']) for e in events if e['event_type'] in {'handoff','agent_handoff'}],
            'reads':reads,'policy_sources':list(sources.values()),'code_assessment':assessment,
            'citations':outer['result']['citations'] if outer['result'] is not None and 'citations' in outer['result'] else [],
            'usage':trial['usage'],'latency_ms':trial['latency_ms'],'cleanup':record['cleanup'],'owner_interaction':archive.get('owner_interaction'),
            'human_review':None})
    assert len(rows)==len({r['case_id'] for r in rows})==len({r['run_id'] for r in rows})
    return rows,denied,loaded


def write_readable(output):
    if TASK_CONFIG is not None:return write_round2(output)
    summary=output['summary'];rows=output['rows'];main=[r for r in rows if r['phase']=='main']
    g=next(g for g in summary['groups'] if g['phase']=='main')
    budget=summary['task_budget'];lo=g['task']['wilson_95']['lower'];hi=g['task']['wilson_95']['upper']
    lines=['# P1–P3修复、固定100题重测与表达变体验证', '',
        '本轮执行及离线评分闭环已完成。质量目标未全部达到，停止新增调用。以下新Task评分为AI辅助事后建议，尚未获本轮人工确认。', '',
        f"主集 **{summary['main_strict_PASS']}/100 PASS、9 PARTIAL、17 FAIL**；严格PASS描述性Wilson95%区间{lo:.2%}–{hi:.2%}。未运行0，不因缺候选或未交付排除Task分母。没有达到85%或90%。",
        f"原51个历史PASS在新试次保留{summary['original_51']['retained_PASS']}/51；旧49案恢复{summary['original_49']['new_PASS']}/49为PASS；与历史已接受51/100相比净增23个百分点。旧分数不改写，新旧审核层不同，未伪称新人工成绩或纯Multi收益。", '',
        '| 执行对象 | 实际Run | PASS | PARTIAL | FAIL | Provider | tokens | 本地保守估算USD |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for group in summary['groups']:
        c=group['task']['counts']
        lines.append(f"|{group['phase']}|{group['N']}|{c.get('PASS',0)}|{c.get('PARTIAL',0)}|{c.get('FAIL',0)}|{group['provider_calls']}|{group['tokens']}|{group['estimated_usd']:.7f}|")
    lines += ['', '主集与变体使用同一最终runtime：`56c86832a44d29446f3660643644bbbc32c5286c5d878b1568e5fe60341bdde7`。小验收为另一绑定，单独计分；8例小验收是原100题的重叠样本，不是额外8个独立问题。10个变体在真实输出前锁定，未用于修复测试，但作者/调试上下文并非独立盲测。', '',
        '## 实施与实际验证', '',
        '- P1：修复局部引导语、服务预约名词短语、申请谓词与业务主体的边界；保留时长、起算、条件、对象与本轮引用约束。25个原受影响完整候选回放通过，074旧错误负例仍拒绝。验收新句式再次暴露边界，离线修复后才冻结主集；没有重跑该验收失败。',
        '- P2：按自然输入进行先澄清/拒绝、显式商品属性、订单日期而非物流替代、订单与券组合、只读草案最小能力计划；两个订单行通过真实结果引用支持多商品详情。未按case ID编写生产分支，也未扩大工具白名单。PRODUCT-002新试次是RULE取数路径，不证明旧模型分类异常唯一根因已查明。',
        '- P3：修复Order与After-sales双专业节点的退款状态汇总，无需无关Policy节点；金额保留分值及未知币种；通用回答要求逐项事实、缺项询问、未知状态和来源ID。提示改进不是答案质量保证。',
        '- 全受影响第一轮417项通过；小验收修复后的完整受影响379项通过，含51个原PASS归档/权限保护及真实多案例CLI、正反候选、两条商品实读。报告最终8项绑定/误提PASS/真实关闭许可/JSON往返回归通过；重叠JUnit不相加。',
        '- 重要限制：51项保护覆盖旧归档重算、权限/范围等，不等于51题当前应用Task保护；本次实测仍发现8个历史PASS未保持。不得用离线回放宣称它们无退化。', '',
        '## 实测剩余缺陷与失败归因', '',
        '- 主集13题被答案门禁拒绝，用户未收到答案，均计Task FAIL。多份候选有充分来源支持，说明期限/主体/条件的自然表达校验仍不稳；不是13次网络失败，也不能全算模型幻觉。',
        '- 单独保留候选语义：POLICY-030/043、EXTRA-067/073/079/090/094的可见政策内容有直接来源支持；038/062/068/096还涉及参考提及、条件省略或作用域组合；074资源标识部分脱敏不能反推。',
        '- CROSS_DOMAIN-049的拒绝审计保存policy_answer段，不能将它当完整BusinessAnswer或据此断言business_answer未生成；实际未交付仍为FAIL。',
        '- PRODUCT-008先查详情违反零读取；AFTER_SALES-028没有读取申请状态；AFTER_SALES-021缺核心金额导致模拟取消未执行；EXTRA-100澄清未索取审批记录/具体事项。质量/数据契约失败没有停止其他独立案例。',
        '- 原P4仍未伪造缺失金额或取消确认，AFTER_SALES-022仍缺确认后实际状态回接。',
        '- 最终回答合并覆盖了handoff数组为空：真实agent_started/task_id/handoff_id、工具与数据依赖仍在原档，完整最终handoff序列不可重建。已在机器报告分列，未把AGENT/MULTI标签当自主协作成功。此问题未在主集中热修。', '',
        '## 原51个PASS在新试次未保持的8项', '', '| Case | 新建议 | 具体依据 |', '|---|---|---|']
    reg=set(summary['original_51']['regressions'])
    for r in main:
        if r['case_id'] in reg:lines.append('|'+r['source_case_id']+'|'+r['ai_review']['task_outcome']+'|'+r['ai_review']['rationale']+'|')
    lines += ['', '## 调用、来源、清理与绑定', '',
        f"全任务{budget['runs']}个真实Run、Provider{budget['provider_calls']}、账本tool{budget['tool_calls']}、tokens{budget['tokens']}、本地保守估算USD{budget['estimated_usd']:.7f}；Judge/Embedding0，供应商账单UNKNOWN。工具类别包含隔离模拟owner/API，不冒充全部只读业务调用，也不将应用镜像账本重复加总。",
        f"独立任务剩余保护额度Provider{budget['remaining_provider_cap']}、USD{budget['remaining_usd_cap']:.7f}未使用；本任务结束，不据此追加采样。未借手动演示100次/USD5额度、未重开历史账户。",
        f"主集路由来源RULE{g['route_sources'].get('RULE',0)}、LLM{g['route_sources'].get('LLM',0)}；{g['actual_model_participation_n']}/100题有真实模型请求，主要是回答。规则、分类、专业节点执行与模拟owner动作分列，不能称100题LLM自主完成。",
        '- 118个原始payload、case、独立application字节与receipt清理关联由既有loader核验，既有Code Grader重算一致；全部精确清理、预留0，最后目标无活动Run/Pending/lease。没有删库重建。',
        '- 控制及应用镜像的4个许可均已关闭、真实模型门禁关闭；启动前未消费镜像在核验零claim后用既有close方法补齐关闭事件，原快照不覆盖。详见final-permit-closure.json及unused-mirror-closure.json。',
        '- 启动前Windows换行与Dev split错误均0试次/0请求/0消费，错误文件、关闭的未消费许可及日志保留；后继许可共用同一任务账本。',
        '- 容器沿既有六代理变量`http://proxy:7892`转发到宿主`127.0.0.1:7892`，NO_PROXY空、TLS开启、SDK重试0；未使用端口9或独立模型探针。不是把容器localhost冒充宿主localhost。',
        '- 价格核验来自[DeepSeek官方文档](https://api-docs.deepseek.com/quick_start/pricing/)，对全部token按最高输出费率USD1.20/百万保守估算；不是供应商硬封顶。镜像构建曾发生工程依赖下载，与106次模型请求分开。',
        '- phase JSON外层split误写frozen_test；实际逐case和签发Registry均为dev，模式为PERSONAL_REAL_EVAL。已签发文件字节保持，不能借此标签声称正式Frozen。',
        '- 当前18065演示仍为customer-final-r2，本轮未替换；本地旧提交0bf7c39保留，本轮源码未提交、未推送。正式Gold/Registry/M2/Frozen Gate不变，不访问受保护题目内容。', '',
        '## 逐案AI预审（新人工字段全部为空）', '',
        '完整问题、完整实际交付/受控候选、同Run工具与来源、节点、字节绑定和用量见`scores-and-evidence.json`；旧人工标签仅作对照。', '',
        '| Phase / Case | Run | Task建议 | 依据 |', '|---|---|---|---|']
    for r in rows:
        lines.append('|'+r['phase']+' / '+r['case_id']+'|'+r['run_id']+'|'+r['ai_review']['task_outcome']+'|'+r['ai_review']['rationale'].replace('|','/').replace('\n',' ')+'|')
    lines += ['', '## 停止位置与下一步', '',
        '本轮118个试次及评分闭环完成，85/90目标未达到。新预审需要用户按精确Run集中裁决；来源有据但未交付、内容错误、数据契约和表达遗漏各自保留。没有用候选通过覆盖交付FAIL，也没有用未确认AI标签冒充正式Gold。',
        '若后续继续，优先处理结构化局部断言/来源条件匹配和完整候选归档、任务定向澄清/状态读取保护、确认后结果回接及handoff投影。应先补齐8个观察退化的行为保护，再讨论新验证，不自动重跑本轮。',
        '离线重算：`python scripts/report_m63_retest.py --check`。实际执行命令和精确配置见`request-*.json`、`frozen-manifest.json`及`docs/probes/m63-repair-retest-20261102/*execution*.log`；旧许可已关闭，不能复用运行。', '']
    (OUT/'closeout.md').write_text('\n'.join(lines),encoding='utf8')


def build_ab(check=False):
    """Same source loader/Code Grader/AI binding; new prespecified paired design."""
    from eval.m6_1.aggregate import cluster_bootstrap_interval
    from eval.m6_2.personal_core_m63 import performance
    rows,attempts,records=load();by=existing.validate_ai(records,read(report_artifact('ai-output-review.json')))
    design=read(OUT/'design.json')
    planned={c['case_id'] for cases in design['phases'].values() for c in cases}
    actual={r['case_id'] for r in rows}
    if len(rows)!=len(actual) or not actual<=planned:
        raise ValueError('the predetermined experiment has extra/duplicated trials')
    stop=read(report_artifact('execution-stop.json')) if actual!=planned else None
    if stop is not None and (stop['status']!='STOPPED_UNKNOWN_PROVIDER_USAGE' or
            {r['case_id'] for r in stop['not_run']}!=planned-actual or stop['actual']!=len(rows)):
        raise ValueError('incomplete experiment has no exact persisted hard-stop evidence')
    for row in rows:row['ai_review']=by[row['case_id']]
    def interval(values):
        return cluster_bootstrap_interval(values,metric='mean',seed=73,resamples=4000)
    groups=[]
    for phase,arm,runtime in sorted({(r['phase'],r['design']['variant'],r['runtime']) for r in rows}):
        current=[r for r in rows if (r['phase'],r['design']['variant'],r['runtime'])==(phase,arm,runtime)]
        measured=[{'trial':{'usage':r['usage'],'latency_ms':r['latency_ms']},'ai_review':r['ai_review'],
            'raw':{'code_assessment':r['code_assessment']}} for r in current]
        measured_performance=performance(measured)
        if any(r['usage'].get('unknown_tokens') or r['usage'].get('unknown_cost') for r in current):
            measured_performance['tokens_conservative_budget_consumption']=measured_performance['tokens']
            measured_performance['estimated_usd_conservative_budget_consumption']=measured_performance['estimated_usd']
            measured_performance.update(tokens=None,estimated_usd=None,usd_per_PASS=None)
        groups.append({'phase':phase,'arm':arm,'runtime':runtime,'N':len(current),
            'task':metric([r['ai_review']['task_outcome'] for r in current],repeated=phase=='repeats'),
            'delivery':metric([r['code_assessment']['checks']['delivery'] for r in current],repeated=phase=='repeats'),
            'performance':measured_performance,'route_sources':dict(Counter(r['route']['decision_source'] if r['route'] else 'MISSING' for r in current)),
            'real_model_participation':sum(r['usage']['calls_by_category']['provider']>0 for r in current),
            'execution_paths':dict(Counter((r['route']['strategy']+'/'+str(r['route'].get('sub_strategy'))) if r['route'] else 'MISSING' for r in current)),
            'actual_specialists':dict(Counter(str(a.get('agent',a.get('agent_name',a.get('node','UNRECORDED')))) for r in current for a in r['actual_agents'])),
            'actual_handoff_events':sum(len(r['handoffs']) for r in current),
            'serialized_completed_handoffs':sum(h['status']=='COMPLETED' for r in current for h in (r['handoff_projection']['final_serialized_handoffs'] or [])),
            'cross_specialist_handoffs':sum(h['status']=='COMPLETED' and h['source_agent'] not in {'supervisor',h['target_agent']} for r in current for h in (r['handoff_projection']['final_serialized_handoffs'] or [])),
            'initial_chat_non_delivery':sum(r['answer'] is None for r in current),
            'post_confirmation_API_outputs':sum(len(r.get('post_confirmation_output',[])) for r in current),
            'candidate_rejection_codes':dict(Counter(f['code'] for r in current if r['candidate'] for f in r['candidate']['validation_findings'])),
            'recorded_failure_categories_nonexclusive':dict(Counter(f for r in current for f in r['code_assessment']['failure_categories'])),
            'code_binary_metrics':{key:{'metric':metric([r['code_assessment']['checks'][key] for r in current if r['code_assessment']['checks'][key] in {'PASS','FAIL'}],repeated=phase=='repeats'),
                'excluded':dict(Counter(r['code_assessment']['checks'][key] for r in current if r['code_assessment']['checks'][key] not in {'PASS','FAIL'}))}
                for key in ('route_reference','required_tools','resource_coverage','result_row_coverage','simulated_owner_action')},
            'checks':{key:dict(Counter(r['code_assessment']['checks'][key] for r in current)) for key in ('route_reference','required_tools','resource_coverage','result_row_coverage')},
            'integrity':{key:dict(Counter(r['code_assessment']['integrity'][key] for r in current)) for key in ('permission','audit','terminal','usage')}})
    main=[r for r in rows if r['phase']=='main'];pairs=[]
    for source in design['cohorts']['route']:
        arms={r['design']['variant']:r for r in main if r['source_case_id']==source}
        if set(arms)!={'A','B'}:raise ValueError('main pair incomplete')
        labels={a:arms[a]['ai_review']['task_outcome'] for a in ('A','B')};order={'FAIL':0,'PARTIAL':.5,'PASS':1}
        winner='UNSCORABLE' if not set(labels.values())<=set(order) else 'TIE' if labels['A']==labels['B'] else max(labels,key=lambda a:order[labels[a]])
        pairs.append({'source_case_id':source,'labels':labels,'winner':winner,
            'strict_PASS_delta':None if winner=='UNSCORABLE' else int(labels['B']=='PASS')-int(labels['A']=='PASS')})
    stability={};selected=set(design['selected_24'])
    repeated=[r for r in rows if r['phase'] in {'main','repeats'} and r['source_case_id'] in selected]
    for arm in ('A','B'):
        cases=[]
        for source in sorted(selected):
            trials=sorted((r for r in repeated if r['source_case_id']==source and r['design']['variant']==arm),key=lambda r:r['design']['round'])
            rounds=[r['design']['round'] for r in trials]
            if len(set(rounds))!=len(rounds) or not set(rounds)<={1,2,3}:raise ValueError('invalid repeated rounds')
            if rounds!=[1,2,3] and stop is None:raise ValueError('three predetermined rounds missing')
            labels=[r['ai_review']['task_outcome'] for r in trials]
            unknown='UNSCORABLE' in labels or len(trials)!=3
            cases.append({'source_case_id':source,'labels':labels,'observed_rounds':rounds,'complete_three':len(trials)==3,'strict_passes':labels.count('PASS'),
                'all_three':False if any(x in {'FAIL','PARTIAL'} for x in labels) else None if unknown else True,
                'at_least_one':True if 'PASS' in labels else None if unknown else False})
        complete_cases=[c for c in cases if c['complete_three']]
        observations=[{'group_id':c['source_case_id'],'value':c['strict_passes']/3} for c in complete_cases if 'UNSCORABLE' not in c['labels']]
        stability[arm]={'N_cases':24,'N_trials':sum(len(c['labels']) for c in cases),'planned_trials':72,'complete_three_cases':len(complete_cases),
            'single_PASS_n':sum(c['strict_passes'] for c in cases),'trial_PASS_cluster_95':interval(observations),
            'interval_scope':'All24 cases have three observed trials; case-cluster interval.' if len(complete_cases)==24 else 'Only cases with all three observed; incomplete experiment selection may bias, not full24 stability.',
            'unscorable_trials':sum(c['labels'].count('UNSCORABLE') for c in cases),
            'all_three_n':sum(c['all_three'] is True for c in cases),'all_three_UNKNOWN':sum(c['all_three'] is None for c in cases),
            'all_three_cluster_95':interval([{'group_id':c['source_case_id'],'value':int(c['all_three'])} for c in cases if c['all_three'] is not None]),
            'at_least_one_n':sum(c['at_least_one'] is True for c in cases),'at_least_one_UNKNOWN':sum(c['at_least_one'] is None for c in cases),
            'at_least_one_cluster_95':interval([{'group_id':c['source_case_id'],'value':int(c['at_least_one'])} for c in cases if c['at_least_one'] is not None]),'cases':cases}
    repeat_pairs=[]
    for source in sorted(selected):
        for round_number in (1,2,3):
            pair={r['design']['variant']:r['ai_review']['task_outcome'] for r in repeated
                  if r['source_case_id']==source and r['design']['round']==round_number}
            if set(pair)!={'A','B'}:continue
            order={'FAIL':0,'PARTIAL':.5,'PASS':1}
            winner='UNSCORABLE' if not set(pair.values())<=set(order) else 'TIE' if pair['A']==pair['B'] else max(pair,key=lambda a:order[pair[a]])
            repeat_pairs.append({'source_case_id':source,'round':round_number,'labels':pair,'winner':winner,
                'strict_PASS_delta':None if winner=='UNSCORABLE' else int(pair['B']=='PASS')-int(pair['A']=='PASS')})
    repeat_delta=[{'group_id':p['source_case_id'],'value':p['strict_PASS_delta']} for p in repeat_pairs if p['strict_PASS_delta'] is not None]
    benchmark={}
    for arm in ('A','B'):
        current=[r for r in main if r['design']['variant']==arm];retrieval=[]
        for r in current:
            if r['source_case_id'] not in design['cohorts']['retrieval']:continue
            required={s for v in r['expected_task']['required_evidence'] for s in v['source_ids']}
            first=r['retrieval_queries'][0] if r['retrieval_queries'] else None
            ranked={s['chunk_id']:s['rank'] for s in first['retrieval_result']['evidence']} if first else {}
            found=required&set(ranked)
            retrieval.append({'source_case_id':r['source_case_id'],'reference_ids':sorted(required),
                'actual_query_count':len(r['retrieval_queries']),'first_query':first['query'] if first else None,
                'returned_ids':list(ranked),'all_reference_IDs_returned':required<=set(ranked),
                'first_reference_RR_at_returned_k':1/min(ranked[s] for s in found) if found else 0})
        benchmark[arm]={'all100':metric([r['ai_review']['task_outcome'] for r in current]),
            'primary_outcome60':metric([r['ai_review']['task_outcome'] for r in current if r['source_case_id'] in design['cohorts']['outcome']]),
            'retrieval67':{'N':len(retrieval),'all_reference_IDs_returned_n':sum(v['all_reference_IDs_returned'] for v in retrieval),
                'mean_reference_RR_at_returned_k':sum(v['first_reference_RR_at_returned_k'] for v in retrieval)/len(retrieval),'rows':retrieval,
                'qualification':'First actual application query per trial, not a union of best ranks across queries. Dev intended source-ID proxy; legal equivalent clauses may exist. Not semantic Recall/MRR or exhaustive qrels.'},
            'missing_run_or_audit_n':sum(r['code_assessment']['integrity']['audit']!='PASS' for r in current),
            'non_delivery_n':sum(r['answer'] is None for r in current)}
    old=read(ROOT/TASK_CONFIG['previous_ai_baseline']);oldB={r['source_case_id']:r for r in old['rows'] if r['phase']=='main'}
    historical_path=ROOT/'docs/eval/m6.3-personal-execution-20261101/metrics-and-budget.json'
    historical=read(historical_path)
    nowB={r['source_case_id']:r for r in main if r['design']['variant']=='B'}
    variant_pairs=[]
    if REPORT_REVISION:
        for source in sorted({r['source_case_id'] for r in rows if r['phase']=='variants'}):
            current={r['design']['variant']:r['ai_review']['task_outcome'] for r in rows if r['phase']=='variants' and r['source_case_id']==source}
            if set(current)!={'A','B'}:raise ValueError('new expression pair incomplete')
            variant_pairs.append({'source_case_id':source,'labels':current,'strict_PASS_delta':int(current['B']=='PASS')-int(current['A']=='PASS')})
    accepted=read(OUT/'baseline-protection.json')['all_original_labels'];original51={c.split('-r1-',1)[1] for c,v in accepted.items() if v=='PASS'}
    accounting=read(OUT/('accounting-'+str(len(rows))+'.json'))
    totals={'runs':len(rows),'provider':sum(r['usage']['calls_by_category']['provider'] for r in rows),'tools':sum(r['usage']['calls_by_category']['tool'] for r in rows),
        'tokens':sum(r['usage']['tokens'] for r in rows),'estimated_usd':sum(r['usage']['estimated_usd'] for r in rows),'supplier_invoice':'UNKNOWN'}
    unknown=[r for r in rows if r['usage'].get('unknown_tokens') or r['usage'].get('unknown_cost')]
    exception=read(OUT/'continuation-r1/confirmed-disposition.json') if REPORT_REVISION else None
    if unknown:
        if exception is not None:
            if (exception['decision']!='APPROVED_SINGLE_HISTORICAL_UNKNOWN_CONTINUATION' or
                    exception['allowed_unknown_reservations']!=accounting['unknown_usage_reservations'] or
                    len(unknown)!=1 or unknown[0]['trial_id']!=exception['allowed_unknown_reservations'][0]['trial_id']):
                raise ValueError('historical unknown exception expanded or changed')
        if (stop is None and exception is None) or accounting['unknown_usage_provider_calls']!=sum(r['usage']['calls_by_category']['provider'] for r in unknown):raise ValueError('unknown usage lacks stop/accounting evidence')
        totals.update(tokens_conservative_budget_consumption=totals['tokens'],estimated_usd_conservative_budget_consumption=totals['estimated_usd'],
            known_recorded_tokens=sum(r['usage']['tokens'] for r in rows if not r['usage'].get('unknown_tokens')),
            known_recorded_estimated_usd=sum(r['usage']['estimated_usd'] for r in rows if not r['usage'].get('unknown_cost')),
            unknown_usage_provider_calls=accounting['unknown_usage_provider_calls'],tokens=None,estimated_usd=None)
        for key in ('known_recorded_tokens','tokens_conservative_budget_consumption'):
            if totals[key]!=accounting[key]:raise ValueError('known/conservative tokens differ')
        for key in ('known_recorded_estimated_usd','estimated_usd_conservative_budget_consumption'):
            if abs(totals[key]-accounting[key])>1e-10:raise ValueError('known/conservative estimate differs')
    elif not accounting['known_settled'] or totals['tokens']!=accounting['tokens'] or abs(totals['estimated_usd']-accounting['estimated_usd'])>1e-10:raise ValueError('actual accounting differs')
    if any(totals[k]!=accounting[k] for k in ('runs','provider','tools')):raise ValueError('actual accounting counts differ')
    closure=read(report_artifact('final-permit-closure.json'))
    for side in ('controller','application'):
        permits=closure[side]['permits']
        if not all(g['closed'] for g in permits) or sum(g['claimed'] for g in permits)!=len(rows) or sum(g['settled'] for g in permits)!=len(rows) or closure[side]['mode']['real_provider_allowed']:raise ValueError('permits not closed/settled')
    delta=benchmark['B']['all100']['value']-benchmark['A']['all100']['value']
    operational=[]
    if REPORT_REVISION:
        from scripts.manage_m63_retest import classify_pretool_model_failure
        saved=read(OUT/'continuation-r1/pretool-model-failure-reassessment.json')
        record=next(r for r in records if r['trial']['trial_id']==saved['trial_id'])
        projected=next(r for r in rows if r['trial_id']==saved['trial_id'])
        recalculated=classify_pretool_model_failure(record['raw'],record['trial'],projected['cleanup'])
        if saved['raw_sha256']!=record['raw_sha256'] or recalculated!=saved['assessment'] or not recalculated['operational_continue']:
            raise ValueError('operational failed-stage reassessment/cleanup differs from actual source')
        operational.append(saved)
    family_intervals={}
    if REPORT_REVISION:
        family={r['source_case_id']:r['design']['group_id'] for r in main}
        family_intervals={'main':interval([{'group_id':family[p['source_case_id']],'value':p['strict_PASS_delta']} for p in pairs if p['strict_PASS_delta'] is not None]),
            'repeated':interval([{'group_id':family[p['source_case_id']],'value':p['strict_PASS_delta']} for p in repeat_pairs if p['strict_PASS_delta'] is not None]),
            'main_family_count':len(set(family.values())),'selected24_family_count':len({family[s] for s in selected}),
            'qualification':'Actual preserved semantic group_id clusters; source-case clustered historical intervals remain separately named. Wilson100 is descriptive only.'}
    summary={'groups':groups,'benchmark':benchmark,'main_pairs':pairs,'main_pair_wins':dict(Counter(p['winner'] for p in pairs)),
        'strict_B_minus_A_pp':delta*100,'paired_case_delta_95':interval([{'group_id':p['source_case_id'],'value':p['strict_PASS_delta']} for p in pairs if p['strict_PASS_delta'] is not None]),
        'unscorable_main_pairs':sum(p['strict_PASS_delta'] is None for p in pairs),'stability_24x3':stability,
        'stability_paired_trials':repeat_pairs,'stability_pair_wins':dict(Counter(p['winner'] for p in repeat_pairs)),
        'stability_paired_case_delta_95':interval(repeat_delta),
        'historical_original24':{'source':historical_path.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(historical_path.read_bytes()).hexdigest(),
            'strict_PASS_n':{arm:sum(s['labels'].count('PASS') for s in historical['stability'] if s['variant']==arm) for arm in ('A','B')},
            'repeat_summary':historical['repeat_summary'],'paired_case_bootstrap':historical['paired_case_bootstrap'],
            'qualification':'Historical complete24x3, original runtime and reviews retained. New incomplete repeat scope cannot replace it or claim updated full24x3.'},
        'experiment_complete':stop is None,'planned_trials':len(planned),'not_run':stop['not_run'] if stop else [],'hard_stop':stop,
        **({'historical_unknown_exception':exception,'actual_usage_fully_reconciled':False,'historical_stop_source':'execution-stop.json'} if exception is not None else {}),
        **({'operational_failure_reassessments':operational} if REPORT_REVISION else {}),
        **({'actual_group_id_delta_intervals':family_intervals} if REPORT_REVISION else {}),
        **({'new_expression_pairs':variant_pairs,'new_expression_pair_outcomes':dict(Counter('B' if p['strict_PASS_delta']>0 else 'A' if p['strict_PASS_delta']<0 else 'TIE' for p in variant_pairs))} if REPORT_REVISION else {}),
        'B_84_baseline':{'retained':sum(oldB[s]['ai_review']['task_outcome']=='PASS' and r['ai_review']['task_outcome']=='PASS' for s,r in nowB.items()),
            'regressions':[s for s,r in nowB.items() if oldB[s]['ai_review']['task_outcome']=='PASS' and r['ai_review']['task_outcome']!='PASS'],
            'new_PASS':[s for s,r in nowB.items() if oldB[s]['ai_review']['task_outcome']!='PASS' and r['ai_review']['task_outcome']=='PASS']},
        'original51_retained':sum(nowB[s]['ai_review']['task_outcome']=='PASS' for s in original51),'budget':totals,'network_attempts':len(attempts),
        'target_B_gt85_and_above_A':benchmark['B']['all100']['n']>=86 and delta>0,'human_confirmation':None,
        'comparison_scope':design['fairness'],'formal_metrics_unavailable':{'macroF1':'Current semantic route correctness labels not independently adjudicated; code route reference agreement is separate.',
            'semantic_Recall_MRR':'No exhaustive relevant/equivalent clause labels for current queries; ID proxy retained separately.',
            'atomic_fact_accuracy':'AI proposals available, no complete independently accepted atomic labels for new outputs.',
            'Judge_Human':'No new Judge authorized; prior different-object calibration retained, not mixed.'}}
    output={'summary':summary,'rows':rows};destination=report_artifact('scores-and-evidence.json')
    if check:
        if read(destination)!=output:raise ValueError('independent report differs')
        print(json.dumps({'status':'MATCH','real_trials':len(rows),'network_attempts':len(attempts)}));return
    destination.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    lines=['# 第三轮共同修复与A/B主集验证','', 'AI辅助事后预审，人工决定为空；不是正式Gold或未见Frozen。', '',
        f"实验状态：{'完整' if stop is None else '未完成：未知Provider用量硬停止'}；预定{len(planned)}次、实际{len(rows)}次、未运行{len(planned-actual)}次。A100/B100完整，稳定性及新表达是否完成另列。", '',
        '| 配置 | 严格PASS | PARTIAL | FAIL | 主要Outcome60 PASS |','|---|---:|---:|---:|---:|']
    for a in ('A','B'):
        v=benchmark[a]['all100'];lines.append(f"|{a}|{v['n']}/100|{v['counts'].get('PARTIAL',0)}|{v['counts'].get('FAIL',0)}|{benchmark[a]['primary_outcome60']['n']}/60|")
    displayed_interval=family_intervals['main'] if REPORT_REVISION else summary['paired_case_delta_95']
    interval_label='真实语义族group_id聚类' if REPORT_REVISION else 'case聚类'
    lines += ['',f"B−A严格PASS差{delta*100:+.2f}个百分点；目标B>85%且高于A：{summary['target_B_gt85_and_above_A']}。所有质量失败保留。",
        f"配对胜负B {summary['main_pair_wins'].get('B',0)}、A {summary['main_pair_wins'].get('A',0)}、平局{summary['main_pair_wins'].get('TIE',0)}；严格PASS差{interval_label}95%区间{displayed_interval['lower']*100:.2f}至{displayed_interval['upper']*100:.2f}个百分点，包含0，不宣称显著优于。",
        '单策略Wilson95%：'+ '；'.join(f"{a} {benchmark[a]['all100']['wilson_95']['lower']:.2%}–{benchmark[a]['all100']['wilson_95']['upper']:.2%}" for a in ('A','B'))+'。这些只是目的性、已暴露个人Dev样本的描述区间，不证明总体泛化。','',
        '## 预定三重复','', '| 策略 | 单次PASS | 至少一次pass@3 | 三次全PASS pass^3 |','|---|---:|---:|---:|']
    for a,s in stability.items():lines.append(f"|{a}|{s['single_PASS_n']}/{s['N_trials']}实际试次|{s['at_least_one_n']}/24，未知{s['at_least_one_UNKNOWN']}|{s['all_three_n']}/24，未知{s['all_three_UNKNOWN']}|")
    lines += ['', '原历史完整24×3仍为A 27/72、B 38/72；pass@3为14/24、17/24，pass^3为4/24、9/24，旧差15.28个百分点，保留原绑定。'+('当前重复已完整，按72个新观察重新计算，不混历史成功试次。' if stop is None else '当前部分重复未完成，不能以已观察53/54个试次替代旧完整72分母，也不能宣称稳定性已提高。')]
    budget_text=(f"实际tokens/估算UNKNOWN；已知记录{totals['known_recorded_tokens']}tokens/USD{totals['known_recorded_estimated_usd']:.7f}，保守预算占用{totals['tokens_conservative_budget_consumption']}tokens/USD{totals['estimated_usd_conservative_budget_consumption']:.7f}；{totals['unknown_usage_provider_calls']}次请求用量未知" if unknown else f"tokens{totals['tokens']}、估算USD{totals['estimated_usd']:.7f}")
    lines += ['',f"所有{len(rows)}个实际试次 Provider{totals['provider']}，{budget_text}，账单UNKNOWN。独立任务，不占手动额度；各许可关闭、精确清理、预留0。",
        f"原84 PASS保留{summary['B_84_baseline']['retained']}/84；旧51 PASS保留{summary['original51_retained']}/51。退化及恢复按新Run单列，不继承旧人工标签。",'',
        '## 对照边界与指标','',design['fairness'],
        '相同基础修复、原始问题/Fixture/权限、模型/配置、政策时点、验证器与Task要求；B原有确定性汇总和A自主flat工具循环仍为实际策略差异，不宣称纯Multi因果收益。',
        '路由来源、实际模型参与、工具与来源ID、交付、用量及Run墙钟P50/P95按策略/阶段/绑定分列。原24题首试已预注册复用，只补固定两次，不按成功与否决定次数。变体独立报告，当前项目上下文开发，不称独立盲测。',
        '检索67只计算实际返回排名与Dev预期来源ID代理，未把引用存在当语义支持。正式macroF1、语义Recall/MRR、原子事实准确率和新Judge-Human仍缺适当参考或本轮Judge授权；不填零。', '',
        '## 主集真实路径、费用及检索代理','',
        '| 策略 | RULE/LLM路由 | 实际模型参与 | Provider/只读及owner调用 | tokens | 本地估算USD | 墙钟P50/P95秒 | 未交付 |',
        '|---|---|---:|---|---:|---:|---|---:|']
    for g in groups:
        if g['phase']!='main':continue
        p=g['performance'];lines.append(f"|{g['arm']}|{g['route_sources']}|{g['real_model_participation']}/100|{p['provider']}/{p['tools']}|{p['tokens']}|{p['estimated_usd']:.7f}|{p['p50_seconds']:.3f}/{p['p95_seconds']:.3f}|{g['initial_chat_non_delivery']}|")
    lines += ['', 'A主集30次flat控制器节点与52次共同政策节点；B的产品/订单/售后/政策节点按原事件分别统计。B有跨专业交接，不把所有100题都称多节点或LLM自主路由。',
        '主集检索67题：'+ '；'.join(f"{a} 首次实际查询返回全部预期来源ID {benchmark[a]['retrieval67']['all_reference_IDs_returned_n']}/67，首相关ID倒数排名均值{benchmark[a]['retrieval67']['mean_reference_RR_at_returned_k']:.4f}" for a in ('A','B'))+'。未检索计未命中；不同查询不取最好排名。此为Dev来源ID代理，不是语义Recall/MRR。',
        'route_reference为旧标签一致性，不能把白名单允许或旧SINGLE标签相同直接当正确路由；合法AGENT等价路径与语义任务要求分别预审。代码检查PASS/FAIL分母及NOT_APPLICABLE/UNKNOWN排除见code_binary_metrics。', '',
        '## 修复、仍失败及验证范围','',
        '共同修复实际请求的BusinessAnswer/Step schema与业务结果引用、局部期限主体/条件/起点和否定范围、缺损事实补问，以及确认API返回真实取消/退款/工单阶段。未修改owner、确认摘要、幂等或事务核心，不补零金额。',
        '主集B仍有期限/阶段候选拒绝；多份候选的局部事实有据，不能都归为模型幻觉。EXTRA-074特定对象仍未可靠判定，POLICY-038旧渠道条件遗漏的负例继续拒绝。候选AI内容复核及原校验发现均保留在ai_review.candidate_content_review，未交付照计FAIL。',
        'A跨域多次仅取首个订单或商品即结束，确实未完成后续必要读取；不是因未调用某个任意工具名就判FAIL。两策略AFTER_SALES-021缺paid_cents导致核心取消契约未物化，保留数据问题。',
        'PARTIAL包括目录状态误称库存、金额未说明分值、具体退货/取消草案或必要附件条件遗漏、延保登记前提遗漏。安全完整不等于任务完整；独立owner/API确认效果不证明模型已准备完整草案。',
        '最终235项政策/真实旧候选/反例/当前保护回归通过；路由与真实SSE断连等86项通过。完整CLI174项中SSE首次因未启用既有loopback选项失败，后续已按86项重验；1项PG工程回归因未提供显式测试连接跳过，未伪称通过。前端14项及类型检查通过。原失败迭代保留，重叠测试不相加。',
        '原有PG事务、Checkpoint、确认幂等、MCP/legacy/memory等未修改核心证据仅在对应范围复用，未宣称本轮重跑所有实验；新动作API输出、现有实际receipt与数据库identity单独核验。详见'+report_artifact('verification.json').name+'的重测/复用/未测矩阵。',
        '本轮未部署到18065演示（仍customer-final-r2）、未提交或推送、未调用Judge/Embedding，也未使用手动演示100次/USD5额度。', '',
        '## 每Trial可复核建议','', '| Case | Run | Task AI建议 | 理由 |','|---|---|---|---|']
    for r in rows:lines.append('|'+r['case_id']+'|'+r['run_id']+'|'+r['ai_review']['task_outcome']+'|'+r['ai_review']['rationale'].replace('|','/').replace('\n',' ')+'|')
    if stop:
        lines += ['', '## 硬停止及未完成范围','',
            f"预定{len(planned)}次，实际{len(rows)}次，未运行{len(stop['not_run'])}次。main A100/B100完整；三重复与新表达未完成，不能宣布完整实验收口或完整稳定性/泛化验证。",
            '已知RULE路由和本人订单行读取成功；下游模型步骤无可核验usage，仅保存ChatFailure，不能据60秒耗时断定网络、鉴权或某个schema根因。账本COMMIT保留null，保守占用不是实际token。没有重试或新请求；模型门关闭。',
            '先从供应商请求/用量记录核对该唯一失败请求；未核实前不继续。已消费案例不重放。37次稳定性和20次变体均为NOT_RUN，不填FAIL或PASS。',
            '部分重复集的已观测结果、case聚类区间单列；未完成情况与潜在选择偏差保留，不与完整24×3历史估计直接比较。']
    if exception is not None:
        lines += ['', '## 已确认单请求续跑例外','',
            '用户明确批准只对旧B-r3-ORDER-010保守预算续跑。该已消费失败不重放，实际usage/账单及远端完成状态仍UNKNOWN，原null与STOP不变；继续扣原120000tokens/USD0.50。新请求遇到UNKNOWN仍停止，没有修改应用/模型/Task评分。',
            '控制端以处置记录的精确reservation/trial绑定后继许可，沿同任务总账扣除原消耗及保守占用。不是将保守预留伪装成可核验usage；旧267报告和原3份关闭许可继续保留。']
        lines += ['', '后继首案A-r3-ORDER-010响应契约不合格但usage完整、零工具执行，旧检查器未识别阶段而保留现场。控制端按真实route/controller/error正向事件、已结算用量与原签名边界精确清理证明完成通用阶段复核，Task仍FAIL，原checker audit UNKNOWN不回填；无候选，不推断其正文内容。随后仅继续未消费案例，未修改冻结应用或评分。']
        lines += ['', '## 新表达配对与完整稳定性','',
            '| 策略 | 新表达严格PASS | PARTIAL | FAIL |','|---|---:|---:|---:|']
        for g in groups:
            if g['phase']=='variants':lines.append(f"|{g['arm']}|{g['task']['n']}/{g['N']}|{g['task']['counts'].get('PARTIAL',0)}|{g['task']['counts'].get('FAIL',0)}|")
        ci=summary['stability_paired_case_delta_95']
        lines += ['',f"新表达配对：{summary['new_expression_pair_outcomes']}。每表达每策略仅一次，冻结后未用于修复，不是独立未见Gold。B6/10不支持“换问法稳定解决”的结论。",
            f"完整24题三次：72对胜负{summary['stability_pair_wins']}；严格PASS差{ci['estimate']*100:.2f}个百分点，case聚类95%区间{ci['lower']*100:.2f}至{ci['upper']*100:.2f}个百分点。只适用于该预定24题，不能代替主集100题的区间或声称Multi普遍更优。",
            '剩余缺陷主要是局部期限/阶段句式误拒、flat控制器提前结束遗漏后续读取、目录状态/金额/草案条件等完整性；没有为了达85%热修或追加采样。报告的完整是324次计划执行及证据/评分覆盖完整，不代表每个Task正确、actual usage均已核实、正式Gate通过或本轮版本已部署。']
        fam=family_intervals['main'];rep=family_intervals['repeated']
        lines += ['',f"真实group_id复核：100个来源case实际上属于{family_intervals['main_family_count']}个语义族，选定24题属于{family_intervals['selected24_family_count']}族。旧source-case区间保留作历史描述，不冒称100个独立语义族。",
            f"按实际语义族聚类的当前主区间：主集B−A {fam['estimate']*100:.2f}个百分点，95% {fam['lower']*100:.2f}至{fam['upper']*100:.2f}个百分点；重复集{rep['estimate']*100:.2f}个百分点，95% {rep['lower']*100:.2f}至{rep['upper']*100:.2f}个百分点。Task计数/阈值未变，不跨组拼接成绩。"]
        # Keep the substantive continuation results before the long Trial table.
        start=lines.index('## 已确认单请求续跑例外')-1
        details=lines[start:];lines=lines[:start]
        position=lines.index('## 每Trial可复核建议')
        lines[position:position]=details+['']
    report_artifact('closeout.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({'runs':len(rows),'A':benchmark['A']['all100']['counts'],'B':benchmark['B']['all100']['counts'],'delta_pp':delta*100,'budget':totals}))


def build_targeted(check=False):
    """Small repaired-path validation over the same archive/Code Grader loader."""
    rows,attempts,records=load()
    review=read(OUT/'ai-output-review.json')
    by=existing.validate_ai(records,review)
    for row in rows:row['ai_review']=by[row['case_id']]
    accounting=read(OUT/f'accounting-{len(rows)}.json')
    summary={'scope':'TARGETED_REPAIR_VALIDATION_NOT_FULL_COHORT_RETEST',
        'planned_N':len(TASK_CONFIG['targeted_specs']),'actual_runs':len(rows),
        'old_main_Task_unchanged':{'A':79,'B':83},
        'groups':{arm:{'N':sum(r['design']['variant']==arm for r in rows),
            'Task':dict(Counter(r['ai_review']['task_outcome'] for r in rows if r['design']['variant']==arm)),
            'delivered':sum(r['answer'] is not None for r in rows if r['design']['variant']==arm)} for arm in ('A','B')},
        'task_budget':accounting,'network_attempts':len(attempts),
        'all_cleaned':all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in rows),
        'runtime':read(OUT/'frozen-manifest.json')['execution_runtime']}
    if (OUT/'frozen-segments.json').exists():
        summary['runtime_groups']=dict(Counter(r['runtime'] for r in rows))
        summary['runtime']='MULTIPLE_FROZEN_SEGMENTS_SEPARATELY_REPORTED'
    value={'summary':summary,'rows':rows}
    path=OUT/'scores-and-evidence.json'
    if check:
        if read(path)!=value:raise ValueError('targeted archive/report recompute differs')
        print(json.dumps({'status':'MATCH','N':len(rows),'network_attempts':len(attempts)}));return
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))


def build(check=False):
    if TASK_CONFIG and TASK_CONFIG.get('targeted_specs'):
        return build_targeted(check)
    if TASK_CONFIG and TASK_CONFIG.get('comparison_design'):return build_ab(check)
    rows,attempts,records=load();review=read(OUT/'ai-output-review.json')
    # Reuse the existing output review binding/known-failure invariant checker.
    by=existing.validate_ai(records,review)
    for r in rows:
        r['ai_review']=by[r['case_id']]
    baseline=read(OUT/'baseline-protection.json')['all_original_labels'];old_by_source={k.split('-r1-',1)[1]:v for k,v in baseline.items()}
    main=[r for r in rows if r['phase']=='main'];retained=[r for r in main if old_by_source[r['source_case_id']]=='PASS']
    recovered=[r for r in main if old_by_source[r['source_case_id']]!='PASS']
    groups=[]
    for phase,runtime in sorted({(r['phase'],r['runtime']) for r in rows}):
        group=[r for r in rows if r['phase']==phase and r['runtime']==runtime]
        groups.append({'phase':phase,'runtime':runtime,'N':len(group),'task':metric([r['ai_review']['task_outcome'] for r in group]),
            'delivery':metric([r['code_assessment']['checks']['delivery'] for r in group]),
            'lifecycle':dict(Counter(r['lifecycle'] for r in group)),
            'route_sources':dict(Counter(r['route']['decision_source'] if r['route'] else 'MISSING' for r in group)),
            'actual_model_participation_n':sum(r['usage']['calls_by_category']['provider']>0 for r in group),
            'provider_calls':sum(r['usage']['calls_by_category']['provider'] for r in group),
            'tool_calls':sum(r['usage']['calls_by_category']['tool'] for r in group),
            'tokens':sum(r['usage']['tokens'] for r in group),'estimated_usd':sum(r['usage']['estimated_usd'] for r in group),
            'failure_categories':dict(Counter(v for r in group for v in r['code_assessment']['failure_categories'])),
            'code_checks':{key:metric([r['code_assessment']['checks'][key] for r in group]) for key in
                ('route_reference','required_tools','resource_coverage','result_row_coverage')},
            'integrity':{key:metric([r['code_assessment']['integrity'][key] for r in group]) for key in ('audit','permission','terminal','usage')},
            'handoff_projection':{'with_real_m3_start_records':sum(bool(r['handoff_projection']['m3_start_records']) for r in group),
                'empty_final_list_despite_m3_starts':sum(bool(r['handoff_projection']['m3_start_records']) and r['handoff_projection']['final_serialized_handoffs']==[] for r in group),
                'qualification':'Actual starts, task IDs and read-result links retained; full serialized final handoff details are not reconstructed'},
            'actual_specialist_count_distribution':dict(Counter(str(len({a['agent'] for a in r['actual_agents']})) for r in group))})
    summary={'scope':'PERSONAL_DISCLOSED_DEV_RETEST_NOT_FORMAL_FROZEN','groups':groups,
        'main_planned':100,'main_actual':len(main),'main_not_run':[c['case_id'] for c in read(OUT/'design.json')['phases']['main'] if c['case_id'] not in {r['case_id'] for r in main}],
        'original_51':{'actual':len(retained),'retained_PASS':sum(r['ai_review']['task_outcome']=='PASS' for r in retained),
            'regressions':[r['case_id'] for r in retained if r['ai_review']['task_outcome']!='PASS']},
        'original_49':{'actual':len(recovered),'new_PASS':sum(r['ai_review']['task_outcome']=='PASS' for r in recovered)},
        'main_strict_PASS':sum(r['ai_review']['task_outcome']=='PASS' for r in main),
        'task_budget':{'runs':len(rows),'provider_calls':sum(r['usage']['calls_by_category']['provider'] for r in rows),
            'tool_calls':sum(r['usage']['calls_by_category']['tool'] for r in rows),'tokens':sum(r['usage']['tokens'] for r in rows),
            'estimated_usd':sum(r['usage']['estimated_usd'] for r in rows),'reserved':0,
            'owner_API_and_policy_calls':sum(len(r['owner_interaction']['calls'])+len(r['owner_interaction'].get('policy_reads',[])) for r in rows if r['owner_interaction']),
            'application_read_invocations':sum(len(r['reads']) for r in rows),
            'trial_wall_seconds_sum':sum(r['latency_ms']['total_ms'] for r in rows)/1000,
            'no_external_Judge_or_Embedding':True,'remaining_provider_cap':1000-sum(r['usage']['calls_by_category']['provider'] for r in rows),
            'remaining_usd_cap':10-sum(r['usage']['estimated_usd'] for r in rows)},
        'supplier_invoice':'UNKNOWN','network_attempts':len(attempts),'human_acceptance':None,
        'qualification':'New AI-assisted Task proposals, not new human scores. Historical51/100 remains unchanged; heterogeneous acceptance phase is separate.'}
    accounting=read(OUT/('accounting-'+str(len(rows))+'.json'))
    if (budget := summary['task_budget'])['provider_calls']!=accounting['provider'] or budget['tokens']!=accounting['tokens'] or budget['tool_calls']!=accounting['tools'] or abs(budget['estimated_usd']-accounting['estimated_usd'])>1e-12 or not accounting['known_settled']:
        raise ValueError('independent controller accounting differs from sum of actual trial usage')
    summary['input_metadata_note']='Phase-wrapper split header says frozen_test; actual per-case split and signed registry are dev, mode PERSONAL_REAL_EVAL. Frozen bytes preserved; header does not confer formal Frozen access.'
    if TASK_CONFIG is not None:
        previous=read(ROOT/TASK_CONFIG['previous_ai_baseline'])
        earlier={r['source_case_id']:r for r in previous['rows'] if r['phase']=='main'}
        if set(earlier)!={r['source_case_id'] for r in main}:raise ValueError('previous-round comparison scope differs')
        summary['previous_round']={'source':TASK_CONFIG['previous_ai_baseline'],
            'reference_status':'PREVIOUS_AI_PROPOSALS_NOT_HUMAN',
            'retained_PASS':sum(earlier[r['source_case_id']]['ai_review']['task_outcome']=='PASS' and r['ai_review']['task_outcome']=='PASS' for r in main),
            'recovered_PASS':sum(earlier[r['source_case_id']]['ai_review']['task_outcome']!='PASS' and r['ai_review']['task_outcome']=='PASS' for r in main),
            'regressions':[r['case_id'] for r in main if earlier[r['source_case_id']]['ai_review']['task_outcome']=='PASS' and r['ai_review']['task_outcome']!='PASS'],
            'previous_13_rejections_now_delivered':sum(earlier[r['source_case_id']]['answer'] is None and r['answer'] is not None for r in main)}
        summary['input_metadata_note']='Round2 phase header and actual per-case split are dev; signed mode PERSONAL_REAL_EVAL, not formal Frozen.'
        summary['acceptance_scope']={'planned':8,'actual':sum(r['phase']=='acceptance' for r in rows),
            'pre_run_aborted':['RETEST-acceptance-POLICY-030'],
            'qualification':'Verified zero Run/Provider/tool setup abort remains consumed; original POLICY-030 executed once in main100, not replay of acceptance.'}
    closure=read(OUT/'final-permit-closure.json')
    for side in ('controller','application'):
        permits=closure[side]['permits']
        if len(permits)!=(len(closure['controller']['permits']) if TASK_CONFIG else 4) or not permits or not all(p['closed'] for p in permits) or closure[side]['mode']['real_provider_allowed']:
            raise ValueError('task permits or model gate remain open')
        aborted=0
        if TASK_CONFIG is not None:
            abort=read(OUT/'pre-run-zero-execution-cleanup.json')
            reconciled=read(OUT/'pre-run-budget-reconciliation.json')['snapshot']
            if abort['run_created'] is not False or abort['provider_calls']!=0 or abort['tool_calls']!=0 or not reconciled['complete'] or not reconciled['settled'] or reconciled['committed_calls']!=0:
                raise ValueError('pre-Run abort is not a verified zero-execution settlement')
            aborted=1
        if sum(p['claimed'] for p in permits)!=len(rows)+aborted or sum(p['settled'] for p in permits)!=len(rows)+aborted:
            raise ValueError('permit claim/settlement count differs from actual trials')
    summary['closure_proof']='final-permit-closure.json'
    output={'summary':summary,'rows':rows}
    destination=OUT/'scores-and-evidence.json'
    if check:
        if read(destination)!=output:raise ValueError('saved report differs from independent offline reconstruction')
        print(json.dumps({'status':'MATCH','actual_trials':len(rows),'main':len(main),'network_attempts':len(attempts)}))
    else:
        destination.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        write_readable(output)
        print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');p.add_argument('--inspect-only',action='store_true');p.add_argument('--task-config');p.add_argument('--revision',choices=['continuation-r1']);a=p.parse_args()
    REPORT_REVISION=a.revision
    if a.task_config:configure(a.task_config)
    if a.inspect_only:
        rows,_,_=load();(OUT/'review-inputs.json').write_text(json.dumps({'rows':rows},ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({'actual_trials':len(rows)}))
    else:build(a.check)
