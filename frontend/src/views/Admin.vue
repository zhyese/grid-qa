<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="loadFeedbacks(fbFilter); tab = 'feedback'">🛡️ 反馈管理</button>
      <button class="tab" :class="{ active: tab === 'log' }" @click="tab = 'log'">📋 操作日志</button>
      <button class="tab" :class="{ active: tab === 'config' }" @click="tab = 'config'">⚙️ 系统配置</button>
    </div>

    <!-- 反馈管理 -->
    <div class="card" v-show="tab === 'feedback'">
      <div class="card-header">
        <h3 class="card-title">坏 case 看板 <span class="badge badge-neutral">{{ feedbacks.total }}</span></h3>
        <div class="row">
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === 'dislike' }" @click="loadFeedbacks('dislike')">只看👎</button>
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === 'like' }" @click="loadFeedbacks('like')">只看👍</button>
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === '' }" @click="loadFeedbacks('')">全部</button>
        </div>
      </div>
      <p class="hint" style="margin-top:0">dislike 自动异步跑 LLM-judge 打质量分；确认坏 case 后「标为 golden」→ 自动写入 golden 集 → CI 门禁永久覆盖。</p>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>问题</th><th>反馈</th><th>judge幻觉</th><th>理由</th><th>用户</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="f in feedbacks.list" :key="f.id">
              <td style="max-width:260px">{{ f.query }}</td>
              <td>{{ f.feedback === 'like' ? '👍' : '👎' }}</td>
              <td><span :class="judgeBadge(f.judgeHalluc)">{{ f.judgeHalluc != null ? (f.judgeHalluc * 100).toFixed(0) + '%' : '待评' }}</span></td>
              <td class="muted" style="max-width:200px">{{ f.reason || '—' }}</td>
              <td>{{ f.username || '—' }}</td>
              <td class="muted">{{ f.createdAt }}</td>
              <td><button class="btn btn-link btn-sm" @click="markGolden(f)">标为 golden</button></td>
            </tr>
            <tr v-if="!feedbacks.list.length"><td colspan="7" class="empty">暂无反馈</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 操作日志 -->
    <div class="card" v-show="tab === 'log'">
      <div class="card-header"><h3 class="card-title">操作日志 <span class="badge badge-neutral">{{ logs.total }}</span></h3></div>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>用户</th><th>类型</th><th>内容</th><th>时间</th></tr></thead>
          <tbody><tr v-for="l in logs.list" :key="l.id"><td>{{ l.operateUser }}</td><td><span class="badge badge-neutral">{{ l.operateType }}</span></td><td>{{ l.content }}</td><td class="muted">{{ l.operateTime }}</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- 系统配置 -->
    <div v-show="tab === 'config'">
      <div class="config-grid">
        <div class="card">
          <div class="card-header"><h3 class="card-title">Milvus 索引配置</h3></div>
          <div class="field"><label class="field-label">indexType</label><input class="input" v-model="milvus.indexType" /></div>
          <div class="field"><label class="field-label">nprobe</label><input class="input" v-model="milvus.nprobe" /></div>
          <div class="field"><label class="field-label">nlist</label><input class="input" v-model="milvus.nlist" /></div>
          <button class="btn btn-primary" @click="saveMilvus">保存</button>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">模型参数配置</h3></div>
          <div class="field"><label class="field-label">modelType</label><input class="input" v-model="model.modelType" /></div>
          <div class="field"><label class="field-label">temperature</label><input class="input" v-model="model.temperature" /></div>
          <button class="btn btn-primary" @click="saveModel">保存</button>
        </div>
      </div>
    </div>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getLogs, configMilvus, configModel, getFeedbacks, markFeedbackGolden } from '../api'

const tab = ref('feedback')
const logs = ref({ total: 0, list: [] })
const feedbacks = ref({ total: 0, list: [] })
const fbFilter = ref('dislike')
const milvus = reactive({ indexType: 'IVF_FLAT', nprobe: 16, nlist: 1024 })
const model = reactive({ modelType: 'deepseek', temperature: 0.2 })
const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1600) }
function judgeBadge(h) {
  if (h == null) return 'badge badge-neutral'
  if (h >= 0.5) return 'badge badge-danger'
  if (h >= 0.2) return 'badge badge-warning'
  return 'badge badge-success'
}

async function loadLogs() { logs.value = (await getLogs({ page: 1, size: 20 })).data }
async function loadFeedbacks(fb = 'dislike') { fbFilter.value = fb; try { feedbacks.value = (await getFeedbacks({ feedback: fb, page: 1, size: 30 })).data } catch (e) { toast('加载反馈失败') } }
async function markGolden(f) { try { const r = (await markFeedbackGolden(f.id)).data; toast(r.added ? `已加入 golden 集（共 ${r.total} 条）` : `未加入：${r.reason || '已存在'}`) } catch (e) { toast('操作失败') } }
async function saveMilvus() { await configMilvus(milvus.indexType, { nprobe: Number(milvus.nprobe), nlist: Number(milvus.nlist) }); toast('已保存') }
async function saveModel() { await configModel(model.modelType, { temperature: Number(model.temperature) }); toast('已保存') }
onMounted(() => { loadLogs(); loadFeedbacks('dislike') })
</script>

<style scoped>
.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .config-grid { grid-template-columns: 1fr } }
</style>
