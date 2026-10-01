<template>
  <div v-if="data" class="evidence-panel">
    <h4 class="modal-title">证据链复核 · {{ data['证据链id'] }}</h4>
    <p class="modal-sub">
      桥梁 {{ data['桥梁编号'] }} {{ data['桥梁名称'] }} ｜ 版本 v{{ data['版本号'] }} ｜
      <span class="badge" :class="badgeClass(data['审定状态'])">{{ data['审定状态'] }}</span>
      <span v-if="data['异常']" class="badge abnormal">结论异常</span>
    </p>

    <p :class="data['一致'] ? 'evidence-ok' : 'evidence-bad'">
      {{ data['一致'] ? '✓ 图面、定检清单、工程待办与档案版本同链一致，可在两处验收。' : '✗ 证据链存在断点或口径不一致：' }}
      <template v-if="!data['一致']">
      <ul>
        <li v-for="issue in data['异常项']" :key="issue">{{ issue }}</li>
      </ul>
      </template>
    </p>

    <div v-if="data['裁决信息']?.['冲突字段']?.length" class="batch-panel">
      桥型/荷载与最新批准记录（#{{ data['裁决信息']['裁决记录'] }}）冲突：
      {{ data['裁决信息']['冲突字段'].join('、') }} ｜
      裁决后桥型结构：<strong>{{ data['桥型结构'] ?? data['图面']?.['桥型结构'] }}</strong>
      ｜ 设计荷载：<strong>{{ data['设计荷载'] ?? data['图面']?.['设计荷载'] }}</strong>
      <br /><span class="muted">历史评定等级按各版本原快照保存，不参与裁决。</span>
    </div>

    <div class="evidence-section">
      <h4>版本快照</h4>
      <div class="kv-list">
        <div class="kv-row"><div class="kv-k">审定结论</div><div class="kv-v">{{ data['版本快照']['审定结论']?.['结论'] ?? '—' }}</div></div>
        <div class="kv-row"><div class="kv-k">审定意见</div><div class="kv-v">{{ data['版本快照']['审定结论']?.['意见'] || '—' }}</div></div>
        <div class="kv-row"><div class="kv-k">历史评定等级</div><div class="kv-v">{{ data['版本快照']['历史评定等级'] ?? '—' }}</div></div>
        <div class="kv-row"><div class="kv-k">发布状态</div><div class="kv-v">{{ data['发布状态'] }}</div></div>
      </div>
    </div>

    <div class="evidence-section">
      <h4>图面</h4>
      <div v-if="data['图面']" class="kv-list">
        <div class="kv-row"><div class="kv-k">图号</div><div class="kv-v">{{ data['图面']['图号'] }}</div></div>
        <div class="kv-row"><div class="kv-k">版本</div><div class="kv-v">v{{ data['图面']['版本号'] }} ｜ {{ data['图面']['发布状态'] }}</div></div>
        <div class="kv-row"><div class="kv-k">桥型结构/荷载</div><div class="kv-v">{{ data['图面']['桥型结构'] }} ｜ {{ data['图面']['设计荷载'] }}</div></div>
        <div class="kv-row"><div class="kv-k">来源批准记录</div><div class="kv-v">#{{ data['图面']['来源批准记录'] }}</div></div>
      </div>
      <p v-else class="muted">待审定快照锁定中，审定通过后回写图面。</p>
    </div>

    <div class="evidence-section">
      <h4>定检清单（{{ data['定检记录'].length }} 条）</h4>
      <div v-if="data['定检记录'].length">
        <div v-for="row in data['定检记录']" :key="row.id" class="kv-list">
          <div class="kv-row"><div class="kv-k">检测编号</div><div class="kv-v">{{ row['检测编号'] }} ｜ v{{ row['档案版本号'] }} ｜ {{ row['发布状态'] }}</div></div>
          <div class="kv-row"><div class="kv-k">主要病害</div><div class="kv-v">{{ row['主要病害'] }}</div></div>
          <div class="kv-row"><div class="kv-k">来源批准记录</div><div class="kv-v">#{{ row['来源批准记录'] }} ｜ {{ row['证据链id'] }}</div></div>
        </div>
      </div>
      <p v-else class="muted">暂无关联定检记录。</p>
    </div>

    <div class="evidence-section">
      <h4>工程待办</h4>
      <div v-if="data['工程待办']" class="kv-list">
        <div class="kv-row"><div class="kv-k">工程编号</div><div class="kv-v">{{ data['工程待办']['工程编号'] }} ｜ {{ data['工程待办']['状态'] }}</div></div>
        <div class="kv-row"><div class="kv-k">事项</div><div class="kv-v">{{ data['工程待办']['事项'] }}</div></div>
        <div class="kv-row"><div class="kv-k">版本/发布</div><div class="kv-v">v{{ data['工程待办']['版本号'] }} ｜ {{ data['工程待办']['发布状态'] }}</div></div>
      </div>
      <p v-else class="muted">待审定快照锁定中，审定通过后生成工程待办。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
type EvidenceData = Record<string, any>

defineProps<{ data: EvidenceData | null }>()

function badgeClass(state: string): string {
  if (state === '待审定') return 'pending'
  if (state === '已审定') return 'approved'
  return 'published'
}
</script>
