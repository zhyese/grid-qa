/**
 * 设备几何体工厂 — 每种 device.model 都有专属造型
 *
 * 不用 glTF/glb（避免外部资源依赖），全部用 Three.js 原生几何体组合。
 * 返回 Group，每台设备可整体定位/着色/拾取。
 *
 * 设计原则：
 *   1. 视觉辨识度 — 主变压器的"罐+油枕+套管"、断路器的"罐+绝缘子"一眼能认出
 *   2. 风险可视化 — riskColor 仅在指定组（套管/金属外壳）上体现，金属本体保持工业色
 *   3. 性能 — 共享材质（同一颜色/粗糙度共用 Material 实例）
 */
import * as THREE from 'three'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'

// ========== 共享材质（按需懒加载，全场景共享） ==========
const _matCache = new Map()
function getMaterial(key, factory) {
  if (!_matCache.has(key)) _matCache.set(key, factory())
  return _matCache.get(key)
}

function metalMat(color, opts = {}) {
  return getMaterial(`metal-${color}-${JSON.stringify(opts)}`, () =>
    new THREE.MeshStandardMaterial({
      color,
      metalness: opts.metalness ?? 0.7,
      roughness: opts.roughness ?? 0.35,
      emissive: opts.emissive ?? 0x000000,
      emissiveIntensity: opts.emissiveIntensity ?? 0.0,
    })
  )
}

function porcelainMat(color = 0xE8DCC4) {
  // 瓷质绝缘子（米白偏暖）
  return getMaterial(`porcelain-${color}`, () =>
    new THREE.MeshStandardMaterial({
      color,
      metalness: 0.1,
      roughness: 0.55,
      emissive: 0x000000,
    })
  )
}

function concreteMat() {
  // 水泥底座/地面
  return getMaterial('concrete', () =>
    new THREE.MeshStandardMaterial({ color: 0x9AA0A6, metalness: 0.05, roughness: 0.9 })
  )
}

function grassMat() {
  // 站区外绿地
  return getMaterial('grass', () =>
    new THREE.MeshStandardMaterial({ color: 0x3E5C3A, metalness: 0.0, roughness: 0.95 })
  )
}

function buildingMat() {
  // 控制室/开关室墙体
  return getMaterial('building', () =>
    new THREE.MeshStandardMaterial({ color: 0xC8C2B4, metalness: 0.05, roughness: 0.85 })
  )
}

function roofMat() {
  // 屋顶（深灰）
  return getMaterial('roof', () =>
    new THREE.MeshStandardMaterial({ color: 0x4D4D4D, metalness: 0.2, roughness: 0.7 })
  )
}

function roadMat() {
  // 巡检道路
  return getMaterial('road', () =>
    new THREE.MeshStandardMaterial({ color: 0x555A5F, metalness: 0.1, roughness: 0.8 })
  )
}

function fenceMat() {
  // 围栏（铁艺）
  return getMaterial('fence', () =>
    new THREE.MeshStandardMaterial({ color: 0x8B8E91, metalness: 0.6, roughness: 0.5, transparent: true, opacity: 0.7 })
  )
}

// ========== 通用辅助：绝缘子（白色圆盘堆叠） ==========
function buildInsulator(radiusBottom = 0.18, radiusTop = 0.12, height = 1.0, disks = 4) {
  const group = new THREE.Group()
  const mat = porcelainMat()
  const segHeight = height / disks
  for (let i = 0; i < disks; i++) {
    const y = segHeight * (i + 0.5)
    // 圆盘伞裙（更宽）
    const skirt = new THREE.Mesh(
      new THREE.CylinderGeometry(radiusBottom * 1.4, radiusBottom * 1.4, segHeight * 0.18, 16),
      mat
    )
    skirt.position.y = y - segHeight * 0.4
    skirt.castShadow = true
    group.add(skirt)
    // 主体圆柱
    const trunk = new THREE.Mesh(
      new THREE.CylinderGeometry(radiusTop, radiusBottom, segHeight * 0.7, 12),
      mat
    )
    trunk.position.y = y + segHeight * 0.05
    trunk.castShadow = true
    group.add(trunk)
  }
  return group
}

// ========== 通用辅助：圆角金属底座 ==========
function buildBase(width, depth, height = 0.2) {
  const base = new THREE.Mesh(
    new RoundedBoxGeometry(width, height, depth, 2, 0.04),
    concreteMat()
  )
  base.position.y = height / 2
  base.castShadow = true
  base.receiveShadow = true
  return base
}

// ========== 主变压器 ==========
function buildTransformer(size, riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 3.0  // 标准化到 3 单位高度
  const bodyW = w, bodyH = h * 0.7, bodyD = d

  // 水泥底座
  g.add(buildBase(bodyW * 1.3, bodyD * 1.3, 0.15 * scale))

  // 主体（油箱）— 风险色
  const bodyMat = metalMat(riskColor, { metalness: 0.5, roughness: 0.55, emissive: riskColor, emissiveIntensity: 0.05 })
  const body = new THREE.Mesh(new THREE.CylinderGeometry(bodyH * 0.5, bodyH * 0.5, bodyW, 28), bodyMat)
  body.rotation.z = Math.PI / 2
  body.position.y = 0.15 * scale + bodyH * 0.5
  body.castShadow = true
  body.receiveShadow = true
  g.add(body)

  // 顶部油枕（横向圆柱）
  const tank = new THREE.Mesh(
    new THREE.CylinderGeometry(0.3 * scale, 0.3 * scale, bodyW * 0.9, 16),
    metalMat(0x6E6E6E, { metalness: 0.8, roughness: 0.3 })
  )
  tank.rotation.z = Math.PI / 2
  tank.position.y = 0.15 * scale + bodyH + 0.1 * scale
  tank.castShadow = true
  g.add(tank)

  // 套管（4 个：左前、左后、右前、右后）— 高压端
  const bushHeight = 1.2 * scale
  const bushRadius = 0.08 * scale
  const bushMat = porcelainMat(0xEFE5D0)
  const positions = [
    [-bodyW * 0.3, 0.3, bodyD * 0.3],
    [-bodyW * 0.3, 0.3, -bodyD * 0.3],
    [bodyW * 0.3, 0.3, bodyD * 0.3],
    [bodyW * 0.3, 0.3, -bodyD * 0.3],
  ]
  for (const [x, _y, z] of positions) {
    const bush = new THREE.Mesh(
      new THREE.CylinderGeometry(bushRadius, bushRadius * 1.3, bushHeight, 12),
      bushMat
    )
    bush.position.set(x, 0.15 * scale + bodyH + bushHeight / 2, z)
    bush.castShadow = true
    g.add(bush)
    // 顶部金属接线帽
    const cap = new THREE.Mesh(
      new THREE.CylinderGeometry(bushRadius * 1.4, bushRadius * 1.1, 0.08 * scale, 12),
      metalMat(0xC0C0C0, { metalness: 0.9, roughness: 0.2 })
    )
    cap.position.set(x, 0.15 * scale + bodyH + bushHeight + 0.04 * scale, z)
    cap.castShadow = true
    g.add(cap)
  }

  // 散热片（两侧：6 片竖向）
  const finMat = metalMat(0x707B8C, { metalness: 0.6, roughness: 0.45 })
  for (let side = -1; side <= 1; side += 2) {
    for (let i = 0; i < 6; i++) {
      const fin = new THREE.Mesh(
        new THREE.BoxGeometry(0.04 * scale, bodyH * 0.85, bodyD * 0.85),
        finMat
      )
      fin.position.set(side * (bodyW * 0.5 + 0.05 * scale), 0.15 * scale + bodyH / 2, bodyD * 0)
      fin.castShadow = true
      g.add(fin)
    }
  }

  // 控制箱（旁边）
  const ctrl = new THREE.Mesh(
    new RoundedBoxGeometry(bodyW * 0.35, 0.8 * scale, bodyD * 0.4, 2, 0.05),
    metalMat(0x404B5C, { metalness: 0.4, roughness: 0.6 })
  )
  ctrl.position.set(bodyW * 0.85, 0.15 * scale + 0.4 * scale, 0)
  ctrl.castShadow = true
  g.add(ctrl)

  return g
}

// ========== 断路器（SF6 罐式） ==========
function buildBreaker(size, riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 2.0

  // 底座
  g.add(buildBase(w * 1.2, d * 1.2, 0.15 * scale))

  // 灭弧室（罐体）— 风险色
  const tankMat = metalMat(riskColor, { metalness: 0.5, roughness: 0.4, emissive: riskColor, emissiveIntensity: 0.05 })
  const tank = new THREE.Mesh(
    new THREE.CylinderGeometry(0.25 * scale, 0.25 * scale, h * 0.5, 18),
    tankMat
  )
  tank.position.y = 0.15 * scale + h * 0.25
  tank.castShadow = true
  g.add(tank)

  // 上下支柱绝缘子
  const insTop = buildInsulator(0.12 * scale, 0.08 * scale, h * 0.25, 3)
  insTop.position.y = 0.15 * scale + h * 0.5
  g.add(insTop)

  // 顶部接线端子
  const topCap = new THREE.Mesh(
    new THREE.CylinderGeometry(0.1 * scale, 0.1 * scale, 0.1 * scale, 12),
    metalMat(0xC0C0C0, { metalness: 0.85, roughness: 0.2 })
  )
  topCap.position.y = 0.15 * scale + h * 0.5 + h * 0.25 + 0.05 * scale
  g.add(topCap)

  // 顶部金属导线引出
  const wire = new THREE.Mesh(
    new THREE.CylinderGeometry(0.02 * scale, 0.02 * scale, 0.6, 8),
    metalMat(0xA8A8A8, { metalness: 0.9, roughness: 0.15 })
  )
  wire.position.y = 0.15 * scale + h * 0.5 + h * 0.25 + 0.4
  g.add(wire)

  // 操作机构箱（侧面）
  const op = new THREE.Mesh(
    new RoundedBoxGeometry(0.4 * scale, 0.6 * scale, 0.35 * scale, 2, 0.05),
    metalMat(0x3A4A5C, { metalness: 0.4, roughness: 0.6 })
  )
  op.position.set(w * 0.6, 0.15 * scale + 0.3 * scale, 0)
  op.castShadow = true
  g.add(op)

  return g
}

// ========== 隔离开关（双柱 + 水平刀闸） ==========
function buildDisconnector(size, _riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 1.5

  // 底座
  g.add(buildBase(w * 1.8, d * 1.8, 0.15 * scale))

  // 两个支柱绝缘子
  const post1 = buildInsulator(0.1 * scale, 0.07 * scale, h * 0.6, 3)
  post1.position.set(-w * 0.35, 0.15 * scale + h * 0.3, 0)
  g.add(post1)
  const post2 = buildInsulator(0.1 * scale, 0.07 * scale, h * 0.6, 3)
  post2.position.set(w * 0.35, 0.15 * scale + h * 0.3, 0)
  g.add(post2)

  // 水平刀闸（细长杆 + 端部触头）
  const blade = new THREE.Mesh(
    new THREE.BoxGeometry(w * 0.95, 0.04 * scale, 0.04 * scale),
    metalMat(0xC0C5CA, { metalness: 0.9, roughness: 0.2 })
  )
  blade.position.y = 0.15 * scale + h * 0.6
  blade.castShadow = true
  g.add(blade)
  // 端部触头（小球）
  const tipMat = metalMat(0xE8E8E8, { metalness: 0.95, roughness: 0.1 })
  const tip1 = new THREE.Mesh(new THREE.SphereGeometry(0.06 * scale, 12, 8), tipMat)
  tip1.position.set(-w * 0.5, 0.15 * scale + h * 0.6, 0)
  g.add(tip1)
  const tip2 = new THREE.Mesh(new THREE.SphereGeometry(0.06 * scale, 12, 8), tipMat)
  tip2.position.set(w * 0.5, 0.15 * scale + h * 0.6, 0)
  g.add(tip2)

  // 操作连杆（水平）
  const rod = new THREE.Mesh(
    new THREE.CylinderGeometry(0.02 * scale, 0.02 * scale, w * 1.2, 8),
    metalMat(0x6E7378, { metalness: 0.7, roughness: 0.4 })
  )
  rod.rotation.z = Math.PI / 2
  rod.position.y = 0.15 * scale + 0.05
  g.add(rod)

  return g
}

// ========== 电流互感器（CT：环形） ==========
function buildCT(size, riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 2.0

  // 底座
  g.add(buildBase(w * 1.5, d * 1.5, 0.15 * scale))

  // 支柱
  const post = buildInsulator(0.09 * scale, 0.06 * scale, h * 0.4, 3)
  post.position.y = 0.15 * scale + h * 0.2
  g.add(post)

  // 环形主体（顶部）— 风险色
  const ringMat = metalMat(riskColor, { metalness: 0.5, roughness: 0.45, emissive: riskColor, emissiveIntensity: 0.05 })
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(0.25 * scale, 0.08 * scale, 12, 24),
    ringMat
  )
  ring.position.y = 0.15 * scale + h * 0.4 + 0.25 * scale
  ring.rotation.x = Math.PI / 2
  ring.castShadow = true
  g.add(ring)

  // 顶部接线盒
  const top = new THREE.Mesh(
    new THREE.BoxGeometry(0.4 * scale, 0.15 * scale, 0.4 * scale),
    metalMat(0x4A5566, { metalness: 0.4, roughness: 0.6 })
  )
  top.position.y = 0.15 * scale + h * 0.4 + 0.5 * scale
  top.castShadow = true
  g.add(top)

  return g
}

// ========== 电压互感器（PT：圆筒形） ==========
function buildPT(size, riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 2.0

  g.add(buildBase(w * 1.5, d * 1.5, 0.15 * scale))

  // 支柱
  const post = buildInsulator(0.09 * scale, 0.06 * scale, h * 0.35, 3)
  post.position.y = 0.15 * scale + h * 0.175
  g.add(post)

  // 圆筒主体 — 风险色
  const cyl = new THREE.Mesh(
    new THREE.CylinderGeometry(0.18 * scale, 0.2 * scale, h * 0.45, 16),
    metalMat(riskColor, { metalness: 0.5, roughness: 0.45, emissive: riskColor, emissiveIntensity: 0.05 })
  )
  cyl.position.y = 0.15 * scale + h * 0.35 + h * 0.225
  cyl.castShadow = true
  g.add(cyl)

  // 顶部伞裙
  const cap = new THREE.Mesh(
    new THREE.CylinderGeometry(0.22 * scale, 0.18 * scale, 0.08 * scale, 16),
    porcelainMat(0xEFE5D0)
  )
  cap.position.y = 0.15 * scale + h * 0.35 + h * 0.45 + 0.04 * scale
  g.add(cap)

  // 顶部接线柱
  const term = new THREE.Mesh(
    new THREE.CylinderGeometry(0.04 * scale, 0.04 * scale, 0.2 * scale, 10),
    metalMat(0xC0C0C0, { metalness: 0.9, roughness: 0.2 })
  )
  term.position.y = 0.15 * scale + h * 0.35 + h * 0.45 + 0.18 * scale
  g.add(term)

  return g
}

// ========== 避雷器（多节圆柱堆叠） ==========
function buildArrester(size, _riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = h / 2.5

  g.add(buildBase(w * 1.5, d * 1.5, 0.15 * scale))

  // 4 节圆柱堆叠（氧化锌阀片）
  const segMat = metalMat(0x6E7A8A, { metalness: 0.5, roughness: 0.55 })
  const segs = 4
  const segH = h * 0.85 / segs
  for (let i = 0; i < segs; i++) {
    const seg = new THREE.Mesh(
      new THREE.CylinderGeometry(0.12 * scale, 0.12 * scale, segH * 0.92, 14),
      segMat
    )
    seg.position.y = 0.15 * scale + segH * (i + 0.5)
    seg.castShadow = true
    g.add(seg)
    // 段间法兰（稍宽）
    if (i < segs - 1) {
      const flange = new THREE.Mesh(
        new THREE.CylinderGeometry(0.16 * scale, 0.16 * scale, 0.03 * scale, 14),
        metalMat(0xB0B5BB, { metalness: 0.85, roughness: 0.2 })
      )
      flange.position.y = 0.15 * scale + segH * (i + 1)
      g.add(flange)
    }
  }

  // 顶部球形电极
  const ball = new THREE.Mesh(
    new THREE.SphereGeometry(0.1 * scale, 12, 10),
    metalMat(0xE0E0E0, { metalness: 0.9, roughness: 0.15 })
  )
  ball.position.y = 0.15 * scale + h * 0.85 + 0.1 * scale
  g.add(ball)

  // 接地线（到底座）
  const ground = new THREE.Mesh(
    new THREE.CylinderGeometry(0.015 * scale, 0.015 * scale, 0.15 * scale, 6),
    metalMat(0x2A2A2A, { metalness: 0.3, roughness: 0.7 })
  )
  ground.position.set(0.15 * scale, 0.15 * scale, 0)
  g.add(ground)

  return g
}

// ========== 母线（横长方体 + 支柱绝缘子） ==========
function buildBusbar(size, _riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  const scale = w / 8.0  // 标准化

  // 横长方体（铝/铜排）— 银白
  const bar = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    metalMat(0xC8CDD2, { metalness: 0.85, roughness: 0.25 })
  )
  bar.position.y = 0.15 * scale + h * 0.5 + 0.4 * scale
  bar.castShadow = true
  g.add(bar)

  // 多个支柱绝缘子（每隔 1.5m 一个）
  const spacing = 1.5 * scale
  const count = Math.max(2, Math.floor(w / spacing) + 1)
  for (let i = 0; i < count; i++) {
    const x = -w / 2 + (w / (count - 1)) * i
    const post = buildInsulator(0.1 * scale, 0.07 * scale, 0.4 * scale, 2)
    post.position.set(x, 0.15 * scale + 0.2 * scale, 0)
    g.add(post)
  }

  return g
}

// ========== 电缆（弯曲管道） ==========
function buildCable(size, _riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()
  // 沿 X 轴的管道
  const tube = new THREE.Mesh(
    new THREE.CylinderGeometry(0.05, 0.05, d, 12),
    metalMat(0x2C3E50, { metalness: 0.3, roughness: 0.7 })
  )
  tube.rotation.x = Math.PI / 2
  tube.position.y = h
  g.add(tube)
  return g
}

// ========== 补偿装置（电抗/电容：大方块 + 鳍片） ==========
function buildCompensation(size, riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()

  // 水泥平台
  g.add(buildBase(w * 1.2, d * 1.2, 0.2))

  // 主体（电抗器/电容器组）— 风险色
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(w, h * 0.7, d),
    metalMat(riskColor, { metalness: 0.4, roughness: 0.5, emissive: riskColor, emissiveIntensity: 0.04 })
  )
  body.position.y = 0.2 + h * 0.35
  body.castShadow = true
  body.receiveShadow = true
  g.add(body)

  // 顶部接线柱（3 相 × 2 端）
  for (let i = -1; i <= 1; i++) {
    const term = new THREE.Mesh(
      new THREE.CylinderGeometry(0.06, 0.06, 0.3, 10),
      metalMat(0xC0C0C0, { metalness: 0.9, roughness: 0.2 })
    )
    term.position.set(i * w * 0.25, 0.2 + h * 0.7 + 0.15, 0)
    g.add(term)
  }

  // 散热鳍片（顶部 3 片）
  for (let i = -1; i <= 1; i++) {
    const fin = new THREE.Mesh(
      new THREE.BoxGeometry(0.02, 0.15, d * 0.8),
      metalMat(0x5D6D7E, { metalness: 0.6, roughness: 0.5 })
    )
    fin.position.set(i * w * 0.3, 0.2 + h * 0.7 + 0.08, 0)
    g.add(fin)
  }

  return g
}

// ========== 电源系统（机柜造型） ==========
function buildPowerSupply(size, _riskColor) {
  const [w, h, d] = size
  const g = new THREE.Group()

  // 机柜主体
  const cab = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    metalMat(0x384C66, { metalness: 0.5, roughness: 0.55 })
  )
  cab.position.y = h / 2
  cab.castShadow = true
  cab.receiveShadow = true
  g.add(cab)

  // 机柜门缝（细黑线，模拟柜门）
  const door = new THREE.Mesh(
    new THREE.BoxGeometry(w * 0.95, h * 0.9, 0.02),
    metalMat(0x1A2533, { metalness: 0.3, roughness: 0.7 })
  )
  door.position.set(0, h / 2, d / 2 + 0.01)
  g.add(door)

  // 顶部状态指示灯（3 排小灯）
  const ledMat = (color) => metalMat(color, { metalness: 0.1, roughness: 0.3, emissive: color, emissiveIntensity: 0.6 })
  for (let i = 0; i < 6; i++) {
    const led = new THREE.Mesh(
      new THREE.SphereGeometry(0.04, 8, 6),
      i % 3 === 0 ? ledMat(0x2ECC71) : (i % 3 === 1 ? ledMat(0xF1C40F) : ledMat(0xE74C3C))
    )
    led.position.set(-w * 0.3 + (i % 3) * w * 0.3, h + 0.05, -d * 0.2 + Math.floor(i / 3) * d * 0.4)
    g.add(led)
  }

  // 散热栅格（顶部）
  const grill = new THREE.Mesh(
    new THREE.BoxGeometry(w * 0.8, 0.05, d * 0.8),
    metalMat(0x1A1F28, { metalness: 0.4, roughness: 0.6 })
  )
  grill.position.y = h + 0.025
  g.add(grill)

  return g
}

// ========== 工厂入口 ==========
const BUILDERS = {
  transformer: buildTransformer,
  breaker: buildBreaker,
  disconnector: buildDisconnector,
  ct: buildCT,
  pt: buildPT,
  arrester: buildArrester,
  busbar: buildBusbar,
  cable: buildCable,
  compensation: buildCompensation,
  powersupply: buildPowerSupply,
}

/**
 * 构建设备 3D 模型 Group
 * @param {string} model - 设备模型描述符（来自后端 twin_service.DEVICE_TYPES）
 * @param {number[]} size - [宽, 高, 深]
 * @param {number} riskColor - 风险色（0xRRGGBB）
 * @returns {THREE.Group}
 */
export function buildDevice(model, size, riskColor) {
  const builder = BUILDERS[model] || buildDefault
  return builder(size, riskColor)
}

function buildDefault(size, riskColor) {
  // 兜底：单一金属盒子（但比之前稍好 — 仍保留基础样式）
  const g = new THREE.Group()
  const [w, h, d] = size
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    metalMat(riskColor, { metalness: 0.4, roughness: 0.55 })
  )
  body.position.y = h / 2
  body.castShadow = true
  g.add(body)
  return g
}

/**
 * 构建区域指示（地面矩形 + 文字）
 */
export function buildAreaFloor(area) {
  const g = new THREE.Group()
  const pos = area.position || [0, 0, 0]
  const sz = area.size || [4, 0, 4]
  // 关键：y 提到 0.025（高于 buildLand 的 platforms 0.005），配合 polygonOffset 防 z-fighting
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(sz[0], sz[2]),
    new THREE.MeshStandardMaterial({
      color: 0x2A2D3A,
      transparent: true,
      opacity: 0.55,
      metalness: 0.1,
      roughness: 0.85,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2,
    })
  )
  floor.rotation.x = -Math.PI / 2
  floor.position.set(pos[0], 0.025, pos[2])
  // 半透明地面不接阴影(否则阴影 alpha 叠加会闪烁)
  floor.receiveShadow = false
  g.add(floor)

  // 边框（细线）
  const halfW = sz[0] / 2, halfD = sz[2] / 2
  const lineMat = new THREE.LineBasicMaterial({ color: 0x4A5468, transparent: true, opacity: 0.5 })
  const corners = [
    [-halfW, 0, -halfD], [halfW, 0, -halfD],
    [halfW, 0, halfD], [-halfW, 0, halfD], [-halfW, 0, -halfD]
  ].map(c => new THREE.Vector3(c[0] + pos[0], 0.026, c[2] + pos[2]))
  g.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(corners), lineMat))
  return g
}

/**
 * 站区环境（地面 + 道路 + 围栏 + 建筑）
 */
export function buildEnvironment() {
  const g = new THREE.Group()

  // 大地面（绿地）— 60×60
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(80, 80),
    grassMat()
  )
  ground.rotation.x = -Math.PI / 2
  ground.position.y = -0.01
  ground.receiveShadow = true
  g.add(ground)

  // 站内混凝土地面 — 30×30
  const inner = new THREE.Mesh(
    new THREE.PlaneGeometry(35, 25),
    new THREE.MeshStandardMaterial({ color: 0xB0B3B8, metalness: 0.05, roughness: 0.9 })
  )
  inner.rotation.x = -Math.PI / 2
  inner.position.set(0, 0, 0)
  inner.receiveShadow = true
  g.add(inner)

  // 巡检道路（十字交叉）— 1m 宽
  const road1 = new THREE.Mesh(
    new THREE.PlaneGeometry(35, 1.2),
    roadMat()
  )
  road1.rotation.x = -Math.PI / 2
  road1.position.set(0, 0.01, 0)
  road1.receiveShadow = true
  g.add(road1)
  const road2 = new THREE.Mesh(
    new THREE.PlaneGeometry(1.2, 25),
    roadMat()
  )
  road2.rotation.x = -Math.PI / 2
  road2.position.set(0, 0.01, 0)
  road2.receiveShadow = true
  g.add(road2)

  // 控制室建筑（左侧 -12, 8）— 实心盒
  const cr = new THREE.Mesh(
    new THREE.BoxGeometry(8, 4, 4),
    buildingMat()
  )
  cr.position.set(-12, 2, 8)
  cr.castShadow = true
  cr.receiveShadow = true
  g.add(cr)
  // 屋顶
  const crRoof = new THREE.Mesh(
    new THREE.BoxGeometry(8.4, 0.2, 4.4),
    roofMat()
  )
  crRoof.position.set(-12, 4.1, 8)
  crRoof.castShadow = true
  g.add(crRoof)
  // 控制室窗（深色玻璃）
  const winMat = new THREE.MeshStandardMaterial({ color: 0x1A2A3A, metalness: 0.7, roughness: 0.2, emissive: 0x0A141F, emissiveIntensity: 0.3 })
  for (let i = 0; i < 3; i++) {
    const win = new THREE.Mesh(
      new THREE.PlaneGeometry(1.5, 1.2),
      winMat
    )
    win.position.set(-12 + (-1 + i) * 2.5, 2.5, 10.01)
    g.add(win)
  }

  // 10kV 开关室（-12, 0）— 比控制室矮一点
  const sw = new THREE.Mesh(
    new THREE.BoxGeometry(8, 3.5, 6),
    buildingMat()
  )
  sw.position.set(-12, 1.75, 0)
  sw.castShadow = true
  sw.receiveShadow = true
  g.add(sw)
  const swRoof = new THREE.Mesh(
    new THREE.BoxGeometry(8.4, 0.2, 6.4),
    roofMat()
  )
  swRoof.position.set(-12, 3.6, 0)
  swRoof.castShadow = true
  g.add(swRoof)

  // 围栏（变电站外圈）
  const fencePts = [
    [-17.5, 0, -12.5], [17.5, 0, -12.5],
    [17.5, 0, 12.5], [-17.5, 0, 12.5], [-17.5, 0, -12.5]
  ]
  for (let i = 0; i < fencePts.length - 1; i++) {
    const p1 = fencePts[i], p2 = fencePts[i + 1]
    const dx = p2[0] - p1[0], dz = p2[2] - p1[2]
    const len = Math.sqrt(dx * dx + dz * dz)
    const post = new THREE.Mesh(
      new THREE.BoxGeometry(0.1, 1.2, 0.1),
      fenceMat()
    )
    post.position.set(p1[0], 0.6, p1[2])
    g.add(post)
    // 横杆
    const bar = new THREE.Mesh(
      new THREE.BoxGeometry(len, 0.04, 0.04),
      fenceMat()
    )
    bar.position.set((p1[0] + p2[0]) / 2, 1.0, (p1[2] + p2[2]) / 2)
    bar.rotation.y = -Math.atan2(dz, dx)
    g.add(bar)
  }
  // 角落立柱
  for (const [x, _y, z] of [[-17.5, 0, -12.5], [17.5, 0, -12.5], [17.5, 0, 12.5], [-17.5, 0, 12.5]]) {
    const corner = new THREE.Mesh(
      new THREE.BoxGeometry(0.15, 1.3, 0.15),
      fenceMat()
    )
    corner.position.set(x, 0.65, z)
    g.add(corner)
  }

  return g
}

/**
 * 区域标签（白底蓝字大字号）
 */
export function buildAreaLabel(text, x, z, color = '#5DADE2') {
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 64
  const ctx = canvas.getContext('2d')
  // 关键：标签改为完全实心不透明（alpha=1.0），配合 transparent:false 彻底避免 z-sort 闪烁
  // 背景（实心圆角矩形）
  const r = 16
  ctx.fillStyle = 'rgb(20, 25, 40)'
  ctx.beginPath(); ctx.roundRect(0, 0, 256, 64, r); ctx.fill()
  // 左侧色条
  ctx.save(); ctx.beginPath(); ctx.roundRect(0, 0, 256, 64, r); ctx.clip()
  ctx.fillStyle = color
  ctx.fillRect(0, 0, 6, 64)
  ctx.restore()
  // 文字
  ctx.fillStyle = '#FFFFFF'
  ctx.font = 'bold 22px "Microsoft YaHei", "PingFang SC", sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, 128, 32)
  const tex = new THREE.CanvasTexture(canvas)
  const mat = new THREE.SpriteMaterial({
    map: tex,
    transparent: false,
    depthTest: false,
    depthWrite: false,
  })
  const sprite = new THREE.Sprite(mat)
  sprite.position.set(x, 0.3, z)
  sprite.scale.set(4.0, 1.0, 1)
  // 强制最后画，避免与设备连接线/树遮挡竞争
  sprite.renderOrder = 100
  return sprite
}

/**
 * 设备标签（黑底白字 + 设备类型 icon）
 */
export function buildDeviceLabel(name, typeIcon, x, y, z, blink = false) {
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 56
  const ctx = canvas.getContext('2d')
  // 关键：标签改为完全实心不透明（alpha=1.0），配合 transparent:false 彻底避免 z-sort 闪烁
  // 背景（实心圆角矩形）
  const r = 14
  ctx.fillStyle = blink ? 'rgb(120, 30, 30)' : 'rgb(15, 20, 35)'
  ctx.beginPath(); ctx.roundRect(0, 0, 256, 56, r); ctx.fill()
  // 边框（圆角）
  ctx.strokeStyle = blink ? '#FF6644' : '#4A5468'
  ctx.lineWidth = 2
  ctx.beginPath(); ctx.roundRect(1, 1, 254, 54, r - 1); ctx.stroke()
  // icon
  ctx.font = '20px sans-serif'
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillStyle = '#FFFFFF'
  ctx.fillText(typeIcon || '📦', 8, 28)
  // 名字
  ctx.fillStyle = blink ? '#FFDD66' : '#FFFFFF'
  ctx.font = 'bold 16px "Microsoft YaHei", "PingFang SC", sans-serif'
  ctx.fillText(name, 36, 28)
  const tex = new THREE.CanvasTexture(canvas)
  const mat = new THREE.SpriteMaterial({
    map: tex,
    transparent: false,
    depthTest: false,
    depthWrite: false,
  })
  const sprite = new THREE.Sprite(mat)
  sprite.position.set(x, y, z)
  sprite.scale.set(2.6, 0.55, 1)
  // 强制最后画，避免与设备/连接线/树遮挡竞争
  sprite.renderOrder = 100
  return sprite
}

/**
 * 连接线（架空线/电缆）— 弧形
 */
export function buildConnectionLine(p1, p2, color = 0x8B95A1) {
  const v1 = new THREE.Vector3(...p1)
  const v2 = new THREE.Vector3(...p2)
  // 中点抬高（弧形更明显，避开设备几何）
  const mid = v1.clone().add(v2).multiplyScalar(0.5)
  const dist = v1.distanceTo(v2)
  mid.y += Math.max(0.8, dist * 0.2)
  const curve = new THREE.QuadraticBezierCurve3(v1, mid, v2)
  const pts = curve.getPoints(16)
  const geo = new THREE.BufferGeometry().setFromPoints(pts)
  // 关键：transparent: false — LineBasicMaterial 透明会 z-sort 闪烁
  const mat = new THREE.LineBasicMaterial({ color, transparent: false })
  return new THREE.Line(geo, mat)
}

/**
 * 释放共享材质（场景销毁时调用）
 */
export function disposeMaterials() {
  for (const m of _matCache.values()) {
    if (m.dispose) m.dispose()
  }
  _matCache.clear()
}
