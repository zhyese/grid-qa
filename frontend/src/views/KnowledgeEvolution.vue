<template>
  <div class="evo-page">
    <div class="toolbar">
      <div class="stats">
        <span class="badge badge-neutral">草稿 {{ stats.byStatus?.draft || 0 }}</span>
        <span class="badge badge-warning">待审 {{ stats.byStatus?.approved || 0 }}</span>
        <span class="badge badge-success">已回流 {{ stats.byStatus?.indexed || 0 }}</span>
        <span class="badge badge-info">已驳回 {{ stats.byStatus?.rejected || 0 }}</span>
        <span class="badge badge-neutral">已撤回 {{ stats.byStatus?.withdrawn || 0 }}</span>
      </div>
      <div class="actions">
        <select v-model="filter.status"><option value="">全部状态</option><option v-for="s in statuses" :key="s" :value="s">{{ statusLabel(s) }}</option></select>
        <button class="btn btn-primary" :disabled="scanning" @click="triggerScan">{{ scanning ? '扫描中…' : '🧬 触发盲区扫描' }}</button>
      </div>
    </div>

    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>

    <div class="draft-list">
      <div v-for="d in drafts.list" :key="d.id" class="draft-card">
        <div class="draft-head">
          <span :class="statusBadge(d.status)">{{ statusLabel(d.status) }}</span>
          <span class="rep">{{ d.representativeQuery }}</span>
          <span class="meta">{{ d.memberQueries?.length || 0 }} 条相似疑问 · 得分 {{ (d.gapEvidence?.top1_score || 0).toFixed(2) }} · {{ fmt(d.createdAt) }}</span>
        </div>
        <div class="draft-body">
          <div class="title">{{ d.draftTitle || '（无标题）' }}</div>
          <pre class="content">{{ d.draftContent }}</pre>
          <div class="members">相似疑问：{{ (d.memberQueries || []).join(' | ') }}</div>
        </div>
        <div class="draft-foot">
          <template v-if="d.status === 'draft'">
            <button class="btn btn-success" @click="review(d.id, 'approve')">✓ 批准回流</button>
            <button class="btn btn-danger" @click="review(d.id, 'reject')">✗ 驳回</button>
          </template>
          <template v-else-if="d.status === 'approved'">
            <span class="hint">已批准，worker 将回流（或手动触发）</span>
          </template>
          <template v-else-if="d.status === 'indexed'">
            <button class="btn btn-warning" @click="withdraw(d.id)">↩ 撤回（删向量）</button>
          </template>
          <span v-else class="hint">{{ d.reviewer ? `审核人 ${d.reviewer}` : '' }}</span>
        </div>
      </div>
      <div v-if="!drafts.list?.length" class="empty">暂无草稿。dislike 累计成簇后触发扫描可生成增量知识草稿。</div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import { getEvolutionDrafts, getEvolutionStats, scanKnowledgeEvolution, reviewEvolutionDraft, withdrawEvolutionDraft } from '../api'

const auth = useAuthStore()
const canManage = hasPerm(auth.role, 'doc:manage')
const drafts = ref({ total: 0, list: [] })
const stats = ref({ byStatus: {} })
const filter = reactive({ status: '' })
const scanning = ref(false)
const toastMsg = ref('')
const statuses = ['draft', 'approved', 'indexed', 'rejected', 'withdrawn']
let toastTimer, refreshTimer

const toast = (m) => { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 2200) }
const statusLabel = (s) => ({ draft: '待审核', approved: '已批准', indexed: '已回流', rejected: '已驳回', withdrawn: '已撤回' })[s] || s
const statusBadge = (s) => `badge ${({ draft: 'badge-warning', approved: 'badge-info', indexed: 'badge-success', rejected: 'badge-danger', withdrawn: 'badge-neutral' })[s] || 'badge-neutral'}`
const fmt = (t) => t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : ''

async function load() {
  try {
    const [d, s] = await Promise.all([getEvolutionDrafts({ status: filter.status, size: 50 }), getEvolutionStats()])
    drafts.value = d.data || { total: 0, list: [] }
    stats.value = s.data || { byStatus: {} }
  } catch { toast('加载失败') }
}
async function triggerScan() {
  if (!canManage) return toast('无权限')
  scanning.value = true
  try { const r = await scanKnowledgeEvolution({ sinceHours: 168 }); toast(r.data?.task ? '扫描已入队，稍后刷新查看' : '扫描完成'); setTimeout(load, 3000) }
  catch { toast('扫描失败') } finally { scanning.value = false }
}
async function review(id, action) {
  if (!canManage) return
  const note = action === 'reject' ? (prompt('驳回原因（可选）：') || '') : ''
  try { await reviewEvolutionDraft(id, action, note); toast(action === 'approve' ? '已批准，待回流' : '已驳回'); await load() }
  catch (e) { toast(e.response?.data?.message || '操作失败') }
}
async function withdraw(id) {
  if (!confirm('撤回将从知识库删除该 AI 草稿向量，确认？')) return
  try { await withdrawEvolutionDraft(id); toast('已撤回'); await load() }
  catch (e) { toast(e.response?.data?.message || '撤回失败') }
}

onMounted(() => { load(); refreshTimer = setInterval(load, 10000) })
onUnmounted(() => { clearInterval(refreshTimer); clearTimeout(toastTimer) })
</script>

<style scoped>
.evo-page { display: flex; flex-direction: column; gap: 14px; height: calc(100vh - var(--topbar-h) - 8px); overflow: auto; }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.stats { display: flex; gap: 6px; flex-wrap: wrap; }
.actions { display: flex; gap: 8px; align-items: center; }
.draft-list { display: flex; flex-direction: column; gap: 10px; }
.draft-card { background: var(--card-bg, #fff); border: 1px solid var(--border, #e3e3e3); border-radius: 10px; padding: 14px; }
.draft-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.draft-head .rep { font-weight: 600; flex: 1; min-width: 200px; }
.draft-head .meta { font-size: 12px; color: #888; }
.draft-body .title { font-weight: 600; margin-bottom: 4px; }
.draft-body .content { white-space: pre-wrap; background: var(--bg-soft, #f6f7f9); padding: 8px 10px; border-radius: 6px; font-size: 13px; max-height: 180px; overflow: auto; margin: 0; }
.draft-body .members { font-size: 12px; color: #999; margin-top: 6px; }
.draft-foot { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
.hint { font-size: 12px; color: #888; }
.empty { text-align: center; color: #999; padding: 40px; }
.toast { position: fixed; top: 70px; right: 20px; background: #333; color: #fff; padding: 8px 14px; border-radius: 6px; z-index: 99; }
.btn { padding: 5px 12px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-primary { background: #3498db; color: #fff; }
.btn-success { background: #2ecc71; color: #fff; }
.btn-danger { background: #e74c3c; color: #fff; }
.btn-warning { background: #f39c12; color: #fff; }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge-warning { background: #f39c12; color: #fff; }
.badge-info { background: #3498db; color: #fff; }
.badge-success { background: #2ecc71; color: #fff; }
.badge-danger { background: #e74c3c; color: #fff; }
.badge-neutral { background: #bdc3c7; color: #fff; }
</style>
