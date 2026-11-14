export function unwrapKg(response) {
  if (!response || response.code !== 200 || !response.data) {
    throw new Error(response?.message || '抽取请求失败，请刷新后重试')
  }
  return response.data
}

export function extractionError(error) {
  if (error?.response?.status === 429) return '操作过于频繁，请稍后重试；多份文档请使用批量抽取，一次提交即可'
  if (error?.code === 'ECONNABORTED') return '请求超时，请先查看任务记录确认是否已提交，避免重复操作'
  if (error?.response?.status === 422) return '提交参数不正确：每批请选择 1–200 份文档'
  return error?.message || '请求失败，请检查网络后重试'
}

export function taskLabel(task) {
  if (task.status === 'succeeded') {
    return { empty: '未抽到三元组', partial: '部分完成', completed: '完成' }[task.result?.status] || '完成'
  }
  return { queued: '排队中', running: '抽取中', failed: '等待自动重试', dead: '失败' }[task.status] || task.status
}

export function taskDetail(task) {
  if (task.error) return task.error
  const r = task.result
  if (!r || !Object.keys(r).length) return '后台逐篇执行，可离开页面'
  const notes = [`抽取 ${r.tripleCount || 0} 条，新增 ${r.newCount || 0} 条`]
  if (r.failedBatches) notes.push(`${r.failedBatches} 个分块批次失败，可重新选择文档补抽`)
  if (r.neo4jSynced === false) notes.push('已存 MySQL，Neo4j 同步失败，请联系管理员检查')
  return notes.join('；')
}

export function selectDocuments(selected, documents, onlyMissing = false) {
  return [...new Set([...selected, ...documents.filter(d => d.chunkCount > 0 && (!onlyMissing || !d.tripleCount)).map(d => d.docId)])].slice(0, 200)
}
