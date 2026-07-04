<template>
  <div>
    <div class="card" style="margin-bottom:12px">
      <div class="row" style="gap:8px">
        <input class="input" v-model="entity" placeholder="搜索设备/故障（空=全量图谱）" @keyup.enter="loadGraph" style="flex:1" />
        <button class="btn btn-primary" @click="loadGraph" :disabled="loading">{{ loading ? '加载中…' : '🔍 搜索' }}</button>
        <button class="btn btn-ghost" @click="toggle3D">{{ is3D ? '切换2D' : '切换3D' }}</button>
      </div>
    </div>

    <div class="graph-container" ref="containerRef">
      <canvas ref="canvasRef"></canvas>
      <div v-if="!graph" class="graph-hint">搜索设备或点击「搜索」加载知识图谱</div>
      <div class="graph-info" v-if="graph">
        <span class="badge badge-neutral">{{ graph.nodes?.length || 0 }} 节点</span>
        <span class="badge badge-neutral">{{ graph.links?.length || 0 }} 关系</span>
      </div>
    </div>

    <div class="card" v-if="selected">
      <div class="src-head">节点详情</div>
      <div class="cause"><b>{{ selected.label || selected.name }}</b><div class="cause-line">类型：{{ selected.type }} · 出度 {{ selected.outDegree || 0 }}</div></div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { getKgGraph } from '../api'

const entity = ref('')
const graph = ref(null)
const loading = ref(false)
const selected = ref(null)
const is3D = ref(true)

const containerRef = ref(null)
const canvasRef = ref(null)
let animId = null

async function loadGraph() {
  loading.value = true
  try {
    const r = await getKgGraph(entity.value, 500)
    graph.value = r.data || null
    await nextTick()
    if (graph.value && is3D.value) render3D()
  } catch { /* silent */ } finally { loading.value = false }
}

// 简易 3D 力导向图（基于 Three.js）
function render3D() {
  if (!canvasRef.value || !graph.value) return
  const container = containerRef.value
  const W = container.clientWidth || 600
  const H = container.clientHeight || 500

  // 使用 Three.js 从 CDN 动态加载
  const script = document.createElement('script')
  script.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'
  script.onload = () => {
    try {
      const THREE = window.THREE
      if (!THREE) return

      const scene = new THREE.Scene()
      scene.background = new THREE.Color(0x1a1a2e)
      const camera = new THREE.PerspectiveCamera(60, W / H, 0.1, 1000)
      camera.position.set(15, 10, 15)
      const renderer = new THREE.WebGLRenderer({ canvas: canvasRef.value, antialias: true })
      renderer.setSize(W, H)
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))

      // 光源
      const ambient = new THREE.AmbientLight(0x404060)
      scene.add(ambient)
      const dir = new THREE.DirectionalLight(0xffffff, 0.8)
      dir.position.set(10, 20, 10)
      scene.add(dir)

      // 布局
      const nodes = graph.value.nodes || []
      const links = graph.value.links || []
      const N = nodes.length
      const positions = new Array(N).fill(0).map(() => ({
        x: (Math.random() - 0.5) * 20,
        y: (Math.random() - 0.5) * 20,
        z: (Math.random() - 0.5) * 20,
        vx: 0, vy: 0, vz: 0,
      }))

      // 节点颜色按类型
      const typeColors = {
        Equipment: 0x3498db, Fault: 0xe74c3c, Action: 0x2ecc71,
        default: 0x95a5a6,
      }
      const spheres = []
      for (let i = 0; i < N; i++) {
        const color = typeColors[nodes[i]?.type] || typeColors.default
        const size = Math.min(1.0, Math.max(0.3, (nodes[i]?.outDegree || 1) * 0.15))
        const geo = new THREE.SphereGeometry(size, 16, 16)
        const mat = new THREE.MeshPhongMaterial({ color, emissive: color, emissiveIntensity: 0.3 })
        const mesh = new THREE.Mesh(geo, mat)
        mesh.position.set(positions[i].x, positions[i].y, positions[i].z)
        scene.add(mesh)
        spheres.push(mesh)
      }

      // 连线
      const lineMat = new THREE.LineBasicMaterial({ color: 0x555577, transparent: true, opacity: 0.4 })
      const lineGroup = new THREE.Group()
      const linkPairs = []
      for (const l of links || []) {
        const si = nodes.findIndex(n => n.id === l.source || n.id === l.source?.id)
        const ti = nodes.findIndex(n => n.id === l.target || n.id === l.target?.id)
        if (si >= 0 && ti >= 0) {
          linkPairs.push([si, ti])
          const pts = [new THREE.Vector3(positions[si].x, positions[si].y, positions[si].z),
                       new THREE.Vector3(positions[ti].x, positions[ti].y, positions[ti].z)]
          const geo = new THREE.BufferGeometry().setFromPoints(pts)
          const line = new THREE.Line(geo, lineMat)
          lineGroup.add(line)
        }
      }
      scene.add(lineGroup)

      // 标签 Sprite
      const labels = []
      const ctx = document.createElement('canvas').getContext('2d')
      for (let i = 0; i < Math.min(N, 50); i++) {
        const label = (nodes[i]?.name || nodes[i]?.label || '')?.slice(0, 8)
        if (!label) continue
        ctx.canvas.width = 128; ctx.canvas.height = 64
        ctx.clearRect(0, 0, 128, 64)
        ctx.fillStyle = '#ffffff'; ctx.font = '20px sans-serif'; ctx.textAlign = 'center'
        ctx.fillText(label, 64, 36)
        const tex = new THREE.CanvasTexture(ctx.canvas)
        const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.8 })
        const sprite = new THREE.Sprite(mat)
        sprite.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        sprite.scale.set(3, 1.5, 1)
        scene.add(sprite)
        labels.push(sprite)
      }

      // 简单力导向模拟
      let iter = 0
      function simulate() {
        if (iter++ > 100) return
        for (let i = 0; i < N; i++) {
          let fx = 0, fy = 0, fz = 0
          // 电荷斥力
          for (let j = 0; j < N; j++) {
            if (i === j) continue
            const dx = positions[i].x - positions[j].x
            const dy = positions[i].y - positions[j].y
            const dz = positions[i].z - positions[j].z
            const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.1
            const f = 6 / (d * d)
            fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
          }
          // 弹簧引力
          for (const [si, ti] of linkPairs) {
            if (si === i || ti === i) {
              const j = si === i ? ti : si
              const dx = positions[j].x - positions[i].x
              const dy = positions[j].y - positions[i].y
              const dz = positions[j].z - positions[i].z
              const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.1
              const f = d * 0.03
              fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
            }
          }
          positions[i].vx = (positions[i].vx + fx) * 0.85
          positions[i].vy = (positions[i].vy + fy) * 0.85
          positions[i].vz = (positions[i].vz + fz) * 0.85
          positions[i].x += positions[i].vx
          positions[i].y += positions[i].vy
          positions[i].z += positions[i].vz
        }
        // 更新位置
        for (let i = 0; i < N; i++) {
          spheres[i]?.position.set(positions[i].x, positions[i].y, positions[i].z)
          labels[i]?.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        }
        // 更新连线
        while (lineGroup.children.length) lineGroup.remove(lineGroup.children[0])
        for (const [si, ti] of linkPairs) {
          const pts = [new THREE.Vector3(positions[si].x, positions[si].y, positions[si].z),
                       new THREE.Vector3(positions[ti].x, positions[ti].y, positions[ti].z)]
          const geo = new THREE.BufferGeometry().setFromPoints(pts)
          const line = new THREE.Line(geo, lineMat)
          lineGroup.add(line)
        }
        if (iter < 100) setTimeout(simulate, 30)
      }
      simulate()

      // 旋转动画
      let angle = 0
      function animate() {
        angle += 0.003
        camera.position.x = 15 * Math.cos(angle)
        camera.position.z = 15 * Math.sin(angle)
        camera.lookAt(0, 0, 0)
        renderer.render(scene, camera)
        animId = requestAnimationFrame(animate)
      }
      animate()
    } catch (e) { console.error('3D render error:', e) }
  }
  document.head.appendChild(script)
}

function toggle3D() { is3D.value = !is3D.value; if (is3D.value) { nextTick(() => loadGraph()) } }

function cleanup() {
  if (animId) { cancelAnimationFrame(animId); animId = null }
}

onMounted(() => loadGraph())
onUnmounted(() => cleanup())
</script>

<style scoped>
.graph-container { position: relative; background: var(--surface-2); border-radius: var(--radius); min-height: 500px; overflow: hidden; }
.graph-container canvas { display: block; width: 100%; height: 100%; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 14px; }
.graph-info { position: absolute; top: 10px; right: 10px; display: flex; gap: 6px; }
</style>