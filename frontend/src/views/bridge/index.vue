<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>桥梁定检管理</h2>
        <p class="page-desc">
          定检清单受档案版本快照锁约束，只能读到已发布审定版本；异常与桥梁档案明细沿同一条证据链复核。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记检测记录</button>
        <button class="btn" type="button" @click="exportRows">导出桥梁定检清单</button>
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
        <span>桥梁编号</span>
        <input v-model="bridgeCode" placeholder="按桥梁编号过滤已发布版本" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>快照版本</th>
          <th>审定结论</th>
          <th>可执行动作</th>
          <th>证据链</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.快照版本 ? `v${row.快照版本}` : '—' }}</td>
          <td>{{ row.审定结论 ?? '—' }}</td>
          <td class="row-actions">
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
          <td>
            <button class="link" type="button" @click="reviewEvidence(row)">复核</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无已发布版本的定检数据（待审定快照中的记录已被版本锁隐藏）</td>
        </tr>
      </tbody>
    </table>

    <section v-if="evidence" class="evidence-panel">
      <header class="page-head">
        <div>
          <h3>证据链复核 — {{ evidence.桥梁编号 }}</h3>
          <p class="page-desc">与桥梁档案明细页面使用同一个接口、同一份快照链与批准记录。</p>
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
      <span>共 {{ total }} 条桥梁定检记录</span>
      <span v-if="noticeMessage" class="ok-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type Evidence = Record<string, any>

const ENDPOINT = '/api/bridge'
const columns = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "主要病害", "检测单位", "检测状态"]
const actions = ["开始检测", "完成评定", "归档报告"]
const stats = [
  { label: "已发布版本定检", value: 0 },
  { label: "版本锁隐藏", value: 0 },
  { label: "最新批准记录", value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)
const bridgeCode = ref('')
const evidence = ref<Evidence | null>(null)

function resetFilters() {
  filters.value = {}
  bridgeCode.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '检测记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      throw new Error(body.detail ?? body.message ?? '桥梁定检动作未生效')
    }
    noticeMessage.value = body.message ?? '操作成功'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁定检操作失败'
  }
}

async function reviewEvidence(row: Row) {
  const code = String(row.桥梁编号 ?? '')
  if (!code) {
    errorMessage.value = '该记录尚未关联已发布桥梁档案，没有证据链可复核'
    return
  }
  errorMessage.value = ''
  const response = await request(`${ENDPOINT}/by-code/${encodeURIComponent(code)}/evidence`)
  if (!response.ok) {
    errorMessage.value = '证据链读取失败'
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
  if (bridgeCode.value) query.set('bridge_code', bridgeCode.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('检测记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = rows.value.length
    stats[1].value = 0
    stats[2].value = rows.value.reduce(
      (max: number, row: Row) => Math.max(max, Number(row.证据链?.最新批准记录id ?? 0)),
      0,
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁定检列表读取失败'
  }
}

onMounted(reload)
</script>
