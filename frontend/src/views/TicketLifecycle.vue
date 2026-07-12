<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'tickets' }" @click="loadTickets(); tab = 'tickets'">📋 票据列表</button>
      <button class="tab" :class="{ active: tab === 'create' }" @click="tab = 'create'">✏️ 新建两票</button>
      <button class="tab" :class="{ active: tab === 'stats' }" @click="loadStats(); tab = 'stats'">📊 统计看板</button>
    </div>

    <!-- 统计看板 -->
    <div class="card" v-show="tab === 'stats'">
      <div v-if="stats" class="stats-grid">
        <div class="stat stat-accent"><div class="stat-val">{{ stats.total }}</div><div class="stat-lbl">总票据</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ stats.byType?.操作票 || 0 }}</div><div class="stat-lbl">操作票</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ stats.byType?.工作票 || 0 }}</div><div class="stat-lbl">工作票</div></div>
        <div class="stat stat-accent"><div class="stat-val">{{ stats.avgReviewScore }}</div><div class="stat-lbl">平均审核分</div></div>
      </div>
      <div v-if="stats?.byStatus" class="status-dist" style="margin-top:12px">
        <div class="src-head">各状态分布</div>
        <div class="cause" v-for="(cnt, st) in stats.byStatus" :key="st">
          <span class="badge" :class="statusBadge(st)" style="width:80px;">{{ statusLabel(st) }}</span>
          <span style="font-weight:600">{{ cnt }} 张</span>
        </div>
      </div>
      <div v-else class="hint" style="margin-top:8px">暂无票据数据</div>
    </div>

    <!-- 新建两票 -->
    <div class="card" v-show="tab === 'create'">
      <div class="row" style="gap:8px;margin-bottom:10px">
        <label><input type="radio" v-model="form.type" value="操作票" /> 操作票</label>
        <label><input type="radio" v-model="form.type" value="工作票" /> 工作票</label>
      </div>
      <div class="field"><label class="field-label">操作任务 *</label><input class="input" v-model="form.task" placeholder="如：1号主变压器由运行转检修" /></div>
      <div class="field"><label class="field-label">涉及设备</label><input class="input" v-model="form.device" placeholder="如：1号主变" /></div>
      <div class="field"><label class="field-label">作业地点</label><input class="input" v-model="form.location" placeholder="如：220kV变电站" /></div>
      <div class="field"><label class="field-label">操作步骤（每行一步）</label><textarea class="input" v-model="form.stepsText" rows="4" style="width:100%;resize:vertical;font-family:inherit" placeholder="1. 确认操作条件&#10;2. 断开...&#10;3. ..."></textarea></div>
      <div class="field"><label class="field-label">安全措施（每行一条）</label><textarea class="input" v-model="form.safetyText" rows="3" style="width:100%;resize:vertical;font-family:inherit" placeholder="1. 验电接地&#10;2. ..."></textarea></div>
      <div class="field"><label class="field-label">风险点（每行一条）</label><textarea class="input" v-model="form.risksText" rows="2" style="width:100%;resize:vertical;font-family:inherit" placeholder="如：误操作风险&#10;触电风险"></textarea></div>
      <div class="field"><label class="field-label">备注</label><textarea class="input" v-model="form.notes" rows="2" style="width:100%;resize:vertical;font-family:inherit"></textarea></div>
      <button class="btn btn-primary" @click="handleCreate" :disabled="createLoading || !form.task.trim()">{{ createLoading ? '创建中…' : '创建草稿' }}</button>
    </div>

    <!-- 票据列表 -->
    <div class="card" v-show="tab === 'tickets'">
      <div class="card-header">
        <h3 class="card-title">票据列表 <span class="badge badge-neutral">{{ ticketList.total }}</span></h3>
        <div class="row">
          <select class="select" v-model="filterStatus" style="max-width:140px" @change="loadTickets">
            <option value="">全部状态</option>
            <option value="draft">草稿</option>
            <option value="pending_review">待审核</option>
            <option value="reviewed">已审核</option>
            <option value="issued">已签发</option>
            <option value="in_execution">执行中</option>
            <option value="completed">已完成</option>
            <option value="archived">已归档</option>
            <option value="rejected">驳回</option>
          </select>
          <button class="btn btn-ghost btn-sm" @click="loadTickets">🔄 刷新</button>
        </div>
      </div>
      <div v-if="!ticketList.list?.length" class="empty">暂无票据</div>
      <div class="ticket-card" v-for="t in ticketList.list" :key="t.id" @click="selectTicket(t)" :class="{ active: selected?.id === t.id }">
        <div class="tc-header">
          <span class="badge" :class="statusBadge(t.status)">{{ statusLabel(t.status) }}</span>
          <span class="tc-type">{{ t.ticketType }}</span>
          <span class="tc-title">{{ t.task?.slice(0, 40) || t.title }}</span>
          <span class="hint" style="margin-left:auto">{{ t.createdAt }}</span>
        </div>
        <div class="tc-body" v-if="t.device || t.creator">
          <span v-if="t.device" class="chip">📌 {{ t.device }}</span>
          <span v-if="t.creator" class="chip">👤 {{ t.creator }}</span>
          <span v-if="t.reviewScore" class="chip">得分 {{ t.reviewScore }}</span>
        </div>
      </div>
    </div>

    <!-- 票据详情 -->
    <div class="card" v-if="selected" style="margin-top:12px">
      <div class="card-header"><h3 class="card-title">📄 票据详情</h3></div>
      <div class="ticket-detail">
        <div class="td-row"><b>状态：</b><span class="badge" :class="statusBadge(selected.status)">{{ statusLabel(selected.status) }}</span></div>
        <div class="td-row"><b>类型：</b>{{ selected.ticketType }}</div>
        <div class="td-row"><b>任务：</b>{{ selected.task }}</div>
        <div class="td-row" v-if="selected.device"><b>设备：</b>{{ selected.device }}</div>
        <div class="td-row" v-if="selected.location"><b>地点：</b>{{ selected.location }}</div>
        <div class="td-block" v-if="selected.steps?.length"><b>操作步骤：</b><ol><li v-for="(s, i) in selected.steps" :key="i">{{ s }}</li></ol></div>
        <div class="td-block" v-if="selected.safety?.length"><b>安全措施：</b><ul><li v-for="(s, i) in selected.safety" :key="i">{{ s }}</li></ul></div>
        <div class="td-block" v-if="selected.risks?.length"><b>风险点：</b><span class="badge badge-danger" v-for="r in selected.risks" :key="r" style="margin:2px">{{ r }}</span></div>
        <div class="td-row" v-if="selected.notes"><b>备注：</b>{{ selected.notes }}</div>
        <div class="td-row" v-if="selected.reviewScore"><b>审核得分：</b>{{ selected.reviewScore }}</div>
        <div class="td-row" v-if="selected.reviewComment"><b>审核意见：</b>{{ selected.reviewComment }}</div>
        <div class="td-row" v-if="selected.auditReport?.items?.length"><b>审核报告：</b><span class="hint">{{ selected.auditReport.items.length }} 项</span></div>
        <div class="td-row" v-if="selected.executionLog"><b>执行记录：</b>{{ selected.executionLog }}</div>
        <div class="td-row" v-if="selected.deviation"><b>偏差：</b>{{ selected.deviation }}</div>

        <div class="td-actions" style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">
          <template v-if="selected.status === 'draft'">
            <button class="btn btn-primary btn-sm" @click="handleSubmit(selected.id)">📤 提交审核</button>
          </template>
          <template v-if="selected.status === 'pending_review' && can('system:config')">
            <button class="btn btn-success btn-sm" @click="handleReview(selected.id, true)">✅ 通过</button>
            <button class="btn btn-danger btn-sm" @click="handleReview(selected.id, false)">❌ 驳回</button>
          </template>
          <template v-if="selected.status === 'reviewed'">
            <button class="btn btn-primary btn-sm" @click="handleIssue(selected.id)">📨 签发</button>
          </template>
          <template v-if="selected.status === 'issued'">
            <button class="btn btn-primary btn-sm" @click="handleStartExec(selected.id)">▶️ 开始执行</button>
          </template>
          <template v-if="selected.status === 'in_execution'">
            <button class="btn btn-success btn-sm" @click="showCompleteDialog = true">✅ 完成执行</button>
          </template>
          <template v-if="selected.status === 'completed'">
            <button class="btn btn-ghost btn-sm" @click="handleArchive(selected.id)">📦 归档</button>
          </template>
          <button class="btn btn-ghost btn-sm" @click="handleDelete(selected.id)" style="color:var(--danger)">🗑️ 删除</button>
        </div>
      </div>

      <!-- 完成执行对话框 -->
      <div class="modal-overlay" v-if="showCompleteDialog" @click.self="showCompleteDialog = false">
        <div class="modal">
          <h3>完成执行</h3>
          <div class="field"><label class="field-label">执行日志</label><textarea class="input" v-model="execLog" rows="4" style="width:100%;font-family:inherit;resize:vertical" placeholder="记录执行过程和关键操作"></textarea></div>
          <div class="field"><label class="field-label">偏差记录</label><textarea class="input" v-model="execDeviation" rows="2" style="width:100%;font-family:inherit;resize:vertical" placeholder="与规程不一致之处"></textarea></div>
          <div class="row" style="margin-top:10px;gap:8px">
            <button class="btn btn-primary" @click="handleCompleteExec(selected.id)">确认完成</button>
            <button class="btn btn-ghost" @click="showCompleteDialog = false">取消</button>
          </div>
        </div>
      </div>
    </div>
    <div class="toast" v-if="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { createTicket, listTickets, getTicket, submitTicket, reviewTicket, issueTicket, executeTicket, archiveTicket, deleteTicket, getTicketStats } from '../api'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'

const auth = useAuthStore()
const can = (p) => hasPerm(auth.role, p)   // RBAC：两票审核(system:config)仅管理员

const tab = ref('tickets')
const toast = ref('')
let toastTimer = null
function show(m) { toast.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toast.value = ''), 2000) }

// 统计
const stats = ref(null)
async function loadStats() {
  try { stats.value = (await getTicketStats()).data } catch { /* silent */ }
}

// 新建
const form = ref({ type: '操作票', task: '', device: '', location: '', stepsText: '', safetyText: '', risksText: '', notes: '' })
const createLoading = ref(false)
async function handleCreate() {
  if (!form.value.task.trim()) return
  createLoading.value = true
  try {
    const steps = form.value.stepsText.split('\n').filter(s => s.trim()).map(s => s.replace(/^\d+[.、]\s*/, ''))
    const safety = form.value.safetyText.split('\n').filter(s => s.trim()).map(s => s.replace(/^\d+[.、]\s*/, ''))
    const risks = form.value.risksText.split('\n').filter(s => s.trim()).map(s => s.replace(/^\d+[.、]\s*/, ''))
    const r = (await createTicket({
      ticketType: form.value.type, task: form.value.task, device: form.value.device,
      location: form.value.location, steps, safety, risks, notes: form.value.notes,
    })).data
    show('创建成功')
    tab.value = 'tickets'
    loadTickets()
  } catch (e) { show('创建失败') } finally { createLoading.value = false }
}

// 列表
const ticketList = ref({ total: 0, list: [] })
const filterStatus = ref('')
async function loadTickets() {
  try { ticketList.value = (await listTickets({ status: filterStatus.value, page: 1, size: 20 })).data } catch { /* silent */ }
}

// 详情
const selected = ref(null)
const showCompleteDialog = ref(false)
const execLog = ref('')
const execDeviation = ref('')
async function selectTicket(t) {
  try { selected.value = (await getTicket(t.id)).data } catch { show('加载失败') }
}
async function handleSubmit(id) { try { selected.value = (await submitTicket(id)).data; show('已提交审核'); loadTickets() } catch (e) { show('操作失败') } }
async function handleReview(id, approved) {
  const comment = approved ? '' : prompt('驳回理由：')
  if (!approved && comment === null) return
  try { selected.value = (await reviewTicket(id, approved, comment || '')).data; show(approved ? '已通过' : '已驳回'); loadTickets() } catch (e) { show('操作失败') }
}
async function handleIssue(id) { try { selected.value = (await issueTicket(id)).data; show('已签发'); loadTickets() } catch (e) { show('操作失败') } }
async function handleStartExec(id) { try { selected.value = (await executeTicket(id, {})).data; show('开始执行'); loadTickets() } catch (e) { show('操作失败') } }
async function handleCompleteExec(id) {
  try {
    selected.value = (await executeTicket(id, { log: execLog.value || '已完成', deviation: execDeviation.value || '' })).data
    show('执行完成'); showCompleteDialog.value = false; loadTickets()
  } catch (e) { show('操作失败') }
}
async function handleArchive(id) { try { selected.value = (await archiveTicket(id)).data; show('已归档'); loadTickets() } catch (e) { show('操作失败') } }
async function handleDelete(id) { if (!confirm('确认删除？')) return; try { await deleteTicket(id); selected.value = null; show('已删除'); loadTickets() } catch (e) { show('删除失败') } }

// 辅助
function statusLabel(s) {
  return { draft: '草稿', pending_review: '待审核', reviewed: '已审核', issued: '已签发', in_execution: '执行中', completed: '已完成', archived: '已归档', rejected: '驳回' }[s] || s
}
function statusBadge(s) {
  return { draft: 'badge badge-neutral', pending_review: 'badge badge-warning', reviewed: 'badge badge-info', issued: 'badge badge-primary', in_execution: 'badge badge-accent', completed: 'badge badge-success', archived: 'badge badge-neutral', rejected: 'badge badge-danger' }[s] || 'badge badge-neutral'
}

onMounted(() => { loadTickets(); loadStats() })
</script>

<style scoped>
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.ticket-card { background: var(--surface-2); padding: 8px 12px; border-radius: var(--radius-sm); margin-bottom: 6px; cursor: pointer; border: 1px solid transparent; }
.ticket-card:hover { border-color: var(--primary); }
.ticket-card.active { border-color: var(--primary); background: var(--primary-soft); }
.tc-header { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tc-type { font-size: 11px; color: var(--text-muted); }
.tc-title { font-weight: 600; font-size: 13px; }
.tc-body { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.chip { font-size: 11px; color: var(--text-muted); }
.ticket-detail { font-size: 13px; }
.td-row { margin-bottom: 4px; }
.td-block { margin-bottom: 8px; }
.td-block ol, .td-block ul { margin: 4px 0; padding-left: 20px; }
.td-actions button { font-size: 12px; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { background: var(--surface); padding: 20px; border-radius: var(--radius); min-width: 360px; max-width: 520px; }
</style>