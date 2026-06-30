<template>
  <div class="login-page">
    <div class="login-bg">
      <div class="blob b1"></div>
      <div class="blob b2"></div>
    </div>
    <div class="login-card">
      <div class="brand">
        <div class="brand-logo">电网</div>
        <div class="brand-text">运维 RAG <small>智能问答系统</small></div>
      </div>
      <p class="login-desc">基于大模型 + 检索增强生成，覆盖变电 · 配电 · 输电运维场景</p>
      <div class="field">
        <label class="field-label">用户名</label>
        <input class="input" v-model="username" placeholder="请输入用户名" @keyup.enter="doLogin" />
      </div>
      <div class="field">
        <label class="field-label">密码</label>
        <input class="input" v-model="password" type="password" placeholder="请输入密码" @keyup.enter="doLogin" />
      </div>
      <button class="btn btn-primary login-btn" @click="doLogin" :disabled="loading">
        {{ loading ? '登录中...' : '登 录' }}
      </button>
      <p class="hint">默认管理员 admin / admin123</p>
      <p class="err" v-if="err">{{ err }}</p>
      <div class="login-feats">
        <span class="badge badge-primary">自纠错 CRAG</span>
        <span class="badge badge-info">GraphRAG</span>
        <span class="badge badge-success">真 faithfulness</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { login } from '../api'

const username = ref('admin')
const password = ref('admin123')
const loading = ref(false)
const err = ref('')
const router = useRouter()
const auth = useAuthStore()

async function doLogin() {
  loading.value = true
  err.value = ''
  try {
    const r = await login(username.value, password.value)
    if (r.code === 200) { auth.setAuth(r.data); router.push('/chat') }
    else err.value = r.message
  } catch (e) { err.value = '登录失败，请检查网络' }
  loading.value = false
}
</script>

<style scoped>
.login-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; background: var(--bg); }
.login-bg { position: absolute; inset: 0; z-index: 0; }
.blob { position: absolute; border-radius: 50%; filter: blur(80px); opacity: .35; }
.b1 { width: 480px; height: 480px; background: var(--primary); top: -120px; left: -100px; }
.b2 { width: 420px; height: 420px; background: var(--accent); bottom: -120px; right: -80px; }
.login-card { position: relative; z-index: 1; width: 380px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-xl); padding: 32px; box-shadow: var(--shadow-lg); }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.brand-logo { width: 44px; height: 44px; border-radius: 12px; background: linear-gradient(135deg, var(--primary), var(--accent)); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; }
.brand-text { font-size: 18px; font-weight: 700; color: var(--text); line-height: 1.2; }
.brand-text small { display: block; font-size: 11px; font-weight: 400; color: var(--text-soft); }
.login-desc { color: var(--text-muted); font-size: 12px; margin: 0 0 22px; line-height: 1.6; }
.login-btn { width: 100%; padding: 11px; margin-top: 4px; font-size: 14px; }
.hint { color: var(--text-soft); font-size: 11px; text-align: center; margin: 12px 0 0; }
.err { color: var(--danger); font-size: 12px; text-align: center; margin: 8px 0 0; }
.login-feats { display: flex; gap: 6px; justify-content: center; margin-top: 18px; flex-wrap: wrap; }
</style>
