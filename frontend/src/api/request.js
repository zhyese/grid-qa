import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import router from '../router'

const request = axios.create({ baseURL: '/api', timeout: 60000 })

request.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) config.headers.Authorization = `Bearer ${auth.token}`
  return config
})

// 统一解包 {code,message,data}；401 自动登出
request.interceptors.response.use(
  (res) => {
    const d = res.data
    if (d && d.code === 401) {
      useAuthStore().logout()
      router.push('/login')
    }
    return d
  },
  (err) => Promise.reject(err)
)

export default request
