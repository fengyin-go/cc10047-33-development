/** 快照差异审计接口封装：异常条目、待核清单、两种签核边界、补证与重开。 */
import { request } from './client'

export type Boundary = 'item' | 'snapshot'

export type ChecklistRow = {
  key: string
  comparison_id: string
  path: string
  change_type: string
  order: number
  duplicate_path: boolean
  base_checksums: string[]
  current_checksums: string[]
  base_rows: number
  current_rows: number
  evidence: Record<string, unknown> | null
  evidence_status: 'proven' | 'pending'
  signoff_status: 'signed' | 'waiting' | 'pending' | 'rejected' | null
  signoff_attempt: number | null
  frozen_reason: string | null
  evidence_summary?: {
    path: string
    evidence_status: string
    signoff_status: string | null
    frozen_reason: string | null
    covered_checksums: string[]
    missing_checksums: string[]
  }
}

export type AuditTodo = {
  comparison_id: string
  item_key: string
  path: string
  change_type: string
  evidence_status: string
  signoff_status: string
  conclusion: string | null
}

export type Comparison = {
  comparison_id: string
  snapshot_id: string
  boundary: Boundary | null
  status: 'open' | 'partially_signed' | 'frozen' | 'confirmed'
  signoff_count: number
  attempt_count: number
  conclusion: string | null
  conclusion_attempt: number | null
  audited_item_keys: string[]
  waiting_item_keys: string[]
  checklist: ChecklistRow[]
  audit_todo: AuditTodo[]
}

export type ChecklistPayload = {
  comparison_id: string
  boundary: Boundary | null
  total_diff_items: number
  proven_diff_items: number
  pending_diff_items: number
  pending_rows: number
  duplicate_paths: unknown[]
  checklist: ChecklistRow[]
  audit_todo: AuditTodo[]
  order_preserved: string[]
  status?: string
  signoff_count?: number
  attempt_count?: number
  conclusion?: string | null
}

export type AbnormalEntry = {
  comparison_id: string
  side: string
  order: number
  path: string
  checksum: string
  rows: number
  reasons: string[]
}

export type ActionResponse = {
  ok: boolean
  reused?: boolean
  message: string
  signoff_count?: number
  status?: string
  attempt?: number
  conclusion?: string | null
  boundary?: Boundary
  checklist?: ChecklistRow[]
  audit_todo?: AuditTodo[]
  audited_item_keys?: string[]
  waiting_item_keys?: string[]
}

const BASE = '/api/snapshot-audit/comparisons/S13'

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export function fetchAbnormalEntries(): Promise<{ items: AbnormalEntry[]; abnormal_count: number }> {
  return request(`${BASE}/abnormal-entries`).then((r) => r.json())
}

export function buildChecklist(): Promise<ChecklistPayload> {
  return postJson(`${BASE}/checklist`)
}

export function fetchComparison(): Promise<Comparison> {
  return request(BASE).then((r) => r.json())
}

export function reorderDuplicate(path: string): Promise<{ current_order: string[] }> {
  const query = new URLSearchParams({ path }).toString()
  return postJson(`${BASE}/reorder?${query}`)
}

export function openAudit(boundary: Boundary): Promise<ChecklistPayload & { status: string }> {
  return postJson(`${BASE}/audit`, { boundary })
}

export function signOff(itemKey: string | null): Promise<ActionResponse> {
  return postJson(`${BASE}/signoff`, { item_key: itemKey })
}

export function supplementEvidence(
  path: string,
  checksum: string,
  evidenceDetail: string,
  succeed: boolean,
): Promise<ActionResponse & { evidence_status?: string }> {
  return postJson(`${BASE}/evidence`, {
    path,
    checksum,
    evidence_detail: evidenceDetail,
    succeed,
  })
}

export function reopenChecklist(): Promise<ChecklistPayload> {
  return postJson(`${BASE}/reopen`)
}

export function resetComparison(): Promise<{ ok: boolean }> {
  return postJson(`${BASE}/reset`)
}
