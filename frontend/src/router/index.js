import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', component: () => import('../views/Login.vue') },
  { path: '/', redirect: '/chat' },
  { path: '/chat', component: () => import('../views/Chat.vue'), meta: { auth: true } },
  { path: '/documents', component: () => import('../views/Documents.vue'), meta: { auth: true } },
  { path: '/admin', component: () => import('../views/Admin.vue'), meta: { auth: true, admin: true } },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.auth && !auth.token) return '/login'
  if (to.meta.admin && auth.role !== 'admin') return '/chat'
})

export default router
