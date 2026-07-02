import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',           // 允许外网/内网穿透访问
    port: 5173,
    allowedHosts: true,        // 放行 cpolar 等穿透域名（Vite 类型是 string[]|true，非 'all'）
    // 代理 /api 到后端 8001（后端固定 8001：本机 8000 被 Manager.exe 占用）
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true }
    }
  }
})
