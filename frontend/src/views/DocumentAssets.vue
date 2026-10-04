<template>
  <div class="page">
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">🗂️ 文档资产管理</h3>
        <select class="select" v-model="docId" style="max-width:420px" @change="onDocChange">
          <option value="" disabled>选择文档…</option>
          <option v-for="d in docs" :key="d.docId" :value="d.docId">{{ d.docName }}</option>
        </select>
      </div>
      <p class="hint" style="margin-top:0">
        版本历史/回滚/分块编辑/相似查重/版本对比——后端能力此前一直没有入口，本页把它们接上。
        分块保存需要 <code>doc:manage</code> 权限；回滚后需重新解析+向量化。
      </p>

      <div class="assets-tabs" v-if="docId">
        <button class="assets-tab" :class="{ active: sub === 'versions' }" @click="loadVersions">版本历史</button>
        <button class="assets-tab" :class="{ active: sub === 'chunks' }" @click="loadChunks">分块编辑</button>
        <button class="assets-tab" :class="{ active: sub === 'similar' }" @click="sub = 'similar'">相似查重</button>
        <button class="assets-tab" :class="{ active: sub === 'diff' }" @click="sub = 'diff'">版本对比</button>
      </div>

      <!-- 版本历史 -->
      <div v-if="docId && sub === 'versions'" style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>版本</th><th>大小</th><th>操作人</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="v in versions" :key="v.version">
              <td><span class="badge badge-info">v{{ v.version }}</span></td>
              <td>{{ formatSize(v.fileSize) }}</td>
              <td>{{ v.createdBy || '—' }}</td>
              <td class="muted">{{ v.createdAt }}</td>
              <td><button class="btn btn-ghost btn-sm" @click="doRollback(v)">回滚到此版</button></td>
            </tr>
            <tr v-if="!versions.length"><td colspan="5" class="empty">无归档版本（换版上传后才有）</td></tr>
          </tbody>
        </table>
      </div>

      <!-- 分块编辑 -->
      <div v-if="docId && sub === 'chunks'" class="chunks-wrap">
        <div class="chunks-list">
          <div v-for="c in chunks" :key="c.id" class="chunk-item" :class="{ active: editChunk && editChunk.id === c.id }" @click="editChunk = { ...c }">
            <b>#{{ c.chunkIdx }}</b><small>{{ c.charCount }}字</small>
            <p>{{ c.content.slice(0, 60) }}…</p>
          </div>
          <p v-if="!chunks.length" class="empty">无分块（先向量化）</p>
        </div>
        <div class="chunk-editor" v-if="editChunk">
          <small class="muted">#{{ editChunk.chunkIdx }} · {{ editChunk.section || '无章节' }} {{ editChunk.contextLine ? `· 定位语：${editChunk.contextLine}` : '' }}</small>
          <textarea class="input" v-model="editChunk.content" rows="12"></textarea>
          <button class="btn btn-primary btn-sm" :disabled="savingChunk || !canManage" @click="saveChunk" :title="canManage ? '' : '需要 doc:manage 权限'">{{ savingChunk ? '保存并重向量中…' : '保存（自动重新向量化）' }}</button>
        </div>
      </div>

      <!-- 相似查重 -->
      <div v-if="docId && sub === 'similar'">
        <textarea class="input" v-model="similarText" rows="5" placeholder="粘贴一段文本（或新文档开头），查知识库里最相似的已存在文档（上传前去重）"></textarea>
        <button class="btn btn-primary btn-sm" style="margin:8px 0" :disabled="similarLoading || !similarText.trim()" @click="doSimilar">{{ similarLoading ? '向量化检索中…' : '查相似文档' }}</button>
        <table class="tbl" v-if="similarList.length">
          <thead><tr><th>文档</th><th>相似度</th></tr></thead>
          <tbody>
            <tr v-for="s in similarList" :key="s.docId">
              <td>{{ s.docName }}</td>
              <td><span class="badge" :class="s.score >= 0.9 ? 'badge-danger' : 'badge-info'">{{ (s.score * 100).toFixed(1) }}%</span></td>
            </tr>
          </tbody>
        </table>
        <p class="hint" v-else-if="similarDone">没有明显相似的文档。</p>
      </div>

      <!-- 版本对比 -->
      <div v-if="docId && sub === 'diff'">
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
          <select class="select" v-model.number="diffV1" style="width:auto"><option v-for="v in versions" :key="v.version" :value="v.version">v{{ v.version }}</option></select>
          <span>→</span>
          <select class="select" v-model.number="diffV2" style="width:auto"><option v-for="v in versions" :key="v.version" :value="v.version">v{{ v.version }}</option></select>
          <button class="btn btn-primary btn-sm" :disabled="!diffV1 || !diffV2 || diffLoading" @click="doDiff">{{ diffLoading ? '对比中…' : '对比' }}</button>
          <span class="hint" v-if="diffResult">v{{ diffResult.v1 }}({{ diffResult.v1Chars }}字) → v{{ diffResult.v2 }}({{ diffResult.v2Chars }}字)，变更 {{ diffResult.changedLines }} 行</span>
        </div>
        <pre class="diff-view" v-if="diffResult">{{ diffResult.diff.join('\n') }}</pre>
      </div>
    </div>
    <div class="global-toast" v-if="msg">{{ msg }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  listDocs, getDocVersions, rollbackDocVersion, getDocChunks, updateDocChunk,
  similarCheck, getDocVersionDiff,
} from '../api'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'

const auth = useAuthStore()
const canManage = computed(() => hasPerm(auth.role, 'doc:manage'))

const docs = ref([])
const docId = ref('')
const sub = ref('versions')
const msg = ref('')
let msgTimer = null
function toast(m) { msg.value = m; clearTimeout(msgTimer); msgTimer = setTimeout(() => (msg.value = ''), 2200) }

const versions = ref([])
const chunks = ref([])
const editChunk = ref(null)
const savingChunk = ref(false)
const similarText = ref('')
const similarList = ref([])
const similarLoading = ref(false)
const similarDone = ref(false)
const diffV1 = ref(null)
const diffV2 = ref(null)
const diffLoading = ref(false)
const diffResult = ref(null)

onMounted(async () => {
  try {
    const res = (await listDocs(1, 200)).data
    docs.value = (res.list || res.docs || res || []).map((d) => ({ docId: d.docId || d.id, docName: d.docName || d.name || d.filename || d.docId }))
  } catch (e) { toast('文档列表加载失败') }
})

function onDocChange() {
  versions.value = []; chunks.value = []; editChunk.value = null
  similarList.value = []; similarDone.value = false; diffResult.value = null
  diffV1.value = null; diffV2.value = null
  if (sub.value === 'versions' || sub.value === 'diff') loadVersions()
  if (sub.value === 'chunks') loadChunks()
}

async function loadVersions() {
  if (!docId.value) return
  try {
    const res = (await getDocVersions(docId.value)).data
    versions.value = res.versions || []
    if (versions.value.length && !diffV1.value) { diffV1.value = versions.value[0].version; diffV2.value = versions.value[0].version }
  } catch (e) { toast('版本历史加载失败') }
}

async function loadChunks() {
  if (!docId.value) return
  try { chunks.value = (await getDocChunks(docId.value)).data || [] } catch (e) { toast('分块加载失败') }
}

async function doRollback(v) {
  if (!window.confirm(`确认回滚到 v${v.version}？当前向量将被清除，需重新解析+向量化。`)) return
  try { await rollbackDocVersion(docId.value, v.version); toast(`已回滚到 v${v.version}，请重新解析向量化`) }
  catch (e) { toast(e?.response?.data?.message || '回滚失败') }
}

async function saveChunk() {
  if (!editChunk.value) return
  savingChunk.value = true
  try {
    const res = (await updateDocChunk(editChunk.value.id, editChunk.value.content)).data
    toast(`已保存 #${res.chunkIdx ?? editChunk.value.chunkIdx}（${res.charCount}字），整篇已重新向量化`)
  } catch (e) { toast(e?.response?.data?.message || '保存失败') }
  finally { savingChunk.value = false }
}

async function doSimilar() {
  similarLoading.value = true; similarDone.value = false; similarList.value = []
  try { similarList.value = (await similarCheck(similarText.value)).data || []; similarDone.value = true }
  catch (e) { toast(e?.response?.data?.message || '查重失败') }
  finally { similarLoading.value = false }
}

async function doDiff() {
  if (!diffV1.value || !diffV2.value) return
  diffLoading.value = true
  try { diffResult.value = (await getDocVersionDiff(docId.value, diffV1.value, diffV2.value)).data }
  catch (e) { toast(e?.response?.data?.message || '对比失败（二进制/不可解码版本？）') }
  finally { diffLoading.value = false }
}

function formatSize(n) {
  if (n == null) return '—'
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}
</script>

<style scoped>
.global-toast { position: fixed; top: 64px; right: 20px; z-index: 99; padding: 9px 16px; border-radius: 8px; background: var(--primary); color: #fff; font-size: 13px; box-shadow: 0 6px 20px rgba(0,0,0,.2); }
.assets-tabs { display: flex; gap: 8px; margin: 12px 0; }
.assets-tab { padding: 6px 14px; border: 0; border-radius: 7px; background: var(--surface-2); color: var(--text-muted); cursor: pointer; font-size: 13px; }
.assets-tab.active { background: var(--primary-soft); color: var(--primary); font-weight: 650; }
.chunks-wrap { display: grid; grid-template-columns: 280px 1fr; gap: 14px; }
.chunks-list { max-height: 460px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.chunk-item { padding: 8px 10px; border-radius: 8px; background: var(--surface-2); cursor: pointer; }
.chunk-item.active { outline: 2px solid var(--primary); }
.chunk-item p { margin: 4px 0 0; font-size: 12px; color: var(--text-muted); }
.chunk-editor { display: flex; flex-direction: column; gap: 8px; }
.diff-view { max-height: 420px; overflow: auto; padding: 10px; border-radius: 8px; background: var(--surface-2); font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; }
</style>
