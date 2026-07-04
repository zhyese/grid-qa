<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="loadFeedbacks(fbFilter); tab = 'feedback'">🛡️ 反馈管理</button>
      <button class="tab" :class="{ active: tab === 'log' }" @click="tab = 'log'">📋 操作日志</button>
      <button class="tab" :class="{ active: tab === 'alert' }" @click="loadAlerts(); tab = 'alert'">🚨 告警 <span v-if="alerts.total" class="badge badge-danger">{{ alerts.total }}</span></button>
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
      <p class="hint" style="margin-top:0">dislike 自动异步跑 LLM-judge 打质量分 + 检索质量评估；确认坏 case 后「标为 golden」→ 自动写入 golden 集 → CI 门禁永久覆盖。</p>
      <!-- 检索→回答一致性矩阵 -->
      <div v-if="fbStats?.consistencyMatrix" style="margin-bottom:10px; padding:10px; background:var(--surface-2); border-radius:8px; font-size:12px">
        <strong style="font-size:13px">📊 检索→回答 一致性矩阵</strong>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:6px">
          <div class="badge badge-success" style="justify-content:center">检索好 + 回答好 ✅ {{ fbStats.consistencyMatrix.retrieval_good_answer_good }}</div>
          <div class="badge badge-warning" style="justify-content:center">检索好 + 回答差 🔧 {{ fbStats.consistencyMatrix.retrieval_good_answer_bad }}</div>
          <div class="badge badge-danger" style="justify-content:center">检索差 + 回答好 ⚠️ 编造 {{ fbStats.consistencyMatrix.retrieval_poor_answer_good }}</div>
          <div class="badge badge-danger" style="justify-content:center">检索差 + 回答差 ❌ 根因 {{ fbStats.consistencyMatrix.retrieval_poor_answer_bad }}</div>
        </div>
        <div v-if="fbStats.consistencyMatrix.retrieval_poor_answer_good_queries?.length" style="margin-top:6px; color:var(--danger)">
          ⚠️ 疑似 LLM 编造 case：<span v-for="q in fbStats.consistencyMatrix.retrieval_poor_answer_good_queries" :key="q" class="chip" style="color:var(--danger)">{{ q }}</span>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>问题</th><th>反馈</th><th>检索质量</th><th>judge幻觉</th><th>理由</th><th>用户</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="f in feedbacks.list" :key="f.id">
              <td style="max-width:220px">{{ f.query }}</td>
              <td>{{ f.feedback === 'like' ? '👍' : '👎' }}</td>
              <td><span :class="retrievalBadge(f.retrievalQuality)">{{ retrievalLabel(f.retrievalQuality) }}</span></td>
              <td><span :class="judgeBadge(f.judgeHalluc)">{{ f.judgeHalluc != null ? (f.judgeHalluc * 100).toFixed(0) + '%' : '待评' }}</span></td>
              <td class="muted" style="max-width:160px">{{ f.reason || '—' }}</td>
              <td>{{ f.username || '—' }}</td>
              <td class="muted">{{ f.createdAt }}</td>
              <td><button class="btn btn-link btn-sm" @click="markGolden(f)">标为 golden</button></td>
            </tr>
            <tr v-if="!feedbacks.list.length"><td colspan="8" class="empty">暂无反馈</td></tr>
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

    <!-- 告警（Grafana alerting → webhook 落库） -->
    <div class="card" v-show="tab === 'alert'">
      <div class="card-header">
        <h3 class="card-title">🚨 告警 <span class="badge badge-neutral">{{ alerts.total }}</span></h3>
        <button class="btn btn-ghost btn-sm" @click="loadAlerts">🔄 刷新</button>
      </div>
      <p class="hint" style="margin-top:0">Grafana 告警规则（组件下线/降级激增/幻觉率/安全命中）触发后经 webhook 回调落库，在此实时可见。规则在 Grafana「Alerting」页可查可改。</p>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>级别</th><th>告警</th><th>来源</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="a in alerts.list" :key="a.id">
              <td><span class="badge" :class="sevBadge(a.content)">{{ sevOf(a.content) }}</span></td>
              <td>{{ a.content.replace(/^\[(info|warning|critical)\]\s*/, '') }}</td>
              <td class="muted">{{ a.operateUser }}</td>
              <td class="muted">{{ a.operateTime }}</td>
            </tr>
            <tr v-if="!alerts.list.length"><td colspan="4" class="empty">暂无告警（系统正常）</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 系统配置 -->
    <div v-show="tab === 'config'">
      <!-- provider 连通性 -->
      <div class="card">
        <div class="card-header">
          <h3 class="card-title">Provider 连通性</h3>
          <button class="btn btn-primary btn-sm" :disabled="healthLoading" @click="loadHealth">{{ healthLoading ? '探测中...' : '🧪 测试连通' }}</button>
        </div>
        <p class="hint" style="margin-top:0">主动 ping LLM/Embedding provider，抓欠费/配额/key 失效/网络问题（消耗少量 token）。</p>
        <div class="config-grid" v-if="health">
          <div class="stat stat-accent">
            <div class="stat-val" :style="{ color: health.llm.status === 'ok' ? 'var(--success)' : 'var(--danger)' }">{{ health.llm.status === 'ok' ? '正常' : '异常' }}</div>
            <div class="stat-lbl">LLM：{{ health.llm.provider || '—' }}<br><span class="muted" style="font-size:11px">{{ health.llm.detail || health.llm.error || '' }}</span></div>
          </div>
          <div class="stat stat-accent">
            <div class="stat-val" :style="{ color: health.embedding.status === 'ok' ? 'var(--success)' : 'var(--danger)' }">{{ health.embedding.status === 'ok' ? '正常' : '异常' }}</div>
            <div class="stat-lbl">Embedding：{{ health.embedding.provider || '—' }}<br><span class="muted" style="font-size:11px">{{ health.embedding.detail || health.embedding.error || '' }}</span></div>
          </div>
        </div>
        <div v-else class="hint">点「测试连通」探测当前 provider（结果不会自动刷新）。</div>
      </div>

      <div class="config-grid">
        <div class="card">
          <div class="card-header"><h3 class="card-title">Milvus 索引配置</h3><span v-if="configLoaded" class="badge badge-success">已读取线上值</span></div>
          <div class="field"><label class="field-label">indexType</label><input class="input" v-model="milvus.indexType" /></div>
          <div class="field"><label class="field-label">M（HNSW 建索引参数）</label><input class="input" v-model="milvus.M" /></div>
          <div class="field"><label class="field-label">efConstruction（建索引参数）</label><input class="input" v-model="milvus.efConstruction" /></div>
          <div class="field"><label class="field-label">ef（查询参数 · 运行时即时生效）</label><input class="input" v-model="milvus.ef" /><span class="hint">↑ef 召回↑延迟↑，可实时调</span></div>
          <button class="btn btn-primary" @click="saveMilvus">保存</button>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">模型参数配置</h3><span v-if="configLoaded" class="badge badge-success">已读取线上值</span></div>
          <div class="field"><label class="field-label">modelType</label><input class="input" v-model="model.modelType" /></div>
          <div class="field"><label class="field-label">temperature（主答案 · 运行时即时生效）</label><input class="input" v-model="model.temperature" /></div>
          <div class="field"><label class="field-label">max_tokens</label><input class="input" v-model="model.max_tokens" /></div>
          <button class="btn btn-primary" @click="saveModel">保存</button>
        </div>
      </div>

      <!-- BM25 重建 -->
      <div class="card">
        <div class="card-header"><h3 class="card-title">BM25 索引</h3><button class="btn btn-ghost btn-sm" :disabled="bm25Loading" @click="handleRebuildBm25">{{ bm25Loading ? '重建中...' : '🔄 全量重建' }}</button></div>
        <p class="hint" style="margin-top:0">新文档默认增量进内存；进程重启/异常后点此兜底全量重建。</p>
      </div>
    </div>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getLogs, getAlerts, configMilvus, configModel, getMilvusConfig, getModelConfig, getProviderHealth, rebuildBm25, getFeedbacks, markFeedbackGolden, getFeedbackStats } from '../api'

const tab = ref('feedback')
const logs = ref({ total: 0, list: [] })
const alerts = ref({ total: 0, list: [] })
const feedbacks = ref({ total: 0, list: [] })
const fbStats = ref(null)
const fbFilter = ref('dislike')
const milvus = reactive({ indexType: 'HNSW', M: 16, efConstruction: 200, ef: 64 })
const model = reactive({ modelType: 'deepseek', temperature: 0.2, max_tokens: 2048 })
const health = ref(null)
const healthLoading = ref(false)
const bm25Loading = ref(false)
const configLoaded = ref(false)
const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1600) }
function judgeBadge(h) {
  if (h == null) return 'badge badge-neutral'
  if (h >= 0.5) return 'badge badge-danger'
  if (h >= 0.2) return 'badge badge-warning'
  return 'badge badge-success'
}
function retrievalLabel(q) {
  if (q === 'good') return '✅ 好'
  if (q === 'partial') return '⚠️ 部分'
  if (q === 'poor') return '❌ 差'
  return '待评'
}
function retrievalBadge(q) {
  if (q === 'good') return 'badge badge-success'
  if (q === 'partial') return 'badge badge-warning'
  if (q === 'poor') return 'badge badge-danger'
  return 'badge badge-neutral'
}

async function loadLogs() { logs.value = (await getLogs({ page: 1, size: 20 })).data }
async function loadAlerts() { try { alerts.value = (await getAlerts({ page: 1, size: 30 })).data } catch (e) { toast('加载告警失败') } }
const sevOf = (c = '') => { const m = c.match(/^\[(info|warning|critical)\]/i); return m ? m[1].toLowerCase() : 'info' }
const sevBadge = (c = '') => ({ critical: 'badge badge-danger', warning: 'badge badge-warning', info: 'badge badge-info' }[sevOf(c)] || 'badge badge-neutral')
async function loadFeedbacks(fb = 'dislike') { fbFilter.value = fb; try { feedbacks.value = (await getFeedbacks({ feedback: fb, page: 1, size: 30 })).data } catch (e) { toast('加载反馈失败') } }
async function loadFbStats() { try { fbStats.value = (await getFeedbackStats()).data } catch (e) { /* silent */ } }
async function markGolden(f) { try { const r = (await markFeedbackGolden(f.id)).data; toast(r.added ? `已加入 golden 集（共 ${r.total} 条）` : `未加入：${r.reason || '已存在'}`) } catch (e) { toast('操作失败') } }
async function saveMilvus() { await configMilvus(milvus.indexType, { M: Number(milvus.M), efConstruction: Number(milvus.efConstruction), ef: Number(milvus.ef) }); toast('Milvus 已保存（ef 即时生效）') }
async function saveModel() { await configModel(model.modelType, { temperature: Number(model.temperature), max_tokens: Number(model.max_tokens) }); toast('模型参数已保存（temperature 即时生效）') }
async function loadConfig() {
  try {
    const mv = (await getMilvusConfig()).data || {}
    const md = (await getModelConfig()).data || {}
    const mp = mv.param || {}
    milvus.indexType = mv.indexType || 'HNSW'
    milvus.M = mp.M ?? 16
    milvus.efConstruction = mp.efConstruction ?? 200
    milvus.ef = mp.ef ?? 64
    const pp = md.param || {}
    model.modelType = md.modelType || 'deepseek'
    model.temperature = pp.temperature ?? 0.2
    model.max_tokens = pp.max_tokens ?? 2048
    configLoaded.value = true
  } catch (e) { toast('读取线上配置失败') }
}
async function loadHealth() {
  healthLoading.value = true
  try { health.value = (await getProviderHealth()).data } catch (e) { toast('探测失败') } finally { healthLoading.value = false }
}
async function handleRebuildBm25() {
  bm25Loading.value = true
  try { const r = (await rebuildBm25()).data; toast(`BM25 重建完成（${r.chunks} 个分块）`) } catch (e) { toast('重建失败') } finally { bm25Loading.value = false }
}
onMounted(() => { loadLogs(); loadFeedbacks('dislike'); loadFbStats(); loadAlerts(); loadConfig() })
</script>

<style scoped>
.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .config-grid { grid-template-columns: 1fr } }
</style>
