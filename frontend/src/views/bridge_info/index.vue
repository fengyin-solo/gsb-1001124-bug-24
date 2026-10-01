<template>
  <section class="page" data-module="bridge_info">
    <header class="page-head">
      <div>
        <h2>桥梁档案管理</h2>
        <p class="page-desc">
          档案审定按 待审定 → 已审定 → 已发布 顺序推进；批量审定先冻结版本快照，
          图面、定检清单、工程待办只能读取快照版本，审定发布后三处同事务落库。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记桥梁</button>
        <button class="btn" type="button" @click="batchPanelOpen = true">批量审定</button>
        <button class="btn" type="button" @click="exportRows">导出桥梁档案清单</button>
      </div>
    </header>

    <div v-if="batchPanelOpen" class="batch-panel">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <strong>批量审定（版本快照锁）</strong>
        <button class="link" type="button" @click="batchPanelOpen = false">收起</button>
      </div>
      <p class="muted" style="margin:6px 0;">
        先把勾选桥梁冻结成待审定快照；连接断开后凭同一批次号从待审定游标继续，重复提交不生成新档案。
      </p>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <label><span class="muted">批次号</span>
          <input v-model="batchNo" placeholder="如 B20261001-01" style="margin-left:6px;" />
        </label>
        <button class="btn" type="button" :disabled="!selected.size || !batchNo" @click="submitBatch">
          冻结 {{ selected.size }} 条为待审定快照
        </button>
        <button class="btn" type="button" :disabled="!batchNo" @click="resumeBatch">按批次号继续/查看游标</button>
        <button class="btn" type="button" :disabled="!batchView" @click="approveBatch">整批审定通过</button>
        <button class="btn primary" type="button" :disabled="!batchView" @click="publishBatch">整批发布</button>
      </div>
      <div v-if="batchView" style="margin-top:8px;">
        <span class="badge" :class="batchBadge(batchView['状态'])">{{ batchView['状态'] }}</span>
        已收录 {{ batchView['条目数'] }} 条，待审定游标 {{ batchView['游标'] }}
        <span class="muted">（{{ batchView.items.map((i: Row) => i['桥梁编号']).join('、') }}）</span>
      </div>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>桥梁编号</span>
        <input v-model="keyword" placeholder="按桥梁编号检索" />
      </label>
      <label class="filter-item">
        <span>审定状态</span>
        <select v-model="reviewStatus">
          <option value="">全部</option>
          <option v-for="s in reviewStates" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th style="width:32px;"><input type="checkbox" :checked="allSelected" @change="toggleAll" /></th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>版本/审定</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-selected': selected.has(row.id) }">
          <td><input type="checkbox" :checked="selected.has(row.id)" @change="toggleSelect(row.id)" /></td>
          <td>{{ row['桥梁编号'] ?? '—' }}</td>
          <td>{{ row['桥梁名称'] ?? '—' }}</td>
          <td>{{ row['桥型结构'] ?? '—' }}<span v-if="row['裁决信息']?.['冲突字段']?.length" class="badge abnormal" style="margin-left:4px;">冲突</span></td>
          <td>{{ row['跨径组合'] ?? '—' }}</td>
          <td>{{ row['设计荷载'] ?? '—' }}</td>
          <td>{{ row['建成年份'] ?? '—' }}</td>
          <td>{{ row['上次评定等级'] ?? '—' }}</td>
          <td>{{ row['桥梁状态'] ?? '—' }}</td>
          <td>
            v{{ row['版本号'] }}
            <span class="badge" :class="stateBadge(row['审定状态'])">{{ row['审定状态'] }}</span>
            <span v-if="row['异常']" class="badge abnormal">异常</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEvidence(row)">证据链</button>
            <button class="link" type="button" @click="openSubmit(row)" :disabled="row['审定状态'] === '已审定'">修订送审</button>
            <button class="link" type="button" @click="openApprove(row)" :disabled="row['审定状态'] !== '待审定'">审定通过</button>
            <button class="link" type="button" @click="publish(row)" :disabled="row['审定状态'] !== '已审定'">发布</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无桥梁档案数据，可先登记桥梁</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁档案记录 ｜ 已选 {{ selected.size }} 条</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text">{{ okMessage }}</span>
    </footer>

    <!-- 登记桥梁 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <div class="modal-card">
        <h3 class="modal-title">登记桥梁（自动冻结 v1 待审定快照）</h3>
        <p class="modal-sub">同一桥梁编号重复提交不会生成新档案。</p>
        <div class="form-grid">
          <label v-for="field in createFields" :key="field">
            <span>{{ field }}</span>
            <input v-model="createForm[field]" />
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="button" @click="submitCreate">登记并冻结</button>
        </div>
      </div>
    </div>

    <!-- 修订送审 -->
    <div v-if="submitOpen" class="modal-mask" @click.self="submitOpen = false">
      <div class="modal-card">
        <h3 class="modal-title">修订送审 · {{ submitForm['桥梁编号'] }}</h3>
        <p class="modal-sub">仅已发布版本可修订送审，会开出新的待审定快照版本；基线版本不符将按并发冲突拒绝。</p>
        <div class="form-grid">
          <label v-for="field in editableFields" :key="field">
            <span>{{ field }}</span>
            <input v-model="submitForm[field]" />
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="submitOpen = false">取消</button>
          <button class="btn primary" type="button" @click="submitReview">冻结待审定快照</button>
        </div>
      </div>
    </div>

    <!-- 审定结论 -->
    <div v-if="approveOpen" class="modal-mask" @click.self="approveOpen = false">
      <div class="modal-card">
        <h3 class="modal-title">审定通过 · {{ approveForm['桥梁编号'] }} v{{ approveForm['版本号'] }}</h3>
        <p class="modal-sub">结论将在同一事务回写图面、定检清单与工程待办（当前为待发布，发布后对外生效）。</p>
        <div class="form-grid">
          <label><span>审定人</span><input v-model="approveForm['审定人']" /></label>
          <label><span>评定等级（历史按快照留存）</span><input v-model="approveForm['上次评定等级']" /></label>
          <label><span>桥型结构（冲突按最新批准记录裁决）</span><input v-model="approveForm['桥型结构']" /></label>
          <label><span>设计荷载</span><input v-model="approveForm['设计荷载']" /></label>
          <label style="grid-column:1/3;"><span>审定意见</span><textarea v-model="approveForm['意见']" rows="2"></textarea></label>
          <label style="grid-column:1/3;"><input type="checkbox" v-model="approveForm['异常']" /> 结论异常（发布后工程待办为“待办”）</label>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="approveOpen = false">取消</button>
          <button class="btn primary" type="button" @click="approveReview">审定通过并回写</button>
        </div>
      </div>
    </div>

    <!-- 证据链复核抽屉 -->
    <div v-if="evidenceOpen" class="modal-mask" @click.self="evidenceOpen = false">
      <div class="modal-card wide">
        <EvidenceChain :data="evidence" />
        <div class="modal-foot">
          <button class="btn primary" type="button" @click="evidenceOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { readError, request } from '@/api/client'
import EvidenceChain from '@/components/EvidenceChain.vue'

type Row = Record<string, any>

const ENDPOINT = '/api/bridge_info'
const columns = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]
const reviewStates = ["待审定", "已审定", "已发布"]
const createFields = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级"]
const editableFields = ["桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const okMessage = ref('')
const keyword = ref('')
const reviewStatus = ref('')
const selected = ref<Set<number>>(new Set())

const createOpen = ref(false)
const submitOpen = ref(false)
const approveOpen = ref(false)
const evidenceOpen = ref(false)
const batchPanelOpen = ref(false)
const createForm = ref<Row>({})
const submitForm = ref<Row>({})
const approveForm = ref<Row>({})
const evidence = ref<Row | null>(null)

const batchNo = ref(`B${new Date().toISOString().slice(0, 10).replace(/-/g, '')}-01`)
const batchView = ref<Row | null>(null)

const allSelected = computed(() => rows.value.length > 0 && rows.value.every(r => selected.value.has(r.id)))

function stateBadge(state: string): string {
  if (state === '待审定') return 'pending'
  if (state === '已审定') return 'approved'
  return 'published'
}
function batchBadge(state: string): string {
  return stateBadge(state)
}

function flashOk(message: string) {
  okMessage.value = message
  errorMessage.value = ''
}
function flashErr(message: string) {
  errorMessage.value = message
  okMessage.value = ''
}

function resetFilters() {
  keyword.value = ''
  reviewStatus.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function toggleSelect(id: number) {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
  selected.value = new Set(selected.value)
}

function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selected.value = new Set(checked ? rows.value.map(r => r.id) : [])
}

async function reload() {
  flashOk('')
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (reviewStatus.value) params.set('review_status', reviewStatus.value)
  params.set('size', '200')
  try {
    const response = await request(`${ENDPOINT}?${params}`)
    if (!response.ok) throw new Error(await readError(response, '桥梁档案列表读取失败'))
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    selected.value = new Set([...selected.value].filter(id => rows.value.some(r => r.id === id)))
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '桥梁档案列表读取失败')
  }
}

// -- 登记 ------------------------------------------------------------------
function openCreate() {
  createForm.value = {}
  createOpen.value = true
}

async function submitCreate() {
  try {
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createForm.value }) })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    createOpen.value = false
    flashOk(payload.message)
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '桥梁登记失败')
  }
}

// -- 修订送审 ---------------------------------------------------------------
function openSubmit(row: Row) {
  submitForm.value = { ...row, expected_version: row['版本号'] }
  submitOpen.value = true
}

async function submitReview() {
  const id = submitForm.value.id
  const values: Row = { expected_version: submitForm.value['expected_version'] }
  for (const field of editableFields) {
    if (submitForm.value[field] != null && submitForm.value[field] !== '') values[field] = submitForm.value[field]
  }
  try {
    const response = await request(`${ENDPOINT}/${id}/review`, { method: 'POST', body: JSON.stringify({ values }) })
    if (!response.ok) throw new Error(await readError(response, '送审失败'))
    submitOpen.value = false
    flashOk('待审定快照已冻结')
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '送审失败')
  }
}

// -- 审定通过 ---------------------------------------------------------------
function openApprove(row: Row) {
  approveForm.value = {
    ...row,
    expected_version: row['版本号'],
    审定人: '值班管理员',
    异常: Boolean(row['异常']),
    意见: '',
  }
  approveOpen.value = true
}

async function approveReview() {
  const id = approveForm.value.id
  const values: Row = {
    expected_version: approveForm.value['expected_version'],
    审定人: approveForm.value['审定人'] || '值班管理员',
    意见: approveForm.value['意见'] || '',
    异常: approveForm.value['异常'],
    桥型结构: approveForm.value['桥型结构'],
    设计荷载: approveForm.value['设计荷载'],
    上次评定等级: approveForm.value['上次评定等级'],
  }
  try {
    const response = await request(`${ENDPOINT}/${id}/approve`, { method: 'POST', body: JSON.stringify({ values }) })
    if (!response.ok) throw new Error(await readError(response, '审定未生效'))
    approveOpen.value = false
    flashOk('审定结论已回写图面、定检清单与工程待办（待发布）')
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '审定未生效')
  }
}

// -- 发布 -------------------------------------------------------------------
async function publish(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/publish`, {
      method: 'POST',
      body: JSON.stringify({ values: { expected_version: row['版本号'] } }),
    })
    if (!response.ok) throw new Error(await readError(response, '发布未生效'))
    flashOk('档案已发布，图面、定检清单与工程待办同事务翻牌')
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '发布未生效')
  }
}

// -- 证据链 -----------------------------------------------------------------
async function openEvidence(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/evidence`)
    if (!response.ok) throw new Error(await readError(response, '证据链读取失败'))
    evidence.value = await response.json()
    evidenceOpen.value = true
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '证据链读取失败')
  }
}

// -- 批量审定 ---------------------------------------------------------------
async function submitBatch() {
  const items = rows.value
    .filter(r => selected.value.has(r.id))
    .map(r => ({ 档案id: r.id, expected_version: r['版本号'] }))
  try {
    const response = await request(`${ENDPOINT}/review/batches`, {
      method: 'POST',
      body: JSON.stringify({ batch_no: batchNo.value, items }),
    })
    if (!response.ok) throw new Error(await readError(response, '批量快照冻结失败'))
    const payload = await response.json()
    batchView.value = payload.entry
    flashOk(payload.message)
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '批量快照冻结失败')
  }
}

async function resumeBatch() {
  try {
    const response = await request(`${ENDPOINT}/review/batches/${encodeURIComponent(batchNo.value)}`)
    if (!response.ok) throw new Error(await readError(response, '批次读取失败'))
    const view = await response.json()
    batchView.value = view
    flashOk(`已从待审定游标 ${view['游标']} 继续`)
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '批次读取失败')
  }
}

async function approveBatch() {
  try {
    const response = await request(`${ENDPOINT}/review/batches/${encodeURIComponent(batchNo.value)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ values: { 审定人: '批量审定组' } }),
    })
    if (!response.ok) throw new Error(await readError(response, '整批审定失败'))
    const payload = await response.json()
    batchView.value = payload.entry
    flashOk(payload.message)
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '整批审定失败')
  }
}

async function publishBatch() {
  try {
    const response = await request(`${ENDPOINT}/review/batches/${encodeURIComponent(batchNo.value)}/publish`, {
      method: 'POST',
    })
    if (!response.ok) throw new Error(await readError(response, '整批发布失败'))
    const payload = await response.json()
    batchView.value = payload.entry
    flashOk(payload.message)
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '整批发布失败')
  }
}

onMounted(reload)
</script>
