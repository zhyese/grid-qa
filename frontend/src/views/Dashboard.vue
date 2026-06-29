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
import { getStats } from '../api'

// 按需注册，避免打包全量 echarts
echarts.use([PieChart, BarChart, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const auth = useAuthStore()
const router = useRouter()
const stats = ref(null)
const statusChart = ref(null)
const typeChart = ref(null)
const statusLabels = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }

async function load() {
  try {
    const r = await getStats()
    stats.value = r.data
    await nextTick()
    renderCharts()
  } catch (e) {}
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
.chart-card { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.chart-card h3 { margin: 0 0 8px; color: #1e3a8a; font-size: 15px; }
.chart { height: 300px; }
.loading { text-align: center; color: #94a3b8; margin-top: 80px; }
@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .charts { grid-template-columns: 1fr; }
}
</style>
