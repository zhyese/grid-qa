<template>
  <div>
    <div class="card" style="margin-bottom:12px">
      <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
        <span class="badge badge-neutral">{{ overview.deviceCount || 0 }} 设备</span>
        <span class="badge badge-danger" v-if="overview.highRiskCount">{{ overview.highRiskCount }} 高风险</span>
        <span class="muted" style="font-size:12px">{{ overview.stationName || '加载中...' }}</span>
        <span class="muted" style="font-size:12px">· 智慧园区模式</span>
        <div style="flex:1"></div>
        <label class="ws-toggle"><input type="checkbox" v-model="autoRotate" /> 自动旋转</label>
        <label class="ws-toggle"><input type="checkbox" v-model="showLabels" /> 标签</label>
        <label class="ws-toggle"><input type="checkbox" v-model="showWires" /> 连线</label>
        <button class="btn btn-ghost btn-sm" @click="loadOverview">🔄 刷新</button>
        <button class="btn btn-ghost btn-sm" @click="resetView">🎯 复位</button>
        <button class="btn btn-ghost btn-sm" @click="viewStation">⚡ 看变电站</button>
        <button class="btn btn-ghost btn-sm" @click="viewCity">🏙️ 看园区</button>
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
      <div class="twin-info" v-if="overview.devices?.length">
        <div>🖱️ 拖动旋转 / 滚轮缩放 / 点击设备查看详情</div>
        <div>📐 视距 {{ cameraDistance.toFixed(1) }}m · 设备 {{ overview.deviceCount }} · 园区 25 栋建筑</div>
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
          <div class="cause" style="justify-content:space-between"><span>设备类型</span><span>{{ selectedDevice.typeLabel || selectedDevice.type || '-' }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>3D 模型</span><span class="muted" style="font-size:12px">{{ selectedDevice.model || '-' }}</span></div>
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
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { getStationOverview, getDeviceDetail } from '../api'
import { useAuthStore } from '../stores/auth'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import {
  buildDevice,
  buildAreaFloor,
  buildAreaLabel,
  buildDeviceLabel,
  buildConnectionLine,
} from '../three/deviceFactory'
import {
  buildSky,
  buildPMREMEnvironment,
  buildDistrict,
} from '../three/sceneEnvironment'

const auth = useAuthStore()
const overview = ref({ devices: [], stationName: '' })
const selectedDevice = ref(null)
const autoRotate = ref(true)
const showLabels = ref(true)
const showWires = ref(true)
const cameraDistance = ref(40)

const containerRef = ref(null)
const canvasRef = ref(null)
const isFs = ref(false)
let animId = null
let scene = null
let camera = null
let renderer = null
let raycaster = null
let ndcMouse = null
let envMap = null
let deviceMeshes = []
let labelSprites = []
let wireLines = []
let blinkDevices = new Set()
let twinWs = null
let envGroup = null
let isDragging = false
let lastMouse = { x: 0, y: 0 }

// 相机控制(azimuth/elevation/distance 三维轨道)
let cameraState = {
  azimuth: Math.PI * 0.7,
  elevation: Math.PI / 5,
  distance: 40,
  target: new THREE.Vector3(0, 2, 0),
}

function toggleFullscreen() {
  const el = containerRef.value
  if (!el) return
  if (document.fullscreenElement) document.exitFullscreen()
  else el.requestFullscreen()
}
function onFsChange() { isFs.value = !!document.fullscreenElement; resize() }
function resize() {
  const w = containerRef.value?.clientWidth || window.innerWidth
  const h = containerRef.value?.clientHeight || 600
  if (renderer) {
    renderer.setSize(w, h)
    camera.aspect = w / h
    camera.updateProjectionMatrix()
  }
}
async function loadOverview() {
  try {
    const r = await getStationOverview('110kV-demo')
    overview.value = r.data || { devices: [] }
    await nextTick()
    render3D()
  } catch (e) { console.error('load overview error:', e) }
}
function applyCamera() {
  const { azimuth, elevation, distance, target } = cameraState
  camera.position.set(
    target.x + distance * Math.cos(elevation) * Math.sin(azimuth),
    target.y + distance * Math.sin(elevation),
    target.z + distance * Math.cos(elevation) * Math.cos(azimuth)
  )
  camera.lookAt(target)
  cameraDistance.value = distance
}
function resetView() {
  cameraState = {
    azimuth: Math.PI * 0.7,
    elevation: Math.PI / 5,
    distance: 40,
    target: new THREE.Vector3(0, 2, 0),
  }
  applyCamera()
}
function viewStation() {
  cameraState = {
    azimuth: Math.PI * 0.75,
    elevation: Math.PI / 6,
    distance: 22,
    target: new THREE.Vector3(0, 2, 0),
  }
  applyCamera()
}
function viewCity() {
  cameraState = {
    azimuth: Math.PI * 0.7,
    elevation: Math.PI / 4.5,
    distance: 55,
    target: new THREE.Vector3(0, 5, 0),
  }
  applyCamera()
}
function parseColor(colorStr) {
  if (!colorStr) return 0x3498DB
  if (colorStr.startsWith('#')) return parseInt(colorStr.slice(1), 16)
  const m = colorStr.match(/hsl\((\d+),\s*(\d+)%,\s*(\d+)%\)/)
  if (m) {
    const h = parseInt(m[1]) / 360, s = parseInt(m[2]) / 100, l = parseInt(m[3]) / 100
    return new THREE.Color().setHSL(h, s, l).getHex()
  }
  return 0x3498DB
}

function render3D() {
  if (!canvasRef.value || !overview.value.devices?.length) return
  const container = containerRef.value
  const W = container.clientWidth || 1200
  const H = container.clientHeight || 600

  if (animId) { cancelAnimationFrame(animId); animId = null }
  if (renderer) { renderer.dispose(); renderer = null }
  deviceMeshes = []
  labelSprites = []
  wireLines = []
  blinkDevices.clear()

  try {
    scene = new THREE.Scene()
    // 雾化 — 模拟大气透视
    scene.fog = new THREE.Fog(0xC7D4DD, 60, 180)

    camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 500)
    applyCamera()

    renderer = new THREE.WebGLRenderer({ canvas: canvasRef.value, antialias: true, alpha: false })
    renderer.setSize(W, H)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.05
    renderer.outputEncoding = THREE.sRGBEncoding

    // PMREM 环境光(玻璃幕墙反射用)
    try {
      envMap = buildPMREMEnvironment(renderer)
    } catch (e) {
      console.warn('PMREM init failed:', e)
      envMap = null
    }

    // 光照 — 真实太阳光
    const ambient = new THREE.AmbientLight(0xB0C0D0, 0.18)
    scene.add(ambient)
    const hemi = new THREE.HemisphereLight(0x9FC0E8, 0x4A5040, 0.3)
    scene.add(hemi)
    // 主太阳光
    const sun = new THREE.DirectionalLight(0xFFF4E0, 1.7)
    sun.position.set(35, 50, 25)
    sun.castShadow = true
    sun.shadow.mapSize.set(2048, 2048)
    sun.shadow.camera.left = -45
    sun.shadow.camera.right = 45
    sun.shadow.camera.top = 45
    sun.shadow.camera.bottom = -45
    sun.shadow.camera.near = 1
    sun.shadow.camera.far = 120
    sun.shadow.bias = -0.0003
    sun.shadow.normalBias = 0.02
    scene.add(sun)
    // 副光(蓝色补光,模拟天空反射)
    const fill = new THREE.DirectionalLight(0x8AB0D0, 0.3)
    fill.position.set(-25, 20, -15)
    scene.add(fill)

    // 天空盒
    scene.add(buildSky())

    // 园区环境(地形/道路/建筑/树木/控制室)
    envGroup = buildDistrict(envMap)
    scene.add(envGroup)

    // 站内区域地面已由 buildDistrict 内部 platforms 提供，不再调用 buildAreaFloor 避免重叠 z-fight
    // 区域标签仍然渲染（标签和地面分开，无 z-fight 风险）
    for (const area of overview.value.areas || []) {
      if (showLabels.value) {
        const pos = area.position || [0, 0, 0]
        const sz = area.size || [4, 0, 4]
        const lbl = buildAreaLabel(area.name || area.id, pos[0], pos[2] - sz[2] / 2 - 0.8, '#5DADE2')
        scene.add(lbl)
        labelSprites.push(lbl)
      }
    }

    // 设备渲染(从 deviceFactory 取专属几何体)
    const devices = overview.value.devices || []
    for (const dev of devices) {
      const pos = dev.position || [0, 0, 0]
      const sz = dev.size || [1, 1, 1]
      const riskColor = parseColor(dev.color)
      const modelName = dev.model || 'default'
      const root = buildDevice(modelName, sz, riskColor)
      root.position.set(pos[0], pos[1], pos[2])
      root.userData = { deviceId: dev.deviceId, name: dev.name, originalColor: riskColor, riskColor }
      // 给设备材质应用 envMap
      if (envMap) {
        root.traverse(o => {
          if (o.isMesh && o.material) {
            const mats = Array.isArray(o.material) ? o.material : [o.material]
            for (const m of mats) {
              if (m.isMeshStandardMaterial || m.isMeshPhysicalMaterial) {
                m.envMap = envMap
                m.envMapIntensity = 0.6
                m.needsUpdate = true
              }
            }
          }
        })
      }
      scene.add(root)
      deviceMeshes.push(root)
      if (dev.blink) blinkDevices.add(dev.deviceId)
      // 设备标签
      if (showLabels.value) {
        const lblY = pos[1] + sz[1] + 0.6
        const lbl = buildDeviceLabel(dev.name || dev.deviceId, dev.icon || '📦', pos[0], lblY, pos[2], dev.blink)
        scene.add(lbl)
        labelSprites.push(lbl)
      }
    }

    // 连接线(弧形)
    if (showWires.value) {
      const drawn = new Set()
      for (const dev of devices) {
        if (!dev.connections) continue
        for (const connId of dev.connections) {
          const key = [dev.deviceId, connId].sort().join('|')
          if (drawn.has(key)) continue
          drawn.add(key)
          const target = devices.find(d => d.deviceId === connId)
          if (!target) continue
          const p1 = dev.position || [0, 0, 0]
          const p2 = target.position || [0, 0, 0]
          // 起止点抬高到设备顶部以上（避开母线/支柱绝缘子几何，避免线穿过设备"闪"）
          const line = buildConnectionLine(
            [p1[0], p1[1] + Math.max(p1[1] * 0.5, 0.5), p1[2]],
            [p2[0], p2[1] + Math.max(p2[1] * 0.5, 0.5), p2[2]]
          )
          scene.add(line)
          wireLines.push(line)
        }
      }
    }

    // 鼠标交互
    raycaster = new THREE.Raycaster()
    ndcMouse = new THREE.Vector2()
    const dom = renderer.domElement

    const onPointerDown = (e) => {
      isDragging = true
      lastMouse = { x: e.clientX, y: e.clientY }
    }
    const onPointerMove = (e) => {
      const r = dom.getBoundingClientRect()
      ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
      ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
      raycaster.setFromCamera(ndcMouse, camera)
      const hits = raycaster.intersectObjects(deviceMeshes, true)
      dom.style.cursor = hits.length ? 'pointer' : (isDragging ? 'grabbing' : 'grab')
      if (isDragging) {
        const dx = e.clientX - lastMouse.x
        const dy = e.clientY - lastMouse.y
        cameraState.azimuth -= dx * 0.005
        cameraState.elevation = Math.max(0.05, Math.min(Math.PI / 2 - 0.05, cameraState.elevation + dy * 0.005))
        lastMouse = { x: e.clientX, y: e.clientY }
        applyCamera()
      }
    }
    const onPointerUp = (e) => {
      if (isDragging) {
        const moved = Math.hypot(e.clientX - lastMouse.x, e.clientY - lastMouse.y)
        if (moved < 5) {
          const r = dom.getBoundingClientRect()
          ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
          ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
          raycaster.setFromCamera(ndcMouse, camera)
          const hits = raycaster.intersectObjects(deviceMeshes, true)
          if (hits.length) {
            let obj = hits[0].object
            while (obj && !obj.userData?.deviceId) obj = obj.parent
            if (obj) selectDevice(obj.userData.deviceId)
          }
        }
      }
      isDragging = false
    }
    const onWheel = (e) => {
      e.preventDefault()
      const delta = e.deltaY > 0 ? 1.1 : 0.9
      cameraState.distance = Math.max(8, Math.min(120, cameraState.distance * delta))
      applyCamera()
    }
    dom.addEventListener('pointerdown', onPointerDown)
    dom.addEventListener('pointermove', onPointerMove)
    dom.addEventListener('pointerup', onPointerUp)
    dom.addEventListener('pointerleave', onPointerUp)
    dom.addEventListener('wheel', onWheel, { passive: false })

    // 动画循环
    function animate() {
      if (autoRotate.value && !isDragging) {
        cameraState.azimuth += 0.0012
        applyCamera()
      }
      const t = performance.now() * 0.003
      for (const root of deviceMeshes) {
        const devId = root.userData.deviceId
        if (blinkDevices.has(devId)) {
          root.traverse(o => {
            if (o.isMesh && o.material?.emissiveIntensity !== undefined) {
              o.material.emissiveIntensity = 0.1 + 0.4 * Math.abs(Math.sin(t))
            }
          })
        }
      }
      renderer.render(scene, camera)
      animId = requestAnimationFrame(animate)
    }
    animate()
  } catch (e) { console.error('3D render error:', e) }
}

async function selectDevice(deviceId) {
  try {
    const r = await getDeviceDetail(deviceId)
    selectedDevice.value = r.data
    highlightFaultChain(deviceId)
    flyToDevice(deviceId)
  } catch (e) { console.error('select device error:', e) }
}
function flyToDevice(deviceId) {
  const dev = overview.value.devices?.find(d => d.deviceId === deviceId)
  if (!dev) return
  const pos = dev.position || [0, 0, 0]
  const target = new THREE.Vector3(pos[0], pos[1] + 1, pos[2])
  const startTarget = cameraState.target.clone()
  const startDist = cameraState.distance
  const endDist = 12
  let t = 0
  const fly = () => {
    t += 0.04
    if (t >= 1) return
    const ease = t * (2 - t)
    cameraState.target.lerpVectors(startTarget, target, ease)
    cameraState.distance = startDist + (endDist - startDist) * ease
    applyCamera()
    requestAnimationFrame(fly)
  }
  fly()
}
function highlightFaultChain(deviceId) {
  clearHighlight()
  if (!selectedDevice.value?.faultChain?.length) return
  const chainEntities = new Set()
  for (const chain of selectedDevice.value.faultChain) {
    for (const node of chain.chain || []) chainEntities.add(node)
  }
  for (const root of deviceMeshes) {
    const dev = overview.value.devices?.find(d => d.deviceId === root.userData.deviceId)
    if (dev && chainEntities.has(dev.kgEntity)) {
      root.traverse(o => {
        if (o.isMesh && o.material?.emissive) {
          if (!o.userData._origEmissive) o.userData._origEmissive = o.material.emissive.getHex()
          o.material.emissive.setHex(0xff8800)
          o.userData._highlighted = true
        }
      })
    }
  }
}
function clearHighlight() {
  for (const root of deviceMeshes) {
    root.traverse(o => {
      if (o.isMesh && o.userData._highlighted && o.userData._origEmissive !== undefined) {
        o.material.emissive.setHex(o.userData._origEmissive)
        o.material.emissiveIntensity = 0.05
        o.userData._highlighted = false
      }
    })
  }
}
function riskBadge(level) {
  return { '高': 'badge-danger', '中': 'badge-warning', '低': 'badge-success' }[level] || 'badge-neutral'
}
function sevBadge(sev) {
  return { critical: 'badge-danger', error: 'badge-danger', warning: 'badge-warning', info: 'badge-info' }[sev?.toLowerCase()] || 'badge-neutral'
}

watch(showLabels, (v) => {
  if (v) render3D()
  else { labelSprites.forEach(s => scene?.remove(s)); labelSprites = [] }
})
watch(showWires, (v) => {
  if (v) render3D()
  else { wireLines.forEach(l => scene?.remove(l)); wireLines = [] }
})

function connectTwinWs() {
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    twinWs = new WebSocket(`${proto}://${location.host}/api/twin/ws/twin?token=${encodeURIComponent(auth.token || '')}`)
    twinWs.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'alert' && msg.position) {
          flyToAlert(msg.position)
          if (msg.deviceId) blinkDevices.add(msg.deviceId)
        }
      } catch (err) { /* skip */ }
    }
    twinWs.onerror = () => { try { twinWs.close() } catch (x) {} }
  } catch (e) { /* skip */ }
}
function flyToAlert(position) {
  const [x, y, z] = position
  const target = new THREE.Vector3(x, y + 1, z)
  const startTarget = cameraState.target.clone()
  let t = 0
  const fly = () => {
    t += 0.04
    if (t >= 1) return
    const ease = t * (2 - t)
    cameraState.target.lerpVectors(startTarget, target, ease)
    applyCamera()
    requestAnimationFrame(fly)
  }
  fly()
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
  if (envMap) envMap.dispose()
  if (twinWs) { try { twinWs.close() } catch (e) {} }
  document.removeEventListener('fullscreenchange', onFsChange)
  window.removeEventListener('resize', resize)
})
</script>

<style scoped>
.twin-container { position: relative; background: var(--surface-2); border-radius: var(--radius); min-height: 640px; overflow: hidden; }
.twin-container canvas { display: block; width: 100%; height: 100%; cursor: grab; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 14px; }
.fs-btn { position: absolute; top: 10px; right: 10px; z-index: 10; width: 34px; height: 34px; border-radius: var(--radius-sm); background: rgba(0,0,0,.55); color: #fff; border: 1px solid rgba(255,255,255,.25); cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }
.fs-btn:hover { background: rgba(0,0,0,.75); }
.twin-legend { position: absolute; bottom: 10px; left: 10px; display: flex; gap: 12px; background: rgba(0,0,0,.55); padding: 6px 10px; border-radius: 6px; }
.legend-item { display: flex; align-items: center; gap: 4px; color: #ddd; font-size: 12px; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.ws-toggle { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer; }
.ws-toggle input { width: auto; }
.twin-info { position: absolute; bottom: 10px; right: 10px; background: rgba(0,0,0,.55); padding: 6px 10px; border-radius: 6px; color: #ddd; font-size: 12px; line-height: 1.6; }
</style>
