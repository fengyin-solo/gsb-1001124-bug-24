<template>
  <section class="page" data-module="bridge_info">
    <header class="page-head">
      <div>
        <h2>桥梁档案管理</h2>
        <p class="page-desc">
          审定状态机：待审定 → 已审定 → 已发布，只能顺序推进。批量审定采用版本快照锁，
          图面、定检清单、工程待办在发布前只能读取上一版已发布数据。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记桥梁</button>
        <button class="btn" type="button" :disabled="!selected.length" @click="submitBatch">
          批量审定{{ selected.length ? `（${selected.length}）` : '' }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出桥梁档案清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>审定状态</span>
        <select v-model="auditStatus">
          <option value="">全部</option>
          <option v-for="s in auditStatuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>审定状态</th>
          <th>审定版本</th>
          <th>审定操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td><input v-model="selected" type="checkbox" :value="Number(row.id)" :disabled="row['审定状态'] !== '待审定'" /></td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['审定状态'] ?? '待审定' }}</td>
          <td>v{{ row['审定版本'] ?? 0 }}</td>
          <td class="row-actions">
            <button
              v-if="row['审定状态'] === '待审定'"
              class="link"
              type="button"
              @click="submitRow(row)"
            >
              提交审定
            </button>
            <button class="link" type="button" @click="reviewEvidence(row)">证据链复核</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无桥梁档案数据，可先登记桥梁</td>
        </tr>
      </tbody>
    </table>

    <section v-if="batch" class="batch-panel">
      <h3>批量审定批次 #{{ batch['批量审定id'] }} — {{ batch['审定状态'] }}（游标 {{ batch.cursor }}/{{ batch.总数 }}）</h3>
      <p class="page-desc">连接断开后可凭此批次继续，重复提交不会生成新档案。待审定游标：{{ batch['待审定游标'].join('、') || '无' }}</p>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="batch['审定状态'] === '已发布'" @click="approveBatch">审定通过整批</button>
        <button class="btn" type="button" :disabled="!['已审定', '已发布'].includes(batch['审定状态'])" @click="publishBatch">发布整批</button>
        <button class="btn ghost" type="button" @click="refreshBatch">刷新游标</button>
      </div>
      <ul v-if="batch.结果?.length" class="batch-results">
        <li v-for="r in batch.结果" :key="r.快照id" :class="{ 'is-error': !r.ok }">
          快照 {{ r.快照id }}：{{ r.ok ? r.message : `失败：${r.message}` }}
        </li>
      </ul>
    </section>

    <section v-if="evidence" class="evidence-panel">
      <header class="page-head">
        <div>
          <h3>证据链复核 — {{ evidence.桥梁编号 }}</h3>
          <p class="page-desc">档案明细与定检页面共用同一条证据链，异常沿同一根因复核。</p>
        </div>
        <button class="btn ghost" type="button" @click="evidence = null">关闭</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>快照</th>
            <th>版本</th>
            <th>审定状态</th>
            <th>评定等级</th>
            <th>审定结论</th>
            <th>异常</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in evidence.快照链" :key="s.快照id" :class="{ 'is-abnormal': s.异常 }">
            <td>#{{ s.快照id }}</td>
            <td>v{{ s.版本号 }}</td>
            <td>{{ s.审定状态 }}</td>
            <td>{{ s.评定等级 ?? '—' }}</td>
            <td>{{ s.审定结论 ?? '—' }}</td>
            <td>{{ s.异常 ? '桥型/荷载与最新批准记录冲突' : '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="page-desc">
        图面 {{ evidence.图面.length }} 份 · 定检记录 {{ visibleCount(evidence.定检记录) }} 条（锁定中 {{ lockedCount(evidence.定检记录) }} 条）
        · 工程待办 {{ visibleCount(evidence.工程待办) }} 项（锁定中 {{ lockedCount(evidence.工程待办) }} 项）
        · 批准记录 {{ evidence.批准记录.length }} 条
      </p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁档案记录</span>
      <span v-if="noticeMessage" class="ok-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type Batch = Record<string, any>
type Evidence = Record<string, any>

const ENDPOINT = '/api/bridge_info'
const columns = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]
const auditStatuses = ["待审定", "已审定", "已发布"]
const stats = [
  { label: "待审定档案", value: 0 },
  { label: "已审定待发布", value: 0 },
  { label: "已发布版本", value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const auditStatus = ref('')
const selected = ref<number[]>([])
const batch = ref<Batch | null>(null)
const evidence = ref<Evidence | null>(null)

async function readMessage(response: Response): Promise<string> {
  try {
    const body = await response.json()
    return typeof body.detail === 'string' ? body.detail : body.message ?? '操作未生效'
  } catch {
    return `请求失败（${response.status}）`
  }
}

function resetFilters() {
  filters.value = {}
  auditStatus.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '桥梁登记请使用“登记桥梁”表单，新档案进入待审定状态'
}

async function submitRow(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  const clientToken = `submit-${row.id}-${Date.now()}`
  const response = await request(`${ENDPOINT}/${row.id}/approvals`, {
    method: 'POST',
    body: JSON.stringify({
      client_token: clientToken,
      values: {
        桥梁名称: row['桥梁名称'],
        桥型结构: row['桥型结构'],
        跨径组合: row['跨径组合'],
        设计荷载: row['设计荷载'],
        建成年份: row['建成年份'],
        上次评定等级: row['上次评定等级'],
      },
    }),
  })
  const body = await response.json()
  if (!response.ok) {
    errorMessage.value = body.detail ?? '提交审定失败'
    return
  }
  await approveAndPublish(body.entry.id, clientToken)
}

async function approveAndPublish(snapshotId: number, submitToken: string) {
  const approveToken = `${submitToken}:approve`
  const approve = await request(`${ENDPOINT}/approvals/${snapshotId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ conclusion: '审定通过', client_token: approveToken }),
  })
  const approveBody = await approve.json()
  if (!approve.ok) {
    errorMessage.value = approveBody.detail ?? '审定未通过'
    await reload()
    return
  }
  const publish = await request(`${ENDPOINT}/approvals/${snapshotId}/publish`, {
    method: 'POST',
    body: JSON.stringify({ client_token: `${submitToken}:publish` }),
  })
  const publishBody = await publish.json()
  if (!publish.ok) {
    errorMessage.value = publishBody.detail ?? '发布失败'
  } else {
    noticeMessage.value = '审定通过并已发布，图面、定检清单、工程待办同事务落库'
  }
  await reload()
}

async function submitBatch() {
  errorMessage.value = ''
  noticeMessage.value = ''
  const batchToken = `batch-${Date.now()}`
  const response = await request(`${ENDPOINT}/approvals/batch`, {
    method: 'POST',
    body: JSON.stringify({ archive_ids: selected.value, batch_token: batchToken }),
  })
  const body = await response.json()
  if (!response.ok) {
    errorMessage.value = body.detail ?? '批量快照形成失败'
    return
  }
  batch.value = body.entry
  noticeMessage.value = '批量待审定快照已锁定，请审定后发布'
}

async function approveBatch() {
  if (!batch.value) return
  const response = await request(`${ENDPOINT}/approvals/batch/${batch.value['批量审定id']}/approve`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  const body = await response.json()
  if (!response.ok) {
    errorMessage.value = body.detail ?? '批量审定失败'
    return
  }
  batch.value = body.entry
  await reload()
}

async function publishBatch() {
  if (!batch.value) return
  const response = await request(`${ENDPOINT}/approvals/batch/${batch.value['批量审定id']}/publish`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  const body = await response.json()
  if (!response.ok) {
    errorMessage.value = body.entry ? '部分档案发布失败，请查看批次结果' : (body.detail ?? '批量发布失败')
  } else {
    noticeMessage.value = '批量审定版本已全部发布'
  }
  batch.value = body.entry
  await reload()
}

async function refreshBatch() {
  if (!batch.value) return
  const response = await request(`${ENDPOINT}/approvals/batch/${batch.value['批量审定id']}`)
  if (response.ok) {
    batch.value = await response.json()
  } else {
    errorMessage.value = await readMessage(response)
  }
}

async function reviewEvidence(row: Row) {
  errorMessage.value = ''
  const code = String(row['桥梁编号'] ?? '')
  const response = await request(`${ENDPOINT}/by-code/${encodeURIComponent(code)}/evidence`)
  if (!response.ok) {
    errorMessage.value = await readMessage(response)
    return
  }
  evidence.value = await response.json()
}

function visibleCount(items: Row[] | undefined): number {
  return (items ?? []).filter(item => !item.锁定中).length
}

function lockedCount(items: Row[] | undefined): number {
  return (items ?? []).filter(item => item.锁定中).length
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value as Record<string, string>).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  if (auditStatus.value) query.set('audit_status', auditStatus.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('桥梁档案列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    selected.value = selected.value.filter(id => rows.value.some(row => Number(row.id) === id))
    stats[0].value = rows.value.filter((r: Row) => r['审定状态'] === '待审定').length
    stats[1].value = rows.value.filter((r: Row) => r['审定状态'] === '已审定').length
    stats[2].value = rows.value.filter((r: Row) => r['审定状态'] === '已发布').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁档案列表读取失败'
  }
}

onMounted(reload)
</script>
