import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 代理 /api 到后端 8001（后端固定 8001：本机 8000 被 Manager.exe 占用）
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true }
    }
  }
})
