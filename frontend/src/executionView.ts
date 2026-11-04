import type { EvidenceItem, RunEvent, RunExecution } from './api.ts'

export function evidenceGroups(citations: EvidenceItem[]) {
  const groups = new Map<string, { title: string; version: string; clauses: EvidenceItem[] }>()
  for (const item of citations) {
    const title = item.title || item.source || item.chunk_id
    const version = item.version || item.knowledge_version || ''
    const key = JSON.stringify([title, version])
    const group = groups.get(key)
    if (group) group.clauses.push(item)
    else groups.set(key, { title, version, clauses: [item] })
  }
  return [...groups.values()]
}

export function executionPhases(events: RunEvent[], execution: RunExecution) {
  const rows: {key: string; title: string; detail: string; at: string; duration: number | null}[] = []
  const route = events.find(e => e.type === 'route_decided')
  if (route) rows.push({key: 'route', title:'理解请求', detail:[execution.strategy, execution.route_source].filter(Boolean).join(' · '), at:route.timestamp, duration:execution.route_latency_ms ?? null})
  // Only recorded calls become phases. No invented reasoning or elapsed duration.
  execution.tool_calls.forEach((call, index) => rows.push({key:`tool:${index}`, title:call.capability === 'policy_search' || call.tool_id?.includes('retriev') ? '查阅政策' : '核对业务记录', detail:[call.tool_id || call.capability, call.status].filter(Boolean).join(' · '), at:'', duration:call.duration_ms ?? null}))
  if (!execution.tool_calls.length) {
    for (const reference of execution.tool_references) rows.push({key:`result:${reference.result_ref}`,title:'读取已保存的业务结果',detail:reference.tool || '业务来源',at:'',duration:null})
    const retrieval = events.find(e => e.type === 'retrieval_hop')
    const count = execution.retrieval.retrieval_count
    if (retrieval || (typeof count === 'number' && count > 0)) rows.push({key:'retrieval',title:'查阅政策',detail:typeof count === 'number' ? `已记录 ${count} 次检索 · ${execution.citations.length} 条答案引用` : '检索事件已保存',at:retrieval?.timestamp || '',duration:null})
  }
  const answer = events.find(e => e.type === 'answer')
  if (answer) rows.push({key:'answer',title:'回复已保存',detail:'任务是否解决请结合回复与来源判断',at:answer.timestamp,duration:null})
  const error = events.find(e => e.type === 'error')
  if (error || execution.error_code) rows.push({key:'error',title:'回复未正常完成',detail:execution.error_code || String(error?.payload.code || '查看技术记录'),at:error?.timestamp || '',duration:null})
  return rows
}

export function durationLabel(ms: number | null) {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return '耗时未记录'
  return ms < 1000 ? `${Math.round(ms)} 毫秒` : `${(ms / 1000).toFixed(1)} 秒`
}

export function retrievalLabel(execution: RunExecution) {
  const strategy = execution.retrieval.strategy_executed ?? execution.policy_retrieval.strategy_executed
  // MIXED is a persisted aggregate marker, not evidence that multiple strategies ran.
  return typeof strategy === 'string' && strategy.trim() ? strategy : '未记录'
}

export function orderSnapshot(value: unknown) {
  if (!value || typeof value !== 'object' || !('get_order' in value)) return null
  const row = value.get_order
  if (!row || typeof row !== 'object' || !('order_id' in row) || typeof row.order_id !== 'string' || !('status' in row) || typeof row.status !== 'string') return null
  const states: Record<string,string> = {PAID:'已付款',CREATED:'已创建',CANCELLED:'已取消',DELIVERED:'已送达',SHIPPED:'已发货'}
  const cents = 'paid_cents' in row && typeof row.paid_cents === 'number' && Number.isSafeInteger(row.paid_cents) && row.paid_cents >= 0 ? row.paid_cents : null
  return {id:row.order_id,status:states[row.status] || row.status,rawStatus:row.status,cents,amount:cents === null ? null : `¥${(cents / 100).toFixed(2)}`}
}

export function orderListSnapshot(value: unknown) {
  if (!value || typeof value !== 'object' || !('list_orders' in value)) return null
  const list = value.list_orders
  if (!list || typeof list !== 'object' || !('orders' in list) || !Array.isArray(list.orders)) return null
  const orders = list.orders.map(row => orderSnapshot({get_order: row}))
  if (orders.some(row => row === null)) return null
  return {orders: orders.filter(row => row !== null), hasMore: 'has_more' in list && list.has_more === true,
    before: 'next_before' in list && typeof list.next_before === 'string' ? list.next_before : null}
}
