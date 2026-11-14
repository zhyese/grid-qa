// Shared by the stream handler and evidence panel; no Vue/browser dependency.
export function qaTransport(agentMode, useWS) {
  return useWS && !agentMode ? 'ws' : 'sse'
}

export function hydrateHistoryMessage(message) {
  const steps = Array.isArray(message.agentSteps) ? message.agentSteps : []
  return {
    time: 0, fb: '', ...message,
    sources: Array.isArray(message.sources) ? message.sources : [],
    query: message.role === 'user' ? message.content : (message.query || ''),
    halluc: message.agenticRag ? null : (message.halluc ?? 0),
    agentMode: Boolean(message.agentMode || message.agenticRag),
    agentSteps: steps,
    agentPhase: message.agentPhase || steps.at(-1)?.phase || '',
    streaming: false,
  }
}

export function applyAgenticEvent(message, event) {
  if (event.agentMode || event.agenticRag) message.agentMode = true
  if (event.agenticRag) message.agenticRag = true
  if (message.agenticRag) message.halluc = null

  if (event.type === 'meta' || event.type === 'done') {
    // Omitted fields are not replacements; explicit [] can revoke stale evidence.
    for (const key of ['sources', 'evidencePack']) {
      if (Array.isArray(event[key])) message[key] = event[key]
    }
    if (event.agenticResult && typeof event.agenticResult === 'object') {
      message.agenticResult = { ...message.agenticResult, ...event.agenticResult }
    }
    if (event.confidence) message.confidence = event.confidence
    if (typeof event.degraded === 'boolean') message.degraded = event.degraded
  }
  if (event.type === 'tool_step' && event.step) {
    message.agentMode = true
    const step = { ...event.step }
    if (step.result != null && typeof step.result !== 'string') step.result = JSON.stringify(step.result)
    const steps = (message.agentSteps ||= [])
    steps.push(step)
    if (step.phase) message.agentPhase = step.phase
  }
  if (event.type === 'error' && message.agentMode) {
    message.agenticError = event.message || event.content || '深度检索中断，已保留已接收证据与执行阶段。'
  }
}

const stopLabels = {
  sufficient: '当前证据已足够作答', tool_not_allowed: '请求的工具不在只读白名单',
  missing_parameters: '缺少必要查询参数', source_changed: '来源内容或访问权限已变化',
  unsupported_claims: '不受证据支持的表述已拦截', timeout: '达到执行时限', provider_error: '模型服务异常',
  sufficient_evidence: '当前证据已足够作答', completed: '证据收集完成',
  round_budget: '已达到轮次上限', tool_budget: '已达到工具调用上限',
  time_budget: '已达到总时限', no_new_evidence: '连续补查无新增证据',
  insufficient_evidence: '证据不足', clarification_needed: '需要补充问题信息',
  no_evidence: '未取得可用证据', model_error: '模型调用异常', tool_error: '工具调用异常',
  cancelled: '已中止', verification_failed: '引用或数字校验未通过',
}
const phaseLabels = {
  retrieve: '收集证据', assess: '评估证据', blocked: '已阻止不合规调用', generate: '整理答案', stopped: '检索已停止',
  plan: '规划检索', planning: '规划检索', collect: '收集证据', tool: '调用只读工具',
  evaluate: '评估证据', assessment: '评估证据', verify: '结构校验',
  revalidate: '复核证据权限与版本', answer: '整理答案', final: '整理答案',
}

export function agenticSummary(message) {
  const result = message.agenticResult || {}
  const verification = result.verification || {}
  const checked = []
  if (verification.citationIdsChecked) checked.push('引用编号')
  if (verification.numbersChecked) checked.push('数字')
  const rejected = Number.isInteger(verification.rejectedClaims) && verification.rejectedClaims > 0
    ? `；已剔除 ${verification.rejectedClaims} 条不支持的表述` : ''
  const count = value => Number.isInteger(value) && value >= 0 ? value : '—'
  return {
    statusLabel: ({ completed: '证据收集完成', partial: '证据有限', insufficient: '证据不足', clarification: '需要澄清' })[result.status]
      || (message.streaming ? '正在收集证据' : '等待证据结果'),
    tone: ({ completed: 'info', partial: 'warning', insufficient: 'danger', clarification: 'warning' })[result.status] || 'neutral',
    stopLabel: stopLabels[result.stopReason] || result.stopReason || '尚未返回停止原因',
    phaseLabel: phaseLabels[message.agentPhase] || message.agentPhase || '',
    confidenceLabel: ({ high: '后端置信等级：高（不代表语义已核验）', medium: '证据可信度有限', refused: '证据不足，保守作答', low: '证据可信度低' })[message.confidence] || '置信度未评估',
    verificationLabel: `${checked.length ? `${checked.join('、')}已做结构校验` : '结构校验结果未返回'}；${verification.semanticEntailmentVerified === true ? '语义核验结果由后端报告' : '语义支持未全面核验'}${rejected}`,
    budgetLabel: `轮次 ${count(result.rounds)}/3 · 工具调用 ${count(result.toolCalls)}/6 · 总时限 60 秒`,
  }
}

export function sourceDocumentPath(source) {
  if (!source || typeof source !== 'object' || source.kind === 'telemetry') return ''
  const id = source.docId
  if ((typeof id !== 'string' && typeof id !== 'number') || !String(id).trim()) return ''
  return `/api/document/preview/${encodeURIComponent(String(id))}`
}

export function sourceAnchorId(messageIndex, citationNumber) {
  return `src-${messageIndex}-${citationNumber}`
}

export function sourceCitationNumber(source, index) {
  return Number.isInteger(source?.citationIndex) && source.citationIndex > 0
    ? source.citationIndex : index + 1
}

export async function sourcePreviewContent(response) {
  if (!response.ok) throw new Error('原文不可访问，可能已撤回或无权限')
  const blob = await response.blob()
  if (blob.type.includes('application/json')) throw new Error('原文不可访问，可能已撤回或无权限')
  if (blob.type.includes('pdf')) return { type: 'pdf', blob }
  if (blob.type.startsWith('image/')) return { type: 'image', blob }
  return { type: 'text', text: await blob.text() }
}
