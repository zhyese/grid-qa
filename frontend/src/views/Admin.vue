<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="loadFeedbacks(fbFilter); tab = 'feedback'">🛡️ 反馈管理</button>
      <button class="tab" :class="{ active: tab === 'log' }" @click="tab = 'log'">📋 操作日志</button>
      <button class="tab" :class="{ active: tab === 'alert' }" @click="loadAlerts(); tab = 'alert'">🚨 告警 <span v-if="alerts.total" class="badge badge-danger">{{ alerts.total }}</span></button>
      <button class="tab" :class="{ active: tab === 'config' }" @click="tab = 'config'">⚙️ 系统配置</button>
      <button class="tab" :class="{ active: tab === 'optimizer' }" @click="loadOptimizer(); tab = 'optimizer'">📈 优化建议</button>
      <button class="tab" :class="{ active: tab === 'cost' }" @click="loadCostReport(); tab = 'cost'">💰 成本</button>
      <button class="tab" :class="{ active: tab === 'quality' }" @click="loadQuality(); tab = 'quality'">📚 知识库质量</button>
      <button class="tab" :class="{ active: tab === 'eval' }" @click="loadEval(); tab = 'eval'">📊 评测趋势</button>
      <button class="tab" :class="{ active: tab === 'abtest' }" @click="loadABTest(); tab = 'abtest'">🧪 A/B测试</button>
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
    </div><!-- /config tab -->

      <!-- 优化建议 -->
      <div class="card" v-show="tab === 'optimizer'">
        <div class="card-header">
          <h3 class="card-title">📈 反馈驱动优化建议</h3>
          <button class="btn btn-primary btn-sm" :disabled="optLoading" @click="generateOptimizer">{{ optLoading ? '分析中…' : '🔄 重新分析' }}</button>
        </div>
        <p class="hint" style="margin-top:0">基于用户反馈自动分析知识盲区、缓存策略和检索质量，生成可执行优化建议。</p>
        <div v-if="optimizer" style="margin-top:8px">
          <div class="opt-meta" style="margin-bottom:8px; font-size:12px; color:var(--text-muted)">
            分析时间：{{ optimizer.generatedAt || '未生成' }} · 总 dislike {{ optimizer.totalDislike }} · 近7天 {{ optimizer.recentDislike }}
          </div>
          <div v-if="!optimizer.suggestions?.length" class="empty">暂无优化建议（数据积累中）</div>
          <div class="opt-card" v-for="(s, i) in optimizer.suggestions" :key="i" :class="'sev-' + s.severity">
            <div class="opt-header">
              <span class="badge" :class="severityBadge(s.severity)">{{ {high:'高优',medium:'中优',low:'低优'}[s.severity] || s.severity }}</span>
              <span class="opt-type">{{ typeLabel(s.type) }}</span>
              <strong class="opt-title">{{ s.title }}</strong>
            </div>
            <div class="opt-detail">{{ s.detail }}</div>
            <div class="opt-actions" v-if="s.actions?.length">
              <div class="opt-action" v-for="(a, j) in s.actions" :key="j">{{ a }}</div>
            </div>
          </div>
        </div>
        <div v-else class="hint" style="margin-top:12px">点「重新分析」生成优化建议报告。</div>
      </div>

      <!-- 成本 -->
      <div class="card" v-show="tab === 'cost'">
        <div class="card-header"><h3 class="card-title">💰 LLM 成本报告</h3><button class="btn btn-ghost btn-sm" @click="loadCostReport">🔄 刷新</button></div>
        <div v-if="costReport">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ costReport.todayTokens?.toLocaleString() || 0 }}</div><div class="stat-lbl">今日 Token</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ costReport.monthTokens?.toLocaleString() || 0 }}</div><div class="stat-lbl">本月 Token</div></div>
            <div class="stat stat-accent"><div class="stat-val">¥{{ costReport.todayByModel?.reduce((s,m)=>s+m.cost,0).toFixed(4) || '0' }}</div><div class="stat-lbl">今日费用</div></div>
          </div>
          <div v-if="costReport.todayByModel?.length" class="src-head">今日各模型消耗</div>
          <div class="cause" v-for="m in costReport.todayByModel" :key="m.model" style="justify-content:space-between">
            <span><b>{{ m.model }}</b></span><span>{{ m.tokens?.toLocaleString() }} tokens · ¥{{ m.cost?.toFixed(4) }}</span>
          </div>
          <div v-if="costReport.topUsers?.length" class="src-head" style="margin-top:10px">本月用户排行 Top-10</div>
          <div class="cause" v-for="(u, i) in costReport.topUsers" :key="i" style="justify-content:space-between">
            <span>{{ i+1 }}. {{ u.username }}</span><span>{{ u.tokens?.toLocaleString() }} tokens</span>
          </div>
          <div class="hint" style="margin-top:8px">用户配额：{{ costReport.userQuota?.toLocaleString() }} · 租户配额：{{ costReport.tenantQuota?.toLocaleString() }}</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- 知识库质量 -->
      <div class="card" v-show="tab === 'quality'">
        <div class="card-header"><h3 class="card-title">📚 知识库质量</h3><button class="btn btn-ghost btn-sm" @click="loadQuality">🔄 刷新</button></div>
        <div v-if="quality">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ quality.overallGrade || '?' }}</div><div class="stat-lbl">综合评级</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ (quality.qualityScore * 100).toFixed(0) }}%</div><div class="stat-lbl">分块质量</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ (quality.coverageRate * 100).toFixed(0) }}%</div><div class="stat-lbl">向量化覆盖</div></div>
          </div>
          <div class="cause" style="justify-content:space-between"><span>文档数</span><span>{{ quality.docCount }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>分块数</span><span>{{ quality.chunkCount }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>重复率</span><span>{{ (quality.dupRate * 100).toFixed(1) }}%</span></div>
          <div class="cause" style="justify-content:space-between"><span>过短分块</span><span>{{ quality.tooShortChunks }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>过长分块</span><span>{{ quality.tooLongChunks }}</span></div>
          <div v-if="quality.docTypeDistribution" class="src-head" style="margin-top:8px">文档类型分布</div>
          <div class="cause" v-for="(c, t) in quality.docTypeDistribution" :key="t" style="justify-content:space-between"><span>{{ t }}</span><span>{{ c }} 份</span></div>
          <div v-if="quality.gaps?.length" class="src-head" style="margin-top:8px;color:var(--warning)">⚠ 知识盲区</div>
          <div class="cause" v-for="g in quality.gaps" :key="g.term"><span>{{ g.suggestion }}</span></div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- 评测趋势 -->
      <div class="card" v-show="tab === 'eval'">
        <div class="card-header"><h3 class="card-title">📊 检索质量评测趋势</h3><button class="btn btn-ghost btn-sm" @click="loadEval">🔄 刷新</button></div>
        <div v-if="evalTrend">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ (evalTrend.latestOverall * 100 || 0).toFixed(1) }}%</div><div class="stat-lbl">综合评分</div></div>
          </div>
          <div class="src-head">近 {{ evalTrend.days }} 天趋势</div>
          <div class="cause" v-for="t in evalTrend.trends" :key="t.date" style="flex-direction:column;align-items:flex-start">
            <div style="display:flex;justify-content:space-between;width:100%">
              <span><b>{{ t.date }}</b></span><span>{{ t.samples }} 条</span>
            </div>
            <div style="display:flex;gap:8px;font-size:12px;margin-top:2px">
              <span>综合:{{ (t.overall * 100).toFixed(0) }}%</span>
              <span>相关性:{{ (t.relevance * 100).toFixed(0) }}%</span>
              <span>忠实度:{{ (t.faithfulness * 100).toFixed(0) }}%</span>
            </div>
          </div>
          <div v-if="!evalTrend.trends?.length" class="empty">暂无评测数据（需要用户问答触发采样）</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- A/B 测试 -->
      <div class="card" v-show="tab === 'abtest'">
        <div class="card-header"><h3 class="card-title">🧪 路由 A/B 测试</h3><button class="btn btn-ghost btn-sm" @click="loadABTest">🔄 刷新</button></div>
        <div v-if="abConfig">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ abConfig.enabled ? '✅ 开' : '❌ 关' }}</div><div class="stat-lbl">路由总开关</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ abConfig.abTestRatio < 1 ? ((1 - abConfig.abTestRatio) * 100).toFixed(0) + '%' : '全量' }}</div><div class="stat-lbl">B 组流量</div></div>
          </div>
          <div class="cause" style="justify-content:space-between"><span>Sparse 最大长度</span><span>{{ abConfig.sparseMaxLen }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>Dense 最小长度</span><span>{{ abConfig.denseMinLen }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>最低置信度</span><span>{{ abConfig.minConfidence }}</span></div>
          <div class="hint" style="margin-top:8px">B 组走 hybrid 全链路，A 组走智能路由。对比两组延迟和检索质量。</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>
      <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getLogs, getAlerts, configMilvus, configModel, getMilvusConfig, getModelConfig, getProviderHealth, rebuildBm25, getFeedbacks, markFeedbackGolden, getFeedbackStats } from '../api'
import request from '../api/request'

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
const optimizer = ref(null)
const optLoading = ref(false)
async function loadOptimizer() {
  optLoading.value = true
  try {
    const r = await request.get('/system/optimizer/report')
    optimizer.value = r.data || null
  } catch (e) { /* silent */ } finally { optLoading.value = false }
}
async function generateOptimizer() {
  optLoading.value = true
  try {
    const r = await request.post('/system/optimizer/generate')
    optimizer.value = r.data || null
    toast('优化建议已生成')
  } catch (e) { toast('生成失败') } finally { optLoading.value = false }
}
function typeLabel(t) {
  return { retrieval: '检索优化', knowledge_gap: '知识盲区', cache: '缓存策略', trend: '趋势预警' }[t] || t
}
function severityBadge(s) {
  return { high: 'badge badge-danger', medium: 'badge badge-warning', low: 'badge badge-info' }[s] || 'badge badge-neutral'
}
const costReport = ref(null); const quality = ref(null); const evalTrend = ref(null); const abConfig = ref(null)
async function loadCostReport() { try { costReport.value = (await request.get('/system/cost/report', { params: { period: 'today' } })).data } catch (e) { toast('加载失败') } }
async function loadQuality() { try { quality.value = (await request.get('/system/knowledge/quality')).data } catch (e) { toast('加载失败') } }
async function loadEval() { try { evalTrend.value = (await request.get('/system/eval/trends', { params: { days: 7 } })).data } catch (e) { toast('加载失败') } }
async function loadABTest() { try { abConfig.value = (await request.get('/system/routing/config')).data } catch (e) { toast('加载失败') } }
onMounted(() => { loadLogs(); loadFeedbacks('dislike'); loadFbStats(); loadAlerts(); loadConfig() })
</script>

<style scoped>
.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .config-grid { grid-template-columns: 1fr } }
.opt-card { background: var(--surface-2); padding: 10px 12px; border-radius: var(--radius-sm); margin-bottom: 8px; border-left: 3px solid var(--border); }
.opt-card.sev-high { border-left-color: var(--danger); }
.opt-card.sev-medium { border-left-color: var(--warning); }
.opt-card.sev-low { border-left-color: var(--info); }
.opt-header { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-bottom: 4px; flex-wrap: wrap; }
.opt-type { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.opt-detail { font-size: 12px; color: var(--text); margin-bottom: 4px; line-height: 1.5; }
.opt-actions { display: flex; flex-direction: column; gap: 2px; }
.opt-action { font-size: 12px; color: var(--text-muted); padding-left: 8px; border-left: 2px solid var(--border); margin: 1px 0; }
</style>
