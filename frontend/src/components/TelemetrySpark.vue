<!--
  F2 迷你遥测趋势图（内联 SVG，无第三方依赖）。
  用于 Chat 的 Agent 思考链中 query_telemetry 步骤旁：按步骤参数拉只读序列并画折线。
  失败/无数据时静默不渲染（不打扰主对话）。
-->
<template>
  <div v-if="points.length >= 2" class="spark">
    <div class="spark-head">
      <span class="spark-title">{{ deviceId }}{{ metric ? ' · ' + metric : '' }} · 近{{ hours }}h</span>
      <span class="spark-stat" v-if="stats">
        {{ stats.min }}~{{ stats.max }}{{ stats.unit }} ·
        <b :class="trendClass">{{ trendText }}</b>
      </span>
    </div>
    <svg :viewBox="`0 0 ${W} ${H}`" class="spark-svg" preserveAspectRatio="none">
      <path class="spark-area" :d="areaPath" />
      <polyline class="spark-line" :points="polyline" />
      <circle class="spark-dot" :cx="lastX" :cy="lastY" r="2.4" />
    </svg>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getTelemetrySeries } from '../api'

const props = defineProps({
  args: { type: Object, default: () => ({}) },
})

const W = 240
const H = 48
const points = ref([])
const stats = ref(null)
const trend = ref(null)
const deviceId = ref('')
const metric = ref('')
const hours = ref(24)

const vals = computed(() => points.value.map(p => Number(p.value)))
const polyline = computed(() => {
  const v = vals.value
  if (v.length < 2) return ''
  const min = Math.min(...v)
  const max = Math.max(...v)
  const span = max - min || 1
  const pad = 4
  return v.map((y, i) => {
    const x = (i / (v.length - 1)) * W
    const yy = H - pad - ((y - min) / span) * (H - pad * 2)
    return `${x.toFixed(2)},${yy.toFixed(2)}`
  }).join(' ')
})
const areaPath = computed(() => {
  const v = vals.value
  if (v.length < 2) return ''
  return `M0,${H} L${polyline.value.split(' ').join(' L')} L${W},${H} Z`
})
const lastX = computed(() => W)
const lastY = computed(() => {
  const v = vals.value
  if (v.length < 2) return H / 2
  const min = Math.min(...v)
  const max = Math.max(...v)
  const span = max - min || 1
  const pad = 4
  return H - pad - ((v[v.length - 1] - min) / span) * (H - pad * 2)
})
const trendText = computed(() => trendIcon(trend.value?.direction) + (trend.value?.direction || '') +
  (trend.value?.slopePerHour != null ? ` ${trend.value.slopePerHour > 0 ? '+' : ''}${trend.value.slopePerHour}/h` : ''))
// 遥测涨跌不作价值判断（非金融口径），统一中性色 + 方向箭头，避免红绿误读
const trendClass = computed(() => {
  const d = trend.value?.direction
  return (d === '上升' || d === '下降') ? 'dir' : 'flat'
})
function trendIcon(d) {
  if (d === '上升') return '↗ '
  if (d === '下降') return '↘ '
  if (d === '平稳') return '→ '
  return ''
}

async function load() {
  const a = props.args || {}
  if (!a.deviceId) return
  deviceId.value = a.deviceId
  metric.value = a.metric || ''
  hours.value = Number(a.hours || 24)
  try {
    const res = await getTelemetrySeries(
      a.deviceId, a.metric || '', Number(a.hours || 24), a.source || '',
    )
    const data = res?.data
    if (!data || !data.stats) return
    points.value = data.points || []
    stats.value = data.stats
    trend.value = data.trend
  } catch (e) {
    // 开关未开 / 无权限 / 无数据 → 静默不渲染
    points.value = []
  }
}

onMounted(load)
watch(() => props.args, load, { deep: true })
</script>

<style scoped>
.spark { margin-top: 4px; padding: 4px 6px; background: var(--surface); border: 1px dashed var(--border); border-radius: 6px; }
.spark-head { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: var(--text-muted); }
.spark-title { font-weight: 600; color: var(--text-soft); }
.spark-stat b.dir { color: var(--primary); }
.spark-stat b.flat { color: var(--text-muted); }
.spark-svg { width: 100%; height: 48px; display: block; margin-top: 2px; }
/* SVG 走 CSS 类着色（presentation attribute 不支持 var()） */
.spark-area { fill: var(--primary); opacity: 0.12; }
.spark-line { fill: none; stroke: var(--primary); stroke-width: 1.6; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.spark-dot { fill: var(--primary); }
</style>
