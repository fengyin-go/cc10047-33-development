<template>
  <section class="page" data-module="snapshot_audit">
    <header class="page-head">
      <div>
        <h2>快照差异审计工作台（S13）</h2>
        <p class="page-desc">
          重复路径文件清单与完整单路径清单分开核对：可证差异交审计，证据缺失进入快照差异待核清单，
          无证据差异不按零计；同一比较只保留一条签核结论。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn ghost" type="button" data-testid="reset-s13" @click="onReset">复位 S13</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">比较状态</span>
        <strong class="stat-value">{{ statusText(comparison?.status) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">签核边界</span>
        <strong class="stat-value">{{ boundaryText(comparison?.boundary) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已证实 / 待证差异</span>
        <strong class="stat-value">{{ provenCount }} / {{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">签核次数 / 尝试次数</span>
        <strong class="stat-value">
          {{ comparison?.signoff_count ?? 0 }} / {{ comparison?.attempt_count ?? 0 }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待证行数（不按零计）</span>
        <strong class="stat-value">{{ checklistPayload?.pending_rows ?? 0 }}</strong>
      </article>
    </div>

    <div class="analysis-form">
      <button class="btn" type="button" data-testid="read-abnormal" @click="loadAbnormal">
        读取异常条目
      </button>
      <button class="btn" type="button" data-testid="build-checklist" @click="onBuildChecklist">
        生成快照差异待核清单
      </button>
      <button class="btn" type="button" data-testid="swap-p7" @click="onSwapP7">
        交换 p7 重复条目顺序
      </button>
      <label>
        签核边界
        <select v-model="boundary" data-testid="boundary-select">
          <option value="item">逐项签核（p8 先签、p7 等待补证）</option>
          <option value="snapshot">整张签核（冻结整张 S13）</option>
        </select>
      </label>
      <button class="btn primary" type="button" data-testid="open-audit" @click="onOpenAudit">
        发起审计并记录边界
      </button>
      <button class="btn" type="button" data-testid="reopen" @click="onReopen">
        补全后重开差异清单
      </button>
    </div>

    <p v-if="message" class="page-foot" data-testid="audit-message">
      <span :class="lastOk ? '' : 'error-text'">{{ message }}</span>
    </p>

    <div v-if="abnormal.length" class="audit-panel">
      <h3>异常条目读取（重复路径 / 证据缺失）</h3>
      <table class="data-table">
        <thead>
          <tr><th>原顺序</th><th>快照侧</th><th>路径</th><th>校验值</th><th>行数</th><th>异常原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in abnormal" :key="`${entry.side}-${entry.order}-${entry.checksum}`">
            <td>{{ entry.order }}</td>
            <td>{{ entry.side === 'current_snapshot' ? '当前快照' : '基线快照' }}</td>
            <td>{{ entry.path }}</td>
            <td><code>{{ entry.checksum }}</code></td>
            <td>{{ entry.rows }}</td>
            <td>
              <span v-for="reason in entry.reasons" :key="reason" class="tag tag-warn">
                {{ reasonText(reason) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="audit-panel">
      <h3>快照差异待核清单（保留原文件顺序）</h3>
      <table class="data-table" data-testid="checklist-table">
        <thead>
          <tr>
            <th>顺序</th><th>路径</th><th>差异类型</th><th>基线校验值</th><th>当前校验值</th>
            <th>证据状态</th><th>签核状态</th><th>已核/未定说明</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.key" :data-testid="`checklist-${row.key}`">
            <td>{{ row.order }}</td>
            <td>
              {{ row.path }}
              <span v-if="row.duplicate_path" class="tag tag-warn">重复路径</span>
            </td>
            <td>{{ changeTypeText(row.change_type) }}</td>
            <td>
              <code v-for="checksum in row.base_checksums" :key="checksum">{{ checksum }} </code>
              <span v-if="!row.base_checksums.length">—</span>
            </td>
            <td>
              <code v-for="checksum in row.current_checksums" :key="checksum">{{ checksum }} </code>
              <span v-if="!row.current_checksums.length">—</span>
            </td>
            <td>
              <span :class="['tag', row.evidence_status === 'proven' ? 'tag-ok' : 'tag-warn']">
                {{ row.evidence_status === 'proven' ? '可证（交审计）' : '待证（待核清单）' }}
              </span>
            </td>
            <td>{{ signoffText(row.signoff_status) }}</td>
            <td class="reason-cell">
              <span v-if="row.evidence_summary?.covered_checksums?.length">
                已覆盖：{{ row.evidence_summary.covered_checksums.join(', ') }}
              </span>
              <span v-if="row.evidence_summary?.missing_checksums?.length" class="error-text">
                缺证：{{ row.evidence_summary.missing_checksums.join(', ') }}
              </span>
              <span v-if="row.frozen_reason" class="error-text">{{ row.frozen_reason }}</span>
            </td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`sign-${row.key}`"
                @click="onSign(row.key)"
              >
                签核此条
              </button>
              <button
                v-if="row.evidence_status === 'pending'"
                class="link"
                type="button"
                :data-testid="`evidence-ok-${row.key}`"
                @click="onSupplement(row, true)"
              >
                补证
              </button>
              <button
                v-if="row.evidence_status === 'pending'"
                class="link"
                type="button"
                :data-testid="`evidence-fail-${row.key}`"
                @click="onSupplement(row, false)"
              >
                模拟补证失败
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="9" class="empty-state">暂无待核清单，请先生成快照差异待核清单</td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions whole-actions">
        <button class="btn primary" type="button" data-testid="sign-whole" @click="onSign(null)">
          整张签核
        </button>
      </div>
    </div>

    <div class="audit-panel">
      <h3>审计待办（签核结果回写）</h3>
      <table class="data-table" data-testid="audit-todo-table">
        <thead>
          <tr><th>路径</th><th>差异类型</th><th>证据状态</th><th>待办状态</th><th>签核结论</th></tr>
        </thead>
        <tbody>
          <tr v-for="todo in auditTodos" :key="todo.item_key">
            <td>{{ todo.path }}</td>
            <td>{{ changeTypeText(todo.change_type) }}</td>
            <td>{{ todo.evidence_status === 'proven' ? '可证' : '待证' }}</td>
            <td>{{ todo.signoff_status }}</td>
            <td>{{ todo.conclusion ?? '—' }}</td>
          </tr>
          <tr v-if="!auditTodos.length">
            <td colspan="5" class="empty-state">审计待办为空：可证差异在发起审计后写入</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildChecklist,
  fetchAbnormalEntries,
  fetchComparison,
  openAudit,
  reorderDuplicate,
  resetComparison,
  reopenChecklist,
  signOff,
  supplementEvidence,
} from '@/api/snapshot-audit'
import type {
  AbnormalEntry,
  ActionResponse,
  Boundary,
  ChecklistPayload,
  ChecklistRow,
  Comparison,
} from '@/api/snapshot-audit'

const P7 = 'data/p7/part-000.parquet'

const comparison = ref<Comparison | null>(null)
const checklistPayload = ref<ChecklistPayload | null>(null)
const abnormal = ref<AbnormalEntry[]>([])
const boundary = ref<Boundary>('item')
const message = ref('')
const lastOk = ref(true)

const rows = computed<ChecklistRow[]>(() => checklistPayload.value?.checklist ?? [])
const auditTodos = computed(() => comparison.value?.audit_todo ?? checklistPayload.value?.audit_todo ?? [])
const provenCount = computed(() => rows.value.filter((row) => row.evidence_status === 'proven').length)
const pendingCount = computed(() => rows.value.filter((row) => row.evidence_status === 'pending').length)

function notify(ok: boolean, text: string) {
  lastOk.value = ok
  message.value = text
}

async function refreshComparison() {
  comparison.value = await fetchComparison()
}

async function loadAbnormal() {
  try {
    const payload = await fetchAbnormalEntries()
    abnormal.value = payload.items
    notify(true, `读取到 ${payload.abnormal_count} 条异常条目（重复路径、证据缺失）`)
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '异常条目读取失败')
  }
}

async function onBuildChecklist() {
  try {
    checklistPayload.value = await buildChecklist()
    await refreshComparison()
    const payload = checklistPayload.value
    notify(
      true,
      `待核清单已生成：可证 ${payload.proven_diff_items} 条交审计，待证 ${payload.pending_diff_items} 条留待核`,
    )
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '待核清单生成失败')
  }
}

async function onSwapP7() {
  try {
    const swapped = await reorderDuplicate(P7)
    checklistPayload.value = await buildChecklist()
    await refreshComparison()
    notify(true, `p7 重复条目顺序已交换为：${swapped.current_order.join(' → ')}，差异判定保持不变`)
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '顺序交换失败')
  }
}

async function onOpenAudit() {
  try {
    const payload = await openAudit(boundary.value)
    checklistPayload.value = payload
    await refreshComparison()
    const frozenTip = payload.status === 'frozen' ? '；整张 S13 已冻结，p8 一并等待补证' : ''
    notify(true, `已发起审计，本次采用${boundaryText(boundary.value)}${frozenTip}`)
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '发起审计失败')
  }
}

async function onSign(itemKey: string | null) {
  try {
    const result: ActionResponse = await signOff(itemKey)
    checklistPayload.value = await buildChecklist().catch(() => checklistPayload.value)
    await refreshComparison()
    if (result.reused) {
      notify(false, result.message)
    } else {
      notify(result.ok, result.message)
    }
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '签核失败')
  }
}

async function onSupplement(row: ChecklistRow, succeed: boolean) {
  const missing = row.evidence_summary?.missing_checksums ?? []
  const checksum = missing[0] ?? row.current_checksums[row.current_checksums.length - 1]
  try {
    const result = await supplementEvidence(
      row.path,
      checksum,
      succeed ? '审计工作台补证：补偿提交 commit#2009' : '来源不明，补证失败',
      succeed,
    )
    notify(result.ok, result.message)
    await refreshComparison()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '补证操作失败')
  }
}

async function onReopen() {
  try {
    checklistPayload.value = await reopenChecklist()
    await refreshComparison()
    const payload = checklistPayload.value
    notify(
      true,
      `差异清单已重开：待证 ${payload.pending_diff_items} 条，签核次数 ${payload.signoff_count}（已核范围与结论保留）`,
    )
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '重开差异清单失败')
  }
}

async function onReset() {
  await resetComparison()
  checklistPayload.value = null
  abnormal.value = []
  message.value = ''
  await refreshComparison()
}

function statusText(status?: string | null) {
  const map: Record<string, string> = {
    open: '待审计',
    partially_signed: '部分签核（p7 等待补证）',
    frozen: '整张冻结',
    confirmed: '已确认',
  }
  return status ? map[status] ?? status : '待审计'
}

function boundaryText(value?: Boundary | null) {
  if (value === 'item') return '逐项签核'
  if (value === 'snapshot') return '整张签核'
  return '未选择'
}

function reasonText(reason: string) {
  const map: Record<string, string> = {
    duplicate_path: '同路径重复条目',
    missing_evidence: '证据缺失',
  }
  return map[reason] ?? reason
}

function changeTypeText(type: string) {
  const map: Record<string, string> = {
    added: '新增',
    removed: '删除',
    modified: '校验值变化',
    duplicate_conflict: '重复路径冲突',
  }
  return map[type] ?? type
}

function signoffText(status: string | null) {
  const map: Record<string, string> = {
    signed: '已签核',
    waiting: '等待补证（冻结）',
    pending: '待核',
    rejected: '补证失败（保留首份结论）',
  }
  return status ? map[status] ?? status : '未签核'
}

onMounted(refreshComparison)
</script>

<style scoped>
.audit-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 16px;
}

.audit-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}

.tag {
  display: inline-block;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
  margin-right: 4px;
}

.tag-ok {
  background: #dcfce7;
  color: #166534;
}

.tag-warn {
  background: #fef3c7;
  color: #92400e;
}

.reason-cell {
  max-width: 260px;
  font-size: 12px;
}

.whole-actions {
  margin-top: 10px;
  justify-content: flex-end;
}

code {
  font-size: 12px;
  background: #f1f5f9;
  padding: 1px 4px;
  border-radius: 4px;
}
</style>
