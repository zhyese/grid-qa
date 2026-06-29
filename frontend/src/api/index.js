import request from './request'

// 系统
export const login = (username, password) => request.post('/system/login', { username, password })
export const register = (username, password, role) => request.post('/system/register', { username, password, role })
export const getLogs = (params) => request.get('/system/logs', { params })
export const configMilvus = (indexType, param) => request.post('/system/config/milvus', { indexType, param })
export const configModel = (modelType, param) => request.post('/system/config/model', { modelType, param })

// 文档
export const uploadDocs = (form) => request.post('/document/upload', form)
export const listDocs = (keyword = '') => request.get('/document/list', { params: { keyword } })
export const parseDocs = (docIds) => request.post('/document/parse', { docIds })
export const vectorize = (docId) => request.post('/document/vector/generate', { docId })
export const deleteDoc = (docId) => request.delete('/document/delete', { params: { docId } })

// 检索与问答
export const answer = (query, modelType) => request.post('/qa/answer', { query, modelType })
export const mixedRetrieval = (query, topK) => request.post('/retrieval/mixed', { query, topK })
export const sendFeedback = (query, answer, feedback, conversationId) =>
  request.post('/qa/feedback', { query, answer, feedback, conversationId })
