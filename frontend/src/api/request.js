import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import router from '../router'

const request = axios.create({ baseURL: '/api', timeout: 60000 })

request.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) config.headers.Authorization = `Bearer ${auth.token}`
  return config
})

// 统一解包 {code,message,data}；401 自动登出；403 全局提示「无权限」
request.interceptors.response.use(
  (res) => {
    const d = res.data
    if (d && d.code === 401) {
      useAuthStore().logout()
      router.push('/login')
    }
    if (d && d.code === 403) {
      // 后端 require_perm 拒绝；派发全局通知，不依赖各视图各自处理
      window.dispatchEvent(new CustomEvent('app:notify', { detail: { msg: '⛔ ' + (d.message || '无权限执行此操作') } }))
    }
    return d
  },
  (err) => Promise.reject(err)
)

export default request
