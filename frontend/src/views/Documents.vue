<template>
  <div class="doc-page">
    <div class="doc-grid">
      <!-- 上传卡 -->
      <div class="card" v-if="can('doc:upload')">
        <div class="card-header"><h3 class="card-title">📤 上传文档</h3><span class="hint">PDF/Word/Excel/TXT/图片 · 批量≤5 · 单份≤100MB</span></div>
        <div class="dropzone" :class="{ over: dragOver }" @dragover.prevent="dragOver = true" @dragleave.prevent="dragOver = false" @drop.prevent="onDrop" @click="$refs.fileInput.click()">
          <input type="file" multiple ref="fileInput" @change="onFile" style="display:none" />
          <div v-if="!files.length" class="dz-empty">
            <div class="dz-icon">📂</div>
            <div>拖拽文件到此处，或<span class="dz-link">点击选择</span></div>
          </div>
          <ul v-else class="file-list">
            <li v-for="(f, i) in files" :key="i"><span class="ficon">📄</span>{{ f.name }} <span class="hint">({{ fmtSize(f.size) }})</span></li>
          </ul>
        </div>
        <div class="row" style="margin-top: 14px">
          <select class="select" v-model="docType" style="width:auto">
            <option>运维手册</option><option>故障案例</option><option>检修规程</option><option>其他</option>
          </select>
          <input class="input" v-model="dept" placeholder="部门(调度/检修,空=公开)" style="width:170px" title="文档级 ACL：限定某部门可见，空=全员公开" />
          <input class="input" v-model="allowedRoles" placeholder="授权角色(逗号分隔,空=全员)" style="width:170px" title="限定可读角色，空=部门内全员" />
          <button class="btn btn-primary" @click="upload" :disabled="!files.length || uploading">{{ uploading ? '上传中...' : '上传' }}</button>
          <button class="btn btn-ghost" v-if="files.length" @click="files = []">清空</button>
        </div>
        <div class="progress" v-if="uploading"><div class="progress-bar" :style="{ width: progress + '%' }"></div><span>{{ progress }}%</span></div>
      </div>

      <!-- 统计概览卡 -->
      <div class="card">
        <div class="card-header"><h3 class="card-title">📊 知识库概览</h3></div>
        <div class="mini-stats">
          <div class="mini-stat"><div class="mini-val">{{ stats?.docTotal ?? '-' }}</div><div class="mini-lbl">文档</div></div>
          <div class="mini-stat"><div class="mini-val">{{ stats?.chunkTotal ?? '-' }}</div><div class="mini-lbl">分块</div></div>
          <div class="mini-stat"><div class="mini-val">{{ stats?.vectorTotal ?? '-' }}</div><div class="mini-lbl">向量</div></div>
          <div class="mini-stat"><div class="mini-val">{{ stats?.byStatus?.vectorized ?? 0 }}</div><div class="mini-lbl">已向量化</div></div>
        </div>
        <p class="hint" style="margin: 12px 0 0">上传后需依次执行「解析」「向量化」才能被检索；同名文档换版会自动归档可回滚。</p>
      </div>
    </div>

    <!-- 文档列表 -->
    <div class="card doc-list">
      <div class="card-header">
        <h3 class="card-title">📚 文档列表 <span class="badge badge-neutral">{{ filtered.length }}</span></h3>
        <div class="row">
          <input class="input" v-model="filterKw" placeholder="🔍 按文件名筛选" style="width:200px" />
          <select class="select" v-model="filterType" style="width:auto"><option value="">全部类型</option><option v-for="t in types" :key="t">{{ t }}</option></select>
          <select class="select" v-model="filterStatus" style="width:auto"><option value="">全部状态</option><option value="pending">待解析</option><option value="parsed">已解析</option><option value="vectorized">已向量化</option></select>
          <button class="btn btn-ghost btn-sm" @click="batchParse" :disabled="!selected.length">批量解析({{ selected.length }})</button>
          <button class="btn btn-ghost btn-sm" @click="batchVectorize" :disabled="!selected.length">批量向量化({{ selected.length }})</button>
          <button v-if="can('doc:delete')" class="btn btn-danger btn-sm" @click="batchDelete" :disabled="!selected.length">批量删除</button>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th style="width:36px"><input type="checkbox" :checked="allChecked" @change="toggleAll($event.target.checked)" /></th>
            <th>文件名</th><th>类型</th><th>状态</th><th>分块</th><th>关联设备</th><th style="width:200px">操作</th>
          </tr></thead>
          <tbody v-if="loading"><tr v-for="i in 5" :key="i"><td colspan="7"><div class="skeleton"></div></td></tr></tbody>
          <tbody v-else>
            <tr v-for="d in filtered" :key="d.docId">
              <td><input type="checkbox" :value="d.docId" v-model="selected" /></td>
              <td><a class="doc-name" @click="previewDoc(d)" :title="'预览 ' + d.docName">{{ d.docName }}</a></td>
              <td><span class="badge badge-neutral">{{ d.docType }}</span></td>
              <td><span class="badge" :class="statusBadge(d.status)">{{ statusMap[d.status] || d.status }}</span></td>
              <td>{{ d.chunkCount }}</td>
              <td class="eq-cell">{{ (d.equipmentTags || '').split(',').filter(Boolean).slice(0, 3).join('、') || '—' }}</td>
              <td>
                <button class="btn btn-ghost btn-sm" @click="parseDoc(d.docId)" :disabled="busy[d.docId]">解析</button>
                <button class="btn btn-ghost btn-sm" @click="vectorizeDoc(d.docId)" :disabled="busy[d.docId]">向量化</button>
                <button v-if="can('doc:delete')" class="btn btn-danger btn-sm" @click="removeDoc(d.docId)" :disabled="busy[d.docId]">删除</button>
              </td>
            </tr>
            <tr v-if="!filtered.length"><td colspan="7" class="empty">无匹配文档</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 预览弹窗 -->
    <div class="modal-bg" v-if="preview.show" @click.self="closePreview">
      <div class="modal">
        <div class="modal-head"><span>📄 文档预览</span><button class="icon-btn" @click="closePreview">✕</button></div>
        <iframe v-if="preview.type === 'pdf'" :src="preview.url" style="flex:1;border:0"></iframe>
        <img v-else-if="preview.type === 'img'" :src="preview.url" style="flex:1;object-fit:contain;max-height:100%" />
        <pre v-else class="preview-text">{{ preview.text }}</pre>
      </div>
    </div>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { listDocs, uploadDocs, parseDocs, vectorize, vectorizeBatch, deleteDoc, getStats, getDocPerms, updateDocPerms } from '../api'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'

const auth = useAuthStore()
const can = (p) => hasPerm(auth.role, p)   // RBAC：操作级隐藏（上传/删除），无权限不渲染
const docs = ref([])
const stats = ref(null)
const files = ref([])
const docType = ref('运维手册')
const dept = ref('')          // RBAC 文档部门（上传时定）
const allowedRoles = ref('')  // RBAC 授权角色（逗号分隔，空=部门内全员）
const uploading = ref(false)
const progress = ref(0)
const dragOver = ref(false)
const busy = reactive({})
const selected = ref([])
const filterKw = ref('')
const filterType = ref('')
const filterStatus = ref('')
const loading = ref(false)
const statusMap = { pending: '待解析', parsed: '已解析', vectorized: '已向量化' }
const preview = reactive({ show: false, type: '', url: '', text: '' })
const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1500) }
function statusBadge(s) { return { pending: 'badge-neutral', parsed: 'badge-info', vectorized: 'badge-success' }[s] || 'badge-neutral' }

const types = computed(() => [...new Set(docs.value.map((d) => d.docType))])
const filtered = computed(() => docs.value.filter((d) =>
  (!filterKw.value || (d.docName || '').includes(filterKw.value)) &&
  (!filterType.value || d.docType === filterType.value) &&
  (!filterStatus.value || d.status === filterStatus.value)))
const allChecked = computed(() => filtered.value.length > 0 && selected.value.length === filtered.value.length)

async function load() {
  loading.value = true
  try { const [d, s] = await Promise.all([listDocs(), getStats()]); docs.value = d.data.list || []; stats.value = s.data } finally { loading.value = false }
}
function fmtSize(b) { if (!b) return '0B'; if (b < 1024) return b + 'B'; if (b < 1048576) return (b / 1024).toFixed(1) + 'KB'; return (b / 1048576).toFixed(1) + 'MB' }
function onFile(e) { files.value = [...files.value, ...Array.from(e.target.files)]; e.target.value = '' }
function onDrop(e) { dragOver.value = false; files.value = [...files.value, ...Array.from(e.dataTransfer.files)] }
async function upload() {
  if (!files.value.length) return
  const form = new FormData(); files.value.forEach((f) => form.append('files', f)); form.append('docType', docType.value); form.append('dept', dept.value); form.append('allowedRoles', allowedRoles.value)
  uploading.value = true; progress.value = 0
  try { await uploadDocs(form, (e) => { if (e.total) progress.value = Math.round((e.loaded / e.total) * 100) }); await load(); toast('上传成功'); files.value = [] } catch (e) { toast('上传失败') }
  uploading.value = false
}
function toggleAll(c) { selected.value = c ? filtered.value.map((d) => d.docId) : [] }
async function parseDoc(id) { busy[id] = true; try { await parseDocs([id]); await load(); toast('解析完成') } finally { busy[id] = false } }
async function vectorizeDoc(id) { busy[id] = true; try { await vectorize(id); await load(); toast('向量化完成') } finally { busy[id] = false } }
async function removeDoc(id) { if (!confirm('确认删除该文档（含向量/图谱）？')) return; busy[id] = true; try { await deleteDoc(id); await load(); toast('已删除') } finally { busy[id] = false } }
async function batchParse() { if (!selected.value.length) return; try { await parseDocs(selected.value); await load(); toast(`已提交解析 ${selected.value.length} 份`); selected.value = [] } catch (e) { toast('批量解析失败') } }
async function batchVectorize() {
  if (!selected.value.length) return
  try {
    const resp = await vectorizeBatch(selected.value)
    const ok = resp.data?.successList?.length || 0
    const fail = resp.data?.failList?.length || 0
    await load()
    toast(fail ? `向量化完成 ${ok} 份，失败 ${fail} 份（未解析？）` : `已向量化 ${ok} 份`)
    selected.value = []
  } catch (e) { toast('批量向量化失败') }
}
async function batchDelete() { if (!selected.value.length || !confirm(`删除 ${selected.value.length} 份？`)) return; try { await Promise.all(selected.value.map((id) => deleteDoc(id))); await load(); toast(`已删除 ${selected.value.length} 份`); selected.value = [] } catch (e) { toast('批量删除失败') } }
async function previewDoc(d) {
  const ext = (d.docName.split('.').pop() || '').toLowerCase()
  try {
    const resp = await fetch(`/api/document/preview/${d.docId}`, { headers: { Authorization: `Bearer ${auth.token}` } })
    if (!resp.ok) { toast('该格式不支持预览'); return }
    const blob = await resp.blob()
    if (ext === 'pdf') { preview.type = 'pdf'; preview.url = URL.createObjectURL(blob) }
    else if (['png', 'jpg', 'jpeg'].includes(ext)) { preview.type = 'img'; preview.url = URL.createObjectURL(blob) }
    else { preview.type = 'text'; preview.text = await blob.text() }
    preview.show = true
  } catch (e) { toast('预览失败') }
}
function closePreview() { if (preview.url) URL.revokeObjectURL(preview.url); preview.show = false; preview.url = ''; preview.text = '' }
onMounted(load)
</script>

<style scoped>
.doc-page { display: flex; flex-direction: column; height: calc(100vh - var(--topbar-h) - 8px); gap: 14px; }
.doc-page .doc-grid { flex-shrink: 0; margin-bottom: 0; }
.doc-page .doc-list { flex: 1; min-height: 0; margin-bottom: 0; display: flex; flex-direction: column; overflow: hidden; }
.doc-page .tbl-wrap { flex: 1; overflow: auto; }
.doc-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; margin-bottom: 16px; }
.dropzone { border: 2px dashed var(--border); border-radius: var(--radius); padding: 28px; text-align: center; cursor: pointer; transition: all .15s; background: var(--surface-2); }
.dropzone.over { border-color: var(--primary); background: var(--primary-soft); }
.dz-empty { color: var(--text-muted); }
.dz-icon { font-size: 32px; margin-bottom: 6px; }
.dz-link { color: var(--primary); }
.file-list { list-style: none; padding: 0; margin: 0; text-align: left; max-width: 360px; margin-inline: auto; max-height: 110px; overflow-y: auto; font-size: 12px; }
.file-list li { padding: 4px 0; color: var(--text-muted); }
.ficon { margin-right: 6px; }
.progress { margin-top: 14px; height: 18px; background: var(--surface-3); border-radius: 9px; overflow: hidden; position: relative; }
.progress-bar { height: 100%; background: linear-gradient(90deg, var(--primary), var(--accent)); transition: width .2s; }
.progress span { position: absolute; right: 8px; top: 0; font-size: 11px; line-height: 18px; color: #fff; }
.mini-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.mini-stat { text-align: center; background: var(--surface-2); border-radius: var(--radius-sm); padding: 12px 6px; }
.mini-val { font-size: 22px; font-weight: 700; color: var(--primary); }
.mini-lbl { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.doc-name { color: var(--primary); cursor: pointer; font-weight: 500; }
.doc-name:hover { text-decoration: underline; }
.eq-cell { font-size: 11px; color: var(--text-muted); max-width: 160px; }
.skeleton { height: 16px; background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 50%, var(--surface-2) 75%); background-size: 200% 100%; animation: shimmer 1.4s infinite; border-radius: 4px; }
@keyframes shimmer { 0% { background-position: 200% 0 } 100% { background-position: -200% 0 } }
.preview-text { flex: 1; overflow: auto; margin: 0; padding: 18px; white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.7; color: var(--text-muted); }
@media (max-width: 900px) { .doc-grid { grid-template-columns: 1fr } }
</style>
