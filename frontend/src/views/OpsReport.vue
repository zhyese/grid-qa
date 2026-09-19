<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'create' }" @click="tab = 'create'">✨ 生成报告</button>
      <button class="tab" :class="{ active: tab === 'list' }" @click="loadList(); tab = 'list'">📚 报告库</button>
    </div>

    <!-- 生成 -->
    <div class="card" v-show="tab === 'create'">
      <div class="card-header"><h3 class="card-title">生成运维报告</h3></div>
      <div class="field"><label class="field-label">报告类型 *</label>
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <button v-for="t in meta" :key="t.type" class="btn"
                  :class="{ 'btn-primary': form.reportType === t.type }" @click="pickType(t)">
            {{ t.label }}
          </button>
        </div>
        <div class="hint" style="margin-top:6px" v-if="curTypeMeta">
          分节：{{ curTypeMeta.sections.join(' · ') }}
        </div>
      </div>
      <div class="row" style="gap:12px">
        <div class="field" style="flex:1;min-width:140px">
          <label class="field-label">统计窗口（天）</label>
          <input class="input" type="number" min="1" max="31" v-model="form.days" />
        </div>
        <div class="field" style="flex:2;min-width:200px" v-if="form.reportType === 'fault' || form.reportType === 'device'">
          <label class="field-label">聚焦设备（可选）</label>
          <input class="input" v-model="form.deviceId" placeholder="如：1号主变、110kV城东站#1" />
        </div>
        <div class="field" style="flex:2;min-width:200px">
          <label class="field-label">报告标题（可选）</label>
          <input class="input" v-model="form.title" placeholder="缺省自动命名" />
        </div>
      </div>
      <div class="hint" style="margin:8px 0">
        数据源：告警事件 · 两票 · 主动运维处置 · 交接班快照 · 遥测。LLM 不可用时自动降级为模板报告。
      </div>
      <button class="btn btn-primary" :disabled="creating || !canManage" @click="handleCreate">
        {{ creating ? '创建中…' : '🚀 聚合数据并生成' }}
      </button>
      <span v-if="!canManage" class="hint" style="margin-left:8px">当前角色只读（生成需 report:manage）</span>
    </div>

    <!-- 列表 -->
    <div class="card" v-show="tab === 'list'">
      <div class="card-header">
        <h3 class="card-title">报告库 <span class="badge badge-neutral">{{ list.total }}</span></h3>
        <div class="row">
          <select class="select" v-model="filterType" style="max-width:150px" @change="loadList">
            <option value="">全部类型</option>
            <option v-for="t in meta" :key="t.type" :value="t.type">{{ t.label }}</option>
          </select>
          <button class="btn btn-ghost btn-sm" @click="loadList">🔄 刷新</button>
        </div>
      </div>
      <div v-if="!list.items?.length" class="empty">暂无报告，先去「生成报告」</div>
      <div class="ticket-card" v-for="r in list.items" :key="r.id" @click="openReport(r.id)"
           :class="{ active: selected?.id === r.id }">
        <div class="tc-header">
          <span class="badge" :class="statusBadge(r.status)">{{ statusLabel(r.status) }}</span>
          <span class="badge badge-neutral">{{ typeLabel(r.reportType) }}</span>
          <span class="tc-title">{{ r.title }}</span>
          <span class="hint" v-if="r.llmMeta?.fallback" title="LLM 不可用，模板降级版">⚠ 模板版</span>
          <span class="hint" style="margin-left:auto">{{ r.createdAt?.slice(0, 16).replace('T', ' ') }}</span>
        </div>
        <div class="tc-body" v-if="r.summary">
          <span class="hint">{{ r.summary }}</span>
        </div>
      </div>
    </div>

    <!-- 详情 -->
    <div class="card" v-if="selected" style="margin-top:12px">
      <div class="card-header">
        <h3 class="card-title">📄 {{ selected.title }}</h3>
        <div class="row">
          <button v-if="selected.status === 'generating'" class="btn btn-ghost btn-sm" disabled>⏳ 生成中…</button>
          <template v-else-if="selected.status === 'done'">
            <button class="btn btn-ghost btn-sm" @click="handleExport">⬇ 导出 Word</button>
            <button v-if="canManage" class="btn btn-ghost btn-sm" @click="handleRegen">🔁 重新生成</button>
          </template>
          <button v-if="canManage" class="btn btn-danger btn-sm" @click="handleDelete">🗑 删除</button>
        </div>
      </div>
      <div v-if="selected.status === 'failed'" class="empty" style="color:var(--danger)">
        生成失败：{{ selected.error || '未知错误' }}
        <button v-if="canManage" class="btn btn-ghost btn-sm" style="margin-left:8px" @click="handleRegen">重试</button>
      </div>
      <div v-else-if="selected.status === 'done'" class="md-body" v-html="renderedMd"></div>
      <div v-else class="hint">正在聚合数据与生成内容，稍候自动刷新…</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import {
  createReport, deleteReport, exportReportDocx, getReport, getReportMeta,
  getReports, regenerateReport,
} from '../api'

const auth = useAuthStore()
const canManage = computed(() => hasPerm(auth.role, 'report:manage'))
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const tab = ref('create')
const meta = ref([])
const creating = ref(false)
const form = ref({ reportType: 'shift', days: 1, deviceId: '', title: '' })
const list = ref({ total: 0, items: [] })
const filterType = ref('')
const selected = ref(null)
let pollTimer = null

const curTypeMeta = computed(() => meta.value.find(t => t.type === form.value.reportType))
const renderedMd = computed(() => md.render(selected.value?.contentMd || ''))

function pickType(t) {
  form.value.reportType = t.type
  form.value.days = t.defaultDays
}

async function loadMeta() {
  try {
    const res = await getReportMeta()
    meta.value = res.data || []
    if (meta.value.length && !curTypeMeta.value) pickType(meta.value[0])
  } catch (e) { /* 功能未开启时静默 */ }
}

async function loadList() {
  try {
    const res = await getReports({ reportType: filterType.value, size: 50 })
    list.value = res.data || { total: 0, items: [] }
  } catch (e) { /* 同上 */ }
}

async function handleCreate() {
  if (creating.value) return
  creating.value = true
  try {
    const res = await createReport({
      reportType: form.value.reportType,
      days: Number(form.value.days) || undefined,
      deviceId: form.value.deviceId.trim(),
      title: form.value.title.trim(),
    })
    tab.value = 'list'
    await loadList()
    openReport(res.data?.id)
  } finally { creating.value = false }
}

async function openReport(id) {
  stopPoll()
  try {
    const res = await getReport(id)
    selected.value = res.data
    if (selected.value?.status === 'generating') startPoll(id)
  } catch (e) { /* 忽略 */ }
}

function startPoll(id) {
  pollTimer = setInterval(async () => {
    try {
      const res = await getReport(id)
      selected.value = res.data
      if (res.data?.status !== 'generating') { stopPoll(); loadList() }
    } catch (e) { stopPoll() }
  }, 2500)
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onUnmounted(stopPoll)

async function handleRegen() {
  try {
    await regenerateReport(selected.value.id)
    openReport(selected.value.id)
  } catch (e) { /* BizError 已由拦截器提示 */ }
}

async function handleDelete() {
  if (!confirm('确认删除该报告？')) return
  try {
    await deleteReport(selected.value.id)
    selected.value = null
    loadList()
  } catch (e) { /* 同上 */ }
}

async function handleExport() {
  try {
    const blob = await exportReportDocx(selected.value.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selected.value.title || '运维报告'}.docx`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { alert(e.message || '导出失败') }
}

function statusBadge(s) {
  return { generating: 'badge-warning', done: 'badge-success', failed: 'badge-danger' }[s] || 'badge-neutral'
}
function statusLabel(s) {
  return { generating: '生成中', done: '已完成', failed: '失败' }[s] || s
}
function typeLabel(t) {
  return meta.value.find(m => m.type === t)?.label || t
}

loadMeta()
loadList()
</script>

<style scoped>
.md-body { font-size: 13.5px; line-height: 1.75; color: var(--text); word-break: break-word; }
.md-body :deep(h1) { font-size: 18px; margin: 4px 0 12px; }
.md-body :deep(h2) { font-size: 15px; margin: 18px 0 8px; color: var(--text); }
.md-body :deep(p) { margin: 6px 0; }
.md-body :deep(blockquote) { border-left: 3px solid var(--border); margin: 8px 0; padding: 2px 12px; color: var(--text-soft); }
.md-body :deep(ul), .md-body :deep(ol) { padding-left: 22px; margin: 6px 0; }
</style>
