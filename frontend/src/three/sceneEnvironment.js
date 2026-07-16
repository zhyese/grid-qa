/**
 * 场景环境模块：天空 + 城市园区 + 道路 + 树木 + PBR 环境光
 *
 * 商业产品级 3D 数字孪生视觉:
 *  - 程序化 HDR 风格天空盒(蓝→天蓝→白,模拟晴天)
 *  - PMREMGenerator + RoomEnvironment 做 PBR 环境贴图(玻璃幕墙反射)
 *  - 园区建筑群(玻璃幕墙写字楼 / 混凝土厂房 / 彩钢仓库)
 *  - 程序化道路(沥青 + 双黄线 + 人行道)
 *  - Billboard 树木(双面 + alpha)
 *  - 真实阴影 + 雾化
 *
 * 设计原则:
 *  1. 零外部资源 — 全部用 Three.js + Canvas 程序化生成
 *  2. 性能 — 共享材质 / 阴影分主次
 *  3. 复用 — 与 deviceFactory.js 共享 _matCache
 */
import * as THREE from 'three'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

// ========== 共享材质缓存(与 deviceFactory.js 共用) ==========
const _matCache = new Map()
function getMaterial(key, factory) {
  if (!_matCache.has(key)) _matCache.set(key, factory())
  return _matCache.get(key)
}

// ========== 简单确定性 PRNG(保证每次刷新建筑分布一致) ==========
function mulberry32(seed) {
  return function() {
    let t = seed += 0x6D2B79F5
    t = Math.imul(t ^ t >>> 15, t | 1)
    t ^= t + Math.imul(t ^ t >>> 7, t | 61)
    return ((t ^ t >>> 14) >>> 0) / 4294967296
  }
}

// ========== 1. 程序化天空盒(大球内表面) ==========
export function buildSky() {
  const geo = new THREE.SphereGeometry(150, 32, 16)
  // 程序化渐变(蓝→浅蓝→白雾)
  const canvas = document.createElement('canvas')
  canvas.width = 16
  canvas.height = 256
  const ctx = canvas.getContext('2d')
  const grad = ctx.createLinearGradient(0, 0, 0, 256)
  grad.addColorStop(0, '#4A6FA5')     // 顶部深蓝
  grad.addColorStop(0.45, '#7FA9C7')  // 中部天蓝
  grad.addColorStop(0.75, '#C7D4DD')  // 地平线雾蓝
  grad.addColorStop(0.85, '#A8B5BD')  // 远处建筑剪影
  grad.addColorStop(1, '#7A8B92')     // 地面雾
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, 16, 256)
  const tex = new THREE.CanvasTexture(canvas)
  tex.magFilter = THREE.LinearFilter
  tex.minFilter = THREE.LinearFilter
  const mat = new THREE.MeshBasicMaterial({ map: tex, side: THREE.BackSide, fog: false, depthWrite: false })
  const mesh = new THREE.Mesh(geo, mat)
  mesh.renderOrder = -1000
  return mesh
}

// ========== 2. PBR 环境贴图(PMREM + RoomEnvironment) ==========
export function buildPMREMEnvironment(renderer) {
  const pmrem = new THREE.PMREMGenerator(renderer)
  pmrem.compileEquirectangularShader()
  const roomScene = new RoomEnvironment()
  // 给 RoomEnvironment 加几盏彩色光,模拟城市反射的多彩
  const ambientMat = new THREE.MeshBasicMaterial({ color: 0xA0B8D0 })
  // 不用,RoomEnvironment 自身够用
  const envMap = pmrem.fromScene(roomScene, 0.04).texture
  pmrem.dispose()
  return envMap
}

// ========== 3. 地形(大地面 + 主道路) ==========
export function buildLand() {
  const g = new THREE.Group()

  // 大地面(绿地) — 200x200
  const grass = new THREE.Mesh(
    new THREE.PlaneGeometry(200, 200),
    new THREE.MeshStandardMaterial({ color: 0x6B8E5A, roughness: 0.95, metalness: 0.0 })
  )
  grass.rotation.x = -Math.PI / 2
  grass.position.y = -0.02
  grass.receiveShadow = true
  g.add(grass)

  // 站内混凝土地坪 — 36x28
  // 关闭 receiveShadow(在它之上的 platforms 已经接阴影),加 polygonOffset 防和 grass/子组件微 z-fight
  const inner = new THREE.Mesh(
    new THREE.PlaneGeometry(36, 28),
    new THREE.MeshStandardMaterial({
      color: 0xB0B3B8, roughness: 0.92, metalness: 0.02,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    })
  )
  inner.rotation.x = -Math.PI / 2
  inner.position.set(0, 0, 0)
  inner.receiveShadow = false
  g.add(inner)

  // 站内分区(深色块,模拟设备基础平台)
  const platMat = new THREE.MeshStandardMaterial({ color: 0x8A8D92, roughness: 0.85 })
  const platforms = [
    { pos: [0, 0, 0], size: [13, 8] },          // 主变区
    { pos: [15, 0, 0], size: [11, 13] },         // 110kV 配电区
    { pos: [-12, 0, 0], size: [9, 7] },          // 10kV 开关室
    { pos: [-12, 0, 8], size: [9, 5] },          // 控制室
    { pos: [0, 0, -8], size: [7, 5] },           // 无功补偿区
  ]
  for (const p of platforms) {
    const m = new THREE.Mesh(
      new THREE.PlaneGeometry(p.size[0], p.size[1]),
      new THREE.MeshStandardMaterial({
        color: 0x8A8D92,
        roughness: 0.85,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1,
      })
    )
    m.rotation.x = -Math.PI / 2
    m.position.set(p.pos[0], 0.012, p.pos[2])
    m.receiveShadow = true
    g.add(m)
  }

  return g
}

// ========== 4. 程序化道路(主干道 + 支路) ==========
export function buildRoads() {
  const g = new THREE.Group()
  // 路面 + 人行道材质(路面加 polygonOffset 防和 inner/grass 微 z-fight;路面阴影意义不大,关掉减少阴影边界闪烁)
  const roadMat = () => getMaterial('road-asphalt', () =>
    new THREE.MeshStandardMaterial({
      color: 0x3A3D42, roughness: 0.88, metalness: 0.05,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
    })
  )
  const lineMat = () => getMaterial('road-line', () =>
    new THREE.MeshBasicMaterial({ color: 0xF5E8B0 })
  )
  const sidewalkMat = () => getMaterial('sidewalk', () =>
    new THREE.MeshStandardMaterial({
      color: 0xA8A8A8, roughness: 0.92,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
    })
  )

  // 主干道 — 站前大道(E-W) + 中央大道(N-S),路宽 6m
  const mainRoads = [
    { start: [-30, 0, 14], end: [30, 0, 14], width: 6 },   // 站前大道(南侧)
    { start: [20, 0, -16], end: [20, 0, 14], width: 6 },    // 中央大道(东侧)
    { start: [-30, 0, -14], end: [20, 0, -14], width: 5 }, // 站后路(北侧)
  ]
  for (const rd of mainRoads) {
    const dx = rd.end[0] - rd.start[0]
    const dz = rd.end[2] - rd.start[2]
    const len = Math.sqrt(dx * dx + dz * dz)
    const road = new THREE.Mesh(
      new THREE.PlaneGeometry(len, rd.width),
      roadMat()
    )
    road.rotation.x = -Math.PI / 2
    road.rotation.z = -Math.atan2(dz, dx)
    road.position.set((rd.start[0] + rd.end[0]) / 2, 0.011, (rd.start[2] + rd.end[2]) / 2)
    // 路面不接阴影(阴影边界和路面纹理叠加会有 z-fight 视觉感)
    road.receiveShadow = false
    g.add(road)
    // 中央双黄线(错开 y 到 0.04 + polygonOffset 防 z-fight)
    const lineGeo = new THREE.PlaneGeometry(len, 0.18)
    const lineMatInst = new THREE.MeshBasicMaterial({
      color: 0xF5E8B0,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    })
    const l1 = new THREE.Mesh(lineGeo, lineMatInst)
    l1.rotation.x = -Math.PI / 2
    l1.rotation.z = -Math.atan2(dz, dx)
    l1.position.set(road.position.x, 0.04, road.position.z + 0.25)
    g.add(l1)
    const l2 = new THREE.Mesh(lineGeo, lineMatInst)
    l2.rotation.x = -Math.PI / 2
    l2.rotation.z = -Math.atan2(dz, dx)
    l2.position.set(road.position.x, 0.04, road.position.z - 0.25)
    g.add(l2)
    // 人行道(路两侧) — y 错开到 0.02 + 不接阴影
    for (const side of [-1, 1]) {
      const sw = new THREE.Mesh(
        new THREE.PlaneGeometry(len, 1.0),
        sidewalkMat()
      )
      sw.rotation.x = -Math.PI / 2
      sw.rotation.z = -Math.atan2(dz, dx)
      sw.position.set(road.position.x, 0.02, road.position.z + side * (rd.width / 2 + 0.6))
      sw.receiveShadow = false
      g.add(sw)
    }
  }

  return g
}

// ========== 5. 程序化建筑(玻璃幕墙/写字楼/厂房) ==========
const BUILDING_CONFIGS = {
  // 玻璃幕墙高层(园区写字楼)
  glassTower: (w, d) => ({ height: 8 + Math.random() * 12, color: 0x6E92B0, mat: 'glass' }),
  // 商务楼(中高层,带窗框)
  office: (w, d) => ({ height: 4 + Math.random() * 6, color: 0xC5CBD2, mat: 'office' }),
  // 厂房(低矮,平顶)
  factory: (w, d) => ({ height: 2 + Math.random() * 2, color: 0x9DA8B0, mat: 'factory' }),
  // 仓库(彩钢)
  warehouse: (w, d) => ({ height: 2 + Math.random() * 1.5, color: 0xB85450, mat: 'warehouse' }),
  // 住宅(暖灰)
  residential: (w, d) => ({ height: 3 + Math.random() * 5, color: 0xD4C8A8, mat: 'residential' }),
}

function buildGlassTower(w, h, d, color) {
  const tower = new THREE.Group()
  // 玻璃幕墙材质:加 polygonOffset 防 self-shadow acne,关闭 receiveShadow(金属反射为主,阴影意义不大且会闪)
  const mat = getMaterial(`glass-${color}`, () =>
    new THREE.MeshStandardMaterial({
      color, metalness: 0.85, roughness: 0.08,
      envMapIntensity: 1.0,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    })
  )
  const body = new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 2, 0.15), mat)
  body.position.y = h / 2
  body.castShadow = true
  body.receiveShadow = false
  tower.add(body)

  // 顶部机房/屋顶(尺寸缩小,留 0.15m 边距避免和 body 边沿 z-fight)
  const mech = new THREE.Mesh(
    new RoundedBoxGeometry(w * 0.35, 0.8, d * 0.35, 2, 0.1),
    new THREE.MeshStandardMaterial({ color: 0x5D6D7E, roughness: 0.7, metalness: 0.4 })
  )
  mech.position.y = h + 0.4
  mech.castShadow = true
  tower.add(mech)

  // 顶部天线(段数 6→16,法线平滑,消除阴影硬边)
  const antenna = new THREE.Mesh(
    new THREE.CylinderGeometry(0.05, 0.05, 1.5, 16),
    new THREE.MeshStandardMaterial({ color: 0xBBBBBB, metalness: 0.8, roughness: 0.3 })
  )
  antenna.position.y = h + 1.3
  antenna.castShadow = true
  tower.add(antenna)

  // 楼层横线条(每 1.5m 一条) — 改用 LineSegments(0 厚度,无 z-fight)
  const lineMat = getMaterial('floor-line', () =>
    new THREE.LineBasicMaterial({ color: 0x2C3E50, transparent: true, opacity: 0.7 })
  )
  const floors = Math.floor(h / 1.5)
  for (let i = 1; i < floors; i++) {
    const y = i * 1.5
    // 4 条线框出楼层分隔
    const pts = [
      // 前
      new THREE.Vector3(-w/2, y, d/2), new THREE.Vector3(w/2, y, d/2),
      // 后
      new THREE.Vector3(-w/2, y, -d/2), new THREE.Vector3(w/2, y, -d/2),
      // 左
      new THREE.Vector3(-w/2, y, -d/2), new THREE.Vector3(-w/2, y, d/2),
      // 右
      new THREE.Vector3(w/2, y, -d/2), new THREE.Vector3(w/2, y, d/2),
    ]
    const lineGeo = new THREE.BufferGeometry().setFromPoints(pts)
    const line = new THREE.LineSegments(lineGeo, lineMat)
    // renderOrder=1 让楼层线绘制在 body 之后(覆盖在表面)
    line.renderOrder = 1
    tower.add(line)
  }

  return tower
}

function buildOffice(w, h, d, color) {
  const tower = new THREE.Group()
  // body 加 polygonOffset 防 self-shadow acne
  const bodyMat = getMaterial(`office-${color}`, () =>
    new THREE.MeshStandardMaterial({
      color, metalness: 0.1, roughness: 0.75,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    })
  )
  const body = new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 2, 0.15), bodyMat)
  body.position.y = h / 2
  body.castShadow = true
  body.receiveShadow = true
  tower.add(body)

  // 窗框(深色窗带) — 改用 LineSegments(0 厚度,无 z-fight)
  const winMat = getMaterial('office-win', () =>
    new THREE.LineBasicMaterial({
      color: 0x2A3D5C, transparent: true, opacity: 0.85,
    })
  )
  const floors = Math.max(2, Math.floor(h / 1.2))
  // 每层画 4 条窗带线框(前/后/左/右)
  for (let i = 0; i < floors; i++) {
    const y = 0.5 + i * 1.2
    if (y >= h - 0.3) break
    const pts = [
      new THREE.Vector3(-w/2, y, d/2), new THREE.Vector3(w/2, y, d/2),
      new THREE.Vector3(-w/2, y, -d/2), new THREE.Vector3(w/2, y, -d/2),
      new THREE.Vector3(-w/2, y, -d/2), new THREE.Vector3(-w/2, y, d/2),
      new THREE.Vector3(w/2, y, -d/2), new THREE.Vector3(w/2, y, d/2),
    ]
    const lineGeo = new THREE.BufferGeometry().setFromPoints(pts)
    const line = new THREE.LineSegments(lineGeo, winMat)
    line.renderOrder = 1
    tower.add(line)
  }
  return tower
}

function buildFactory(w, h, d, color) {
  const tower = new THREE.Group()
  const mat = getMaterial(`factory-${color}`, () =>
    new THREE.MeshStandardMaterial({ color, metalness: 0.2, roughness: 0.8 })
  )
  // 锯齿形屋顶(工业厂房典型)
  const body = new THREE.Mesh(new RoundedBoxGeometry(w, h * 0.6, d, 2, 0.15), mat)
  body.position.y = h * 0.3
  body.castShadow = true
  tower.add(body)
  // 锯齿
  for (let i = 0; i < 4; i++) {
    const x = -w / 2 + (w / 4) * (i + 0.5)
    const saw = new THREE.Mesh(
      new RoundedBoxGeometry(w / 4 - 0.2, h * 0.4, d * 0.9, 2, 0.1),
      new THREE.MeshStandardMaterial({ color: 0x5D6D7E, roughness: 0.7, metalness: 0.3 })
    )
    saw.position.set(x, h * 0.6 + h * 0.2, 0)
    saw.castShadow = true
    tower.add(saw)
  }
  return tower
}

function buildWarehouse(w, h, d, color) {
  const tower = new THREE.Group()
  const mat = getMaterial(`warehouse-${color}`, () =>
    new THREE.MeshStandardMaterial({ color, metalness: 0.4, roughness: 0.55 })
  )
  // 拱形屋顶(用 CylinderGeometry 截段)
  const body = new THREE.Mesh(new RoundedBoxGeometry(w, h * 0.5, d, 2, 0.15), mat)
  body.position.y = h * 0.25
  body.castShadow = true
  tower.add(body)
  // 屋顶(半圆筒)
  const roof = new THREE.Mesh(
    new THREE.CylinderGeometry(d / 2, d / 2, w, 16, 1, false, 0, Math.PI),
    mat
  )
  roof.rotation.z = Math.PI / 2
  roof.position.y = h * 0.5
  roof.castShadow = true
  tower.add(roof)
  return tower
}

function buildResidential(w, h, d, color) {
  const tower = new THREE.Group()
  // body 加 polygonOffset 防 self-shadow acne
  const mat = getMaterial(`residential-${color}`, () =>
    new THREE.MeshStandardMaterial({
      color, metalness: 0.1, roughness: 0.85,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    })
  )
  const body = new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 2, 0.15), mat)
  body.position.y = h / 2
  body.castShadow = true
  body.receiveShadow = true
  tower.add(body)
  // 斜屋顶
  const roofGeo = new THREE.ConeGeometry(Math.max(w, d) * 0.75, 1.2, 4)
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x8B3A2E, roughness: 0.85 })
  const roof = new THREE.Mesh(roofGeo, roofMat)
  roof.rotation.y = Math.PI / 4
  roof.position.y = h + 0.5
  roof.castShadow = true
  tower.add(roof)
  // 阳台(每层) — 紧贴 body 外侧(z = d/2 + 0.31),不突出 w 方向,加 polygonOffset 防 z-fight
  const balconyMat = new THREE.MeshStandardMaterial({
    color: 0xFFFFFF, roughness: 0.6,
    polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
  })
  for (let i = 0; i < Math.floor(h / 1.5); i++) {
    // 阳台只在 z 正方向一侧,尺寸严格 w(不突出),仅 z 方向突出 0.31m
    const balcony = new THREE.Mesh(
      new RoundedBoxGeometry(w, 0.15, 0.6, 2, 0.05),
      balconyMat
    )
    balcony.position.set(0, 0.3 + i * 1.5, d / 2 + 0.3)
    balcony.castShadow = true
    tower.add(balcony)
  }
  return tower
}

const BUILDERS = {
  glass: buildGlassTower,
  office: buildOffice,
  factory: buildFactory,
  warehouse: buildWarehouse,
  residential: buildResidential,
}

/**
 * 园区建筑群(围绕变电站的程序化生成)
 * @param {object} config - {seed, exclusionZones: [{x, z, radius}]}
 */
export function buildCityBlock(config = {}) {
  const { seed = 42, exclusionZones = [] } = config
  const rand = mulberry32(seed)
  const g = new THREE.Group()
  g.name = 'CityBlock'

  // 候选建筑模板(在 -30..30 范围内随机分布)
  const candidates = [
    // 园区西侧(居民区)
    { type: 'residential', x: -25, z: -25, w: 3, d: 3, h: 4 },
    { type: 'residential', x: -22, z: -22, w: 3, d: 3, h: 4 },
    { type: 'residential', x: -22, z: -28, w: 3, d: 3, h: 5 },
    { type: 'residential', x: -28, z: -25, w: 3, d: 3, h: 4 },
    { type: 'residential', x: -28, z: -22, w: 3, d: 3, h: 5 },
    { type: 'residential', x: -25, z: -20, w: 3, d: 3, h: 4 },

    // 园区西北(科技园)
    { type: 'office', x: -28, z: 20, w: 4, d: 4, h: 5 },
    { type: 'office', x: -28, z: 24, w: 4, d: 4, h: 6 },
    { type: 'office', x: -24, z: 22, w: 4, d: 4, h: 5 },
    { type: 'office', x: -24, z: 26, w: 4, d: 4, h: 7 },

    // 园区南侧(商业)
    { type: 'glassTower', x: 0, z: 22, w: 4, d: 4, h: 14 },
    { type: 'glassTower', x: 5, z: 22, w: 4, d: 4, h: 18 },
    { type: 'glassTower', x: 10, z: 22, w: 4, d: 4, h: 12 },
    { type: 'office', x: -3, z: 26, w: 3, d: 3, h: 6 },
    { type: 'office', x: 14, z: 26, w: 3, d: 3, h: 7 },

    // 园区东侧(科技园)
    { type: 'office', x: 26, z: 22, w: 4, d: 4, h: 8 },
    { type: 'office', x: 26, z: 18, w: 4, d: 4, h: 10 },
    { type: 'office', x: 30, z: 22, w: 4, d: 4, h: 6 },
    { type: 'glassTower', x: 28, z: 26, w: 3, d: 3, h: 15 },

    // 园区东南(厂房/仓库)
    { type: 'factory', x: 26, z: -20, w: 5, d: 4, h: 3 },
    { type: 'factory', x: 26, z: -25, w: 5, d: 4, h: 3 },
    { type: 'warehouse', x: 30, z: -22, w: 5, d: 3, h: 2.5 },
    { type: 'warehouse', x: 30, z: -27, w: 4, d: 3, h: 2.5 },

    // 园区东北(科技园)
    { type: 'office', x: 26, z: 0, w: 4, d: 4, h: 7 },
    { type: 'office', x: 30, z: 0, w: 4, d: 4, h: 8 },
    { type: 'glassTower', x: 28, z: 4, w: 3, d: 3, h: 12 },

    // 园区北侧(住宅 + 写字楼)
    { type: 'residential', x: 0, z: -25, w: 3, d: 3, h: 5 },
    { type: 'residential', x: 4, z: -25, w: 3, d: 3, h: 4 },
    { type: 'residential', x: 8, z: -25, w: 3, d: 3, h: 4 },
    { type: 'residential', x: 12, z: -25, w: 3, d: 3, h: 5 },
    { type: 'office', x: -8, z: -25, w: 4, d: 4, h: 5 },
    { type: 'office', x: -8, z: -22, w: 4, d: 4, h: 6 },
  ]

  for (const b of candidates) {
    // 检查是否在禁区(变电站主区域 35x28)
    if (Math.abs(b.x) < 19 && Math.abs(b.z) < 15) continue
    const conf = BUILDING_CONFIGS[b.type](b.w, b.d)
    const h = b.h || conf.height
    const builder = BUILDERS[conf.mat] || buildOffice
    const tower = builder(b.w, h, b.d, conf.color)
    tower.position.set(b.x, 0, b.z)
    // 随机微小旋转(增加层次)
    tower.rotation.y = (rand() - 0.5) * 0.15
    g.add(tower)
  }

  return g
}

// ========== 6. 程序化树木(billboard) ==========
function buildTreeTexture(style = 'pine') {
  const c = document.createElement('canvas')
  c.width = 64
  c.height = 128
  const ctx = c.getContext('2d')
  // 透明背景
  ctx.clearRect(0, 0, 64, 128)
  if (style === 'pine') {
    // 松树:深绿三角形
    ctx.fillStyle = '#2D5C2D'
    for (let i = 0; i < 3; i++) {
      ctx.beginPath()
      ctx.moveTo(32, 8 + i * 28)
      ctx.lineTo(8, 38 + i * 28)
      ctx.lineTo(56, 38 + i * 28)
      ctx.closePath()
      ctx.fill()
    }
    // 树干
    ctx.fillStyle = '#5D3F2E'
    ctx.fillRect(28, 92, 8, 30)
  } else if (style === 'round') {
    // 圆树冠
    const grad = ctx.createRadialGradient(32, 50, 5, 32, 50, 28)
    grad.addColorStop(0, '#5DA85D')
    grad.addColorStop(1, '#2D5C2D')
    ctx.fillStyle = grad
    ctx.beginPath()
    ctx.arc(32, 50, 28, 0, Math.PI * 2)
    ctx.fill()
    // 树干
    ctx.fillStyle = '#5D3F2E'
    ctx.fillRect(28, 78, 8, 40)
  } else {
    // 樱花/花树
    const grad = ctx.createRadialGradient(32, 50, 5, 32, 50, 28)
    grad.addColorStop(0, '#FFC8D0')
    grad.addColorStop(1, '#D88FA0')
    ctx.fillStyle = grad
    ctx.beginPath()
    ctx.arc(32, 50, 28, 0, Math.PI * 2)
    ctx.fill()
    ctx.fillStyle = '#5D3F2E'
    ctx.fillRect(28, 78, 8, 40)
  }
  return new THREE.CanvasTexture(c)
}

export function buildTrees() {
  const g = new THREE.Group()
  g.name = 'Trees'

  // 沿主道路两侧 + 园区边界
  const rand = mulberry32(7)
  const positions = []

  // 道路两侧
  const roadEdges = [
    { axis: 'x', from: -30, to: 30, z: 11.5, side: -1 },
    { axis: 'x', from: -30, to: 30, z: 16.5, side: 1 },
    { axis: 'z', from: -16, to: 14, x: 17.5, side: -1 },
    { axis: 'z', from: -16, to: 14, x: 22.5, side: 1 },
  ]
  for (const e of roadEdges) {
    const step = 2 + rand() * 1.5
    if (e.axis === 'x') {
      for (let v = e.from; v <= e.to; v += step) {
        positions.push([v, 0, e.z])
      }
    } else {
      for (let v = e.from; v <= e.to; v += step) {
        positions.push([e.x, 0, v])
      }
    }
  }

  // 园区外圈(稀疏)
  for (let i = 0; i < 80; i++) {
    const angle = rand() * Math.PI * 2
    const r = 30 + rand() * 12
    const x = Math.cos(angle) * r
    const z = Math.sin(angle) * r
    // 排除主道路
    if (Math.abs(z - 14) < 4 && Math.abs(x) < 30) continue
    if (Math.abs(x - 20) < 4 && Math.abs(z) < 16) continue
    positions.push([x, 0, z])
  }

  // 创建树
  // 关键：transparent: false + depthWrite 恢复默认(true)
  // 原来 transparent:true + depthWrite:false + 两片交叉 = z-sort 闪烁经典组合
  // alphaTest 已裁掉透明区域,不需要 transparent
  const styles = ['pine', 'round', 'round', 'round', 'sakura']
  const matCache = {}
  for (const [x, y, z] of positions) {
    const style = styles[Math.floor(rand() * styles.length)]
    if (!matCache[style]) {
      matCache[style] = new THREE.MeshBasicMaterial({
        map: buildTreeTexture(style),
        transparent: false,
        alphaTest: 0.4,
        side: THREE.DoubleSide,
      })
    }
    const tree = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 3.2), matCache[style])
    tree.position.set(x, 1.6, z)
    tree.rotation.y = rand() * Math.PI
    g.add(tree)
    // 交叉另一面(避免从背面看)
    const tree2 = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 3.2), matCache[style])
    tree2.position.set(x, 1.6, z)
    tree2.rotation.y = tree.rotation.y + Math.PI / 2
    g.add(tree2)
  }

  return g
}

// ========== 7. 整合入口 ==========
/**
 * 完整园区场景(站外 + 站内环境)
 * @returns {THREE.Group} 包含地形/道路/建筑/树木/控制室/围栏
 */
export function buildDistrict(envMap) {
  const g = new THREE.Group()
  g.name = 'District'

  // 地形
  g.add(buildLand())

  // 道路
  g.add(buildRoads())

  // 建筑群：Kenney glTF 真实模型（环形 25 栋外圈，替换程序化方盒）
  const _loader = new GLTFLoader()
  const _names = ['building-a','building-b','building-c','building-d','building-e','building-f','building-g','building-h','building-i','building-j','building-k','building-l','building-m','building-n','building-o','building-p','building-q','building-r','building-s','building-t']
  for (let i = 0; i < 25; i++) {
    const ang = (i / 25) * Math.PI * 2
    const r = 24 + (i % 3) * 3
    const nm = _names[i % _names.length]
    _loader.load(`/models/kenney_industrial/Models/GLB format/${nm}.glb`, (gltf) => {
      const m = gltf.scene
      m.scale.set(2.5, 2.5, 2.5)
      m.position.set(Math.cos(ang) * r, 0, Math.sin(ang) * r)
      m.rotation.y = -ang + Math.PI / 2
      m.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true } })
      g.add(m)
    })
  }

  // 树木
  g.add(buildTrees())

  // 围栏(变电站外圈)
  g.add(buildFence())

  // 站区控制室(已有逻辑但更精致)
  g.add(buildStationBuildings())

  // 应用 PBR envMap 到所有 Standard 材质
  if (envMap) applyEnvMap(g, envMap)

  return g
}

// ========== 辅助:围栏 ==========
function buildFence() {
  const g = new THREE.Group()
  const mat = getMaterial('fence', () =>
    new THREE.MeshStandardMaterial({
      color: 0x8B8E91, metalness: 0.6, roughness: 0.5,
      transparent: true, opacity: 0.85,
    })
  )
  const corners = [
    [-18, -14], [18, -14], [18, 14], [-18, 14], [-18, -14]
  ]
  for (let i = 0; i < corners.length - 1; i++) {
    const [x1, z1] = corners[i]
    const [x2, z2] = corners[i + 1]
    const dx = x2 - x1, dz = z2 - z1
    const len = Math.sqrt(dx * dx + dz * dz)
    const post = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 1.4, 8), mat)
    post.position.set(x1, 0.7, z1)
    g.add(post)
    const bar = new THREE.Mesh(
      new THREE.BoxGeometry(len, 0.05, 0.05),
      mat
    )
    bar.position.set((x1 + x2) / 2, 1.2, (z1 + z2) / 2)
    bar.rotation.y = -Math.atan2(dz, dx)
    g.add(bar)
  }
  return g
}

// ========== 辅助:控制室/开关室建筑(更精致) ==========
function buildStationBuildings() {
  const g = new THREE.Group()

  // 控制室
  const wallMat = new THREE.MeshStandardMaterial({ color: 0xD9D2C0, roughness: 0.85 })
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x4D4D4D, roughness: 0.7, metalness: 0.2 })
  const winMat = new THREE.MeshStandardMaterial({
    color: 0x1A2A3A, metalness: 0.7, roughness: 0.2,
    emissive: 0x0A141F, emissiveIntensity: 0.3,
  })

  // 控制室主体
  const cr = new THREE.Mesh(new RoundedBoxGeometry(8, 4, 4, 2, 0.3), wallMat)
  cr.position.set(-12, 2, 8)
  cr.castShadow = true
  cr.receiveShadow = true
  g.add(cr)
  const crRoof = new THREE.Mesh(new THREE.BoxGeometry(8.4, 0.2, 4.4), roofMat)
  crRoof.position.set(-12, 4.1, 8)
  g.add(crRoof)
  // 控制室窗
  for (let i = 0; i < 3; i++) {
    const win = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 1.2), winMat)
    win.position.set(-12 + (-1 + i) * 2.5, 2.5, 10.01)
    g.add(win)
  }
  // 控制室门
  const door = new THREE.Mesh(
    new THREE.BoxGeometry(0.8, 2.0, 0.05),
    new THREE.MeshStandardMaterial({ color: 0x4A3525, roughness: 0.7 })
  )
  door.position.set(-12, 1, 10.05)
  g.add(door)

  // 10kV 开关室
  const sw = new THREE.Mesh(new RoundedBoxGeometry(8, 3.5, 6, 2, 0.3), wallMat)
  sw.position.set(-12, 1.75, 0)
  sw.castShadow = true
  sw.receiveShadow = true
  g.add(sw)
  const swRoof = new THREE.Mesh(new THREE.BoxGeometry(8.4, 0.2, 6.4), roofMat)
  swRoof.position.set(-12, 3.6, 0)
  g.add(swRoof)
  // 通风口(顶部小盒子)
  for (let i = -1; i <= 1; i++) {
    const vent = new THREE.Mesh(
      new RoundedBoxGeometry(0.8, 0.4, 0.8, 2, 0.08),
      new THREE.MeshStandardMaterial({ color: 0x3D3D3D, metalness: 0.5, roughness: 0.6 })
    )
    vent.position.set(-12 + i * 2, 3.8, 0)
    g.add(vent)
  }

  return g
}

// ========== 辅助:给 Group 内所有 PBR 材质应用 envMap ==========
function applyEnvMap(root, envMap) {
  root.traverse(o => {
    if (o.isMesh && o.material) {
      const mats = Array.isArray(o.material) ? o.material : [o.material]
      for (const m of mats) {
        if (m.isMeshStandardMaterial || m.isMeshPhysicalMaterial) {
          m.envMap = envMap
          m.envMapIntensity = m.envMapIntensity ?? 1.0
          m.needsUpdate = true
        }
      }
    }
  })
}
