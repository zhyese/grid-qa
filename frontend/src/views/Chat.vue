<template>
  <div class="chat-page">
    <!-- 左侧栏：对话历史 -->
    <aside class="sidebar">
      <button class="new-btn" @click="newChat">+ 新建对话</button>
      <div class="conv-list">
        <div
          v-for="c in conversations" :key="c.id"
          class="conv-item" :class="{ active: c.id === currentConvId }"
          @click="selectConv(c.id)"
        >
          <div class="conv-title">{{ c.title || '(无标题对话)' }}</div>
          <div class="conv-time">{{ c.createdAt }}</div>
        </div>
        <div v-if="!conversations.length" class="empty-side">暂无历史对话</div>
      </div>
      <div class="side-nav">
        <router-link to="/documents">文档</router-link> ·
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> ·
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </div>
    </aside>

    <!-- 右侧：聊天区 -->
    <main class="chat-main">
      <header class="topbar"><span>电网运维 RAG 智能问答</span></header>
      <div class="chat-wrap">
        <div class="chat-list">
          <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
            <div v-if="m.role === 'user'" class="bubble"><b>提问：</b>{{ m.content }}</div>
            <div v-else class="bubble">
              <pre class="ans">{{ m.content }}</pre>
              <div class="src" v-if="m.sources && m.sources.length">
                <b>📎 引用来源：</b>
                <div v-for="(s, j) in m.sources" :key="j" class="src-item">[{{ j + 1 }}] {{ s }}</div>
              </div>
              <div class="meta" v-if="m.time">
                耗时 {{ m.time }}s · 幻觉率 {{ m.halluc }}
                <span class="fb">
                  <a class="fb-btn" @click="like(m)" :class="{ on: m.fb === 'like' }">👍</a>
                  <a class="fb-btn" @click="dislike(m)" :class="{ on: m.fb === 'dislike' }">👎</a>
                  <span v-if="m.fb" class="fb-done">已记录</span>
                </span>
              </div>
            </div>
          </div>
          <div v-if="!messages.length" class="empty">输入运维问题开始问答，例如：主变压器温度异常如何处置？</div>
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
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { answer, sendFeedback, getConversations, getHistory } from '../api'

const auth = useAuthStore()
const router = useRouter()
const query = ref('')
const modelType = ref('')
const loading = ref(false)
const messages = ref([])
const conversations = ref([])
const currentConvId = ref('')

async function loadConversations() {
  try { conversations.value = (await getConversations()).data || [] } catch (e) {}
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
  try {
    const r = await answer(q, modelType.value || undefined)
    messages.value.push({
      role: 'assistant', content: r.data.answer,
      sources: r.data.retrievalSource,
      time: r.data.responseTime, halluc: r.data.hallucinationRate,
      conversationId: r.data.conversationId, query: q, fb: '',
    })
    currentConvId.value = r.data.conversationId
    await loadConversations()  // 刷新侧栏（新对话进列表）
  } catch (e) { messages.value.push({ role: 'assistant', content: '请求失败：' + (e.message || '') }) }
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
.side-nav { font-size: 12px; padding-top: 8px; border-top: 1px solid #334155; }
.side-nav a { color: #94a3b8; text-decoration: none; }
.chat-main { flex: 1; display: flex; flex-direction: column; }
.chat-wrap { flex: 1; max-width: 900px; margin: 20px auto; padding: 0 16px; width: 100%; display: flex; flex-direction: column; }
.chat-list { flex: 1; overflow-y: auto; }
.msg { margin-bottom: 16px; }
.msg.user .bubble { background: #e0edff; display: inline-block; padding: 10px 14px; border-radius: 8px; }
.msg.assistant .bubble { background: #fff; padding: 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.ans { white-space: pre-wrap; word-wrap: break-word; margin: 0; font-family: inherit; }
.src { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e2e8f0; font-size: 13px; }
.src-item { color: #475569; margin: 2px 0; }
.meta { color: #94a3b8; font-size: 12px; margin-top: 6px; }
.fb { margin-left: 12px; }
.fb-btn { cursor: pointer; margin: 0 4px; opacity: .6; }
.fb-btn:hover, .fb-btn.on { opacity: 1; }
.fb-done { color: #16a34a; margin-left: 4px; }
.empty { color: #94a3b8; text-align: center; margin-top: 80px; }
.input-bar { display: flex; gap: 8px; margin-top: 16px; }
.input-bar input { flex: 1; }
</style>
