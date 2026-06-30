<template>
  <div class="page">
    <header class="topbar">
      <span>故障诊断助手</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/diagnose">诊断</router-link> |
        <router-link to="/kg">图谱</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/dashboard">统计</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>

    <div class="diag-wrap">
      <div class="tabs">
        <button :class="{ on: tab === 'diagnose' }" @click="tab = 'diagnose'">🩺 故障诊断推理</button>
        <button :class="{ on: tab === 'case' }" @click="tab = 'case'">📚 相似案例</button>
        <button :class="{ on: tab === 'ticket' }" @click="tab = 'ticket'">📝 两票生成</button>
      </div>

      <!-- Tab1: 故障诊断 -->
      <div v-show="tab === 'diagnose'" class="card">
        <div class="ctrl-row">
          <input v-model="symptom" placeholder="描述故障症状，如：1号主变上层油温 88℃ 持续上升，负载 70%，风扇运行正常"
                 @keyup.enter="doDiagnose" style="flex:1;min-width:260px" />
          <select v-model="modelType" style="max-width:140px">
            <option value="">默认模型</option>
            <option value="deepseek">DeepSeek</option>
            <option value="qwen">通义千问</option>
            <option value="doubao">豆包</option>
          </select>
          <button @click="doDiagnose" :disabled="loading || !symptom.trim()">
            {{ loading ? '诊断中…(多查询分解+检索+图谱)' : '开始诊断' }}
          </button>
        </div>
        <div v-if="diag" class="result">
          <div class="dims">
            <b>排查方向（多查询分解）：</b>
            <span class="dim-tag" v-for="d in diag.dimensions" :key="d">{{ d }}</span>
            <span class="meta-inline">· 证据{{ diag.evidenceCount }} 条 · 图谱{{ diag.graphCount }} 条</span>
          </div>
          <div class="summary" v-if="diag.diagnosis?.summary">
            <b>总体判断：</b>{{ diag.diagnosis.summary }}
          </div>
          <h4 v-if="diag.diagnosis?.causes?.length">可能原因（按可能性排序）</h4>
          <div class="cause" v-for="(c, i) in (diag.diagnosis?.causes || [])" :key="i">
            <span class="lk" :class="'lk-' + (c.likelihood || '中')">{{ c.likelihood || '中' }}</span>
            <div class="cause-body">
              <div class="cause-name">{{ c.name }}</div>
              <div class="cause-evidence" v-if="c.evidence"><b>依据：</b>{{ c.evidence }}</div>
              <div class="cause-handling" v-if="c.handling"><b>处置：</b>{{ c.handling }}</div>
            </div>
          </div>
          <div class="risks" v-if="diag.diagnosis?.risks?.length">
            <b>⚠ 风险提示：</b>
            <span class="risk-tag" v-for="r in diag.diagnosis.risks" :key="r">{{ r }}</span>
          </div>
          <sources-list :sources="diag.sources"></sources-list>
        </div>
      </div>

      <!-- Tab2: 相似案例 -->
      <div v-show="tab === 'case'" class="card">
        <div class="ctrl-row">
          <input v-model="caseSymptom" placeholder="输入当前故障/症状，查找历史相似案例"
                 @keyup.enter="doCase" style="flex:1;min-width:260px" />
          <button @click="doCase" :disabled="caseLoading || !caseSymptom.trim()">
            {{ caseLoading ? '检索中…' : '查找相似案例' }}
          </button>
        </div>
        <div v-if="cases" class="result">
          <p class="meta-inline" v-if="!cases.cases.length">未在故障案例库找到相似案例（请确认已上传 docType=故障案例 的文档）</p>
          <div class="case-item" v-for="(c, i) in cases.cases" :key="i">
            <div class="case-doc">📄 {{ c.docName }} <span class="score">相关度 {{ (c.score * 100).toFixed(0) }}%</span></div>
            <div class="case-text">{{ c.text }}</div>
          </div>
        </div>
      </div>

      <!-- Tab3: 两票生成 -->
      <div v-show="tab === 'ticket'" class="card">
        <div class="ctrl-row">
          <input v-model="ticketTask" placeholder="输入操作任务，如：1号主变压器由运行转检修"
                 @keyup.enter="doTicket" style="flex:1;min-width:260px" />
          <select v-model="modelType" style="max-width:140px">
            <option value="">默认模型</option>
            <option value="deepseek">DeepSeek</option>
            <option value="qwen">通义千问</option>
            <option value="doubao">豆包</option>
          </select>
          <button @click="doTicket" :disabled="ticketLoading || !ticketTask.trim()">
            {{ ticketLoading ? '生成中…' : '生成操作票' }}
          </button>
        </div>
        <div v-if="ticket" class="result">
          <p class="tip-warn">⚠ 辅助生成草案，必须由持票人核对调度指令与安规后才能执行。</p>
          <div class="ticket" v-if="ticket.ticket">
            <div class="t-row" v-if="ticket.ticket.device"><b>涉及设备：</b>{{ ticket.ticket.device }}</div>
            <div class="t-block" v-if="ticket.ticket.steps?.length">
              <b>操作步骤（按顺序）：</b>
              <ol><li v-for="(s, i) in ticket.ticket.steps" :key="i">{{ s }}</li></ol>
            </div>
            <div class="t-block" v-if="ticket.ticket.safety?.length">
              <b>安全措施：</b>
              <ul><li v-for="(s, i) in ticket.ticket.safety" :key="i">{{ s }}</li></ul>
            </div>
            <div class="t-block risks" v-if="ticket.ticket.risks?.length">
              <b>风险点：</b>
              <span class="risk-tag" v-for="(r, i) in ticket.ticket.risks" :key="i">{{ r }}</span>
            </div>
            <div class="t-row" v-if="ticket.ticket.notes"><b>备注：</b>{{ ticket.ticket.notes }}</div>
          </div>
          <sources-list :sources="ticket.sources"></sources-list>
        </div>
      </div>
    </div>
    <div class="toast" v-if="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { ref, h } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { diagnose, similarCase, generateTicket } from '../api'

// 引用来源列表内联组件（三 tab 复用）
const SourcesList = {
  props: ['sources'],
  setup(props) {
    return () => props.sources && props.sources.length
      ? h('div', { class: 'sources' }, [
          h('b', '📎 规程依据：'),
          ...props.sources.map((s) => h('div', { class: 'src' }, `📄 ${s.docName}：${(s.text || '').slice(0, 100)}`)),
        ])
      : null
  },
}

const auth = useAuthStore()
const router = useRouter()
const tab = ref('diagnose')
const modelType = ref('')
const toast = ref('')
function show(m) { toast.value = m; setTimeout(() => (toast.value = ''), 2400) }

// 诊断
const symptom = ref('')
const loading = ref(false)
const diag = ref(null)
async function doDiagnose() {
  if (!symptom.value.trim()) return
  loading.value = true; diag.value = null
  try { diag.value = (await diagnose(symptom.value, modelType.value || null)).data }
  catch (e) { show('诊断失败：' + (e.response?.data?.message || e.message)) }
  finally { loading.value = false }
}

// 相似案例
const caseSymptom = ref('')
const caseLoading = ref(false)
const cases = ref(null)
async function doCase() {
  if (!caseSymptom.value.trim()) return
  caseLoading.value = true; cases.value = null
  try { cases.value = (await similarCase(caseSymptom.value, modelType.value || null)).data }
  catch (e) { show('查询失败：' + (e.response?.data?.message || e.message)) }
  finally { caseLoading.value = false }
}

// 两票
const ticketTask = ref('')
const ticketLoading = ref(false)
const ticket = ref(null)
async function doTicket() {
  if (!ticketTask.value.trim()) return
  ticketLoading.value = true; ticket.value = null
  try { ticket.value = (await generateTicket(ticketTask.value, modelType.value || null)).data }
  catch (e) { show('生成失败：' + (e.response?.data?.message || e.message)) }
  finally { ticketLoading.value = false }
}

function logout() { auth.logout(); router.push('/login') }
</script>

<style scoped>
.diag-wrap { max-width: 920px; margin: 20px auto; padding: 0 16px; }
.tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.tabs button { background: var(--surface, #fff); color: #64748b; border: 1px solid var(--border, #e2e8f0); }
.tabs button.on { background: #2563eb; color: #fff; border-color: #2563eb; }
.card { background: #fff; border-radius: 10px; padding: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.ctrl-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
button { cursor: pointer; border: none; padding: 8px 16px; border-radius: 6px; background: #2563eb; color: #fff; }
button:disabled { opacity: .5; cursor: not-allowed; }
.result { margin-top: 18px; }
.dims { margin-bottom: 14px; font-size: 14px; }
.dim-tag { display: inline-block; background: #eef2ff; color: #4338ca; padding: 3px 10px; border-radius: 12px; margin: 3px 4px 3px 0; font-size: 12px; }
.meta-inline { color: #94a3b8; font-size: 12px; }
.summary { background: #f0f9ff; border-left: 3px solid #2563eb; padding: 10px 12px; border-radius: 4px; margin-bottom: 14px; font-size: 14px; }
h4 { margin: 14px 0 8px; color: #1e3a8a; }
.cause { display: flex; gap: 10px; background: #f8fafc; padding: 10px 12px; border-radius: 8px; margin-bottom: 8px; }
.lk { flex-shrink: 0; width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 13px; font-weight: bold; }
.lk-高 { background: #dc2626; }
.lk-中 { background: #d97706; }
.lk-低 { background: #64748b; }
.cause-name { font-weight: 600; color: #0f172a; margin-bottom: 2px; }
.cause-evidence, .cause-handling { font-size: 13px; color: #475569; margin-top: 2px; line-height: 1.6; }
.risks { margin-top: 12px; font-size: 13px; }
.risk-tag { display: inline-block; background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; padding: 2px 10px; border-radius: 12px; margin: 3px 4px 0 0; font-size: 12px; }
.sources { margin-top: 14px; padding-top: 10px; border-top: 1px dashed #e2e8f0; font-size: 13px; }
.src { color: #64748b; margin: 3px 0; }
.case-item { background: #f8fafc; padding: 10px 12px; border-radius: 8px; margin-bottom: 8px; }
.case-doc { font-weight: 600; color: #0f172a; font-size: 13px; margin-bottom: 4px; }
.score { color: #2563eb; font-weight: normal; font-size: 12px; }
.case-text { color: #475569; font-size: 13px; line-height: 1.6; }
.tip-warn { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
.ticket { background: #f8fafc; padding: 14px; border-radius: 8px; }
.t-row { font-size: 14px; margin-bottom: 8px; }
.t-block { font-size: 13px; margin-bottom: 10px; }
.t-block ol, .t-block ul { margin: 4px 0 4px 4px; padding-left: 22px; color: #334155; line-height: 1.7; }
.toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 10px 18px; border-radius: 8px; z-index: 9999; font-size: 14px; }
</style>
