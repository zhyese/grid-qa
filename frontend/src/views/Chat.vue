<template>
  <div class="chat-wrap">
    <!-- 对话历史栏 -->
    <aside class="conv-bar" :class="{ collapsed: convCollapsed }">
      <button class="btn btn-primary new-btn" @click="newChat">＋ 新建对话</button>
      <input class="input" v-model="searchKw" placeholder="🔍 搜索对话..." @input="onSearch" />
      <div class="conv-list">
        <div v-for="c in conversations" :key="c.id" class="conv-item" :class="{ active: c.id === currentConvId }" @click="selectConv(c.id)">
          <input v-if="editingId === c.id" class="input rename-input" v-model="editingTitle" @click.stop @keyup.enter="saveRename(c)" @keyup.esc="cancelRename" />
          <template v-else>
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
      <div class="msg-list" ref="msgListEl">
        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div v-if="m.role === 'user'" class="bubble user-bubble">{{ m.content }}</div>
          <div v-else class="bubble ai-bubble">
            <div v-if="!m.streaming" class="bubble-actions">
              <a @click="copyAnswer(m)">📋 复制</a>
              <a @click="exportWord(m)">📄 导出 Word</a>
            </div>
            <pre v-if="m.streaming" class="streaming-text">{{ m.content }}<span class="cursor">▍</span></pre>
            <div v-else class="ans md" @click="onAnsClick($event, m)" v-html="renderMd(m.content)"></div>

            <div class="sources" v-if="m.sources && m.sources.length">
              <div class="src-head">📎 引用来源 <span class="hint">点 [n] 定位 · 点卡片复制</span></div>
              <div v-for="(s, j) in m.sources" :key="j" :id="'src-' + (j + 1)" class="src-item" :class="{ hi: m.hiIdx === j + 1 }" @click="copySource(s)">
                <b class="src-no">[{{ j + 1 }}]</b>
                <span class="src-doc" v-if="srcName(s)">📄 {{ srcName(s) }}</span>
                <span class="src-text">{{ srcText(s) }}</span>
              </div>
            </div>

            <div class="meta" v-if="m.time">
              <span>⏱ {{ m.time }}s</span>
              <span>· 幻觉率 {{ m.halluc }}</span>
              <span v-if="m.graphCount" class="badge badge-info">🔗 图谱{{ m.graphCount }}</span>
              <span v-if="m.highRisk && m.highRisk.length" class="badge badge-danger" :title="'高风险：' + m.highRisk.join('、')">⚠ {{ m.highRisk.slice(0, 3).join('、') }}</span>
              <span v-if="m.confidence" class="badge" :class="confBadge(m.confidence)" :title="confTitle(m.confidence)">{{ confLabel(m.confidence) }}</span>
              <span class="fb">
                <a @click="like(m)" :class="{ on: m.fb === 'like' }">👍</a>
                <a @click="dislike(m)" :class="{ on: m.fb === 'dislike' }">👎</a>
              </span>
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
        <input class="input" v-model="query" placeholder="输入运维问题，如：主变压器温度异常如何处置..." @keyup.enter="ask" />
        <button class="btn btn-primary send-btn" @click="ask" :disabled="loading">{{ loading ? '生成中...' : '发送' }}</button>
      </div>
    </section>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import xml from 'highlight.js/lib/languages/xml'
import markdown from 'highlight.js/lib/languages/markdown'
import { useAuthStore } from '../stores/auth'
import { streamAnswer, streamAnswerWS, sendFeedback, getFaithfulness, getRelatedQuestions, getConversations, getHistory, deleteConversation, renameConversation, exportAnswer } from '../api'

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

const md = new MarkdownIt({ html: false, linkify: true, breaks: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try { return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>` } catch (e) {}
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
function renderMd(text) {
  if (!text) return ''
  return md.render(String(text)).replace(/\[(\d+)\]/g, '<sup class="cite-ref" data-idx="$1">[$1]</sup>')
}

const auth = useAuthStore()
const router = useRouter()
const msgListEl = ref(null)
const query = ref('')
const modelType = ref('')
const loading = ref(false)
const messages = ref([])
const conversations = ref([])
const currentConvId = ref('')
const searchKw = ref('')
const editingId = ref('')
const editingTitle = ref('')
const convCollapsed = ref(localStorage.getItem('conv-collapsed') === '1')
const useWS = ref(false)
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
    if (r && r.data && typeof r.data.hallucination === 'number') { m.halluc = r.data.hallucination; m.judgeReason = r.data.reason || '' }
  } catch (e) {}
}

async function loadConversations() {
  try { conversations.value = (await getConversations(searchKw.value)).data || [] } catch (e) {}
}
let searchTimer = null
function onSearch() { clearTimeout(searchTimer); searchTimer = setTimeout(loadConversations, 300) }
async function selectConv(id) {
  currentConvId.value = id
  const r = await getHistory(id)
  messages.value = (r.data || []).map((m) => ({ role: m.role, content: m.content, sources: [], time: 0, halluc: 0, query: m.role === 'user' ? m.content : '', fb: '' }))
}
function newChat() { currentConvId.value = ''; messages.value = [] }

async function ask() {
  if (!query.value.trim() || loading.value) return
  const q = query.value
  messages.value.push({ role: 'user', content: q })
  query.value = ''
  loading.value = true
  const msg = reactive({ role: 'assistant', content: '', sources: [], time: 0, halluc: 0, conversationId: currentConvId.value || '', query: q, fb: '', streaming: true })
  messages.value.push(msg)
  nextTick(() => { if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight })
  const onStreamEvent = (ev) => {
    if (ev.type === 'meta') { msg.sources = ev.sources || []; if (ev.conversationId) msg.conversationId = ev.conversationId }
    else if (ev.type === 'token') { msg.content += ev.content || ''; nextTick(() => { if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight }) }
    else if (ev.type === 'done' || ev.type === 'error') {
      if (ev.content) msg.content = ev.content
      if (typeof ev.responseTime === 'number') msg.time = ev.responseTime
      if (typeof ev.hallucinationRate === 'number') msg.halluc = ev.hallucinationRate
      if (typeof ev.graphCount === 'number') msg.graphCount = ev.graphCount
      if (ev.highRisk) msg.highRisk = ev.highRisk
      if (ev.confidence) msg.confidence = ev.confidence
      if (ev.conversationId) msg.conversationId = ev.conversationId
      msg.streaming = false
      loading.value = false
      currentConvId.value = msg.conversationId
      loadConversations()
      loadRelated(msg)
      loadFaithfulness(msg)
    }
  }
  try {
    if (useWS.value) streamAnswerWS(q, modelType.value || undefined, currentConvId.value || undefined, onStreamEvent)
    else await streamAnswer(q, modelType.value || undefined, currentConvId.value || undefined, onStreamEvent)
  } catch (e) {
    msg.content += (msg.content ? '\n' : '') + '（流式中断：' + (e.message || '') + '）'
    msg.streaming = false; loading.value = false
  }
}

async function like(m) { if (m.fb) return; try { await sendFeedback(m.query, m.content, 'like', m.conversationId); m.fb = 'like' } catch (e) {} }
async function dislike(m) {
  if (m.fb) return
  const reason = window.prompt('感谢反馈，请简述答案哪里有问题（可选，用于优化）：') || ''
  try { await sendFeedback(m.query, m.content, 'dislike', m.conversationId, reason); m.fb = 'dislike' } catch (e) {}
}

function confLabel(c) { return ({ high: '✓ 高置信', medium: '⚠ 证据有限', refused: '✗ 证据不足' })[c] || '' }
function confTitle(c) { return ({ high: '检索证据充分，答案可信度高', medium: '检索相关性中等，部分内容建议人工核对', refused: '未找到强相关资料，答案已保守处理' })[c] || '' }
function confBadge(c) { return ({ high: 'badge-success', medium: 'badge-warning', refused: 'badge-danger' })[c] || 'badge-neutral' }
function srcName(s) { return typeof s === 'string' ? '' : (s.docName || '') }
function srcText(s) { return typeof s === 'string' ? s : (s.text || '') }
async function copyAnswer(m) { try { await navigator.clipboard.writeText(m.content); toast('答案已复制') } catch (e) { toast('复制失败') } }
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

async function removeConv(c) {
  if (!confirm(`删除对话「${c.title || '无标题'}」？`)) return
  try { await deleteConversation(c.id); await loadConversations(); if (currentConvId.value === c.id) newChat(); toast('已删除') } catch (e) { toast('删除失败') }
}
function startRename(c) { editingId.value = c.id; editingTitle.value = c.title || '' }
function cancelRename() { editingId.value = ''; editingTitle.value = '' }
async function saveRename(c) {
  const t = editingTitle.value.trim(); if (!t) { cancelRename(); return }
  try { await renameConversation(c.id, t); c.title = t; cancelRename(); toast('已重命名') } catch (e) { toast('重命名失败') }
}

onMounted(loadConversations)
</script>

<style scoped>
.chat-wrap { display: flex; gap: 16px; height: calc(100vh - var(--topbar-h) - 48px); }
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

.chat-main { flex: 1; display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; min-width: 0; }
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
.empty.small { padding: 20px; font-size: 12px; }
@media (max-width: 768px) {
  .conv-bar { position: absolute; left: 14px; right: 14px; bottom: 14px; top: 14px; z-index: 20; height: auto; }
  .conv-bar.collapsed { display: none; }
  .bubble { max-width: 92%; }
}
</style>
