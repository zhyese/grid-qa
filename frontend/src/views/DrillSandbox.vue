<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'run' }" @click="tab = 'run'">🎬 开始演练</button>
      <button class="tab" :class="{ active: tab === 'scenarios' }" @click="loadScenarios(); tab = 'scenarios'">📖 演练剧本</button>
      <button class="tab" :class="{ active: tab === 'history' }" @click="loadHistory(); tab = 'history'">🏅 演练历史</button>
    </div>

    <!-- 开始演练 -->
    <div class="card" v-show="tab === 'run'">
      <!-- 进行中 -->
      <template v-if="run && run.status === 'running'">
        <div class="card-header">
          <h3 class="card-title">🔴 演练进行中：{{ run.scenarioName }}</h3>
          <div class="row">
            <span class="badge badge-danger">⏱ {{ run.elapsedSec }}s</span>
            <span class="badge badge-neutral">事件 {{ run.dueEvents?.length }}/{{ run.totalEvents }}</span>
            <button class="btn btn-ghost btn-sm" @click="handleAbort">终止</button>
            <button class="btn btn-primary btn-sm" @click="handleFinish">完成并评分</button>
          </div>
        </div>
        <div class="hint" style="margin-bottom:8px">故障设备：{{ run.faultDevice || '—' }} · 按时间轴推演，事件到期自动出现</div>
        <div class="drill-feed">
          <div v-for="(e, i) in [...(run.dueEvents || [])].reverse()" :key="i"
               class="drill-event" :class="'sev-' + (e.severity || 'info')">
            <span class="badge" :class="sevBadge(e.severity)">{{ e.severity }}</span>
            <b style="margin:0 6px">t+{{ e.tOffset }}s</b>
            <span style="font-weight:600">{{ e.device }}</span>：{{ e.event }}
          </div>
          <div v-if="!run.dueEvents?.length" class="hint" style="padding:12px 0">等待第一个事件…</div>
        </div>
        <div class="row" style="gap:8px; margin-top:12px">
          <input class="input" style="flex:1" v-model="actionText" placeholder="记录你的处置操作（如：查看1号主变遥测；汇报调度；隔离故障设备）"
                 @keyup.enter="handleAction" />
          <button class="btn btn-primary btn-sm" :disabled="!actionText.trim()" @click="handleAction">打卡</button>
        </div>
        <div style="margin-top:8px">
          <span class="hint">已打卡 {{ run.actions?.length || 0 }} 步：</span>
          <span v-for="(a, i) in run.actions" :key="i" class="badge badge-neutral" style="margin:2px">
            t+{{ a.t }}s {{ a.action.slice(0, 16) }}</span>
        </div>
      </template>

      <!-- 已结束 -->
      <template v-else-if="run && run.status !== 'running'">
        <div class="card-header">
          <h3 class="card-title">{{ run.status === 'finished' ? '🏁 演练复盘' : '⛔ 演练已终止' }}：{{ run.scenarioName }}</h3>
          <button class="btn btn-ghost btn-sm" @click="run = null">返回选剧本</button>
        </div>
        <div v-if="run.score?.coverage !== undefined" class="stats-grid" style="margin-bottom:12px">
          <div class="stat stat-accent"><div class="stat-val">{{ Math.round((run.score.coverage || 0) * 100) }}%</div><div class="stat-lbl">动作覆盖率</div></div>
          <div class="stat stat-accent"><div class="stat-val">{{ run.score.grade || '—' }}</div><div class="stat-lbl">评级</div></div>
          <div class="stat stat-accent"><div class="stat-val">{{ run.score.avgResponseSec ?? '—' }}s</div><div class="stat-lbl">平均响应</div></div>
          <div class="stat stat-accent"><div class="stat-val">{{ run.durationSec }}s</div><div class="stat-lbl">用时</div></div>
        </div>
        <div class="md-body" v-html="renderMd(run.evaluationMd || '')"></div>
      </template>

      <!-- 选剧本开始 -->
      <template v-else>
        <div class="card-header">
          <h3 class="card-title">选择剧本开始演练</h3>
          <button class="btn btn-ghost btn-sm" @click="loadScenarios">🔄 刷新</button>
        </div>
        <div v-if="!scenarios.length" class="empty">暂无剧本，去「演练剧本」创建或从孪生故障链生成</div>
        <div v-for="s in scenarios" :key="s.id" class="ticket-card">
          <div class="tc-header">
            <span class="badge" :class="diffBadge(s.difficulty)">{{ diffLabel(s.difficulty) }}</span>
            <span class="badge badge-info" v-if="s.source === 'fault_chain'">孪生生成</span>
            <span class="tc-title">{{ s.name }}</span>
            <span class="hint" style="margin-left:auto">{{ s.propagation?.length }} 事件 · {{ s.checklist?.length }} 动作</span>
          </div>
          <div class="tc-body" v-if="s.faultDesc"><span class="hint">{{ s.faultDesc }}</span></div>
          <div class="td-actions" style="margin-top:8px">
            <button class="btn btn-primary btn-sm" :disabled="!s.enabled || !canManage" @click="handleStart(s.id)">
              {{ s.enabled ? '🚀 开始演练' : '已停用' }}</button>
          </div>
        </div>
      </template>
    </div>

    <!-- 剧本管理 -->
    <div class="card" v-show="tab === 'scenarios'">
      <div class="card-header">
        <h3 class="card-title">演练剧本 <span class="badge badge-neutral">{{ scenarioPage.total }}</span></h3>
        <div class="row">
          <button v-if="canManage" class="btn btn-ghost btn-sm" @click="chainGen = !chainGen">⚡ 从孪生故障链生成</button>
          <button v-if="canManage" class="btn btn-primary btn-sm" @click="openEditor()">＋ 手工新建</button>
        </div>
      </div>

      <div v-if="chainGen && canManage" style="border:1px dashed var(--border); border-radius:8px; padding:12px; margin-bottom:14px">
        <div class="row" style="gap:8px; align-items:flex-end">
          <div class="field" style="flex:1"><label class="field-label">站点</label>
            <input class="input" v-model="chainForm.stationId" placeholder="110kV-demo" /></div>
          <div class="field" style="flex:1"><label class="field-label">故障设备 ID *</label>
            <input class="input" v-model="chainForm.deviceId" placeholder="如：main-transformer-1" /></div>
          <button class="btn btn-primary btn-sm" :disabled="!chainForm.deviceId.trim()" @click="handleGenFromChain">生成</button>
        </div>
        <div class="hint" style="margin-top:6px">读取孪生故障传播链 → 自动编排事件时间轴与处置清单，生成后可编辑</div>
      </div>

      <div v-if="editor" style="border:1px solid var(--border); border-radius:8px; padding:14px; margin-bottom:14px">
        <div class="card-header"><h3 class="card-title">{{ editor.id ? '编辑剧本' : '新建剧本' }}</h3></div>
        <div class="row" style="gap:8px">
          <div class="field" style="flex:2"><label class="field-label">名称 *</label>
            <input class="input" v-model="editor.name" /></div>
          <div class="field" style="flex:1"><label class="field-label">难度</label>
            <select class="select" v-model="editor.difficulty">
              <option value="easy">简单</option><option value="normal">常规</option><option value="hard">困难</option>
            </select></div>
        </div>
        <div class="row" style="gap:8px">
          <div class="field" style="flex:1"><label class="field-label">站点</label>
            <input class="input" v-model="editor.stationId" /></div>
          <div class="field" style="flex:1"><label class="field-label">故障设备</label>
            <input class="input" v-model="editor.faultDevice" /></div>
        </div>
        <div class="field"><label class="field-label">故障描述</label>
          <input class="input" v-model="editor.faultDesc" /></div>

        <label class="field-label">传播事件时间轴（{{ editor.propagation.length }}）</label>
        <div v-for="(e, i) in editor.propagation" :key="i" class="row" style="gap:6px; margin:4px 0; align-items:center">
          <input class="input" style="max-width:80px" type="number" min="0" v-model.number="e.tOffset" placeholder="秒" />
          <input class="input" style="max-width:150px" v-model="e.device" placeholder="设备" />
          <select class="select" style="max-width:100px" v-model="e.severity">
            <option value="critical">critical</option><option value="warning">warning</option><option value="info">info</option>
          </select>
          <input class="input" style="flex:1" v-model="e.event" placeholder="事件描述" />
          <button class="btn btn-danger btn-sm" @click="editor.propagation.splice(i, 1)">✕</button>
        </div>
        <button class="btn btn-ghost btn-sm" @click="editor.propagation.push({ tOffset: 0, device: editor.faultDevice, severity: 'warning', event: '' })">＋ 加事件</button>

        <label class="field-label" style="margin-top:12px">应尽动作清单（{{ editor.checklist.length }}）</label>
        <div v-for="(c, i) in editor.checklist" :key="i" class="row" style="gap:6px; margin:4px 0; align-items:center">
          <input class="input" style="flex:2" v-model="c.action" placeholder="动作描述" />
          <input class="input" style="flex:1" v-model="c.keywordsText" placeholder="匹配词（逗号分隔）" />
          <button class="btn btn-danger btn-sm" @click="editor.checklist.splice(i, 1)">✕</button>
        </div>
        <button class="btn btn-ghost btn-sm" @click="editor.checklist.push({ action: '', keywordsText: '' })">＋ 加动作</button>

        <div class="row" style="gap:8px; margin-top:14px">
          <button class="btn btn-primary btn-sm" @click="handleSaveScenario">保存</button>
          <button class="btn btn-ghost btn-sm" @click="editor = null">取消</button>
        </div>
      </div>

      <div v-if="!scenarioPage.items?.length" class="empty">暂无剧本</div>
      <div v-for="s in scenarioPage.items" :key="s.id" class="ticket-card">
        <div class="tc-header">
          <span class="badge" :class="s.enabled ? 'badge-success' : 'badge-neutral'">{{ s.enabled ? '启用' : '停用' }}</span>
          <span class="badge" :class="diffBadge(s.difficulty)">{{ diffLabel(s.difficulty) }}</span>
          <span class="badge badge-info" v-if="s.source === 'fault_chain'">孪生生成</span>
          <span class="tc-title">{{ s.name }}</span>
          <span class="hint" style="margin-left:auto">{{ s.updatedAt?.slice(0, 16).replace('T', ' ') }}</span>
        </div>
        <div class="tc-body"><span class="hint">{{ s.description }}</span></div>
        <div class="td-actions" style="margin-top:8px" v-if="canManage">
          <button class="btn btn-ghost btn-sm" @click="openEditor(s)">✏️ 编辑</button>
          <button class="btn btn-ghost btn-sm" @click="handleToggleEnabled(s)">{{ s.enabled ? '停用' : '启用' }}</button>
          <button class="btn btn-danger btn-sm" @click="handleDeleteScenario(s)">🗑</button>
        </div>
      </div>
    </div>

    <!-- 历史 -->
    <div class="card" v-show="tab === 'history'">
      <div class="card-header">
        <h3 class="card-title">演练历史 <span class="badge badge-neutral">{{ history.total }}</span></h3>
        <button class="btn btn-ghost btn-sm" @click="loadHistory">🔄 刷新</button>
      </div>
      <div v-if="stats" class="stats-grid" style="margin-bottom:12px">
        <div class="stat stat-accent"><div class="stat-val">{{ stats.finished }}</div><div class="stat-lbl">完成场次</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ stats.running }}</div><div class="stat-lbl">进行中</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ stats.avgCoverage != null ? Math.round(stats.avgCoverage * 100) + '%' : '—' }}</div><div class="stat-lbl">平均覆盖率</div></div>
      </div>
      <div v-if="!history.items?.length" class="empty">暂无演练记录</div>
      <div v-for="r in history.items" :key="r.id" class="ticket-card">
        <div class="tc-header">
          <span class="badge" :class="r.status === 'finished' ? 'badge-success' : r.status === 'running' ? 'badge-warning' : 'badge-neutral'">
            {{ { finished: '完成', running: '进行中', aborted: '终止' }[r.status] || r.status }}</span>
          <span v-if="r.score?.grade" class="badge" :class="r.score.coverage >= 0.7 ? 'badge-success' : 'badge-warning'">{{ r.score.grade }}</span>
          <span class="tc-title">{{ r.scenarioName }}</span>
          <span class="hint" style="margin-left:auto">{{ r.startedBy }} · {{ r.startedAt?.slice(0, 16).replace('T', ' ') }}</span>
        </div>
        <div class="tc-body">
          <span class="hint" v-if="r.score?.coverage !== undefined">
            覆盖率 {{ Math.round((r.score.coverage || 0) * 100) }}% · 用时 {{ r.durationSec }}s</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import {
  abortDrillRun, createDrillScenario, deleteDrillScenario, finishDrillRun,
  genScenarioFromFaultChain, getDrillRuns, getDrillScenarios, getDrillRunState,
  getDrillStats, recordDrillAction, startDrillRun, updateDrillScenario,
} from '../api'

const auth = useAuthStore()
const canManage = computed(() => hasPerm(auth.role, 'drill:manage'))
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const tab = ref('run')
const scenarios = ref([])
const scenarioPage = ref({ total: 0, items: [] })
const history = ref({ total: 0, items: [] })
const stats = ref(null)
const run = ref(null)
const actionText = ref('')
const chainGen = ref(false)
const chainForm = ref({ stationId: '', deviceId: '' })
const editor = ref(null)
let pollTimer = null

function renderMd(text) { return md.render(String(text || '')) }

async function loadScenarios() {
  try {
    const res = await getDrillScenarios({ size: 100 })
    scenarioPage.value = res.data || { total: 0, items: [] }
    scenarios.value = scenarioPage.value.items
  } catch (e) { /* 功能未开启时静默 */ }
}

async function loadHistory() {
  try {
    const [runsRes, statsRes] = await Promise.all([getDrillRuns({ size: 50 }), getDrillStats()])
    history.value = runsRes.data || { total: 0, items: [] }
    stats.value = statsRes.data
  } catch (e) { /* 同上 */ }
}

async function handleStart(scenarioId) {
  stopPoll()
  try {
    const res = await startDrillRun(scenarioId)
    run.value = res.data
    tab.value = 'run'
    startPoll()
  } catch (e) { /* 拦截器已提示 */ }
}

function startPoll() {
  pollTimer = setInterval(async () => {
    if (!run.value || run.value.status !== 'running') return stopPoll()
    try {
      const res = await getDrillRunState(run.value.id)
      run.value = res.data
      if (res.data.status !== 'running') { stopPoll(); loadHistory() }
    } catch (e) { stopPoll() }
  }, 2000)
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onUnmounted(stopPoll)

async function handleAction() {
  const text = actionText.value.trim()
  if (!text) return
  const res = await recordDrillAction(run.value.id, text)
  run.value = res.data
  actionText.value = ''
}

async function handleFinish() {
  stopPoll()
  const res = await finishDrillRun(run.value.id)
  run.value = res.data
  loadHistory()
}

async function handleAbort() {
  if (!confirm('确认终止本次演练？将按当前进度评分')) return
  stopPoll()
  const res = await abortDrillRun(run.value.id)
  run.value = res.data
  loadHistory()
}

// ---- 剧本编辑 ----

function openEditor(s = null) {
  if (s) {
    editor.value = {
      id: s.id, name: s.name, stationId: s.stationId || '', faultDevice: s.faultDevice || '',
      faultDesc: s.faultDesc || '', difficulty: s.difficulty || 'normal',
      propagation: (s.propagation || []).map(e => ({ ...e })),
      checklist: (s.checklist || []).map(c => ({ ...c, keywordsText: (c.keywords || []).join(',') })),
    }
  } else {
    editor.value = {
      id: null, name: '', stationId: '', faultDevice: '', faultDesc: '', difficulty: 'normal',
      propagation: [{ tOffset: 0, device: '', severity: 'critical', event: '' }],
      checklist: [{ action: '', keywordsText: '' }],
    }
  }
}

async function handleSaveScenario() {
  const ed = editor.value
  if (!ed.name.trim() || !ed.propagation.length || !ed.checklist.length) {
    alert('名称 / 事件时间轴 / 动作清单均不能为空')
    return
  }
  const payload = {
    name: ed.name.trim(), stationId: ed.stationId.trim(), faultDevice: ed.faultDevice.trim(),
    faultDesc: ed.faultDesc.trim(), difficulty: ed.difficulty,
    propagation: ed.propagation.filter(e => e.event?.trim()).map(e => ({
      tOffset: Number(e.tOffset) || 0, device: (e.device || '').trim(),
      severity: e.severity || 'warning', event: e.event.trim(),
    })),
    checklist: ed.checklist.filter(c => c.action?.trim()).map((c, i) => ({
      id: `c${i + 1}`, action: c.action.trim(),
      keywords: (c.keywordsText || '').split(/[,，]/).map(k => k.trim()).filter(Boolean),
    })),
  }
  if (!payload.propagation.length || !payload.checklist.length) {
    alert('事件与动作至少各一条有效内容')
    return
  }
  try {
    if (ed.id) await updateDrillScenario(ed.id, payload)
    else await createDrillScenario(payload)
    editor.value = null
    loadScenarios()
  } catch (e) { /* 拦截器已提示 */ }
}

async function handleGenFromChain() {
  try {
    await genScenarioFromFaultChain({
      stationId: chainForm.value.stationId.trim(),
      deviceId: chainForm.value.deviceId.trim(),
    })
    chainGen.value = false
    chainForm.value = { stationId: '', deviceId: '' }
    loadScenarios()
  } catch (e) { /* 同上 */ }
}

async function handleToggleEnabled(s) {
  await updateDrillScenario(s.id, { enabled: !s.enabled })
  loadScenarios()
}

async function handleDeleteScenario(s) {
  if (!confirm(`删除剧本「${s.name}」？`)) return
  await deleteDrillScenario(s.id)
  loadScenarios()
}

function sevBadge(sev) {
  return { critical: 'badge-danger', warning: 'badge-warning', info: 'badge-info' }[sev] || 'badge-neutral'
}
function diffBadge(d) {
  return { easy: 'badge-success', normal: 'badge-info', hard: 'badge-danger' }[d] || 'badge-neutral'
}
function diffLabel(d) {
  return { easy: '简单', normal: '常规', hard: '困难' }[d] || d
}

loadScenarios()
loadHistory()
</script>

<style scoped>
.drill-feed { max-height: 300px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.drill-event { padding: 7px 10px; border-radius: 6px; margin-bottom: 6px; background: var(--surface-2); font-size: 13px; }
.drill-event.sev-critical { border-left: 3px solid var(--danger); }
.drill-event.sev-warning { border-left: 3px solid var(--warning); }
.drill-event.sev-info { border-left: 3px solid var(--info); }
.md-body { font-size: 13.5px; line-height: 1.75; color: var(--text); }
.md-body :deep(h2) { font-size: 15px; margin: 14px 0 6px; }
.md-body :deep(p) { margin: 6px 0; }
.md-body :deep(ul), .md-body :deep(ol) { padding-left: 22px; margin: 6px 0; }
</style>
