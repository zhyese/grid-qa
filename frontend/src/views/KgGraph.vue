<template>
  <div class="page">
    <header class="topbar">
      <span>知识图谱</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/kg">图谱</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/dashboard">统计</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>

    <div class="kg-wrap">
      <!-- 统计卡片 -->
      <div class="cards" v-if="stats">
        <div class="stat-card c1"><div class="num">{{ stats.tripleTotal }}</div><div class="lbl">三元组总数</div></div>
        <div class="stat-card c2"><div class="num">{{ stats.entityTotal }}</div><div class="lbl">实体数</div></div>
        <div class="stat-card c3"><div class="num">{{ stats.relationTotal }}</div><div class="lbl">关系类型</div></div>
        <div class="stat-card c4"><div class="num">{{ paths.length }}</div><div class="lbl">影响链数</div></div>
      </div>

      <!-- Tab 切换 -->
      <div class="tabs">
        <button :class="{ on: tab === 'graph' }" @click="tab = 'graph'">🔗 关系图谱</button>
        <button :class="{ on: tab === 'path' }" @click="tab = 'path'">🔀 多跳影响链</button>
        <button :class="{ on: tab === 'hub' }" @click="switchHub">⭐ 枢纽实体</button>
      </div>

      <!-- Tab1: 关系图谱 -->
      <div v-show="tab === 'graph'">
        <div class="ctrl card">
          <div class="ctrl-row">
            <input v-model="entity" placeholder="🔍 输入实体(如：主变压器)过滤图谱" @keyup.enter="searchGraph" style="flex:1;min-width:200px" />
            <button @click="searchGraph">搜索</button>
            <button class="secondary" @click="resetGraph">显示全部</button>
          </div>
          <div class="ctrl-row">
            <select v-model="selDoc" style="max-width:300px">
              <option value="">选择文档抽取三元组…</option>
              <option v-for="d in docs" :key="d.docId" :value="d.docId">{{ d.docName }}（{{ d.status }}）</option>
            </select>
            <select v-model="modelType" style="max-width:150px">
              <option value="">默认模型</option>
              <option value="qwen">通义千问</option>
              <option value="deepseek">DeepSeek</option>
              <option value="doubao">豆包</option>
            </select>
            <button @click="doExtract" :disabled="!selDoc || extracting">
              {{ extracting ? '抽取中…(LLM 分块)' : '抽取三元组' }}
            </button>
          </div>
        </div>
        <div class="chart-card">
          <h3>设备-故障-处置 关系图谱 <span class="muted">（{{ graph.nodes.length }} 节点 / {{ graph.links.length }} 关系，可拖拽缩放）</span></h3>
          <div ref="graphEl" class="graph" v-show="graph.nodes.length"></div>
          <div v-if="!graph.nodes.length" class="empty">暂无图谱数据，请在上方选择文档并抽取三元组</div>
        </div>
      </div>

      <!-- Tab2: 多跳影响链 -->
      <div v-show="tab === 'path'">
        <div class="ctrl card">
          <div class="ctrl-row">
            <input v-model="pathEntity" placeholder="🔍 输入起点实体(如：配电变压器)" @keyup.enter="searchPaths" style="flex:1;min-width:200px" />
            <label class="depth-lbl">跳数：
              <select v-model="depth" style="width:90px">
                <option v-for="d in [1, 2, 3, 4, 5]" :key="d" :value="d">{{ d }} 跳</option>
              </select>
            </label>
            <button @click="searchPaths">查找影响链</button>
          </div>
          <div class="path-hint muted">💡 多跳推理：从某设备沿"故障→处置→关联"因果链追溯影响范围（Neo4j，MySQL 做不到）</div>
        </div>
        <div class="chart-card">
          <h3>影响链路径 <span class="muted">（{{ paths.length }} 条，按跳数升序）</span></h3>
          <div v-if="!paths.length" class="empty">输入起点实体，查找其多跳因果影响链</div>
          <div v-else class="path-list">
            <div v-for="(p, i) in paths" :key="i" class="path-item">
              <span class="path-hops">{{ p.hops }}跳</span>
              <span class="path-chain">
                <template v-for="(node, k) in p.chain" :key="k">
                  <span class="path-node" :class="{ start: k === 0, end: k === p.chain.length - 1 }">{{ node }}</span>
                  <span v-if="k < p.rels.length" class="path-rel">—{{ p.rels[k] }}→</span>
                </template>
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab3: 枢纽实体 -->
      <div v-show="tab === 'hub'">
        <div class="chart-card">
          <h3>枢纽实体（出度 Top） <span class="muted">— 影响传播的源头 / 核心设备</span></h3>
          <div v-if="!hubs.length" class="empty">暂无数据，请先抽取三元组</div>
          <div v-else class="hub-list">
            <div v-for="(h, i) in hubs" :key="i" class="hub-item">
              <span class="hub-rank">{{ i + 1 }}</span>
              <span class="hub-name">{{ h.name }}</span>
              <div class="hub-bar-wrap"><div class="hub-bar" :style="{ width: barWidth(h.outDegree) + '%' }"></div></div>
              <span class="hub-deg">{{ h.outDegree }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <transition name="fade">
      <div v-if="toast" class="toast">{{ toast }}</div>
    </transition>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useAuthStore } from '../stores/auth'
import { extractKg, getKgGraph, getKgStats, getKgPaths, getKgInfluence, listDocs } from '../api'

// 按需注册 graph
echarts.use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer])

const auth = useAuthStore()
const router = useRouter()

const tab = ref('graph')
const stats = ref(null)
const docs = ref([])
const graph = ref({ nodes: [], links: [], categories: [], total: 0 })
const entity = ref('')
const selDoc = ref('')
const modelType = ref('')
const extracting = ref(false)
const toast = ref('')
const graphEl = ref(null)
let chart = null
// 多跳影响链
const pathEntity = ref('')
const depth = ref(3)
const paths = ref([])
// 枢纽
const hubs = ref([])

function show(msg) { toast.value = msg; setTimeout(() => (toast.value = ''), 2800) }

async function loadStats() { try { stats.value = (await getKgStats()).data } catch (e) {} }
async function loadDocs() { try { docs.value = (await listDocs()).data.list || [] } catch (e) {} }
async function loadGraph(kw = '') {
  try {
    graph.value = (await getKgGraph(kw, 300)).data
    await nextTick()
    if (tab.value === 'graph') render()
  } catch (e) {}
}
function render() {
  if (!graphEl.value) return
  if (!chart) chart = echarts.init(graphEl.value)
  const cats = graph.value.categories && graph.value.categories.length
    ? graph.value.categories
    : [{ name: '实体' }, { name: '属性/关系' }]
  chart.setOption({
    tooltip: {
      formatter: (p) => p.dataType === 'edge'
        ? `${p.data.source} —${p.data.value}→ ${p.data.target}`
        : p.data.name,
    },
    legend: [{ data: cats.map((c) => c.name), bottom: 0, textStyle: { color: '#64748b' } }],
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true, categories: cats,
      data: graph.value.nodes.map((n) => ({ ...n, category: n.category || 0 })),
      links: graph.value.links,
      label: { show: true, position: 'right', color: '#334155', fontSize: 12 },
      lineStyle: { color: '#94a3b8', width: 1.5, curveness: 0.12 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      force: { repulsion: 220, edgeLength: [60, 150], gravity: 0.08 },
    }],
  })
}

function searchGraph() { loadGraph(entity.value.trim()) }
function resetGraph() { entity.value = ''; loadGraph('') }

// 多跳影响链
async function searchPaths() {
  if (!pathEntity.value.trim()) { show('请输入起点实体'); return }
  try {
    const r = (await getKgPaths(pathEntity.value.trim(), depth.value, 30)).data
    paths.value = r.paths || []
    if (!paths.value.length) show('未找到该实体的多跳影响链')
  } catch (e) { show('查询失败：' + (e.response?.data?.message || e.message)) }
}
// 枢纽
async function switchHub() {
  tab.value = 'hub'
  try { hubs.value = (await getKgInfluence(15)).data.hubs || [] } catch (e) {}
}
function barWidth(deg) {
  const max = Math.max(...hubs.value.map((h) => h.outDegree), 1)
  return Math.round((deg / max) * 100)
}

async function doExtract() {
  if (!selDoc.value) return
  extracting.value = true
  show('开始抽取，LLM 分块处理中，双写 MySQL + Neo4j…')
  try {
    const r = (await extractKg(selDoc.value, modelType.value || null)).data
    show(`抽取完成：${r.tripleCount} 条三元组（${r.docName}）`)
    await Promise.all([loadStats(), loadGraph('')])
    paths.value = []; hubs.value = []   // 抽取后清空缓存，重新查
  } catch (e) { show('抽取失败：' + (e.response?.data?.message || e.message)) }
  finally { extracting.value = false }
}

function logout() { auth.logout(); router.push('/login') }
function onResize() { chart && chart.resize() }

onMounted(async () => {
  await Promise.all([loadStats(), loadDocs(), loadGraph('')])
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); chart && chart.dispose() })
</script>

<style scoped>
.kg-wrap { max-width: 1100px; margin: 20px auto; padding: 0 16px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 18px; }
.stat-card { border-radius: 10px; padding: 22px; text-align: center; color: #fff; box-shadow: var(--shadow); }
.stat-card.c1 { background: linear-gradient(135deg, #2563eb, #3b82f6); }
.stat-card.c2 { background: linear-gradient(135deg, #0891b2, #06b6d4); }
.stat-card.c3 { background: linear-gradient(135deg, #7c3aed, #a855f7); }
.stat-card.c4 { background: linear-gradient(135deg, #ea580c, #f97316); }
.stat-card .num { font-size: 30px; font-weight: bold; }
.stat-card .lbl { font-size: 13px; opacity: .9; margin-top: 4px; }
.tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.tabs button { background: var(--surface); color: var(--text-muted); border: 1px solid var(--border); }
.tabs button.on { background: var(--primary); color: #fff; border-color: var(--primary); }
.ctrl { display: flex; flex-direction: column; gap: 12px; }
.ctrl-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.depth-lbl { font-size: 13px; color: var(--text-muted); display: flex; align-items: center; gap: 4px; }
.chart-card { background: var(--surface); border-radius: 10px; padding: 16px; box-shadow: var(--shadow); margin-top: 16px; }
.chart-card h3 { margin: 0 0 8px; color: var(--primary-dark); font-size: 15px; }
.muted { color: var(--text-muted); font-weight: normal; font-size: 13px; }
.graph { height: 560px; }
.empty { text-align: center; color: var(--text-muted); padding: 50px 0; }
.path-hint { margin-top: 4px; }
/* 影响链 */
.path-list { display: flex; flex-direction: column; gap: 10px; max-height: 560px; overflow-y: auto; }
.path-item { display: flex; align-items: center; gap: 10px; background: var(--surface-2); padding: 10px 12px; border-radius: 8px; flex-wrap: wrap; }
.path-hops { background: var(--primary); color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; flex-shrink: 0; }
.path-chain { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 13px; }
.path-node { background: var(--surface); border: 1px solid var(--border); padding: 3px 8px; border-radius: 6px; color: var(--text); }
.path-node.start { background: #dbeafe; border-color: #3b82f6; font-weight: 600; color: #1e40af; }
.path-node.end { background: #dcfce7; border-color: #22c55e; color: #15803d; }
.path-rel { color: var(--text-muted); font-size: 11px; }
/* 枢纽 */
.hub-list { display: flex; flex-direction: column; gap: 10px; }
.hub-item { display: flex; align-items: center; gap: 12px; }
.hub-rank { width: 24px; height: 24px; background: var(--primary); color: #fff; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; flex-shrink: 0; }
.hub-name { width: 140px; font-size: 14px; flex-shrink: 0; }
.hub-bar-wrap { flex: 1; height: 18px; background: var(--surface-2); border-radius: 9px; overflow: hidden; }
.hub-bar { height: 100%; background: linear-gradient(90deg, #3b82f6, #06b6d4); border-radius: 9px; transition: width .3s; }
.hub-deg { width: 40px; text-align: right; font-weight: bold; color: var(--primary); }
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: #1e293b; color: #fff; padding: 10px 18px; border-radius: 8px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .2); z-index: 9999; font-size: 14px;
}
.fade-enter-active, .fade-leave-active { transition: opacity .25s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .graph { height: 420px; }
  .hub-name { width: 90px; }
}
</style>
