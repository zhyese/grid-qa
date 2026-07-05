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
      <button class="fs-btn" @click="toggleFullscreen" :title="isFs ? '退出全屏(Esc)' : '全屏'">{{ isFs ? '⤫' : '⛶' }}</button>
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
import * as THREE from 'three'

const entity = ref('')
const graph = ref(null)
const loading = ref(false)
const selected = ref(null)
const is3D = ref(true)

const containerRef = ref(null)
const canvasRef = ref(null)
let animId = null
let dragCleanup = null
let currentRenderer = null
let currentCamera = null
const isFs = ref(false)
function toggleFullscreen() {
  const el = containerRef.value
  if (!el) return
  if (document.fullscreenElement) document.exitFullscreen()
  else el.requestFullscreen()
}
function onFsChange() {
  isFs.value = !!document.fullscreenElement
  const w = containerRef.value?.clientWidth || window.innerWidth
  const h = containerRef.value?.clientHeight || window.innerHeight
  if (currentRenderer) { currentRenderer.setSize(w, h); currentCamera.aspect = w / h; currentCamera.updateProjectionMatrix() }
}

async function loadGraph() {
  loading.value = true
  try {
    const r = await getKgGraph(entity.value, 200)
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

  try {
      const scene = new THREE.Scene()
      scene.background = new THREE.Color(0x1a1a2e)
      const camera = new THREE.PerspectiveCamera(60, W / H, 0.1, 1000)
      currentCamera = camera
      camera.position.set(25, 22, 25)
      const renderer = new THREE.WebGLRenderer({ canvas: canvasRef.value, antialias: true })
      currentRenderer = renderer
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
        x: (Math.random() - 0.5) * 12,
        y: (Math.random() - 0.5) * 12,
        z: (Math.random() - 0.5) * 12,
        vx: 0, vy: 0, vz: 0,
      }))
      // 拖动交互状态 + 渲染同步（simulate 与拖动共用，避免位置/连线不一致）
      const raycaster = new THREE.Raycaster()
      const ndcMouse = new THREE.Vector2()
      const dragPlane = new THREE.Plane()
      const dragHit = new THREE.Vector3()
      let dragIdx = -1
      const syncRender = () => {
        for (let i = 0; i < N; i++) {
          spheres[i]?.position.set(positions[i].x, positions[i].y, positions[i].z)
          labels[i]?.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        }
        while (lineGroup.children.length) lineGroup.remove(lineGroup.children[0])
        for (const [si, ti] of linkPairs) {
          const pts = [new THREE.Vector3(positions[si].x, positions[si].y, positions[si].z),
                       new THREE.Vector3(positions[ti].x, positions[ti].y, positions[ti].z)]
          lineGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat))
        }
      }

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

      // 标签 Sprite（每个标签独立 canvas，避免共用 ctx 导致 CanvasTexture 串台成同一文字）
      const labels = []
      for (let i = 0; i < Math.min(N, 50); i++) {
        const label = (nodes[i]?.name || nodes[i]?.label || '')?.slice(0, 8)
        if (!label) continue
        const lc = document.createElement('canvas')
        lc.width = 128; lc.height = 64
        const ctx = lc.getContext('2d')
        ctx.fillStyle = '#ffffff'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center'
        ctx.fillText(label, 64, 36)
        const tex = new THREE.CanvasTexture(lc)
        const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.8 })
        const sprite = new THREE.Sprite(mat)
        sprite.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        sprite.scale.set(3, 1.5, 1)
        scene.add(sprite)
        labels.push(sprite)
      }

      // 简单力导向模拟
      let iter = 0
      const L0 = 3, REPULSE = 18, SPRING_K = 0.06, GRAVITY = 0.012, BOUND = N > 120 ? 24 : 14
      function simulate() {
        if (iter++ > 120) return
        const damp = Math.max(0.35, 0.88 - iter * 0.004)   // 降温收敛
        for (let i = 0; i < N; i++) {
          if (i === dragIdx) continue
          let fx = 0, fy = 0, fz = 0
          // 电荷斥力（带 cutoff：距离>12 忽略，省算力 + 防节点四散）
          for (let j = 0; j < N; j++) {
            if (i === j) continue
            const dx = positions[i].x - positions[j].x
            const dy = positions[i].y - positions[j].y
            const dz = positions[i].z - positions[j].z
            const d2 = dx * dx + dy * dy + dz * dz
            if (d2 > 144) continue
            const d = Math.sqrt(d2) + 0.1
            const f = REPULSE / (d * d)
            fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
          }
          // 弹簧力（带平衡长度 L0：边长趋近 L0=4，而非越拉越长）
          for (const [si, ti] of linkPairs) {
            if (si === i || ti === i) {
              const j = si === i ? ti : si
              const dx = positions[j].x - positions[i].x
              const dy = positions[j].y - positions[i].y
              const dz = positions[j].z - positions[i].z
              const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.1
              const f = (d - L0) * SPRING_K
              fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
            }
          }
          // 向心力（拉回原点，防飞出视野）
          fx -= positions[i].x * GRAVITY
          fy -= positions[i].y * GRAVITY
          fz -= positions[i].z * GRAVITY
          positions[i].vx = (positions[i].vx + fx) * damp
          positions[i].vy = (positions[i].vy + fy) * damp
          positions[i].vz = (positions[i].vz + fz) * damp
          // 位置边界约束（防飞出相机视野）
          positions[i].x = Math.max(-BOUND, Math.min(BOUND, positions[i].x + positions[i].vx))
          positions[i].y = Math.max(-BOUND, Math.min(BOUND, positions[i].y + positions[i].vy))
          positions[i].z = Math.max(-BOUND, Math.min(BOUND, positions[i].z + positions[i].vz))
        }
        syncRender()
        if (iter < 100) setTimeout(simulate, 30)
      }
      simulate()

      // 节点拖动（raycaster 命中 → 投影平面跟随鼠标 → syncRender 实时刷新）
      const dom = renderer.domElement
      const setMouse = (e) => {
        const r = dom.getBoundingClientRect()
        ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
        ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
      }
      const onDown = (e) => {
        setMouse(e)
        raycaster.setFromCamera(ndcMouse, camera)
        const hits = raycaster.intersectObjects(spheres)
        if (hits.length) {
          dragIdx = spheres.indexOf(hits[0].object)
          const camDir = new THREE.Vector3()
          camera.getWorldDirection(camDir)
          dragPlane.setFromNormalAndCoplanarPoint(camDir, hits[0].point)
          dom.style.cursor = 'grabbing'
          e.preventDefault()
        }
      }
      const onMove = (e) => {
        setMouse(e)
        if (dragIdx >= 0) {
          raycaster.setFromCamera(ndcMouse, camera)
          if (raycaster.ray.intersectPlane(dragPlane, dragHit)) {
            positions[dragIdx].x = Math.max(-BOUND, Math.min(BOUND, dragHit.x))
            positions[dragIdx].y = Math.max(-BOUND, Math.min(BOUND, dragHit.y))
            positions[dragIdx].z = Math.max(-BOUND, Math.min(BOUND, dragHit.z))
            positions[dragIdx].vx = 0; positions[dragIdx].vy = 0; positions[dragIdx].vz = 0
            syncRender()
          }
        } else {
          raycaster.setFromCamera(ndcMouse, camera)
          dom.style.cursor = raycaster.intersectObjects(spheres).length ? 'grab' : 'default'
        }
      }
      const onUp = () => { dragIdx = -1; dom.style.cursor = 'default' }
      dom.addEventListener('pointerdown', onDown)
      dom.addEventListener('pointermove', onMove)
      dom.addEventListener('pointerup', onUp)
      dragCleanup = () => { dom.removeEventListener('pointerdown', onDown); dom.removeEventListener('pointermove', onMove); dom.removeEventListener('pointerup', onUp) }

      // 旋转动画：先左右逆时针 10 圈，再上下 10 圈，循环；拖动时暂停
      let phase = 1, phaseAngle = 0
      const RADIUS = 30, Y0 = 22, SPIN_SPEED = 0.0025
      const TEN_LOOPS = Math.PI * 20
      function animate() {
        if (dragIdx < 0) {
          phaseAngle += SPIN_SPEED
          if (phaseAngle >= TEN_LOOPS) { phaseAngle = 0; phase = phase === 1 ? 2 : 1 }
          if (phase === 1) {
            camera.position.set(RADIUS * Math.cos(phaseAngle), Y0, RADIUS * Math.sin(phaseAngle))
          } else {
            camera.position.set(0, RADIUS * Math.sin(phaseAngle), RADIUS * Math.cos(phaseAngle))
          }
        }
        camera.lookAt(0, 0, 0)
        renderer.render(scene, camera)
        animId = requestAnimationFrame(animate)
      }
      animate()
  } catch (e) { console.error('3D render error:', e) }
}

function toggle3D() { is3D.value = !is3D.value; if (is3D.value) { nextTick(() => loadGraph()) } }

function cleanup() {
  if (animId) { cancelAnimationFrame(animId); animId = null }
  if (dragCleanup) { dragCleanup(); dragCleanup = null }
}

onMounted(() => { loadGraph(); document.addEventListener('fullscreenchange', onFsChange) })
onUnmounted(() => { cleanup(); document.removeEventListener('fullscreenchange', onFsChange) })
</script>

<style scoped>
.graph-container { position: relative; background: var(--surface-2); border-radius: var(--radius); min-height: 500px; overflow: hidden; }
.graph-container canvas { display: block; width: 100%; height: 100%; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 14px; }
.graph-info { position: absolute; top: 50px; right: 10px; display: flex; gap: 6px; }
.fs-btn { position: absolute; top: 10px; right: 10px; z-index: 10; width: 34px; height: 34px; border-radius: var(--radius-sm); background: rgba(0,0,0,.55); color: #fff; border: 1px solid rgba(255,255,255,.25); cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }
.fs-btn:hover { background: rgba(0,0,0,.75); }
</style>