import request from './request'
import { useAuthStore } from '../stores/auth'

// 系统
export const login = (username, password) => request.post('/system/login', { username, password })
export const register = (username, password, role) => request.post('/system/register', { username, password, role })
export const getLogs = (params) => request.get('/system/logs', { params })
export const configMilvus = (indexType, param) => request.post('/system/config/milvus', { indexType, param })
export const configModel = (modelType, param) => request.post('/system/config/model', { modelType, param })

// 文档
export const uploadDocs = (form, onProgress) => request.post('/document/upload', form, {
  onUploadProgress: onProgress,
})
export const listDocs = (keyword = '') => request.get('/document/list', { params: { keyword } })
export const parseDocs = (docIds) => request.post('/document/parse', { docIds })
export const vectorize = (docId) => request.post('/document/vector/generate', { docId })
export const deleteDoc = (docId) => request.delete('/document/delete', { params: { docId } })

// 检索与问答
export const answer = (query, modelType) => request.post('/qa/answer', { query, modelType })
export const mixedRetrieval = (query, topK) => request.post('/retrieval/mixed', { query, topK })

// 流式问答（SSE）：fetch + ReadableStream，支持 JWT header（EventSource 无法带 header）
export const streamAnswer = async (query, modelType, conversationId, onEvent) => {
  const auth = useAuthStore()
  const resp = await fetch('/api/qa/answer/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify({ query, modelType, conversationId }),
  })
  if (!resp.ok) throw new Error(`流式请求失败: ${resp.status}`)
  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
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
}
export const sendFeedback = (query, answer, feedback, conversationId) =>
  request.post('/qa/feedback', { query, answer, feedback, conversationId })

// 对话历史
export const getConversations = (keyword = '') => request.get('/qa/conversations', { params: { keyword } })
export const deleteConversation = (id) => request.delete(`/qa/conversations/${id}`)
export const renameConversation = (id, title) => request.put(`/qa/conversations/${id}`, { title })
export const getHistory = (conversationId) =>
  request.get('/qa/history', { params: { conversationId } })
