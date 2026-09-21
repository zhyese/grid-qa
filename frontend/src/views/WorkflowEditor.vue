<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'list' }" @click="loadList(); tab = 'list'">📋 工作流</button>
      <button class="tab" :class="{ active: tab === 'editor' }" @click="tab = 'editor'">✏️ 编辑器</button>
    </div>

    <!-- 列表 -->
    <div class="card" v-show="tab === 'list'">
      <div class="card-header">
        <h3 class="card-title">工作流列表 <span class="badge badge-neutral">{{ page.total }}</span></h3>
        <div class="row" v-if="canManage">
          <button class="btn btn-primary btn-sm" @click="newFlow">＋ 新建</button>
          <button class="btn btn-ghost btn-sm" @click="loadList">🔄 刷新</button>
        </div>
      </div>
      <div v-if="!page.items?.length" class="empty">暂无工作流，去编辑器拖一个</div>
      <div v-for="w in page.items" :key="w.id" class="ticket-card" :class="{ active: sel?.id === w.id }"
           @click="selectFlow(w)">
        <div class="tc-header">
          <span class="badge" :class="w.enabled ? 'badge-success' : 'badge-neutral'">{{ w.enabled ? '启用' : '停用' }}</span>
          <span class="tc-title">{{ w.name }}</span>
          <span class="badge badge-info">{{ (w.graph?.nodes || []).length }} 节点</span>
          <span class="hint" style="margin-left:auto">{{ w.updatedAt?.slice(0, 16).replace('T', ' ') }}</span>
        </div>
        <div class="tc-body" v-if="w.description"><span class="hint">{{ w.description }}</span></div>
        <div class="td-actions" style="margin-top:8px" v-if="canManage">
          <button class="btn btn-primary btn-sm" @click.stop="openRun(w)">🚀 运行</button>
          <button class="btn btn-ghost btn-sm" @click.stop="editFlow(w)">✏️ 编辑</button>
          <button class="btn btn-ghost btn-sm" @click.stop="toggleEnabled(w)">{{ w.enabled ? '停用' : '启用' }}</button>
          <button class="btn btn-danger btn-sm" @click.stop="delFlow(w)">🗑</button>
        </div>
      </div>
    </div>

    <!-- 编辑器 -->
    <div class="card" v-show="tab === 'editor'">
      <div class="card-header">
        <h3 class="card-title">{{ draft.id ? '编辑' : '新建' }}：{{ draft.name || '未命名' }}
          <span class="badge badge-neutral">{{ draft.nodes.length }} 节点 · {{ draft.edges.length }} 连线</span></h3>
        <div class="row" style="gap:6px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': linkMode }" @click="toggleLinkMode">
            {{ linkMode ? (linkFrom ? '点目标节点…' : '🔗 连线中：点源节点') : '🔗 连线' }}</button>
          <button class="btn btn-ghost btn-sm" :disabled="!selNode" @click="delNode">✕ 删节点</button>
          <button class="btn btn-ghost btn-sm" :disabled="!selEdge" @click="delEdge">✕ 删连线</button>
          <input class="input" style="max-width:150px" v-model="draft.name" placeholder="名称 *" />
          <button class="btn btn-primary btn-sm" :disabled="!canSave || saving" @click="save">{{ saving ? '保存中…' : '💾 保存' }}</button>
        </div>
      </div>
      <div class="wf-layout">
        <!-- 节点面板 -->
        <div class="wf-palette">
          <div class="src-head">节点类型</div>
          <button v-for="t in nodeTypes" :key="t.type" class="wf-pal-item" @click="addNode(t.type)"
                  :title="t.desc" :disabled="!canManage">
            <b>{{ typeIcon(t.type) }} {{ t.label }}</b>
            <span class="hint">{{ t.desc }}</span>
          </button>
          <div class="src-head" style="margin-top:10px">说明</div>
          <div class="hint" style="padding:0 2px">拖动节点摆位；「连线」模式点源再点目标；input 需为唯一起点，output 为终点。</div>
        </div>
        <!-- 画布 -->
        <div class="wf-canvas" ref="canvasEl" @mousedown.self="selNode = selEdge = null">
          <svg class="wf-svg" :width="canvasW" :height="canvasH">
            <path v-for="e in draft.edges" :key="e.id"
                  :d="edgePath(e)" :class="{ 'wf-edge-sel': selEdge === e.id }"
                  @mousedown.stop="selEdge = e.id; selNode = null" />
            <template v-for="e in draft.edges" :key="'t' + e.id">
              <text v-if="e.branch" :x="edgeMid(e).x" :y="edgeMid(e).y"
                    class="wf-edge-label">{{ e.branch }}</text>
            </template>
          </svg>
          <div v-for="n in draft.nodes" :key="n.id" class="wf-node" :class="[`wf-${n.type}`, { sel: selNode === n.id }]"
               :style="{ left: n.x + 'px', top: n.y + 'px' }"
               @mousedown.stop="onNodeDown($event, n)" @click.stop="clickNode(n)">
            <div class="wf-node-head">{{ typeIcon(n.type) }} {{ n.label || n.type }}</div>
            <div class="wf-node-sub hint" v-if="n.key">{{ n.key }}</div>
          </div>
          <div v-if="!draft.nodes.length" class="empty" style="padding:60px 0">从左侧点击添加节点</div>
        </div>
        <!-- 属性面板 -->
        <div class="wf-props">
          <div class="src-head">属性 {{ selNode ? '' : '（点选节点）' }}</div>
          <template v-if="selNodeObj">
            <div class="field"><label class="field-label">显示名</label>
              <input class="input" v-model="selNodeObj.label" /></div>
            <div class="field" v-if="selNodeObj.type !== 'input' && selNodeObj.type !== 'output'">
              <label class="field-label">key（检索字段/模型/persona）</label>
              <input class="input" v-model="selNodeObj.key"
                     :placeholder="keyPlaceholder(selNodeObj.type)" :list="selNodeObj.type === 'agent' ? 'persona-list' : null" /></div>
            <div class="field"><label class="field-label">params（JSON）</label>
              <textarea class="input" rows="4" style="width:100%;resize:vertical;font-family:monospace"
                        v-model="selParamsText" /></div>
            <div class="hint">{{ paramsHint(selNodeObj.type) }}</div>
            <datalist id="persona-list">
              <option v-for="p in personas" :key="p" :value="p" />
            </datalist>
          </template>
          <template v-else>
            <div class="hint" style="padding:8px 2px">选中画布节点编辑参数；选中连线后可删除。</div>
          </template>
        </div>
      </div>
    </div>

    <!-- 运行对话框 -->
    <div v-if="runDlg" class="modal-mask" @click.self="runDlg = null; stopRunPoll()">
      <div class="card" style="max-width:640px;margin:8vh auto">
        <div class="card-header">
          <h3 class="card-title">🚀 运行：{{ runDlg.name }}</h3>
          <button class="btn btn-ghost btn-sm" @click="runDlg = null; stopRunPoll()">✕</button>
        </div>
        <div class="row" style="gap:8px">
          <input class="input" style="flex:1" v-model="runQuery" placeholder="运行入参 query，如：1号主变油温高怎么处置" />
          <button class="btn btn-primary btn-sm" :disabled="!runQuery.trim() || runStarting" @click="doRun">
            {{ runStarting ? '启动中…' : '运行' }}</button>
        </div>
        <div v-if="runData" style="margin-top:12px">
          <div class="td-row">
            <span class="badge" :class="runBadge(runData.status)">{{ runLabel(runData.status) }}</span>
            <span class="hint" style="margin-left:8px">耗时 {{ runData.durationMs || 0 }}ms</span>
          </div>
          <div class="src-head" style="margin-top:8px">节点执行</div>
          <div v-for="st in runData.nodeStates" :key="st.nodeId" class="cause" style="align-items:flex-start">
            <span class="badge" :class="nodeStateBadge(st.status)">{{ st.status }}</span>
            <span style="font-weight:600">{{ st.label || st.key || st.type }}</span>
            <span class="hint" style="margin-left:auto">{{ st.ms }}ms</span>
            <div class="hint" style="width:100%;white-space:pre-wrap;max-height:90px;overflow-y:auto"
                 v-if="st.output || st.error">{{ (st.error || st.output || '').slice(0, 400) }}</div>
          </div>
          <div v-if="runData.output" class="src-head" style="margin-top:8px">最终输出</div>
          <div v-if="runData.output" class="hint" style="white-space:pre-wrap">{{ runData.output.slice(0, 600) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import {
  createWorkflow, deleteWorkflow, getWorkflowMeta, getWorkflowRun,
  getWorkflows, runWorkflow, updateWorkflow,
} from '../api'

const auth = useAuthStore()
const canManage = computed(() => hasPerm(auth.role, 'workflow:manage'))

const tab = ref('list')
const page = ref({ total: 0, items: [] })
const sel = ref(null)
const nodeTypes = ref([])
const personas = ref([])
const saving = ref(false)

const canvasW = 900
const canvasH = 460
const canvasEl = ref(null)
const draft = ref({ id: null, name: '', description: '', nodes: [], edges: [] })
const selNode = ref(null)
const selEdge = ref(null)
const linkMode = ref(false)
const linkFrom = ref(null)

const runDlg = ref(null)
const runQuery = ref('')
const runStarting = ref(false)
const runData = ref(null)
let runTimer = null

const selNodeObj = computed(() => draft.value.nodes.find(n => n.id === selNode.value) || null)
const selParamsText = computed({
  get() {
    const n = selNodeObj.value
    if (!n) return ''
    return n.params && Object.keys(n.params).length ? JSON.stringify(n.params, null, 1) : ''
  },
  set(v) {
    const n = selNodeObj.value
    if (!n) return
    try { n.params = v.trim() ? JSON.parse(v) : {} } catch (e) { /* 编辑中非法 JSON 暂存跳过 */ }
  },
})
const canSave = computed(() =>
  canManage.value && draft.value.name.trim() && draft.value.nodes.length &&
  draft.value.nodes.some(n => n.type === 'input') && draft.value.nodes.some(n => n.type === 'output'))

async function loadList() {
  try { page.value = (await getWorkflows({ size: 50 })).data || { total: 0, items: [] } } catch (e) { /* 未启用/无权限静默 */ }
}
async function loadMeta() {
  try {
    const res = (await getWorkflowMeta()).data || {}
    nodeTypes.value = res.nodeTypes || []
    personas.value = res.personas || []
  } catch (e) { /* 同上 */ }
}

function newFlow() {
  draft.value = { id: null, name: '', description: '', nodes: [], edges: [] }
  selNode.value = selEdge.value = null
  tab.value = 'editor'
}
function editFlow(w) {
  draft.value = {
    id: w.id, name: w.name, description: w.description || '',
    nodes: (w.graph?.nodes || []).map(n => ({ params: {}, ...n })),
    edges: (w.graph?.edges || []).map(e => ({ ...e })),
  }
  selNode.value = selEdge.value = null
  tab.value = 'editor'
}
function selectFlow(w) { sel.value = w }

async function save() {
  if (!canSave.value || saving.value) return
  // 连通性前置校验：非 input 节点必须有入边（output 除外不强制出边）
  const withIn = new Set(draft.value.edges.map(e => e.target))
  const orphan = draft.value.nodes.find(n => n.type !== 'input' && !withIn.has(n.id))
  if (orphan) { alert(`节点「${orphan.label || orphan.type}」没有入边（连线断链）`); return }
  saving.value = true
  try {
    const payload = {
      name: draft.value.name.trim(), description: draft.value.description,
      graph: { nodes: draft.value.nodes, edges: draft.value.edges },
    }
    if (draft.value.id) await updateWorkflow(draft.value.id, payload)
    else {
      const res = await createWorkflow(payload)
      draft.value.id = res.data?.id
    }
    loadList()
  } finally { saving.value = false }
}
async function delFlow(w) {
  if (!confirm(`删除工作流「${w.name}」？`)) return
  await deleteWorkflow(w.id); loadList()
}
async function toggleEnabled(w) {
  await updateWorkflow(w.id, { name: w.name, enabled: !w.enabled }); loadList()
}

// ---- 画布交互 ----
let dragging = null
function addNode(type) {
  if (!canManage.value || linkMode.value) return  // 连线模式下点面板不加节点（防误触）
  const id = `n${Date.now().toString(36)}${Math.floor(Math.random() * 100)}`
  const t = nodeTypes.value.find(x => x.type === type) || {}
  draft.value.nodes.push({
    id, type, label: t.label || type, key: '', params: {},
    x: 60 + (draft.value.nodes.length % 4) * 190,
    y: 40 + Math.floor(draft.value.nodes.length / 4) * 110,
  })
  selNode.value = id; selEdge.value = null
}
function onNodeDown(ev, n) {
  if (!canManage.value || linkMode.value) return
  dragging = { id: n.id, ox: ev.clientX - n.x, oy: ev.clientY - n.y }
  const move = (e2) => {
    if (!dragging) return
    const nn = draft.value.nodes.find(x => x.id === dragging.id)
    if (nn) {
      nn.x = Math.max(0, Math.min(canvasW - 150, e2.clientX - dragging.ox))
      nn.y = Math.max(0, Math.min(canvasH - 60, e2.clientY - dragging.oy))
    }
  }
  const up = () => { dragging = null; window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}
function clickNode(n) {
  selNode.value = n.id; selEdge.value = null
  if (linkMode.value) {
    if (!linkFrom.value) { linkFrom.value = n.id }
    else if (linkFrom.value !== n.id) {
      const dup = draft.value.edges.some(e => e.source === linkFrom.value && e.target === n.id)
      if (!dup) draft.value.edges.push({ id: `e${Date.now().toString(36)}`, source: linkFrom.value, target: n.id })
      linkFrom.value = null
    }
  }
}
function toggleLinkMode() { linkMode.value = !linkMode.value; linkFrom.value = null }
function delNode() {
  const id = selNode.value
  draft.value.nodes = draft.value.nodes.filter(n => n.id !== id)
  draft.value.edges = draft.value.edges.filter(e => e.source !== id && e.target !== id)
  selNode.value = null
}
function delEdge() {
  draft.value.edges = draft.value.edges.filter(e => e.id !== selEdge.value)
  selEdge.value = null
}
function nodePos(id) {
  const n = draft.value.nodes.find(x => x.id === id) || { x: 0, y: 0 }
  return { x: n.x, y: n.y }
}
const NODE_W = 150
const NODE_H = 52
function edgePath(e) {
  const a = nodePos(e.source), b = nodePos(e.target)
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2
  const cx = (x1 + x2) / 2
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`
}
function edgeMid(e) {
  const a = nodePos(e.source), b = nodePos(e.target)
  return { x: (a.x + NODE_W + b.x) / 2, y: (a.y + b.y + NODE_H) / 2 - 4 }
}

// ---- 运行 ----
function openRun(w) { runDlg.value = w; runData.value = null; runQuery.value = '' }
async function doRun() {
  runStarting.value = true
  try {
    const res = await runWorkflow(runDlg.value.id, runQuery.value.trim())
    runData.value = res.data
    startRunPoll(res.data.id)
  } finally { runStarting.value = false }
}
function startRunPoll(runId) {
  stopRunPoll()
  runTimer = setInterval(async () => {
    try {
      const res = await getWorkflowRun(runId)
      runData.value = res.data
      if (res.data?.status !== 'running') stopRunPoll()
    } catch (e) { stopRunPoll() }
  }, 2000)
}
function stopRunPoll() { if (runTimer) { clearInterval(runTimer); runTimer = null } }
onUnmounted(stopRunPoll)

// ---- 展示辅助 ----
function typeIcon(t) {
  return { input: '📥', retrieval: '🔍', kg: '🧠', llm: '🤖', agent: '🕹️', condition: '🔀', output: '📤' }[t] || '📦'
}
function keyPlaceholder(t) {
  return { retrieval: '如 topk', llm: '模型名（空=默认）', agent: 'persona 名', condition: '如 contains' }[t] || ''
}
function paramsHint(t) {
  return {
    retrieval: '{"topk": 6}', kg: '{"depth": 3, "entity": ""}',
    llm: '{"temperature": 0.3}', agent: '{"maxSteps": 6}',
    condition: '{"op": "contains", "value": "故障"}',
    input: '{}（query 为运行入参）', output: '{}',
  }[t] || ''
}
function runBadge(s) {
  return { running: 'badge-warning', done: 'badge-success', failed: 'badge-danger', stopped: 'badge-neutral' }[s] || 'badge-neutral'
}
function runLabel(s) { return { running: '运行中', done: '完成', failed: '失败', stopped: '已停止' }[s] || s }
function nodeStateBadge(s) {
  return { done: 'badge-success', error: 'badge-danger', running: 'badge-warning', pending: 'badge-neutral', skipped: 'badge-info' }[s] || 'badge-neutral'
}

onMounted(() => { loadList(); loadMeta() })
</script>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 100; overflow: auto; }
.wf-layout { display: flex; gap: 10px; align-items: stretch; }
.wf-palette { width: 170px; flex: none; display: flex; flex-direction: column; gap: 6px; }
.wf-pal-item { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; text-align: left;
  padding: 7px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
  cursor: pointer; font-size: 12px; }
.wf-pal-item:hover { border-color: var(--primary); }
.wf-pal-item b { font-size: 12.5px; }
.wf-canvas { flex: 1; position: relative; border: 1px dashed var(--border); border-radius: 10px;
  overflow: hidden; background: var(--surface-2, rgba(127,127,127,.06)); min-height: 460px; }
.wf-svg { position: absolute; inset: 0; }
.wf-svg path { stroke: var(--text-muted, #888); stroke-width: 2; fill: none; cursor: pointer; }
.wf-svg path:hover, .wf-edge-sel { stroke: var(--danger, #e74c3c); stroke-width: 3; }
.wf-edge-label { font-size: 11px; fill: var(--text-soft); }
.wf-node { position: absolute; width: 150px; min-height: 52px; border: 2px solid var(--border);
  border-radius: 9px; background: var(--surface); box-shadow: var(--shadow-sm); cursor: grab;
  user-select: none; padding: 6px 9px; }
.wf-node.sel { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }
.wf-input { border-left: 4px solid var(--info); }
.wf-output { border-left: 4px solid var(--success); }
.wf-retrieval { border-left: 4px solid var(--primary); }
.wf-llm, .wf-agent { border-left: 4px solid #9b59b6; }
.wf-kg { border-left: 4px solid #e67e22; }
.wf-condition { border-left: 4px solid var(--warning); }
.wf-node-head { font-size: 12.5px; font-weight: 700; }
.wf-node-sub { font-size: 11px; }
.wf-props { width: 210px; flex: none; }
@media (max-width: 900px) { .wf-layout { flex-direction: column; } .wf-palette, .wf-props { width: auto; } }
</style>
