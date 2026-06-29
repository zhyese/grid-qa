<template>
  <div class="page">
    <header class="topbar">
      <span>文档管理</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
    <div class="doc-wrap">
      <div class="card">
        <h3>上传文档</h3>
        <input type="file" multiple @change="onFile" />
        <select v-model="docType">
          <option>运维手册</option><option>故障案例</option><option>检修规程</option>
        </select>
        <button @click="upload" :disabled="!files.length || uploading">
          {{ uploading ? '上传中...' : '上传' }}
        </button>
        <p class="tip">支持 PDF/Word/TXT/图片(OCR)，批量≤5份，单份≤100MB</p>
      </div>
      <div class="card">
        <table>
          <thead><tr><th>文件名</th><th>类型</th><th>状态</th><th>分块</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="d in docs" :key="d.docId">
              <td>{{ d.docName }}</td><td>{{ d.docType }}</td>
              <td><span :class="'st-' + d.status">{{ statusMap[d.status] || d.status }}</span></td>
              <td>{{ d.chunkCount }}</td>
              <td>
                <button class="secondary" @click="parseDoc(d.docId)" :disabled="busy[d.docId]">解析</button>
                <button class="secondary" @click="vectorizeDoc(d.docId)" :disabled="busy[d.docId]">向量化</button>
                <button class="danger" @click="removeDoc(d.docId)" :disabled="busy[d.docId]">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { listDocs, uploadDocs, parseDocs, vectorize, deleteDoc } from '../api'

const auth = useAuthStore()
const router = useRouter()
const docs = ref([])
const files = ref([])
const docType = ref('运维手册')
const uploading = ref(false)
const busy = reactive({})
const statusMap = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }

async function load() { docs.value = (await listDocs()).data }
function onFile(e) { files.value = Array.from(e.target.files) }
async function upload() {
  const form = new FormData()
  files.value.forEach((f) => form.append('files', f))
  form.append('docType', docType.value)
  uploading.value = true
  try { await uploadDocs(form); await load() } catch (e) {}
  uploading.value = false
  files.value = []
}
async function parseDoc(id) { busy[id] = true; try { await parseDocs([id]); await load() } finally { busy[id] = false } }
async function vectorizeDoc(id) { busy[id] = true; try { await vectorize(id); await load() } finally { busy[id] = false } }
async function removeDoc(id) {
  if (!confirm('确认删除该文档（含向量）？')) return
  busy[id] = true; try { await deleteDoc(id); await load() } finally { busy[id] = false }
}
function logout() { auth.logout(); router.push('/login') }
onMounted(load)
</script>

<style scoped>
.doc-wrap { max-width: 1000px; margin: 20px auto; padding: 0 16px; }
h3 { margin: 0 0 12px; color: #1e3a8a; }
.tip { color: #94a3b8; font-size: 12px; }
button { margin-left: 6px; }
.st-pending { color: #94a3b8; }
.st-parsed { color: #0891b2; }
.st-vectorized { color: #16a34a; }
</style>
