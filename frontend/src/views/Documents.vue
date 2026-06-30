<template>
  <div class="page">
    <header class="topbar">
      <span>文档管理</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/dashboard">统计</router-link> |
        <router-link to="/admin" v-if="auth.role === 'admin'">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
    <div class="doc-wrap">
      <!-- 上传区：拖拽 + 进度 -->
      <div class="card">
        <h3>上传文档</h3>
        <div class="dropzone" :class="{ over: dragOver }"
             @dragover.prevent="dragOver = true"
             @dragleave.prevent="dragOver = false"
             @drop.prevent="onDrop">
          <p v-if="!files.length">📂 拖拽文件到此处，或点击下方按钮选择</p>
          <p v-else>已选 {{ files.length }} 个文件</p>
          <ul v-if="files.length" class="file-list">
            <li v-for="(f, i) in files" :key="i">{{ f.name }} <span class="sz">({{ fmtSize(f.size) }})</span></li>
          </ul>
          <input type="file" multiple @change="onFile" />
        </div>
        <div class="upload-opts">
          <select v-model="docType">
            <option>运维手册</option><option>故障案例</option><option>检修规程</option><option>其他</option>
          </select>
          <button @click="upload" :disabled="!files.length || uploading">
            {{ uploading ? '上传中...' : '上传' }}
          </button>
          <button class="ghost" v-if="files.length" @click="files = []">清空</button>
        </div>
        <div class="progress" v-if="uploading">
          <div class="bar" :style="{ width: progress + '%' }"></div>
          <span>{{ progress }}%</span>
        </div>
        <p class="tip">支持 PDF/Word/TXT/图片(OCR)，批量≤5份，单份≤100MB</p>
      </div>

      <!-- 文档列表：筛选 + 批量 -->
      <div class="card">
        <h3>文档列表（共 {{ filtered.length }} 份）</h3>
        <div class="filters">
          <input v-model="filterKw" placeholder="🔍 按文件名筛选..." />
          <select v-model="filterType">
            <option value="">全部类型</option>
            <option v-for="t in types" :key="t">{{ t }}</option>
          </select>
          <select v-model="filterStatus">
            <option value="">全部状态</option>
            <option value="pending">待解析</option>
            <option value="parsed">已解析</option>
            <option value="vectorized">已向量化</option>
          </select>
          <button class="secondary" @click="batchParse" :disabled="!selected.length">批量解析({{ selected.length }})</button>
          <button class="danger" @click="batchDelete" :disabled="!selected.length">批量删除</button>
        </div>
        <table>
          <thead><tr>
            <th class="cb"><input type="checkbox" :checked="allChecked" @change="toggleAll($event.target.checked)" /></th>
            <th>文件名</th><th>类型</th><th>状态</th><th>分块</th><th>关联设备</th><th>操作</th>
          </tr></thead>
          <tbody v-if="loading">
            <tr v-for="i in 5" :key="i"><td colspan="7"><div class="skeleton"></div></td></tr>
          </tbody>
          <tbody v-else>
            <tr v-for="d in filtered" :key="d.docId">
              <td class="cb"><input type="checkbox" :value="d.docId" v-model="selected" /></td>
              <td>{{ d.docName }}</td><td>{{ d.docType }}</td>
              <td><span :class="'st-' + d.status">{{ statusMap[d.status] || d.status }}</span></td>
              <td>{{ d.chunkCount }}</td>
              <td class="eq">{{ (d.equipmentTags || '').split(',').filter(Boolean).slice(0, 3).join('、') || '—' }}</td>
              <td>
                <button class="secondary" @click="parseDoc(d.docId)" :disabled="busy[d.docId]">解析</button>
                <button class="secondary" @click="vectorizeDoc(d.docId)" :disabled="busy[d.docId]">向量化</button>
                <button class="danger" @click="removeDoc(d.docId)" :disabled="busy[d.docId]">删除</button>
              </td>
            </tr>
            <tr v-if="!filtered.length"><td colspan="7" class="empty-row">无匹配文档</td></tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { listDocs, uploadDocs, parseDocs, vectorize, deleteDoc } from '../api'

const auth = useAuthStore()
const router = useRouter()
const docs = ref([])
const files = ref([])
const docType = ref('运维手册')
const uploading = ref(false)
const progress = ref(0)
const dragOver = ref(false)
const busy = reactive({})
const selected = ref([])
const filterKw = ref('')
const filterType = ref('')
const filterStatus = ref('')
const statusMap = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }

const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1500) }

const types = computed(() => [...new Set(docs.value.map((d) => d.docType))])
const filtered = computed(() =>
  docs.value.filter((d) =>
    (!filterKw.value || (d.docName || '').includes(filterKw.value)) &&
    (!filterType.value || d.docType === filterType.value) &&
    (!filterStatus.value || d.status === filterStatus.value)
  )
)
const allChecked = computed(() => filtered.value.length > 0 && selected.value.length === filtered.value.length)

const loading = ref(false)
async function load() {
  loading.value = true
  try { const r = await listDocs(); docs.value = r.data.list || [] } finally { loading.value = false }
}
function fmtSize(b) {
  if (!b) return '0B'
  if (b < 1024) return b + 'B'
  if (b < 1048576) return (b / 1024).toFixed(1) + 'KB'
  return (b / 1048576).toFixed(1) + 'MB'
}
function onFile(e) { files.value = [...files.value, ...Array.from(e.target.files)]; e.target.value = '' }
function onDrop(e) { dragOver.value = false; files.value = [...files.value, ...Array.from(e.dataTransfer.files)] }
async function upload() {
  if (!files.value.length) return
  const form = new FormData()
  files.value.forEach((f) => form.append('files', f))
  form.append('docType', docType.value)
  uploading.value = true; progress.value = 0
  try {
    await uploadDocs(form, (e) => { if (e.total) progress.value = Math.round((e.loaded / e.total) * 100) })
    await load(); toast('上传成功'); files.value = []
  } catch (e) { toast('上传失败') }
  uploading.value = false
}
function toggleAll(checked) { selected.value = checked ? filtered.value.map((d) => d.docId) : [] }
async function parseDoc(id) { busy[id] = true; try { await parseDocs([id]); await load(); toast('解析完成') } finally { busy[id] = false } }
async function vectorizeDoc(id) { busy[id] = true; try { await vectorize(id); await load(); toast('向量化完成') } finally { busy[id] = false } }
async function removeDoc(id) {
  if (!confirm('确认删除该文档（含向量）？')) return
  busy[id] = true; try { await deleteDoc(id); await load(); toast('已删除') } finally { busy[id] = false }
}
async function batchParse() {
  if (!selected.value.length) return
  try { await parseDocs(selected.value); await load(); toast(`已提交解析 ${selected.value.length} 份`); selected.value = [] }
  catch (e) { toast('批量解析失败') }
}
async function batchDelete() {
  if (!selected.value.length || !confirm(`确认删除选中的 ${selected.value.length} 份文档？`)) return
  try { await Promise.all(selected.value.map((id) => deleteDoc(id))); await load(); toast(`已删除 ${selected.value.length} 份`); selected.value = [] }
  catch (e) { toast('批量删除失败') }
}
function logout() { auth.logout(); router.push('/login') }
onMounted(load)
</script>

<style scoped>
.doc-wrap { max-width: 1000px; margin: 20px auto; padding: 0 16px; }
h3 { margin: 0 0 12px; color: #1e3a8a; }
.tip { color: #94a3b8; font-size: 12px; }
.dropzone { border: 2px dashed #cbd5e1; border-radius: 8px; padding: 22px; text-align: center; cursor: pointer; transition: all .2s; background: #f8fafc; }
.dropzone.over { border-color: #2563eb; background: #eff6ff; }
.dropzone input[type=file] { margin-top: 10px; }
.file-list { text-align: left; margin: 10px auto 0; max-width: 360px; font-size: 12px; color: #475569; max-height: 90px; overflow-y: auto; }
.file-list .sz { color: #94a3b8; }
.upload-opts { margin-top: 12px; display: flex; gap: 8px; align-items: center; }
.progress { margin-top: 12px; height: 18px; background: #e2e8f0; border-radius: 9px; overflow: hidden; position: relative; }
.progress .bar { height: 100%; background: linear-gradient(90deg, #2563eb, #06b6d4); transition: width .2s; }
.progress span { position: absolute; right: 8px; top: 0; font-size: 11px; line-height: 18px; color: #fff; }
.filters { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
.filters input { flex: 1; min-width: 140px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 13px; }
th { background: #f1f5f9; }
th.cb, td.cb { width: 36px; }
td.eq { font-size: 11px; color: #64748b; max-width: 150px; }
.st-pending { color: #94a3b8; }
.st-parsed { color: #0891b2; }
.st-vectorized { color: #16a34a; }
.empty-row { text-align: center; color: #94a3b8; padding: 24px; }
.skeleton { height: 18px; background: linear-gradient(90deg, #e2e8f0 25%, #f1f5f9 50%, #e2e8f0 75%); background-size: 200% 100%; animation: shimmer 1.4s infinite; border-radius: 4px; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
button { cursor: pointer; border: none; padding: 5px 12px; border-radius: 4px; background: #2563eb; color: #fff; }
button.secondary { background: #64748b; }
button.danger { background: #ef4444; }
button.ghost { background: transparent; color: #64748b; border: 1px solid #cbd5e1; }
button:disabled { opacity: .5; cursor: not-allowed; }
.toast { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 8px 18px; border-radius: 6px; z-index: 999; font-size: 13px; box-shadow: 0 4px 12px rgba(0,0,0,.15); }
</style>
