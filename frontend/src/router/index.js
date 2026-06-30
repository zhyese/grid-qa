import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import AppLayout from '../views/AppLayout.vue'

const routes = [
  { path: '/login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', redirect: '/chat' },
      { path: 'chat', component: () => import('../views/Chat.vue'), meta: { auth: true, title: '智能问答', sub: '自然语言提问 · 自纠错 · 可信答案' } },
      { path: 'diagnose', component: () => import('../views/Diagnose.vue'), meta: { auth: true, title: '故障诊断', sub: '多查询分解 · 因果链 · 原因排序' } },
      { path: 'documents', component: () => import('../views/Documents.vue'), meta: { auth: true, title: '知识库', sub: '上传 · 解析 · 向量化 · 预览' } },
      { path: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { auth: true, title: '统计看板', sub: '知识库规模 + 故障趋势' } },
      { path: 'kg', component: () => import('../views/KgGraph.vue'), meta: { auth: true, title: '知识图谱', sub: '设备-故障-处置 多跳推理' } },
      { path: 'admin', component: () => import('../views/Admin.vue'), meta: { auth: true, admin: true, title: '系统管理', sub: '反馈 · 日志 · 配置' } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.auth && !auth.token) return '/login'
  if (to.meta.admin && auth.role !== 'admin') return '/chat'
})

export default router
