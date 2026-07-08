<!--
  Agent 思考链渲染（共享组件）：Chat 🎯深度思考 / Admin 证据深度补全 复用。
  props: steps(思考链数组 [{iter,tool,args,result,error}]) / title
-->
<template>
  <div class="agent-trace" v-if="steps && steps.length">
    <div class="trace-head" @click="open = !open">
      <span>{{ title }} · {{ steps.length }}步</span>
      <span class="trace-toggle">{{ open ? '收起 ▾' : '展开 ▸' }}</span>
    </div>
    <div v-show="open" class="trace-steps">
      <div v-for="(st, k) in steps" :key="k" class="trace-step" :class="{ 'is-final': !st.tool }">
        <span class="trace-iter">第{{ st.iter }}轮</span>
        <span v-if="st.tool" class="trace-tool">🔧 {{ st.tool }}<span class="trace-args" v-if="st.args && Object.keys(st.args).length">({{ JSON.stringify(st.args) }})</span></span>
        <span v-else class="trace-thought">💭 综合作答</span>
        <div v-if="st.result" class="trace-result" :class="{ err: st.error }">{{ (st.result || '').slice(0, 220) }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
defineProps({
  steps: { type: Array, default: () => [] },
  title: { type: String, default: '🧠 深度思考' },
})
const open = ref(true)
</script>

<style scoped>
.agent-trace { padding: 6px 10px; background: rgba(108, 92, 231, 0.06); border: 1px solid var(--border); border-radius: 8px; font-size: 12px; }
.trace-head { font-weight: 600; color: var(--primary); cursor: pointer; user-select: none; display: flex; justify-content: space-between; }
.trace-toggle { font-weight: 400; color: var(--text-muted); }
.trace-steps { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
.trace-step { padding: 4px 6px; background: var(--surface); border-radius: 6px; border-left: 3px solid var(--primary); }
.trace-step.is-final { border-left-color: #6c5ce7; }
.trace-iter { color: var(--text-muted); margin-right: 6px; }
.trace-tool { font-weight: 600; color: var(--text); }
.trace-args { color: var(--text-muted); font-weight: 400; }
.trace-thought { color: var(--text-soft); }
.trace-result { color: var(--text-soft); margin-top: 2px; white-space: pre-wrap; word-break: break-all; }
.trace-result.err { color: var(--danger); }
</style>
