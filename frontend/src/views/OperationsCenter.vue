<template>
  <div>
    <div class="stat-grid ops-stats">
      <div class="stat stat-accent"><div class="stat-val">{{ runs.total || 0 }}</div><div class="stat-lbl">主动运维记录</div></div>
      <div class="stat"><div class="stat-val warning">{{ proposedCount }}</div><div class="stat-lbl">待人工确认</div></div>
      <div class="stat"><div class="stat-val">{{ events.total || 0 }}</div><div class="stat-lbl">实时事件</div></div>
      <div class="stat"><div class="stat-val danger">{{ deadTaskLabel }}</div><div class="stat-lbl">{{ canAudit ? '死信任务' : '任务视图需审计权限' }}</div></div>
    </div>

    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'runs' }" @click="tab='runs'; loadRuns()">处置建议</button>
      <button class="tab" :class="{ active: tab === 'events' }" @click="tab='events'; loadEvents()">实时事件</button>
      <button v-if="can('system:config')" class="tab" :class="{ active: tab === 'mapping' }" @click="tab='mapping'; loadMappings()">设备映射</button>
      <button v-if="can('audit:read')" class="tab" :class="{ active: tab === 'tasks' }" @click="tab='tasks'; loadTasks()">任务与事件</button>
    </div>

    <div class="card" v-show="tab === 'runs'">
      <div class="card-header">
        <div><h3 class="card-title">主动运维建议</h3><div class="card-desc">Agent 仅做只读分析；确认后也只会生成两票草稿。</div></div>
        <button class="btn btn-ghost btn-sm" @click="loadRuns">刷新</button>
      </div>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr><th>状态</th><th>事件 / 设备</th><th>建议摘要</th><th>证据</th><th>安全边界</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <template v-for="r in runs.list" :key="r.id">
            <tr class="run-row"
                :class="{ open: expandedRunId === r.id }"
                @click="expandedRunId = expandedRunId === r.id ? '' : r.id">
              <td><span class="badge" :class="statusBadge(r.status)">{{ statusLabel(r.status) }}</span></td>
              <td class="main-cell"><b>{{ r.event?.title || '未命名事件' }}</b><small>{{ r.event?.device?.canonicalName || r.event?.device?.sourceDeviceId || '未映射设备' }} · {{ r.event?.source }}</small></td>
              <td class="wide-cell">{{ r.recommendation?.summary || r.recommendation?.handling || r.errorMessage || '分析中…' }}</td>
              <td><span v-if="r.evidence?.toolsUsed?.length" class="badge badge-info">{{ r.evidence.toolsUsed.length }} 个工具</span><span v-else class="muted">—</span></td>
              <td><span class="badge badge-success">只读</span> <span v-if="r.requiresHumanReview" class="badge badge-warning">需人审</span></td>
              <td class="muted">{{ r.createdAt }}</td>
              <td class="actions" @click.stop>
                <button v-if="can('alert:manage') && r.status==='proposed'" class="btn btn-primary btn-sm" @click="confirmRun(r)">确认</button>
                <button v-if="can('alert:manage') && ['proposed','confirmed'].includes(r.status)" class="btn btn-ghost btn-sm" @click="rejectRun(r)">驳回</button>
                <button v-if="can('alert:manage') && r.status==='confirmed'" class="btn btn-success btn-sm" @click="toTicket(r)">转两票草稿</button>
                <button v-if="can('alert:manage') && r.status==='failed'" class="btn btn-ghost btn-sm" @click="retryRun(r)">重试</button>
                <span v-if="r.ticketId" class="badge badge-neutral">票 {{ r.ticketId.slice(-6) }}</span>
              </td>
            </tr>
            <tr v-if="expandedRunId === r.id">
              <td colspan="7" class="run-detail">
                <template v-if="isV2(r)">
                  <div class="rec-head">
                    <b>{{ r.recommendation.summary || '（无摘要）' }}</b>
                    <span class="badge" :class="levelBadge(r.recommendation.confidence)">
                      置信 {{ levelLabel(r.recommendation.confidence) }}
                    </span>
                  </div>
                  <table class="tbl inner">
                    <thead><tr><th>根因</th><th>可能性</th><th>证据</th><th>处置建议</th></tr></thead>
                    <tbody>
                      <tr v-for="(c, i) in r.recommendation.rootCauses || []" :key="i">
                        <td>{{ c.name }}</td>
                        <td><span class="badge" :class="levelBadge(c.likelihood)">{{ levelLabel(c.likelihood) }}</span></td>
                        <td><ul class="evi"><li v-for="(e, j) in c.evidence || []" :key="j">{{ e }}</li></ul></td>
                        <td>{{ c.handling }}</td>
                      </tr>
                      <tr v-if="!(r.recommendation.rootCauses || []).length"><td colspan="4" class="empty">模型未给出根因列表</td></tr>
                    </tbody>
                  </table>
                  <div class="chip-group" v-if="(r.recommendation.steps || []).length">
                    <span class="lbl">步骤</span>
                    <span v-for="(s, i) in r.recommendation.steps" :key="'s' + i" class="chip">{{ s }}</span>
                  </div>
                  <div class="chip-group" v-if="(r.recommendation.safety || []).length">
                    <span class="lbl">安全</span>
                    <span v-for="(s, i) in r.recommendation.safety" :key="'f' + i" class="chip safe">{{ s }}</span>
                  </div>
                  <div class="chip-group" v-if="(r.recommendation.risks || []).length">
                    <span class="lbl lbl-danger">风险</span>
                    <span v-for="(s, i) in r.recommendation.risks" :key="'r' + i" class="chip risk">{{ s }}</span>
                  </div>
                  <div class="basis" v-if="(r.recommendation.basis || []).length">
                    依据：{{ r.recommendation.basis.join('；') }}
                  </div>
                </template>
                <template v-else>
                  <!-- v1（alert persona 自由 JSON）回退现状展示 -->
                  <div class="rec-head"><b>{{ r.recommendation?.summary || r.errorMessage || '分析中…' }}</b></div>
                  <div class="rec-v1" v-if="r.recommendation?.handling">{{ r.recommendation.handling }}</div>
                </template>
              </td>
            </tr>
            </template>
            <tr v-if="!runs.list?.length"><td colspan="7" class="empty">暂无主动运维记录</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" v-show="tab === 'events'">
      <div class="card-header"><div><h3 class="card-title">统一实时事件流</h3><div class="card-desc">SCADA / OMS / PMS / generic，按租户和源事件 ID 幂等接收。</div></div><button class="btn btn-ghost btn-sm" @click="loadEvents">刷新</button></div>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr><th>等级</th><th>来源</th><th>事件</th><th>规范设备</th><th>门禁</th><th>处理状态</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="e in events.list" :key="e.id">
              <td><span class="badge" :class="severityBadge(e.severity)">{{ e.severity }}</span></td>
              <td><span class="badge badge-neutral">{{ (e.source || '').toUpperCase() }}</span></td>
              <td class="main-cell"><b>{{ e.title || e.eventType }}</b><small>{{ e.summary }}</small></td>
              <td class="main-cell"><b>{{ e.device?.canonicalName || e.device?.canonicalDeviceId }}</b><small :class="e.device?.mapped ? 'ok' : 'warning'">{{ e.device?.mapped ? '已映射' : '未映射' }}</small></td>
              <td><span class="badge" :class="e.ruleDecision==='trigger' ? 'badge-warning':'badge-neutral'">{{ e.ruleDecision }}</span><div class="reason">{{ e.ruleReason }}</div></td>
              <td><span class="badge" :class="statusBadge(e.processingStatus)">{{ statusLabel(e.processingStatus) }}</span><small v-if="e.duplicateCount">重复 {{ e.duplicateCount }}</small></td>
              <td class="muted">{{ e.occurredAt }}</td>
            </tr>
            <tr v-if="!events.list?.length"><td colspan="7" class="empty">暂无实时事件</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="can('system:config')" class="card" v-show="tab === 'mapping'">
      <div class="card-header"><div><h3 class="card-title">设备身份映射</h3><div class="card-desc">将各业务系统的设备编码统一到平台规范设备 ID。</div></div><button class="btn btn-ghost btn-sm" @click="loadMappings">刷新</button></div>
      <div class="mapping-form">
        <select class="select" v-model="mappingForm.source"><option value="scada">SCADA</option><option value="oms">OMS</option><option value="pms">PMS</option><option value="generic">通用</option></select>
        <input class="input" v-model="mappingForm.sourceDeviceId" placeholder="源设备 ID" />
        <input class="input" v-model="mappingForm.canonicalDeviceId" placeholder="规范设备 ID" />
        <input class="input" v-model="mappingForm.canonicalName" placeholder="设备名称" />
        <input class="input" v-model="mappingForm.station" placeholder="站点" />
        <button class="btn btn-primary" @click="saveMapping">保存映射</button>
      </div>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>来源</th><th>源设备</th><th>规范设备</th><th>名称</th><th>站点</th><th>状态</th></tr></thead><tbody>
        <tr v-for="m in mappings.list" :key="m.id"><td>{{ m.source }}</td><td>{{ m.sourceDeviceId }}</td><td>{{ m.canonicalDeviceId }}</td><td>{{ m.canonicalName }}</td><td>{{ m.station }}</td><td><span class="badge" :class="m.active?'badge-success':'badge-neutral'">{{ m.active?'启用':'停用' }}</span></td></tr>
        <tr v-if="!mappings.list?.length"><td colspan="6" class="empty">暂无设备映射</td></tr>
      </tbody></table></div>
    </div>

    <div v-if="can('audit:read')" class="card" v-show="tab === 'tasks'">
      <div class="card-header"><div><h3 class="card-title">持久化任务与事件 Outbox</h3><div class="card-desc">失败自动退避重试，耗尽后进入死信；服务重启不会丢任务。</div></div><button class="btn btn-ghost btn-sm" @click="loadTasks">刷新</button></div>
      <div class="mini-stats"><span>排队 {{ taskStats.tasks?.queued || 0 }}</span><span>运行 {{ taskStats.tasks?.running || 0 }}</span><span>失败 {{ taskStats.tasks?.failed || 0 }}</span><span class="danger">死信 {{ taskStats.tasks?.dead || 0 }}</span><span>待投递事件 {{ taskStats.events?.pending || 0 }}</span></div>
      <div class="section-title"><span>任务队列</span><small>可恢复的后台工作</small></div>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>状态</th><th>队列</th><th>任务类型</th><th>尝试</th><th>错误</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
        <tr v-for="t in tasks.list" :key="t.id"><td><span class="badge" :class="statusBadge(t.status)">{{ statusLabel(t.status) }}</span></td><td>{{ t.queue }}</td><td>{{ t.taskType }}</td><td>{{ t.attempts }}/{{ t.maxAttempts }}</td><td class="error-cell">{{ t.lastError || '—' }}</td><td class="muted">{{ t.createdAt }}</td><td class="actions"><button v-if="can('system:config') && ['failed','dead'].includes(t.status)" class="btn btn-primary btn-sm" @click="retryTask(t)">重试</button><button v-if="can('system:config') && !['running','succeeded','dead'].includes(t.status)" class="btn btn-ghost btn-sm" @click="terminateTask(t)">终止</button></td></tr>
        <tr v-if="!tasks.list?.length"><td colspan="7" class="empty">暂无任务</td></tr>
      </tbody></table></div>
      <div class="section-title event-title"><span>领域事件流</span><small>业务变化的可追踪投递记录</small></div>
      <div class="table-wrap"><table class="tbl event-table"><thead><tr><th>状态</th><th>事件类型</th><th>来源</th><th>关联对象</th><th>尝试</th><th>发生时间</th><th>操作</th></tr></thead><tbody>
        <tr v-for="e in domainEvents.list" :key="e.id">
          <td><span class="badge" :class="statusBadge(e.status)">{{ statusLabel(e.status) }}</span></td>
          <td class="event-type">{{ e.eventType }}</td><td>{{ e.source }}</td>
          <td class="main-cell"><b>{{ e.aggregateType || '—' }}</b><small>{{ e.aggregateId || e.correlationId || '无关联对象' }}</small></td>
          <td>{{ e.attempts }}/{{ e.maxAttempts }}</td><td class="muted">{{ e.occurredAt }}</td>
          <td class="actions"><button v-if="can('system:config') && ['failed','dead'].includes(e.status)" class="btn btn-primary btn-sm" @click="retryEvent(e)">重新投递</button><span v-else-if="e.status==='published'" class="badge badge-success">已送达</span></td>
        </tr>
        <tr v-if="!domainEvents.list?.length"><td colspan="7" class="empty">暂无领域事件；实时事件接入后会在这里留下投递轨迹。</td></tr>
      </tbody></table></div>
    </div>
    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useRouter } from 'vue-router'
import { hasPerm } from '../utils/perm'
import {
  confirmProactiveRun, getDeviceMappings, getDomainEvents, getPersistentTasks,
  getProactiveRuns, getRealtimeEvents, getTaskCenterStats, proactiveRunToTicket,
  rejectProactiveRun, retryDomainEvent, retryPersistentTask, retryProactiveRun,
  saveDeviceMapping, terminatePersistentTask,
} from '../api'

const auth = useAuthStore()
const router = useRouter()
const can = (p) => hasPerm(auth.role, p)
const tab = ref('runs')
const runs = ref({ total: 0, list: [] })
const events = ref({ total: 0, list: [] })
const mappings = ref({ total: 0, list: [] })
const tasks = ref({ total: 0, list: [] })
const domainEvents = ref({ total: 0, list: [] })
const taskStats = ref({ tasks: {}, events: {} })
const mappingForm = reactive({ source: 'scada', sourceDeviceId: '', canonicalDeviceId: '', canonicalName: '', deviceType: '', station: '', active: true, metadata: {} })
const toastMsg = ref('')
let toastTimer
let refreshTimer
const toast = (text) => { toastMsg.value = text; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 2200) }
const proposedCount = computed(() => (runs.value.list || []).filter(r => r.status === 'proposed').length)
const canAudit = computed(() => can('audit:read'))
const deadTaskLabel = computed(() => canAudit.value ? (taskStats.value.tasks?.dead || 0) : '—')
// 运维建议行点击展开详情（仿 QaTraceChart 单开互斥；按 id 记忆，10s 轮询刷新后仍跟随原行）
const expandedRunId = ref('')
// v2 判据：schema 字段自识别；v1（无 schema）回退现状文本
function isV2(r) { return r?.recommendation?.schema === 'proactive-recommendation/v2' }
const levelLabel = (v) => ({ high: '高', medium: '中', low: '低' })[v] || v || '—'
const levelBadge = (v) => ({ high: 'badge-danger', medium: 'badge-warning', low: 'badge-info' })[v] || 'badge-neutral'

function unwrapBiz(response) {
  if (!response || response.code !== 200) throw new Error(response?.message || '请求失败')
  return response.data
}

function statusBadge(s) { return ({ proposed:'badge-warning', confirmed:'badge-info', ticketed:'badge-success', succeeded:'badge-success', completed:'badge-success', published:'badge-success', failed:'badge-danger', dead:'badge-danger', running:'badge-info', processing:'badge-info', dispatching:'badge-info', queued:'badge-neutral', pending:'badge-warning', ignored:'badge-neutral', rejected:'badge-neutral' })[s] || 'badge-neutral' }
function statusLabel(s) { return ({ proposed:'待确认', confirmed:'已确认', ticketed:'已转两票', succeeded:'成功', completed:'完成', published:'已投递', failed:'失败', dead:'死信', running:'运行中', processing:'处理中', dispatching:'投递中', queued:'排队中', pending:'待投递', ignored:'已忽略', rejected:'已驳回' })[s] || s }
function severityBadge(s) { return s === 'critical' || s === 'major' ? 'badge-danger' : s === 'warning' ? 'badge-warning' : 'badge-info' }

async function loadRuns() { try { const data = unwrapBiz(await getProactiveRuns({ size: 50 })) || runs.value; runs.value = data; const ids = new Set((data.list || []).map(r => r.id)); if (expandedRunId.value && !ids.has(expandedRunId.value)) expandedRunId.value = '' } catch { toast('主动运维记录加载失败') } }
async function loadEvents() { try { events.value = unwrapBiz(await getRealtimeEvents({ size: 50 })) || events.value } catch { toast('实时事件加载失败') } }
async function loadMappings() { if (!can('system:config')) return; try { mappings.value = unwrapBiz(await getDeviceMappings({ size: 100 })) || mappings.value } catch { toast('设备映射加载失败') } }
async function loadTasks() { if (!canAudit.value) return; try { const [list, eventList, stats] = await Promise.all([getPersistentTasks({ size: 50 }), getDomainEvents({ size: 50 }), getTaskCenterStats()]); tasks.value = unwrapBiz(list) || tasks.value; domainEvents.value = unwrapBiz(eventList) || domainEvents.value; taskStats.value = unwrapBiz(stats) || taskStats.value } catch { toast('任务与事件中心加载失败') } }
async function refresh() { await Promise.all([loadRuns(), loadEvents(), canAudit.value ? loadTasks() : Promise.resolve()]) }
async function confirmRun(r) { try { unwrapBiz(await confirmProactiveRun(r.id)); toast('建议已确认，尚未执行任何设备控制'); await loadRuns() } catch { toast('确认失败') } }
async function rejectRun(r) { const note = prompt('请输入驳回原因：') ?? ''; if (!note) return; try { unwrapBiz(await rejectProactiveRun(r.id, note)); toast('已驳回'); await loadRuns() } catch { toast('驳回失败') } }
async function toTicket(r) { if (!confirm('仅创建两票草稿，仍需后续审核、签发和执行。继续？')) return; try { unwrapBiz(await proactiveRunToTicket(r.id)); toast('两票草稿已创建，正在跳转…'); await loadRuns(); setTimeout(() => router.push('/ticket'), 600) } catch { toast('创建两票草稿失败') } }
async function retryRun(r) { try { unwrapBiz(await retryProactiveRun(r.id)); toast('重试任务已入队'); await refresh() } catch { toast('重试失败') } }
async function saveMapping() { if (!mappingForm.sourceDeviceId || !mappingForm.canonicalDeviceId) return toast('请填写源设备 ID 和规范设备 ID'); try { unwrapBiz(await saveDeviceMapping({ ...mappingForm })); toast('设备映射已保存'); mappingForm.sourceDeviceId=''; mappingForm.canonicalDeviceId=''; mappingForm.canonicalName=''; await loadMappings() } catch { toast('保存失败') } }
async function retryTask(t) { try { unwrapBiz(await retryPersistentTask(t.id)); toast('任务已重新入队'); await loadTasks() } catch { toast('重试失败') } }
async function terminateTask(t) { if (!confirm('确认终止该任务并移入死信？')) return; try { unwrapBiz(await terminatePersistentTask(t.id)); toast('任务已终止'); await loadTasks() } catch { toast('终止失败') } }
async function retryEvent(e) { try { unwrapBiz(await retryDomainEvent(e.id)); toast('事件已重新投递'); await loadTasks() } catch { toast('事件重新投递失败') } }

let propWs = null
function connectProposalWs() {
  // 断点2 收尾：监听主动运维建议 WS 推送（后端 proactive_ops.proposed→ws_manager.broadcast），
  // 收到即刷新建议列表；10s 轮询保留作 WS 断连降级兜底。
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    propWs = new WebSocket(`${proto}://${location.host}/api/system/ws/alerts?token=${auth.token}`)
    propWs.onmessage = (e) => {
      try { const d = JSON.parse(e.data); if (d.type === 'proactive_proposal') { loadRuns(); toast('新运维建议已生成') } } catch (_) {}
    }
  } catch (_) {}
}
onMounted(() => { refresh(); refreshTimer = setInterval(refresh, 10000); connectProposalWs() })
onUnmounted(() => { clearInterval(refreshTimer); clearTimeout(toastTimer); try { propWs && propWs.close() } catch (_) {} })
</script>

<style scoped>
.ops-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.warning { color: var(--warning); }.danger { color: var(--danger); }.ok { color: var(--success); }
.table-wrap { overflow-x:auto; }.main-cell { min-width:190px; }.main-cell small { display:block; color:var(--text-soft); margin-top:4px; max-width:300px; }.wide-cell { min-width:260px; max-width:420px; line-height:1.55; }.reason { max-width:230px; margin-top:4px; color:var(--text-soft); font-size:11px; }.actions { white-space:nowrap; }.actions .btn,.actions .badge { margin:2px; }.error-cell { max-width:260px; color:var(--danger); font-size:12px; overflow:hidden; text-overflow:ellipsis; }.mapping-form { display:grid; grid-template-columns:120px repeat(4,minmax(130px,1fr)) auto; gap:8px; margin-bottom:16px; }.mini-stats { display:flex; gap:18px; flex-wrap:wrap; padding:10px 12px; margin-bottom:12px; background:var(--surface-2); border-radius:8px; color:var(--text-muted); font-size:13px; }.section-title{display:flex;align-items:baseline;gap:10px;margin:14px 0 8px;padding-left:9px;border-left:3px solid var(--primary);font-weight:700}.section-title small{color:var(--text-soft);font-size:11px;font-weight:400}.event-title{margin-top:24px;border-left-color:var(--warning)}.event-type{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--primary);font-size:12px}.event-table tbody tr{border-left:2px solid transparent}.event-table tbody tr:hover{border-left-color:var(--warning)}
@media(max-width:900px){.ops-stats{grid-template-columns:1fr 1fr}.mapping-form{grid-template-columns:1fr 1fr}.wide-cell{min-width:200px}}
/* 主动运维建议行点击展开详情（v2 结构化 / v1 文本回退） */
.run-row { cursor: pointer; }
.run-row.open { background: var(--surface-2); }
.run-detail { background: var(--surface-2); border-left: 3px solid var(--primary); padding: 12px 16px; font-size: 12px; }
.run-detail .inner { margin: 8px 0; }
.run-detail .inner th, .run-detail .inner td { font-size: 12px; padding: 4px 8px; }
.rec-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.rec-v1 { color: var(--text-soft); margin-top: 4px; white-space: pre-wrap; }
.evi { margin: 0; padding-left: 16px; color: var(--text-soft); }
.chip-group { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0; }
.chip-group .lbl { color: var(--text-muted); font-weight: 700; }
.chip-group .lbl-danger { color: var(--danger); }
.chip { background: var(--surface-3); border-radius: 10px; padding: 2px 10px; }
.chip.safe { border: 1px solid var(--success); }
.chip.risk { border: 1px solid var(--danger); color: var(--danger); }
.basis { color: var(--text-soft); margin-top: 6px; }
</style>
