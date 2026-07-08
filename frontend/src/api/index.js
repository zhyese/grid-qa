import request from './request'
import { useAuthStore } from '../stores/auth'

// 系统
export const login = (username, password) => request.post('/system/login', { username, password })
export const register = (username, password, role) => request.post('/system/register', { username, password, role })
export const getLogs = (params) => request.get('/system/logs', { params })
export const getAlerts = (params) => request.get('/system/alerts', { params })
export const configMilvus = (indexType, param) => request.post('/system/config/milvus', { indexType, param })
export const configModel = (modelType, param) => request.post('/system/config/model', { modelType, param })
export const getMilvusConfig = () => request.get('/system/config/milvus')
export const getModelConfig = () => request.get('/system/config/model')
export const getProviderHealth = () => request.get('/system/health/providers')
export const rebuildBm25 = () => request.post('/retrieval/bm25/rebuild')

// 文档
export const uploadDocs = (form, onProgress) => request.post('/document/upload', form, {
  onUploadProgress: onProgress,
})
export const listDocs = (keyword = '') => request.get('/document/list', { params: { keyword } })
export const parseDocs = (docIds) => request.post('/document/parse', { docIds })
export const vectorize = (docId) => request.post('/document/vector/generate', { docId })
export const vectorizeBatch = (docIds) => request.post('/document/vector/batch', { docIds })
export const deleteDoc = (docId) => request.delete('/document/delete', { params: { docId } })
export const getStats = () => request.get('/document/stats')

// 知识图谱
export const extractKg = (docId, modelType) => request.post('/kg/extract', { docId, modelType })
export const getKgGraph = (entity = '', limit = 300) => request.get('/kg/graph', { params: { entity, limit } })
export const getKgStats = () => request.get('/kg/stats')
export const getKgPaths = (entity, depth = 3, limit = 20) => request.get('/kg/path', { params: { entity, depth, limit } })
export const getKgInfluence = (limit = 15) => request.get('/kg/influence', { params: { limit } })

// 检索与问答
export const answer = (query, modelType) => request.post('/qa/answer', { query, modelType })
export const mixedRetrieval = (query, topK) => request.post('/retrieval/mixed', { query, topK })
export const debugRetrieval = (query, topK, params = {}) =>
  request.post('/retrieval/debug', { query, topK, ...params })

// 领域增强：故障诊断(D1) / 相似案例(D2) / 两票生成(D3)
export const diagnose = (symptom, modelType) => request.post('/domain/diagnose', { symptom, modelType })
export const diagnoseAgent = (symptom, modelType) => request.post('/domain/diagnose-agent', { symptom, modelType })
export const diagnoseDebate = (symptom, modelType) => request.post('/domain/diagnose-debate', { symptom, modelType })
export const similarCase = (symptom, modelType) => request.post('/domain/similar-case', { symptom, modelType })
export const generateTicket = (task, modelType) => request.post('/domain/ticket', { task, modelType })
export const auditTicket = (ticketText, ticketType, modelType) =>
  request.post('/domain/ticket/audit', { ticketText, ticketType, modelType })

// 两票全生命周期管理
export const createTicket = (data) => request.post('/domain/ticket/create', data)
export const listTickets = (params) => request.get('/domain/ticket/list', { params })
export const getTicket = (id) => request.get(`/domain/ticket/${id}`)
export const submitTicket = (id) => request.post(`/domain/ticket/${id}/submit`)
export const reviewTicket = (id, approved, comment) => request.post(`/domain/ticket/${id}/review`, { approved, comment })
export const issueTicket = (id) => request.post(`/domain/ticket/${id}/issue`)
export const executeTicket = (id, data) => request.post(`/domain/ticket/${id}/execute`, data)
export const archiveTicket = (id) => request.post(`/domain/ticket/${id}/archive`)
export const deleteTicket = (id) => request.delete(`/domain/ticket/${id}`)
export const getTicketStats = () => request.get('/domain/ticket-stats')

// 流式问答（SSE）：fetch + ReadableStream，支持 JWT header（EventSource 无法带 header）
// signal：AbortController.signal，用于「停止生成」；regen：跳过缓存重新生成
export const streamAnswer = async (query, modelType, conversationId, onEvent, signal, regen = false, agentMode = false) => {
  const auth = useAuthStore()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  try {
    const resp = await fetch(`/api/qa/answer/stream${regen ? '?regen=true' : ''}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
      },
      body: JSON.stringify({ query, modelType, conversationId, agentMode }),
      signal,
    })
    if (!resp.ok) throw new Error(`流式请求失败: ${resp.status}`)
    const reader = resp.body.getReader()
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop()                       // 保留半行，下次拼接
      for (const line of lines) {
        const s = line.trim()
        if (!s.startsWith('data:')) continue
        const payload = s.slice(5).trim()
        if (payload === '[DONE]') return onEvent({ type: 'done' })
        try { onEvent(JSON.parse(payload)) } catch (e) { /* skip */ }
      }
    }
    onEvent({ type: 'done' })
  } catch (e) {
    // 停止生成（fetch/reader 任一阶段 abort）：保留已收内容，发 aborted 事件
    if (e.name === 'AbortError') { onEvent({ type: 'aborted' }); return }
    throw e
  }
}
// S3 告警自动处置：手动触发 + 处置记录列表
export const alertDispose = (severity, title, summary, modelType) =>
  request.post('/system/alerts/dispose', { severity, title, summary, modelType })
export const getAlertDisposals = (params) => request.get('/system/alerts/disposals', { params })
// S5 persona 配置管理（admin）
export const getPersonas = () => request.get('/system/agent/personas')
export const upsertPersona = (data) => request.post('/system/agent/personas', data)
export const deletePersona = (name) => request.delete('/system/agent/personas', { params: { name } })
export const sendFeedback = (query, answer, feedback, conversationId, reason, sources = []) =>
  request.post('/qa/feedback', { query, answer, feedback, conversationId, reason, retrievalSources: (sources || []).map(s => typeof s === 'string' ? s : (s?.docName || '')).filter(Boolean).join(',') })

// 答案导出 Word（blob 下载，绕过 JSON 响应拦截器）
export const exportAnswer = async (query, answer, sources, meta) => {
  const auth = useAuthStore()
  const resp = await fetch('/api/qa/export', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify({ query, answer, sources, meta }),
  })
  if (!resp.ok) throw new Error('导出失败')
  return await resp.blob()
}

// 真 faithfulness：流式 done 后异步拉取，覆盖粗糙"幻觉率"展示（不阻塞首字）
export const getFaithfulness = (answer, sources, modelType) =>
  request.post('/qa/faithfulness', { answer, sources, modelType })

// WebSocket 流式问答（双向，SSE 增强版，为服务端主动推送留能力）
export const streamAnswerWS = (query, modelType, conversationId, onEvent) => {
  const auth = useAuthStore()
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/api/qa/answer/ws?token=${encodeURIComponent(auth.token || '')}`)
  ws.onopen = () => ws.send(JSON.stringify({ query, modelType, conversationId }))
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data)
      onEvent(ev)
      if (ev.type === '_ws_done' || ev.type === 'error') ws.close()
    } catch (err) { /* skip */ }
  }
  ws.onerror = () => { onEvent({ type: 'done' }); try { ws.close() } catch (x) {} }
  return ws
}

// 反馈管理（admin）：坏 case 看板 + 一键回流 golden
export const getFeedbacks = (params) => request.get('/qa/feedbacks', { params })
export const markFeedbackGolden = (id) => request.post(`/qa/feedbacks/${id}/golden`)
export const getFeedbackStats = () => request.get('/qa/feedback-stats')

// 智能推荐：生成 3 个相关问题（答案渲染后异步拉取，不阻塞流式）
export const getRelatedQuestions = (query, answer, modelType) =>
  request.post('/qa/related', { query, answer, modelType })

// 对话历史
export const getConversations = (keyword = '') => request.get('/qa/conversations', { params: { keyword } })
export const deleteConversation = (id) => request.delete(`/qa/conversations/${id}`)
export const batchDeleteConversations = (ids) =>
  request.post('/qa/conversations/batch-delete', { ids })
export const batchDeleteMessages = (ids) =>
  request.post('/qa/messages/batch-delete', { ids })
export const renameConversation = (id, title) => request.put(`/qa/conversations/${id}`, { title })
export const getHistory = (conversationId) =>
  request.get('/qa/history', { params: { conversationId } })

// P2-⑦ 成本追踪
export const getCostReport = (period) => request.get('/system/cost/report', { params: { period } })
export const checkQuota = () => request.get('/system/cost/quota')

// P2-⑨ 复杂问题分解
export const queryPlan = (question, modelType) => request.post('/domain/query-plan', { question, modelType })

// P2-⑩ 在线评测
export const getEvalTrends = (days = 7) => request.get('/system/eval/trends', { params: { days } })

// P2-⑧ 路由配置 & A/B 测试
export const getRoutingConfig = () => request.get('/system/routing/config')

// P3-⑬ 知识库质量
export const getKnowledgeQuality = () => request.get('/system/knowledge/quality')

// P4-⑮ 证据溯源
export const getEvidenceTrace = (answer, sources, modelType) =>
  request.post('/qa/evidence-trace', { answer, sources, modelType })

// 证据补全闭环（admin）
export const getEvidenceGaps = (params) => request.get('/system/evidence-gap', { params })
export const aiDraftGap = (id, modelType) => request.post(`/system/evidence-gap/${id}/ai-draft`, { modelType })
export const deepDraftGap = (id, modelType) => request.post(`/system/evidence-gap/${id}/deep-draft`, { modelType })
export const confirmGap = (id, finalAnswer, modelType) =>
  request.post(`/system/evidence-gap/${id}/confirm`, { finalAnswer, modelType })
export const ignoreGap = (id) => request.post(`/system/evidence-gap/${id}/ignore`)
export const deleteGap = (id) => request.delete(`/system/evidence-gap/${id}`)
export const reportEvidenceGap = (query, answer, confidence, grade, action) =>
  request.post('/qa/evidence-gap/report', { query, answer, confidence, grade, action })
