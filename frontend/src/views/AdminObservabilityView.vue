<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { ElDrawer, ElOption, ElRadioButton, ElRadioGroup, ElResult, ElSelect, ElTable, ElTableColumn } from 'element-plus'
import { ElLoadingDirective as vLoading } from 'element-plus/es/components/loading/index.mjs'
import 'element-plus/theme-chalk/el-drawer.css'
import 'element-plus/theme-chalk/el-option.css'
import 'element-plus/theme-chalk/el-radio-button.css'
import 'element-plus/theme-chalk/el-radio-group.css'
import 'element-plus/theme-chalk/el-result.css'
import 'element-plus/theme-chalk/el-select.css'
import 'element-plus/theme-chalk/el-table.css'
import 'element-plus/theme-chalk/el-table-column.css'
import {
  buildRunQuery, fetchOverview, fetchRunDetail, fetchRuns, formatDate, formatMetric, formatMoney, formatPercent, formatRunDuration, retrievalStrategyLabel,
  pageTrend, trendBarHeight, statusColor, type AdminApiError, type AdminOverview, type AdminRunDetail, type AdminRunRow,
} from '../adminObservability'

const emit = defineEmits<{ (event: 'back-to-chat'): void; (event: 'sign-out'): void }>()
const overview = ref<AdminOverview | null>(null)
const rows = ref<AdminRunRow[]>([])
const hasMore = ref(false)
const page = ref(1)
const pageSize = 25
const loading = ref(false)
const detailLoading = ref(false)
const detailVisible = ref(false)
const selectedRun = ref<AdminRunDetail | null>(null)
const selectedRunId = ref('')
const error = ref('')
const authState = ref<'READY' | 'UNAUTHENTICATED' | 'FORBIDDEN' | 'ERROR'>('READY')
const preset = ref('24h')
const customFrom = ref('')
const customTo = ref('')
const filters = ref({ status: '', strategy: '', agent: '', toolId: '', orchestrator: '' })
let controller: AbortController | null = null
let detailController: AbortController | null = null
let requestSequence = 0
let onVisibility: (() => void) | null = null

const windowRange = computed(() => {
  const end = new Date()
  const durations: Record<string, number> = { '1h': 3600_000, '24h': 86400_000, '7d': 7 * 86400_000, '30d': 30 * 86400_000 }
  if (preset.value === 'custom') {
    const from = customFrom.value ? new Date(customFrom.value) : null
    const to = customTo.value ? new Date(customTo.value) : null
    return { from: from && Number.isFinite(from.valueOf()) ? from.toISOString() : '', to: to && Number.isFinite(to.valueOf()) ? to.toISOString() : '' }
  }
  const duration = durations[preset.value] || durations['24h']
  return { from: new Date(end.valueOf() - duration).toISOString(), to: end.toISOString() }
})
const trend = computed(() => pageTrend(rows.value, windowRange.value.from, windowRange.value.to, 7))
const maxTrend = computed(() => Math.max(1, ...trend.value.map(item => item.count)))
const statusEntries = computed(() => Object.entries(overview.value?.run_counts.by_status || {}).sort(([a], [b]) => a.localeCompare(b)))
const visibleRange = computed(() => {
  if (!overview.value) return ''
  return `${formatDate(overview.value.window.from)} — ${formatDate(overview.value.window.to)}（本地时区显示）`
})

function setPreset(value: string) { preset.value = value; page.value = 1; void loadDashboard() }
function rangeError(): string {
  const { from, to } = windowRange.value
  if (!from || !to || Number.isNaN(Date.parse(from)) || Number.isNaN(Date.parse(to))) return '请选择有效的开始和结束时间。'
  const duration = Date.parse(to) - Date.parse(from)
  if (duration <= 0) return '结束时间必须晚于开始时间。'
  if (duration > 31 * 86400_000) return '时间范围不能超过31天。'
  return ''
}
async function loadDashboard() {
  const validation = rangeError()
  if (validation) { error.value = validation; return }
  controller?.abort()
  controller = new AbortController()
  const signal = controller.signal
  const sequence = ++requestSequence
  const { from, to } = windowRange.value
  const query = buildRunQuery(from, to, page.value, pageSize, filters.value)
  loading.value = true; error.value = ''; authState.value = 'READY'
  try {
    const [summary, result] = await Promise.all([fetchOverview(from, to, signal), fetchRuns(query, signal)])
    if (sequence !== requestSequence || signal.aborted) return
    overview.value = summary; rows.value = result.items; hasMore.value = result.has_more
  } catch (cause) {
    if (signal.aborted || sequence !== requestSequence) return
    const failure = cause as Partial<AdminApiError>
    if (failure.status === 401) authState.value = 'UNAUTHENTICATED'
    else if (failure.status === 403) authState.value = 'FORBIDDEN'
    else { authState.value = 'ERROR'; error.value = failure.status === 400 ? '所选时间窗口超过服务端限制或参数无效，请缩小范围。' : (cause instanceof Error ? cause.message : '监控数据暂时不可用。') }
    overview.value = null; rows.value = []; hasMore.value = false
  } finally { if (sequence === requestSequence) loading.value = false }
}
function applyFilters() { page.value = 1; void loadDashboard() }
function changePage(delta: number) { page.value = Math.max(1, page.value + delta); void loadDashboard() }
async function openRun(row: AdminRunRow) {
  detailController?.abort(); detailController = new AbortController()
  selectedRunId.value = row.run_id; selectedRun.value = null; detailVisible.value = true; detailLoading.value = true
  try { selectedRun.value = await fetchRunDetail(row.run_id, detailController.signal) }
  catch (cause) {
    if (detailController.signal.aborted) return
    const failure = cause as Partial<AdminApiError>
    if (failure.status === 401) authState.value = 'UNAUTHENTICATED'
    else if (failure.status === 403) authState.value = 'FORBIDDEN'
    else ElMessage.error(failure.status === 404 ? '该运行记录不存在。' : '运行详情读取失败。')
  } finally { if (!detailController.signal.aborted) detailLoading.value = false }
}
function closeDetail() { detailController?.abort(); detailVisible.value = false; selectedRun.value = null; selectedRunId.value = '' }
function back() { controller?.abort(); closeDetail(); emit('back-to-chat') }
function statusLabel(status: string) {
  const labels: Record<string, string> = { SUCCEEDED: '执行成功', FAILED: '执行失败', MODEL_UNAVAILABLE: '模型不可用', UNKNOWN_RESULT: '结果未知', RUNNING: '运行中', CREATED: '已创建' }
  return labels[status] || status
}
function rate(value: number | null | undefined) { return formatPercent(value) }
function countMap(map: Record<string, number> | null | undefined) { return Object.entries(map || {}).sort((a, b) => b[1] - a[1]) }
async function copyRunId(id: string) {
  try { await navigator.clipboard.writeText(id) } catch { error.value = '复制失败，请从详情选择完整运行编号。' }
}
function trendHeight(value: number) { return trendBarHeight(value, maxTrend.value) }
function eventText(type: string) {
  const labels: Record<string, string> = { started: '请求开始', route_decided: '路由决策', agent_started: 'Agent启动', retrieval_hop: '检索阶段', tool_started: '工具启动', tool_finished: '工具完成', action_pending: '动作待确认', answer: '答案完成', done: '运行终态', error: '执行错误' }
  return labels[type] || type
}
function eventSummary(data: Record<string, unknown>) {
  const fields = ['strategy', 'sub_strategy', 'agent', 'tool_id', 'capability', 'status', 'error_code', 'stage', 'degraded_reason', 'knowledge_version']
  return fields.filter(key => data[key] !== undefined && data[key] !== null).map(key => `${key}=${String(data[key])}`).join(' · ')
}
function evidenceSummary(data: Record<string, unknown>) {
  return Object.entries(data).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : String(value)}`).join(' · ')
}
function detailDuration(span: { duration_ms?: number }) { return span.duration_ms == null ? '不可统计' : formatMetric(span.duration_ms, ' ms') }

onMounted(() => {
  void loadDashboard()
  onVisibility = () => { if (document.visibilityState === 'visible') void loadDashboard() }
  document.addEventListener('visibilitychange', onVisibility)
})
onUnmounted(() => { controller?.abort(); detailController?.abort(); if (onVisibility) document.removeEventListener('visibilitychange', onVisibility) })
</script>

<template>
  <main class="admin-shell">
    <header class="admin-topbar">
      <div class="admin-brand"><span class="admin-mark">S</span><div><strong>ShopRelay</strong><small>管理员 · 运行可观测性</small></div></div>
      <div class="admin-actions"><el-button text @click="back">返回工作台</el-button><el-button text @click="emit('sign-out')">退出</el-button></div>
    </header>

    <section v-if="authState === 'UNAUTHENTICATED'" class="admin-gate"><el-result icon="warning" title="需要登录" sub-title="请先通过ShopRelay安全登录，再访问管理员监控。"><template #extra><el-button type="primary" @click="back">前往登录</el-button></template></el-result></section>
    <section v-else-if="authState === 'FORBIDDEN'" class="admin-gate"><el-result icon="error" title="无权访问" sub-title="当前账号未被服务端管理员 allowlist 授权。管理员数据不会缓存在普通用户页面中。"><template #extra><el-button @click="back">返回工作台</el-button></template></el-result></section>
    <section v-else class="admin-content">
      <div class="page-heading"><div><p class="admin-eyebrow">只读监控</p><h1>运行总览</h1><p>指标来自持久化Run、事件、Span与模型记录。成功仅表示执行终态，不代表答案正确。</p></div><el-button :loading="loading" @click="loadDashboard">刷新数据</el-button></div>
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="admin-alert" />
      <section class="filters-card">
        <div class="filter-line"><span class="filter-label">时间范围</span><el-radio-group v-model="preset" size="small" @change="setPreset(String($event))"><el-radio-button label="1h">1小时</el-radio-button><el-radio-button label="24h">24小时</el-radio-button><el-radio-button label="7d">7天</el-radio-button><el-radio-button label="30d">30天</el-radio-button><el-radio-button label="custom">自定义</el-radio-button></el-radio-group></div>
        <div v-if="preset === 'custom'" class="filter-line custom-range"><label>开始 <input v-model="customFrom" type="datetime-local"></label><label>结束 <input v-model="customTo" type="datetime-local"></label><el-button size="small" type="primary" @click="loadDashboard">应用</el-button><small>最长31天，时间按本地输入并以UTC发送</small></div>
        <p class="filter-scope">以下筛选仅影响运行列表及当前页趋势；时间窗总览不随这些条件变化。</p><div class="filter-line filters-grid">
          <label>执行器<el-select v-model="filters.orchestrator" clearable placeholder="全部" @change="applyFilters"><el-option label="Legacy（有证据）" value="legacy"/><el-option label="LangGraph（有证据）" value="langgraph"/><el-option label="未知/证据冲突" value="unknown"/></el-select></label>
          <label>Run状态<el-select v-model="filters.status" clearable placeholder="全部" @change="applyFilters"><el-option v-for="item in ['SUCCEEDED','FAILED','MODEL_UNAVAILABLE','UNKNOWN_RESULT','RUNNING','CREATED']" :key="item" :label="statusLabel(item)" :value="item"/></el-select></label>
          <label>路由策略<el-select v-model="filters.strategy" clearable placeholder="全部" @change="applyFilters"><el-option v-for="item in ['DIRECT','SINGLE','MULTI','AGENT','REFUSE']" :key="item" :label="item" :value="item"/></el-select></label>
          <label>专业Agent<el-select v-model="filters.agent" clearable placeholder="全部" @change="applyFilters"><el-option v-for="item in [{label:'政策',value:'policy'},{label:'订单物流',value:'order_logistics'},{label:'售后',value:'after_sales'},{label:'商品',value:'product'}]" :key="item.value" :label="item.label" :value="item.value"/></el-select></label>
          <label>工具ID<el-input v-model="filters.toolId" clearable placeholder="精确筛选" @keyup.enter="applyFilters" /></label>
        </div>
        <div class="window-caption">{{ visibleRange || '正在读取窗口…' }} · 服务端窗口最多5000 Run</div>
      </section>

      <div v-if="loading && !overview" class="admin-loading"><el-icon class="is-loading"><Loading /></el-icon> 正在读取授权监控数据…</div>
      <template v-else-if="overview">
        <p class="scope-note">全时间窗概览 · 执行成功不代表任务完成，费用为记录范围内估算。</p>
        <section class="kpi-grid"><article class="kpi-card"><span>窗口Run总数</span><strong>{{ overview.run_counts.total }}</strong><small>窗口内按Run行计数</small></article><article class="kpi-card warning-kpi"><span>执行失败 / 结果未知</span><strong>{{ (overview.run_counts.by_status.FAILED ?? 0) + (overview.run_counts.by_status.MODEL_UNAVAILABLE ?? 0) }} / {{ overview.run_counts.unknown_result_count }}</strong><small>分别计数，不当作任务评分</small></article><article class="kpi-card"><span>Run墙钟时长 P95</span><strong>{{ formatMetric(overview.run_wall_duration_ms.p95, ' ms') }}</strong><small>非HTTP端到端耗时 · 样本 {{ overview.run_wall_duration_ms.sample_count }}</small></article><article class="kpi-card"><span>Provider Tokens</span><strong>{{ formatMetric(overview.model.prompt_tokens + overview.model.completion_tokens) }}</strong><small>Provider usage记录 {{ overview.model.provider_usage_run_count }}/{{ overview.model.usage_coverage_denominator }}</small></article><article class="kpi-card"><span>费用估算</span><strong>{{ formatMoney(overview.model.estimated_cost_usd) }}</strong><small>峰值费率估算，非账单 · {{ overview.model.estimated_cost_sample_count }}条</small></article></section>
        <details class="engineering-details"><summary>执行、延迟与工具细项</summary><section class="kpi-grid"><article class="kpi-card"><span>终态Run</span><strong>{{ overview.run_counts.terminal_denominator }}</strong><small>包含 UNKNOWN_RESULT</small></article><article class="kpi-card"><span>执行成功率</span><strong>{{ rate(overview.run_counts.success_rate) }}</strong><small>SUCCEEDED / 终态分母</small></article><article class="kpi-card"><span>执行失败率</span><strong>{{ rate(overview.run_counts.failure_rate) }}</strong><small>FAILED + MODEL_UNAVAILABLE</small></article><article class="kpi-card"><span>记录执行耗时 P50</span><strong>{{ formatMetric(overview.latency_ms.p50, ' ms') }}</strong><small>样本 {{ overview.latency_ms.sample_count }} · {{ overview.latency_ms.source }}</small></article><article class="kpi-card"><span>记录执行耗时 P95</span><strong>{{ formatMetric(overview.latency_ms.p95, ' ms') }}</strong><small>Run.latency_ms · 样本 {{ overview.latency_ms.sample_count }}</small></article><article class="kpi-card"><span>Run墙钟时长 P50</span><strong>{{ formatMetric(overview.run_wall_duration_ms.p50, ' ms') }}</strong><small>started_at → finished_at · 样本 {{ overview.run_wall_duration_ms.sample_count }}</small></article><article class="kpi-card"><span>实际Agent启动</span><strong>{{ overview.agent_execution.actual_agent_started_count }}</strong><small>不等于AGENT路由意图</small></article><article class="kpi-card"><span>工具启动 / 失败</span><strong>{{ overview.tools.started_count }} <em>/ {{ overview.tools.failed_count }}</em></strong><small>失败分母：{{ overview.tools.failure_rate_denominator }}次完成事件</small></article></section></details>
        <section class="analysis-grid">
          <article class="panel-card"><div class="panel-heading"><div><h2>Run状态分布</h2><small>全时间窗 · 总计 {{ overview.run_counts.total }}</small></div></div>
            <div v-if="statusEntries.length" class="status-list"><div v-for="[key,value] in statusEntries" :key="key" class="status-item"><span class="status-dot" :style="{background:statusColor(key)}"></span><span>{{ statusLabel(key) }}</span><b>{{ value }}</b><small>{{ overview.run_counts.total ? ((value / overview.run_counts.total) * 100).toFixed(1) : '0.0' }}%</small></div></div><div v-else class="empty-note">所选时间窗口暂无Run记录。</div>
          </article>
          <article class="panel-card"><div class="panel-heading"><div><h2>筛选后当前页趋势</h2><small>仅当前页最多{{ pageSize }}条；不是全窗口时序</small></div></div>
            <div v-if="trend.length" class="trend-bars"><div v-for="item in trend" :key="item.key" class="trend-column"><div class="trend-bar-area"><span class="trend-bar" :style="{height:trendHeight(item.count)}" :title="`${formatDate(item.start)} 至 ${formatDate(item.end)}：${item.count}条当前页样本`"></span></div><small>{{ item.label }}</small><b>{{ item.count }}</b></div></div><div v-else class="empty-note">当前页没有记录；不表示整个时间窗为零。</div>
          </article>
        </section>
        <details class="engineering-details"><summary>专业节点、工具、模型与检索明细</summary><section class="analysis-grid">
          <article class="panel-card"><div class="panel-heading"><div><h2>Agent协作</h2><small>实际 agent_started 事件；Supervisor Span {{ overview.agent_execution.supervisor_span_count }}</small></div></div>
            <div class="metric-pair"><div><span>Agent启动</span><b>{{ overview.agent_execution.actual_agent_started_count }}</b></div><div><span>Handoff</span><b>{{ overview.agent_execution.handoff_count }}</b></div></div>
            <div v-if="countMap(overview.agent_execution.by_agent).length" class="rank-list"><div v-for="[name,value] in countMap(overview.agent_execution.by_agent)" :key="name"><span>{{ name }}</span><b>{{ value }}</b></div></div><div v-else class="empty-note">当前窗口没有可统计的专业Agent启动。</div>
            <small class="muted-note">Agent调用/Run：均值 {{ formatMetric(overview.agent_execution.agent_calls_per_run_mean) }} · 中位数 {{ formatMetric(overview.agent_execution.agent_calls_per_run_median) }}。MULTI检索不表示多Agent协作。</small>
          </article>
          <article class="panel-card"><div class="panel-heading"><div><h2>工具调用</h2><small>失败率分母为已完成工具事件</small></div></div>
            <div class="metric-pair"><div><span>启动</span><b>{{ overview.tools.started_count }}</b></div><div><span>失败</span><b>{{ overview.tools.failed_count }}</b></div></div>
            <div v-if="countMap(overview.tools.by_tool).length" class="rank-list"><div v-for="[name,value] in countMap(overview.tools.by_tool)" :key="name"><span>{{ name }}</span><b>{{ value }}</b></div></div><div v-else class="empty-note">当前窗口没有工具调用记录。</div>
            <small class="muted-note">耗时样本 {{ overview.tools.duration_sample_count }} · P50 {{ formatMetric(overview.tools.duration_p50_ms, ' ms') }} · P95 {{ formatMetric(overview.tools.duration_p95_ms, ' ms') }}</small>
          </article>
          <article class="panel-card"><div class="panel-heading"><div><h2>模型与费用</h2><small>真实Provider usage与节点指标分开</small></div></div>
            <div class="metric-pair"><div><span>Prompt tokens</span><b>{{ formatMetric(overview.model.prompt_tokens) }}</b></div><div><span>Completion tokens</span><b>{{ formatMetric(overview.model.completion_tokens) }}</b></div></div>
            <div class="model-meta"><span>记录的节点模型请求 <b>{{ overview.model.recorded_node_request_count }}</b></span><span>Stub Runs <b>{{ overview.model.stub_run_count }}</b></span><span>未计量非Stub <b>{{ overview.model.unknown_or_unmetered_run_count }}</b></span></div>
            <div v-if="countMap(Object.fromEntries(Object.entries(overview.model.by_model_run_usage).map(([k,v])=>[k,v.run_count]))).length" class="rank-list"><div v-for="[model,data] in Object.entries(overview.model.by_model_run_usage)" :key="model"><span>{{ model }} · {{ data.provider_usage_run_count }}条usage</span><b>{{ data.prompt_tokens + data.completion_tokens }} tok</b></div></div>
            <small class="muted-note">离线估算Token不混入Provider usage；估算金额不是供应商账单。</small>
          </article>
          <article class="panel-card"><div class="panel-heading"><div><h2>检索策略</h2><small>选择策略与实际执行分列</small></div></div>
            <div class="retrieval-columns"><div><strong>实际执行 · 规范口径</strong><p v-for="[name,value] in countMap(overview.retrieval.canonical_executed_strategy_counts)" :key="name">{{ retrievalStrategyLabel(name) }} <b>{{ value }}</b></p><small v-if="!Object.keys(overview.retrieval.canonical_executed_strategy_counts).length">暂无</small></div><div><strong>请求选择 · 规范口径</strong><p v-for="[name,value] in countMap(overview.retrieval.canonical_requested_strategy_counts)" :key="name">{{ retrievalStrategyLabel(name) }} <b>{{ value }}</b></p><small v-if="!Object.keys(overview.retrieval.canonical_requested_strategy_counts).length">暂无</small></div></div>
            <details class="raw-strategy-details"><summary>查看原始策略标签</summary><div class="retrieval-columns"><div><strong>实际执行 · raw</strong><p v-for="[name,value] in countMap(overview.retrieval.executed_strategy_counts)" :key="name">{{ name }} <b>{{ value }}</b></p></div><div><strong>请求选择 · raw</strong><p v-for="[name,value] in countMap(overview.retrieval.requested_strategy_counts)" :key="name">{{ name }} <b>{{ value }}</b></p></div></div></details>
            <small class="muted-note">Raw/canonical守恒：请求 {{ overview.retrieval.canonical_counts_conserve_raw_counts.requested ? '通过' : '异常' }} · 实际执行 {{ overview.retrieval.canonical_counts_conserve_raw_counts.executed ? '通过' : '异常' }}。RRF融合与融合+重排分开统计。</small>
            <div class="retrieval-foot">降级 {{ overview.retrieval.degraded_run_count }} · BM25 {{ overview.retrieval.bm25_count }} · HYBRID_RERANK {{ overview.retrieval.hybrid_rerank_count }} · 引用 {{ overview.retrieval.citation_count }}（不代表答案准确率）</div>
            <small v-if="Object.keys(overview.retrieval.degraded_reason_counts).length" class="muted-note">降级原因：{{ Object.entries(overview.retrieval.degraded_reason_counts).map(([k,v])=>`${k} (${v})`).join('、') }}</small>
          </article>
        </section></details>

        <section class="runs-panel panel-card">
          <div class="panel-heading"><div><h2>运行记录</h2><small>按创建时间计数；动作审计不混作聊天次数。筛选仅作用于本列表和页内趋势。</small></div><div class="page-controls"><el-button size="small" :disabled="page <= 1 || loading" @click="changePage(-1)">上一页</el-button><span>第 {{ page }} 页 · {{ rows.length }} 条</span><el-button size="small" :disabled="!hasMore || loading" @click="changePage(1)">下一页</el-button></div></div>
          <el-table v-loading="loading" :data="rows" row-key="run_id" empty-text="所选窗口没有运行记录" @row-click="openRun">
            <el-table-column prop="created_at" label="创建时间" min-width="150"><template #default="scope">{{ formatDate(scope.row.created_at) }}</template></el-table-column>
            <el-table-column prop="run_id" label="Run ID" min-width="180"><template #default="scope"><button class="run-copy" :title="`复制完整运行编号 ${scope.row.run_id}`" @click.stop="copyRunId(scope.row.run_id)"><code>{{ scope.row.run_id.slice(0,8) }}…</code></button></template></el-table-column>
            <el-table-column prop="orchestrator" label="执行器" width="108"><template #default="scope">{{ scope.row.orchestrator }}</template></el-table-column>
            <el-table-column prop="strategy" label="路由" width="100"><template #default="scope">{{ scope.row.strategy || '不可统计' }}</template></el-table-column>
            <el-table-column prop="status" label="状态" width="145"><template #default="scope"><span class="run-status" :style="{color:statusColor(scope.row.status)}">{{ statusLabel(scope.row.status) }}</span></template></el-table-column>
            <el-table-column prop="model_id" label="存档模型 · 非调用证明" min-width="170"><template #default="scope">{{ scope.row.model_id || '未记录' }}</template></el-table-column>
            <el-table-column prop="latency_ms" label="执行耗时" width="115"><template #default="scope"><span :title="scope.row.latency_semantics">{{ formatRunDuration(scope.row.latency_ms) }}</span><small class="latency-sub">墙钟 {{ formatRunDuration(scope.row.wall_duration_ms) }}</small></template></el-table-column>
            <el-table-column prop="error_code" label="错误码" min-width="130"><template #default="scope"><span class="error-cell">{{ scope.row.error_code || '—' }}</span></template></el-table-column>
            <el-table-column label="下钻" width="80"><template #default="scope"><el-button link type="primary" @click.stop="openRun(scope.row)">详情</el-button></template></el-table-column>
          </el-table>
          <div class="table-footnote">当前Run列表由服务端分页；单次页大小25。点击行查看受限脱敏详情。</div>
        </section>
      </template>
      <section v-else-if="authState === 'ERROR'" class="admin-gate"><el-result icon="warning" title="监控数据暂不可用" :sub-title="error || '请检查服务状态后重试。'"><template #extra><el-button type="primary" @click="loadDashboard">重试</el-button></template></el-result></section>
    </section>

    <el-drawer v-model="detailVisible" :title="`Run 详情 · ${selectedRunId}`" size="min(860px, 94vw)" destroy-on-close @closed="closeDetail">
      <div v-loading="detailLoading" class="detail-drawer">
        <template v-if="selectedRun">
          <div class="detail-summary"><span class="run-status" :style="{color:statusColor(selectedRun.run.status)}">{{ statusLabel(selectedRun.run.status) }}</span><span>{{ selectedRun.orchestrator }} · {{ selectedRun.orchestrator_evidence }}</span><span>路由 {{ selectedRun.run.strategy || '不可统计' }}</span><span>执行耗时 {{ formatRunDuration(selectedRun.run.latency_ms) }}</span><span>Run墙钟 {{ formatRunDuration(selectedRun.run.wall_duration_ms) }}</span><span>模型 {{ selectedRun.run.model_id || '不可统计' }}</span></div>
          <div class="detail-section"><h3>Run元数据</h3><dl><dt>Run ID</dt><dd>{{ selectedRun.run.run_id }}</dd><dt>创建</dt><dd>{{ formatDate(selectedRun.run.created_at) }}</dd><dt>开始 / 结束</dt><dd>{{ formatDate(selectedRun.run.started_at) }} / {{ formatDate(selectedRun.run.finished_at) }}</dd><dt>Graph状态</dt><dd>{{ selectedRun.graph_execution?.status || '无GraphExecution记录' }}</dd><dt>错误码</dt><dd>{{ selectedRun.run.error_code || selectedRun.graph_execution?.error_code || '—' }}</dd><dt>Provider usage</dt><dd>{{ selectedRun.run.usage.provider_usage_present === true ? `${selectedRun.run.usage.prompt_tokens ?? '—'} prompt / ${selectedRun.run.usage.completion_tokens ?? '—'} completion` : '不可统计 / Stub或无usage' }}</dd></dl></div>
          <div class="detail-section"><h3>Span时间线 <small>{{ selectedRun.spans.items.length }}条</small></h3><div v-if="selectedRun.spans.items.length" class="detail-timeline"><div v-for="(span,index) in selectedRun.spans.items" :key="span.span_id || `${span.node_name}-${index}`" class="detail-row"><i :class="{bad:span.error_code || span.status === 'FAILED'}"></i><div><strong>{{ span.node_name || '未知节点' }}</strong><small>{{ span.strategy || '无策略记录' }} · {{ statusLabel(span.status || 'UNAVAILABLE') }} · {{ detailDuration(span) }} · {{ span.error_code || '无错误码' }}</small><small>Span {{ span.span_id || '—' }} · Parent {{ span.parent_span_id || '—' }}</small></div></div></div><p v-else class="empty-note">该Run没有可展示Span；不推断未记录的层级。</p><small v-if="selectedRun.spans.has_more" class="muted-note">Span超过服务端返回上限，列表已截断。</small></div>
          <div class="detail-section"><h3>SSE事件 <small>{{ selectedRun.events.items.length }}条</small></h3><div class="event-list"><div v-for="event in selectedRun.events.items" :key="event.event_id"><time>#{{ event.seq }} · {{ formatDate(event.timestamp) }}</time><strong>{{ eventText(event.type) }}</strong><small>{{ eventSummary(event.data) || '无可展示的白名单元数据' }}</small></div></div><small v-if="selectedRun.events.has_more" class="muted-note">事件超过服务端返回上限，列表已截断。</small></div>
          <div class="detail-section"><h3>检索证据下钻 <small>{{ selectedRun.retrieval_hops.items.length }}个同Run检索Hop</small></h3><div v-if="selectedRun.retrieval_hops.items.length" class="retrieval-hop-list"><details v-for="(hop,index) in selectedRun.retrieval_hops.items" :key="`${hop.run_id}-${hop.hop ?? index}`"><summary>Hop {{ hop.hop ?? index + 1 }} · {{ hop.aspect_id || '方面未记录' }} · {{ retrievalStrategyLabel(hop.canonical_executed_strategy) }}<el-tag v-if="hop.degraded" size="small" type="warning">已降级</el-tag></summary><dl><dt>请求策略</dt><dd>raw: {{ hop.requested_strategy || '未记录' }} · 规范: {{ retrievalStrategyLabel(hop.canonical_requested_strategy) }}<span v-if="hop.requested_strategy_truncated"> · raw已截断</span></dd><dt>实际策略</dt><dd>raw: {{ hop.executed_strategy || '未记录' }} · 规范: {{ retrievalStrategyLabel(hop.canonical_executed_strategy) }}<span v-if="hop.executed_strategy_truncated"> · raw已截断</span></dd><dt>知识版本</dt><dd>{{ hop.knowledge_version || '未记录' }}</dd><dt>证据数量</dt><dd>{{ hop.evidence_count ?? '未记录' }}</dd><dt>检索耗时</dt><dd>{{ formatMetric(hop.retrieval_latency_ms, ' ms') }} · {{ hop.latency_source_field || '无来源字段' }}</dd><dt>降级原因</dt><dd>{{ hop.degraded_reason || '—' }}</dd><dt>引用ID</dt><dd><code v-for="id in hop.evidence_ids" :key="id">{{ id }} </code><span v-if="!hop.evidence_ids.length">未记录</span><span v-if="hop.evidence_ids_truncated || hop.evidence_id_values_truncated"> · 已截断</span></dd><dt>缺失字段</dt><dd>{{ hop.missing_fields.length ? hop.missing_fields.join(', ') : '无' }}</dd></dl></details><small class="muted-note">引用数量及ID仅表示该Run保存的检索记录，不代表答案正确性。</small><small v-if="selectedRun.retrieval_hops.has_more" class="muted-note">Hop超过服务端上限20条，已截断。</small></div><p v-else class="empty-note">该Run没有持久化检索Hop（例如DIRECT/REFUSE）；未从聚合指标推测证据。</p><div v-if="selectedRun.trace_metadata.length" class="metadata-list"><div v-for="(entry,index) in selectedRun.trace_metadata" :key="`${entry.event_type}-${index}`"><b>{{ entry.event_type }}</b><small>{{ evidenceSummary(entry.data) || '无可展示元数据' }}</small></div></div></div>
          <div class="detail-section"><h3>业务审计摘要</h3><div v-if="selectedRun.audit_metadata.length" class="audit-list"><div v-for="audit in selectedRun.audit_metadata" :key="`${audit.action_id}-${audit.event_type}-${audit.occurred_at}`"><b>{{ audit.event_type }}</b><small>{{ audit.from_status || '—' }} → {{ audit.to_status || '—' }} · {{ formatDate(audit.occurred_at) }} · {{ audit.error_code || '无错误码' }}</small></div></div><p v-else class="empty-note">该Run没有关联的Action审计事件。</p></div>
        </template>
      </div>
    </el-drawer>
  </main>
</template>

<style scoped>
.admin-shell{min-height:100vh;background:#f5f7fb;color:#243047}.admin-topbar{height:64px;background:#fff;border-bottom:1px solid #e7ebf2;display:flex;justify-content:space-between;align-items:center;padding:0 clamp(18px,4vw,58px);position:sticky;top:0;z-index:5}.admin-brand{display:flex;align-items:center;gap:11px}.admin-mark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:#596bde;color:#fff;font-weight:700}.admin-brand strong,.admin-brand small{display:block}.admin-brand strong{font-size:14px}.admin-brand small{font-size:10px;color:#8c96aa;margin-top:3px}.admin-content{max-width:1440px;margin:0 auto;padding:30px clamp(16px,3vw,40px) 48px}.page-heading,.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:15px}.page-heading{margin:4px 0 20px}.page-heading h1{font-size:25px;margin:3px 0 7px}.page-heading p:last-child{font-size:12px;color:#818ca0;margin:0}.admin-eyebrow{font-size:9px;letter-spacing:1.5px;color:#6574d4;margin:0}.filters-card,.panel-card{background:#fff;border:1px solid #e8ecf3;border-radius:13px;box-shadow:0 3px 12px #29395c05}.filters-card{padding:15px 18px;margin-bottom:17px}.filter-line{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.filter-line+.filter-line{margin-top:13px}.filter-label{font-size:11px;color:#738096;font-weight:600}.custom-range{font-size:11px;color:#6c7890}.custom-range input{border:1px solid #dce2ec;border-radius:6px;padding:6px;margin-left:5px;color:#36445d}.custom-range small,.window-caption{font-size:10px;color:#98a2b4}.filters-grid{display:grid;grid-template-columns:repeat(5,minmax(125px,1fr));align-items:end}.filters-grid label{display:grid;gap:5px;font-size:10px;color:#7a859a}.filters-grid .el-select,.filters-grid .el-input{width:100%}.window-caption{border-top:1px solid #edf0f5;margin-top:13px;padding-top:10px}.admin-alert{margin-bottom:14px}.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(145px,1fr));gap:11px;margin-bottom:14px}.kpi-card{background:#fff;border:1px solid #e8ecf3;border-radius:12px;padding:14px 15px;min-height:90px;display:flex;flex-direction:column;gap:6px}.kpi-card>span{font-size:10px;color:#7a869b}.kpi-card strong{font-size:21px;line-height:1.1;color:#303d55;font-weight:650}.kpi-card strong em{font-style:normal;font-size:14px;color:#8792a5}.kpi-card small{font-size:9px;color:#a0a9b8;line-height:1.4}.warning-kpi strong{color:#7959ba}.analysis-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px;margin-bottom:15px}.panel-card{padding:17px}.panel-heading{margin-bottom:15px}.panel-heading h2{font-size:13px;margin:0 0 5px;font-weight:650}.panel-heading small{font-size:9px;color:#99a3b4;line-height:1.5}.status-list,.rank-list{display:grid;gap:10px}.status-item{display:grid;grid-template-columns:8px 1fr auto 42px;gap:9px;align-items:center;font-size:10px;color:#68758a}.status-dot{width:8px;height:8px;border-radius:50%}.status-item b,.rank-list b{color:#3e4a61}.status-item small{text-align:right;color:#98a2b2}.trend-bars{height:140px;display:flex;align-items:stretch;justify-content:space-around;gap:8px;border-bottom:1px solid #ebeff5;padding:0 7px}.trend-column{min-width:25px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:4px;color:#8792a5}.trend-column b{font-size:9px;color:#68758b}.trend-column small{font-size:8px}.trend-bar-area{height:95px;width:18px;display:flex;align-items:flex-end}.trend-bar{display:block;width:100%;min-height:3px;border-radius:4px 4px 0 0;background:linear-gradient(180deg,#7b89ed,#a9b3f4)}.metric-pair{display:grid;grid-template-columns:1fr 1fr;background:#f7f8fc;border-radius:8px;padding:10px;margin-bottom:12px}.metric-pair>div{display:grid;gap:4px}.metric-pair span{font-size:9px;color:#929caf}.metric-pair b{font-size:17px;color:#3b4962}.rank-list>div{display:flex;justify-content:space-between;gap:8px;font-size:10px;color:#758197}.muted-note{display:block;font-size:9px;line-height:1.6;color:#9aa4b5;margin-top:10px}.model-meta{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0;color:#8590a3;font-size:9px}.model-meta span{background:#f7f8fb;border-radius:6px;padding:5px}.model-meta b{color:#4b5870}.retrieval-columns{display:grid;grid-template-columns:1fr 1fr;gap:10px}.retrieval-columns>div{background:#f8f9fc;border-radius:8px;padding:9px}.retrieval-columns strong{font-size:9px;color:#7f8a9c}.retrieval-columns p{display:flex;justify-content:space-between;font-size:9px;color:#68758a}.retrieval-columns small{font-size:9px;color:#a1aabb}.retrieval-foot{font-size:9px;color:#7c8799;line-height:1.7;margin-top:10px}.empty-note{font-size:10px;color:#9ca6b6;line-height:1.7;padding:10px 0}.runs-panel{padding:17px}.page-controls{display:flex;align-items:center;gap:7px;white-space:nowrap}.page-controls span{font-size:9px;color:#8792a5}.table-footnote{font-size:9px;color:#9ca6b5;padding-top:11px}.el-table{font-size:10px}.el-table code{font-size:9px}.admin-loading,.admin-gate{min-height:300px;display:flex;align-items:center;justify-content:center;color:#8792a5}.admin-loading{gap:10px;font-size:12px}.detail-drawer{min-height:200px}.detail-summary{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:0 0 15px;border-bottom:1px solid #edf0f5;font-size:10px;color:#68758a}.detail-section{padding:15px 0;border-bottom:1px solid #edf0f5}.detail-section h3{font-size:12px;margin:0 0 12px}.detail-section h3 small{color:#98a2b2;font-size:9px;font-weight:400}.detail-section dl{display:grid;grid-template-columns:130px 1fr;gap:8px;margin:0;font-size:10px}.detail-section dt{color:#909bad}.detail-section dd{margin:0;color:#4c5970;overflow-wrap:anywhere}.detail-timeline,.event-list,.metadata-list,.audit-list{display:grid;gap:9px}.detail-row,.event-list>div,.metadata-list>div,.audit-list>div{display:grid;grid-template-columns:9px 1fr;gap:9px;align-items:start;font-size:10px}.detail-row>i{width:7px;height:7px;background:#40ae8a;border-radius:50%;margin-top:4px}.detail-row>i.bad{background:#dc5b55}.detail-row strong,.event-list strong,.metadata-list b,.audit-list b{display:block;color:#4b5870;font-size:10px}.detail-row small,.event-list small,.event-list time,.metadata-list small,.audit-list small{display:block;color:#8e99ab;line-height:1.55;margin-top:3px;overflow-wrap:anywhere}.event-list>div{grid-template-columns:130px 1fr}.event-list time{grid-row:span 2;margin:0;font-size:9px}.metadata-list>div,.audit-list>div{grid-template-columns:1fr}.admin-gate{padding:24px}@media(max-width:1000px){.kpi-grid{grid-template-columns:repeat(3,minmax(140px,1fr))}.filters-grid{grid-template-columns:repeat(3,minmax(125px,1fr))}}@media(max-width:700px){.admin-content{padding:20px 12px 32px}.page-heading{align-items:flex-start}.page-heading h1{font-size:21px}.page-heading p:last-child{max-width:75vw;line-height:1.6}.filters-card{padding:13px}.filters-grid{grid-template-columns:repeat(2,minmax(120px,1fr))}.analysis-grid{grid-template-columns:1fr}.kpi-grid{grid-template-columns:repeat(2,minmax(135px,1fr));gap:8px}.kpi-card{padding:11px;min-height:82px}.kpi-card strong{font-size:18px}.runs-panel{padding:10px}.panel-heading{align-items:flex-start;flex-direction:column}.page-controls{align-self:flex-end}.custom-range input{max-width:150px}.detail-section dl{grid-template-columns:95px 1fr}}@media(max-width:420px){.kpi-grid{grid-template-columns:1fr 1fr}.admin-actions{gap:0}.admin-actions .el-button{padding:8px}.detail-summary{gap:6px}.filters-grid{grid-template-columns:1fr 1fr}}
 .latency-sub{display:block;color:#97a1b2;font-size:9px;margin-top:3px}.raw-strategy-details{margin-top:9px;color:#7c8799;font-size:9px}.raw-strategy-details summary,.retrieval-hop-list summary{cursor:pointer}.retrieval-hop-list{display:grid;gap:10px}.retrieval-hop-list details{background:#f8f9fc;border-radius:8px;padding:10px}.retrieval-hop-list summary{display:flex;align-items:center;gap:8px;color:#4c5970;font-size:10px}.retrieval-hop-list dl{margin-top:10px!important}.retrieval-hop-list code{display:inline-block;margin:2px 4px 2px 0;overflow-wrap:anywhere}

/* Match the current customer workspace: quiet surfaces, teal only on data. */
.admin-mark{background:#246b60}.admin-eyebrow{color:#586962}.admin-shell{font-family:"Microsoft YaHei",sans-serif;background:#f5f7f7;color:#243331}
.admin-content{max-width:1440px;margin:auto}.admin-eyebrow{letter-spacing:normal}
.kpi-grid{grid-template-columns:repeat(5,minmax(0,1fr));gap:0;border:1px solid #e0e6e5;border-radius:8px;background:white}
.kpi-card{border:0;border-radius:0;box-shadow:none;border-right:1px solid #e0e6e5;min-height:94px}
.kpi-card:last-child{border-right:0}.kpi-card strong{font-size:24px;color:#233c37}.kpi-card span{font-size:14px}
.kpi-card small,.scope-note,.filter-scope,.muted-note,.panel-heading small{font-size:12px;color:#586962;line-height:1.6}
.engineering-details{margin:16px 0}.engineering-details>summary{cursor:pointer;font-size:14px;padding:10px 0}.engineering-details .kpi-grid{grid-template-columns:repeat(4,minmax(0,1fr))}
.analysis-grid{align-items:start}.panel-card{box-shadow:none;min-height:0;border:1px solid #e0e6e5}.panel-heading h2{font-size:16px}.empty-note{min-height:48px;font-size:13px}
.trend-bar-area{border-bottom:1px solid #d6dfdc}.trend-bar{background:#246b60;border-radius:3px 3px 0 0;min-height:0}.trend-column small{font-size:12px}
.detail-section,.detail-row,.detail-summary,.event-list>div,.metadata-list>div,.audit-list>div{font-size:13px}.detail-section h3,.detail-row strong,.event-list strong,.metadata-list b,.audit-list b{font-size:14px}.detail-section h3 small,.event-list time,.latency-sub,.raw-strategy-details{font-size:12px}
.detail-row small,.event-list small,.metadata-list small,.audit-list small{font-size:12px}.error-cell,.detail-summary code{overflow-wrap:anywhere}
@media(max-width:1000px){.kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.engineering-details .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi-card:last-child{grid-column:1/-1}.trend-column small{font-size:11px}.filter-scope{margin-bottom:12px}}
.run-copy{background:none;border:0;padding:6px;color:#246b60;cursor:pointer}.error-cell{white-space:normal;overflow-wrap:anywhere}
.runs-panel :deep(.el-table){font-size:13px}.runs-panel :deep(.el-table .cell){font-size:13px;line-height:1.6}.run-copy code{font-size:13px}.run-status{display:inline-block;font-size:13px;white-space:normal}.rank-list,.status-list,.retrieval-columns p{font-size:13px}.rank-list span,.rank-list b,.status-item span,.status-item b,.retrieval-columns strong{font-size:13px}.admin-brand small,.admin-eyebrow,.window-caption,.filter-label,.filters-grid label,.table-footnote,.page-controls span{font-size:12px}.model-meta span,.model-meta b,.retrieval-foot{font-size:12px}.detail-row small,.detail-section dt,.detail-section dd{font-size:13px}
.admin-shell{font-variant-numeric:tabular-nums}.admin-shell .status-item small,.admin-shell .metric-pair span,.admin-shell .table-footnote{font-size:12px;color:#586962}.admin-shell small,.admin-shell .latency-sub,.admin-shell .window-caption,.admin-shell .filters-grid label{color:#586962}
@media(max-width:700px){.admin-content{padding-left:16px;padding-right:16px}}
</style>
