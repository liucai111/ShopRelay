export interface ApiError extends Error { status: number; code: string; traceId?: string; retryable: boolean }
export interface User { user_id: string; display_name: string }
export interface SessionRow { session_id: string; title: string | null; created_at: string; updated_at: string | null }
export interface MessageRow { message_id: string; role: 'user' | 'assistant'; content: string; status: string; created_at: string }
export interface RunRow {
  run_id: string; status: string; created_at: string; updated_at?: string | null;
  answer?: AnswerPayload | null; error?: { code: string; details?: unknown } | null; events_cursor?: number;
}
export interface AnswerPayload {
  answer: string; citations: string[]; answer_kind?: string | null; strategy?: string;
  sub_strategy?: string; route?: string | null; retrieval?: unknown; business_results?: unknown;
}
export interface SessionDetail extends SessionRow { messages: MessageRow[]; runs: RunRow[] }
export interface RunDetail extends RunRow {
  session_id: string; answer?: AnswerPayload | null; error?: { code: string; details?: unknown } | null;
  events_cursor: number; latency_ms?: number | null; started_at?: string | null; finished_at?: string | null;
}
export interface RunEvent { event_id: string; run_id: string; session_id: string; trace_id: string; seq: number; timestamp: string; type: string; payload: Record<string, unknown> }
export interface RunSpan {
  trace_id?: string; span_id?: string; parent_span_id?: string | null; node_name?: string; strategy?: string | null;
  start_time?: string; end_time?: string; duration_ms?: number; status?: string; error_code?: string | null; graph_version?: string;
}
export interface EvidenceItem {
  chunk_id: string; title?: string | null; content?: string | null; source?: string | null; version?: string | null;
  effective_from?: string | null; effective_to?: string | null; knowledge_version?: string | null;
  index_fingerprint?: string | null; rank?: number | null; score?: number | null;
  citation_status?: 'CITED' | 'RETRIEVED_NOT_CITED'; validity_status?: 'RUNTIME_CITATION_CHECKED' | 'SAVED_RUN_REFERENCE';
}
export interface RunExecution {
  run_id: string; session_id: string; status: string; strategy?: string | null; sub_strategy?: string | null;
  route_source?: string | null; route_latency_ms?: number | null; skill_id?: string | null; skill_version?: string | null;
  retrieval: Record<string, unknown>;
  policy_retrieval: Record<string, unknown>; citations: EvidenceItem[]; retrieved_evidence: EvidenceItem[];
  spans: RunSpan[]; trace_events: Array<{event_type: string; status: string; created_at: string; data: Record<string, unknown>}>;
  handoffs: Array<{handoff_id?: string; source_agent?: string; target_agent?: string;
    required_capabilities?: string[]; context_refs?: string[]; evidence_refs?: string[]; status?: string; error_code?: string | null}>;
  tool_calls: Array<{tool_id?: string; capability?: string; status?: string; executed?: boolean; duration_ms?: number;
    error_code?: string | null; retrieval_mode?: string; knowledge_version?: string; index_fingerprint?: string;
    evidence_ids?: string[]; retrieval_count?: number; degraded?: boolean}>;
  tool_references: Array<{tool?: string; source?: string; result_ref: string}>;
  error_code?: string | null; finished_at?: string | null;
}
export interface MemoryStatus {
  session_id: string; status: 'CLEARED' | 'PERSISTED' | 'NOT_CREATED'; summary_version: number | null;
  summary_persisted: boolean; summary_content_exposed: false; summary_created_at: string | null;
  source_run_id: string | null; source_message_count: number; source_through_sequence: number;
  estimated_tokens_before: number | null; estimated_tokens_after: number | null; compression_ratio: number | null;
  load_ratio: number | null; effective_context_tokens: number | null;
  level: string; thresholds: {soft: number; hard: number; auto_compact: number}; recent_message_count: number;
  last_run_id: string | null; last_run_status: string | null; last_compaction_persisted: boolean | null;
  last_compaction_version: number | null; token_estimates_are_heuristic: true;
}
export interface ChatStart { run_id: string | null; status: string | null }
export interface PendingActionProposal {
  action_id: string; session_id: string; run_id: string; tool_id: string; target_resource_id: string;
  status: 'CREATED' | 'PENDING_CONFIRMATION' | 'CONFIRMED' | 'EXECUTING' | 'SUCCEEDED' | 'FAILED' | 'UNKNOWN_RESULT' | 'REJECTED' | 'CANCELLED' | 'EXPIRED';
  risk_level: string; action_version: number; created_at: string; expires_at: string; confirmed_at: string | null;
  finished_at: string | null; expired: boolean; safe_error_code: string | null; parameters: Record<string, unknown>;
  verified_snapshot: Record<string, unknown>; user_claim_markers: Record<string, string>;
  policy_references: Array<{ chunk_id?: string; policy_id?: string; clause_id?: string; version?: string }>;
  policy_version: string | null; policy_evaluation_at: string | null;
  result_summary: Record<string, string | number | boolean | null> | null;
  execution_available: boolean; execution_scope: 'ISOLATED_TEST_DATABASE' | 'DISABLED';
  confirmation_hash: string | null;
}

let csrfToken: string | null = null

function apiError(status: number, body: Record<string, unknown>): ApiError {
  const error = new Error(String(body.message || `请求失败 (${status})`)) as ApiError
  error.status = status
  error.code = String(body.code || 'HTTP_ERROR')
  error.traceId = typeof body.trace_id === 'string' ? body.trace_id : undefined
  error.retryable = Boolean(body.retryable)
  return error
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, credentials: 'include', headers: { Accept: 'application/json', ...init.headers } })
  const body = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) throw apiError(response.status, body)
  return body as T
}

function writeHeaders(): Record<string, string> {
  if (!csrfToken) throw new Error('安全令牌尚未就绪，请刷新后重试')
  return { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }
}

export async function refreshCsrf(): Promise<void> {
  const result = await request<{ csrf_token: string }>('/api/demo/csrf')
  csrfToken = result.csrf_token
}

export async function currentUser(): Promise<{ display_name: string }> {
  return (await request<{ user: { display_name: string } }>('/api/demo/me')).user
}

export async function login(username: string, password: string): Promise<User> {
  await refreshCsrf()
  const result = await request<{ user: User }>('/api/demo/login', {
    method: 'POST', headers: writeHeaders(), body: JSON.stringify({ username, password }),
  })
  return result.user
}

export async function logout(): Promise<void> {
  await refreshCsrf()
  await request('/api/demo/logout', { method: 'POST', headers: writeHeaders(), body: '{}' })
  csrfToken = null
}

export async function listSessions(): Promise<SessionRow[]> {
  return (await request<{ sessions: SessionRow[] }>('/api/sessions')).sessions
}

export async function createSession(title: string | null = null): Promise<SessionRow> {
  await ensureCsrf()
  return (await request<{ session: SessionRow }>('/api/sessions', { method: 'POST', headers: writeHeaders(), body: JSON.stringify({ title }) })).session
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  return (await request<{ session: SessionDetail }>(`/api/sessions/${encodeURIComponent(sessionId)}`)).session
}

export async function getRun(runId: string): Promise<RunDetail> {
  return (await request<{ run: RunDetail }>(`/api/runs/${encodeURIComponent(runId)}`)).run
}

export async function getRunExecution(runId: string): Promise<RunExecution> {
  return (await request<{ execution: RunExecution }>(`/api/runs/${encodeURIComponent(runId)}/execution`)).execution
}

export async function getMemoryStatus(sessionId: string): Promise<MemoryStatus> {
  return (await request<{ memory: MemoryStatus }>(`/api/sessions/${encodeURIComponent(sessionId)}/memory`)).memory
}

export async function getSessionActions(sessionId: string): Promise<PendingActionProposal[]> {
  return (await request<{ actions: PendingActionProposal[] }>(`/api/sessions/${encodeURIComponent(sessionId)}/actions`)).actions
}

export async function confirmPendingAction(actionId: string, confirmationHash: string): Promise<string> {
  await ensureCsrf()
  const result = await request<{ answer: string }>(`/api/actions/${encodeURIComponent(actionId)}/confirm`, {
    method: 'POST', headers: writeHeaders(),
    body: JSON.stringify({ confirm: true, arguments_hash: confirmationHash }),
  })
  return result.answer
}

export async function closePendingAction(actionId: string, disposition: 'reject' | 'cancel'): Promise<void> {
  await ensureCsrf()
  await request(`/api/actions/${encodeURIComponent(actionId)}/${disposition}`, {
    method: 'POST', headers: writeHeaders(), body: JSON.stringify({ acknowledge: true }),
  })
}

export async function clearSessionMemory(sessionId: string): Promise<void> {
  await ensureCsrf()
  await request(`/api/sessions/${encodeURIComponent(sessionId)}/memory`, {
    method: 'DELETE', headers: writeHeaders(), body: '{}',
  })
}

export async function getRunEvents(runId: string, afterSeq: number): Promise<{ status: string; events: RunEvent[]; events_cursor: number }> {
  return request(`/api/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`)
}

export async function createChatStream(sessionId: string, message: string, requestId: string, signal?: AbortSignal): Promise<{ response: Response; start: ChatStart }> {
  await ensureCsrf()
  const response = await fetch('/api/chat/stream', {
    method: 'POST', credentials: 'include', signal,
    headers: { ...writeHeaders(), Accept: 'text/event-stream' },
    body: JSON.stringify({ session_id: sessionId, message, client_request_id: requestId }),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as Record<string, unknown>
    throw apiError(response.status, body)
  }
  return { response, start: { run_id: response.headers.get('X-Run-Id'), status: response.headers.get('X-Run-Status') } }
}

async function ensureCsrf(): Promise<void> { if (!csrfToken) await refreshCsrf() }

export async function consumeSse(response: Response, onEvent: (event: RunEvent) => void): Promise<void> {
  if (!response.body) throw new Error('浏览器未提供SSE响应流')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const chunks = buffer.split(/\r?\n\r?\n/)
      buffer = chunks.pop() || ''
      for (const chunk of chunks) {
        const lines = chunk.split(/\r?\n/)
        const dataLine = lines.find(line => line.startsWith('data:'))
        if (!dataLine) continue
        try { onEvent(JSON.parse(dataLine.slice(5).trim()) as RunEvent) } catch { /* ignore malformed non-contract event */ }
      }
      if (done) break
    }
  } finally { reader.releaseLock() }
}

export function isAuthError(error: unknown): boolean { return (error as Partial<ApiError>)?.status === 401 }
