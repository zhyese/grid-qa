<template>
  <div class="page">
    <header class="topbar">
      <span>电网运维 RAG 智能问答</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
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
            <div class="meta">耗时 {{ m.time }}s · 幻觉率 {{ m.halluc }}</div>
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
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { answer } from '../api'

const auth = useAuthStore()
const router = useRouter()
const query = ref('')
const modelType = ref('')
const loading = ref(false)
const messages = ref([])

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
    })
  } catch (e) { messages.value.push({ role: 'assistant', content: '请求失败：' + (e.message || '') }) }
  loading.value = false
}
function logout() { auth.logout(); router.push('/login') }
</script>

<style scoped>
.chat-wrap { max-width: 900px; margin: 20px auto; padding: 0 16px; }
.chat-list { min-height: 400px; }
.msg { margin-bottom: 16px; }
.msg.user .bubble { background: #e0edff; display: inline-block; padding: 10px 14px; border-radius: 8px; }
.msg.assistant .bubble { background: #fff; padding: 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.ans { white-space: pre-wrap; word-wrap: break-word; margin: 0; font-family: inherit; }
.src { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e2e8f0; font-size: 13px; }
.src-item { color: #475569; margin: 2px 0; }
.meta { color: #94a3b8; font-size: 12px; margin-top: 6px; }
.empty { color: #94a3b8; text-align: center; margin-top: 80px; }
.input-bar { display: flex; gap: 8px; margin-top: 16px; }
.input-bar input { flex: 1; }
</style>
