<script setup lang="ts">
import type { MemoryStatus } from '../api'
defineProps<{ memory: MemoryStatus | null; loading: boolean; clearing: boolean }>()
const emit = defineEmits<{ clear: [] }>()
const percent = (value: number) => `${Math.round(value * 100)}%`
</script>

<template>
  <div class="workbench-section">
    <div v-if="loading" class="workbench-empty">正在读取会话记忆状态…</div>
    <template v-else-if="memory">
      <div class="workbench-meta"><span>记忆状态 <b>{{ memory.status }}</b></span><span>摘要版本 <b>{{ memory.summary_version ?? '—' }}</b></span></div>
      <p v-if="memory.status === 'NOT_CREATED'" class="workbench-empty"><strong>尚未创建会话摘要</strong><br>当前直接使用最近对话。对话较长时，这里会显示已保存的摘要状态。</p>
      <details class="memory-technical"><summary>上下文估算与技术字段</summary>
      <div v-if="memory.load_ratio != null" class="memory-meter">
        <div class="memory-meter-head"><strong>最近一次上下文级别</strong><el-tag size="small" effect="plain">{{ memory.level }}</el-tag></div>
        <div class="memory-track"><i :style="{ width: percent(Math.max(0, Math.min(1, memory.load_ratio))) }" /></div>
        <div class="memory-thresholds"><span>SOFT {{ percent(memory.thresholds.soft) }}</span><span>HARD {{ percent(memory.thresholds.hard) }}</span><span>AUTO {{ percent(memory.thresholds.auto_compact) }}</span></div>
      </div>
      <dl class="memory-facts"><div><dt>摘要已持久化</dt><dd>{{ memory.summary_persisted ? '是' : '否' }}</dd></div>
        <div><dt>摘要来源消息</dt><dd>{{ memory.source_message_count }}</dd></div>
        <div><dt>来源水位</dt><dd>{{ memory.source_through_sequence }}</dd></div>
        <div><dt>最近消息保留</dt><dd>{{ memory.recent_message_count }} 条</dd></div>
        <div><dt>估算压缩前</dt><dd>{{ memory.estimated_tokens_before ?? '—' }} tokens</dd></div>
        <div><dt>估算压缩后</dt><dd>{{ memory.estimated_tokens_after ?? '—' }} tokens</dd></div>
        <div><dt>估算压缩比例</dt><dd>{{ memory.compression_ratio == null ? '—' : percent(memory.compression_ratio) }}</dd></div>
        <div><dt>估算上下文占用</dt><dd>{{ memory.load_ratio == null ? '—' : percent(memory.load_ratio) }}</dd></div>
        <div v-if="memory.summary_created_at"><dt>摘要保存时间</dt><dd>{{ memory.summary_created_at }}</dd></div>
      </dl>
      </details>
      <details class="workbench-footnote"><summary>关于记忆与估算</summary>Token 为启发式估算。摘要不构成身份、授权、订单或政策事实；原文不在此展示。</details>
      <el-button v-if="memory.status === 'PERSISTED'" type="danger" plain :loading="clearing" :disabled="clearing" @click="emit('clear')">清除会话记忆</el-button>
      <p v-if="memory.status === 'CLEARED'" class="workbench-empty">记忆已清除。原始审计消息仍保留，历史水位之前的摘要不会重新加载。</p>
    </template>
    <p v-else class="workbench-empty">选择一个会话后可以查看其记忆状态。</p>
  </div>
</template>
