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
        <div class="topbar-user">
          <div class="avatar">{{ (auth.username || 'U')[0].toUpperCase() }}</div>
          <span>{{ auth.username }} <span class="muted">· {{ auth.role === 'admin' ? '管理员' : '操作员' }}</span></span>
        </div>
      </header>
      <main class="page-body">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDark, useToggle } from '@vueuse/core'
import { useAuthStore } from '../stores/auth'

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
    { to: '/documents', icon: '📄', label: '知识库' },
    { to: '/dashboard', icon: '📊', label: '统计看板' },
    { to: '/kg', icon: '🧠', label: '知识图谱' },
  ]
  if (auth.role === 'admin') {
    items.push({ to: '/retrieval-debug', icon: '🔬', label: '检索调试' })
    items.push({ to: '/admin', icon: '⚙️', label: '系统管理' })
  }
  return items
})
function isActive(to) { return route.path === to || route.path.startsWith(to + '/') }

function logout() { auth.logout(); router.push('/login') }
</script>
