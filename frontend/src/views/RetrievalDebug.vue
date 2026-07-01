<template>
  <div style="padding: 20px;">
    <!-- 查询输入 -->
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">🔍 检索调试 <span class="card-desc">· 全链路 trace + 每条命中的分数归因（仅管理员）</span></h3>
      </div>
      <div class="field">
        <label class="field-label">查询语句（Ctrl+Enter 提交）</label>
        <textarea class="input" v-model="query" rows="2" placeholder="如：变压器绕组温度过高怎么处理" @keydown.ctrl.enter="run"></textarea>
      </div>
      <div class="row" style="align-items: flex-end">
        <div class="field" style="margin-bottom:0">
          <label class="field-label">topK</label>
          <input class="input" type="number" v-model.number="topK" min="1" max="50" style="width:90px" />
        </div>
        <div class="field grow" style="margin-bottom:0">
          <label class="field-label">文档类型（可选）</label>
          <input class="input" v-model="docType" placeholder="运维手册/故障案例/检修规程/其他" />
        </div>
        <div class="field grow" style="margin-bottom:0">
          <label class="field-label">设备标签（可选）</label>
          <input class="input" v-model="equipment" placeholder="如：主变压器" />
        </div>
        <button class="btn btn-primary" :disabled="loading || !query.trim()" @click="run">
          {{ loading ? '调试中...' : '调试检索' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="card" style="border-color: var(--danger); color: var(--danger); font-size: 13px;">{{ error }}</div>

    <template v-if="trace">
      <!-- 配置快照 -->
      <div class="card">
        <h3 class="section-title">运行配置</h3>
        <div class="row">
          <span v-for="f in flags" :key="f.label" class="badge" :class="f.on ? 'badge-success' : 'badge-neutral'">{{ f.label }} {{ f.on ? 'ON' : 'OFF' }}</span>
          <span class="badge badge-info">emb: {{ trace.config.embProvider }}</span>
          <span class="badge badge-info">rerank: {{ trace.config.rerankModel }}</span>
          <span class="badge badge-primary">topK {{ trace.config.topK }} · 候选 {{ trace.config.candidate }}</span>
        </div>
      </div>

      <!-- 步骤 trace -->
      <div class="card">
        <h3 class="section-title">检索链路（{{ trace.steps.length }} 步 · 耗时 {{ trace.result.latencyMs }}ms）</h3>
        <div class="steps">
          <div v-for="(s, i) in trace.steps" :key="i" class="step">
            <div class="step-dot">{{ i + 1 }}</div>
            <div class="step-body">
              <div class="step-head">
                <span class="step-name">{{ stepName(s.step) }}</span>
                <span class="step-meta">{{ stepMeta(s) }}</span>
              </div>
              <div v-if="s.step === 'query_rewrite' && s.changed" class="step-detail">「{{ s.input }}」 → 「{{ s.output }}」</div>
              <div v-else-if="s.step === 'multi_query' && s.subQueries && s.subQueries.length" class="step-detail">
                子查询：<span v-for="(q, idx) in s.subQueries" :key="idx" class="chip">{{ q }}</span>
              </div>
              <div v-else-if="s.step === 'retrieve'" class="step-detail">
                <span v-for="(pq, idx) in s.perQuery" :key="idx" class="chip">query{{ idx + 1 }}: dense {{ pq.denseHits }} · bm25 {{ pq.bm25Hits }}{{ pq.hyde ? ' · HyDE' : '' }}</span>
              </div>
              <div v-else-if="s.step === 'rerank' && s.error" class="step-detail" style="color: var(--warning)">降级：{{ s.error }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 最终命中 + 分数归因 -->
      <div class="card">
        <h3 class="section-title">最终命中（{{ trace.result.finalHits }} 条，按 rerank/rrf 排序）</h3>
        <div style="overflow-x: auto">
          <table class="tbl">
            <thead>
              <tr>
                <th>#</th><th>文档</th><th>来源路径</th><th>片段预览</th>
                <th>RRF</th><th>Rerank</th><th style="min-width:110px">最终分</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(h, i) in trace.result.hits" :key="i">
                <td>{{ i + 1 }}</td>
                <td style="max-width:200px">{{ h.docName }}<div class="muted" style="font-size:11px">chunk {{ h.chunkIdx }}</div></td>
                <td>
                  <span v-for="src in h.sources" :key="src" class="badge" :class="srcBadge(src)" style="margin-right:3px">{{ srcLabel(src) }}</span>
                </td>
                <td class="muted" style="max-width:340px; font-size:12px">{{ h.text }}</td>
                <td><code>{{ fmt(h.scores.rrf) }}</code></td>
                <td><code>{{ h.scores.rerank != null ? fmt(h.scores.rerank) : '—' }}</code></td>
                <td>
                  <div class="score-bar">
                    <div class="score-fill" :style="{ width: barWidth(h.scores.final) }"></div>
                    <span>{{ fmt(h.scores.final) }}</span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <div v-else-if="!loading" class="empty">输入查询后点「调试检索」，查看检索每一步的中间结果与每条命中的分数归因。</div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { debugRetrieval } from '../api'

const query = ref('')
const topK = ref(5)
const docType = ref('')
const equipment = ref('')
const loading = ref(false)
const error = ref('')
const trace = ref(null)

const flags = computed(() => {
  const c = trace.value?.config || {}
  return [
    { label: 'Query改写', on: c.queryRewrite },
    { label: 'HyDE', on: c.hyde },
    { label: '多查询', on: c.multiQuery },
    { label: 'Rerank', on: c.rerank },
    { label: 'MMR', on: c.mmr },
    { label: 'Small-to-big', on: c.smallToBig },
  ]
})

const STEP_NAMES = {
  query_rewrite: 'Query 改写', multi_query: '多查询分解', retrieve: 'Dense + BM25 召回',
  rrf_fuse: 'RRF 融合', rerank: '重排', metadata_filter: '元数据过滤', mmr: 'MMR 去冗余',
}
const stepName = (k) => STEP_NAMES[k] || k
function stepMeta(s) {
  switch (s.step) {
    case 'query_rewrite': return s.changed ? '已改写' : '未改写'
    case 'multi_query': return `${s.totalQueries} 个查询`
    case 'retrieve': return `dense ${s.denseTotal} · bm25 ${s.bm25Total}`
    case 'rrf_fuse': return `${s.fusedCount} 个融合候选`
    case 'rerank': return s.ok ? `重排 ${s.reranked} 条` : (s.reason || '未启用')
    case 'metadata_filter': return s.skipped ? '跳过（无过滤条件）' : `${s.before} → ${s.after}`
    case 'mmr': return s.applied ? `选 topK` : '未触发'
    default: return ''
  }
}
const srcLabel = (s) => ({ dense_cloud: '云稠密', dense_bge: 'bge稠密', bm25: 'BM25' }[s] || s)
const srcBadge = (s) => ({ dense_cloud: 'badge-info', dense_bge: 'badge-primary', bm25: 'badge-warning' }[s] || 'badge-neutral')
const fmt = (v) => (v == null ? '—' : Number(v).toFixed(4))
const barWidth = (v) => `${Math.min(100, Math.max(2, (Number(v) || 0) * 100))}%`

async function run() {
  loading.value = true
  error.value = ''
  trace.value = null
  try {
    const res = await debugRetrieval(query.value.trim(), topK.value, {
      docType: docType.value.trim() || undefined,
      equipment: equipment.value.trim() || undefined,
    })
    if (res.code !== 200) { error.value = res.message || '调试失败'; return }
    trace.value = res.data
  } catch (e) {
    error.value = '请求失败：' + (e.message || e) + '（冷启动或外部 LLM/embedding 调用较慢，可重试）'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.steps { display: flex; flex-direction: column; gap: 10px; }
.step { display: flex; gap: 12px; }
.step-dot { width: 24px; height: 24px; border-radius: 50%; background: var(--primary-soft); color: var(--primary); display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; flex-shrink: 0; }
html.dark .step-dot { background: var(--primary-soft-2); }
.step-body { flex: 1; min-width: 0; }
.step-head { display: flex; align-items: center; gap: 10px; }
.step-name { font-weight: 600; font-size: 13px; }
.step-meta { font-size: 12px; color: var(--text-muted); }
.step-detail { font-size: 12px; color: var(--text-muted); margin-top: 3px; }
.chip { display: inline-block; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 1px 7px; margin: 2px 4px 0 0; font-size: 11px; }
.score-bar { position: relative; min-width: 100px; height: 18px; background: var(--surface-2); border-radius: 4px; overflow: hidden; display: flex; align-items: center; }
.score-fill { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, var(--primary), var(--accent)); opacity: .85; }
.score-bar span { position: relative; font-size: 11px; padding-left: 6px; color: var(--text); font-weight: 600; }
code { font-size: 12px; background: var(--surface-2); padding: 1px 5px; border-radius: 4px; color: var(--text); }
</style>
