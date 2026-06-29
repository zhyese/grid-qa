<template>
  <div class="login-wrap">
    <div class="login-card">
      <h2>电网运维 RAG 智能问答系统</h2>
      <input v-model="username" placeholder="用户名" @keyup.enter="doLogin" />
      <input v-model="password" type="password" placeholder="密码" @keyup.enter="doLogin" />
      <button @click="doLogin" :disabled="loading">{{ loading ? '登录中...' : '登录' }}</button>
      <p class="tip">默认管理员 admin / admin123</p>
      <p class="err" v-if="err">{{ err }}</p>
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
.login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #1e3a8a, #2563eb); }
.login-card { background: #fff; padding: 32px; border-radius: 12px; width: 340px; display: flex; flex-direction: column; gap: 12px; }
.login-card h2 { margin: 0 0 8px; font-size: 18px; color: #1e3a8a; }
.tip { color: #94a3b8; font-size: 12px; margin: 0; }
.err { color: #dc2626; font-size: 13px; margin: 0; }
</style>
