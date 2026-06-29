<template>
  <div class="chat-page">
    <!-- 左侧栏：对话历史 -->
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <button class="new-btn" @click="newChat">+ 新建对话</button>
      <input class="search-box" v-model="searchKw" placeholder="🔍 搜索对话..." @input="onSearch" />
      <div class="conv-list">
        <div
          v-for="c in conversations" :key="c.id"
          class="conv-item" :class="{ active: c.id === currentConvId }"
          @click="selectConv(c.id)"
        >
          <input v-if="editingId === c.id" class="rename-input" v-model="editingTitle"
                 @click.stop @keyup.enter="saveRename(c)" @keyup.esc="cancelRename" />
          <template v-else>
            <div class="conv-title">{{ c.title || '(无标题对话)' }}</div>
            <div class="conv-time">{{ c.createdAt }}</div>
            <div class="conv-ops" @click.stop>
              <a class="op" @click="startRename(c)" title="重命名">✏️</a>
              <a class="op danger" @click="removeConv(c)" title="删除">🗑️</a>
            </div>
          </template>
        </div>
        <div v-if="!conversations.length" class="empty-side">{{ searchKw ? '无匹配对话' : '暂无历史对话' }}</div>
      </div>
      <div class="side-nav">
        <router-link to="/dashboard">统计</router-link> ·
        <router-link to="/kg">图谱</router-link> ·
        <router-link to="/documents">文档</router-link> ·
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> ·
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </div>
    </aside>

    <!-- 右侧：聊天区 -->
    <main class="chat-main">
      <header class="topbar">
        <span class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</span>
        <span>电网运维 RAG 智能问答</span>
      </header>
      <div class="chat-wrap">
        <div class="chat-list">
          <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
            <div v-if="m.role === 'user'" class="bubble"><b>提问：</b>{{ m.content }}</div>
            <div v-else class="bubble">
              <div v-if="!m.streaming" class="ans-actions">
                <a class="cp-btn" @click="copyAnswer(m)">📋 复制答案</a>
              </div>
              <pre v-if="m.streaming" class="ans">{{ m.content }}<span class="cursor">▍</span></pre>
              <div v-else class="ans md" @click="onAnsClick($event, m)" v-html="renderMd(m.content)"></div>
              <div class="src" v-if="m.sources && m.sources.length">
                <b>📎 引用来源：</b> <span class="src-hint">点数字标注定位 · 点卡片复制</span>
                <div v-for="(s, j) in m.sources" :key="j" :id="'src-' + (j + 1)"
                     class="src-item" :class="{ hi: m.hiIdx === j + 1 }" @click="copySource(s)">
                  <b class="src-no">[{{ j + 1 }}]</b>
                  <span class="src-doc" v-if="srcName(s)">📄 {{ srcName(s) }}</span>
                  <span class="src-text">{{ srcText(s) }}</span>
                </div>
              </div>
              <div class="meta" v-if="m.time">
                耗时 {{ m.time }}s · 幻觉率 {{ m.halluc }}
                <span v-if="m.graphCount" class="kg-tag" title="本次问答融合的知识图谱结构化三元组数">🔗 图谱{{ m.graphCount }}</span>
                <span class="fb">
                  <a class="fb-btn" @click="like(m)" :class="{ on: m.fb === 'like' }">👍</a>
                  <a class="fb-btn" @click="dislike(m)" :class="{ on: m.fb === 'dislike' }">👎</a>
                  <span v-if="m.fb" class="fb-done">已记录</span>
                </span>
              </div>
              <div class="related" v-if="!m.streaming && m.related && m.related.length">
                <b>💡 相关追问：</b>
                <div class="rq-list">
                  <button class="rq" v-for="(rq, k) in m.related" :key="k" @click="askRelated(rq)">{{ rq }}</button>
                </div>
              </div>
            </div>
          </div>
          <div v-if="!messages.length" class="empty">
            <div class="welcome">👋 欢迎使用电网运维智能问答</div>
            <div class="quick-hint">试试这些问题：</div>
            <div class="qq-list">
              <button class="qq" v-for="q in quickQuestions" :key="q" @click="quickAsk(q)">{{ q }}</button>
            </div>
          </div>
        </div>
        <div class="input-bar">
          <select v-model="modelType">
            <option value="">默认模型</option>
            <option value="deepseek">DeepSeek</option>
            <option value="qwen">通义千问</option>
            <option value="doubao">豆包</option>
          </select>
          <input v-model="query" placeholder="输入运维问题..." @keyup.enter="ask" />
          <button @click="ask" :disabled="loading">{{ loading ? '生成中...' : '提问' }}</button>
        </div>
      </div>
    </main>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'   // 仅按需注册语言，避免打包全部 190+ 语言
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import xml from 'highlight.js/lib/languages/xml'
import markdown from 'highlight.js/lib/languages/markdown'
import { useAuthStore } from '../stores/auth'

// 注册电网运维/代码场景常用语言（其余语言回退为纯文本转义）
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
import { streamAnswer, sendFeedback, getRelatedQuestions, getConversations, getHistory, deleteConversation, renameConversation } from '../api'

// F1: Markdown 渲染 + 代码高亮
const md = new MarkdownIt({
  html: false,        // 禁止内嵌 HTML，防注入
  linkify: true,      // 自动识别链接
  breaks: true,       // 换行转 <br>
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`
      } catch (e) { /* fallthrough */ }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
function renderMd(text) {
  if (!text) return ''
  let html = md.render(String(text))
  // 引用标注 [n] → 可点击上标，点击定位到来源卡片
  html = html.replace(/\[(\d+)\]/g, '<sup class="cite-ref" data-idx="$1">[$1]</sup>')
  return html
}

const auth = useAuthStore()
const router = useRouter()
const query = ref('')
const modelType = ref('')
const loading = ref(false)
const messages = ref([])
const conversations = ref([])
const currentConvId = ref('')
const searchKw = ref('')
const editingId = ref('')
const editingTitle = ref('')
const sidebarOpen = ref(false)
const quickQuestions = [
  '主变压器温度异常如何处置？',
  '配电线路单相接地故障如何排查？',
  'SF6断路器漏气该如何处理？',
  '变压器日常巡视检查哪些项目？',
]
function quickAsk(q) { query.value = q; ask() }

// 智能推荐相关问题：答案渲染后异步拉取（不阻塞流式），点击直接追问
function askRelated(q) { query.value = q; ask() }
async function loadRelated(m) {
  try {
    const r = await getRelatedQuestions(m.query, m.content, modelType.value || undefined)
    m.related = (r.data && r.data.questions) || []
  } catch (e) { m.related = [] }
}

async function loadConversations() {
  try { conversations.value = (await getConversations(searchKw.value)).data || [] } catch (e) {}
}
let searchTimer = null
function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(loadConversations, 300)   // 防抖 300ms
}

async function selectConv(id) {
  currentConvId.value = id
  const r = await getHistory(id)
  messages.value = (r.data || []).map((m) => ({
    role: m.role, content: m.content, sources: [], time: 0, halluc: 0,
    query: m.role === 'user' ? m.content : '', fb: '',
  }))
}

function newChat() {
  currentConvId.value = ''
  messages.value = []
}

async function ask() {
  if (!query.value.trim() || loading.value) return
  const q = query.value
  messages.value.push({ role: 'user', content: q })
  query.value = ''
  loading.value = true
  // 预置空 assistant 消息（reactive），逐 token 追加 → 打字机效果
  const msg = reactive({
    role: 'assistant', content: '', sources: [], time: 0, halluc: 0,
    conversationId: currentConvId.value || '', query: q, fb: '', streaming: true,
  })
  messages.value.push(msg)
  try {
    await streamAnswer(q, modelType.value || undefined, currentConvId.value || undefined, (ev) => {
      if (ev.type === 'meta') {
        msg.sources = ev.sources || []
        if (ev.conversationId) msg.conversationId = ev.conversationId
      } else if (ev.type === 'token') {
        msg.content += ev.content || ''        // 打字机：逐字追加
      } else if (ev.type === 'done') {
        if (ev.content) msg.content = ev.content               // 无来源时的兜底文案
        if (typeof ev.responseTime === 'number') msg.time = ev.responseTime
        if (typeof ev.hallucinationRate === 'number') msg.halluc = ev.hallucinationRate
        if (typeof ev.graphCount === 'number') msg.graphCount = ev.graphCount
        if (ev.conversationId) msg.conversationId = ev.conversationId
        msg.streaming = false
        currentConvId.value = msg.conversationId
        loadConversations()      // 刷新侧栏（新对话进列表）
        loadRelated(msg)          // 智能推荐：答案渲染后异步拉取 3 个相关问题（不阻塞流式）
      }
    })
  } catch (e) {
    msg.content += (msg.content ? '\n' : '') + '（流式中断：' + (e.message || '') + '）'
    msg.streaming = false
  }
  loading.value = false
}

async function like(m) {
  if (m.fb) return
  try { await sendFeedback(m.query, m.content, 'like', m.conversationId); m.fb = 'like' } catch (e) {}
}
async function dislike(m) {
  if (m.fb) return
  try { await sendFeedback(m.query, m.content, 'dislike', m.conversationId); m.fb = 'dislike' } catch (e) {}
}

// F3: 引用溯源 + 复制（来源对象兼容字符串/对象，历史消息可能为空）
function srcName(s) { return typeof s === 'string' ? '' : (s.docName || '') }
function srcText(s) { return typeof s === 'string' ? s : (s.text || '') }
async function copyAnswer(m) {
  try { await navigator.clipboard.writeText(m.content); toast('答案已复制') } catch (e) { toast('复制失败') }
}
async function copySource(s) {
  try { await navigator.clipboard.writeText(srcText(s)); toast('来源已复制') } catch (e) {}
}
// 事件委托：点击答案里的 [n] → 高亮并滚动到对应来源卡片
function onAnsClick(e, m) {
  const el = e.target.closest('.cite-ref')
  if (!el) return
  const idx = Number(el.dataset.idx)
  m.hiIdx = idx
  const target = document.getElementById('src-' + idx)
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
const toastMsg = ref('')
let toastTimer = null
function toast(msg) {
  toastMsg.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toastMsg.value = ''), 1500)
}

// F4: 对话管理（删除/重命名/搜索）
async function removeConv(c) {
  if (!confirm(`删除对话「${c.title || '无标题'}」？`)) return
  try {
    await deleteConversation(c.id); await loadConversations()
    if (currentConvId.value === c.id) newChat()
    toast('已删除')
  } catch (e) { toast('删除失败') }
}
function startRename(c) { editingId.value = c.id; editingTitle.value = c.title || '' }
function cancelRename() { editingId.value = ''; editingTitle.value = '' }
async function saveRename(c) {
  const t = editingTitle.value.trim()
  if (!t) { cancelRename(); return }
  try { await renameConversation(c.id, t); c.title = t; cancelRename(); toast('已重命名') } catch (e) { toast('重命名失败') }
}

function logout() { auth.logout(); router.push('/login') }
onMounted(loadConversations)
</script>

<style scoped>
.chat-page { display: flex; min-height: 100vh; }
.sidebar {
  width: 240px; background: #1e293b; color: #cbd5e1;
  display: flex; flex-direction: column; padding: 12px;
}
.new-btn { background: #2563eb; margin-bottom: 12px; }
.conv-list { flex: 1; overflow-y: auto; }
.conv-item { padding: 10px; border-radius: 6px; cursor: pointer; margin-bottom: 4px; }
.conv-item:hover { background: #334155; }
.conv-item.active { background: #2563eb; color: #fff; }
.conv-title { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-time { font-size: 11px; opacity: .6; margin-top: 2px; }
.empty-side { color: #64748b; font-size: 12px; padding: 12px; }
.search-box { width: 100%; padding: 7px 10px; margin-bottom: 10px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; border-radius: 6px; box-sizing: border-box; }
.search-box:focus { outline: none; border-color: #2563eb; }
.conv-item { position: relative; }
.conv-ops { position: absolute; right: 6px; top: 8px; display: none; gap: 4px; }
.conv-item:hover .conv-ops { display: flex; }
.op { cursor: pointer; font-size: 13px; opacity: .7; }
.op:hover { opacity: 1; }
.op.danger:hover { color: #ef4444; }
.rename-input { width: 100%; background: #0f172a; color: #e2e8f0; border: 1px solid #2563eb; border-radius: 4px; padding: 3px 6px; box-sizing: border-box; }
.side-nav { font-size: 12px; padding-top: 8px; border-top: 1px solid #334155; }
.side-nav a { color: #94a3b8; text-decoration: none; }
.chat-main { flex: 1; display: flex; flex-direction: column; }
.chat-wrap { flex: 1; max-width: 900px; margin: 20px auto; padding: 0 16px; width: 100%; display: flex; flex-direction: column; }
.chat-list { flex: 1; overflow-y: auto; }
.msg { margin-bottom: 16px; }
.msg.user .bubble { background: #e0edff; display: inline-block; padding: 10px 14px; border-radius: 8px; }
.msg.assistant .bubble { background: #fff; padding: 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.ans { margin: 0; font-family: inherit; line-height: 1.7; word-wrap: break-word; }
.ans :first-child { margin-top: 0; }
.ans :last-child { margin-bottom: 0; }
.ans p { margin: 8px 0; }
.ans h1, .ans h2, .ans h3, .ans h4 { margin: 12px 0 6px; color: #0f172a; }
.ans ul, .ans ol { margin: 8px 0; padding-left: 22px; }
.ans li { margin: 3px 0; }
.ans code { background: #f1f5f9; padding: 2px 5px; border-radius: 4px; font-size: .9em; color: #be123c; }
.ans pre.hljs { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 6px; overflow-x: auto; margin: 8px 0; }
.ans pre.hljs code { background: transparent; padding: 0; color: inherit; }
.ans table { border-collapse: collapse; margin: 8px 0; }
.ans th, .ans td { border: 1px solid #e2e8f0; padding: 6px 10px; }
.ans blockquote { border-left: 3px solid #cbd5e1; margin: 8px 0; padding-left: 12px; color: #64748b; }
.src { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e2e8f0; font-size: 13px; }
.src-item { color: #475569; margin: 2px 0; }
.meta { color: #94a3b8; font-size: 12px; margin-top: 6px; }
.kg-tag { display: inline-block; margin-left: 10px; padding: 1px 8px; background: #ddd6fe; color: #6d28d9; border-radius: 10px; font-size: 11px; }
.fb { margin-left: 12px; }
.related { margin-top: 10px; font-size: 13px; }
.rq-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.rq { background: var(--surface-2); border: 1px solid var(--border); color: var(--primary); padding: 6px 12px; border-radius: 16px; cursor: pointer; font-size: 12px; transition: all .15s; }
.rq:hover { background: var(--primary); color: #fff; }
.fb-btn { cursor: pointer; margin: 0 4px; opacity: .6; }
.fb-btn:hover, .fb-btn.on { opacity: 1; }
.cursor { color: #2563eb; font-weight: bold; animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }
.cite-ref { color: #2563eb; cursor: pointer; font-weight: bold; }
.cite-ref:hover { text-decoration: underline; }
.ans-actions { text-align: right; margin-bottom: 4px; }
.cp-btn { font-size: 12px; color: #2563eb; cursor: pointer; }
.src-hint { color: #94a3b8; font-size: 11px; font-weight: normal; }
.src-item { color: #475569; margin: 4px 0; padding: 6px 8px; border-radius: 4px; cursor: pointer; transition: background .2s; line-height: 1.5; }
.src-item:hover { background: #f1f5f9; }
.src-item.hi { background: #fef3c7; }
.src-no { color: #2563eb; margin-right: 4px; }
.src-doc { color: #0369a1; font-size: 12px; }
.toast { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 8px 18px; border-radius: 6px; z-index: 999; font-size: 13px; box-shadow: 0 4px 12px rgba(0,0,0,.15); }
.fb-done { color: #16a34a; margin-left: 4px; }
.empty { text-align: center; margin-top: 60px; }
.welcome { font-size: 20px; font-weight: bold; color: #1e3a8a; margin-bottom: 8px; }
.quick-hint { color: #64748b; font-size: 13px; margin-bottom: 14px; }
.qq-list { display: flex; flex-direction: column; gap: 10px; max-width: 420px; margin: 0 auto; }
.qq { background: var(--surface); border: 1px solid var(--border); color: var(--primary); padding: 11px 16px; border-radius: 8px; cursor: pointer; text-align: left; transition: all .15s; font-size: 14px; }
.qq:hover { background: var(--primary); color: #fff; transform: translateX(4px); }
.menu-btn { display: none; cursor: pointer; margin-right: 12px; font-size: 20px; color: #fff; }
@media (max-width: 768px) {
  .menu-btn { display: block; }
  .sidebar { position: fixed; left: 0; top: 0; bottom: 0; z-index: 100; transform: translateX(-100%); transition: transform .25s; box-shadow: 2px 0 12px rgba(0,0,0,.2); }
  .sidebar.open { transform: translateX(0); }
  .chat-wrap { margin: 12px auto; padding: 0 8px; }
  .input-bar { flex-wrap: wrap; }
  .input-bar select { width: 100%; }
  .input-bar input { min-width: 100%; }
}
.input-bar { display: flex; gap: 8px; margin-top: 16px; }
.input-bar input { flex: 1; }
</style>
