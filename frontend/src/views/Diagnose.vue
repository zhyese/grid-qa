<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'diagnose' }" @click="tab = 'diagnose'">🩺 故障诊断推理</button>
      <button class="tab" :class="{ active: tab === 'case' }" @click="tab = 'case'">📚 相似案例</button>
      <button class="tab" :class="{ active: tab === 'ticket' }" @click="tab = 'ticket'">📝 两票生成</button>
    </div>

    <!-- 诊断 -->
    <div class="card" v-show="tab === 'diagnose'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="symptom" placeholder="描述故障症状，如：1号主变上层油温 88℃ 持续上升，负载 70%" @keyup.enter="doDiagnose" style="flex:1;min-width:260px" />
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doDiagnose" :disabled="loading || !symptom.trim()">{{ loading ? '诊断中…' : '开始诊断' }}</button>
      </div>
      <div v-if="diag" class="result">
        <div class="src-head">排查方向（多查询分解）<span class="hint">· 证据{{ diag.evidenceCount }} · 图谱{{ diag.graphCount }}</span></div>
        <div class="dim-list"><span class="dim-tag" v-for="d in diag.dimensions" :key="d">{{ d }}</span></div>
        <div class="summary" v-if="diag.diagnosis?.summary"><b>总体判断：</b>{{ diag.diagnosis.summary }}</div>
        <div class="src-head" v-if="diag.diagnosis?.causes?.length">可能原因（按可能性排序）</div>
        <div class="cause" v-for="(c, i) in (diag.diagnosis?.causes || [])" :key="i">
          <span class="lk" :class="'lk-' + (c.likelihood || '中')">{{ c.likelihood || '中' }}</span>
          <div class="cause-body"><div class="cause-name">{{ c.name }}</div><div class="cause-line" v-if="c.evidence"><b>依据：</b>{{ c.evidence }}</div><div class="cause-line" v-if="c.handling"><b>处置：</b>{{ c.handling }}</div></div>
        </div>
        <div class="risks" v-if="diag.diagnosis?.risks?.length"><b>⚠ 风险提示：</b><span class="badge badge-danger" v-for="r in diag.diagnosis.risks" :key="r" style="margin:2px">{{ r }}</span></div>
        <sources-list :sources="diag.sources" />
      </div>
    </div>

    <!-- 相似案例 -->
    <div class="card" v-show="tab === 'case'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="caseSymptom" placeholder="输入当前故障/症状，查找历史相似案例" @keyup.enter="doCase" style="flex:1;min-width:260px" />
        <button class="btn btn-primary" @click="doCase" :disabled="caseLoading || !caseSymptom.trim()">{{ caseLoading ? '检索中…' : '查找相似案例' }}</button>
      </div>
      <div v-if="cases">
        <div v-if="!cases.cases.length" class="empty">未在故障案例库找到相似案例（确认已上传 docType=故障案例 文档）</div>
        <div class="case-item" v-for="(c, i) in cases.cases" :key="i"><div class="case-doc">📄 {{ c.docName }} <span class="badge badge-info">相关度 {{ (c.score * 100).toFixed(0) }}%</span></div><div class="case-text">{{ c.text }}</div></div>
      </div>
    </div>

    <!-- 两票 -->
    <div class="card" v-show="tab === 'ticket'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="ticketTask" placeholder="操作任务，如：1号主变压器由运行转检修" @keyup.enter="doTicket" style="flex:1;min-width:260px" />
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doTicket" :disabled="ticketLoading || !ticketTask.trim()">{{ ticketLoading ? '生成中…' : '生成操作票' }}</button>
      </div>
      <div v-if="ticket">
        <p class="warning-tip">⚠ 辅助生成草案，必须由持票人核对调度指令与安规后才能执行。</p>
        <div class="ticket" v-if="ticket.ticket">
          <div class="t-row" v-if="ticket.ticket.device"><b>涉及设备：</b>{{ ticket.ticket.device }}</div>
          <div class="t-block" v-if="ticket.ticket.steps?.length"><b>操作步骤：</b><ol><li v-for="(s, i) in ticket.ticket.steps" :key="i">{{ s }}</li></ol></div>
          <div class="t-block" v-if="ticket.ticket.safety?.length"><b>安全措施：</b><ul><li v-for="(s, i) in ticket.ticket.safety" :key="i">{{ s }}</li></ul></div>
          <div class="t-block risks" v-if="ticket.ticket.risks?.length"><b>风险点：</b><span class="badge badge-danger" v-for="(r, i) in ticket.ticket.risks" :key="i" style="margin:2px">{{ r }}</span></div>
          <div class="t-row" v-if="ticket.ticket.notes"><b>备注：</b>{{ ticket.ticket.notes }}</div>
        </div>
        <sources-list :sources="ticket.sources" />
      </div>
    </div>
    <div class="toast" v-if="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { ref, h } from 'vue'
import { diagnose, similarCase, generateTicket } from '../api'

const SourcesList = {
  props: ['sources'],
  setup(props) {
    return () => props.sources && props.sources.length
      ? h('div', { class: 'sources' }, [h('div', { class: 'src-head' }, '📎 规程依据'), ...props.sources.map((s) => h('div', { class: 'src-item' }, `📄 ${s.docName}：${(s.text || '').slice(0, 100)}`))])
      : null
  },
}
const tab = ref('diagnose')
const modelType = ref('')
const toast = ref('')
let toastTimer = null
function show(m) { toast.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toast.value = ''), 2400) }

const symptom = ref(''); const loading = ref(false); const diag = ref(null)
async function doDiagnose() {
  if (!symptom.value.trim()) return
  loading.value = true; diag.value = null
  try { diag.value = (await diagnose(symptom.value, modelType.value || null)).data } catch (e) { show('诊断失败') } finally { loading.value = false }
}
const caseSymptom = ref(''); const caseLoading = ref(false); const cases = ref(null)
async function doCase() {
  if (!caseSymptom.value.trim()) return
  caseLoading.value = true; cases.value = null
  try { cases.value = (await similarCase(caseSymptom.value, modelType.value || null)).data } catch (e) { show('查询失败') } finally { caseLoading.value = false }
}
const ticketTask = ref(''); const ticketLoading = ref(false); const ticket = ref(null)
async function doTicket() {
  if (!ticketTask.value.trim()) return
  ticketLoading.value = true; ticket.value = null
  try { ticket.value = (await generateTicket(ticketTask.value, modelType.value || null)).data } catch (e) { show('生成失败') } finally { ticketLoading.value = false }
}
</script>

<style scoped>
.src-head { font-size: 13px; font-weight: 700; color: var(--text); margin: 14px 0 8px; }
.dim-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.dim-tag { background: var(--primary-soft); color: var(--primary); padding: 4px 10px; border-radius: 999px; font-size: 12px; }
html.dark .dim-tag { background: var(--primary-soft-2) }
.summary { background: var(--info-soft); border-left: 3px solid var(--info); padding: 10px 12px; border-radius: var(--radius-sm); margin: 10px 0; font-size: 13px; color: var(--text); }
.cause { display: flex; gap: 10px; background: var(--surface-2); padding: 10px 12px; border-radius: var(--radius-sm); margin-bottom: 8px; }
.lk { flex-shrink: 0; width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 13px; font-weight: 700; }
.lk-高 { background: var(--danger) } .lk-中 { background: var(--warning) } .lk-低 { background: var(--text-soft) }
.cause-name { font-weight: 600; color: var(--text); margin-bottom: 2px; }
.cause-line { font-size: 12px; color: var(--text-muted); margin-top: 2px; line-height: 1.6; }
.risks { margin-top: 10px; font-size: 13px; }
.sources { margin-top: 14px; padding-top: 10px; border-top: 1px dashed var(--border); }
.src-item { color: var(--text-muted); margin: 3px 0; font-size: 12px; }
.case-item { background: var(--surface-2); padding: 10px 12px; border-radius: var(--radius-sm); margin-bottom: 8px; }
.case-doc { font-weight: 600; color: var(--text); font-size: 13px; margin-bottom: 4px; }
.case-text { color: var(--text-muted); font-size: 13px; line-height: 1.6; }
.warning-tip { background: var(--warning-soft); color: var(--warning); border: 1px solid var(--warning); padding: 8px 12px; border-radius: var(--radius-sm); font-size: 13px; margin-bottom: 12px; }
.ticket { background: var(--surface-2); padding: 14px; border-radius: var(--radius); }
.t-row { font-size: 14px; margin-bottom: 8px; } .t-block { font-size: 13px; margin-bottom: 10px; }
.t-block ol, .t-block ul { margin: 4px 0; padding-left: 22px; color: var(--text); line-height: 1.8; }
</style>
