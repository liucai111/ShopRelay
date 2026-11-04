<script setup lang="ts">
import { computed, ref } from 'vue'
import type { RunExecution } from '../api'
import { evidenceGroups, retrievalLabel } from '../executionView'
const props = defineProps<{ execution: RunExecution | null; loading: boolean }>()
const groups = computed(() => evidenceGroups(props.execution?.citations || []))
const otherEvidence = computed(() => props.execution?.retrieved_evidence.filter(item => !props.execution?.citations.some(c => c.chunk_id === item.chunk_id)) || [])
const expanded = ref<Record<string, boolean>>({})
function toggle(id: string) { expanded.value[id] = !expanded.value[id] }
function safeText(value: unknown) { return typeof value === 'string' ? value : '' }
</script>

<template>
  <div class="workbench-section">
    <div v-if="loading" class="workbench-empty">正在读取本次 Run 的证据记录…</div>
    <template v-else-if="execution">
      <div class="workbench-meta"><span>已记录策略 <b>{{ retrievalLabel(execution) }}</b></span>
        <span>引用 {{ execution.citations.length }} · 检索到 {{ execution.retrieved_evidence.length }}</span></div>
      <p v-if="retrievalLabel(execution) === 'MIXED'" class="evidence-note">MIXED 是服务端聚合标记，也可能出现在降级返回中；它本身不证明执行了多种检索策略。</p>
      <div v-if="execution.citations.length" class="evidence-list">
        <section v-for="group in groups" :key="`${group.title}:${group.version}`" class="source-document">
          <div class="evidence-heading"><strong>{{ group.title }}</strong><span>{{ group.clauses.length }} 条引用</span></div>
        <article v-for="citation in group.clauses" :key="citation.chunk_id" class="evidence-card">
          <code>{{ citation.chunk_id }}</code>
          <dl><div v-if="citation.source"><dt>来源</dt><dd>{{ citation.source }}</dd></div>
            <div v-if="citation.version || citation.knowledge_version"><dt>版本</dt><dd>{{ citation.version || citation.knowledge_version }}</dd></div>
            <div v-if="citation.effective_from || citation.effective_to"><dt>有效期</dt><dd>{{ citation.effective_from || '未记录' }} — {{ citation.effective_to || '持续有效' }}</dd></div>
            <div><dt>校验记录</dt><dd>{{ citation.validity_status === 'RUNTIME_CITATION_CHECKED' ? '政策执行链记录为通过' : '已保存的 Run 引用' }}</dd></div>
            <div v-if="citation.score != null"><dt>检索分数</dt><dd>{{ citation.score }}</dd></div></dl>
          <button v-if="safeText(citation.content)" class="evidence-toggle" type="button" @click="toggle(citation.chunk_id)">{{ expanded[citation.chunk_id] ? '收起条款' : '查看政策条款' }}</button>
          <p v-if="expanded[citation.chunk_id]" class="evidence-content">{{ citation.content }}</p>
          <p v-else-if="!safeText(citation.content)" class="evidence-content muted">服务端仅保存了来源标识，本页面不补写或推测原文。</p>
        </article>
        </section>
      </div>
      <p v-else class="workbench-empty">本次 Run 没有保存政策引用。业务工具事实请以该 Run 的服务端结果为准。</p>
      <section v-if="execution.tool_references.length" class="retrieved-list">
        <strong class="source-section-title">已核验业务来源</strong>
        <div v-for="reference in execution.tool_references" :key="reference.result_ref" class="retrieved-row">
          <span>{{ reference.tool || '业务工具' }} · {{ reference.source || '来源未记录' }}</span>
          <code>{{ reference.result_ref }}</code>
        </div>
        <p class="evidence-note">这些标识关联本 Run 的服务端工具结果；用户陈述仍显示为对话内容，不会升级为已核验事实。</p>
      </section>
      <details v-if="otherEvidence.length" class="retrieved-list">
        <summary>未被引用的检索结果（{{ otherEvidence.length }}）</summary>
        <div v-for="item in otherEvidence" :key="item.chunk_id" class="retrieved-row">
          <code>{{ item.chunk_id }}</code><span>{{ item.title || item.source || '政策来源' }}</span>
          <el-tag size="small" :type="item.citation_status === 'CITED' ? 'success' : 'info'" effect="plain">{{ item.citation_status === 'CITED' ? '已引用' : '未引用' }}</el-tag>
        </div>
      </details>
      <p v-if="execution.retrieval.degraded || execution.policy_retrieval.degraded" class="workbench-callout">本次记录标记为检索降级。策略、引用数量与是否生成答案是不同状态；没有引用不代表未发生检索。</p>
    </template>
    <p v-else class="workbench-empty">当前没有可读取的 Run 证据。</p>
  </div>
</template>
