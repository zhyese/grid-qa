import request from './request'

const base = '/ops-workbench'
export const listWorkbenchDocuments = (keyword) => request.get(`${base}/documents`, { params: { keyword } })
export const listWorkbenchDevices = (keyword) => request.get(`${base}/devices`, { params: { keyword } })
export const getTableSchema = (id) => request.get(`${base}/tables/${encodeURIComponent(id)}/schema`)
export const analyzeImpact = (body) => request.post(`${base}/impacts`, body)
export const createHandover = (body) => request.post(`${base}/handovers`, body)
export const listOpsSnapshots = (kind, page = 1) => request.get(`${base}/snapshots`, { params: { kind, page } })
export const getOpsSnapshot = (id) => request.get(`${base}/snapshots/${encodeURIComponent(id)}`)
export const reviewOpsSnapshot = (id, body) => request.post(`${base}/snapshots/${encodeURIComponent(id)}/review`, body)
export const getDeviceDossier = (id) => request.get(`${base}/devices/${encodeURIComponent(id)}`)
export const saveDeviceDossier = (id) => request.post(`${base}/devices/${encodeURIComponent(id)}/snapshots`)
export const importTelemetry = (body) => request.post(`${base}/telemetry/import`, body)
export const queryTelemetry = (body) => request.post(`${base}/telemetry/query`, body)
export const queryTable = (body) => request.post(`${base}/tables/query`, body)
