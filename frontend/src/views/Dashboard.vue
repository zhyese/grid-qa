<template>
  <div class="page">
    <header class="topbar">
      <span>知识库统计</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/dashboard">统计</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
    <div class="dash-wrap" v-if="stats">
      <div class="cards">
        <div class="stat-card c1"><div class="num">{{ stats.docTotal }}</div><div class="lbl">文档总数</div></div>
        <div class="stat-card c2"><div class="num">{{ stats.chunkTotal }}</div><div class="lbl">分块总数</div></div>
        <div class="stat-card c3"><div class="num">{{ stats.vectorTotal }}</div><div class="lbl">向量总数</div></div>
        <div class="stat-card c4"><div class="num">{{ stats.byStatus?.vectorized || 0 }}</div><div class="lbl">已向量化</div></div>
      </div>
      <div class="charts">
        <div class="chart-card"><h3>文档状态分布</h3><div ref="statusChart" class="chart"></div></div>
        <div class="chart-card"><h3>文档类型分布</h3><div ref="typeChart" class="chart"></div></div>
      </div>

      <!-- 故障趋势看板（反馈反哺）-->
      <div class="charts" v-if="fb">
        <div class="chart-card">
          <h3>反馈分布</h3>
          <div class="fb-cards">
            <div class="fb-card"><div class="fb-num">{{ fb.total }}</div><div class="fb-lbl">反馈总数</div></div>
            <div class="fb-card like"><div class="fb-num">{{ fb.like }}</div><div class="fb-lbl">👍 有用</div></div>
            <div class="fb-card dislike"><div class="fb-num">{{ fb.dislike }}</div><div class="fb-lbl">👎 有问题</div></div>
            <div class="fb-card warn"><div class="fb-num">{{ (fb.dislikeRate * 100).toFixed(0) }}%</div><div class="fb-lbl">坏 case 率</div></div>
            <div class="fb-card halluc" v-if="fb.avgHallucination != null"><div class="fb-num">{{ (fb.avgHallucination * 100).toFixed(0) }}%</div><div class="fb-lbl">坏case平均幻觉</div></div>
          </div>
        </div>
        <div class="chart-card">
          <h3>坏 case 设备聚类（反哺优化重点）</h3>
          <div ref="deviceChart" class="chart" v-if="fb.topDevices?.length"></div>
          <div v-else class="empty">暂无坏 case 数据</div>
        </div>
      </div>
      <div class="chart-card" v-if="fb && fb.topBadCases?.length">
        <h3>高频坏 case Top</h3>
        <div class="bad-list">
          <div class="bad-item" v-for="(b, i) in fb.topBadCases" :key="i">
            <span class="bad-cnt">{{ b.count }}</span>
            <span class="bad-q">{{ b.query }}</span>
          </div>
        </div>
      </div>
    </div>
    <div v-else class="loading">加载中...</div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { PieChart, BarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useAuthStore } from '../stores/auth'
import { getStats, getFeedbackStats } from '../api'

echarts.use([PieChart, BarChart, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const auth = useAuthStore()
const router = useRouter()
const stats = ref(null)
const fb = ref(null)
const statusChart = ref(null)
const typeChart = ref(null)
const deviceChart = ref(null)
const statusLabels = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }

async function load() {
  try {
    const r = await getStats()
    stats.value = r.data
    await nextTick()
    renderCharts()
  } catch (e) {}
  // 反馈趋势（admin 可见，非 admin 静默跳过）
  if (auth.role === 'admin') {
    try {
      const f = await getFeedbackStats()
      fb.value = f.data
      await nextTick()
      renderDevice()
    } catch (e) {}
  }
}
function renderCharts() {
  if (statusChart.value) {
    echarts.init(statusChart.value).setOption({
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [{
        type: 'pie', radius: ['40%', '70%'],
        data: Object.entries(stats.value.byStatus || {}).map(([k, v]) => ({ name: statusLabels[k] || k, value: v })),
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      }],
    })
  }
  if (typeChart.value) {
    const entries = Object.entries(stats.value.byType || {})
    echarts.init(typeChart.value).setOption({
      tooltip: {},
      xAxis: { type: 'category', data: entries.map((e) => e[0]), axisLabel: { rotate: 20 } },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: entries.map((e) => e[1]), itemStyle: { color: '#2563eb', borderRadius: [4, 4, 0, 0] } }],
    })
  }
}
function renderDevice() {
  if (!deviceChart.value || !fb.value?.topDevices?.length) return
  const d = fb.value.topDevices
  echarts.init(deviceChart.value).setOption({
    tooltip: {},
    grid: { left: 10, right: 20, top: 10, bottom: 10, containLabel: true },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: d.map((x) => x.device).reverse(), axisLabel: { fontSize: 11 } },
    series: [{ type: 'bar', data: d.map((x) => x.count).reverse(), itemStyle: { color: '#ef4444', borderRadius: [0, 4, 4, 0] } }],
  })
}
function logout() { auth.logout(); router.push('/login') }
onMounted(load)
</script>

<style scoped>
.dash-wrap { max-width: 1000px; margin: 20px auto; padding: 0 16px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
.stat-card { border-radius: 10px; padding: 22px; text-align: center; color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
.stat-card.c1 { background: linear-gradient(135deg, #2563eb, #3b82f6); }
.stat-card.c2 { background: linear-gradient(135deg, #0891b2, #06b6d4); }
.stat-card.c3 { background: linear-gradient(135deg, #7c3aed, #a855f7); }
.stat-card.c4 { background: linear-gradient(135deg, #16a34a, #22c55e); }
.stat-card .num { font-size: 32px; font-weight: bold; }
.stat-card .lbl { font-size: 13px; opacity: .9; margin-top: 4px; }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.chart-card { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 16px; }
.chart-card h3 { margin: 0 0 8px; color: #1e3a8a; font-size: 15px; }
.chart { height: 300px; }
.loading { text-align: center; color: #94a3b8; margin-top: 80px; }
.empty { text-align: center; color: #94a3b8; padding: 40px 0; font-size: 13px; }
.fb-cards { display: flex; gap: 12px; flex-wrap: wrap; }
.fb-card { flex: 1; min-width: 90px; background: #f8fafc; border-radius: 8px; padding: 14px; text-align: center; border: 1px solid #e2e8f0; }
.fb-card.like { background: #f0fdf4; border-color: #bbf7d0; }
.fb-card.dislike { background: #fef2f2; border-color: #fecaca; }
.fb-card.warn { background: #fffbeb; border-color: #fde68a; }
.fb-card.halluc { background: #f5f3ff; border-color: #ddd6fe; }
.fb-num { font-size: 24px; font-weight: bold; color: #1e293b; }
.fb-card.dislike .fb-num { color: #dc2626; }
.fb-card.warn .fb-num { color: #d97706; }
.fb-lbl { font-size: 12px; color: #64748b; margin-top: 2px; }
.bad-list { display: flex; flex-direction: column; gap: 8px; }
.bad-item { display: flex; align-items: center; gap: 10px; background: #f8fafc; padding: 8px 12px; border-radius: 6px; }
.bad-cnt { background: #ef4444; color: #fff; font-size: 12px; padding: 2px 8px; border-radius: 10px; flex-shrink: 0; }
.bad-q { color: #475569; font-size: 13px; }
@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .charts { grid-template-columns: 1fr; }
}
</style>
