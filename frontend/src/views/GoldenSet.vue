<template>
  <div class="gs-page">
    <!-- 概览 -->
    <div class="toolbar">
      <div class="stats">
        <span class="badge badge-neutral">用例 {{ stats.total || 0 }}</span>
        <span class="badge badge-success">可发布 {{ stats.publishable || 0 }}</span>
        <span class="badge badge-info">golden 文件 {{ stats.goldenFileCount || 0 }} 条</span>
        <span class="badge badge-warning">最新版本 v{{ stats.latestVersion || 0 }}</span>
        <span class="badge badge-neutral">待标注 {{ stats.byStatus?.pending || 0 }}</span>
      </div>
      <div class="actions">
        <button class="btn btn-ghost btn-sm" @click="reloadAll">🔄 刷新</button>
        <template v-if="canManage">
          <button class="btn btn-ghost btn-sm" :disabled="busy" @click="onIngest">🚨 拉取坏 case</button>
          <label class="btn btn-ghost btn-sm file-btn">
            📥 导入 CSV/Excel
            <input type="file" accept=".csv,.xlsx,.xlsm" @change="onImport" hidden />
          </label>
          <button class="btn btn-primary btn-sm" :disabled="busy" @click="onPublish">🚀 发布版本</button>
        </template>
        <button class="btn btn-ghost btn-sm" @click="onExportLaya">🧪 导出 Laya</button>
      </div>
    </div>

    <div v-if="toastMsg" class="toast" :class="{ err: toastErr }">{{ toastMsg }}</div>

    <!-- 筛选 -->
    <div class="filters">
      <input class="input" v-model="filter.keyword" placeholder="搜索问题 / 设备标签" @keyup.enter="loadCases" />
      <select class="input" v-model="filter.category" @change="loadCases">
        <option value="">全部分类</option>
        <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
      </select>
      <select class="input" v-model="filter.difficulty" @change="loadCases">
        <option value="">全部难度</option>
        <option v-for="d in DIFFICULTIES" :key="d" :value="d">{{ d }}</option>
      </select>
      <select class="input" v-model="filter.labelStatus" @change="loadCases">
        <option value="">全部状态</option>
        <option v-for="s in LABEL_STATUSES" :key="s" :value="s">{{ statusLabel(s) }}</option>
      </select>
      <button class="btn btn-ghost btn-sm" @click="loadCases">查询</button>
      <button v-if="canManage" class="btn btn-primary btn-sm" @click="openEditor()">＋ 新建用例</button>
    </div>

    <!-- 用例表 -->
    <div class="card">
      <table class="gs-table">
        <thead>
          <tr>
            <th style="width:34%">问题</th>
            <th>分类</th>
            <th>难度</th>
            <th>设备标签</th>
            <th>期望来源</th>
            <th>状态</th>
            <th>更新</th>
            <th v-if="canManage" style="width:110px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in cases.list" :key="c.id">
            <td class="q">{{ c.query }}</td>
            <td>{{ c.category || '—' }}</td>
            <td>{{ c.difficulty || '—' }}</td>
            <td class="tags">{{ (c.deviceTags || []).join('、') || '—' }}</td>
            <td class="tags">{{ (c.expectedSources || []).join('、') || '—' }}</td>
            <td><span :class="statusBadge(c.labelStatus)">{{ statusLabel(c.labelStatus) }}</span></td>
            <td class="meta">{{ c.updatedBy || '—' }}<br />{{ c.updatedAt }}</td>
            <td v-if="canManage">
              <button class="btn btn-ghost btn-xs" @click="openEditor(c)">编辑</button>
              <button class="btn btn-ghost btn-xs danger" @click="onDelete(c)">删除</button>
            </td>
          </tr>
          <tr v-if="!cases.list?.length">
            <td :colspan="canManage ? 8 : 7" class="empty">
              暂无用例。可「导入 CSV/Excel」或从质量事件「拉取坏 case」生成待标注草稿。
            </td>
          </tr>
        </tbody>
      </table>
      <div class="pager" v-if="cases.total > filter.size">
        <button class="btn btn-ghost btn-sm" :disabled="filter.page <= 1" @click="goPage(-1)">上一页</button>
        <span class="meta">第 {{ filter.page }} 页 / 共 {{ Math.ceil(cases.total / filter.size) }} 页（{{ cases.total }} 条）</span>
        <button class="btn btn-ghost btn-sm"
                :disabled="filter.page >= Math.ceil(cases.total / filter.size)"
                @click="goPage(1)">下一页</button>
      </div>
    </div>

    <!-- 版本 -->
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">🗂️ 版本与哈希链</h3>
        <span class="hint">发布即写回 backend/data/golden_qa.json，评测门禁自动覆盖</span>
      </div>
      <table class="gs-table">
        <thead>
          <tr><th>版本</th><th>条数</th><th>内容哈希</th><th>前序哈希</th><th>说明</th><th>发布人</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="v in versions" :key="v.version">
            <td>v{{ v.version }}</td>
            <td>{{ v.caseCount }}</td>
            <td class="mono">{{ short(v.contentHash) }}</td>
            <td class="mono">{{ short(v.prevHash) }}</td>
            <td>{{ v.note || '—' }}</td>
            <td>{{ v.createdBy || '—' }}</td>
            <td class="meta">{{ v.createdAt }}</td>
            <td><button class="btn btn-ghost btn-xs" @click="diffBase = v.version">设为对比基准</button></td>
          </tr>
          <tr v-if="!versions.length"><td colspan="8" class="empty">尚无发布版本。</td></tr>
        </tbody>
      </table>
      <div class="diff-bar" v-if="diffBase">
        <span class="meta">基准 v{{ diffBase }} →</span>
        <select class="input" v-model.number="diffTarget">
          <option :value="0">选择目标版本</option>
          <option v-for="v in versions" :key="v.version" :value="v.version">v{{ v.version }}</option>
        </select>
        <button class="btn btn-ghost btn-sm" :disabled="!diffTarget" @click="onDiff">对比</button>
        <button class="btn btn-ghost btn-sm" @click="diffBase = 0; diff = null">清除</button>
      </div>
      <div v-if="diff" class="diff-result">
        <div>新增 {{ diff.addedCount }} 条<span v-if="diff.added?.length">：{{ diff.added.slice(0, 5).join(' | ') }}</span></div>
        <div>删除 {{ diff.removedCount }} 条<span v-if="diff.removed?.length">：{{ diff.removed.slice(0, 5).join(' | ') }}</span></div>
        <div>变更 {{ diff.changedCount }} 条<span v-if="diff.changed?.length">：{{ diff.changed.slice(0, 5).join(' | ') }}</span></div>
      </div>
    </div>

    <!-- 编辑抽屉 -->
    <div v-if="editor.open" class="modal-mask" @click.self="editor.open = false">
      <div class="modal">
        <div class="modal-head">
          <b>{{ editor.id ? '编辑用例' : '新建用例' }}</b>
          <button class="btn btn-ghost btn-sm" @click="editor.open = false">✕</button>
        </div>
        <div class="modal-body">
          <label class="field"><span>问题 query *</span>
            <textarea class="input" rows="2" v-model="editor.query" placeholder="如：主变压器温度异常如何处置"></textarea>
          </label>
          <label class="field"><span>期望关键词 expect（逗号分隔；feedback 来源可留空）</span>
            <input class="input" v-model="editor.expect" placeholder="主变压器, 油温" />
          </label>
          <div class="row">
            <label class="field"><span>分类 category *</span>
              <input class="input" v-model="editor.category" placeholder="变电 / 配电 / 输电" />
            </label>
            <label class="field"><span>难度 difficulty</span>
              <select class="input" v-model="editor.difficulty">
                <option value="">未标注</option>
                <option v-for="d in DIFFICULTIES" :key="d" :value="d">{{ d }}</option>
              </select>
            </label>
            <label class="field"><span>标注状态</span>
              <select class="input" v-model="editor.labelStatus">
                <option v-for="s in LABEL_STATUSES" :key="s" :value="s">{{ statusLabel(s) }}</option>
              </select>
            </label>
          </div>
          <label class="field"><span>设备标签（逗号分隔）</span>
            <input class="input" v-model="editor.deviceTags" placeholder="1号主变, 2号主变" />
          </label>
          <label class="field"><span>期望来源文档（逗号分隔，用于召回评估）</span>
            <input class="input" v-model="editor.expectedSources" placeholder="主变压器运行规程, 安全规程-电力安全工作规程" />
          </label>
          <label class="field"><span>备注</span>
            <input class="input" v-model="editor.note" maxlength="500" />
          </label>
          <div v-if="editor.errors?.length" class="err-box">
            校验提示：<div v-for="(e, i) in editor.errors" :key="i">· {{ e }}</div>
          </div>
        </div>
        <div class="modal-foot">
          <span class="hint">「已标注 / 已复核」的用例才会进入发布版本</span>
          <button class="btn btn-ghost" @click="editor.open = false">取消</button>
          <button class="btn btn-primary" :disabled="busy" @click="saveCase">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import {
  createGoldenCase, deleteGoldenCase, diffGoldenVersions, exportLayaFormat,
  getGoldenCategories, getGoldenStats, importGoldenCases, ingestGoldenQualityEvents,
  listGoldenCases, listGoldenVersions, publishGoldenSet, updateGoldenCase,
} from '../api'

const DIFFICULTIES = ['easy', 'medium', 'hard']
const LABEL_STATUSES = ['pending', 'labeled', 'verified', 'rejected']
const STATUS_TEXT = { pending: '待标注', labeled: '已标注', verified: '已复核', rejected: '弃用' }

const auth = useAuthStore()
const canManage = computed(() => hasPerm(auth.role, 'golden:manage'))

const stats = ref({})
const cases = ref({ total: 0, list: [] })
const categories = ref([])
const versions = ref([])
const diffBase = ref(0)
const diffTarget = ref(0)
const diff = ref(null)
const busy = ref(false)
const toastMsg = ref('')
const toastErr = ref(false)

const filter = reactive({ keyword: '', category: '', difficulty: '', labelStatus: '', page: 1, size: 20 })
const editor = reactive({
  open: false, id: '', query: '', expect: '', category: '', difficulty: '',
  deviceTags: '', expectedSources: '', labelStatus: 'pending', source: 'manual',
  note: '', errors: [],
})

function statusLabel(s) { return STATUS_TEXT[s] || s || '—' }
function statusBadge(s) {
  return { pending: 'badge badge-warning', labeled: 'badge badge-info',
           verified: 'badge badge-success', rejected: 'badge badge-neutral' }[s] || 'badge badge-neutral'
}
function short(h) { return h ? `${h.slice(0, 10)}…${h.slice(-6)}` : '—' }
function toast(msg, err = false) {
  toastMsg.value = msg
  toastErr.value = err
  setTimeout(() => { toastMsg.value = '' }, 4000)
}

async function loadStats() {
  try { stats.value = (await getGoldenStats()).data || {} } catch (e) { /* 开关未开/无权限 */ }
}
async function loadCategories() {
  try { categories.value = (await getGoldenCategories()).data?.categories || [] } catch (e) { categories.value = [] }
}
async function loadCases() {
  try {
    const res = await listGoldenCases({ ...filter })
    cases.value = res.data || { total: 0, list: [] }
  } catch (e) { cases.value = { total: 0, list: [] } }
}
async function loadVersions() {
  try { versions.value = (await listGoldenVersions()).data?.versions || [] } catch (e) { versions.value = [] }
}
async function reloadAll() {
  await Promise.all([loadStats(), loadCategories(), loadCases(), loadVersions()])
}
function goPage(delta) { filter.page += delta; loadCases() }

function openEditor(c) {
  if (c) {
    Object.assign(editor, {
      open: true, id: c.id, query: c.query,
      expect: (c.expect || []).join(', '), category: c.category || '',
      difficulty: c.difficulty || '', deviceTags: (c.deviceTags || []).join(', '),
      expectedSources: (c.expectedSources || []).join(', '),
      labelStatus: c.labelStatus || 'pending', source: c.source || 'manual',
      note: c.note || '', errors: [],
    })
  } else {
    Object.assign(editor, {
      open: true, id: '', query: '', expect: '', category: '', difficulty: '',
      deviceTags: '', expectedSources: '', labelStatus: 'pending', source: 'manual',
      note: '', errors: [],
    })
  }
}

function splitList(v) {
  return String(v || '').split(/[,，]/).map(s => s.trim()).filter(Boolean)
}

async function saveCase() {
  busy.value = true
  editor.errors = []
  try {
    const payload = {
      query: editor.query.trim(),
      expect: splitList(editor.expect),
      category: editor.category.trim(),
      difficulty: editor.difficulty,
      deviceTags: splitList(editor.deviceTags),
      expectedSources: splitList(editor.expectedSources),
      labelStatus: editor.labelStatus,
      source: editor.source || 'manual',
      note: editor.note,
    }
    const res = editor.id
      ? await updateGoldenCase(editor.id, payload)
      : await createGoldenCase(payload)
    const errs = res.data?.validationErrors || []
    if (errs.length) {
      editor.errors = errs
      toast('已保存，但存在校验提示（不影响标注，发布时会被拦截）', true)
    } else {
      editor.open = false
      toast('已保存')
    }
    await Promise.all([loadStats(), loadCases(), loadCategories()])
  } catch (e) {
    toast('保存失败：' + (e?.message || e), true)
  } finally { busy.value = false }
}

async function onDelete(c) {
  if (!confirm(`确认删除用例「${c.query}」？`)) return
  try {
    await deleteGoldenCase(c.id)
    toast('已删除')
    await Promise.all([loadStats(), loadCases()])
  } catch (e) { toast('删除失败', true) }
}

async function onImport(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const res = await importGoldenCases(file)
    const d = res.data || {}
    toast(`导入完成：成功 ${d.imported} 行，失败 ${d.failed} 行（解析 ${d.parsed} 行）`, !!d.failed)
    await Promise.all([loadStats(), loadCases(), loadCategories()])
  } catch (e) { toast('导入失败：' + (e?.message || e), true) } finally { busy.value = false }
}

async function onIngest() {
  busy.value = true
  try {
    const res = await ingestGoldenQualityEvents(200)
    const d = res.data || {}
    toast(`已生成待标注草稿 ${d.created} 条（扫描 ${d.scanned} 条事件，跳过 ${d.skipped} 条）`)
    await Promise.all([loadStats(), loadCases()])
  } catch (e) { toast('拉取失败', true) } finally { busy.value = false }
}

async function onPublish() {
  const note = prompt('版本说明（可留空）：', '') ?? null
  if (note === null) return
  busy.value = true
  try {
    const res = await publishGoldenSet(note)
    const d = res.data || {}
    toast(`已发布 v${d.version}：${d.published} 条写入 golden_qa.json`
      + (d.invalid ? `，${d.invalid} 条因校验未通过被跳过` : ''))
    if (d.nextStep) console.info('下一步验收：', d.nextStep)
    await Promise.all([loadStats(), loadVersions()])
  } catch (e) { toast('发布失败：' + (e?.data?.message || e?.message || e), true) } finally { busy.value = false }
}

async function onExportLaya() {
  try {
    const res = await exportLayaFormat('')
    const d = res.data || {}
    const blob = new Blob([JSON.stringify(d.samples || [], null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `laya_bootstrap_${d.count || 0}.json`
    a.click()
    URL.revokeObjectURL(a.href)
    toast(`已导出 ${d.count} 条 Laya 冷启动样本（gold 标签留空待标注）`)
  } catch (e) { toast('导出失败', true) }
}

async function onDiff() {
  try {
    diff.value = (await diffGoldenVersions(diffBase.value, diffTarget.value)).data
  } catch (e) { toast('对比失败', true) }
}

onMounted(reloadAll)
</script>

<style scoped>
.gs-page { display: flex; flex-direction: column; gap: 12px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
.stats { display: flex; gap: 6px; flex-wrap: wrap; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.file-btn { cursor: pointer; }
.filters { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.filters .input { max-width: 180px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.card-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; gap: 8px; }
.card-title { margin: 0; font-size: 14px; }
.gs-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.gs-table th { text-align: left; padding: 6px 8px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
.gs-table td { padding: 6px 8px; border-bottom: 1px dashed var(--border); vertical-align: top; }
.gs-table td.q { color: var(--text); }
.gs-table td.tags, .gs-table td.meta { color: var(--text-muted); }
.gs-table td.mono { font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: var(--text-muted); }
.empty { text-align: center; color: var(--text-muted); padding: 18px 0; }
.pager { display: flex; align-items: center; gap: 10px; justify-content: center; padding-top: 8px; }
.hint { color: var(--text-muted); font-size: 12px; }
.meta { color: var(--text-muted); font-size: 11.5px; }
.toast { padding: 8px 12px; border-radius: 8px; background: rgba(108, 92, 231, 0.1); border: 1px solid var(--border); font-size: 12.5px; }
.toast.err { background: rgba(214, 48, 49, 0.1); }
.diff-bar { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
.diff-result { margin-top: 8px; font-size: 12.5px; color: var(--text-soft); display: flex; flex-direction: column; gap: 3px; }
.modal-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: var(--surface); border-radius: 12px; width: min(680px, 92vw); max-height: 88vh; display: flex; flex-direction: column; }
.modal-head, .modal-foot { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; border-bottom: 1px solid var(--border); gap: 8px; }
.modal-foot { border-bottom: none; border-top: 1px solid var(--border); }
.modal-body { padding: 12px 14px; overflow: auto; display: flex; flex-direction: column; gap: 10px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: var(--text-muted); flex: 1; }
.field .input { width: 100%; }
.row { display: flex; gap: 10px; }
.err-box { background: rgba(214, 48, 49, 0.08); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; color: var(--danger); }
.btn-xs { padding: 2px 6px; font-size: 11.5px; }
.btn-xs.danger { color: var(--danger); }
</style>
