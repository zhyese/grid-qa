<template>
  <div class="pred-page">
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">🔮 故障预测建议</h3>
        <div class="row" style="gap:8px;align-items:center">
          <select class="select" v-model="days" style="width:auto" @change="load">
            <option :value="7">近 7 天</option>
            <option :value="30">近 30 天</option>
            <option :value="90">近 90 天</option>
          </select>
          <button class="btn btn-ghost btn-sm" @click="load">🔄 刷新</button>
        </div>
      </div>
      <p class="hint" style="margin-top:0;line-height:1.7">
        基于历史告警做<b>频次/趋势聚合</b>，识别升温设备与高频故障类型，给出主动关注建议（统计+规则，可解释、零额外模型成本）。<br/>
        近 {{ days }} 天告警 {{ data?.totalAlerts ?? 0 }} 条 / 票据 {{ data?.ticketCount ?? 0 }} 份 / 坏 case {{ data?.badCaseCount ?? 0 }} 条 · 生成于 {{ data?.generatedAt || '-' }}
      </p>
      <div class="stats-grid" style="margin-top:8px">
        <div class="stat stat-accent"><div class="stat-val">{{ data?.totalAlerts ?? 0 }}</div><div class="stat-lbl">告警总数</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ data?.distinctTitles ?? 0 }}</div><div class="stat-lbl">告警类型</div></div>
        <div class="stat stat-accent"><div class="stat-val" style="color:var(--danger)">{{ data?.highRiskCount ?? 0 }}</div><div class="stat-lbl">高风险项</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ data?.ticketCount ?? 0 }}</div><div class="stat-lbl">历史票据</div></div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-header"><h3 class="card-title">⚠️ 风险条目（按风险分排序）</h3></div>
      <div v-if="loading" class="hint" style="margin:12px">分析中...</div>
      <div v-else-if="!data?.items?.length" class="empty" style="margin:12px">暂无告警数据（需 Grafana 告警接入后积累）</div>
      <div v-else style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>告警类型</th><th>严重度</th><th>次数</th><th>近7天</th><th>趋势</th><th>风险</th><th>建议</th></tr></thead>
          <tbody>
            <tr v-for="(it, i) in data.items" :key="i">
              <td>{{ it.title }}</td>
              <td><span class="badge" :class="it.severity === 'critical' ? 'badge-danger' : 'badge-warning'">{{ it.severity }}</span></td>
              <td>{{ it.count }}</td>
              <td class="muted">{{ it.recent7 }}</td>
              <td><span :style="{color: it.trend === '上升' ? 'var(--danger)' : 'var(--text-muted)'}">{{ it.trend === '上升' ? '↑上升' : it.trend === '下降' ? '↓下降' : '—平稳' }}</span></td>
              <td><span class="badge" :class="it.riskLevel === '高' ? 'badge-danger' : it.riskLevel === '中' ? 'badge-warning' : 'badge-success'">{{ it.riskLevel }}</span></td>
              <td class="muted" style="max-width:360px">{{ it.suggestion }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getFaultPrediction } from '../api'
const days = ref(30)
const data = ref(null)
const loading = ref(false)
async function load() {
  loading.value = true
  try { data.value = (await getFaultPrediction(days.value)).data } finally { loading.value = false }
}
onMounted(load)
</script>
