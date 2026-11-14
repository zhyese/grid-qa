import test from 'node:test'
import assert from 'node:assert/strict'
import { unwrapKg, extractionError, taskLabel, taskDetail, selectDocuments } from './kgExtraction.js'

test('business error cannot masquerade as successful extraction', () => {
  assert.throws(() => unwrapKg({ code: 403, message: '无权限', data: null }), /无权限/)
  assert.deepEqual(unwrapKg({ code: 200, data: { newCount: 2 } }), { newCount: 2 })
})
test('rate limiting and timeouts have actionable messages', () => {
  assert.match(extractionError({ response: { status: 429 } }), /频繁/)
  assert.match(extractionError({ code: 'ECONNABORTED' }), /任务/)
})
test('partial results and retries are not presented as completed', () => {
  assert.equal(taskLabel({ status: 'failed' }), '等待自动重试')
  assert.equal(taskLabel({ status: 'succeeded', result: { status: 'partial' } }), '部分完成')
  assert.equal(taskLabel({ status: 'succeeded', result: { status: 'empty' } }), '未抽到三元组')
  assert.match(taskDetail({ result: { tripleCount: 4, newCount: 1, failedBatches: 2 } }), /新增 1/)
  assert.match(taskDetail({ result: { failedBatches: 2 } }), /2.*失败/)
})
test('select only eligible missing-graph documents, preserve previous pages, deduplicate', () => {
  const rows = [{ docId: 'a', chunkCount: 1, tripleCount: 0 },
    { docId: 'b', chunkCount: 0, tripleCount: 0 }, { docId: 'c', chunkCount: 1, tripleCount: 4 }]
  assert.deepEqual(selectDocuments(['old', 'a'], rows, true), ['old', 'a'])
  assert.deepEqual(selectDocuments(['old'], rows, false), ['old', 'a', 'c'])
})
