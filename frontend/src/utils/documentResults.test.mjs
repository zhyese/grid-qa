import test from 'node:test'
import assert from 'node:assert/strict'
import { checkedDocumentResult, documentBatchSummary, remainingUploadFiles } from './documentResults.js'

test('HTTP 200 business failures must not resolve to success data', () => {
  assert.throws(() => checkedDocumentResult({ code: 502, message: '向量模型不可用', data: null }), /向量模型不可用/)
  assert.throws(() => checkedDocumentResult({ code: 403, message: '无权限' }), /无权限/)
  assert.equal(checkedDocumentResult({ code: 200, data: null }), null)
})

test('batch partial failure reports failures and their actual reason', () => {
  const summary = documentBatchSummary('上传', { successList: ['one'], failList: ['two(格式不支持)'] })
  assert.match(summary, /成功 1/)
  assert.match(summary, /失败 1/)
  assert.match(summary, /格式不支持/)
})

test('all failed batch never says operation complete or successful', () => {
  const summary = documentBatchSummary('向量化', { successList: [], failList: ['模型不可用'] })
  assert.match(summary, /成功 0/)
  assert.match(summary, /失败 1/)
  assert.doesNotMatch(summary, /完成/)
})

test('partial upload retries only failed files and does not replace successful documents', () => {
  const files = [{ name: 'A.pdf' }, { name: 'B.pdf' }, { name: 'C.pdf' }]
  assert.deepEqual(remainingUploadFiles(files, {
    successList: ['A.pdf', 'C.pdf(已换版→需重新解析)'], failList: ['B.pdf(上传失败)'],
  }), [{ name: 'B.pdf' }])
})
