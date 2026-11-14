import test from 'node:test'
import assert from 'node:assert/strict'

// A missing implementation is an assertion failure, not an import-time crash.
const api = await import('./agenticResult.js').catch(error => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error
  return {}
})
const required = name => {
  assert.equal(typeof api[name], 'function', `${name} must implement the public behavior`)
  return api[name]
}

test('deep requests use SSE even when the WS preference is enabled', () => {
  const transport = required('qaTransport')
  assert.equal(transport(true, true), 'sse')
  assert.equal(transport(true, false), 'sse')
  assert.equal(transport(false, true), 'ws')
  assert.equal(transport(false, false), 'sse')
})

test('done preserves evidence, verification and status across the trailing empty done', () => {
  const apply = required('applyAgenticEvent')
  const message = { sources: [], agentSteps: [], halluc: 0 }
  apply(message, { type: 'done', agenticRag: true, sources: [{ docId: 'd1', evidenceId: 'E1' }],
    evidencePack: [{ evidenceId: 'E1', text: '原文' }], confidence: 'medium', degraded: true,
    agenticResult: { status: 'partial', stopReason: 'tool_budget', missingEvidence: ['缺少复测'],
      conflicts: ['两份记录不一致'], rounds: 3, toolCalls: 6,
      verification: { citationIdsChecked: true, numbersChecked: true, semanticEntailmentVerified: false, rejectedClaims: 1 } } })
  apply(message, { type: 'done' })
  apply(message, { type: 'done', agenticResult: {} })
  assert.equal(message.agenticResult.status, 'partial')
  assert.equal(message.agenticResult.toolCalls, 6)
  assert.equal(message.agenticResult.verification.semanticEntailmentVerified, false)
  assert.deepEqual(message.sources, [{ docId: 'd1', evidenceId: 'E1' }])
  assert.deepEqual(message.evidencePack, [{ evidenceId: 'E1', text: '原文' }])
  assert.equal(message.confidence, 'medium')
  assert.equal(message.degraded, true)
  assert.equal(message.halluc, null)
})

test('explicit final empty sources revoke previously received evidence', () => {
  const apply = required('applyAgenticEvent')
  const message = { sources: [{ docId: 'withdrawn' }], evidencePack: [{ evidenceId: 'E1' }] }
  apply(message, { type: 'done', agenticRag: true, sources: [], evidencePack: [], agenticResult: { status: 'insufficient' } })
  assert.deepEqual(message.sources, [])
  assert.deepEqual(message.evidencePack, [])
})

test('meta without sources does not clear earlier sources or the existing conversation', () => {
  const apply = required('applyAgenticEvent')
  const message = { sources: [{ docId: 'd1' }], conversationId: 'conversation-1' }
  apply(message, { type: 'meta', agenticRag: true })
  assert.deepEqual(message.sources, [{ docId: 'd1' }])
  assert.equal(message.conversationId, 'conversation-1')
})

test('tool steps keep readable phase summaries and tolerate structured result payloads', () => {
  const apply = required('applyAgenticEvent')
  const message = { agentMode: true, agentSteps: [] }
  apply(message, { type: 'tool_step', step: { iter: 2, phase: 'collect', tool: 'search_documents', args: { query: '巡视' }, result: { count: 2 } } })
  assert.equal(message.agentPhase, 'collect')
  assert.equal(message.agentSteps[0].tool, 'search_documents')
  assert.equal(typeof message.agentSteps[0].result, 'string')
  assert.match(message.agentSteps[0].result, /2/)
})

test('network or module errors retain evidence, result and last completed phase', () => {
  const apply = required('applyAgenticEvent')
  const message = { agentMode: true, agenticRag: true, content: '已有结论', sources: [{ docId: 'd1' }],
    agentSteps: [{ iter: 1, phase: 'collect', tool: 'search_documents', result: '1 条' }],
    agentPhase: 'collect', agenticResult: { status: 'partial', rounds: 1 } }
  apply(message, { type: 'error', message: '连接中断' })
  assert.equal(message.content, '已有结论')
  assert.equal(message.sources.length, 1)
  assert.equal(message.agentSteps.length, 1)
  assert.equal(message.agentPhase, 'collect')
  assert.equal(message.agenticResult.rounds, 1)
  assert.equal(message.agenticError, '连接中断')
})

test('each evidence outcome has a distinct non-default confidence presentation', () => {
  const summary = required('agenticSummary')
  for (const [status, label] of [
    ['completed', '证据收集完成'], ['partial', '证据有限'],
    ['insufficient', '证据不足'], ['clarification', '需要澄清'],
  ]) {
    const view = summary({ agenticResult: { status }, sources: [] })
    assert.equal(view.statusLabel, label)
    assert.doesNotMatch(view.confidenceLabel, /高置信|可信度高/)
  }
  assert.equal(summary({}).statusLabel, '等待证据结果')
})

test('structural verification never claims semantic verification or an unmeasured zero hallucination rate', () => {
  const view = required('agenticSummary')({ agenticResult: { status: 'completed', rounds: 3, toolCalls: 6,
    verification: { citationIdsChecked: true, numbersChecked: true, semanticEntailmentVerified: false, rejectedClaims: 2 } } })
  assert.match(view.verificationLabel, /引用编号.*数字/)
  assert.match(view.verificationLabel, /语义.*未/)
  assert.match(view.verificationLabel, /2/)
  assert.doesNotMatch(view.verificationLabel, /全部核验通过|0%/)
  assert.equal(view.budgetLabel, '轮次 3/3 · 工具调用 6/6 · 总时限 60 秒')
})

test('uncounted budgets stay unknown rather than displaying fabricated zero usage', () => {
  const view = required('agenticSummary')({})
  assert.match(view.budgetLabel, /轮次 —\/3/)
  assert.match(view.budgetLabel, /工具调用 —\/6/)
})

test('document and grounded graph sources open the authenticated preview endpoint, telemetry never becomes a document link', () => {
  const path = required('sourceDocumentPath')
  assert.equal(path({ kind: 'document', docId: 'doc 1' }), '/api/document/preview/doc%201')
  assert.equal(path({ kind: 'graph', docId: 'd2' }), '/api/document/preview/d2')
  assert.equal(path({ kind: 'telemetry', docId: 'invented', deviceId: 'D1' }), '')
  assert.equal(path({ kind: 'graph' }), '')
  assert.equal(path(null), '')
  assert.equal(path('plain source'), '')
})

test('unknown stop reasons remain visible instead of being presented as success', () => {
  const view = required('agenticSummary')({ agenticResult: { status: 'partial', stopReason: 'future_budget_reason' } })
  assert.match(view.stopLabel, /future_budget_reason/)
  assert.doesNotMatch(view.stopLabel, /完成|成功/)
})

test('same citation number in different chat turns resolves to a different source anchor', () => {
  const anchor = required('sourceAnchorId')
  assert.equal(anchor(1, 1), 'src-1-1')
  assert.equal(anchor(3, 1), 'src-3-1')
  assert.notEqual(anchor(1, 1), anchor(3, 1))
})

test('preview rejects HTTP and HTTP-200 business failures instead of showing them as source text', async () => {
  const preview = required('sourcePreviewContent')
  await assert.rejects(preview(new Response('denied', { status: 403 })), /不可访问/)
  await assert.rejects(preview(new Response(JSON.stringify({ code: 403, message: '无权限', data: null }),
    { headers: { 'Content-Type': 'application/json' } })), /不可访问/)
})

test('preview preserves PDF bytes and renders source markup as text rather than HTML', async () => {
  const preview = required('sourcePreviewContent')
  const pdf = await preview(new Response('%PDF-evidence', { headers: { 'Content-Type': 'application/pdf' } }))
  assert.equal(pdf.type, 'pdf')
  assert.equal(await pdf.blob.text(), '%PDF-evidence')
  const markup = await preview(new Response('<script>unsafe()</script>', { headers: { 'Content-Type': 'text/html' } }))
  assert.equal(markup.type, 'text')
  assert.equal(markup.text, '<script>unsafe()</script>')
})

test('restoring an agentic history message keeps evidence, result, trace and snapshot flags', () => {
  const message = required('hydrateHistoryMessage')({
    id: 42, role: 'assistant', content: '历史答复[3]', agenticRag: true, agentMode: true, historySnapshot: true,
    sources: [{ evidenceId: 'E3', citationIndex: 3, docId: 'd3', text: '仍有权限的原文' }],
    evidencePack: [{ evidenceId: 'E3', kind: 'document', text: '仍有权限的原文' }],
    agenticResult: { status: 'partial', stopReason: 'tool_budget', rounds: 2, toolCalls: 6, missingEvidence: ['复测数据'], conflicts: [] },
    agentSteps: [{ iter: 2, phase: 'assess', result: '仍需复测数据' }], confidence: 'medium', degraded: true,
  })
  assert.equal(message.id, 42)
  assert.equal(message.content, '历史答复[3]')
  assert.deepEqual(message.sources, [{ evidenceId: 'E3', citationIndex: 3, docId: 'd3', text: '仍有权限的原文' }])
  assert.deepEqual(message.evidencePack, [{ evidenceId: 'E3', kind: 'document', text: '仍有权限的原文' }])
  assert.equal(message.agenticResult.status, 'partial')
  assert.equal(message.agenticResult.toolCalls, 6)
  assert.deepEqual(message.agenticResult.missingEvidence, ['复测数据'])
  assert.deepEqual(message.agentSteps, [{ iter: 2, phase: 'assess', result: '仍需复测数据' }])
  assert.equal(message.agentPhase, 'assess')
  assert.equal(message.agentMode, true)
  assert.equal(message.agenticRag, true)
  assert.equal(message.historySnapshot, true)
  assert.equal(message.confidence, 'medium')
  assert.equal(message.degraded, true)
  assert.equal(message.streaming, false)
})

test('agentic history never restores a default zero hallucination measurement', () => {
  const hydrate = required('hydrateHistoryMessage')
  assert.equal(hydrate({ role: 'assistant', content: '历史答复', agenticRag: true }).halluc, null)
  assert.equal(hydrate({ role: 'assistant', content: '历史答复', agenticRag: true, halluc: 0 }).halluc, null)
})

test('legacy history without metadata retains ordinary message defaults and the user question', () => {
  const hydrate = required('hydrateHistoryMessage')
  const user = hydrate({ id: 1, role: 'user', content: '巡视项目有哪些？' })
  assert.equal(user.query, '巡视项目有哪些？')
  assert.equal(user.role, 'user')
  assert.equal(user.id, 1)
  const assistant = hydrate({ id: 2, role: 'assistant', content: '普通历史答案' })
  assert.equal(assistant.content, '普通历史答案')
  assert.equal(assistant.query, '')
  assert.equal(assistant.halluc, 0)
  assert.equal(assistant.time, 0)
  assert.deepEqual(assistant.sources, [])
  assert.equal(assistant.agenticRag, undefined)
})

test('permission-filtered historical sources keep their original citation numbers', () => {
  const number = required('sourceCitationNumber')
  assert.equal(number({ evidenceId: 'E3', citationIndex: 3 }, 0), 3)
  assert.equal(number({ evidenceId: 'E7', citationIndex: 7 }, 1), 7)
  assert.equal(number({ docId: 'ordinary' }, 0), 1)
  assert.equal(number('legacy source text', 1), 2)
})
