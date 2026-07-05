<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'diagnose' }" @click="tab = 'diagnose'">🩺 故障诊断推理</button>
      <button class="tab" :class="{ active: tab === 'debate' }" @click="tab = 'debate'">🗣️ 辩论诊断</button>
      <button class="tab" :class="{ active: tab === 'queryplan' }" @click="tab = 'queryplan'">🧩 问题分解</button>
      <button class="tab" :class="{ active: tab === 'case' }" @click="tab = 'case'">📚 相似案例</button>
      <button class="tab" :class="{ active: tab === 'ticket' }" @click="tab = 'ticket'">📝 两票生成</button>
      <button class="tab" :class="{ active: tab === 'audit' }" @click="tab = 'audit'">🔍 两票审核</button>
    </div>

    <!-- 诊断 -->
    <div class="card" v-show="tab === 'diagnose'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="symptom" placeholder="描述故障症状，如：1号主变上层油温 88℃ 持续上升，负载 70%" @keyup.enter="doDiagnose" style="flex:1;min-width:260px" />
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <label class="agent-toggle"><input type="checkbox" v-model="agentMode" /> 🔬 深度诊断(Agent)</label>
        <button class="btn btn-primary" @click="doDiagnose" :disabled="loading || !symptom.trim()">{{ loading ? (agentMode ? '深度诊断中…' : '诊断中…') : (agentMode ? '深度诊断' : '开始诊断') }}</button>
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
        <div v-if="agentMode && agentSteps.length" class="agent-trace">
          <div class="src-head" @click="traceOpen = !traceOpen" style="cursor:pointer">
            🧠 Agent 思考过程（{{ agentSteps.length }} 步<span v-if="agentDegraded"> · 已降级</span>）<span class="hint">{{ traceOpen ? '▾' : '▸' }}</span>
          </div>
          <div v-show="traceOpen">
            <div class="trace-step" v-for="(s, i) in agentSteps" :key="i">
              <span class="trace-iter">{{ s.iter }}</span>
              <div class="trace-body">
                <div class="trace-thought" v-if="s.thought">{{ s.thought }}</div>
                <div class="trace-tool" v-if="s.tool">🔧 {{ s.tool }}<span class="hint" v-if="s.args"> ({{ JSON.stringify(s.args) }})</span></div>
                <div class="trace-tool" v-else><span class="hint">✓ 综合诊断</span></div>
                <div class="trace-result" v-if="s.result">{{ s.result }}</div>
              </div>
            </div>
          </div>
        </div>
        <sources-list :sources="diag.sources" />
      </div>
    </div>

    <!-- 辩论诊断 -->
    <div class="card" v-show="tab === 'debate'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="debateSymptom" placeholder="描述故障症状，3位专家从规程/图谱/案例视角独立诊断后辩论" @keyup.enter="doDebate" style="flex:1;min-width:260px" />
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doDebate" :disabled="debateLoading || !debateSymptom.trim()">{{ debateLoading ? '辩论诊断中…' : '开始辩论诊断' }}</button>
      </div>
      <div v-if="debate">
        <div v-if="debate.degraded" class="warning-tip">⚠ 辩论流程降级：{{ debate.degradeReason }}</div>
        <!-- 三方专家观点 -->
        <div v-if="debate.debate?.opinions?.length" class="debate-opinions">
          <div class="src-head">三位专家独立诊断意见
            <span class="hint">· 共{{ debate.debate.rounds }}轮 · {{ debate.latencyMs }}ms</span>
            <span v-if="debate.debate.neededDebate" class="badge badge-warning" style="margin-left:8px">有分歧·已辩论</span>
          </div>
          <div class="opinion-grid">
            <div class="opinion-card" v-for="(op, i) in debate.debate.opinions" :key="i" :class="'agent-' + i">
              <div class="opinion-header">{{ ['📋 规程专家','🧠 图谱专家','📚 案例专家'][i] || op.agent }}</div>
              <div class="opinion-summary">{{ op.summary }}</div>
              <div class="cause" v-for="(c, j) in (op.causes || [])" :key="j" style="margin:4px 0;padding:6px 8px">
                <span class="lk" :class="'lk-' + (c.likelihood || '中')" style="width:24px;height:24px;font-size:11px">{{ {高:'高',中:'中',低:'低'}[c.likelihood] || '中' }}</span>
                <div class="cause-body">
                  <div class="cause-name" style="font-size:12px">{{ c.name }}</div>
                  <div class="cause-line" v-if="c.evidence" style="font-size:11px">{{ c.evidence.slice(0,120) }}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <!-- 终裁结果 -->
        <div class="summary" v-if="debate.diagnosis?.summary" style="margin-top:12px">
          <b>⚖ 终裁结论：</b>{{ debate.diagnosis.summary }}
        </div>
        <div class="src-head" v-if="debate.diagnosis?.causes?.length" style="margin-top:10px">终裁可能原因排序</div>
        <div class="cause" v-for="(c, i) in (debate.diagnosis?.causes || [])" :key="'final-' + i">
          <span class="lk" :class="'lk-' + (c.likelihood || '中')">{{ {高:'高',中:'中',低:'低'}[c.likelihood] || '中' }}</span>
          <div class="cause-body">
            <div class="cause-name">{{ c.name }}
              <span class="hint" v-if="c.sourceConsensus"> · [{{ {consensus:'三方一致','regulation':'规程主导','graph':'图谱主导','case':'案例主导','pending':'待确认'}[c.sourceConsensus] || c.sourceConsensus }}]</span>
            </div>
            <div class="cause-line" v-if="c.evidence"><b>综合证据：</b>{{ c.evidence }}</div>
            <div class="cause-line" v-if="c.handling"><b>处置：</b>{{ c.handling }}</div>
          </div>
        </div>
        <!-- 分歧记录 -->
        <div v-if="debate.debate?.disagreements?.length" style="margin-top:12px;padding-top:10px;border-top:1px dashed var(--border)">
          <div class="src-head">辩论分歧记录</div>
          <div class="dispute" v-for="(d, i) in debate.debate.disagreements" :key="i">
            <div class="dispute-issue"><b>分歧点：</b>{{ d.issue }}</div>
            <div class="dispute-views">
              <div class="dv" v-if="d.regulationView"><span class="dv-label">规程：</span>{{ d.regulationView.slice(0,100) }}</div>
              <div class="dv" v-if="d.graphView"><span class="dv-label">图谱：</span>{{ d.graphView.slice(0,100) }}</div>
              <div class="dv" v-if="d.caseView"><span class="dv-label">案例：</span>{{ d.caseView.slice(0,100) }}</div>
            </div>
            <div class="dispute-resolve" v-if="d.resolution"><b>裁决：</b>{{ d.resolution }}</div>
          </div>
        </div>
        <div class="risks" v-if="debate.diagnosis?.risks?.length" style="margin-top:10px"><b>⚠ 最终风险提示：</b><span class="badge badge-danger" v-for="r in debate.diagnosis.risks" :key="r" style="margin:2px">{{ r }}</span></div>
      </div>
    </div>

    <!-- 问题分解 -->
    <div class="card" v-show="tab === 'queryplan'">
      <div class="row" style="margin-bottom: 14px">
        <input class="input" v-model="planQuestion" placeholder="复杂问题，系统自动分解为子问题并行检索后综合回答" @keyup.enter="doPlan" style="flex:1;min-width:260px" />
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doPlan" :disabled="planLoading || !planQuestion.trim()">{{ planLoading ? '分解中…' : '问题分解' }}</button>
      </div>
      <div v-if="planResult">
        <div class="src-head">子问题（{{ planResult.subQueryCount || planResult.subQueries?.length || 0 }} 个）<span class="hint">· {{ planResult.latencyMs }}ms</span></div>
        <div class="cause" v-for="(sq, i) in planResult.subQueries" :key="i">
          <span class="lk" style="width:24px;height:24px;font-size:11px;background:var(--primary)">{{ i+1 }}</span>
          <div class="cause-body">
            <div class="cause-name">{{ sq.query }} <span class="badge badge-neutral" style="font-size:10px">{{ sq.type }}</span></div>
            <div class="cause-line" v-if="sq.contextCount">检索到 {{ sq.contextCount }} 条相关资料</div>
          </div>
        </div>
        <div class="summary" style="margin-top:10px"><b>综合答案：</b>{{ planResult.answer }}</div>
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

    <!-- 两票审核 -->
    <div class="card" v-show="tab === 'audit'">
      <div class="row" style="margin-bottom: 14px">
        <select class="select" v-model="auditType" style="max-width:120px"><option value="操作票">操作票</option><option value="工作票">工作票</option></select>
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doAudit" :disabled="auditLoading || !auditText.trim()">{{ auditLoading ? '审核中…' : '开始审核' }}</button>
      </div>
      <textarea class="input" v-model="auditText" placeholder="粘贴已填票据全文（含 任务/调度指令号/操作人/操作步骤/安全措施/危险点）" style="width:100%;min-height:160px;resize:vertical;font-family:inherit;margin-bottom:12px"></textarea>
      <div v-if="audit">
        <div class="audit-head">
          <span class="badge" :class="'ov-' + audit.overall">{{ {pass:'✓ 合规',warn:'⚠ 需修改',fail:'✗ 不合规'}[audit.overall] }}</span>
          <span class="score">得分 {{ audit.score }}</span>
          <span class="hint">· {{ audit.latencyMs }}ms</span>
        </div>
        <div v-if="!audit.items.length" class="empty">未发现问题</div>
        <div class="cause" v-for="(it, i) in audit.items" :key="i">
          <span class="lk" :class="'sv-' + it.severity">{{ {critical:'严',major:'重',minor:'轻'}[it.severity] }}</span>
          <div class="cause-body">
            <div class="cause-name">{{ it.msg }} <span class="hint">[{{ it.ruleId }} · {{ it.layer }}]</span></div>
            <div class="cause-line" v-if="it.suggestion"><b>建议：</b>{{ it.suggestion }}</div>
          </div>
        </div>
      </div>
    </div>
    <div class="toast" v-if="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { ref, h } from 'vue'
import { diagnose, similarCase, generateTicket, auditTicket, diagnoseAgent, diagnoseDebate, queryPlan } from '../api'

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
const agentMode = ref(false); const agentSteps = ref([]); const agentDegraded = ref(false); const traceOpen = ref(true)
async function doDiagnose() {
  if (!symptom.value.trim()) return
  loading.value = true; diag.value = null; agentSteps.value = []; agentDegraded.value = false
  try {
    if (agentMode.value) {
      const r = (await diagnoseAgent(symptom.value, modelType.value || null)).data
      diag.value = { diagnosis: r.diagnosis }
      agentSteps.value = r.steps || []
      agentDegraded.value = !!r.degraded
    } else {
      diag.value = (await diagnose(symptom.value, modelType.value || null)).data
    }
  } catch (e) { show('诊断失败') } finally { loading.value = false }
}
const debateSymptom = ref(''); const debateLoading = ref(false); const debate = ref(null)
async function doDebate() {
  if (!debateSymptom.value.trim()) return
  debateLoading.value = true; debate.value = null
  try { debate.value = (await diagnoseDebate(debateSymptom.value, modelType.value || null)).data }
  catch (e) { show('辩论诊断失败') } finally { debateLoading.value = false }
}
const planQuestion = ref(''); const planLoading = ref(false); const planResult = ref(null)
async function doPlan() {
  if (!planQuestion.value.trim()) return
  planLoading.value = true; planResult.value = null
  try { planResult.value = (await queryPlan(planQuestion.value, modelType.value || null)).data }
  catch (e) { show('问题分解失败') } finally { planLoading.value = false }
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
const auditText = ref(''); const auditType = ref('操作票'); const auditLoading = ref(false); const audit = ref(null)
async function doAudit() {
  if (!auditText.value.trim()) return
  auditLoading.value = true; audit.value = null
  try { audit.value = (await auditTicket(auditText.value, auditType.value, modelType.value || null)).data }
  catch (e) { show('审核失败（需管理员权限）') } finally { auditLoading.value = false }
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
.audit-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.ov-pass { background: var(--success, #34c759); } .ov-warn { background: var(--warning); } .ov-fail { background: var(--danger); }
.ov-pass, .ov-warn, .ov-fail { color: #fff; }
.score { font-weight: 700; font-size: 15px; color: var(--text); }
.sv-critical { background: var(--danger); } .sv-major { background: var(--warning); } .sv-minor { background: var(--text-soft); }
.agent-toggle { display: flex; align-items: center; gap: 4px; font-size: 13px; color: var(--text-muted); cursor: pointer; user-select: none; white-space: nowrap; }
.agent-trace { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border); }
.trace-step { display: flex; gap: 8px; margin-bottom: 8px; }
.trace-iter { flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%; background: var(--primary); color: #fff; font-size: 12px; font-weight: 700; display: flex; align-items: center; justify-content: center; }
.trace-body { flex: 1; background: var(--surface-2); padding: 6px 10px; border-radius: var(--radius-sm); }
.trace-thought { font-size: 12px; color: var(--text); font-style: italic; margin-bottom: 2px; }
.trace-tool { font-size: 12px; color: var(--primary); font-weight: 600; }
.trace-result { font-size: 12px; color: var(--text-muted); margin-top: 2px; line-height: 1.5; white-space: pre-wrap; }
.opinion-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-bottom: 10px; }
.opinion-card { background: var(--surface-2); padding: 10px; border-radius: var(--radius); }
.agent-0 { border-top: 3px solid var(--primary, #007aff); }
.agent-1 { border-top: 3px solid var(--success, #34c759); }
.agent-2 { border-top: 3px solid var(--warning, #ff9500); }
.opinion-header { font-weight: 700; font-size: 13px; margin-bottom: 6px; }
.opinion-summary { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }
.dispute { background: var(--surface-2); padding: 8px 10px; border-radius: var(--radius-sm); margin-bottom: 6px; }
.dispute-issue { font-size: 13px; margin-bottom: 4px; }
.dispute-views { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.dv-label { font-weight: 600; color: var(--text); }
</style>
