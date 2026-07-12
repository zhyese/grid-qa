<template>
  <div class="chat-wrap">
    <!-- 对话历史栏 -->
    <aside class="conv-bar" :class="{ collapsed: convCollapsed }">
      <button class="btn btn-primary new-btn" @click="newChat">＋ 新建对话</button>
      <button class="btn btn-ghost btn-sm" style="margin:6px 0" @click="toggleFavPanel">⭐ 我的收藏</button>
      <div v-if="showFavPanel" class="fav-panel" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-bottom:8px;max-height:240px;overflow:auto">
        <div v-if="!favList.length" class="hint">暂无收藏（答案下方⭐收藏）</div>
        <div v-for="f in favList" :key="f.id" class="fav-item" style="padding:6px;border-bottom:1px dashed var(--border);cursor:pointer;display:flex;justify-content:space-between;gap:6px" @click="useFav(f)">
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ f.query }}</span>
          <a @click.stop="delFav(f.id)" style="color:var(--danger)">✕</a>
        </div>
      </div>
      <input class="input" v-model="searchKw" placeholder="🔍 搜索对话..." @input="onSearch" />
      <div class="conv-batch" v-if="conversations.length">
        <label class="msg-check"><input type="checkbox" :checked="allConvsSelected" @change="toggleAllConvs($event.target.checked)" /> 全选</label>
        <span class="hint">{{ selectedConvs.size }}/{{ conversations.length }}</span>
        <button class="btn btn-danger btn-sm" :disabled="!selectedConvs.size" @click="batchRemoveConvs">🗑️ 批量删除</button>
        <button class="btn btn-ghost btn-sm" v-if="selectedConvs.size" @click="selectedConvs = new Set()">取消</button>
      </div>
      <div class="conv-list">
        <div v-for="c in conversations" :key="c.id" class="conv-item" :class="{ active: c.id === currentConvId }" @click="selectConv(c.id)">
          <input v-if="editingId === c.id" class="input rename-input" v-model="editingTitle" @click.stop @keyup.enter="saveRename(c)" @keyup.esc="cancelRename" />
          <template v-else>
            <input type="checkbox" class="conv-check" :checked="selectedConvs.has(c.id)" @click.stop="toggleConv(c.id)" title="选中以批量删除" />
            <div class="conv-title">{{ c.title || '(无标题对话)' }}</div>
            <div class="conv-time">{{ c.createdAt }}</div>
            <div class="conv-ops" @click.stop>
              <a @click="startRename(c)" title="重命名">✏️</a>
              <a class="danger" @click="removeConv(c)" title="删除">🗑️</a>
            </div>
          </template>
        </div>
        <div v-if="!conversations.length" class="empty small">{{ searchKw ? '无匹配对话' : '暂无历史对话' }}</div>
      </div>
      <button class="collapse-btn" @click="convCollapsed = !convCollapsed" :title="convCollapsed ? '展开' : '收起'">{{ convCollapsed ? '»' : '«' }}</button>
    </aside>

    <!-- 聊天主区 -->
    <section class="chat-main">
      <button class="menu-btn" v-show="convCollapsed" @click="convCollapsed = !convCollapsed" title="历史对话">☰</button>
      <div class="msg-list" ref="msgListEl">
        <div class="msg-batch" v-if="selectableMsgIds.length">
          <label class="msg-check"><input type="checkbox" :checked="allMsgsSelected" @change="toggleAllMsgs($event.target.checked)" /> 全选</label>
          <span class="hint">已选 {{ selectedMsgs.size }}/{{ selectableMsgIds.length }}</span>
          <button class="btn btn-danger btn-sm" :disabled="!selectedMsgs.size" @click="batchRemoveMsgs">🗑️ 批量删除</button>
          <button class="btn btn-ghost btn-sm" v-if="selectedMsgs.size" @click="selectedMsgs = new Set()">取消</button>
        </div>
        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div v-if="m.role === 'user'" class="bubble user-bubble">
            <template v-if="m.editing">
              <textarea class="input edit-area" v-model="m.editText" rows="2"></textarea>
              <div class="edit-ops"><button class="btn btn-primary btn-sm" @click="resendEdit(m)">重提</button><button class="btn btn-ghost btn-sm" @click="cancelEdit(m)">取消</button></div>
            </template>
            <template v-else>
              <label v-if="m.id" class="msg-check" @click.stop><input type="checkbox" :checked="selectedMsgs.has(m.id)" @change="toggleMsg(m.id)" /></label>
              {{ m.content }}
              <a class="edit-btn" @click="startEdit(m)" title="编辑后重新提问">✏️</a>
            </template>
          </div>
          <div v-else class="bubble ai-bubble">
            <div v-if="!m.streaming" class="bubble-actions">
              <a @click="regenerate(m)">🔄 重新生成</a>
              <a @click="copyAnswer(m)">📋 复制</a>
              <a @click="exportWord(m)">📄 导出 Word</a>
              <a v-if="m.content && !m.streaming" @click="saveFavorite(m)">⭐ 收藏</a>
            </div>
            <pre v-if="m.streaming" class="streaming-text">{{ m.content }}<span class="cursor">▍</span></pre>
            <AgentTrace :steps="m.agentSteps" title="🎯 深度思考" />
            <div class="ans md" v-show="!m.streaming" @click="onAnsClick($event, m)" v-html="renderMd(m.content)"></div>
            <div v-if="m.aborted" class="hint" style="margin-top:8px">⏹ 已停止生成（仅显示已接收内容）</div>

            <div class="sources" v-if="m.sources && m.sources.length">
              <div class="src-head">📎 引用来源 <span class="hint">点 [n] 定位 · 点卡片复制</span></div>
              <div v-for="(s, j) in m.sources" :key="j" :id="'src-' + (j + 1)" class="src-item" :class="{ hi: m.hiIdx === j + 1 }" @click="copySource(s)" @mouseenter="hoverSource(m, j + 1)" @mouseleave="leaveSource(m)">
                <b class="src-no">[{{ j + 1 }}]</b>
                <div class="src-main">
                  <div class="src-line1">
                    <span class="src-doc" v-if="srcName(s)">📄 {{ srcName(s) }}</span>
                    <span v-if="s && s.docType" class="badge badge-neutral src-badge">{{ s.docType }}</span>
                    <span v-for="src in (s && s.sources) || []" :key="src" class="badge src-badge" :class="srcBadge(src)">{{ srcLabel(src) }}</span>
                  </div>
                  <div class="src-score" v-if="s && typeof s.score === 'number'">
                    <div class="bar"><div class="bar-fill" :style="{ width: Math.min(100, Math.max(2, s.score * 100)) + '%' }"></div></div>
                    <span class="muted">{{ (s.score * 100).toFixed(0) }}%</span>
                  </div>
                  <div class="src-text" :class="{ clamp: !(s && s._exp) }" @click.stop="s && (s._exp = !s._exp)">
                    {{ srcText(s) }} <span class="hint">{{ s && s._exp ? '收起' : '展开' }}</span>
                  </div>
                </div>
              </div>
            </div>

            <div class="meta" v-if="m.time">
              <span>⏱ {{ m.time }}s</span>
              <span v-if="m.cached" class="badge" :class="cacheBadge(m.cacheLayer)" :title="'命中缓存：' + cacheTitle(m.cacheLayer)">⚡ {{ cacheLabel(m.cacheLayer) }}</span>
              <span v-if="m.halluc != null">· 未引用率 {{ Math.round((m.halluc || 0) * 100) }}%</span>
              <span v-if="m.judgeHalluc != null" class="badge badge-warning" :title="(m.judgeReason ? 'LLM-judge：' + m.judgeReason : 'LLM-judge 实测幻觉率（异步）')">幻觉率{{ Math.round((m.judgeHalluc || 0) * 100) }}%</span>
              <span v-if="m.route" class="badge" :class="routeBadge(m.route)" :title="m.routeReason || ''">🧭 {{ routeLabel(m.route) }}</span>
              <span v-if="m.graphCount" class="badge badge-info">🔗 图谱{{ m.graphCount }}</span>
              <span v-if="m.highRisk && m.highRisk.length" class="badge badge-danger" :title="'高风险：' + m.highRisk.join('、')">⚠ {{ m.highRisk.slice(0, 3).join('、') }}</span>
              <span v-if="m.modelType" class="badge badge-info">🤖 {{ modelLabel(m.modelType) }}</span>
              <span v-if="m.confidence" class="badge" :class="confBadge(m.confidence)" :title="confTitle(m.confidence)">{{ confLabel(m.confidence) }}</span>
              <span class="fb">
                <a @click="like(m)" :class="{ on: m.fb === 'like' }">👍</a>
                <a @click="dislike(m)" :class="{ on: m.fb === 'dislike' }">👎</a>
              </span>
              <a v-if="m.content && !m.streaming" class="ev-btn" @click="showEvidence(m)">🔍 证据溯源</a>
              <a v-if="m.content && !m.streaming && (m.confidence==='medium'||m.confidence==='refused')" class="ev-btn" @click="reportGap(m)">⚠️ 上报证据不足</a>
            </div>
            <!-- 证据溯源弹窗 -->
            <div class="modal-overlay" v-if="m._evOpen" @click.self="m._evOpen = false">
              <div class="modal ev-modal">
                <div class="modal-head">
                  证据溯源·句级引用 <span v-if="!m._evTrace?.cached" class="hint">支持比 {{ m._evTrace?.supportRatio * 100 || 0 }}%</span>
                  <a @click="m._evOpen = false" style="cursor:pointer">✕</a>
                </div>
                <div class="ev-list">
                  <div v-if="m._evTrace?.cached" class="ev-item" style="border-left-color:var(--accent)">
                    <span class="ev-icon">⚡</span>
                    <div class="ev-body">
                      <div class="ev-text">本答案命中<strong>{{ cacheLabel(m._evTrace.cacheLayer) }}</strong>，未走实时检索，故无句级引用可溯源。</div>
                      <div class="ev-src">来源：{{ cacheLabel(m._evTrace.cacheLayer) }}</div>
                    </div>
                  </div>
                  <template v-else>
                    <div v-for="(s, i) in (m._evTrace?.sentences || []).filter(s => (s.sources && s.sources.length) && (s.text || '').trim())" :key="i" class="ev-item" :class="{ 'ev-supported': s.supported, 'ev-unsupported': !s.supported }">
                      <span class="ev-icon">{{ s.supported ? '✅' : '⚠️' }}</span>
                      <div class="ev-body">
                        <div class="ev-text">{{ s.text }}</div>
                        <div v-if="s.sources?.length" class="ev-src">
                          📎 <span v-for="n in s.sources" :key="n" class="ev-ref" @click="scrollToSource(m, n)">[{{ n }}]</span>
                        </div>
                        <div v-else class="ev-nosrc">无引用来源</div>
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <div class="related" v-if="!m.streaming && m.related && m.related.length">
              <div class="src-head">💡 相关追问</div>
              <div class="rq-list">
                <button class="rq" v-for="(rq, k) in m.related" :key="k" @click="askRelated(rq)">{{ rq }}</button>
              </div>
            </div>
          </div>
        </div>
        <div v-if="!messages.length" class="welcome">
          <div class="welcome-icon">👋</div>
          <div class="welcome-title">欢迎使用电网运维智能问答</div>
          <div class="welcome-sub">自然语言提问 · 混合检索 · 自纠错 · 可信答案</div>
          <div class="qq-grid">
            <button class="qq" v-for="q in quickQuestions" :key="q" @click="quickAsk(q)"><span class="qq-icon">💬</span>{{ q }}</button>
          </div>
        </div>
      </div>

      <div class="input-bar">
        <select class="select model-sel" v-model="modelType">
          <option value="">默认模型</option>
          <option value="deepseek">DeepSeek</option>
          <option value="qwen">通义千问</option>
          <option value="doubao">豆包</option>
        </select>
        <label class="ws-toggle" title="WebSocket 双向流式（默认 SSE）"><input type="checkbox" v-model="useWS" /> WS</label>
        <label class="ws-toggle" title="深度思考：AI 自主多轮调工具(检索/图谱/案例)交叉验证后作答（仅 SSE）"><input type="checkbox" v-model="agentMode" /> 🎯深度</label>
        <input class="input" v-model="query" placeholder="输入运维问题，如：主变压器温度异常如何处置..." @keyup.enter="ask" :disabled="loading" />
        <button v-if="loading && !useWS" class="btn btn-danger send-btn" @click="stopGen">⏹ 停止</button>
        <button v-else class="btn btn-primary send-btn" @click="ask" :disabled="loading">{{ loading ? '生成中...' : '发送' }}</button>
      </div>
    </section>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, nextTick, computed } from 'vue'
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import AgentTrace from '../components/AgentTrace.vue'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import xml from 'highlight.js/lib/languages/xml'
import markdown from 'highlight.js/lib/languages/markdown'
import { useAuthStore } from '../stores/auth'
import { streamAnswer, streamAnswerWS, sendFeedback, getFaithfulness, getRelatedQuestions, getConversations, getHistory, deleteConversation, renameConversation, batchDeleteConversations, batchDeleteMessages, exportAnswer, getEvidenceTrace, reportEvidenceGap, addFavorite, listFavorites, deleteFavorite } from '../api'

hljs.registerLanguage('python', python)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('md', markdown)

const md = new MarkdownIt({ html: true, linkify: true, breaks: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try { return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>` } catch (e) {}
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
function renderMd(text) {
  if (!text) return ''
  return md.render(String(text))
    .replace(/<script[\s\S]*?<\/script>/gi, '')   // html:true 后剥 <script> 防 XSS
    .replace(/\[(\d+)\]/g, '<sup class="cite-ref" data-idx="$1">[$1]</sup>')
}

const auth = useAuthStore()
const router = useRouter()
const msgListEl = ref(null)
const query = ref('')
const modelType = ref('')
const loading = ref(false)
const messages = ref([])
const conversations = ref([])
const selectedConvs = ref(new Set())   // 批量选中会话 id
const selectedMsgs = ref(new Set())    // 批量选中消息 id
const selectableMsgIds = computed(() => messages.value.filter((m) => m.id).map((m) => m.id))
const allConvsSelected = computed(() => conversations.value.length > 0 && selectedConvs.value.size === conversations.value.length)
const allMsgsSelected = computed(() => selectableMsgIds.value.length > 0 && selectedMsgs.value.size === selectableMsgIds.value.length)
const currentConvId = ref('')
const searchKw = ref('')
const editingId = ref('')
const editingTitle = ref('')
const convCollapsed = ref(localStorage.getItem('conv-collapsed') === '1')
const useWS = ref(false)
const agentMode = ref(false)   // S2 深度思考(Agent)：仅 SSE，AI 多轮调工具交叉验证
const abortCtrl = ref(null)   // 流式生成 AbortController（停止生成用）
const toastMsg = ref('')
let toastTimer = null
function toast(msg) { toastMsg.value = msg; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1600) }
function watchCollapsed() { localStorage.setItem('conv-collapsed', convCollapsed.value ? '1' : '0') }
import { watch } from 'vue'
watch(convCollapsed, watchCollapsed)

const quickQuestions = [
  '主变压器温度异常如何处置？',
  '配电线路单相接地故障如何排查？',
  'SF6断路器漏气该如何处理？',
  '变压器日常巡视检查哪些项目？',
]
function quickAsk(q) { query.value = q; ask() }
function askRelated(q) { query.value = q; ask() }

async function loadRelated(m) {
  try { const r = await getRelatedQuestions(m.query, m.content, modelType.value || undefined); m.related = (r.data && r.data.questions) || [] } catch (e) { m.related = [] }
}
async function loadFaithfulness(m) {
  try {
    const r = await getFaithfulness(m.content, m.sources, modelType.value || undefined)
    if (r && r.data && typeof r.data.hallucination === 'number') { m.judgeHalluc = r.data.hallucination; m.judgeReason = r.data.reason || '' }
  } catch (e) {}
}

async function loadConversations() {
  try { conversations.value = (await getConversations(searchKw.value)).data || [] } catch (e) {}
}
let searchTimer = null
function onSearch() { clearTimeout(searchTimer); searchTimer = setTimeout(loadConversations, 300) }
async function selectConv(id) {
  selectedMsgs.value = new Set()
  currentConvId.value = id
  const r = await getHistory(id)
  messages.value = (r.data || []).map((m) => ({ id: m.id, role: m.role, content: m.content, sources: [], time: 0, halluc: 0, query: m.role === 'user' ? m.content : '', fb: '' }))
  if (window.innerWidth <= 768) convCollapsed.value = true   // 手机端选完会话自动收起，露出聊天区
}
function newChat() { currentConvId.value = ''; messages.value = []; selectedMsgs.value = new Set() }

async function ask() {
  if (!query.value.trim() || loading.value) return
  const q = query.value
  messages.value.push({ role: 'user', content: q })
  query.value = ''
  await runStream(q)
}

/** 核心流式发送流程，被 ask / regenerate / resendEdit 复用。opts.regen=跳缓存，opts.cid=指定会话。 */
async function runStream(q, opts = {}) {
  const { regen = false, cid } = opts
  loading.value = true
  const msg = reactive({ role: 'assistant', content: '', sources: [], time: 0, halluc: 0, route: '', routeReason: '', modelType: '', conversationId: cid || currentConvId.value || '', query: q, fb: '', streaming: true, aborted: false, agentSteps: [], agentMode: false, _traceOpen: true })
  messages.value.push(msg)
  nextTick(() => { if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight })
  const onStreamEvent = (ev) => {
    if (ev.type === 'meta') { msg.sources = ev.sources || []; if (ev.conversationId) msg.conversationId = ev.conversationId }
    else if (ev.type === 'tool_step') { msg.agentMode = true; (msg.agentSteps ||= []).push(ev.step || {}); nextTick(() => { if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight }) }
    else if (ev.type === 'token') { msg.content += ev.content || ''; nextTick(() => { if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight }) }
    else if (ev.type === 'aborted') { msg.aborted = true; msg.streaming = false; loading.value = false }   // 停止生成：保留已收内容
    else if (ev.type === 'done' || ev.type === 'error') {
      if (ev.content) msg.content = ev.content
      if (ev.annotatedAnswer) msg.content = ev.annotatedAnswer   // 补标后全文替换，触发 renderMd 出 [n] 角标上标
      if (ev.evidenceTrace) msg._evTrace = ev.evidenceTrace      // 句级溯源面板数据源
      if (typeof ev.responseTime === 'number') msg.time = ev.responseTime
      if (typeof ev.hallucinationRate === 'number') msg.halluc = ev.hallucinationRate
      if (typeof ev.graphCount === 'number') msg.graphCount = ev.graphCount
      if (ev.highRisk) msg.highRisk = ev.highRisk
      if (ev.confidence) msg.confidence = ev.confidence
      if (ev.conversationId) msg.conversationId = ev.conversationId
      if (ev.route) msg.route = ev.route
      if (ev.routeReason) msg.routeReason = ev.routeReason
      if (ev.modelType) msg.modelType = ev.modelType
      if (ev.cached !== undefined) msg.cached = ev.cached
      if (ev.cacheLayer) msg.cacheLayer = ev.cacheLayer
      msg.streaming = false
      loading.value = false
      abortCtrl.value = null
      currentConvId.value = msg.conversationId
      loadConversations()
      if (!msg.aborted) { loadRelated(msg); loadFaithfulness(msg) }   // 中断的不再拉 related/faithfulness
    }
  }
  try {
    if (useWS.value) {
      streamAnswerWS(q, modelType.value || undefined, currentConvId.value || undefined, onStreamEvent)
    } else {
      abortCtrl.value = new AbortController()
      await streamAnswer(q, modelType.value || undefined, currentConvId.value || undefined, onStreamEvent, abortCtrl.value.signal, regen, agentMode.value)
    }
  } catch (e) {
    msg.content += (msg.content ? '\n' : '') + '（流式中断：' + (e.message || '') + '）'
    msg.streaming = false; loading.value = false; abortCtrl.value = null
  }
}

function stopGen() { if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null } }

/** 重新生成：取该 assistant 上一条 user 的 query，regen=true 作为新一轮追加（原答保留）。 */
async function regenerate(m) {
  if (loading.value) return
  // 找该 assistant 消息对应的上一条 user query
  const idx = messages.value.indexOf(m)
  let q = m.query
  for (let i = idx - 1; i >= 0; i--) { if (messages.value[i].role === 'user') { q = messages.value[i].content; break } }
  if (!q) return
  messages.value.push({ role: 'user', content: q })   // 补全 user 回合，形成完整"新轮"
  await runStream(q, { regen: true, cid: m.conversationId || currentConvId.value })
}

/** 编辑重提：就地编辑 user 消息，重提作为新一轮追加（历史保留）。 */
function startEdit(m) { m.editing = true; m.editText = m.content }
function cancelEdit(m) { m.editing = false }
async function resendEdit(m) {
  const q = (m.editText || '').trim(); if (!q || loading.value) return
  m.editing = false
  messages.value.push({ role: 'user', content: q })   // 编辑后的新文本作为新 user 回合
  await runStream(q, { cid: currentConvId.value })
}

async function like(m) { if (m.fb) return; try { await sendFeedback(m.query, m.content, 'like', m.conversationId, '', m.sources); m.fb = 'like' } catch (e) {} }
async function dislike(m) {
  if (m.fb) return
  const reason = window.prompt('感谢反馈，请简述答案哪里有问题（可选，用于优化）：') || ''
  try { await sendFeedback(m.query, m.content, 'dislike', m.conversationId, reason, m.sources); m.fb = 'dislike' } catch (e) {}
}

function confLabel(c) { return ({ high: '✓ 高置信', medium: '⚠ 证据有限', refused: '✗ 证据不足' })[c] || '' }
function confTitle(c) { return ({ high: '检索证据充分，答案可信度高', medium: '检索相关性中等，部分内容建议人工核对', refused: '未找到强相关资料，答案已保守处理' })[c] || '' }
function confBadge(c) { return ({ high: 'badge-success', medium: 'badge-warning', refused: 'badge-danger' })[c] || 'badge-neutral' }
function routeLabel(r) { return ({ sparse: '🔤 纯 BM25 匹配(sparse)', dense: '🧠 向量语义检索(dense)', hybrid: '🔀 全链路(hybrid)', sparse_first: '🔤→🔀 关键词优先(sparse_first)' })[r] || r }
function modelLabel(m) { return ({ deepseek: 'DeepSeek', qwen: '通义千问', doubao: '豆包' })[m] || m }
function routeBadge(r) { return ({ sparse: 'badge-sparse', dense: 'badge-dense', hybrid: 'badge-neutral', sparse_first: 'badge-warning' })[r] || 'badge-neutral' }
// 缓存层标识：redis(L1热点) / mysql(L2持久) / semantic_*(L1.5相似) 三态区分
function cacheLabel(l) { return ({ redis: '高频问答·热点', mysql: '高频问答·历史' })[l] || (l && l.startsWith('semantic') ? '高频问答·相似' : '高频问答') }
function cacheTitle(l) { return ({ redis: 'Redis 热点缓存(L1)，毫秒级秒回', mysql: 'MySQL 持久缓存(L2)' })[l] || (l && l.startsWith('semantic') ? '语义相似缓存(L1.5)，embedding 近似匹配' : '已缓存') }
function cacheBadge(l) { return ({ redis: 'badge-cache', mysql: 'badge-cache-mysql' })[l] || (l && l.startsWith('semantic') ? 'badge-cache-semantic' : 'badge-cache') }
function srcName(s) { return typeof s === 'string' ? '' : (s.docName || '') }
function srcText(s) { return typeof s === 'string' ? s : (s.chunk || s.text || '') }
function srcLabel(s) { return { dense_cloud: '云稠密', dense_bge: 'bge稠密', bm25: 'BM25' }[s] || s }
function srcBadge(s) { return { dense_cloud: 'badge-info', dense_bge: 'badge-primary', bm25: 'badge-warning' }[s] || 'badge-neutral' }
function hoverSource(m, k) {
  m.hiIdx = k
  document.querySelectorAll('.cite-ref[data-idx="' + k + '"]').forEach(el => el.classList.add('cite-hot'))
}
function leaveSource(m) {
  m.hiIdx = null
  document.querySelectorAll('.cite-ref.cite-hot').forEach(el => el.classList.remove('cite-hot'))
}
async function copyAnswer(m) { try { await navigator.clipboard.writeText(m.content); toast('答案已复制') } catch (e) { toast('复制失败') } }
async function saveFavorite(m) { try { await addFavorite(m.query, m.content); toast('已收藏到个人收藏夹') } catch (e) { toast('收藏失败') } }
const showFavPanel = ref(false)
const favList = ref([])
async function toggleFavPanel() { showFavPanel.value = !showFavPanel.value; if (showFavPanel.value) { try { favList.value = (await listFavorites()).data || [] } catch (e) {} } }
async function delFav(id) { try { await deleteFavorite(id); favList.value = favList.value.filter(f => f.id !== id) } catch (e) { toast('删除失败') } }
function useFav(f) { showFavPanel.value = false; query.value = f.query }
async function exportWord(m) {
  try {
    const blob = await exportAnswer(m.query, m.content, m.sources, { confidence: m.confidence, hallucinationRate: m.halluc, responseTime: m.time })
    const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = '运维问答报告.docx'; a.click(); URL.revokeObjectURL(url); toast('已导出 Word')
  } catch (e) { toast('导出失败') }
}
async function copySource(s) { try { await navigator.clipboard.writeText(srcText(s)); toast('来源已复制') } catch (e) {} }
function onAnsClick(e, m) {
  const el = e.target.closest('.cite-ref'); if (!el) return
  const idx = Number(el.dataset.idx); m.hiIdx = idx
  const target = document.getElementById('src-' + idx); if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
async function reportGap(m) {
  try {
    await reportEvidenceGap(m.query, m.content, m.confidence, m.cragGrade, m.cragAction)
    toast('已上报，将补充证据')
  } catch (e) { toast('上报失败') }
}
async function showEvidence(m) {
  if (m._evTrace) { m._evOpen = !m._evOpen; return }
  // 缓存命中：未走实时检索，无句级引用可溯源 → 直接展示缓存来源，不调溯源接口（避免误报"证据溯源失败"）
  if (m.cached) {
    m._evTrace = { cached: true, cacheLayer: m.cacheLayer, supportRatio: null, sentences: [] }
    m._evOpen = true
    return
  }
  try {
    const sources = (m.sources || []).map(s => typeof s === 'string' ? s : (s.chunk || s.text || ''))
    const r = await getEvidenceTrace(m.content, sources, m.model_type || null)
    m._evTrace = r.data || { sentences: [], supportRatio: 0 }
    m._evOpen = true
  } catch (e) { toast('证据溯源失败') }
}
function scrollToSource(m, idx) { m.hiIdx = idx; const el = document.getElementById('src-' + idx); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }) }

async function removeConv(c) {
  if (!confirm(`删除对话「${c.title || '无标题'}」？`)) return
  try { await deleteConversation(c.id); await loadConversations(); if (currentConvId.value === c.id) newChat(); toast('已删除') } catch (e) { toast('删除失败') }
}
function toggleConv(id) {
  const s = new Set(selectedConvs.value); s.has(id) ? s.delete(id) : s.add(id); selectedConvs.value = s
}
async function batchRemoveConvs() {
  const ids = [...selectedConvs.value]
  if (!ids.length) return
  if (!confirm(`删除选中的 ${ids.length} 个对话？`)) return
  try {
    const r = await batchDeleteConversations(ids)
    const n = (r.data && r.data.deleted) || 0
    selectedConvs.value = new Set()
    await loadConversations()
    if (currentConvId.value && ids.includes(currentConvId.value)) newChat()
    toast(`已删除 ${n} 条`)
  } catch (e) { toast('删除失败') }
}
function toggleMsg(id) {
  const s = new Set(selectedMsgs.value); s.has(id) ? s.delete(id) : s.add(id); selectedMsgs.value = s
}
async function batchRemoveMsgs() {
  const ids = [...selectedMsgs.value]
  if (!ids.length) return
  if (!confirm(`删除选中的 ${ids.length} 条消息？`)) return
  try {
    const r = await batchDeleteMessages(ids)
    const n = (r.data && r.data.deleted) || 0
    selectedMsgs.value = new Set()
    messages.value = messages.value.filter((m) => !ids.includes(m.id))   // 本地移除已删 user 消息
    toast(`已删除 ${n} 条`)
  } catch (e) { toast('删除失败') }
}
function toggleAllConvs(checked) {
  selectedConvs.value = checked ? new Set(conversations.value.map((c) => c.id)) : new Set()
}
function toggleAllMsgs(checked) {
  selectedMsgs.value = checked ? new Set(selectableMsgIds.value) : new Set()
}
function startRename(c) { editingId.value = c.id; editingTitle.value = c.title || '' }
function cancelRename() { editingId.value = ''; editingTitle.value = '' }
async function saveRename(c) {
  const t = editingTitle.value.trim(); if (!t) { cancelRename(); return }
  try { await renameConversation(c.id, t); c.title = t; cancelRename(); toast('已重命名') } catch (e) { toast('重命名失败') }
}

onMounted(() => {
  if (window.innerWidth <= 768) convCollapsed.value = true   // 手机端默认收起侧栏
  loadConversations()
})
</script>

<style scoped>
.chat-wrap { display: flex; gap: 16px; height: calc(100vh - var(--topbar-h) - 8px); }
.conv-bar { width: 248px; flex-shrink: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 14px; display: flex; flex-direction: column; position: relative; transition: width .2s, padding .2s, opacity .2s; overflow: hidden; }
.conv-bar.collapsed { width: 0; padding: 0; border: 0; opacity: 0; }
.new-btn { width: 100%; margin-bottom: 10px; }
.conv-list { flex: 1; overflow-y: auto; margin-top: 4px; }
.conv-item { padding: 9px 11px; border-radius: var(--radius-sm); cursor: pointer; margin-bottom: 3px; position: relative; transition: background .15s; }
.conv-item:hover { background: var(--surface-2); }
.conv-item.active { background: var(--primary-soft); }
html.dark .conv-item.active { background: var(--primary-soft-2); }
.conv-title { font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 36px; }
.conv-item.active .conv-title { color: var(--primary); font-weight: 600; }
.conv-time { font-size: 11px; color: var(--text-soft); margin-top: 2px; }
.conv-ops { position: absolute; right: 8px; top: 9px; display: none; gap: 6px; }
.conv-item:hover .conv-ops { display: flex; }
.conv-ops a { cursor: pointer; font-size: 12px; opacity: .6; }
.conv-ops a:hover { opacity: 1; }
.conv-ops a.danger:hover { color: var(--danger); }
.rename-input { padding: 4px 8px; }
.collapse-btn { position: absolute; top: 14px; right: -12px; width: 24px; height: 24px; border-radius: 50%; background: var(--surface); border: 1px solid var(--border); cursor: pointer; z-index: 5; font-size: 11px; color: var(--text-muted); }
.conv-bar.collapsed .collapse-btn { right: -28px; }

.chat-main { position: relative; flex: 1; display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; min-width: 0; }
.msg-list { flex: 1; overflow-y: auto; padding: 24px; }
.msg { margin-bottom: 20px; display: flex; }
.msg.user { justify-content: flex-end; }
.bubble { max-width: 78%; }
.user-bubble { background: var(--primary); color: #fff; padding: 10px 14px; border-radius: var(--radius) var(--radius) 4px var(--radius); }
.ai-bubble { background: var(--surface-2); border: 1px solid var(--border); padding: 16px 18px; border-radius: 4px var(--radius) var(--radius) var(--radius); flex: 1; max-width: 820px; }
html.dark .ai-bubble { background: var(--surface); }
.bubble-actions { float: right; display: flex; gap: 14px; font-size: 12px; margin-bottom: 6px; }
.bubble-actions a { color: var(--primary); cursor: pointer; }
.streaming-text { margin: 0; font-family: inherit; font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-wrap: break-word; color: var(--text); }
.cursor { color: var(--primary); font-weight: bold; animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

.ans { font-size: 14px; line-height: 1.75; color: var(--text); }
.ans :first-child { margin-top: 0; }
.ans :last-child { margin-bottom: 0; }
.ans p { margin: 8px 0; }
.ans h1, .ans h2, .ans h3 { margin: 14px 0 6px; color: var(--text); }
.ans ul, .ans ol { margin: 8px 0; padding-left: 22px; }
.ans li { margin: 3px 0; }
.ans code { background: var(--surface-3); padding: 2px 6px; border-radius: 4px; font-size: .9em; color: var(--danger); }
.ans pre.hljs { background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: var(--radius-sm); overflow-x: auto; margin: 10px 0; }
.ans pre.hljs code { background: transparent; padding: 0; color: inherit; }
.ans table { border-collapse: collapse; margin: 10px 0; }
.ans th, .ans td { border: 1px solid var(--border); padding: 6px 10px; }
.ans blockquote { border-left: 3px solid var(--border); margin: 8px 0; padding-left: 12px; color: var(--text-muted); }
.cite-ref { color: var(--primary); cursor: pointer; font-weight: bold; }
.cite-ref:hover { text-decoration: underline; }

.sources { margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--border); }
.src-head { font-size: 12px; font-weight: 700; color: var(--text-muted); margin-bottom: 6px; }
.src-item { font-size: 12px; color: var(--text-muted); margin: 4px 0; padding: 7px 9px; border-radius: var(--radius-sm); background: var(--surface); border: 1px solid var(--border-soft); cursor: pointer; transition: background .15s; line-height: 1.6; }
.src-item:hover { background: var(--surface-2); }
.src-item.hi { background: var(--warning-soft); border-color: var(--warning); }
.src-item { display: flex; align-items: flex-start; gap: 6px; }
.src-main { flex: 1; min-width: 0; }
.src-line1 { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.src-badge { font-size: 10px; padding: 1px 5px; }
.src-score { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.src-score .bar { flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
.src-score .bar-fill { height: 100%; background: var(--primary, #3b82f6); }
.src-text.clamp { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cite-ref { color: var(--primary, #3b82f6); cursor: pointer; font-size: 0.75em; vertical-align: super; }
.cite-ref.cite-hot { background: var(--warning-soft); border-radius: 2px; padding: 0 2px; }
.src-no { color: var(--primary); margin-right: 4px; }
.src-doc { color: var(--info); }

.meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; font-size: 12px; color: var(--text-soft); }
.meta .fb { margin-left: auto; display: flex; gap: 8px; }
.meta .fb a { cursor: pointer; opacity: .55; font-size: 14px; }
.meta .fb a:hover, .meta .fb a.on { opacity: 1; }
.related { margin-top: 12px; }
.rq-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.rq { background: var(--surface); border: 1px solid var(--border); color: var(--primary); padding: 6px 12px; border-radius: 999px; cursor: pointer; font-size: 12px; transition: all .15s; font-family: inherit; }
.rq:hover { background: var(--primary); color: #fff; border-color: var(--primary); }

.welcome { text-align: center; padding: 60px 20px; max-width: 560px; margin: auto; }
.welcome-icon { font-size: 44px; }
.welcome-title { font-size: 20px; font-weight: 700; color: var(--text); margin: 12px 0 4px; }
.welcome-sub { font-size: 13px; color: var(--text-muted); margin-bottom: 22px; }
.qq-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.qq { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; cursor: pointer; text-align: left; font-size: 13px; color: var(--text); transition: all .15s; font-family: inherit; display: flex; align-items: center; gap: 8px; }
.qq:hover { border-color: var(--primary); background: var(--primary-soft); color: var(--primary); }
.qq-icon { opacity: .6; }

.input-bar { display: flex; gap: 10px; align-items: center; padding: 14px 18px; border-top: 1px solid var(--border); background: var(--surface); }
.model-sel { width: auto; max-width: 130px; }
.ws-toggle { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-muted); cursor: pointer; white-space: nowrap; }
.ws-toggle input { width: auto; }
.send-btn { padding: 9px 22px; }
.edit-btn { margin-left: 6px; cursor: pointer; opacity: .55; font-size: 12px; }
.edit-btn:hover { opacity: 1; }
.edit-area { background: rgba(255,255,255,.15); color: #fff; border-color: rgba(255,255,255,.3); margin-bottom: 6px; }
.edit-area::placeholder { color: rgba(255,255,255,.6); }
.edit-ops { display: flex; gap: 6px; justify-content: flex-end; }
.conv-batch { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.conv-batch .hint { color: var(--text-soft); }
.conv-check { margin-right: 6px; cursor: pointer; }
.msg-batch { position: sticky; top: 0; z-index: 6; display: flex; align-items: center; gap: 8px; padding: 6px 10px; margin-bottom: 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 12px; }
.msg-check { display: inline-flex; align-items: center; margin-right: 6px; cursor: pointer; opacity: .7; }
.msg-check:hover { opacity: 1; }
.empty.small { padding: 20px; font-size: 12px; }
.badge-cache { background: #ff9800; color: #fff; font-weight: 600; }          /* Redis 热点 L1：橙 */
.badge-cache-mysql { background: #2196f3; color: #fff; font-weight: 600; }     /* MySQL 持久 L2：蓝 */
.badge-cache-semantic { background: #9c27b0; color: #fff; font-weight: 600; }  /* 语义相似 L1.5：紫 */
.menu-btn { display: none; }

@media (max-width: 768px) {
  .menu-btn { display: flex; align-items: center; justify-content: center; position: absolute; top: 8px; left: 8px; z-index: 30; width: 30px; height: 30px; padding: 0; border-radius: var(--radius-sm); background: var(--surface); border: 1px solid var(--border); color: var(--text); font-size: 16px; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
  .conv-bar { position: absolute; left: 14px; right: 14px; bottom: 14px; top: 14px; z-index: 20; height: auto; }
  .conv-bar.collapsed { display: none; }
  .bubble { max-width: 92%; }
}
.ev-btn { font-size: 11px; cursor: pointer; color: var(--text-muted); margin-left: 6px; }
.ev-btn:hover { color: var(--primary); }
.modal.ev-modal { height: auto; max-height: 85vh; }
.ev-list { max-height: calc(85vh - 64px); overflow-y: auto; padding: 8px 0; }
.ev-item { display: flex; gap: 8px; padding: 6px 12px; border-radius: var(--radius-sm); margin-bottom: 4px; font-size: 13px; }
.ev-supported { background: var(--surface-2); border-left: 3px solid var(--success); }
.ev-unsupported { background: var(--surface-2); border-left: 3px solid var(--warning); }
.ev-icon { flex-shrink: 0; font-size: 14px; }
.ev-body { flex: 1; }
.ev-text { color: var(--text); line-height: 1.5; }
.ev-src { font-size: 12px; margin-top: 2px; color: var(--text-muted); }
.ev-ref { cursor: pointer; color: var(--primary); font-weight: 600; margin-right: 4px; }
.ev-ref:hover { text-decoration: underline; }
.ev-nosrc { font-size: 11px; color: var(--warning); margin-top: 2px; }
</style>
