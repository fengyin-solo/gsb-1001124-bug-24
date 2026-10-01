<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>桥梁定检管理</h2>
        <p class="page-desc">
          定检清单只能读取桥梁档案的快照版本：档案批量审定期间显示待审定快照，
          审定回写后随版本结论更新；点“证据链复核”可沿同一条链核对图面、档案与工程待办。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记检测记录</button>
        <button class="btn" type="button" @click="exportRows">导出桥梁定检清单</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>检测编号</span>
        <input v-model="keyword" placeholder="按检测编号检索" />
      </label>
      <label class="filter-item">
        <span>检测状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>档案快照版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['检测编号'] ?? '—' }}</td>
          <td>{{ row['桥梁名称'] ?? '—' }}</td>
          <td>{{ row['检测类型'] ?? '—' }}</td>
          <td>{{ row['检测日期'] ?? '—' }}</td>
          <td>{{ row['技术状况评分'] ?? '—' }}</td>
          <td>
            {{ row['主要病害'] ?? '—' }}
            <span v-if="row['异常']" class="badge abnormal">结论异常</span>
          </td>
          <td>{{ row['检测单位'] ?? '—' }}</td>
          <td>{{ row['检测状态'] ?? '—' }}</td>
          <td>
            <span v-if="row['档案版本号']">
              v{{ row['档案版本号'] }}
              <span class="badge" :class="stateBadge(row['档案审定状态'])">{{ row['档案审定状态'] }}</span>
              <span class="muted">{{ row['发布状态'] }}</span>
              <div class="muted" style="font-size:12px;">
                {{ row['桥型结构'] }} ｜ 荷载 {{ row['设计荷载'] }} ｜ 等级 {{ row['上次评定等级'] }}
              </div>
              <div class="muted" style="font-size:12px;">{{ row['证据链id'] }}</div>
            </span>
            <span v-else class="muted">未关联档案</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEvidence(row)" :disabled="!row['档案版本号']">证据链复核</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无桥梁定检数据，可先登记检测记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁定检记录（只读档案快照版本）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text">{{ okMessage }}</span>
    </footer>

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
import { onMounted, ref } from 'vue'

import { readError, request } from '@/api/client'
import EvidenceChain from '@/components/EvidenceChain.vue'

type Row = Record<string, any>

const ENDPOINT = '/api/bridge'
const columns = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "主要病害", "检测单位", "检测状态"]
const actions = ["开始检测", "完成评定", "归档报告"]
const statuses = ["待检测", "检测中", "已评定", "已归档"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const okMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const evidenceOpen = ref(false)
const evidence = ref<Row | null>(null)

function stateBadge(state: string): string {
  if (state === '待审定') return 'pending'
  if (state === '已审定') return 'approved'
  return 'published'
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
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '检测记录登记入口尚未接入审批流'
}

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

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error(await readError(response, '桥梁定检动作未生效，请稍后重试'))
    }
    await reload()
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '桥梁定检操作失败')
  }
}

async function reload() {
  flashOk('')
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  params.set('size', '200')
  try {
    const response = await request(`${ENDPOINT}?${params}`)
    if (!response.ok) {
      throw new Error(await readError(response, '检测记录列表读取失败'))
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    flashErr(error instanceof Error ? error.message : '桥梁定检列表读取失败')
  }
}

onMounted(reload)
</script>
