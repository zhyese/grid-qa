<template>
  <div class="page">
    <header class="topbar">
      <span>系统管理</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/admin">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
    <div class="admin-wrap">
      <div class="card">
        <h3>操作日志（共 {{ logs.total }} 条）</h3>
        <table>
          <thead><tr><th>用户</th><th>类型</th><th>内容</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="l in logs.list" :key="l.id">
              <td>{{ l.operateUser }}</td><td>{{ l.operateType }}</td>
              <td>{{ l.content }}</td><td>{{ l.operateTime }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>Milvus 索引配置</h3>
        <input v-model="milvus.indexType" placeholder="indexType" />
        <input v-model="milvus.nprobe" placeholder="nprobe" />
        <input v-model="milvus.nlist" placeholder="nlist" />
        <button @click="saveMilvus">保存</button>
      </div>
      <div class="card">
        <h3>模型参数配置</h3>
        <input v-model="model.modelType" placeholder="modelType" />
        <input v-model="model.temperature" placeholder="temperature" />
        <button @click="saveModel">保存</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getLogs, configMilvus, configModel } from '../api'

const auth = useAuthStore()
const router = useRouter()
const logs = ref({ total: 0, list: [] })
const milvus = reactive({ indexType: 'IVF_FLAT', nprobe: 16, nlist: 1024 })
const model = reactive({ modelType: 'deepseek', temperature: 0.2 })

async function loadLogs() { logs.value = (await getLogs({ page: 1, size: 20 })).data }
async function saveMilvus() {
  await configMilvus(milvus.indexType, { nprobe: Number(milvus.nprobe), nlist: Number(milvus.nlist) })
  alert('已保存')
}
async function saveModel() {
  await configModel(model.modelType, { temperature: Number(model.temperature) })
  alert('已保存')
}
function logout() { auth.logout(); router.push('/login') }
onMounted(loadLogs)
</script>

<style scoped>
.admin-wrap { max-width: 1000px; margin: 20px auto; padding: 0 16px; }
h3 { margin: 0 0 12px; color: #1e3a8a; }
input { margin-right: 8px; width: 130px; }
</style>
