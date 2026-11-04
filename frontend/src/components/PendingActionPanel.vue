<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { closePendingAction, confirmPendingAction } from '../api'
import type { PendingActionProposal } from '../api'

defineProps<{ actions: PendingActionProposal[]; loading: boolean; error?: string }>()
const emit = defineEmits<{ openRun: [runId: string]; refresh: [] }>()
const selected = ref<PendingActionProposal | null>(null)
const dialogOpen = computed({ get: () => Boolean(selected.value), set: (open: boolean) => { if (!open) selected.value = null } })
const confirmationOpen = ref(false)
const busyActionId = ref<string | null>(null)

const toolLabels: Record<string, string> = {
  create_return_request: '退货申请', create_refund_request: '退款申请',
  cancel_order: '取消订单', escalate_to_human: '人工客服工单',
}
const statusLabels: Record<string, string> = {
  CREATED: '已创建', PENDING_CONFIRMATION: '等待用户确认', CONFIRMED: '已确认',
  EXECUTING: '正在执行', SUCCEEDED: '执行成功', FAILED: '执行失败',
  UNKNOWN_RESULT: '结果未知', REJECTED: '已拒绝', CANCELLED: '已取消', EXPIRED: '已过期',
}
const paramLabels: Record<string, string> = {
  order_id: '订单', reason_code: '原因类型', reason_summary: '申请说明', amount_cents: '申请金额（分）',
  return_id: '关联退货申请', issue_category: '问题类别', issue_summary: '问题摘要',
  user_description: '用户描述', related_agents: '相关专业 Agent', verified_fact_refs: '已核实事实引用',
  unverified_claim_codes: '待核实陈述', evidence_references: '证据引用',
  application_outcome: '申请阶段', business_outcome: '业务阶段',
}
const details = computed(() => selected.value ? Object.entries(selected.value.parameters)
  .filter(([key]) => key in paramLabels) : [])
function statusLabel(action: PendingActionProposal) {
  if (action.expired && ['CREATED', 'PENDING_CONFIRMATION', 'CONFIRMED'].includes(action.status)) return '已过期（未重新执行）'
  return statusLabels[action.status] || action.status
}
function statusType(action: PendingActionProposal) {
  if (action.expired || ['FAILED', 'UNKNOWN_RESULT'].includes(action.status)) return 'danger'
  if (['SUCCEEDED'].includes(action.status)) return 'success'
  if (['PENDING_CONFIRMATION', 'CREATED', 'CONFIRMED', 'EXECUTING'].includes(action.status)) return 'warning'
  return 'info'
}
function valueText(value: unknown): string {
  if (Array.isArray(value)) return value.map(item => String(item)).join('、') || '无'
  if (value === null || value === undefined || value === '') return '未提供'
  return String(value)
}
function effectNote(action: PendingActionProposal) {
  if (action.expired) return '提案已超过有效期；此页面不提供续期或执行操作。'
  if (action.safe_error_code === 'ACTION_EXECUTION_DISABLED') return '当前环境未启用此操作能力；页面不会尝试执行。'
  if (action.status === 'UNKNOWN_RESULT') return '执行结果未知。请以服务端状态为准，不要根据此页面推断操作成功。'
  const notes: Record<string, string> = {
    create_return_request: '退货申请提交不等于退货获批。',
    create_refund_request: '退款申请提交不等于退款获批或到账。',
    cancel_order: '取消结果不代表已发生支付或退款处理。',
    escalate_to_human: '工单提交不等于客服已受理、分配或处理。',
  }
  const note = notes[action.tool_id] || '状态以服务端记录为准。'
  return action.status === 'SUCCEEDED' ? note : action.execution_available
    ? `${note} 此提案仅能在隔离测试数据库执行。`
    : `${note} 当前服务端执行能力未启用。`
}
function canClose(action: PendingActionProposal) {
  return action.execution_available && action.status === 'PENDING_CONFIRMATION' && !action.expired
}
function effectForConfirmation(action: PendingActionProposal) {
  const effects: Record<string, string> = {
    create_return_request: '会创建一条隔离退货申请，初始为已提交并等待审核。不会批准退货或发起退款。',
    create_refund_request: '会创建一条隔离退款申请并等待审核。不会调用支付服务，也不会产生到账记录。',
    cancel_order: '会按服务端订单状态及预留账本规则执行隔离取消事务；不会调用支付或自动退款。',
    escalate_to_human: '会创建一条隔离客服工单并等待受理。不会通知、分配或联系真实客服。',
  }
  return effects[action.tool_id] || '服务端隔离事务。'
}
async function confirmSelected() {
  const action = selected.value
  if (!action || !canClose(action) || !action.confirmation_hash || busyActionId.value) return
  busyActionId.value = action.action_id
  try {
    const answer = await confirmPendingAction(action.action_id, action.confirmation_hash)
    confirmationOpen.value = false
    dialogOpen.value = false
    ElMessage.success(answer)
    emit('refresh')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '确认失败，请先刷新操作状态。')
    emit('refresh')
  } finally { busyActionId.value = null }
}
async function closeSelected(disposition: 'reject' | 'cancel') {
  const action = selected.value
  if (!action || !canClose(action) || busyActionId.value) return
  const actionId = action.action_id
  try {
    await ElMessageBox.confirm(disposition === 'reject' ? '拒绝后，该提案不能再确认执行。' : '撤回后，该提案不能再确认执行。',
      disposition === 'reject' ? '拒绝此提案？' : '撤回此提案？', {
        confirmButtonText: disposition === 'reject' ? '拒绝提案' : '撤回提案',
        cancelButtonText: '返回', type: 'warning',
      })
  } catch { return }
  busyActionId.value = actionId
  try {
    await closePendingAction(actionId, disposition)
    dialogOpen.value = false
    ElMessage.success(disposition === 'reject' ? '提案已拒绝。' : '提案已撤回。')
    emit('refresh')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '状态更新失败，请刷新后核对。')
    emit('refresh')
  } finally { busyActionId.value = null }
}
</script>

<template>
  <section class="pending-actions" aria-label="待确认操作">
    <div v-if="loading" class="action-empty">正在读取本人会话的操作记录…</div>
    <div v-else-if="error" class="action-empty action-error">{{ error }}</div>
    <div v-else-if="actions.length === 0" class="action-empty">
      <strong>没有待确认的操作</strong>
      <p>本会话尚无服务端操作提案。你可以继续咨询；只有服务端提供可执行提案时，这里才会出现确认入口，文字建议不会自动执行。</p>
    </div>
    <article v-for="action in actions" :key="action.action_id" class="action-card">
      <header class="action-card-head">
        <div><span class="action-kicker">受控操作提案</span><h4>{{ toolLabels[action.tool_id] || action.tool_id }}</h4></div>
        <el-tag size="small" :type="statusType(action)" effect="plain">{{ statusLabel(action) }}</el-tag>
      </header>
      <p class="action-target">{{ action.parameters.order_id ? `订单 ${action.parameters.order_id}` : action.target_resource_id }}</p>
      <p class="action-explanation">{{ effectNote(action) }}</p>
      <div class="action-meta"><span>创建 {{ action.created_at }}</span><span>有效至 {{ action.expires_at }}</span></div>
      <div class="action-buttons">
        <el-button size="small" @click="selected = action">查看详情</el-button>
        <el-button size="small" :disabled="!canClose(action)" :loading="busyActionId === action.action_id" @click="selected = action; confirmationOpen = true">确认并提交</el-button>
        <el-button size="small" :disabled="!canClose(action) || busyActionId === action.action_id" @click="selected = action; closeSelected('reject')">拒绝</el-button>
        <el-button size="small" :disabled="!canClose(action) || busyActionId === action.action_id" @click="selected = action; closeSelected('cancel')">撤回</el-button>
        <el-button size="small" :disabled="loading || busyActionId === action.action_id" @click="emit('refresh')">刷新状态</el-button>
      </div>
    </article>
    <details v-if="actions.length" class="action-stage-note"><summary>操作与恢复规则</summary>仅服务端明确授权的提案可操作。结果未知时请刷新核对，不会自动重放。</details>
  </section>

  <el-dialog v-model="dialogOpen" title="操作提案详情" width="min(560px, 92vw)" destroy-on-close>
    <template v-if="selected">
      <div class="action-detail-title"><strong>{{ toolLabels[selected.tool_id] || selected.tool_id }}</strong><el-tag size="small" :type="statusType(selected)">{{ statusLabel(selected) }}</el-tag></div>
      <p class="action-disclaimer">{{ effectNote(selected) }}</p>
      <dl class="action-details">
        <div><dt>Action ID</dt><dd>{{ selected.action_id }}</dd></div>
        <div><dt>关联对象</dt><dd>{{ selected.target_resource_id }}</dd></div>
        <div><dt>风险级别</dt><dd>{{ selected.risk_level }}</dd></div>
        <div><dt>执行范围</dt><dd>{{ selected.execution_scope === 'ISOLATED_TEST_DATABASE' ? '仅隔离测试数据库（模拟数据）' : '当前不可执行' }}</dd></div>
        <div v-for="[key, value] in details" :key="key"><dt>{{ paramLabels[key] }}</dt><dd>{{ valueText(value) }}</dd></div>
        <div v-for="[key, value] in Object.entries(selected.verified_snapshot)" :key="`fact-${key}`"><dt>提案时核实 · {{ key }}</dt><dd>{{ valueText(value) }}</dd></div>
        <div v-if="selected.user_claim_markers.reason_summary"><dt>事实边界</dt><dd>申请说明来自用户陈述，尚不等同于业务系统核实事实。</dd></div>
        <div><dt>创建时间</dt><dd>{{ selected.created_at }}</dd></div>
        <div><dt>有效期</dt><dd>{{ selected.expires_at }}</dd></div>
        <div v-if="selected.safe_error_code"><dt>服务端错误</dt><dd>{{ selected.safe_error_code }}</dd></div>
        <div><dt>关联 Run</dt><dd>{{ selected.run_id }}</dd></div>
      </dl>
      <section class="action-evidence">
        <h5>政策依据</h5>
        <p v-if="selected.policy_version">知识版本 {{ selected.policy_version }} · 适用时点 {{ selected.policy_evaluation_at || '未提供' }}</p>
        <ul v-if="selected.policy_references.length"><li v-for="ref in selected.policy_references" :key="ref.chunk_id">
          {{ ref.policy_id || '政策来源' }} / {{ ref.clause_id || ref.chunk_id }} · {{ ref.version || '版本未提供' }}
        </li></ul>
        <p v-else>此提案没有附带可展示的政策条款引用。</p>
      </section>
      <section v-if="selected.result_summary" class="action-evidence">
        <h5>服务端结果摘要</h5><p v-for="(value, key) in selected.result_summary" :key="key">{{ key }}：{{ value }}</p>
      </section>
      <div v-if="canClose(selected)" class="action-dialog-actions">
        <el-button type="primary" @click="confirmationOpen = true">确认并提交</el-button>
        <el-button type="danger" plain @click="closeSelected('reject')">拒绝提案</el-button>
        <el-button @click="closeSelected('cancel')">撤回提案</el-button>
      </div>
      <p v-else-if="selected.status === 'UNKNOWN_RESULT'" class="action-unknown">结果未知。请刷新读取同一 Action 状态；系统不会重试事务。</p>
      <el-button link type="primary" @click="emit('openRun', selected.run_id)">查看关联 Run 的执行记录</el-button>
    </template>
    <template #footer><el-button @click="selected = null">关闭</el-button></template>
  </el-dialog>
  <el-dialog v-model="confirmationOpen" title="确认隔离模拟操作" width="min(520px, 92vw)" :close-on-click-modal="false">
    <template v-if="selected">
      <el-alert title="仅用于隔离测试数据" description="此确认会提交 shoprelay_m11_test 中的模拟业务事务，不会触达生产订单、支付、真实退款或外部客服。" type="warning" :closable="false" show-icon />
      <dl class="action-details confirmation-details">
        <div><dt>操作</dt><dd>{{ toolLabels[selected.tool_id] || selected.tool_id }}</dd></div>
        <div><dt>目标</dt><dd>{{ selected.target_resource_id }}</dd></div>
        <div v-for="[key, value] in details" :key="`confirm-${key}`"><dt>{{ paramLabels[key] }}</dt><dd>{{ valueText(value) }}</dd></div>
        <div><dt>业务影响</dt><dd>{{ effectForConfirmation(selected) }}</dd></div>
        <div><dt>已核实信息</dt><dd>{{ Object.keys(selected.verified_snapshot).length ? valueText(Object.entries(selected.verified_snapshot).map(([key, value]) => `${key}: ${value}`)) : '没有附加的已核实业务事实' }}</dd></div>
        <div><dt>有效期</dt><dd>{{ selected.expires_at }}</dd></div>
      </dl>
      <p class="action-disclaimer">点击下方按钮会向服务端提交明确确认。服务端将重新验证身份、属主、参数哈希、有效期和业务条件。</p>
    </template>
    <template #footer>
      <el-button @click="confirmationOpen = false">返回</el-button>
      <el-button type="primary" :loading="busyActionId === selected?.action_id" :disabled="!selected || !canClose(selected)" @click="confirmSelected">确认并提交隔离操作</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pending-actions{display:grid;gap:10px}.action-stage-note{margin:0;padding:8px 9px;border-radius:8px;background:#fff8e9;color:#96752f;font-size:12px;line-height:1.6}.action-empty{padding:16px 12px;border:1px dashed #e2e6ef;border-radius:10px;color:#8b96aa;font-size:13px;line-height:1.7}.action-empty strong{color:#5d687e}.action-empty p{margin:6px 0 0}.action-error{color:#b75b4e}.action-card{border:1px solid #e7eaf1;border-radius:10px;background:#fff;padding:11px}.action-card-head,.action-detail-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.action-kicker{font-size:12px;color:#9aa4b5}.action-card h4{margin:3px 0 0;font-size:11px}.action-target{font-size:13px;color:#58657b;margin:10px 0 5px}.action-explanation,.action-meta{font-size:12px;color:#8a95a8;line-height:1.6}.action-meta{display:grid;gap:3px;margin-top:8px;overflow-wrap:anywhere}.action-buttons{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px}.action-buttons .el-button{margin:0;font-size:12px}.action-detail-title strong{font-size:13px}.action-disclaimer{font-size:13px;line-height:1.7;color:#768198;background:#f6f7fb;padding:9px;border-radius:8px}.action-details{display:grid;gap:8px;margin:14px 0}.action-details div{display:grid;grid-template-columns:92px minmax(0,1fr);gap:8px;font-size:13px}.action-details dt{color:#98a2b2}.action-details dd{margin:0;color:#4b5870;overflow-wrap:anywhere;white-space:pre-wrap}.action-evidence{border-top:1px solid #edf0f5;padding-top:9px;margin-top:12px;font-size:12px;color:#778298}.action-evidence h5{margin:0 0 7px;font-size:13px;color:#59657a}.action-evidence p{margin:4px 0;overflow-wrap:anywhere}.action-evidence ul{padding-left:18px;margin:6px 0}.action-evidence li{margin:4px 0;overflow-wrap:anywhere}
.action-dialog-actions{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}.action-unknown{padding:8px;background:#fff4e8;border-radius:8px;color:#936a30;font-size:13px;line-height:1.6}.confirmation-details{margin-bottom:0}
</style>
