<template>
  <div class="kg-page">
    <!-- 统计 -->
    <div class="stat-grid cols-4" v-if="stats">
      <div class="stat stat-accent"><div class="stat-val">{{ stats.tripleTotal }}</div><div class="stat-lbl">三元组</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ stats.entityTotal }}</div><div class="stat-lbl">实体</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ stats.relationTotal }}</div><div class="stat-lbl">关系类型</div></div>
      <div class="stat stat-accent"><div class="stat-val">{{ paths.length }}</div><div class="stat-lbl">影响链</div></div>
    </div>

    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'graph' }" @click="tab = 'graph'">🔗 关系图谱</button>
      <button class="tab" :class="{ active: tab === 'path' }" @click="tab = 'path'">🔀 多跳影响链</button>
      <button class="tab" :class="{ active: tab === 'hub' }" @click="switchHub">⭐ 枢纽实体</button>
    </div>

    <!-- 关系图谱 -->
    <div class="card" v-show="tab === 'graph'">
      <div class="row" style="margin-bottom: 12px">
        <input class="input" v-model="entity" placeholder="🔍 输入实体(如：主变压器)过滤" @keyup.enter="searchGraph" style="flex:1;min-width:200px" />
        <button class="btn btn-primary" @click="searchGraph">搜索</button>
        <button class="btn btn-ghost" @click="resetGraph">显示全部</button>
      </div>
      <div class="row" style="margin-bottom: 12px">
        <select class="select" v-model="selDoc" style="max-width:300px">
          <option value="">选择文档抽取三元组…</option>
          <option v-for="d in docs" :key="d.docId" :value="d.docId">{{ d.docName }}（{{ d.status }}）</option>
        </select>
        <select class="select" v-model="modelType" style="max-width:140px">
          <option value="">默认模型</option><option value="qwen">通义千问</option><option value="deepseek">DeepSeek</option><option value="doubao">豆包</option>
        </select>
        <button class="btn btn-primary" @click="doExtract" :disabled="!selDoc || extracting">{{ extracting ? '抽取中…(LLM 分块)' : '抽取三元组' }}</button>
      </div>
      <div ref="graphEl" class="graph" v-if="graph.nodes.length"></div>
      <div v-else class="empty">暂无图谱数据，请在上方选择文档并抽取三元组</div>
    </div>

    <!-- 多跳影响链 -->
    <div class="card" v-show="tab === 'path'">
      <div class="row" style="margin-bottom: 12px">
        <input class="input" v-model="pathEntity" placeholder="🔍 起点实体(如：配电变压器)" @keyup.enter="searchPaths" style="flex:1;min-width:200px" />
        <label class="hint">跳数 <select class="select" v-model="depth" style="width:80px"><option v-for="d in [1,2,3,4,5]" :key="d" :value="d">{{ d }}</option></select></label>
        <button class="btn btn-primary" @click="searchPaths">查找影响链</button>
      </div>
      <p class="hint">💡 多跳推理：沿「故障→处置→关联」因果链追溯影响范围（Neo4j）</p>
      <div v-if="!paths.length" class="empty">输入起点实体，查找其多跳因果影响链</div>
      <div v-else class="path-list">
        <div class="path-item" v-for="(p, i) in paths" :key="i">
          <span class="badge badge-primary">{{ p.hops }}跳</span>
          <span class="path-chain"><template v-for="(node, k) in p.chain" :key="k"><span class="path-node" :class="{ start: k === 0, end: k === p.chain.length - 1 }">{{ node }}</span><span v-if="k < p.rels.length" class="path-rel">—{{ p.rels[k] }}→</span></template></span>
        </div>
      </div>
    </div>

    <!-- 枢纽 -->
    <div class="card" v-show="tab === 'hub'">
      <div class="card-header"><h3 class="card-title">枢纽实体（出度 Top）<span class="hint">影响传播源头</span></h3></div>
      <div v-if="!hubs.length" class="empty">暂无数据，请先抽取三元组</div>
      <div v-else class="hub-list">
        <div class="hub-item" v-for="(h, i) in hubs" :key="i">
          <span class="hub-rank">{{ i + 1 }}</span><span class="hub-name">{{ h.name }}</span>
          <div class="hub-bar-wrap"><div class="hub-bar" :style="{ width: barWidth(h.outDegree) + '%' }"></div></div>
          <span class="hub-deg">{{ h.outDegree }}</span>
        </div>
      </div>
    </div>
    <transition name="fade"><div class="toast" v-if="toast">{{ toast }}</div></transition>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { extractKg, getKgGraph, getKgStats, getKgPaths, getKgInfluence, listDocs } from '../api'

echarts.use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer])

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
const pathEntity = ref('')
const depth = ref(3)
const paths = ref([])
const hubs = ref([])
let toastTimer = null
function show(msg) { toast.value = msg; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toast.value = ''), 2600) }

async function loadStats() { try { stats.value = (await getKgStats()).data } catch (e) {} }
async function loadDocs() { try { docs.value = (await listDocs()).data.list || [] } catch (e) {} }
async function loadGraph(kw = '') {
  try {
    if (chart) { chart.dispose(); chart = null }
    graph.value = (await getKgGraph(kw, 800)).data
    await nextTick()
    if (tab.value === 'graph' && graphEl.value) render()
  } catch (e) { graph.value = { nodes: [], links: [], categories: [], total: 0 } }
}
function render() {
  if (!graphEl.value || !graph.value.nodes.length) return
  chart = echarts.init(graphEl.value)
  const cats = graph.value.categories?.length ? graph.value.categories : [{ name: '实体' }, { name: '属性/关系' }]
  chart.setOption({
    tooltip: { formatter: (p) => p.dataType === 'edge' ? `${p.data.source} —${p.data.value}→ ${p.data.target}` : p.data.name },
    legend: [{ data: cats.map((c) => c.name), bottom: 0, textStyle: { color: '#94a3b8' } }],
    series: [{ type: 'graph', layout: 'force', roam: true, draggable: true, categories: cats,
      data: graph.value.nodes.map((n) => ({ ...n, category: n.category || 0 })), links: graph.value.links,
      label: { show: true, position: 'right', color: '#64748b', fontSize: 12 },
      lineStyle: { color: '#94a3b8', width: 1.5, curveness: 0.12 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      force: { repulsion: 220, edgeLength: [60, 150], gravity: 0.08 } }],
  })
  setTimeout(() => chart && chart.resize(), 100)
}
function searchGraph() { loadGraph(entity.value.trim()) }
function resetGraph() { entity.value = ''; loadGraph('') }
async function searchPaths() {
  if (!pathEntity.value.trim()) { show('请输入起点实体'); return }
  try { const r = (await getKgPaths(pathEntity.value.trim(), depth.value, 30)).data; paths.value = r.paths || []; if (!paths.value.length) show('未找到该实体的多跳影响链') } catch (e) { show('查询失败') }
}
async function switchHub() { tab.value = 'hub'; try { hubs.value = (await getKgInfluence(15)).data.hubs || [] } catch (e) {} }
function barWidth(deg) { return Math.round((deg / Math.max(...hubs.value.map((h) => h.outDegree), 1)) * 100) }
async function doExtract() {
  if (!selDoc.value) return
  extracting.value = true; show('开始抽取，LLM 分块处理中，双写 MySQL + Neo4j…')
  try { const r = (await extractKg(selDoc.value, modelType.value || null)).data; show(`抽取完成：${r.tripleCount} 条三元组（${r.docName}）`); await Promise.all([loadStats(), loadGraph('')]); paths.value = []; hubs.value = [] }
  catch (e) { show('抽取失败') } finally { extracting.value = false }
}
function onResize() { chart && chart.resize() }
watch(tab, async (t) => { if (t === 'graph') { await nextTick(); chart && chart.resize() } })
onMounted(async () => { await Promise.all([loadStats(), loadDocs(), loadGraph('')]); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); chart && chart.dispose() })
</script>

<style scoped>
.kg-page { display: flex; flex-direction: column; height: calc(100vh - var(--topbar-h) - 8px); gap: 14px; }
.kg-page .stat-grid.cols-4 { flex-shrink: 0; margin-bottom: 0; }
.kg-page .tabs { flex-shrink: 0; }
.kg-page > .card { flex: 1; min-height: 0; margin-bottom: 0; display: flex; flex-direction: column; overflow: hidden; }
.stat-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
.graph { flex: 1; min-height: 320px; }
.path-list { display: flex; flex-direction: column; gap: 8px; max-height: 540px; overflow-y: auto; }
.path-item { display: flex; align-items: center; gap: 10px; background: var(--surface-2); padding: 10px 12px; border-radius: var(--radius-sm); flex-wrap: wrap; }
.path-chain { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 13px; }
.path-node { background: var(--surface); border: 1px solid var(--border); padding: 3px 8px; border-radius: var(--radius-sm); color: var(--text); }
.path-node.start { background: var(--primary-soft); border-color: var(--primary); font-weight: 600; color: var(--primary); }
.path-node.end { background: var(--success-soft); border-color: var(--success); color: var(--success); }
.path-rel { color: var(--text-soft); font-size: 11px; }
.hub-list { display: flex; flex-direction: column; gap: 10px; }
.hub-item { display: flex; align-items: center; gap: 12px; }
.hub-rank { width: 26px; height: 26px; background: var(--primary); color: #fff; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; flex-shrink: 0; }
.hub-name { width: 140px; font-size: 14px; flex-shrink: 0; color: var(--text); }
.hub-bar-wrap { flex: 1; height: 18px; background: var(--surface-2); border-radius: 9px; overflow: hidden; }
.hub-bar { height: 100%; background: linear-gradient(90deg, var(--primary), var(--accent)); border-radius: 9px; transition: width .3s; }
.hub-deg { width: 40px; text-align: right; font-weight: 700; color: var(--primary); }
.fade-enter-active, .fade-leave-active { transition: opacity .25s; }
@media (max-width: 900px) { .stat-grid.cols-4 { grid-template-columns: repeat(2, 1fr) } .graph { height: 420px } .hub-name { width: 90px } }
</style>
