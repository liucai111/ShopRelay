<script setup lang="ts">
import { computed } from 'vue'
import type { RunEvent, RunExecution } from '../api'
import { executionPhases, durationLabel } from '../executionView'

const props = defineProps<{ execution: RunExecution | null; events: RunEvent[]; loading: boolean }>()
const phases = computed(() => props.execution ? executionPhases(props.events, props.execution) : [])
const labels: Record<string, string> = {
  started: '请求已接收', route_decided: '路由决策', agent_started: '专业 Agent 启动',
  retrieval_hop: '政策检索', tool_started: '工具调用开始', tool_finished: '工具调用结束',
  action_pending: '等待用户确认', answer: '回答已保存', done: '运行完成', error: '安全停止 / 失败',
}
const timeline = computed(() => {
  const rows = [
    ...props.events.map(event => ({ key: `event:${event.event_id}`, at: event.timestamp, seq: event.seq, kind: 'event' as const,
      title: labels[event.type] || event.type, detail: event.type === 'route_decided'
        ? String(event.payload.strategy || event.payload.route || '')
        : String(event.payload.code || event.payload.agent || event.payload.tool_id || ''), status: event.type })),
    ...(props.execution?.spans || []).map(span => ({ key: `span:${span.span_id || `${span.node_name}:${span.start_time}`}`,
      at: span.start_time || '', seq: Number.MAX_SAFE_INTEGER, kind: 'span' as const, title: span.node_name || '执行节点',
      detail: [span.strategy, span.status, span.error_code].filter(Boolean).join(' · '), status: span.status || '' })),
  ]
  return rows.sort((a, b) => a.at.localeCompare(b.at) || a.seq - b.seq || (a.kind === 'event' ? -1 : 1))
})
function displayTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}
</script>

<template>
  <div class="workbench-section">
    <div v-if="loading" class="workbench-empty">正在读取已保存的执行记录…</div>
    <template v-else-if="execution">
      <div class="workbench-meta">
        <span>策略 <b>{{ execution.strategy || '未记录' }}</b><small v-if="execution.sub_strategy"> · {{ execution.sub_strategy }}</small></span>
        <span>执行状态 <b>{{ execution.status === 'SUCCEEDED' ? '已结束' : execution.status === 'FAILED' ? '未正常完成' : execution.status }}</b></span>
      </div>
      <div class="phase-list">
        <article v-for="phase in phases" :key="phase.key" class="phase-row"><i class="phase-mark" /><div><strong>{{ phase.title }}</strong><p>{{ phase.detail }}</p><small>{{ durationLabel(phase.duration) }}<template v-if="phase.at"> · {{ displayTime(phase.at) }}</template></small></div></article>
        <p v-if="!phases.length" class="workbench-empty">尚无可归纳的阶段记录，可展开技术记录核对。</p>
      </div>
      <details class="technical-records"><summary>技术记录 · {{ timeline.length }} 条事件与节点</summary>
      <p class="technical-id">Run · {{ execution.run_id }}</p>
      <p class="technical-id">原始终态 · {{ execution.status }} · <a href="/admin/observability">打开运行监控</a></p>
      <div v-for="span in execution.spans" :key="span.span_id" class="technical-id">{{ span.node_name }} · Span {{ span.span_id || '未记录' }}<br>Trace {{ span.trace_id || '未记录' }} · {{ durationLabel(span.duration_ms ?? null) }}</div>
      <div v-if="execution.skill_id" class="skill-chip">Skill · {{ execution.skill_id }}<small v-if="execution.skill_version"> v{{ execution.skill_version }}</small></div>
      <div v-if="timeline.length" class="workbench-timeline">
        <article v-for="item in timeline" :key="item.key" class="workbench-step">
          <i :class="['step-dot', item.status.toLowerCase()]" />
          <div class="step-body"><div class="step-heading"><strong>{{ item.title }}</strong><time>{{ displayTime(item.at) }}</time></div>
            <small v-if="item.detail">{{ item.detail }}</small>
            <small>{{ item.key }}</small>
          </div>
        </article>
      </div>
      <p v-else class="workbench-empty">此 Run 没有可展示的持久化阶段事件。</p>
      <section v-if="execution.handoffs.length" class="handoff-list">
        <h4>结构化 Handoff</h4>
        <article v-for="handoff in execution.handoffs" :key="handoff.handoff_id" class="handoff-card">
          <strong>{{ handoff.source_agent || 'Supervisor' }} → {{ handoff.target_agent || 'Agent' }}</strong>
          <small>{{ handoff.status }}<template v-if="handoff.error_code"> · {{ handoff.error_code }}</template></small>
          <small v-if="handoff.required_capabilities?.length">能力：{{ handoff.required_capabilities.join('、') }}</small>
          <small v-if="handoff.evidence_refs?.length">证据引用：{{ handoff.evidence_refs.length }} 项</small>
        </article>
      </section>
      <section v-if="execution.tool_calls.length || execution.tool_references.length" class="handoff-list">
        <h4>工具与来源</h4>
        <article v-for="(tool, index) in execution.tool_calls" :key="`${tool.tool_id}:${index}`" class="handoff-card">
          <strong>{{ tool.tool_id || tool.capability || '工具' }} · {{ tool.status || '状态未记录' }}</strong>
          <small v-if="tool.duration_ms != null">耗时 {{ tool.duration_ms.toFixed(1) }} ms</small>
          <small v-if="tool.error_code">错误：{{ tool.error_code }}</small>
        </article>
        <article v-for="reference in execution.tool_references" :key="reference.result_ref" class="handoff-card">
          <strong>{{ reference.tool || '业务工具' }} · {{ reference.source || '来源未记录' }}</strong>
          <small>服务端结果引用：{{ reference.result_ref }}</small>
        </article>
        <p class="workbench-footnote">工具结果引用表示服务端记录的来源链，不展示敏感业务参数。</p>
      </section>
      <div v-if="execution.error_code" class="workbench-callout danger">安全停止：{{ execution.error_code }}</div>
      </details>
    </template>
    <p v-else class="workbench-empty">选择一个已运行的会话后，这里会显示该 Run 的真实执行步骤。</p>
  </div>
</template>
