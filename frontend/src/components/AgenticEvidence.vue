<template>
  <section class="agentic-evidence" aria-label="深度问答证据状态" aria-live="polite">
    <div class="evidence-head">
      <strong>深度问答 · 证据状态</strong>
      <span class="badge" :class="'badge-' + summary.tone">{{ summary.statusLabel }}</span>
      <span v-if="message.degraded" class="badge badge-warning">降级结果</span>
    </div>
    <p v-if="message.historySnapshot" class="history-notice">历史查证快照，不代表当前状态；可重新提问刷新。来源仅展示当前仍可访问的部分，引用沿用原编号。</p>
    <p v-if="summary.phaseLabel">最近阶段：{{ summary.phaseLabel }}</p>
    <p v-if="message.agenticError" class="evidence-error">{{ message.agenticError }}（已保留已接收证据与执行阶段）</p>
    <p v-if="!message.streaming">停止原因：{{ summary.stopLabel }}</p>
    <p>{{ summary.budgetLabel }}</p>
    <p>{{ summary.confidenceLabel }} · 已接收 {{ message.sources?.length || 0 }} 条引用来源</p>
    <p class="verification-note">{{ summary.verificationLabel }}；幻觉率未评估。</p>
    <div v-if="result.missingEvidence?.length" class="evidence-details">
      <strong>待补证据 / 澄清信息</strong>
      <ul><li v-for="(item, index) in result.missingEvidence" :key="index">{{ item }}</li></ul>
    </div>
    <div v-if="result.conflicts?.length" class="evidence-details">
      <strong>证据冲突（需人工核对）</strong>
      <ul><li v-for="(item, index) in result.conflicts" :key="index">{{ item }}</li></ul>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { agenticSummary } from '../utils/agenticResult.js'

const props = defineProps({ message: { type: Object, required: true } })
const result = computed(() => props.message.agenticResult || {})
const summary = computed(() => agenticSummary(props.message))
</script>

<style scoped>
.agentic-evidence { margin-top: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-size: 12px; line-height: 1.6; }
.evidence-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
p { margin: 5px 0; overflow-wrap: anywhere; }
.verification-note { color: var(--text-muted); }
.history-notice { padding: 6px 8px; border-left: 3px solid var(--warning); background: var(--surface-2); }
.evidence-error { color: var(--danger); }
.evidence-details { margin-top: 8px; }
ul { margin: 4px 0; padding-left: 20px; }
</style>
