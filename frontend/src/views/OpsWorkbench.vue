<template>
  <section class="ops">
    <header><h2>运维工作台</h2><p class="muted">证据优先 · 人工确认 · 只读分析，不执行设备控制</p></header>
    <p class="notice">需启用 OPS_WORKBENCH_ENABLE。遥测仅分析已导入数据；所有历史快照均标明生成时状态。</p>
    <nav class="tabs" aria-label="运维功能">
      <button v-for="item in tabs" :key="item.key" class="btn" :class="{ selected: tab === item.key }" @click="switchTab(item.key)">{{ item.label }}</button>
    </nav>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="busy" role="status">正在处理，请稍候…</p>
    <form class="toolbar" @submit.prevent="loadCatalog"><label>查找{{ ['impact', 'tables'].includes(tab) ? '文档' : '设备' }} <input v-model.trim="catalogKeyword" maxlength="100" /></label><button class="btn" :disabled="busy">加载候选</button><span v-if="catalogHasMore">仅显示前200条，请缩小关键词。</span></form>
    <datalist id="ops-docs"><option v-for="d in documentOptions" :key="d.id" :value="d.id">{{ d.name }}（{{ d.status }}）</option></datalist>
    <datalist id="ops-devices"><option v-for="(d,i) in deviceOptions" :key="i" :value="d.id">{{ d.name }} {{ d.station }}</option></datalist>

    <form v-if="tab === 'impact'" class="card form" @submit.prevent="runImpact">
      <p>比较两份已解析文档；同文档的历史文件需单独上传。结果不会自动替代规程或修改票据。</p>
      <label>旧版文档<input v-model.trim="impact.old_doc_id" list="ops-docs" required maxlength="64" /></label>
      <label>新版文档<input v-model.trim="impact.new_doc_id" list="ops-docs" required maxlength="64" /></label>
      <button class="btn" :disabled="busy || !can('doc:manage')">生成变更复核清单</button>
    </form>

    <form v-if="tab === 'handover'" class="card form" @submit.prevent="runHandover">
      <label>交班标题<input v-model.trim="handoverTitle" required maxlength="200" /></label>
      <label>班次开始（本机时区）<input v-model="start" required type="datetime-local" /></label>
      <label>班次结束（本机时区）<input v-model="end" required type="datetime-local" /></label>
      <button class="btn" :disabled="busy || !can('ops:write')">生成交班草稿</button>
    </form>

    <form v-if="tab === 'dossier'" class="card form" @submit.prevent="runDossier(false)">
      <label>规范设备 ID<input v-model.trim="deviceId" list="ops-devices" required maxlength="128" placeholder="在主动运维设备映射中配置的 canonicalDeviceId" /></label>
      <button class="btn" :disabled="busy">查看设备档案</button>
      <button type="button" class="btn" :disabled="busy || !deviceId || !can('ops:write')" @click="runDossier(true)">保存复盘快照</button>
    </form>

    <div v-if="tab === 'telemetry'" class="card">
      <form class="form" @submit.prevent="runTelemetry">
        <label>规范设备 ID<input v-model.trim="deviceId" list="ops-devices" required maxlength="128" /></label>
        <label>来源<input v-model.trim="source" required maxlength="64" placeholder="例如 historian_export" /></label>
        <label>指标<input v-model.trim="metric" required maxlength="64" placeholder="例如 oil_temperature" /></label>
        <label>开始（本机时区）<input v-model="start" required type="datetime-local" /></label>
        <label>结束（本机时区）<input v-model="end" required type="datetime-local" /></label>
        <button class="btn" :disabled="busy">查询历史统计</button>
      </form>
      <details v-if="can('system:config')"><summary>导入来源明确的遥测 JSON（每批最多2000点）</summary>
        <p>格式：{"points":[{"source":"historian_export","device_id":"设备ID","metric":"oil_temperature","unit":"℃","value":50,"quality":"good","observed_at":"2026-09-14T08:00:00+08:00"}]}。示例不会自动导入。</p>
        <textarea v-model="importText" rows="6" aria-label="遥测导入 JSON" />
        <button class="btn" :disabled="busy || !importText.trim()" @click="runImport">校验并导入</button>
      </details>
    </div>

    <form v-if="tab === 'tables'" class="card form" @submit.prevent="runTable">
      <label>文档<input v-model.trim="table.doc_id" list="ops-docs" required maxlength="64" /></label>
      <button type="button" class="btn" :disabled="busy || !table.doc_id" @click="loadTableSchema">查看表头及示例行</button>
      <label>目标列名（精确）<input v-model.trim="table.column" required maxlength="200" placeholder="例如 允许温度（℃）" /></label>
      <label>匹配条件 JSON<textarea v-model="filtersText" required rows="3" placeholder='{"型号":"S11","工况":"额定负载"}' /></label>
      <button class="btn" :disabled="busy">查询原表值</button>
    </form>

    <section v-if="tab === 'tables' && schema" class="card"><h3>已解析表结构</h3><p v-if="!schema.tables.length">没有表格块，请先解析文档。</p><article v-for="t in schema.tables" :key="t.chunkId"><h4>页 {{ t.page || '未知' }} · {{ t.chunkId }}</h4><p>{{ t.parseable ? t.columns.join(' / ') : '结构不完整，须核对原文' }}</p><pre>{{ t.preview }}</pre></article></section>

    <section v-if="['impact', 'handover', 'dossier'].includes(tab)" class="card">
      <div class="toolbar"><h3>已保存记录</h3><button class="btn" :disabled="busy" @click="loadHistory">刷新</button>
        <button class="btn" :disabled="busy || page === 1" @click="page--; loadHistory()">上一页</button><span>第 {{ page }} 页</span>
        <button class="btn" :disabled="busy" @click="page++; loadHistory()">下一页</button></div>
      <p v-if="!history.length" class="muted">本页暂无可访问记录。文档权限变更后，相关快照可能不再可见。</p>
      <ul><li v-for="item in history" :key="item.id"><button class="link" :disabled="busy" @click="openSnapshot(item.id)">{{ item.title }}</button> · {{ statusLabel(item.status) }} · {{ item.creator }}</li></ul>
    </section>

    <section v-if="result" class="card results">
      <h3>{{ result.title || '查询结果' }}</h3>
      <p v-if="result.id">记录 {{ result.id }} · {{ statusLabel(result.status) }} · 版本 {{ result.version }} · {{ result.createdAt }}</p>
      <p v-if="payload.note || payload.scope" class="notice">{{ payload.note || payload.scope }}</p>
      <p v-if="payload.limitations" class="notice">{{ payload.limitations }}</p>
      <template v-if="payload.changes">
        <p>{{ payload.changes.length }} 组差异；{{ payload.ticketCandidates.length }} 张疑似关联票据</p>
        <details v-for="(change, i) in payload.changes" :key="i"><summary>差异 {{ i + 1 }} · {{ change.type }} {{ change.numbersChanged ? '· 数值/编号变化' : '' }}</summary>
          <div class="comparison"><article><h4>旧版</h4><div v-for="e in change.before" :key="e.chunkId"><small>{{ e.section }} · 页 {{ e.page || '未知' }} · {{ e.chunkId }}</small><pre>{{ e.text }}</pre></div></article>
            <article><h4>新版</h4><div v-for="e in change.after" :key="e.chunkId"><small>{{ e.section }} · 页 {{ e.page || '未知' }} · {{ e.chunkId }}</small><pre>{{ e.text }}</pre></div></article></div>
        </details>
        <ul><li v-for="ticket in payload.ticketCandidates" :key="ticket.ticketId">{{ ticket.title }} · {{ ticket.ticketId }} — {{ ticket.basis }}</li></ul>
      </template>
      <template v-if="payload.events"><h4>本班事件（{{ payload.events.length }}）</h4><ul><li v-for="e in payload.events" :key="e.id">{{ e.at }} · {{ e.severity }} · {{ e.title }} · {{ e.status }} <small>证据 {{ e.id }}</small></li></ul>
        <h4>待交接/近期更新票据（{{ payload.tickets.length }}）</h4><ul><li v-for="t in payload.tickets" :key="t.id">{{ t.title }} · {{ t.device }} · {{ t.status }} <small>票据 {{ t.id }}</small></li></ul></template>
      <template v-if="payload.timeline"><h4>{{ payload.names.join(' / ') }} · {{ payload.deviceId }}</h4><ul><li v-for="(e, i) in payload.timeline" :key="i">{{ e.at }} · {{ e.title }} · {{ e.severity }}<br /><small>事件 {{ e.eventId }} / 运行 {{ e.runId || '无' }} / 票据 {{ e.ticketId || '无' }} {{ e.ticketStatus }}</small></li></ul></template>
      <template v-if="payload.answer"><h4>{{ payload.answer }}</h4>
        <p v-if="payload.excluded !== undefined">已排除低质量点：{{ payload.excluded }}；来源：{{ payload.source }}（导入历史数据）</p>
        <svg v-if="chartPoints" viewBox="0 0 640 180" role="img" aria-label="有效遥测点历史趋势" class="chart"><polyline :points="chartPoints" fill="none" stroke="currentColor" stroke-width="2" /></svg>
        <p v-if="payload.stats">单位 {{ payload.stats.unit }}；有效样本 {{ payload.stats.count }}；首末变化 {{ payload.stats.delta }}</p>
        <p v-if="payload.governanceVerified === false" class="notice">文档缺治理元数据：适用性未验证，须人工核对。</p>
        <article v-for="(match, i) in payload.matches || []" :key="i"><h4>{{ match.column }}：{{ match.value || '空值' }}</h4><p>{{ match.docName }} · 页 {{ match.page || '未知' }} · 原表第 {{ match.rowNumber }} 行 · {{ match.chunkId }}</p><pre>{{ match.tableText }}</pre></article>
        <details v-if="payload.points?.length"><summary>查看原始点证据（{{ payload.points.length }}）</summary><pre>{{ JSON.stringify(payload.points, null, 2) }}</pre></details>
      </template>
      <p v-if="result.imported !== undefined">导入 {{ result.imported }} 点，跳过相同重复 {{ result.duplicates }} 点。</p>
      <form v-if="canReview" class="form" @submit.prevent="review('review')"><label>审核备注<textarea v-model.trim="reviewNote" required maxlength="2000" /></label><button class="btn" :disabled="busy">确认已人工复核</button></form>
      <form v-if="canAccept" class="form" @submit.prevent="review('accept')"><label>接班确认备注<textarea v-model.trim="reviewNote" required maxlength="2000" /></label><button class="btn" :disabled="busy">确认接班</button></form>
      <ul v-if="result.audit?.length"><li v-for="(a, i) in result.audit" :key="i">{{ a.at }} · {{ a.actor }} · {{ a.action }}：{{ a.note }}</li></ul>
    </section>
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import * as api from '../api/opsWorkbench'

const auth = useAuthStore()
const can = (p) => hasPerm(auth.role, p)
const tabs = [{ key: 'impact', label: '规程变更' }, { key: 'handover', label: '智能交接班' }, { key: 'dossier', label: '设备档案' }, { key: 'telemetry', label: '遥测历史' }, { key: 'tables', label: '表格精准查询' }]
const tab = ref('impact'), busy = ref(false), error = ref(''), result = ref(null), history = ref([]), page = ref(1)
const impact = reactive({ old_doc_id: '', new_doc_id: '' }), table = reactive({ doc_id: '', column: '' })
const handoverTitle = ref(''), start = ref(''), end = ref(''), deviceId = ref(''), source = ref(''), metric = ref('')
const importText = ref(''), filtersText = ref(''), reviewNote = ref('')
const catalogKeyword = ref(''), documentOptions = ref([]), deviceOptions = ref([]), catalogHasMore = ref(false), schema = ref(null)
const payload = computed(() => result.value?.payload || result.value || {})
const canReview = computed(() => result.value?.status === 'draft' && (result.value.kind === 'impact' ? can('doc:manage') : result.value.kind === 'handover' && can('ops:review')))
const canAccept = computed(() => result.value?.kind === 'handover' && result.value.status === 'reviewed' && can('ops:write') && auth.username !== result.value.creator && !result.value.audit.some(a => a.action === 'review' && a.actor === auth.username))
const chartPoints = computed(() => {
  const points = (payload.value.points || []).filter(p => p.quality === 'good')
  if (points.length < 2) return ''
  const ys = points.map(p => p.value), min = Math.min(...ys), span = Math.max(...ys) - min || 1
  const first = Date.parse(points[0].at), duration = Date.parse(points.at(-1).at) - first || 1
  return points.map(p => `${10 + 620 * (Date.parse(p.at) - first) / duration},${170 - 160 * (p.value - min) / span}`).join(' ')
})
const statusLabel = s => ({ draft: '待复核', reviewed: '已复核', accepted: '已接班' }[s] || s)
function switchTab(key) { if (busy.value) return; tab.value = key; result.value = null; error.value = ''; history.value = []; page.value = 1; reviewNote.value = '' }
function range() { return { start: new Date(start.value).toISOString(), end: new Date(end.value).toISOString() } }
async function perform(fn, historyOnly = false) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { const response = await fn(); if (response?.code !== 200) throw new Error(response?.message || '请求失败'); if (historyOnly) history.value = response.data.list; else result.value = response.data }
  catch (e) { error.value = e.message || '请求失败'; if (!historyOnly) result.value = null }
  finally { busy.value = false }
}
const runImpact = () => perform(() => api.analyzeImpact(impact))
const runHandover = () => perform(() => api.createHandover({ title: handoverTitle.value, ...range() }))
const runDossier = save => perform(() => save ? api.saveDeviceDossier(deviceId.value) : api.getDeviceDossier(deviceId.value))
const runTelemetry = () => perform(() => api.queryTelemetry({ device_id: deviceId.value, source: source.value, metric: metric.value, ...range() }))
const runImport = () => perform(() => api.importTelemetry(JSON.parse(importText.value)))
const runTable = () => perform(() => api.queryTable({ ...table, filters: JSON.parse(filtersText.value) }))
const loadHistory = () => perform(() => api.listOpsSnapshots(tab.value, page.value), true)
const openSnapshot = id => perform(() => api.getOpsSnapshot(id))
const review = action => perform(() => api.reviewOpsSnapshot(result.value.id, { version: result.value.version, action, note: reviewNote.value }))
async function loadCatalog() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { const docs = ['impact', 'tables'].includes(tab.value); const r = await (docs ? api.listWorkbenchDocuments(catalogKeyword.value) : api.listWorkbenchDevices(catalogKeyword.value)); if (r.code !== 200) throw new Error(r.message); if (docs) documentOptions.value = r.data.list; else deviceOptions.value = r.data.list; catalogHasMore.value = r.data.hasMore }
  catch(e) { error.value = e.message } finally { busy.value = false }
}
async function loadTableSchema() {
  if (busy.value) return
  busy.value = true; error.value = ''; schema.value = null
  try { const r = await api.getTableSchema(table.doc_id); if (r.code !== 200) throw new Error(r.message); schema.value = r.data }
  catch(e) { error.value = e.message } finally { busy.value = false }
}
</script>

<style scoped>
.ops { max-width: 1200px; margin: auto; }.tabs,.toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:16px 0; }.selected { background:var(--primary,#2563eb); color:white; }.card { padding:20px; margin:16px 0; border:1px solid var(--border,#d1d5db); border-radius:12px; }.form { display:flex; flex-wrap:wrap; align-items:end; gap:16px; }.form p { width:100%; }.form label { display:grid; gap:8px; flex:1 1 260px; }input,textarea { padding:10px; border:1px solid var(--border,#cbd5e1); border-radius:6px; background:var(--bg-card,transparent); color:inherit; width:100%; box-sizing:border-box; }textarea { resize:vertical; }.notice { padding:12px; background:rgba(245,158,11,.1); border-left:3px solid #d97706; }.error { color:#dc2626; }.comparison { display:grid; grid-template-columns:1fr 1fr; gap:16px; }.comparison article { min-width:0; }pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:420px; overflow:auto; background:rgba(128,128,128,.07); padding:12px; }li { margin:12px 0; overflow-wrap:anywhere; }details { margin:16px 0; }summary { cursor:pointer; }.link { background:none; border:0; color:var(--primary,#2563eb); cursor:pointer; }.chart { width:100%; max-height:240px; color:#2563eb; border:1px solid var(--border,#cbd5e1); }@media(max-width:700px) { .comparison { grid-template-columns:1fr; } }
</style>
