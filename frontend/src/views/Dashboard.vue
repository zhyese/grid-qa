<template>
  <div v-if="stats">
    <!-- 统计卡片 -->
    <div class="stat-grid cols-4">
      <div class="stat stat-accent"><div class="stat-val">{{ stats.docTotal }}</div><div class="stat-lbl">文档总数</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ stats.chunkTotal }}</div><div class="stat-lbl">分块总数</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ stats.vectorTotal }}</div><div class="stat-lbl">向量总数</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ stats.byStatus?.vectorized || 0 }}</div><div class="stat-lbl">已向量化</div></div>
    </div>

    <!-- 图表 -->
    <div class="chart-row">
      <div class="card"><div class="card-header"><h3 class="card-title">文档状态分布</h3></div><div ref="statusChart" class="chart"></div></div>
      <div class="card"><div class="card-header"><h3 class="card-title">文档类型分布</h3></div><div ref="typeChart" class="chart"></div></div>
    </div>

    <!-- 故障趋势看板 -->
    <template v-if="fb">
      <div class="chart-row">
        <div class="card">
          <div class="card-header"><h3 class="card-title">📈 反馈分布</h3></div>
          <div class="fb-cards">
            <div class="fb-card"><div class="fb-val">{{ fb.total }}</div><div class="fb-lbl">反馈总数</div></div>
            <div class="fb-card ok"><div class="fb-val">{{ fb.like }}</div><div class="fb-lbl">👍 有用</div></div>
            <div class="fb-card bad"><div class="fb-val">{{ fb.dislike }}</div><div class="fb-lbl">👎 有问题</div></div>
            <div class="fb-card warn"><div class="fb-val">{{ (fb.dislikeRate * 100).toFixed(0) }}%</div><div class="fb-lbl">坏 case 率</div></div>
            <div class="fb-card halluc" v-if="fb.avgHallucination != null"><div class="fb-val">{{ (fb.avgHallucination * 100).toFixed(0) }}%</div><div class="fb-lbl">坏case幻觉</div></div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">🎯 坏 case 设备聚类 <span class="hint">反哺优化重点</span></h3></div>
          <div ref="deviceChart" class="chart" v-if="fb.topDevices?.length"></div>
          <div v-else class="empty">暂无坏 case 数据</div>
        </div>
      </div>
      <div class="card" v-if="fb.topBadCases?.length">
        <div class="card-header"><h3 class="card-title">🔥 高频坏 case Top</h3></div>
        <div class="bad-list">
          <div class="bad-item" v-for="(b, i) in fb.topBadCases" :key="i"><span class="badge badge-danger">{{ b.count }}</span><span>{{ b.query }}</span></div>
        </div>
      </div>
      <div class="card" v-if="fb.coverageGaps?.length">
        <div class="card-header"><h3 class="card-title">🕳️ 知识盲区报告 <span class="hint">高频坏case设备 × 知识库覆盖交叉</span></h3></div>
        <div class="gap-list">
          <div class="gap-item" v-for="(g, i) in fb.coverageGaps" :key="i" :class="{ 'gap-missing': !g.covered }">
            <span class="gap-device">{{ g.device }}</span>
            <span class="badge" :class="g.covered ? 'badge-success' : 'badge-danger'">{{ g.covered ? '已覆盖' : '缺口' }}</span>
            <span class="gap-count">坏case × {{ g.dislikeCount }}</span>
            <span class="gap-tip" v-if="!g.covered">{{ g.suggestion }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
  <div v-else class="loading">加载中...</div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { PieChart, BarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useAuthStore } from '../stores/auth'
import { getStats, getFeedbackStats } from '../api'

echarts.use([PieChart, BarChart, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const auth = useAuthStore()
const stats = ref(null)
const fb = ref(null)
const statusChart = ref(null)
const typeChart = ref(null)
const deviceChart = ref(null)
const statusLabels = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }

async function load() {
  try {
    const r = await getStats(); stats.value = r.data; await nextTick(); renderCharts()
  } catch (e) {}
  if (auth.role === 'admin') {
    try { const f = await getFeedbackStats(); fb.value = f.data; await nextTick(); renderDevice() } catch (e) {}
  }
}
function renderCharts() {
  if (statusChart.value) {
    echarts.init(statusChart.value).setOption({
      tooltip: { trigger: 'item' }, legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
      series: [{ type: 'pie', radius: ['42%', '70%'], itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        data: Object.entries(stats.value.byStatus || {}).map(([k, v]) => ({ name: statusLabels[k] || k, value: v })) }],
    })
  }
  if (typeChart.value) {
    const e = Object.entries(stats.value.byType || {})
    echarts.init(typeChart.value).setOption({
      tooltip: {}, grid: { left: 10, right: 20, top: 10, bottom: 30, containLabel: true },
      xAxis: { type: 'category', data: e.map((x) => x[0]), axisLabel: { rotate: 20, color: '#94a3b8' } },
      yAxis: { type: 'value', axisLabel: { color: '#94a3b8' } },
      series: [{ type: 'bar', data: e.map((x) => x[1]), itemStyle: { color: '#4f46e5', borderRadius: [4, 4, 0, 0] } }],
    })
  }
}
function renderDevice() {
  if (!deviceChart.value || !fb.value?.topDevices?.length) return
  const d = fb.value.topDevices
  echarts.init(deviceChart.value).setOption({
    tooltip: {}, grid: { left: 10, right: 30, top: 10, bottom: 10, containLabel: true },
    xAxis: { type: 'value', axisLabel: { color: '#94a3b8' } },
    yAxis: { type: 'category', data: d.map((x) => x.device).reverse(), axisLabel: { fontSize: 11, color: '#94a3b8' } },
    series: [{ type: 'bar', data: d.map((x) => x.count).reverse(), itemStyle: { color: '#ef4444', borderRadius: [0, 4, 4, 0] } }],
  })
}
onMounted(load)
</script>

<style scoped>
.stat-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.chart { height: 290px; }
.fb-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
.fb-card { background: var(--surface-2); border-radius: var(--radius); padding: 16px; text-align: center; }
.fb-card.ok { background: var(--success-soft); } .fb-card.bad { background: var(--danger-soft); }
.fb-card.warn { background: var(--warning-soft); } .fb-card.halluc { background: var(--primary-soft); }
.fb-val { font-size: 26px; font-weight: 700; color: var(--text); }
.fb-card.ok .fb-val { color: var(--success) } .fb-card.bad .fb-val { color: var(--danger) }
.fb-card.warn .fb-val { color: var(--warning) } .fb-card.halluc .fb-val { color: var(--primary) }
.fb-lbl { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.bad-list { display: flex; flex-direction: column; gap: 8px; }
.bad-item { display: flex; align-items: center; gap: 10px; background: var(--surface-2); padding: 9px 12px; border-radius: var(--radius-sm); font-size: 13px; color: var(--text-muted); }
.gap-list { display: flex; flex-direction: column; gap: 8px; }
.gap-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: var(--radius-sm); background: var(--surface-2); font-size: 13px; }
.gap-item.gap-missing { background: var(--danger-soft); border-left: 3px solid var(--danger); }
.gap-device { font-weight: 600; color: var(--text); min-width: 80px; }
.gap-count { color: var(--text-muted); font-size: 12px; }
.gap-tip { color: var(--danger); font-size: 12px; flex: 1; }
.badge-success { background: var(--success-soft); color: var(--success); }
@media (max-width: 900px) { .stat-grid.cols-4 { grid-template-columns: repeat(2, 1fr) } .chart-row { grid-template-columns: 1fr } }
</style>
