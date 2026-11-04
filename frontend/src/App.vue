<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import {
  clearSessionMemory, consumeSse, createChatStream, createSession, currentUser, getMemoryStatus, getRun,
  getRunEvents, getRunExecution, getSession, isAuthError, listSessions, login as loginApi, logout as logoutApi,
  type ApiError, type MemoryStatus, type PendingActionProposal, type RunDetail, type RunEvent, type RunExecution, type RunRow,
  type SessionDetail, type SessionRow, type User,
} from './api'
import { getSessionActions } from './api'
import { mergeRunEvents } from './runEvents'
import ExecutionTimeline from './components/ExecutionTimeline.vue'
import EvidencePanel from './components/EvidencePanel.vue'
import MemoryPanel from './components/MemoryPanel.vue'
import PendingActionPanel from './components/PendingActionPanel.vue'
import ConversationComposer from './components/ConversationComposer.vue'
import { isSendShortcut } from './composer'
import { orderSnapshot, orderListSnapshot } from './executionView'
const AdminObservabilityView = defineAsyncComponent(() => import('./views/AdminObservabilityView.vue'))

const user = ref<User | null>(null)
const currentPath = ref(location.pathname)
const sessions = ref<SessionRow[]>([])
const activeId = ref<string | null>(null)
const detail = ref<SessionDetail | null>(null)
const initialising = ref(true)
const loadingSession = ref(false)
const sending = ref(false)
const recovering = ref(false)
const loginBusy = ref(false)
const sidebarOpen = ref(false)
const errorText = ref('')
const loginForm = ref({ username: '', password: '' })
const draft = ref('')
const currentRun = ref<RunDetail | null>(null)
const events = ref<RunEvent[]>([])
const execution = ref<RunExecution | null>(null)
const executionLoading = ref(false)
const memory = ref<MemoryStatus | null>(null)
const pendingActions = ref<PendingActionProposal[]>([])
const actionsLoading = ref(false)
const actionsError = ref('')
const memoryLoading = ref(false)
const memoryClearing = ref(false)
const activePanel = ref<'execution' | 'evidence' | 'memory' | 'actions'>('execution')
const runPanelOpen = ref(false)
const panelElement = ref<HTMLElement | null>(null)
let panelOpener: HTMLElement | null = null
watch(runPanelOpen, async open => {
  if (open) { panelOpener = document.activeElement as HTMLElement | null; await nextTick(); panelElement.value?.focus() }
  else panelOpener?.focus()
})
function panelKey(event: KeyboardEvent) {
  if (event.key === 'Escape') { runPanelOpen.value = false; return }
  if (event.key !== 'Tab') return
  const controls = panelElement.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], summary, [tabindex="0"]')
  if (!controls?.length) return
  const first = controls[0], last = controls[controls.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panelElement.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
function messageOrders(runId?: string) { return orderListSnapshot(detail.value?.runs.find(run => run.run_id === runId)?.answer?.business_results) }
function selectOrder(id: string) { draft.value = `查询我的订单 ${id} 的状态`; document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus() }
function messageOrder(runId?: string) { return orderSnapshot(detail.value?.runs.find(run => run.run_id === runId)?.answer?.business_results) }
const scrollBox = ref<HTMLElement | null>(null)
const runtime = ref<{ mode: string; notice: string; model?: string; suggestions: string[] } | null>(null)
const runtimeError = ref(false)
let stopRecovery: (() => void) | null = null
let currentAbort: AbortController | null = null
let executionLoadToken = 0
let memoryLoadToken = 0
let actionsLoadToken = 0

const activeSession = computed(() => sessions.value.find(item => item.session_id === activeId.value) || null)
const unresolvedRun = computed(() => ['RUNNING', 'UNKNOWN_RESULT'].includes(currentRun.value?.status || ''))
const canSend = computed(() => Boolean(draft.value.trim() && !sending.value && !recovering.value && !unresolvedRun.value && !loadingSession.value))
const welcome = computed(() => !activeId.value || (!loadingSession.value && !detail.value?.messages.length))
async function useSuggestion(text: string) {
  if (!activeId.value) {
    sending.value = true
    await newConversation()
    sending.value = false
  }
  draft.value = text
  await nextTick()
  document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus()
}
function composerKey(event: KeyboardEvent) {
  if (isSendShortcut(event)) { event.preventDefault(); void sendMessage() }
}
async function loadRuntime() {
  try {
    const response = await fetch('/api/runtime', { credentials: 'same-origin' })
    if (!response.ok) throw new Error('Runtime mode unavailable')
    runtime.value = await response.json()
  } catch { runtimeError.value = true }
}
const eventLabels: Record<string, string> = {
  started: '请求已接收', route_decided: '已完成路由', agent_started: '执行任务', retrieval_hop: '检索政策依据',
  tool_started: '调用只读工具', tool_finished: '工具执行完成', action_pending: '等待确认',
  answer: '已生成回答', done: '运行完成', error: '运行失败',
}

function setRoute(sessionId: string | null) {
  if (sessionId) {
    localStorage.setItem('shoprelay.current_session', sessionId)
    history.replaceState(null, '', `/chat/${encodeURIComponent(sessionId)}`)
    currentPath.value = `/chat/${encodeURIComponent(sessionId)}`
  } else {
    localStorage.removeItem('shoprelay.current_session')
    history.replaceState(null, '', '/chat')
    currentPath.value = '/chat'
  }
}

function openAdmin() {
  history.pushState(null, '', '/admin/observability')
  currentPath.value = '/admin/observability'
}

function backToChat() {
  const sessionId = activeId.value
  setRoute(sessionId)
}

function authExpired(error: unknown) {
  if (!isAuthError(error)) return false
  const hadAuthenticatedUser = Boolean(user.value)
  user.value = null; sessions.value = []; activeId.value = null; detail.value = null
  pendingActions.value = []
  stopRecovery?.(); stopRecovery = null
  if (currentPath.value !== '/admin/observability') setRoute(null)
  errorText.value = hadAuthenticatedUser ? '登录已过期，请重新登录。未完成请求没有自动重放。' : ''
  return true
}

async function restoreAuth() {
  initialising.value = true
  try {
    const profile = await currentUser()
    const rows = await listSessions()
    sessions.value = rows
    user.value = { user_id: '', display_name: profile.display_name }
    const pathId = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '')
    const savedId = pathId || localStorage.getItem('shoprelay.current_session')
    const selected = savedId && rows.some(item => item.session_id === savedId) ? savedId : rows[0]?.session_id || null
    await selectSession(selected, currentPath.value !== '/admin/observability')
  } catch (error) {
    if (!authExpired(error)) errorText.value = formatError(error)
  } finally { initialising.value = false }
}

async function signIn() {
  loginBusy.value = true; errorText.value = ''
  try {
    user.value = await loginApi(loginForm.value.username.trim(), loginForm.value.password)
    loginForm.value.password = ''
    await refreshSessions()
    ElMessage.success('登录成功')
  } catch (error) { errorText.value = formatError(error) }
  finally { loginBusy.value = false }
}

async function signOut() {
  try { await logoutApi() } catch (error) { if (!authExpired(error)) ElMessage.error(formatError(error)) }
  user.value = null; sessions.value = []; activeId.value = null; detail.value = null; currentRun.value = null
  execution.value = null; memory.value = null; pendingActions.value = []; events.value = []
  stopRecovery?.(); stopRecovery = null; setRoute(null)
}

async function refreshSessions() {
  try {
    sessions.value = await listSessions()
    if (activeId.value && !sessions.value.some(item => item.session_id === activeId.value)) await selectSession(null, false)
  } catch (error) { if (!authExpired(error)) errorText.value = formatError(error) }
}

async function newConversation() {
  try {
    const session = await createSession(null)
    sessions.value = [session, ...sessions.value]
    await selectSession(session.session_id)
    sidebarOpen.value = false
  } catch (error) { if (!authExpired(error)) ElMessage.error(formatError(error)) }
}

async function selectSession(id: string | null, updateRoute = true) {
  stopRecovery?.(); stopRecovery = null
  executionLoadToken += 1; memoryLoadToken += 1
  actionsLoadToken += 1
  activeId.value = id; currentRun.value = null; events.value = []; execution.value = null; memory.value = null; pendingActions.value = []; actionsError.value = ''; errorText.value = ''
  detail.value = null
  if (updateRoute) setRoute(id)
  if (!id) return
  loadingSession.value = true
  try {
    detail.value = await getSession(id)
    void refreshMemory(id)
    void refreshActions(id)
    if (updateRoute) await refreshSessions()
    const latest = detail.value.runs.at(-1)
    if (latest) await restoreRun(latest.run_id, latest.status === 'RUNNING')
    await scrollBottom()
  } catch (error) {
    if (authExpired(error)) return
    if ((error as Partial<ApiError>).status === 404) {
      sessions.value = sessions.value.filter(item => item.session_id !== id)
      setRoute(null); activeId.value = null
    } else errorText.value = formatError(error)
  } finally { loadingSession.value = false }
}

async function refreshActions(sessionId = activeId.value) {
  if (!sessionId) { pendingActions.value = []; actionsError.value = ''; return }
  const token = ++actionsLoadToken
  actionsLoading.value = true; actionsError.value = ''
  try {
    const rows = await getSessionActions(sessionId)
    if (token === actionsLoadToken && activeId.value === sessionId) pendingActions.value = rows
  } catch (error) {
    if (authExpired(error)) return
    if (token === actionsLoadToken && activeId.value === sessionId) {
      pendingActions.value = []; actionsError.value = formatError(error)
    }
  } finally { if (token === actionsLoadToken) actionsLoading.value = false }
}

async function openActionRun(runId: string) {
  activePanel.value = 'execution'; runPanelOpen.value = true
  await restoreRun(runId, false)
}

function appendEvent(event: RunEvent) {
  const runId = currentRun.value?.run_id
  if (!runId) return
  events.value = mergeRunEvents(events.value, [event], runId)
}

async function loadExecution(runId: string) {
  const token = ++executionLoadToken
  executionLoading.value = true
  try {
    const result = await getRunExecution(runId)
    if (token === executionLoadToken && currentRun.value?.run_id === runId) execution.value = result
  } catch (error) {
    if (authExpired(error)) return
    if (token === executionLoadToken && currentRun.value?.run_id === runId) execution.value = null
  } finally { if (token === executionLoadToken) executionLoading.value = false }
}

async function refreshMemory(sessionId = activeId.value) {
  if (!sessionId) { memory.value = null; return }
  const token = ++memoryLoadToken
  memoryLoading.value = true
  try {
    const status = await getMemoryStatus(sessionId)
    if (token === memoryLoadToken && activeId.value === sessionId) memory.value = status
  } catch (error) {
    if (token === memoryLoadToken && activeId.value === sessionId && !authExpired(error)) memory.value = null
  } finally { if (token === memoryLoadToken) memoryLoading.value = false }
}

async function confirmClearMemory() {
  const sessionId = activeId.value
  if (!sessionId || memoryClearing.value) return
  try {
    await ElMessageBox.confirm('这会清除本会话的压缩摘要和后续可读取的记忆水位；原始审计消息不会删除，也不会改变待确认操作。',
      '清除会话记忆？', { confirmButtonText: '确认清除', cancelButtonText: '保留记忆', type: 'warning' })
    memoryClearing.value = true
    await clearSessionMemory(sessionId)
    await refreshMemory(sessionId)
    ElMessage.success('会话记忆已清除')
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    if (!authExpired(error)) ElMessage.error(formatError(error))
  } finally { memoryClearing.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || !canSend.value) return
  if (!activeId.value) {
    sending.value = true
    await newConversation()
    sending.value = false
  }
  const sessionId = activeId.value
  if (!sessionId) return
  sending.value = true; errorText.value = ''; events.value = []
  currentRun.value = null
  stopRecovery?.(); stopRecovery = null
  const requestStartedAt = Date.now()
  const requestId = crypto.randomUUID()
  const controller = new AbortController(); currentAbort = controller
  let runId: string | null = null
  try {
    const { response, start } = await createChatStream(sessionId, content, requestId, controller.signal)
    runId = start.run_id
    if (runId) currentRun.value = await getRun(runId).catch(() => null)
    draft.value = ''
    await consumeSse(response, appendEvent)
    if (runId) await refreshRun(runId)
    await refreshSessions()
    detail.value = await getSession(sessionId)
    await refreshActions(sessionId)
    await scrollBottom()
  } catch (error) {
    if (authExpired(error)) return
    const aborted = error instanceof DOMException && error.name === 'AbortError'
    if (!aborted) errorText.value = formatError(error)
    if (runId) {
      // The run is already claimed. Recover persisted events/result; never repost the chat request.
      void restoreRun(runId, true)
    } else {
      // A transport failure before the response may have an unknown server outcome; do not retry automatically.
      void refreshSessions().then(async () => {
        const runs = (await getSession(sessionId).catch(() => null))?.runs || []
        const latest = runs.filter(run => new Date(run.created_at).valueOf() >= requestStartedAt - 5_000).at(-1)
        if (latest) void restoreRun(latest.run_id, true)
      })
    }
  } finally { sending.value = false; currentAbort = null }
}

async function refreshRun(runId: string) {
  currentRun.value = await getRun(runId)
  await loadExecution(runId)
}

async function restoreRun(runId: string, poll: boolean) {
  stopRecovery?.(); stopRecovery = null
  recovering.value = true
  let stopped = false
  stopRecovery = () => { stopped = true }
  let seq = 0
  try {
    const first = await getRun(runId)
    if (stopped) return
    currentRun.value = first
    void loadExecution(runId)
    const eventsResult = await getRunEvents(runId, 0)
    if (stopped) return
    eventsResult.events.forEach(appendEvent); seq = eventsResult.events_cursor
    if (poll && first.status === 'RUNNING') {
      const deadline = Date.now() + 300_000
      while (!stopped && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 900))
        const [nextEvents, nextRun] = await Promise.all([getRunEvents(runId, seq), getRun(runId)])
        nextEvents.events.forEach(appendEvent); seq = nextEvents.events_cursor
      currentRun.value = nextRun
        if (['SUCCEEDED', 'FAILED', 'MODEL_UNAVAILABLE', 'UNKNOWN_RESULT'].includes(nextRun.status)) break
      }
    }
    await refreshActions(first.session_id)
  } catch (error) { if (!authExpired(error)) errorText.value = formatError(error) }
  finally { if (!stopped) recovering.value = false }
}

function cancelLocalStream() { currentAbort?.abort() }
function formatError(error: unknown): string {
  const e = error as Partial<ApiError>
  if (e.code === 'UNKNOWN_RESULT') return '请求结果未知。系统不会自动重放；请查看运行记录后再决定下一步。'
  if (e.code === 'DATABASE_UNAVAILABLE') return '服务暂时无法读取数据，请稍后查看运行状态。'
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}
function formatTime(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' }).format(date)
}
function statusText(status?: string | null) {
  const labels: Record<string, string> = { READY: '就绪', CREATED: '已接收', RUNNING: '处理中', SUCCEEDED: '回复已保存', FAILED: '未能交付回复', MODEL_UNAVAILABLE: '模型服务不可用', UNKNOWN_RESULT: '结果未知' }
  return labels[status || ''] || status || '就绪'
}
function statusTone(status?: string | null) {
  if (status === 'SUCCEEDED') return 'success'
  if (status === 'FAILED' || status === 'MODEL_UNAVAILABLE' || status === 'UNKNOWN_RESULT') return 'danger'
  if (status === 'RUNNING' || status === 'CREATED') return 'warning'
  return 'info'
}
function eventText(event: RunEvent) {
  const payload = event.payload || {}
  const route = typeof payload.route === 'string' ? ` · ${payload.route}` : ''
  const strategy = typeof payload.strategy === 'string' ? ` · ${payload.strategy}` : ''
  const error = typeof payload.code === 'string' ? ` · ${payload.code}` : ''
  return `${eventLabels[event.type] || event.type}${route}${strategy}${error}`
}
function failedReply(code: string) {
  if (code === 'DEMO_BUDGET_EXHAUSTED') return '个人演示模型额度不足，并非模型连接故障。消息和记录已保留，请联系演示维护者核对额度。'
  if (code === 'ANSWER_SEMANTIC_VALIDATION_FAILED' || code === 'MODEL_INVALID_RESPONSE') return '这次回答没有通过来源核对，暂时不能可靠答复。你可以查看执行详情，或补充具体情况后继续咨询。'
  return '这次请求未能完成，消息和运行记录已保留。系统没有自动重试，请查看执行详情。'
}
async function openMessageSources(runId: string) {
  await restoreRun(runId, false)
  activePanel.value = 'evidence'; runPanelOpen.value = true
}
function historyItems() {
  const messages = detail.value?.messages || []
  const runs = detail.value?.runs || []
  const userMessages = messages.filter(item => item.role === 'user')
  const runByUserMessage = new Map<string, RunRow>()
  const usedRuns = new Set<string>()
  for (const message of userMessages) {
    const match = runs.filter(run => !usedRuns.has(run.run_id)).map(run => ({
      run,
      diff: Math.abs(new Date(run.created_at).valueOf() - new Date(message.created_at).valueOf()),
    })).sort((a, b) => a.diff - b.diff)[0]
    if (!match || match.diff > 120_000) continue
    runByUserMessage.set(message.message_id, match.run)
    usedRuns.add(match.run.run_id)
  }
  const persistedAnswerRuns = new Map<string, RunRow>()
  for (const message of messages.filter(item => item.role === 'assistant')) {
    const match = [...runByUserMessage.values()].filter(run => run.answer?.answer === message.content)
      .map(run => ({ run, diff: Math.abs(new Date(run.updated_at || run.created_at).valueOf() - new Date(message.created_at).valueOf()) }))
      .sort((a, b) => a.diff - b.diff)[0]
    if (match && match.diff <= 120_000) persistedAnswerRuns.set(message.message_id, match.run)
  }
  const items: Array<{ key: string; kind: 'user' | 'assistant'; content: string; time: string; status?: string; runId?: string }> = []
  for (const message of messages) {
    if (message.role === 'user') {
      items.push({ key: message.message_id, kind: 'user', content: message.content, time: message.created_at, status: message.status })
      const run = runByUserMessage.get(message.message_id)
      if (!run) continue
      if (run.answer?.answer && ![...persistedAnswerRuns.values()].some(item => item.run_id === run.run_id)) {
        items.push({ key: `${run.run_id}-answer`, kind: 'assistant', content: run.answer.answer, time: run.updated_at || run.created_at, status: run.status, runId: run.run_id })
      } else if (run.error) {
        items.push({ key: `${run.run_id}-error`, kind: 'assistant', content: failedReply(run.error.code), time: run.updated_at || run.created_at, status: run.status, runId: run.run_id })
      }
      continue
    }
    const run = persistedAnswerRuns.get(message.message_id)
    items.push({ key: message.message_id, kind: 'assistant', content: message.content, time: message.created_at, status: run?.status || message.status, runId: run?.run_id })
  }
  return items
}
async function scrollBottom() { await nextTick(); if (scrollBox.value) scrollBox.value.scrollTop = scrollBox.value.scrollHeight }

watch(() => detail.value?.messages.length, () => { void scrollBottom() })
watch(loadingSession, loading => { if (!loading) void scrollBottom() })
onMounted(() => { void restoreAuth(); void loadRuntime(); window.addEventListener('popstate', handlePop) })
onUnmounted(() => { stopRecovery?.(); cancelLocalStream(); window.removeEventListener('popstate', handlePop) })
function handlePop() {
  currentPath.value = location.pathname
  if (currentPath.value === '/admin/observability') return
  const id = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '') || null
  void selectSession(id, false)
}
</script>

<template>
  <main v-if="!user && !initialising" class="login-shell">
    <section class="login-card">
      <div class="brand-mark">S</div>
      <h1>ShopRelay 客服</h1>
      <p class="muted">登录以查看你的会话并继续咨询。</p>
      <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" show-icon class="form-alert" />
      <el-form @submit.prevent="signIn" label-position="top">
        <el-form-item label="用户名"><el-input v-model="loginForm.username" autocomplete="username" placeholder="输入用户名" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="loginForm.password" type="password" show-password autocomplete="current-password" placeholder="输入密码" @keyup.enter="signIn" /></el-form-item>
        <el-button type="primary" class="login-button" :loading="loginBusy" :disabled="!loginForm.username || !loginForm.password" @click="signIn">安全登录</el-button>
      </el-form>
      <p class="login-foot">{{ runtime?.notice || '使用服务端配置的演示账户。请勿输入真实个人或支付信息。' }}</p>
    </section>
  </main>

  <div v-else-if="initialising" class="boot-screen"><el-icon class="is-loading"><Loading /></el-icon><span>正在恢复安全会话…</span></div>

  <AdminObservabilityView v-else-if="currentPath === '/admin/observability'" @back-to-chat="backToChat" @sign-out="signOut" />

  <div v-else class="app-shell">
    <div v-if="sidebarOpen" class="mobile-scrim" @click="sidebarOpen = false"></div>
    <aside class="sidebar" :class="{ 'sidebar-open': sidebarOpen }">
      <div class="brand-row"><div class="brand-mark small">S</div><div><strong>ShopRelay</strong><span>智能客服</span></div><button class="icon-button mobile-close" aria-label="关闭菜单" @click="sidebarOpen = false">×</button></div>
      <el-button class="new-chat" type="primary" @click="newConversation"><span class="plus">＋</span> 新建会话</el-button>
      <el-button class="admin-nav" plain @click="openAdmin">运行监控</el-button>
      <div class="list-heading"><span>最近会话</span><span>{{ sessions.length }}</span></div>
      <div class="session-list">
        <button v-for="item in sessions" :key="item.session_id" class="session-item" :class="{ active: item.session_id === activeId }" @click="selectSession(item.session_id); sidebarOpen = false">
          <span class="session-dot"></span><span class="session-copy"><strong>{{ item.title || '新会话' }}</strong><small>{{ formatTime(item.updated_at || item.created_at) }}</small></span>
        </button>
        <div v-if="sessions.length === 0" class="empty-sessions"><span class="empty-icon">◷</span><p>还没有会话</p><small>开始一段新对话</small></div>
      </div>
      <div class="sidebar-bottom"><div class="user-info"><strong>{{ user?.display_name || '已登录用户' }}</strong><small>演示账户 · 已登录</small></div><el-button text class="logout-button" @click="signOut">退出</el-button></div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="topbar-left"><button class="icon-button mobile-menu" aria-label="打开菜单" @click="sidebarOpen = true">菜单</button><div><p class="breadcrumb">客服工作台</p><h1>{{ activeSession?.title || '新会话' }}</h1></div></div>
        <div class="topbar-right"><span class="server-state">{{ runtimeError ? '模式未知' : runtime?.mode === 'REAL_MODEL' ? runtime.model : runtime?.mode === 'OFFLINE_ACCEPTANCE' ? '离线验收' : '核对模式中' }}</span><el-button class="mobile-panel-toggle" text @click="runPanelOpen = !runPanelOpen">{{ runPanelOpen ? '收起详情' : '执行详情' }}</el-button></div>
      </header>
      <p class="runtime-notice" :class="{ 'notice-error': runtimeError }">{{ runtimeError ? '暂时无法确认运行模式，请稍后重试。' : runtime?.notice }}</p>

      <div class="content-grid" :class="{ 'is-welcome': welcome }">
        <section class="chat-column">
          <div class="chat-scroll" ref="scrollBox">
            <div v-if="welcome" class="welcome-state">
              <p class="welcome-kicker">ShopRelay 客服</p>
              <h2>说说你想解决的问题</h2><p class="welcome-copy">查订单记录、了解商品，或一起核对售后政策。</p>
              <ConversationComposer v-model="draft" :can-send="canSend" :disabled="sending || recovering" :sending="sending" :recovering="recovering" :unknown="false" @send="sendMessage" @keydown="composerKey" />
              <div class="suggestions-heading"><strong>可以从这些问题开始</strong><span>点击填入，发送后才会处理</span></div>
              <div class="suggestions"><button v-for="text in runtime?.suggestions || []" :key="text" @click="useSuggestion(text)">{{ text }} <span aria-hidden="true">↗</span></button></div>
            </div>
            <div v-else-if="loadingSession" class="loading-history"><el-skeleton :rows="5" animated /></div>
            <template v-else>
              <div class="conversation-intro"><span class="eyebrow">对话记录</span><span class="intro-line"></span><small>{{ detail?.messages.length || 0 }} 条消息</small></div>
              <div v-for="item in historyItems()" :key="item.key" class="message-row" :class="item.kind">
                <div v-if="item.kind === 'assistant'" class="assistant-avatar">S</div>
                <article class="message-card" :class="item.kind">
                  <div class="message-meta"><strong>{{ item.kind === 'user' ? '你' : 'ShopRelay' }}</strong><time>{{ formatTime(item.time) }}</time><span v-if="item.status && ['FAILED', 'MODEL_UNAVAILABLE', 'UNKNOWN_RESULT'].includes(item.status)" class="failed-label">{{ statusText(item.status) }}</span></div>
                  <p class="message-text">{{ item.content }}</p>
                  <div v-if="messageOrders(item.runId)" class="order-options" aria-label="本人订单选项">
                    <button v-for="(order,index) in messageOrders(item.runId)?.orders" :key="order.id" :disabled="sending || recovering" @click="selectOrder(order.id)"><span>{{ index+1 }}. {{ order.id }}</span><span>{{ order.status }}</span><strong>{{ order.amount || '金额未记录' }}</strong><small>填入查询，不自动发送</small></button>
                    <button v-if="messageOrders(item.runId)?.hasMore" :disabled="sending || recovering" @click="draft = '列出我的订单下一页'">下一页（填入，不自动发送）</button>
                  </div>
                  <div v-if="messageOrder(item.runId)" class="order-snapshot"><div><small>订单记录快照</small><strong>{{ messageOrder(item.runId)?.id }}</strong></div><span>{{ messageOrder(item.runId)?.status }}</span><strong v-if="messageOrder(item.runId)?.amount">{{ messageOrder(item.runId)?.amount }}</strong><details><summary>原始记录值</summary>状态 {{ messageOrder(item.runId)?.rawStatus }} · 金额 {{ messageOrder(item.runId)?.cents ?? '未记录' }} 分</details></div>
                  <button v-if="item.runId" class="evidence-link message-source" :disabled="sending || recovering || unresolvedRun" @click="openMessageSources(item.runId)">查看依据与执行 <span aria-hidden="true">↗</span></button>
                </article>
                <div v-if="item.kind === 'user'" class="user-avatar message-avatar">{{ (user?.display_name || 'U').slice(0, 1) }}</div>
              </div>
              <div v-if="sending || recovering" class="execution-card">
                <div class="execution-head"><span class="pulse-dot"></span><strong>{{ sending ? '正在处理你的请求' : '正在恢复运行状态' }}</strong><small>{{ currentRun?.run_id ? `Run ${currentRun.run_id.slice(0, 8)}` : '等待服务端状态' }}</small></div>
                <div v-if="events.length" class="event-list"><div v-for="event in events.slice(-5)" :key="event.event_id" class="event-item"><span class="event-check">{{ event.type === 'error' ? '!' : '·' }}</span><span>{{ eventText(event) }}</span><time>{{ formatTime(event.timestamp) }}</time></div></div>
                <div v-else class="progress-note">正在等待持久化执行事件…</div>
                <button v-if="sending" class="quiet-link" @click="cancelLocalStream">暂时离开</button>
              </div>
              <div v-if="errorText" class="inline-error"><span>!</span><div><strong>状态提示</strong><p>{{ errorText }}</p><small v-if="currentRun?.error?.code">错误码：{{ currentRun.error.code }}</small></div></div>
              <div class="end-marker"><span></span><small>当前会话内容由服务端保存</small><span></span></div>
            </template>
          </div>
          <ConversationComposer v-if="!welcome" v-model="draft" :can-send="canSend" :disabled="sending || recovering" :sending="sending" :recovering="recovering" :unknown="currentRun?.status === 'UNKNOWN_RESULT'" @send="sendMessage" @keydown="composerKey" />
        </section>

        <div v-if="runPanelOpen" class="panel-scrim" @click="runPanelOpen = false" />
        <aside ref="panelElement" class="run-panel" :class="{ 'run-panel-open': runPanelOpen }" role="dialog" aria-modal="true" aria-label="本次执行与来源" tabindex="-1" @keydown="panelKey">
          <div class="panel-title"><h2>本次执行与来源</h2><button class="icon-button" aria-label="关闭详情" @click="runPanelOpen = false">关闭</button></div>
          <nav class="workbench-tabs" aria-label="执行详情模块">
            <button :class="{ active: activePanel === 'execution' }" @click="activePanel = 'execution'">执行过程</button>
            <button :class="{ active: activePanel === 'evidence' }" @click="activePanel = 'evidence'">证据引用</button>
            <button :class="{ active: activePanel === 'memory' }" @click="activePanel = 'memory'">会话记忆</button>
            <button :class="{ active: activePanel === 'actions' }" @click="activePanel = 'actions'; refreshActions()">操作提案<span v-if="pendingActions.length" class="tab-count">{{ pendingActions.length }}</span></button>
          </nav>
          <div v-if="activePanel === 'execution'" class="run-summary">
            <div v-if="currentRun?.latency_ms != null" class="run-id"><span>执行耗时</span><strong>{{ currentRun.latency_ms < 1000 ? `${Math.round(currentRun.latency_ms)} 毫秒` : `${(currentRun.latency_ms / 1000).toFixed(1)} 秒` }}</strong></div>
            <ExecutionTimeline :execution="execution" :events="events" :loading="executionLoading" />
          </div>
          <div v-else-if="activePanel === 'evidence'" class="run-summary"><EvidencePanel :execution="execution" :loading="executionLoading" /></div>
          <div v-else-if="activePanel === 'memory'" class="run-summary"><MemoryPanel :memory="memory" :loading="memoryLoading" :clearing="memoryClearing" @clear="confirmClearMemory" /></div>
          <div v-else class="run-summary"><PendingActionPanel :actions="pendingActions" :loading="actionsLoading" :error="actionsError" @open-run="openActionRun" @refresh="refreshActions()" /></div>
          <details class="panel-note"><summary>如何理解这些记录</summary><p>这里只显示本账号会话的已保存记录，不含模型内部推理。引用与摘要不独立证明答案正确；执行结束也不代表业务申请已批准。</p></details>
        </aside>
      </div>
    </section>
  </div>
</template>
