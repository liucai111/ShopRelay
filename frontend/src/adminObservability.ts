export interface AdminOverview {
  window: { from: string; to: string }
  run_counts: { total: number; by_status: Record<string, number>; terminal_denominator: number; success_rate: number | null; failure_rate: number | null; unknown_result_count: number }
  latency_ms: { sample_count: number; p50: number | null; p95: number | null; source?: string }
  run_wall_duration_ms: { sample_count: number; p50: number | null; p95: number | null; source?: string }
  agent_execution: { actual_agent_started_count: number; by_agent: Record<string, number>; supervisor_span_count: number; handoff_count: number; duration_sample_count: number; duration_p50_ms: number | null; duration_p95_ms: number | null; agent_calls_per_run_mean: number | null; agent_calls_per_run_median: number | null }
  tools: { started_count: number; failed_count: number; by_tool: Record<string, number>; failure_rate_denominator: number; duration_sample_count: number; duration_p50_ms: number | null; duration_p95_ms: number | null; duration_source: string | null }
  model: { recorded_node_request_count: number; provider_usage_run_count: number; usage_coverage_denominator: number; prompt_tokens: number; completion_tokens: number; estimated_cost_usd: number | null; estimated_cost_sample_count: number; cost_semantics: string; stub_run_count: number; unknown_or_unmetered_run_count: number; by_model_run_usage: Record<string, { run_count: number; provider_usage_run_count: number; prompt_tokens: number; completion_tokens: number; estimated_cost_usd: number | null; estimated_cost_sample_count: number }>; by_node_metric_records: Record<string, { request_count: number; prompt_tokens: number; completion_tokens: number; estimated_cost_usd: number; cost_kind: string }> }
  retrieval: { run_strategy_counts: Record<string, number>; requested_strategy_counts: Record<string, number>; executed_strategy_counts: Record<string, number>; canonical_requested_strategy_counts: Record<string, number>; canonical_executed_strategy_counts: Record<string, number>; canonical_strategy_aliases: Record<string, string>; canonical_counts_conserve_raw_counts: { requested: boolean; executed: boolean }; degraded_run_count: number; degraded_reason_counts: Record<string, number>; bm25_count: number; hybrid_rerank_count: number; citation_count: number; citation_count_is_not_answer_accuracy: true }
  events: { persisted_sse_event_count: number; event_type_counts: Record<string, number>; dedupe_basis: string }
  recovery_and_idempotency: { runs_with_graph_checkpoint: number; idempotent_replay_count: number | null; retry_count: number | null; not_recorded: string[] }
  graph_executions: { count: number; by_status: Record<string, number> }
  controlled_action_audit_events: Record<string, number>
  scope_note: string
  sample_limit: number
}

export interface AdminRunRow {
  run_id: string; status: string; strategy: string | null; orchestrator: 'legacy' | 'langgraph' | 'unknown'; orchestrator_evidence: string; model_id: string | null
  latency_ms: number | null; wall_duration_ms: number | null; latency_semantics: string; error_code: string | null; created_at: string; finished_at: string | null
}
export interface AdminRunList { items: AdminRunRow[]; limit: number; offset: number; has_more: boolean }
export interface AdminRunDetail {
  run: { run_id: string; status: string; strategy: string | null; model_id: string | null; latency_ms: number | null; wall_duration_ms: number | null; latency_semantics: string; error_code: string | null; created_at: string; started_at: string | null; finished_at: string | null; usage: Record<string, unknown> }
  orchestrator: 'legacy' | 'langgraph' | 'unknown'; orchestrator_evidence: string
  graph_execution: { orchestrator: string; status: string; graph_version: string; error_code: string | null } | null
  retrieval_hops: { items: Array<{ run_id: string; hop: number | null; aspect_id: string | null; requested_strategy: string | null; requested_strategy_truncated: boolean; canonical_requested_strategy: string; executed_strategy: string | null; executed_strategy_truncated: boolean; canonical_executed_strategy: string; degraded: boolean; degraded_reason: string | null; knowledge_version: string | null; evidence_count: number | null; evidence_ids: string[]; evidence_ids_truncated: boolean; evidence_id_values_truncated: boolean; retrieval_latency_ms: number | null; latency_source_field: string | null; source: string; missing_fields: string[] }>; has_more: boolean; source: string; not_inferred_from_aggregates: true }
  events: { items: Array<{ event_id: string; seq: number; timestamp: string; type: string; data: Record<string, unknown> }>; has_more: boolean }
  spans: { items: Array<{ trace_id?: string; span_id?: string; parent_span_id?: string | null; run_id?: string; node_name?: string; strategy?: string | null; start_time?: string; end_time?: string; duration_ms?: number; status?: string; error_code?: string | null; graph_version?: string }>; has_more: boolean }
  trace_metadata: Array<{ event_type: string; status: string; created_at: string; data: Record<string, unknown> }>
  audit_metadata: Array<{ action_id: string; event_type: string; from_status: string | null; to_status: string | null; occurred_at: string; error_code: string | null; trace_id: string | null; has_transaction_reference: boolean }>
}
export interface AdminApiError extends Error { status: number; code: string }
export interface RunFilters { status: string; strategy: string; agent: string; toolId: string; orchestrator: string }

export function buildRunQuery(from: string, to: string, page: number, pageSize: number, filters: RunFilters): string {
  const params = new URLSearchParams({ from, to, limit: String(pageSize), offset: String((page - 1) * pageSize) })
  if (filters.status) params.set('status', filters.status)
  if (filters.strategy) params.set('strategy', filters.strategy)
  if (filters.agent) params.set('agent', filters.agent)
  if (filters.toolId.trim()) params.set('tool_id', filters.toolId.trim())
  if (filters.orchestrator) params.set('orchestrator', filters.orchestrator)
  return params.toString()
}

export function formatMetric(value: number | null | undefined, suffix = ''): string {
  return value == null || !Number.isFinite(value) ? '暂无数据' : `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}${suffix}`
}
export function formatRunDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return '暂无数据'
  return value < 1000 ? `${Math.round(value)} 毫秒` : `${(value / 1000).toFixed(1)} 秒`
}
export function retrievalStrategyLabel(value: string): string {
  const labels: Record<string, string> = { BM25: 'BM25', VECTOR: 'Vector', RRF: 'RRF（融合）', HYBRID_RERANK: 'HYBRID_RERANK（融合+重排）', HYBRID_RERANK_DEGRADED: 'HYBRID_RERANK（已降级）', MIXED: '混合执行', OTHER: '其他/未知' }
  return labels[value] || '其他/未知'
}
export function formatPercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '暂无数据' : `${(value * 100).toFixed(1)}%`
}
export function formatMoney(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '暂无数据' : `$${value.toFixed(6)}`
}
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '—' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(date)
}

const statusColors: Record<string, string> = { SUCCEEDED: '#21a67a', FAILED: '#dc5b55', MODEL_UNAVAILABLE: '#e3973d', UNKNOWN_RESULT: '#825fc7', RUNNING: '#3975c6', CREATED: '#94a0b3' }
export function statusColor(status: string): string { return statusColors[status] || '#8590a3' }

export function pageTrend(rows: AdminRunRow[], from: string, to: string, buckets = 7): Array<{ key: string; start: string; end: string; label: string; count: number; succeeded: number; failed: number }> {
  if (!rows.length || buckets < 1) return []
  const first = Date.parse(from)
  const end = Date.parse(to)
  if (!Number.isFinite(first) || !Number.isFinite(end) || end <= first) return []
  const span = (end - first) / buckets
  return Array.from({ length: buckets }, (_, index) => {
    const bucketStart = first + index * span
    const day = new Date(bucketStart)
    const key = end - first <= 24 * 3600_000
      ? day.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
      : day.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
    const inBucket = rows.filter(row => {
      const time = new Date(row.created_at).valueOf()
      return time >= bucketStart && (index === buckets - 1 ? time <= end : time < first + (index + 1) * span)
    })
    return { key: new Date(bucketStart).toISOString(), start: new Date(bucketStart).toISOString(), end: new Date(first + (index + 1) * span).toISOString(), label: key, count: inBucket.length, succeeded: inBucket.filter(row => row.status === 'SUCCEEDED').length,
      failed: inBucket.filter(row => ['FAILED', 'MODEL_UNAVAILABLE', 'UNKNOWN_RESULT'].includes(row.status)).length }
  })
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { credentials: 'include', headers: { Accept: 'application/json' }, signal })
  const body = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) {
    const detail = body.detail && typeof body.detail === 'object' ? body.detail as Record<string, unknown> : body
    const error = new Error(String(detail.message || `请求失败 (${response.status})`)) as AdminApiError
    error.status = response.status; error.code = String(detail.code || 'HTTP_ERROR')
    throw error
  }
  return body as T
}
export async function fetchOverview(from: string, to: string, signal?: AbortSignal): Promise<AdminOverview> {
  const result = await get<{ overview: AdminOverview }>(`/api/admin/observability/overview?${new URLSearchParams({ from, to })}`, signal)
  return result.overview
}
export async function fetchRuns(query: string, signal?: AbortSignal): Promise<AdminRunList> {
  const result = await get<{ runs: AdminRunList }>(`/api/admin/observability/runs?${query}`, signal)
  return result.runs
}
export async function fetchRunDetail(runId: string, signal?: AbortSignal): Promise<AdminRunDetail> {
  const result = await get<{ detail: AdminRunDetail }>(`/api/admin/observability/runs/${encodeURIComponent(runId)}?event_limit=200&span_limit=300`, signal)
  return result.detail
}

export function trendBarHeight(value: number, maximum: number): string {
  return `${maximum > 0 ? Math.max(0, value) / maximum * 100 : 0}%`
}
