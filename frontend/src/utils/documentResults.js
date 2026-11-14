export function checkedDocumentResult(response) {
  if (!response || response.code !== 200) throw new Error(response?.message || '文档操作失败')
  return response.data
}

export function documentBatchSummary(action, data) {
  const success = data?.successList?.length || 0
  const failures = data?.failList || []
  const reason = failures.length ? `；${String(failures[0]).slice(0, 180)}` : ''
  return `${action}：成功 ${success} 份，失败 ${failures.length} 份${reason}`
}

export function remainingUploadFiles(files, result) {
  const success = new Set(result?.successList || [])
  return files.filter(file => !success.has(file.name) && !success.has(`${file.name}(已换版→需重新解析)`))
}
