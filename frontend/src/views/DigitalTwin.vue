<template>
  <div>
    <div class="card" style="margin-bottom:12px">
      <div class="row" style="gap:8px;align-items:center">
        <span class="badge badge-neutral">{{ overview.deviceCount || 0 }} 设备</span>
        <span class="badge badge-danger" v-if="overview.highRiskCount">{{ overview.highRiskCount }} 高风险</span>
        <span class="muted" style="font-size:12px">{{ overview.stationName || '加载中...' }}</span>
        <div style="flex:1"></div>
        <button class="btn btn-ghost btn-sm" @click="loadOverview">🔄 刷新状态</button>
        <label class="ws-toggle"><input type="checkbox" v-model="autoRotate" /> 自动旋转</label>
      </div>
    </div>

    <div class="twin-container" ref="containerRef">
      <canvas ref="canvasRef"></canvas>
      <button class="fs-btn" @click="toggleFullscreen" :title="isFs ? '退出全屏(Esc)' : '全屏'">{{ isFs ? '⤫' : '⛶' }}</button>
      <div v-if="!overview.devices?.length" class="graph-hint">加载3D场景中...</div>
      <div class="twin-legend" v-if="overview.devices?.length">
        <div class="legend-item"><span class="legend-dot" style="background:#2ECC71"></span>正常</div>
        <div class="legend-item"><span class="legend-dot" style="background:hsl(60,80%,50%)"></span>中风险</div>
        <div class="legend-item"><span class="legend-dot" style="background:hsl(0,90%,50%)"></span>高风险</div>
      </div>
    </div>

    <!-- 设备详情侧栏 -->
    <div class="card" v-if="selectedDevice" style="margin-top:12px">
      <div class="card-header">
        <h3 class="card-title">📦 {{ selectedDevice.name || selectedDevice.deviceId }}</h3>
        <button class="btn btn-ghost btn-sm" @click="selectedDevice = null; clearHighlight()">✕ 关闭</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <div class="src-head">设备状态</div>
          <div class="cause" style="justify-content:space-between"><span>风险等级</span><span class="badge" :class="riskBadge(selectedDevice.riskLevel)">{{ selectedDevice.riskLevel || '低' }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>风险评分</span><span>{{ selectedDevice.riskScore || 0 }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>设备类型</span><span>{{ selectedDevice.type || '-' }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>所属区域</span><span>{{ selectedDevice.area || '-' }}</span></div>
          <div v-if="selectedDevice.suggestion" class="cause" style="flex-direction:column;align-items:flex-start">
            <span class="muted" style="font-size:12px;margin-bottom:4px">建议</span>
            <span>{{ selectedDevice.suggestion }}</span>
          </div>
        </div>
        <div>
          <div class="src-head">故障传播链</div>
          <div v-if="selectedDevice.faultChain?.length" style="max-height:200px;overflow-y:auto">
            <div v-for="(chain, i) in selectedDevice.faultChain" :key="i" class="cause" style="flex-direction:column;align-items:flex-start;padding:4px 0">
              <span class="muted" style="font-size:12px">{{ chain.hops }} 跳</span>
              <span>{{ (chain.chain || []).join(' → ') }}</span>
            </div>
          </div>
          <div v-else class="empty" style="padding:8px">暂无传播链数据</div>
          <div class="src-head" style="margin-top:8px">知识图谱上下文</div>
          <div v-if="selectedDevice.kgContext?.length" style="max-height:120px;overflow-y:auto;font-size:13px">
            <div v-for="(ctx, i) in selectedDevice.kgContext" :key="i" class="muted" style="padding:2px 0">{{ ctx }}</div>
          </div>
          <div v-else class="empty" style="padding:8px">暂无图谱数据</div>
        </div>
      </div>
      <div v-if="selectedDevice.alerts?.length" style="margin-top:8px">
        <div class="src-head">关联告警</div>
        <div v-for="(a, i) in selectedDevice.alerts" :key="i" class="cause" style="justify-content:space-between">
          <span><span class="badge" :class="sevBadge(a.severity)">{{ a.severity }}</span> {{ a.title }}</span>
          <span class="muted">{{ a.status }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { getStationOverview, getDeviceDetail } from '../api'
import { useAuthStore } from '../stores/auth'
import * as THREE from 'three'

const auth = useAuthStore()
const overview = ref({ devices: [], stationName: '' })
const selectedDevice = ref(null)
const autoRotate = ref(true)

const containerRef = ref(null)
const canvasRef = ref(null)
const isFs = ref(false)
let animId = null
let scene = null
let camera = null
let renderer = null
let raycaster = null
let ndcMouse = null
let deviceMeshes = []  // {mesh, deviceId, name}
let highlightMeshes = []
let blinkDevices = new Set()
let twinWs = null

function toggleFullscreen() {
  const el = containerRef.value
  if (!el) return
  if (document.fullscreenElement) document.exitFullscreen()
  else el.requestFullscreen()
}

function onFsChange() {
  isFs.value = !!document.fullscreenElement
  resize()
}

function resize() {
  const w = containerRef.value?.clientWidth || window.innerWidth
  const h = containerRef.value?.clientHeight || 600
  if (renderer) { renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix() }
}

async function loadOverview() {
  try {
    const r = await getStationOverview('110kV-demo')
    overview.value = r.data || { devices: [] }
    await nextTick()
    render3D()
  } catch (e) { console.error('load overview error:', e) }
}

function parseColor(colorStr) {
  // 解析 hsl(r,g%,l%) 或 #RRGGBB
  if (!colorStr) return 0x3498DB
  if (colorStr.startsWith('#')) return parseInt(colorStr.slice(1), 16)
  const m = colorStr.match(/hsl\((\d+),\s*(\d+)%,\s*(\d+)%\)/)
  if (m) {
    const h = parseInt(m[1]) / 360
    const s = parseInt(m[2]) / 100
    const l = parseInt(m[3]) / 100
    return new THREE.Color().setHSL(h, s, l).getHex()
  }
  return 0x3498DB
}

function render3D() {
  if (!canvasRef.value || !overview.value.devices?.length) return
  const container = containerRef.value
  const W = container.clientWidth || 800
  const H = container.clientHeight || 600

  // 清理旧场景
  if (animId) { cancelAnimationFrame(animId); animId = null }
  if (renderer) { renderer.dispose(); renderer = null }

  try {
    scene = new THREE.Scene()
    scene.background = new THREE.Color(0x1a1a2e)
    camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 500)
    camera.position.set(25, 20, 30)
    camera.lookAt(0, 0, 0)

    renderer = new THREE.WebGLRenderer({ canvas: canvasRef.value, antialias: true })
    renderer.setSize(W, H)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))

    // 光源
    const ambient = new THREE.AmbientLight(0x404060)
    scene.add(ambient)
    const dir = new THREE.DirectionalLight(0xffffff, 0.8)
    dir.position.set(10, 20, 10)
    scene.add(dir)
    const dir2 = new THREE.DirectionalLight(0xffffff, 0.3)
    dir2.position.set(-10, 15, -10)
    scene.add(dir2)

    // 地面网格
    const grid = new THREE.GridHelper(50, 50, 0x333355, 0x222244)
    grid.position.y = 0
    scene.add(grid)

    // 区域指示（半透明平面）
    for (const area of overview.value.areas || []) {
      const pos = area.position || [0, 0, 0]
      const sz = area.size || [4, 0, 4]
      const geo = new THREE.PlaneGeometry(sz[0], sz[2])
      const mat = new THREE.MeshBasicMaterial({ color: 0x2a2a4a, transparent: true, opacity: 0.3, side: THREE.DoubleSide })
      const mesh = new THREE.Mesh(geo, mat)
      mesh.rotation.x = -Math.PI / 2
      mesh.position.set(pos[0], 0.01, pos[2])
      scene.add(mesh)
      // 区域标签
      addLabel(area.name || area.id, pos[0], 0.5, pos[2] + sz[2] / 2, 0x888899)
    }

    // 设备渲染
    deviceMeshes = []
    blinkDevices.clear()
    const devices = overview.value.devices || []
    for (const dev of devices) {
      const pos = dev.position || [0, 0, 0]
      const sz = dev.size || [1, 1, 1]
      const colorHex = parseColor(dev.color)
      const geo = new THREE.BoxGeometry(sz[0], sz[1], sz[2])
      const mat = new THREE.MeshPhongMaterial({ color: colorHex, emissive: colorHex, emissiveIntensity: dev.blink ? 0.5 : 0.2 })
      const mesh = new THREE.Mesh(geo, mat)
      mesh.position.set(pos[0], pos[1], pos[2])
      mesh.userData = { deviceId: dev.deviceId, name: dev.name, originalColor: colorHex }
      scene.add(mesh)
      deviceMeshes.push(mesh)

      // 标签
      addLabel(dev.name || dev.deviceId, pos[0], pos[1] + sz[1] / 2 + 0.8, pos[2], dev.blink ? 0xff6644 : 0xffffff)

      if (dev.blink) blinkDevices.add(dev.deviceId)

      // 连接线
      if (dev.connections) {
        for (const connId of dev.connections) {
          const target = devices.find(d => d.deviceId === connId)
          if (target) {
            const tp = target.position || [0, 0, 0]
            const pts = [
              new THREE.Vector3(pos[0], pos[1], pos[2]),
              new THREE.Vector3(tp[0], tp[1], tp[2]),
            ]
            const lineGeo = new THREE.BufferGeometry().setFromPoints(pts)
            const lineMat = new THREE.LineBasicMaterial({ color: 0x444466, transparent: true, opacity: 0.3 })
            scene.add(new THREE.Line(lineGeo, lineMat))
          }
        }
      }
    }

    // raycaster
    raycaster = new THREE.Raycaster()
    ndcMouse = new THREE.Vector2()

    // 点击设备
    const dom = renderer.domElement
    const onClick = (e) => {
      const r = dom.getBoundingClientRect()
      ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
      ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
      raycaster.setFromCamera(ndcMouse, camera)
      const hits = raycaster.intersectObjects(deviceMeshes)
      if (hits.length) {
        const mesh = hits[0].object
        selectDevice(mesh.userData.deviceId)
      }
    }
    dom.addEventListener('click', onClick)

    // 鼠标悬停高亮
    const onMove = (e) => {
      const r = dom.getBoundingClientRect()
      ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
      ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
      raycaster.setFromCamera(ndcMouse, camera)
      const hits = raycaster.intersectObjects(deviceMeshes)
      dom.style.cursor = hits.length ? 'pointer' : 'default'
    }
    dom.addEventListener('pointermove', onMove)

    // 旋转动画 + 闪烁
    let angle = 0
    function animate() {
      if (autoRotate.value) {
        angle += 0.002
        camera.position.set(25 * Math.cos(angle), 20, 30 * Math.sin(angle))
        camera.lookAt(0, 0, 0)
      }
      // 闪烁设备
      const t = Date.now() * 0.005
      for (const mesh of deviceMeshes) {
        if (blinkDevices.has(mesh.userData.deviceId)) {
          mesh.material.emissiveIntensity = 0.3 + 0.4 * Math.abs(Math.sin(t))
        }
      }
      // 高亮传播链
      for (const m of highlightMeshes) {
        m.material.emissiveIntensity = 0.4 + 0.3 * Math.abs(Math.sin(t * 1.5))
      }
      renderer.render(scene, camera)
      animId = requestAnimationFrame(animate)
    }
    animate()
  } catch (e) { console.error('3D render error:', e) }
}

function addLabel(text, x, y, z, color = 0xffffff) {
  if (!scene || !text) return
  const canvas = document.createElement('canvas')
  canvas.width = 128
  canvas.height = 48
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = `#${color.toString(16).padStart(6, '0')}`
  ctx.font = '14px sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText(text.slice(0, 10), 64, 30)
  const tex = new THREE.CanvasTexture(canvas)
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.85 })
  const sprite = new THREE.Sprite(mat)
  sprite.position.set(x, y, z)
  sprite.scale.set(3, 1.2, 1)
  scene.add(sprite)
}

async function selectDevice(deviceId) {
  try {
    const r = await getDeviceDetail(deviceId)
    selectedDevice.value = r.data
    // 高亮传播链路径
    highlightFaultChain(deviceId)
  } catch (e) { console.error('select device error:', e) }
}

function highlightFaultChain(deviceId) {
  clearHighlight()
  if (!selectedDevice.value?.faultChain?.length) return
  const chainEntities = new Set()
  for (const chain of selectedDevice.value.faultChain) {
    for (const node of chain.chain || []) {
      chainEntities.add(node)
    }
  }
  // 高亮匹配的设备
  for (const mesh of deviceMeshes) {
    const dev = overview.value.devices?.find(d => d.deviceId === mesh.userData.deviceId)
    if (dev && chainEntities.has(dev.kgEntity)) {
      mesh.material.emissive.setHex(0xff8800)
      highlightMeshes.push(mesh)
    }
  }
}

function clearHighlight() {
  for (const mesh of highlightMeshes) {
    mesh.material.emissive.setHex(mesh.userData.originalColor)
    mesh.material.emissiveIntensity = 0.2
  }
  highlightMeshes = []
}

function riskBadge(level) {
  return { '高': 'badge-danger', '中': 'badge-warning', '低': 'badge-success' }[level] || 'badge-neutral'
}

function sevBadge(sev) {
  return { critical: 'badge-danger', error: 'badge-danger', warning: 'badge-warning', info: 'badge-info' }[sev?.toLowerCase()] || 'badge-neutral'
}

// WebSocket 告警订阅
function connectTwinWs() {
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    twinWs = new WebSocket(`${proto}://${location.host}/api/twin/ws/twin?token=${encodeURIComponent(auth.token || '')}`)
    twinWs.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'alert') {
          // 相机飞到设备坐标
          if (msg.position && camera) {
            const [x, y, z] = msg.position
            // 平滑飞到设备附近
            const targetPos = new THREE.Vector3(x + 5, y + 5, z + 5)
            const startPos = camera.position.clone()
            let t = 0
            const flyTo = () => {
              t += 0.02
              if (t >= 1) return
              camera.position.lerpVectors(startPos, targetPos, t)
              camera.lookAt(x, y, z)
              requestAnimationFrame(flyTo)
            }
            flyTo()
          }
          // 设备闪烁
          if (msg.deviceId) {
            blinkDevices.add(msg.deviceId)
            const mesh = deviceMeshes.find(m => m.userData.deviceId === msg.deviceId)
            if (mesh) {
              mesh.material.emissive.setHex(0xff0000)
              blinkDevices.add(msg.deviceId)
            }
          }
        }
      } catch (err) { /* skip */ }
    }
    twinWs.onerror = () => { try { twinWs.close() } catch (x) {} }
  } catch (e) { /* WebSocket 不可用时静默 */ }
}

onMounted(() => {
  loadOverview()
  connectTwinWs()
  document.addEventListener('fullscreenchange', onFsChange)
  window.addEventListener('resize', resize)
})

onUnmounted(() => {
  if (animId) cancelAnimationFrame(animId)
  if (renderer) renderer.dispose()
  if (twinWs) { try { twinWs.close() } catch (e) {} }
  document.removeEventListener('fullscreenchange', onFsChange)
  window.removeEventListener('resize', resize)
})
</script>

<style scoped>
.twin-container { position: relative; background: var(--surface-2); border-radius: var(--radius); min-height: 600px; overflow: hidden; }
.twin-container canvas { display: block; width: 100%; height: 100%; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 14px; }
.fs-btn { position: absolute; top: 10px; right: 10px; z-index: 10; width: 34px; height: 34px; border-radius: var(--radius-sm); background: rgba(0,0,0,.55); color: #fff; border: 1px solid rgba(255,255,255,.25); cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }
.fs-btn:hover { background: rgba(0,0,0,.75); }
.twin-legend { position: absolute; bottom: 10px; left: 10px; display: flex; gap: 12px; background: rgba(0,0,0,.5); padding: 6px 10px; border-radius: 6px; }
.legend-item { display: flex; align-items: center; gap: 4px; color: #ddd; font-size: 12px; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.ws-toggle { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer; }
.ws-toggle input { width: auto; }
</style>
