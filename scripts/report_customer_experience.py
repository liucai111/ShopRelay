"""Offline product-experience closeout from delivered HTTP/SQL/budget evidence."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from eval.m6_1.budget import BudgetLedger, BudgetLimits

ROOT=Path(__file__).resolve().parents[1]
PROOF=ROOT/'docs/probes/customer-experience-r1-20261101'
OUT=ROOT/'docs/delivery/customer-experience-20261101'
INPUTS={'real-acceptance.json':'customer-experience-r1',
    'real-acceptance-after-fix.json':'customer-experience-r2',
    'real-conversation-check.json':'customer-experience-r3',
    'real/real-browser-results.json':'customer-experience-r4',
    'real-final/real-browser-results.json':'customer-experience-r5',
    'real-budget-final/real-browser-results.json':'customer-experience-r7'}

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00'))

def build():
    archive={r['run_id']:r for r in read(PROOF/'exact-cleanup-archive.json')['runs']}
    closed=read(PROOF/'automated-ledger-closed.json')
    limits=BudgetLimits(**closed['limits'])
    recomputed=BudgetLedger.snapshot_from_events(events=closed['events'],limits=limits,locked_limits=closed['limits'])
    for field in ('provider_requests','tokens','active_reservations','calls_by_category'):
        assert recomputed[field]==closed['snapshot'][field]
    assert abs(recomputed['estimated_usd']-closed['snapshot']['estimated_usd'])<1e-10
    events=[json.loads(line) for line in (PROOF/'provider-events.jsonl').read_text().splitlines()]
    assert len(events)==recomputed['provider_requests']==10
    assert sum(e['prompt_tokens']+e['completion_tokens'] for e in events)==recomputed['tokens']
    assert abs(sum(e['local_peak_estimate_usd'] for e in events)-recomputed['estimated_usd'])<1e-10
    assert all(e['status']=='RESPONSE_RECEIVED' and e['finish_reason']=='stop' and e['usage_status']=='RECORDED' for e in events)
    rows=[];claimed_events=[]
    for source,image in INPUTS.items():
        for row in read(PROOF/source)['rows']:
            run=row['run'];stored=archive[run['run_id']]
            assert run['status']==stored['status'] and run['answer']==stored['result']
            route_events=[e for e in stored['events'] if e['type']=='route_decided']
            assert len(route_events)==1
            route=route_events[0]['payload']
            started=[e for e in stored['events'] if e['type']=='policy_retrieval_started']
            finished=[e for e in stored['events'] if e['type']=='policy_retrieval_finished']
            if started:
                assert {e['payload']['call_id'] for e in started}=={e['payload']['call_id'] for e in finished}
                assert all(e['payload']['run_id']==run['run_id'] and e['payload']['result_ref'] and e['payload']['evidence_ids'] for e in finished)
            calls=[e for e in events if stamp(run['started_at'])<=stamp(e['at'])<=stamp(run['finished_at'])]
            claimed_events.extend(e['reservation'] for e in calls)
            answer=stored['result']['answer'] if stored['result'] else None
            tools=[e['payload'] for e in stored['events'] if e['type']=='tool_finished']
            rows.append({'run_id':run['run_id'],'session_id':run['session_id'],'source':source,
                'image':image,'question':row['question'],'status':run['status'],'delivered_answer':answer,
                'route_strategy':route['strategy'],'route_source':route['decision_source'],
                'provider_requests':len(calls),'tool_finished_count':len(tools),
                'policy_retrieval_finished_count':len(finished),'error_code':stored['error_code'],
                'note':'目录检索回答不满足政策咨询，保留为失败观察' if route['reason_code']=='PRODUCT_CATALOG_SEARCH'
                    else '未交付；候选与用户答案分开' if answer is None else '已交付，不等于独立Task审核通过'})
    assert len(rows)==len(archive)==11
    assert len(claimed_events)==len(set(claimed_events))==len(events)
    cleanup=read(PROOF/'exact-cleanup-proof.json')
    assert cleanup['protected_session_before']==cleanup['protected_session_after']=={'sessions':1,'runs':2,'messages':4}
    assert not cleanup['remaining_runs'] and not cleanup['remaining_sessions'] and not any(cleanup['remaining_checkpoints'].values())
    test=ET.parse(PROOF/'offline-final-verified.xml').getroot().find('testsuite')
    assert test is not None and int(test.attrib['tests'])==336 and int(test.attrib['failures'])==0
    return {'status':'REAL_MODEL_AND_UI_DEPLOYED_ACCEPTANCE_PARTIAL','provider':'DeepSeek',
        'endpoint_host':'api.deepseek.com','model':'deepseek-flash','automatic_acceptance':'CLOSED',
        'user_initiated_accounting':'SEPARATE_PERSISTENT_LEDGER_INITIAL_GUARD_10_REQUESTS_USD1',
        'budget':recomputed,'invoice':'UNKNOWN','runs':rows,
        'lifecycle_counts':dict(Counter(r['status'] for r in rows)),
        'route_sources':dict(Counter(r['route_source'] for r in rows)),
        'policy_retrievals':sum(r['policy_retrieval_finished_count'] for r in rows),
        'business_reads':sum(r['tool_finished_count'] for r in rows),
        'last_deployment':read(PROOF/'final-deployment.json'),
        'limitations':['真实多轮解释没有完整成功交付，未宣称通过',
            '未知退货地址两次候选被拒；没有编造地址，但未交付有用澄清',
            '最终回复一次追问多项信息，简洁度与自然度仍需改善',
            '多版本工程观察不是M6成绩，不合并为统一质量通过率',
            '1个早期空UI会话未形成准确receipt，不选择删除；用户凭据与其他会话不清理'],
        'recalculation':'raw SQL archive vs HTTP result, route/retrieval call links, each request assigned to one Run by persisted time window; budget independently derived from append-only events',
        'historical_M6_results_changed':False,'human_task_scores_created':False}

def build_conversation(proof=None):
    """Reconcile this successor's HTTP, SQL archives and independently metered calls."""
    proof = proof or ROOT/'docs/probes/customer-multiturn-r1-20261101'
    closed = [read(proof/name) for name in ('closed-account.json','closed-successor-account.json')]
    snapshots = []
    for account in closed:
        limits = BudgetLimits(**account['limits'])
        snapshot = BudgetLedger.snapshot_from_events(events=account['events'], limits=limits, locked_limits=account['limits'])
        for field in ('provider_requests','tokens','active_reservations','calls_by_category'):
            assert snapshot[field] == account['snapshot'][field]
        assert abs(snapshot['estimated_usd']-account['snapshot']['estimated_usd']) < 1e-10
        assert account['account']['status']=='CLOSED' and snapshot['active_reservations']==0
        snapshots.append(snapshot)
    events = read(proof/'provider-events.json')
    requests = sum(s['provider_requests'] for s in snapshots)
    tokens = sum(s['tokens'] for s in snapshots)
    cost = sum(s['estimated_usd'] for s in snapshots)
    assert requests == len(events) <= 20 and cost <= 1
    assert tokens == sum(e['prompt_tokens']+e['completion_tokens'] for e in events)
    assert abs(cost-sum(e['local_peak_estimate_usd'] for e in events)) < 1e-10
    assert all(e['status']=='RESPONSE_RECEIVED' and e['usage_status']=='RECORDED' and e['finish_reason']=='stop' for e in events)
    review = {r['run_id']:r for r in read(ROOT/'docs/delivery/customer-conversation-20261101/answer-pre-review.json')['rows']}
    rows, claimed = [], []
    for number in (1,2,3,5):
        segment = proof/f'segment-{number}'
        receipt = read(segment/'receipt.json')
        sql = {r['run_id']:r for r in read(segment/'exact-cleanup-archive.json')['runs']}
        cleanup = read(segment/'exact-cleanup-proof.json')
        assert not cleanup['remaining_runs'] and not cleanup['remaining_sessions'] and not any(cleanup['remaining_checkpoints'].values())
        assert set(receipt['run_ids']) == set(sql)
        for scene in receipt['scenarios']:
            delivered = [m['content'] for m in scene['session']['messages'] if m['role']=='assistant']
            for step in scene['steps']:
                rid = step['run_id']; raw = sql[rid]; run = step['run']
                assert raw['status'] == run['status'] and run['status'] in {'SUCCEEDED','FAILED'}
                calls = [e for e in events if e['session_id']==scene['session_id'] and e['client_request_id']==step['client_request_id']]
                answer = (run.get('answer') or {}).get('answer')
                if run['status']=='SUCCEEDED':
                    assert answer and raw['result']['answer']==answer and answer in delivered
                    citations = {c['chunk_id'] for c in step['execution']['citations']}
                    saved_ids = {c if isinstance(c,str) else c['chunk_id'] for c in run['answer']['citations']}
                    assert saved_ids==citations
                    if run['answer'].get('strategy') in {'SINGLE','MULTI'}:
                        kinds = [e['type'] for e in raw['events']]
                        assert 'policy_retrieval_started' in kinds and 'policy_retrieval_finished' in kinds
                        assert citations <= {e['chunk_id'] for e in step['execution']['retrieved_evidence']}
                else:
                    assert not answer and raw['error_code']==run['error']['code']
                    assert calls, 'A classified failure must still reconcile its attempted request'
                annotation = review[rid]
                assert annotation['question']==step['question'] and annotation['delivered_answer']==answer
                assert annotation['answer_sha256']==hashlib.sha256((answer or '').encode()).hexdigest()
                assert all(annotation[k] is None for k in ('reviewer','reviewed_at','reviewer_decision','confirmation_event'))
                assert all(e['run_id']==rid for e in step['events'])
                claimed.extend(e['reservation'] for e in calls)
                route = (run.get('answer') or {}).get('route') or (run.get('error') or {}).get('details',{}).get('route') or {}
                rows.append({'segment':number,'run_id':rid,'question':step['question'],'scenario':scene['name'],
                    'status':run['status'],'route_source':route.get('decision_source','NOT_RECORDED'),
                    'strategy':route.get('strategy','NOT_RECORDED'),'answer':answer,'provider_requests':len(calls),
                    'trace_ids':sorted({e['trace_id'] for e in step['events']}),
                    'error':run.get('error'),'tool_calls':step['execution']['tool_calls'],
                    'raw_archive':(segment/'exact-cleanup-archive.json').relative_to(ROOT).as_posix(),
                    'tokens':sum(e['prompt_tokens']+e['completion_tokens'] for e in calls),
                    'estimated_usd':sum(e['local_peak_estimate_usd'] for e in calls),
                    'citations':step['execution']['citations'],'tool_references':step['execution']['tool_references'],
                    'latency_ms':run.get('latency_ms'),'cleanup':'ZERO_RESIDUAL','review':annotation})
    assert len(claimed)==len(set(claimed))==len(events)
    before=read(proof/'all-sessions-before.json'); after=read(proof/'all-sessions-after-final.json')
    assert before['sessions']==after['sessions'] and before['marker']==after['marker']
    assert before['automated-closed.json_sha256']==after['automated-closed.json_sha256']
    assert before['scope_sha256']==after['scope_sha256']
    assert [r for r in before['budget_events'] if r[0]=='delivery-user_initiated']==[r for r in after['budget_events'] if r[0]=='delivery-user_initiated']
    setup=read(proof/'segment-4/receipt.json')
    assert setup['session_ids']==setup['run_ids']==[]
    return {'scope':'bounded synthetic customer acceptance; not M6 rescoring','runs':rows,
        'lifecycle_counts':dict(Counter(r['status'] for r in rows)),
        'provider_requests':requests,'tokens':tokens,'estimated_usd':cost,'invoice':'UNKNOWN',
        'remaining_requests_unused':20-requests,'remaining_usd_unused':1-cost,
        'both_accounts':'CLOSED','active_reservations':0,'original_sessions_unchanged':len(before['sessions']),
        'human_task_scores_created':False,'setup_failure_without_run':True,
        'deployment':(proof/'final-image.txt').read_text(encoding='utf-8-sig').strip()}


def build_orders(proof=None):
    """Reconcile HTTP, persisted Run events, SDK usage and cleanup, offline."""
    proof = proof or ROOT/'docs/probes/customer-orders-20261101'
    receipt = read(proof/'real/segment-1/receipt.json')
    archive = {row['run_id']: row for row in read(proof/'archive.json')['runs']}
    closed = read(proof/'closed-account.json')
    account = closed['account']
    assert account['status'] == 'CLOSED'
    limits = BudgetLimits(provider_calls=account['provider_calls'], total_tokens=account['total_tokens'],
        estimated_usd=account['estimated_usd'], max_concurrent_trials=1, max_duration_seconds=120, max_retries=0)
    budget = BudgetLedger.snapshot_from_events(events=read(proof/'budget-events.json'),
        limits=limits, locked_limits=limits.to_dict())
    for field in ('provider_requests', 'tokens', 'active_reservations', 'calls_by_category'):
        assert budget[field] == closed['snapshot'][field]
    assert abs(budget['estimated_usd']-closed['snapshot']['estimated_usd']) < 1e-10
    sdk = read(proof/'provider-events.json')
    assert len(sdk) == budget['provider_requests'] == 1
    assert sum(row['prompt_tokens']+row['completion_tokens'] for row in sdk) == budget['tokens']
    assert abs(sum(row['local_peak_estimate_usd'] for row in sdk)-budget['estimated_usd']) < 1e-10
    assert all(row['finish_reason'] == 'stop' and row['usage_status'] == 'RECORDED' for row in sdk)
    reviews = {row['run_id']: row for row in read(ROOT/'docs/delivery/customer-orders-20261101/answer-pre-review.json')['rows']}
    rows = []
    for scenario in receipt['scenarios']:
        for step in scenario['steps']:
            run_id = step['run_id']
            stored = archive[run_id]
            payload = stored['result']
            assert step['run']['status'] == stored['status'] == 'SUCCEEDED'
            assert step['run']['answer'] == payload
            assert payload['answer'] and payload['trace_id']
            assert any(e['type'] == 'answer' for e in stored['events'])
            tools = payload['tool_call_audit']
            if payload['route']['strategy'] == 'AGENT':
                assert payload['route']['required_capabilities']
                assert set(payload['route']['required_capabilities']) == {call['tool'] for call in tools}
            for call in tools:
                assert call['run_id'] == run_id and call['trace_id'] == payload['trace_id']
                assert call['executed'] and call['status'] == 'COMPLETED'
                assert call['result_ref'] and call['parameter_fingerprint']
                assert any(ref['result_ref'] == call['result_ref'] for ref in payload['tool_references'])
                for kind in ('tool_started', 'tool_finished'):
                    assert sum(e['type'] == kind and e['payload']['tool_call_id'] == call['tool_call_id']
                               for e in stored['events']) == 1
                if call['tool'] in {'list_orders', 'get_order', 'get_order_items'}:
                    assert call['owner_scope'] == 'AUTHENTICATED_OWNER_FILTER'
            retrievals = [e['payload'] for e in stored['events'] if e['type'] == 'policy_retrieval_finished']
            for event in retrievals:
                assert event['run_id'] == run_id and event['trace_id'] == payload['trace_id']
                assert event['result_ref'] and event['executed'] and event['status'] == 'SUCCEEDED'
                assert event['evidence_ids']
            for citation in payload['citations']:
                assert any(citation['chunk_id'] in event['evidence_ids'] for event in retrievals)
            calls = [event for event in sdk if event['client_request_id'] == step['client_request_id']
                     and event['session_id'] == scenario['session_id']]
            review = reviews[run_id]
            assert review['reviewer'] is None and review['reviewer_decision'] is None
            rows.append({'run_id': run_id, 'question': step['question'], 'answer': payload['answer'],
                'lifecycle': stored['status'], 'route_source': payload['route']['decision_source'],
                'route': payload['route']['strategy'], 'actual_provider_requests': len(calls),
                'tools': [call['tool'] for call in tools],
                'business_reads': sum(call['tool'] != 'search_knowledge' for call in tools),
                'policy_retrievals': len(retrievals), 'citations': payload['citations'],
                'technical_audit': 'PASS', 'ai_pre_review': review})
    assert len(rows) == len(archive) == 4 and {row['run_id'] for row in rows} == set(receipt['run_ids'])
    assert sum(row['actual_provider_requests'] for row in rows) == budget['provider_requests']
    listing = rows[0]
    page = archive[listing['run_id']]['result']['business_results']['list_orders']
    fixture = read(proof/'fixture.json')
    assert len(page['orders']) == 5 and page['has_more'] and page['sort'] == 'order_id_desc'
    assert set(row['order_id'] for row in page['orders']) <= set(fixture['orders'])
    assert not set(row['order_id'] for row in page['orders']) & set(fixture['other_orders'])
    selected = archive[rows[1]['run_id']]['result']['business_results']['get_order']
    assert selected['order_id'] == page['orders'][0]['order_id'] and selected['paid_cents'] == 3200
    assert selected['paid_at'] is None and selected['delivered_at'] is None
    clean = read(proof/'cleanup.json')
    assert not clean['remaining_runs'] and not clean['remaining_sessions']
    assert not any(clean['remaining_checkpoints'].values()) and clean['protected_session_before'] == clean['protected_session_after']
    assert fixture['state'] == 'CLEANED' and read(proof/'fixture-cleanup.json')['original_sessions_preserved']
    before, final = read(proof/'runtime-preflight.json'), read(proof/'runtime-final.json')
    assert before['manual_baseline'] == final['manual_baseline']
    assert final['active_runs'] == final['pending'] == 0 and final['source_files_verified'] == 53
    initial = read(proof/'source-manifest-r2.json')['files']
    latest = read(proof/'source-manifest-final.json')['files']
    assert all(latest[name] == digest for name, digest in initial.items() if name.startswith('app/'))
    tests = {}
    for name in ('offline-r7.xml', 'offline-api-loopback.xml', 'offline-final-delta.xml', 'offline-closeout.xml'):
        suites = ET.parse(proof/name).getroot()
        for case in suites.iter('testcase'):
            assert case.find('failure') is None and case.find('error') is None
            tests[(case.attrib['classname'], case.attrib['name'])] = True
    return {'scope': 'PERSONAL_CUSTOMER_EXPERIENCE_NOT_M6_REEVALUATION', 'runs': rows,
        'budget': budget, 'actual_business_reads': sum(row['business_reads'] for row in rows),
        'actual_policy_retrievals': sum(row['policy_retrievals'] for row in rows),
        'ai_task_outcomes': dict(Counter(row['ai_pre_review']['task_outcome_ai'] for row in rows)),
        'human_grading': 'NOT_REVIEWED', 'backend_tests_distinct': len(tests),
        'cleanup': clean, 'original_sessions_preserved': len(fixture['preserved_sessions']),
        'manual_account_unchanged': True, 'application_python_unchanged_after_real_acceptance': True,
        'supplier_invoice': 'UNKNOWN', 'new_account_status': 'CLOSED'}


def build_paraphrases():
    """Local diagnosis/replay; no new model or application execution."""
    proof = ROOT/'docs/probes/customer-paraphrase-20261101'
    historical = read(proof/'actual-rejection.json')['rows'][0]
    audit = historical['rejection_audit']
    assert historical['status'] == 'FAILED' and audit['candidate_answer_present']
    candidate = audit['candidate_answer_redacted']
    index = read(ROOT/'data/delivery-index.json')
    evidence = [row for row in index['chunks'] if row['chunk_id'] in audit['candidate_citation_ids']]
    from app.m2.self_rag import _answer_semantic_issues, _policy_window_facts
    assert len(evidence) == len(audit['candidate_citation_ids'])
    assert _answer_semantic_issues(candidate, evidence, '怎么退货') == []
    before = read(proof/'parser-before.json')
    after = read(proof/'parser-after.json')
    assert before['issues'] and not after['issues']
    assert any(row['amount'] == 30 and row['condition'] == ['存在质量问题'] for row in before['candidate'])
    assert any(row['amount'] == 30 and row['condition'] == ['quality_issue'] for row in after['candidate'])
    assert json.loads(json.dumps(_policy_window_facts(candidate))) == after['candidate']
    current = {}
    for name in ('offline-final.xml', 'offline-routing-final.xml'):
        for case in ET.parse(proof/name).getroot().iter('testcase'):
            current[(case.attrib['classname'], case.attrib['name'])] = case
    assert all(case.find('failure') is None and case.find('error') is None for case in current.values())
    runtime = read(proof/'runtime-verification.json')
    assert runtime[0]['source_files_verified'] == 53 and runtime[0]['sdk_retries'] == 0
    assert runtime[0]['TLS_verified'] and runtime[0]['httpx_selected_proxy'] == '127.0.0.1:7892'
    assert runtime[0]['active_runs'] == runtime[0]['pending'] == 0
    assert runtime[1]['application_Run'] == runtime[1]['Provider'] == 0
    manifest = read(proof/'source-manifest.json')
    assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == value
               for name, value in manifest['files'].items())
    from app.m1.routing import decide_route
    examples = []
    for question in ('查看我的订单', '我有哪些订单', '帮我查一下订单', '我的订单列表', 'show my orders'):
        route = decide_route(question)
        assert route.required_capabilities == ['list_orders'] and route.decision_source == 'RULE'
        examples.append({'question': question, 'capabilities': route.required_capabilities, 'source': 'RULE'})
    order = read(proof/'actual-order-route.json')['rows'][0]
    assert order['route']['decision_source'] == 'LLM' and order['route']['strategy'] == 'DIRECT'
    return {'scope': 'OFFLINE_PARAPHRASE_AND_VALIDATOR_FIX', 'unique_current_tests_passed': len(current),
        'historical_order_route': order, 'historical_rejection_run': historical['run_id'],
        'historical_delivery': 'FAILED_UNCHANGED', 'local_candidate_window_replay': 'SUPPORTED',
        'candidate_completeness': 'NOT_HUMAN_GRADED', 'routing_examples': examples,
        'classifier_list_contract_regression': 'PASS_OFFLINE_SDK_ONLY',
        'new_application_runs': 0, 'Provider': 0, 'Judge': 0, 'Embedding': 0,
        'live_repository_reads': 1, 'test_mode': 'real application and repositories with offline SDK replacement',
        'deployed_route_version': runtime[1]['deployed_route_version'], 'image': 'customer-paraphrase-r1',
        'manual_provider_requests_at_verification': runtime[0]['manual_baseline']['provider_requests'],
        'old_accounts_reopened_or_reset': False}



def build_final(proof=None):
    """Reconcile the final synthetic acceptance; no live queries or model calls."""
    proof=proof or ROOT/'docs/probes/customer-final-20261102'
    receipt=read(proof/'real/segment-1/receipt.json')
    raw={row['run_id']:row for row in read(proof/'archive.json')['runs']}
    account=read(proof/'closed-account.json')
    assert account['account']['status']=='CLOSED'
    limits=BudgetLimits(provider_calls=20,total_tokens=400000,estimated_usd=1,
        max_concurrent_trials=1,max_duration_seconds=120,max_retries=0)
    budget=BudgetLedger.snapshot_from_events(read(proof/'budget-events.json'),limits,limits.to_dict())
    for name in ('provider_requests','tokens','estimated_usd','active_reservations'):
        assert budget[name]==account['snapshot'][name]
    assert budget['settled'] and budget['token_status']=='RECORDED'
    calls=read(proof/'provider-events.json')
    assert len(calls)==budget['provider_requests']
    assert sum(c['prompt_tokens']+c['completion_tokens'] for c in calls)==budget['tokens']
    assert abs(sum(c['local_peak_estimate_usd'] for c in calls)-budget['estimated_usd'])<1e-10
    assert all(c['scope']==account['account']['batch_id'] and c['usage_status']=='RECORDED' for c in calls)
    cleanup=read(proof/'cleanup.json');fixture=read(proof/'fixture-cleanup.json')
    assert not cleanup['remaining_runs'] and not cleanup['remaining_sessions']
    assert not any(cleanup['remaining_checkpoints'].values()) and fixture['exact_fixture_cleanup']
    reviews={r['run_id']:r for r in read(proof/'answer-pre-review.json')['rows']}
    rows=[];attributed=[]
    for scene in receipt['scenarios']:
        delivered=[m['content'] for m in scene['session']['messages'] if m['role']=='assistant']
        for step in scene['steps']:
            run=step['run'];rid=step['run_id'];saved=raw[rid]
            assert saved['status']==run['status']
            answer=(run.get('answer') or {}).get('answer')
            if run['status']=='SUCCEEDED':
                assert answer==saved['result']['answer'] and answer in delivered
                result=saved['result']
                for audit in result.get('tool_call_audit',[]):
                    assert audit['run_id']==rid and audit['result_ref'] and audit['executed']
            else:
                assert not answer and saved['error_code']==run['error']['code']
                audit=saved['error_detail']['answer_rejection_audit']
                assert audit['candidate_answer_present'] and audit['candidate_citation_ids']
                assert all(link['run_id']==rid for link in audit['retrieval_links'])
                result={}
            linked=[c for c in calls if c['session_id']==scene['session_id'] and c['client_request_id']==step['client_request_id']]
            attributed.extend(c['reservation'] for c in linked)
            review=reviews[rid]
            assert review['question']==step['question'] and review['answer_sha256']==hashlib.sha256((answer or '').encode()).hexdigest()
            assert review['reviewer'] is None and review['reviewer_decision'] is None
            rows.append({'question':step['question'],'run_id':rid,'lifecycle':run['status'],
                'delivered_answer':answer,'candidate':None if answer else audit['candidate_answer_redacted'],
                'route_source':(result.get('route') or {}).get('decision_source','PRESERVED_IN_FAILED_ARCHIVE'),
                'provider_requests':len(linked),'citations':[c['chunk_id'] for c in result.get('citations',[])],
                'tool_calls':[a['tool'] for a in result.get('tool_call_audit',[])],
                'AI_task_suggestion':review['suggestion'],'AI_reason':review['reason'],
                'reviewer':None,'reviewed_at':None,'reviewer_decision':None})
    assert set(raw)==set(receipt['run_ids'])=={row['run_id'] for row in rows}
    assert sorted(attributed)==sorted(c['reservation'] for c in calls)
    runtime=read(proof/'runtime-verification.json')
    assert runtime['source_files_verified']==len(read(proof/'source-manifest-final.json')['files'])
    assert runtime['manual_baseline']['provider_requests']==9
    assert runtime['manual_current']['remaining_provider_requests']==91
    current_tests={}
    for name in ('offline-final.xml','account-cleanup.xml','followup-final.xml','routing-report-final.xml','final-report-tests.xml'):
        for test in ET.parse(proof/name).getroot().iter('testcase'):
            current_tests[(test.get('classname'),test.get('name'))]=not any(test.find(x) is not None for x in ('failure','error','skipped'))
    assert all(current_tests.values())
    metrics=read(ROOT/'docs/eval/m6.3-personal-execution-20261101/metrics-and-budget.json')['benchmark_B_same_application_descriptive']
    accepted=read(ROOT/'docs/eval/m6.3-personal-execution-20261101/final-stage-closeout-r1.json')
    assert accepted['stage_status']['M6_3_PERSONAL']=='COMPLETE' and metrics['all_100']['n']==51 and metrics['primary_outcome_60']['n']==33
    return {'schema':'customer-final-delivery-v1','image':'shoprelay-m62-eval-app:customer-final-r2',
        'rows':rows,'budget':budget,'cleanup':cleanup,'fixture_cleanup':fixture,
        'manual_before':read(proof/'manual-continuation.json')['predecessor'],
        'manual_remaining_at_verification':runtime['manual_current'],
        'current_unique_tests':len(current_tests),'historical_benchmark':metrics,
        'real_acceptance_image':'customer-final-r1','final_parser_repair':'OFFLINE_REPLAY_ONLY_NO_RESAMPLING',
        'historical_M6_scores_unchanged':True,'remote_CI':'NOT_RUN','supplier_invoice':'UNKNOWN'}

def main():
    if '--final' in sys.argv:
        result=build_final()
        directory=ROOT/'docs/delivery/customer-final-20261102'
        directory.mkdir(exist_ok=True)
        if '--check' in sys.argv:
            assert read(directory/'closeout.json')==result
            print('FINAL_RECALCULATION_MATCH: HTTP, SQL, budget, cleanup and historical denominators verified')
        else:
            (directory/'closeout.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            table='\n'.join('| '+r['question']+' | '+r['lifecycle']+' | '+r['AI_task_suggestion']+' | '+str(r['provider_requests'])+' |' for r in result['rows'])
            answers='\n\n'.join('### '+r['question']+'\n\n'+(r['delivered_answer'] or '未交付。受控候选：'+r['candidate'])+'\n\nAI建议：'+r['AI_task_suggestion']+'。'+r['AI_reason'] for r in result['rows'])
            report=f'''# 最终个人交付收口（2026-11-02）

当前18065已部署customer-final-r2，54文件实际核验、真实模型模式、六代理7892、TLS及SDK重试0；原PG identity与用户记录保留。本地源码提交号见同目录commit-receipt.json，实际提交对象以该记录为准；不推送，远端CI未运行。

## 最新真实验收

6个新合成Run：5交付、1未交付。AI建议4 PASS / 1 PARTIAL / 1 FAIL，不是人工Task批准。前三步RULE业务读取；政策回答与两次追问共5次Provider、13,020 tokens、未缓存峰值保守估算USD0.0043272，供应商账单UNKNOWN。Judge/Embedding0。精确清理会话、Run、Checkpoint及新7订单/1商品/2用户，原6会话与原订单/用户集合保持，预留0、自动账户CLOSED。

| 问题 | 生命周期 | AI Task建议 | Provider请求 |
|---|---|---|---:|
{table}

真实验收使用customer-final-r1。质量追问的重复“是否在签收后30天内”丢失前文质量条件，错误候选的真实未交付保留。后继r2仅做局部范围继承根修，完整候选经过实际离线SDK/API/持久化回放通过；错误值/单位/起算/新增条件仍被拒绝。没有真实重发失败问题。不要把离线修复当成新的模型质量成绩。

## 用量、手动额度与验证

手动累计上限100次/USD5已获用户明确确认；旧9次/13779tokens/USD0.0050616封存且校验哈希，新余额91次/USD4.9949384（核验时点）。实际Provider仍只向api.deepseek.com，自动验收不借手动额度。页面刷新显示余额，耗尽与未知用量有明确说明，规则读取不耗Provider。

当前受影响完整文件去重310项通过，API完整文件37项通过（1项可选向量测试不在本轮范围），前端14项/typecheck/build通过。失败迭代保留；307不是新案例数。报告重算比较SQL/HTTP完整答案、同Run工具/引用、逐请求账本、cleanup和历史分母；报告负例另列，不凭哈希一致冒充独立验证。

## 历史可引用指标（已接受层）

- B主集严格PASS51/100＝51%，PARTIAL10%，FAIL39%；Wilson描述95%区间41.35%–60.58%。相同应用字节，评分runtime跨两段，完整绑定差异保留。
- 主要Outcome33/60＝55%，PARTIAL10/60、FAIL17/60；描述区间42.49%–66.91%。来源metrics-and-budget.json的primary_outcome_60，不是当前部署新测。
- 同题三轮配对A27/72＝37.50%，B38/72＝52.78%，差15.28个百分点；按24题cluster bootstrap描述区间6.94–23.61个百分点。B胜21/A胜6/同分45。
- 三轮全部PASS：A4/24＝16.67%，B9/24＝37.50%。250试次是覆盖汇总，不能合成同版本总准确率。B是规则/模型/Supervisor混合，不作纯Multi因果结论。
- 当前没有可直接引用的独立、统一七维总分；路由正确率、必需证据Recall、完整原子事实支持率及总体误拒/漏放行仍缺对应独立标签/适当分母，N/A。引用数量不替代Recall/MRR。历史有限Judge对象与维度不混算。

## 交付入口与已知问题

[五分钟演示](../../demo/five-minute-demo.md)、[三条简历与指标](../../m5.4-c-technical-highlights.md)、[面试讲解](../../developer-guide.md)、[部署/回退](../../runtime-and-deployment.md)。历史M6 Task及正式Gate不改，033与5份完整候选其他UNKNOWN保留。不是生产发布或严格独立Gold/Frozen放行。

| 已知问题 | 当前处理 |
|---|---|
| 任意同义句不能穷尽 | 组合意图＋合法模型分类＋完整正反回归；不宣称100%自然语言泛化 |
| 退货澄清未完整说明流程 | 本轮Task PARTIAL保留，不补造已办理 |
| 质量追问误拒 | 真实FAIL保留，r2离线修复通过；未重采样 |
| 最新开发版全量质量未知 | 不沿用历史51%作为当前新测准确率 |
| 远端CI与全新环境复制 | 远端未跑；本地对应核心检查通过，冷环境还需合法基础镜像与秘密部署 |
| 真实账单未知 | 本地保守估算单列，invoice UNKNOWN |

完整本机证据docs/probes/customer-final-20261102；私有账本、凭据、旧用户会话及大体积缓存不进入源码提交。离线入口：python scripts/report_customer_experience.py --final --check。

## 六步实际完整输出与AI预审

{answers}
'''
            (directory/'closeout.md').write_text(report,encoding='utf8')
            print(json.dumps({'runs':len(result['rows']),'provider':result['budget']['provider_requests'],'tests':result['current_unique_tests']}))
        return
    if '--paraphrases' in sys.argv:
        result = build_paraphrases()
        directory = ROOT/'docs/delivery/customer-paraphrase-20261101'
        directory.mkdir(exist_ok=True)
        if '--check' in sys.argv:
            assert read(directory/'closeout.json') == result
            print('PARAPHRASE_RECALCULATION_MATCH: original failure, local facts, bindings and regressions verified')
        else:
            (directory/'closeout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            report = f"""# 同义问法与退货校验修复

已部署http://127.0.0.1:18065/，镜像customer-paraphrase-r1，路由版本{result['deployed_route_version']}。本轮新增真实Run/Provider/Judge/Embedding均0，没有重放旧会话或借用关闭额度。

## 原始证据与根因

1. “查看我的订单”的实际Run {result['historical_order_route']['run_id']} 为LLM/DIRECT、task_not_specified，未走列表。旧规则对集合查询覆盖不足；这是已保存的实际决策，不归因为连接失败。
2. 同时发现分类器契约错误地把list_orders列入必须带订单号的工具。即使模型返回正确列表能力也会被拒；这是代码确认及离线SDK反例验证的潜在缺陷，不冒称它就是首条历史Run的唯一原因。现已把集合查询和单笔目标查询分开，工具白名单、owner和确认护栏未放宽。
3. “怎么退货”Run {result['historical_rejection_run']} 已有本轮两条有效引用，候选与用量已保存。实际30天事实的subject=签收后、relation=WITHIN，条件被解析成“存在质量问题”，来源却是quality_issue，发生误拒。局部存在谓词现归一到相同条件；不存在/没有、无论是否、错误天数/单位/对象/起算/新增会员条件仍拒绝。没有原句、case或答案白名单。

## 修后行为

| 输入表达 | 行为 |
|---|---|
| 查看/查下/列出我的订单、购买记录、show my orders | 认证本人列表，默认5条；无须编造订单号 |
| 订单状态/物流/商品明细、退款进度 | 仍需明确目标或合法会话引用，不混作列表 |
| 选定订单后“怎么退货”“退货怎么申请”“这件商品怎么退” | 复用同会话已交付订单引用，重新核验owner并读取订单、商品及政策；缺签收时间时只追问关键事实 |
| 一般/通用政策问题 | 不擅自绑定旧订单；仍走正常政策问答 |
| 他人订单、超白名单、提交动作 | 继续拒绝越权或进入原确认路径，查询改写不授予权限 |

明确的集合意图由RULE处理，其他模型分类可选择真实list_orders能力；不把规则识别宣称为新LLM泛化能力。

## 验证与实际范围

当前{result['unique_current_tests_passed']}项受影响回归通过，覆盖10个完整文件的应用/Repository/范围/持久化/政策校验；最终路由文件完整81项替换旧预期失败，测试去重不相加。保留首轮2失败和后续1条旧预期失败日志。原“check my order”现在确有列表能力；泛化DIRECT澄清测试改用真正不明确的问题，未删除断言。实际应用SDK替身验证完整已保存候选正常交付、错误31天仍拒绝且不保存为assistant答案；普通提交申请仍走确认契约。

修前/修后断言结构见parser-before.json、parser-after.json。回放只证明相关期限主张获得支持，不将候选完整性或历史未交付翻成Task PASS；日期/标识脱敏不复原。

最终容器53文件、HTTPX代理127.0.0.1:7892、六代理一致、TLS开启、重试0和原PG身份已核对；无活动Run/Pending。对原演示账户只做1次真实PG Repository只读检查，没有新会话或业务写入。前端未改，原会话、原失败和账本保留，可回退customer-orders-r4。

手动模型保护账本核对时已使用{result['manual_provider_requests_at_verification']}/10请求，未擅自重置或续额。列表及本轮已验证的规则读取不消耗Provider；后续需要模型的问答仍受该余额限制。

离线重算沿项目既有PYTHONPATH（.vendor/g2、.vendor、仓库根目录）运行：`python scripts/report_customer_experience.py --paraphrases --check`。这不是新的真实模型质量评测，也不是M6重开；正式Gate和旧评分不变。
"""
            (directory/'closeout.md').write_text(report, encoding='utf8')
            proof = ROOT/'docs/probes/customer-paraphrase-20261101'
            paths = {path for path in proof.rglob('*') if path.is_file()}
            paths.update(directory/name for name in ('closeout.md','closeout.json'))
            paths.update(ROOT/name for name in read(proof/'source-manifest.json')['files'])
            paths.update([ROOT/'scripts/report_customer_experience.py', ROOT/'tests/test_customer_paraphrases.py',
                          ROOT/'tests/test_m2_routing.py'])
            (directory/'SHA256SUMS.md').write_text('| SHA-256 | Path |\n|---|---|\n'+''.join(
                f"| {hashlib.sha256(path.read_bytes()).hexdigest()} | {path.relative_to(ROOT).as_posix()} |\n"
                for path in sorted(paths)), encoding='utf8')
            print(json.dumps({'tests': result['unique_current_tests_passed'], 'Provider': 0, 'deployed': result['image']}))
        return
    if '--orders' in sys.argv:
        result = build_orders()
        directory = ROOT/'docs/delivery/customer-orders-20261101'
        destination = directory/'closeout.json'
        if '--check' in sys.argv:
            assert read(destination) == result
            print('ORDER_EXPERIENCE_RECALCULATION_MATCH: raw Run/audit/SDK/budget/cleanup verified; no network')
        else:
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
            text = '# 本人订单与售后连续体验验收\n\n'
            for row in result['runs']:
                text += (f"## {row['question']}\n\nRun：{row['run_id']}\n\n{row['answer']}\n\n"
                    f"AI预审：{row['ai_pre_review']['task_outcome_ai']}。{row['ai_pre_review']['reason']}\n\n")
            (directory/'answers-and-review.md').write_text(text, encoding='utf8')
            report = f"""# 本人订单列表、事实复用与监控纠错收口

已部署 http://127.0.0.1:18065/ 。真实合成验收4个Run全部交付；其中3项AI任务预审PASS、1项PARTIAL，不是人工评分或M6重评。

## 实际结果

| 问题 | 实际路径 | AI任务预审 |
|---|---|---|
"""
            report += ''.join(f"| {row['question']} | {row['route_source']} / {row['route']}，Provider {row['actual_provider_requests']}次；{', '.join(row['tools']) or '正常政策检索'} | {row['ai_pre_review']['task_outcome_ai']} |\n" for row in result['runs'])
            report += f"""
完整实际输出、具体理由及精确Run见[逐项预审](answers-and-review.md)和closeout.json。最后政策答案遗漏来源中的“整单”要求，保留PARTIAL；不重新采样刷分。本人商品退货问答实际读取订单、明细、目录、政策，因缺签收时间只追问该事实；没有把PAID当未发货，也未按general类别擅自批准资格。

## 实现和边界

- 复用Order Repository、Order Agent、Supervisor及同Run工具审计；新增list_orders和认证GET /api/orders。owner只来自认证，不接受模型指定owner。默认5条、订单号降序keyset分页，不虚构创建时间或总量；按钮只填入，不自动付费发送。
- 序号和明确指代只从同owner/session已成功交付、实际有assistant消息的结构化结果取得。最新列表多条且未选择时不猜；失败候选不参与；每次新查询重新查库授权，不继承资格或业务状态。
- 组合读取复用真实商品及订单事实，保留未知签收时间，只问影响当前资格判断的关键缺失项。未增加category到资格的业务映射；并非所有省略句都可无歧义解析。
- 监控零柱为0、时间桶键唯一、24小时显示时分，提示显示完整区间。总览只受时间窗影响，细筛选仅影响列表和当前页趋势（最多25条）。五个主指标与折叠细项分离；存档模型不作为实际调用证明，未知不补0，Run墙钟不冒称浏览器端到端耗时。
- 表格状态可读、短编号可复制、长错误换行；1440/768/390无整页横向溢出，窄屏表格在自身容器内横滑。保留键盘、抽屉、引用、刷新恢复。

## 验证和用量

后端去重{result['backend_tests_distinct']}项通过；其中新增收口19项与前面回归有重叠，未相加。前端14项、typecheck、最终build通过。旧失败JUnit/日志保留；API断流测试允许本机回环，7892和外网仍禁止。报告负例拒绝交付文本、SDK用量、owner、清理和源码绑定漂移。

实际Provider {result['budget']['provider_requests']}/20请求、{result['budget']['tokens']} tokens，峰值未缓存保守估算USD{result['budget']['estimated_usd']:.7f}；供应商账单UNKNOWN。{result['actual_business_reads']}次真实业务只读（含列表），{result['actual_policy_retrievals']}次政策检索单列；Provider专用预算中tool=0不代表应用没有工具。Judge/Embedding0，SDK retries0，六代理及HTTPX实际127.0.0.1:7892，TLS开启，无独立联网探针。

本轮新账户CLOSED、预留0，剩余19次不自动使用。旧自动CLOSED账户未恢复；原手动账户仍6次/8279tokens，前后完全相同。20次/USD1是本轮执行采用的保护上限，不声称用户逐字给出该数字。

## 真实失败与恢复

Fixture凭据服务自带commit，第一次准备先提交2个新虚构用户，后续事务报InvalidRequestError；对账确认订单/商品/Run/Provider均0，保留部分提交receipt后补齐同一Fixture。

首个列表Run成功后，控制脚本尝试读取尚未注册的独立预算触发外键错误。先注册同一实验，再恢复同一会话、跳过已终态Run。一次旧欢迎页按钮检查又停在本地UI，新增Run/Provider为0；修正不适用检查后继续。没有重置次数、重开账户或重放首个Run，全部失败receipt保留。

## 清理、部署与局限

已先导出4个原始Run/40条事件及用量，再精确清理1个合成会话、Run、checkpoint；7个新虚构订单、1个商品、2个新用户及其凭据均清理。原{result['original_sessions_preserved']}个会话及各自Run/消息计数、原用户及订单集合保持；没有按旧owner清理认证记录。无活动Run/Pending，PG marker ca3b0b3d027448a09cc58caa49139862保持。

最终镜像customer-orders-r4；真实验收r2的全部应用Python字节与最终版一致，后续仅监控前端显示修正。53个最终文件在运行容器内核对。customer-conversation-r6及本轮旧镜像可回退，原卷及历史原档保留。

Taste沿用Achromatic ledger / seed1134632388、Microsoft YaHei、深青数据色。最终检测0 FAIL/3 WARN：折叠元数据低对比仍记限制，12px仅用于次级说明/轴标，状态列表按实际条数收缩，不填空白强行与图表拉齐。三宽截图均已查看；不宣称全面WCAG通过。

唯一离线入口：`python scripts/report_customer_experience.py --orders --check`。两进程从HTTP/SQL/SDK/追加账本、owner和精确清理核对；重算一致不代替独立人工审核。历史M6个人结论、严格正式Gate及旧任务评分保持不变；无受保护资产、生产业务写入或Git写操作。
"""
            (directory/'closeout.md').write_text(report, encoding='utf8')
            proof = ROOT/'docs/probes/customer-orders-20261101'
            sources = {path for path in proof.rglob('*') if path.is_file()}
            sources.update(path for path in directory.iterdir() if path.is_file() and path.name != 'SHA256SUMS.md')
            sources.update(ROOT/name for name in read(proof/'source-manifest-final.json')['files'])
            sources.update(ROOT/name for name in ('scripts/report_customer_experience.py',
                'scripts/verify_customer_orders.py', 'scripts/manage_m6_2_container.py',
                'frontend/tests/conversation-real.cjs', 'frontend/tests/ui-readonly.cjs',
                'frontend/tests/admin-observability.test.ts', 'frontend/tests/execution-view.test.ts',
                'tests/test_customer_order_list.py', 'tests/test_customer_experience_report.py'))
            (directory/'SHA256SUMS.md').write_text('| SHA-256 | Path |\n|---|---|\n'+''.join(
                f"| {hashlib.sha256(path.read_bytes()).hexdigest()} | {path.relative_to(ROOT).as_posix()} |\n"
                for path in sorted(sources)), encoding='utf8')
            print(json.dumps({'runs': len(result['runs']), 'provider': result['budget']['provider_requests'],
                'tokens': result['budget']['tokens'], 'ai_outcomes': result['ai_task_outcomes']}))
        return
    if '--conversation' in sys.argv:
        destination=ROOT/'docs/delivery/customer-conversation-20261101/closeout.json'
        result=build_conversation()
        if '--check' in sys.argv:
            assert read(destination)==result
            print('MATCH: HTTP/SQL/delivery/citations/request ledger/cleanup/original sessions reconciled')
        else:
            destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
            print(json.dumps({'runs':len(result['runs']),'provider_requests':result['provider_requests'],'tokens':result['tokens']}))
        return
    result=build()
    OUT.mkdir(parents=True,exist_ok=True)
    if sys.argv[1:]==['--check']:
        assert result==read(OUT/'closeout.json')
        print(json.dumps({'offline_recalculation':'MATCH','runs':len(result['runs']),
            'provider_requests':result['budget']['provider_requests'],'tokens':result['budget']['tokens']}))
        return
    if sys.argv[1:]:raise SystemExit('supported: no args | --check')
    (OUT/'closeout.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    table='\n'.join('| '+r['run_id'][:8]+' | '+r['question']+' | '+r['status']+' | '+r['route_source']+'/'+r['route_strategy']+' | '+str(r['provider_requests'])+' |' for r in result['runs'])
    text=f'''# 客服真实模型与Taste界面收口（2026-11-01）

真实模型和新界面已部署；本轮完整体验验收为PARTIAL。请刷新 http://127.0.0.1:18065/ 后新建会话。旧会话只读保留，不自动发送给Provider。

## 实际交付

- DeepSeek / api.deepseek.com / deepseek-flash，正常API、LangGraph、PG、SSE和owner链路，无静默替身。旧M6.5验收专用43天索引及镜像保留，交互版使用公开的knowledge/policies.json，未使用Gold/受保护题。
- 最终镜像shoprelay-m62-eval-app:customer-experience-r7，image ID {result['last_deployment'][0]['image_id']}。仅演示服务更新；原M6评分、正式边界、业务卷与关闭授权不改。
- 最后真实问题“商品要退货，怎么退”成功交付，引用现行退货政策两条来源；不再错误套用43天。本人订单读取两次成功，均为RULE，不冒称LLM自主读取。
- 共11工程Run：4生命周期SUCCEEDED、7FAILED。其中1个SUCCEEDED目录回答不满足政策问题。所有失败保留；这些不是独立同配置质量成绩。
- 真实模型多轮解释尚未完整成功；缺地址的两次回答也未交付。受控候选显示已有缺口说明，但夹带附加流程/期限；未为通过而关闭校验器。最终成功回复仍一次问了多项信息，不能宣称语气优化已全面验证。

## 代码与验证

- app/m1/chat.py：自然中文客服提示与受约束的上下文检索词；app/m1/routing.py、app/orchestration/graph.py、app/m2/policy_execution.py保留真实最新问题，只将模型解析的同会话检索词用于检索，不授予工具权限。
- app/m2/self_rag.py：7×24不再读成24小时；elapsed小时/天/周严格换算，月份不猜天数；局部超期断言、显式条件和标题/分条作用域匹配。完整真实候选及错误单位、对象、起算、条件和越界引用保留回归。
- app/orchestration/adapters.py及交互profile：真实路由失败明确失败，不用泛化问候伪装模型成功。旧调用默认契约保留。
- app/m1/delivery_provider.py复用BudgetLedger/ResultStore，在实际SDK发送前预留，重启不清零；用量未知阻断后续。密钥只在专用卷及服务进程，前端、镜像、报告无密钥。实际SDK代理127.0.0.1:7892，TLS证书验证开启，SDK retries=0。
- 完整7个受影响后端文件336 passed；清理边界增量文件21 passed，与前者重叠，不相加。前端typecheck、6 tests、production build通过。中间失败日志包括绑定期间文件变动导致的安全拒绝均保留。
- 真实Edge验证登录、新会话、SSE、逐消息来源/条款展开、执行详情、浏览器刷新、桌面/768/390及移动菜单，page errors=0。服务重启后真实模式和独立手动账本保留；未另造模型Run做重启试次。

## Taste设计

Achromatic ledger，最终seed1134632388，保留中文系统无衬线字库，不加载第三方字体。选择理由：客服对话和真实来源是主内容，控制栏保持安静；230px侧栏、对齐导航、正文16px/1.9、收起详情、逐回复“来源与执行”、输入法组合键保护。

检查从6 FAIL降至0 FAIL；1个tiny-text WARN保留：12px仅用于时间、会话次级日期、模式提示等，正文15–16px。边框/textarea清除内置阴影的lint提示不是卡片阴影。截图逐宽度已查看。七问自评：目的清楚8/10；主次8；实际内容8；密度8；产品签名8；状态/键盘8；移动8。文字仍偏长，是后续质量问题，不用设计分数覆盖。

截图：docs/probes/customer-experience-r1-20261101/before/1440.png；real-budget-final/1440.png、768.png、390.png、references.png、execution.png。Taste检查为实际交付后认证DOM的去脚本静态副本，另有真实浏览器交互记录，不将静态检查当交互证明。

## 用量与清理

自动Provider10/10请求，{result['budget']['tokens']} tokens，本地未缓存峰值保守估算USD{result['budget']['estimated_usd']:.7f}，供应商账单UNKNOWN。含8回答/2分类；Judge/Embedding0；8次政策检索、3次业务只读。API内缓存感知估算与本账本保守估算不混成账单。价格依据https://api-docs.deepseek.com/quick_start/pricing/（2026-11-01核对，Flash每百万输入峰值USD0.30、输出USD1.20）。

自动账本CLOSED，20条预留/结算事件、reservation0，独立从事件重算并逐请求关联Run一致。早先进度“5次”漏计路由，最终以账本10次为准，未越限。用户之后主动消息独立记账，初始服务端保护上限仍为10请求/USD1，自动验收脚本拒绝借用；本轮没有消费该账本。

8个已绑定合成会话、11个Run及其checkpoint已先归档再精确清理，残留0。原用户会话71b78efe-6eb9-4659-b92e-e4c19a489f4f仍为1会话/2Run/4消息。密码、其他auth和会话不按owner批删；1个缺准确receipt的早期空UI会话保守保留。无业务写操作或Pending Action。

## 自动验收完整记录

| Run前缀 | 新合成输入 | 生命周期 | 路由来源/策略 | Provider |
|---|---|---|---|---:|
{table}

全部原始输出、拒绝候选、引用、事件、各部署源码清单、测试及账本见docs/probes/customer-experience-r1-20261101/，closeout.json保留精确Run绑定。无内部推理或私人旧会话正文。首次凭据部署自动审批按“离线授权”拒绝；直接核验用户消息01a196f5-b7cc-72e0-b707-8b7053e839a1后重新通过正常审批，没有绕过。

## 保留限制与下一步

1. 当前可以手动新建会话使用真实模型、查看实际来源和只读订单。旧会话不会回放。
2. 多轮通俗解释、未知地址的有效交付仍需新额度下的最小验收；不能借手动台账或继续第11次自动请求。先复用本轮候选与离线测试，不重跑M6。
3. 提问简洁度仍需改善；不承诺此版已消除所有政策句式误拒。
4. 历史M6个人结论与严格正式Gate均不改变，没有新的Gold、人类Task评分或Frozen成绩。

离线重算：python scripts/report_customer_experience.py --check。部署命令及原M6.5回退见docs/runtime-and-deployment.md的真实模型客服后继部署章节。
'''
    (OUT/'closeout.md').write_text(text,encoding='utf8')
    sources=[OUT/'closeout.json',OUT/'closeout.md',Path(__file__),PROOF/'execution-approval.json',
        PROOF/'source-manifest-final.json',PROOF/'automated-ledger-closed.json',PROOF/'provider-events.jsonl',
        PROOF/'exact-cleanup-archive.json',PROOF/'exact-cleanup-proof.json',PROOF/'final-deployment.json',
        PROOF/'offline-final-verified.xml',PROOF/'cleanup-and-delivery-final.xml',
        PROOF/'report-recalculation-tests.xml',ROOT/'tests/test_customer_experience_report.py',
        ROOT/'tests/test_customer_experience.py',ROOT/'README.md',ROOT/'docs/runtime-and-deployment.md',
        ROOT/'deploy/m62-eval/delivery-experience-r1.yaml']
    (OUT/'SHA256SUMS.md').write_text('| SHA-256 | Path |\n|---|---|\n'+''.join(
        '| '+hashlib.sha256(p.read_bytes()).hexdigest()+' | '+p.relative_to(ROOT).as_posix()+' |\n' for p in sources),encoding='utf8')
    print(json.dumps({'report':str(OUT/'closeout.md'),'runs':len(result['runs']),'provider_requests':10}))

if __name__=='__main__':main()
