<template>
  <div class="app-layout">
    <aside class="sidebar" :class="{ collapsed, 'mobile-open': mobileOpen }">
      <div class="brand">
        <div class="brand-logo">电网</div>
        <div class="brand-text">运维 RAG<small>智能问答系统</small></div>
      </div>
      <nav class="nav-list">
        <div class="nav-section">工作台</div>
        <router-link v-for="n in navItems" :key="n.to" :to="n.to" class="nav-item"
                     :class="{ active: isActive(n.to) }" @click="mobileOpen = false">
          <span class="nav-icon">{{ n.icon }}</span>
          <span class="nav-label">{{ n.label }}</span>
        </router-link>
      </nav>
      <div class="sidebar-footer">
        <a class="nav-item" @click="toggleDark()" :title="isDark ? '切亮色' : '切暗色'">
          <span class="nav-icon">{{ isDark ? '☀️' : '🌙' }}</span>
          <span class="nav-label">{{ isDark ? '亮色' : '暗色' }}</span>
        </a>
        <a class="nav-item" @click="logout" title="退出登录">
          <span class="nav-icon">🚪</span>
          <span class="nav-label">退出</span>
        </a>
      </div>
    </aside>

    <div class="main-area" :class="{ expanded: collapsed }">
      <header class="topbar">
        <button class="icon-btn" @click="toggleSidebar" title="折叠侧栏">☰</button>
        <div class="topbar-title">
          {{ title }}
          <span class="topbar-sub" v-if="sub">{{ sub }}</span>
        </div>
        <div class="topbar-spacer"></div>
        <slot name="actions" />
        <div class="topbar-user" style="cursor:pointer" @click="router.push('/profile')" title="个人资料 · 改密码">
          <div class="avatar">{{ (auth.username || 'U')[0].toUpperCase() }}</div>
          <span>{{ auth.username }} <span class="muted">· {{ ROLE_LABEL[auth.role] || auth.role }}</span></span>
        </div>
      </header>
      <main class="page-body">
        <router-view />
      </main>
    </div>
    <div class="global-toast" v-if="notifyMsg">{{ notifyMsg }}</div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDark, useToggle } from '@vueuse/core'
import { useAuthStore } from '../stores/auth'
import { hasPerm, ROLE_LABEL } from '../utils/perm'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const isDark = useDark()
const toggleDark = useToggle(isDark)

const collapsed = ref(localStorage.getItem('nav-collapsed') === '1')
const mobileOpen = ref(false)
function toggleSidebar() {
  // 移动端：开抽屉；桌面端：折叠
  if (window.innerWidth <= 768) { mobileOpen.value = !mobileOpen.value; return }
  collapsed.value = !collapsed.value
  localStorage.setItem('nav-collapsed', collapsed.value ? '1' : '0')
}

const title = computed(() => route.meta.title || '电网运维 RAG')
const sub = computed(() => route.meta.sub || '')

const navItems = computed(() => {
  const items = [
    { to: '/chat', icon: '💬', label: '智能问答' },
    { to: '/diagnose', icon: '🩺', label: '故障诊断' },
    { to: '/operations', icon: '⚡', label: '主动运维' },
    { to: '/documents', icon: '📄', label: '知识库' },
    { to: '/dashboard', icon: '📊', label: '统计看板' },
    { to: '/kg', icon: '🧠', label: '知识图谱' },
    { to: '/kg-3d', icon: '🌐', label: '3D图谱' },
    { to: '/twin', icon: '🏭', label: '数字孪生' },
    { to: '/ticket', icon: '📋', label: '两票管理' },
  ]
  if (hasPerm(auth.role, 'doc:manage')) {
    items.splice(4, 0, { to: '/knowledge-governance', icon: '🧭', label: '知识治理' })
    items.splice(5, 0, { to: '/knowledge-evolution', icon: '🧬', label: '知识自进化' })
  }
  if (hasPerm(auth.role, 'metric:read')) {
    items.push({ to: '/prediction', icon: '🔮', label: '故障预测' })
  }
  if (hasPerm(auth.role, 'system:config')) {
    items.push({ to: '/retrieval-debug', icon: '🔬', label: '检索调试' })
  }
  // 系统管理：管理员全权 + 审计员只读审计（告警/审计/反馈/成本/评测 Tab）
  if (hasPerm(auth.role, 'system:config') || hasPerm(auth.role, 'alert:read')) {
    items.push({ to: '/admin', icon: '⚙️', label: '系统管理' })
  }
  return items
})
function isActive(to) { return route.path === to || route.path.startsWith(to + '/') }

function logout() { auth.logout(); router.push('/login') }

// 全局 403/无权限提示（request.js 拦截 code=403 派发 app:notify）
const notifyMsg = ref('')
let notifyTimer = null
function onNotify(e) {
  notifyMsg.value = e.detail.msg
  clearTimeout(notifyTimer)
  notifyTimer = setTimeout(() => (notifyMsg.value = ''), 2600)
}
onMounted(() => window.addEventListener('app:notify', onNotify))
onUnmounted(() => window.removeEventListener('app:notify', onNotify))
</script>

<style scoped>
.global-toast {
  position: fixed; top: 18px; left: 50%; transform: translateX(-50%);
  background: var(--danger, #e74c3c); color: #fff; padding: 10px 20px;
  border-radius: 8px; font-size: 14px; z-index: 9999;
  box-shadow: 0 6px 20px rgba(0,0,0,.25);
}
</style>
