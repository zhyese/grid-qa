<template>
  <section class="batch-extract">
    <h3>批量抽取三元组</h3>
    <p class="hint">一次提交，后台逐篇抽取。关闭页面不影响任务；仅增量追加，不删除已有三元组。模型调用可能产生费用。</p>
    <div class="row toolbar">
      <input class="input" v-model="keyword" placeholder="搜索文档名称" aria-label="搜索抽取文档" @keyup.enter="search" />
      <button class="btn btn-ghost" @click="search" :disabled="loadingDocs">搜索</button>
      <select class="select" v-model="modelType" aria-label="批量抽取模型" :disabled="submitting">
        <option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option>
      </select>
    </div>
    <div class="row toolbar">
      <button class="btn btn-ghost btn-sm" @click="selectPage(false)" :disabled="loadingDocs || submitting">全选当前页可抽取文档</button>
      <button class="btn btn-ghost btn-sm" @click="selectPage(true)" :disabled="loadingDocs || submitting">选择当前页未建图文档</button>
      <button class="btn btn-ghost btn-sm" @click="selected = []" :disabled="submitting">清空选择</button>
      <span class="hint">已选 {{ selected.length }}/200 份（跨页保留）</span>
      <button class="btn btn-primary" @click="submit" :disabled="!selected.length || submitting">{{ submitting ? '提交中…' : '提交批量抽取' }}</button>
    </div>
    <p v-if="message" class="message" role="status">{{ message }}</p>
    <p v-if="docsError" class="message error" role="alert">{{ docsError }}</p>
    <div class="table-scroll">
      <table class="tbl">
        <thead><tr><th>选择</th><th>文档</th><th>分块数</th><th>已有三元组</th></tr></thead>
        <tbody>
          <tr v-for="d in docs" :key="d.docId">
            <td><input type="checkbox" v-model="selected" :value="d.docId" :aria-label="`选择 ${d.docName}`" :disabled="submitting || !d.chunkCount || (selected.length >= 200 && !selected.includes(d.docId))" /></td>
            <td>{{ d.docName }} <small v-if="!d.chunkCount" class="hint">（请先解析文档）</small></td>
            <td>{{ d.chunkCount }}</td><td>{{ d.tripleCount }}</td>
          </tr>
          <tr v-if="!docs.length"><td colspan="4" class="empty">{{ loadingDocs ? '加载中…' : '没有可显示的文档' }}</td></tr>
        </tbody>
      </table>
    </div>
    <div class="row pagination">
      <button class="btn btn-ghost btn-sm" @click="docPage--; loadDocs()" :disabled="docPage <= 1 || loadingDocs">上一页</button>
      <span>第 {{ docPage }} / {{ docPages }} 页 · {{ docTotal }} 份</span>
      <button class="btn btn-ghost btn-sm" @click="docPage++; loadDocs()" :disabled="docPage >= docPages || loadingDocs">下一页</button>
    </div>

    <h3>我的抽取任务 <span class="hint">共 {{ taskTotal }} 项 · 每 4 秒更新</span></h3>
    <p class="hint">每篇独立计数，失败自动重试最多 3 次；耗尽后可手动重试。部分完成不会标成全部成功。</p>
    <p v-if="tasksError" class="message error" role="alert">{{ tasksError }}（稍后自动重连）</p>
    <div class="table-scroll">
      <table class="tbl">
        <thead><tr><th>文档</th><th>状态</th><th>结果 / 原因</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="t in tasks" :key="t.id">
            <td>{{ t.docName }}</td><td class="status-cell">{{ taskLabel(t) }}</td><td>{{ taskDetail(t) }}</td>
            <td><button v-if="t.status === 'dead'" class="btn btn-ghost btn-sm" @click="retry(t)" :disabled="submitting">重试</button></td>
          </tr>
          <tr v-if="!tasks.length"><td colspan="4" class="empty">尚无批量抽取任务</td></tr>
        </tbody>
      </table>
    </div>
    <div class="row pagination">
      <button class="btn btn-ghost btn-sm" @click="taskPage--; loadTasks()" :disabled="taskPage <= 1 || loadingTasks">上一页</button>
      <span>第 {{ taskPage }} / {{ taskPages }} 页</span>
      <button class="btn btn-ghost btn-sm" @click="taskPage++; loadTasks()" :disabled="taskPage >= taskPages || loadingTasks">下一页</button>
    </div>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getKgExtractDocuments, extractKgBatch, getKgExtractTasks, retryKgExtractTask } from '../api'
import { unwrapKg, extractionError, taskLabel, taskDetail, selectDocuments } from '../utils/kgExtraction'

const emit = defineEmits(['updated'])
const docs = ref([]), selected = ref([]), keyword = ref(''), modelType = ref('')
const docPage = ref(1), docTotal = ref(0), taskPage = ref(1), taskTotal = ref(0)
const tasks = ref([]), message = ref(''), docsError = ref(''), tasksError = ref('')
const loadingDocs = ref(false), loadingTasks = ref(false), submitting = ref(false)
const docPages = computed(() => Math.max(1, Math.ceil(docTotal.value / 20)))
const taskPages = computed(() => Math.max(1, Math.ceil(taskTotal.value / 50)))
let stopped = false, timer, digest = ''

async function loadDocs() {
  if (loadingDocs.value) return
  loadingDocs.value = true
  try {
    const data = unwrapKg(await getKgExtractDocuments({ page: docPage.value, size: 20, keyword: keyword.value }))
    docs.value = data.list; docTotal.value = data.total; docsError.value = ''
  } catch (e) { docsError.value = extractionError(e) }
  finally { loadingDocs.value = false }
}
function search() { if (!loadingDocs.value) { docPage.value = 1; loadDocs() } }
function selectPage(onlyMissing) { selected.value = selectDocuments(selected.value, docs.value, onlyMissing) }
async function loadTasks() {
  if (loadingTasks.value || stopped) return
  loadingTasks.value = true
  try {
    const data = unwrapKg(await getKgExtractTasks({ page: taskPage.value, size: 50 }))
    tasks.value = data.tasks; taskTotal.value = data.total; tasksError.value = ''
    const next = JSON.stringify(data.tasks.map(t => [t.id, t.status, t.result]))
    if (digest && next !== digest) { emit('updated'); loadDocs() }
    digest = next
  } catch (e) { tasksError.value = extractionError(e) }
  finally { loadingTasks.value = false }
}
async function submit() {
  if (submitting.value || !selected.value.length) return
  submitting.value = true
  try {
    const data = unwrapKg(await extractKgBatch(selected.value, modelType.value || null))
    message.value = `已提交或复用 ${data.tasks.length} 个任务；可关闭页面，稍后在此查看结果。`
    selected.value = []; taskPage.value = 1; await loadTasks()
  } catch (e) { message.value = extractionError(e) }
  finally { submitting.value = false }
}
async function retry(task) {
  if (submitting.value) return
  submitting.value = true
  try { unwrapKg(await retryKgExtractTask(task.id)); message.value = '任务已重新排队'; await loadTasks() }
  catch (e) { message.value = extractionError(e) }
  finally { submitting.value = false }
}
async function poll() {
  await loadTasks()
  if (!stopped) timer = setTimeout(poll, 4000)
}
onMounted(() => { loadDocs(); poll() })
onBeforeUnmount(() => { stopped = true; clearTimeout(timer) })
</script>

<style scoped>
.batch-extract { overflow: auto; padding: 4px; }
.batch-extract h3 { margin: 8px 0 12px; }
.toolbar { gap: 8px; flex-wrap: wrap; margin: 10px 0; }
.toolbar .input { flex: 1; min-width: 180px; }
.toolbar .select { width: 150px; }
.table-scroll { overflow: auto; max-height: 380px; }
.pagination { justify-content: center; gap: 12px; margin: 14px 0; }
.message { padding: 10px 12px; background: var(--primary-soft); border-radius: var(--radius-sm); }
.error { color: var(--danger, #dc2626); }
.status-cell { white-space: nowrap; }
input[type=checkbox] { width: 18px; height: 18px; }
</style>
