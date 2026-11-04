import { test } from 'node:test'
import assert from 'node:assert/strict'
import { evidenceGroups, executionPhases, durationLabel, orderSnapshot, orderListSnapshot, retrievalLabel } from '../src/executionView.ts'
import type { RunExecution, RunEvent } from '../src/api.ts'

const empty: RunExecution = {run_id:'r',session_id:'s',status:'SUCCEEDED',retrieval:{},policy_retrieval:{},citations:[],retrieved_evidence:[],spans:[],trace_events:[],handoffs:[],tool_calls:[],tool_references:[]}
test('same document preserves separate clauses and versions', () => {
  const groups=evidenceGroups([{chunk_id:'a',title:'退货',version:'1'}, {chunk_id:'b',title:'退货',version:'1'}, {chunk_id:'c',title:'退货',version:'2'}])
  assert.equal(groups.length,2)
  assert.deepEqual(groups[0]?.clauses.map(c=>c.chunk_id),['a','b'])
})
test('no events never invents phases, success never claims task solved', () => {
  assert.deepEqual(executionPhases([],empty),[])
  const event: RunEvent={event_id:'e',run_id:'r',session_id:'s',trace_id:'t',seq:1,timestamp:'2026-11-01T01:00:00Z',type:'answer',payload:{}}
  const phases=executionPhases([event],empty)
  assert.equal(phases[0]?.title,'回复已保存')
  assert.match(phases[0]?.detail || '',/结合回复与来源/)
})
test('known zero duration differs from missing; failed calls remain visible', () => {
  assert.equal(durationLabel(0),'0 毫秒')
  assert.equal(durationLabel(null),'耗时未记录')
  assert.equal(durationLabel(-1),'耗时未记录')
  const phases=executionPhases([],{...empty,error_code:'DB_ERROR',tool_calls:[{tool_id:'get_order',status:'FAILED',duration_ms:0}]})
  assert.match(phases[0]?.detail || '',/FAILED/)
  assert.equal(phases[1]?.key,'error')
})
test('order presentation consumes only structured order values, unknown money never zero', () => {
  assert.equal(orderSnapshot('PAID 2500'),null)
  assert.equal(orderSnapshot({get_order:{order_id:'o',status:'PAID',paid_cents:null}})?.amount,null)
  const order=orderSnapshot({get_order:{order_id:'o',status:'PAID',paid_cents:2500}})
  assert.equal(order?.amount,'¥25.00'); assert.equal(order?.rawStatus,'PAID'); assert.equal(order?.status,'已付款')
  assert.equal(orderSnapshot({get_order:{order_id:'o',status:'PAID',paid_cents:-1}})?.amount,null)
})
test('missing retrieval never becomes MIXED; zero citations can coexist with recorded retrieval', () => {
  assert.equal(retrievalLabel(empty),'未记录')
  const recorded={...empty,retrieval:{strategy_executed:'MIXED',retrieval_count:1,degraded:true}}
  assert.equal(retrievalLabel(recorded),'MIXED')
  assert.match(executionPhases([],recorded)[0]?.detail || '',/1 次检索 · 0 条答案引用/)
})
test('business references remain visible without inventing tool status or timing', () => {
  const phases=executionPhases([],{...empty,tool_references:[{tool:'get_order',result_ref:'tool_result:bound'}]})
  assert.equal(phases[0]?.title,'读取已保存的业务结果')
  assert.equal(phases[0]?.duration,null)
  assert.equal(phases[0]?.detail,'get_order')
})

test('order options require real structured rows; unknown money stays unknown',()=>{
 assert.equal(orderListSnapshot(undefined),null)
 assert.equal(orderListSnapshot({list_orders:{orders:[{order_id:'x'}]}}),null)
 assert.deepEqual(orderListSnapshot({list_orders:{orders:[],has_more:false}}),{orders:[],hasMore:false,before:null})
 const list=orderListSnapshot({list_orders:{orders:[{order_id:'ORD-A',status:'PAID',paid_cents:null}],has_more:true,next_before:'ORD-A'}})
 assert.equal(list?.orders[0]?.amount,null)
 assert.equal(list?.before,'ORD-A')
})
