<script setup lang="ts">
defineProps<{ modelValue: string; canSend: boolean; disabled: boolean; sending: boolean; recovering: boolean; unknown: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; send: []; keydown: [event: KeyboardEvent] }>()
</script>

<template>
  <div class="composer-wrap">
    <div class="composer">
      <el-input :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" resize="none" maxlength="10000" placeholder="写下你想咨询的内容…" aria-label="咨询内容" :disabled="disabled" @keydown="emit('keydown', $event)" />
      <div class="composer-actions"><span>{{ unknown ? '结果未知，不会自动重放' : 'Enter 换行 · Ctrl/⌘ + Enter 发送' }}</span><el-button type="primary" :disabled="!canSend" :loading="sending" aria-label="发送消息" @click="emit('send')">发送 <span aria-hidden="true">↑</span></el-button></div>
    </div>
    <p v-if="sending || recovering" class="privacy-hint">正在核对并生成回复，请稍候。</p>
  </div>
</template>
