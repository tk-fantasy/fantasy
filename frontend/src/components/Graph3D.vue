<template>
  <div ref="container" class="graph-container"></div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

const props = defineProps({
  graphData: { type: Object, required: true },
  highlightNodes: { type: Set, default: () => new Set() },
  highlightLinks: { type: Set, default: () => new Set() },
})

const emit = defineEmits(['node-click'])
const container = ref(null)

let scene, camera, renderer, controls, raycaster
let sphereGroup, nodeGroup, linkGroup, dotTexture
let nodeSprites = [], nodeInfo = []
let selectedNodeId = null, animFrame = null, linkOpacity = 0
let orbitsFrame = null, snowFrame = null

const RADIUS = 200, NODE_SCALE = 8, HIGHLIGHT_SCALE = 13
const BG_DARK = 0x0e0e14
let isLightMode = false

function isLight() {
  return !document.documentElement.classList.contains('light-mode') ? false : true
}

function applyTheme() {
  const light = isLight()
  if (light === isLightMode) return
  isLightMode = light
  if (!scene) return
  // light mode: canvas 透明，让 CSS sunOrbit 透出来
  scene.background = light ? null : new THREE.Color(BG_DARK)
  // 球体
  if (sphereGroup) {
    const children = sphereGroup.children
    if (children[0]) { // 主球体
      children[0].material.color.setHex(light ? 0xe8e8f0 : 0x1a1a2e)
      children[0].material.emissive.setHex(light ? 0xd0d0e0 : 0x0a0a1e)
      children[0].material.opacity = light ? 0.4 : 0.55
    }
    if (children[1]) children[1].material.color.setHex(light ? 0xbbbbdd : 0x2a2a5e) // 线框
    if (children[2]) children[2].material.color.setHex(light ? 0x99aacc : 0x6688cc) // 辉光
    if (children[3]) children[3].material.color.setHex(light ? 0x8899bb : 0x4466aa) // 外晕
  }
  // 星空 — 白色背景看不见，隐藏
  const stars = scene.getObjectByName('starfield')
  if (stars) stars.visible = !light
  // 雪花
  const snow = scene.getObjectByName('snowfield')
  if (snow) {
    snow.material.blending = light ? THREE.NormalBlending : THREE.AdditiveBlending
    snow.material.color.setHex(light ? 0x5577bb : 0xaaccff)
    snow.material.opacity = light ? 0.5 : 0.6
  }
  // 节点 — light mode 用 NormalBlending + 深色，dark mode 用 AdditiveBlending + 亮色
  const blending = light ? THREE.NormalBlending : THREE.AdditiveBlending
  nodeSprites.forEach(s => {
    s.material.map = dotTexture
    s.material.blending = blending
    const hsl = s.material.color.getHSL({})
    s.material.color.setHSL(hsl.h, hsl.s, light ? 0.4 : 0.55)
    s.material.needsUpdate = true
  })
}

let themeObserver = null

function init() {
  const w = container.value.clientWidth, h = container.value.clientHeight
  scene = new THREE.Scene()
  isLightMode = isLight()
  scene.background = isLightMode ? null : new THREE.Color(BG_DARK)

  camera = new THREE.PerspectiveCamera(50, w / h, 1, 2000)
  camera.position.set(0, RADIUS * 0.6, RADIUS * 2.8)
  camera.lookAt(0, 0, 0)

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.setSize(w, h)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  container.value.appendChild(renderer.domElement)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.08
  controls.minDistance = RADIUS * 1.2
  controls.maxDistance = RADIUS * 6
  controls.autoRotate = true
  controls.autoRotateSpeed = 0.8
  controls.target.set(0, RADIUS * 0.3, 0)

  raycaster = new THREE.Raycaster()
  raycaster.params.Sprite = true

  scene.add(new THREE.AmbientLight(0x404060, 0.6))
  const d1 = new THREE.DirectionalLight(0xffffff, 1.2); d1.position.set(1, 1, 1); scene.add(d1)
  const d2 = new THREE.DirectionalLight(0x4488ff, 0.4); d2.position.set(-1, -0.5, -1); scene.add(d2)

  // 白色纹理（深色背景用）
  const cv = document.createElement('canvas'); cv.width = cv.height = 64
  const ctx = cv.getContext('2d')
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 30)
  g.addColorStop(0, 'rgba(255,255,255,1)')
  g.addColorStop(0.4, 'rgba(255,255,255,0.9)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = g; ctx.fillRect(0, 0, 64, 64)
  dotTexture = new THREE.CanvasTexture(cv)

  buildGlobe(); buildNodes(); addStars(); addPlanets(); addSnowfall(); animate()
  renderer.domElement.addEventListener('click', onCanvasClick)
  window.addEventListener('resize', onResize)
}

function buildGlobe() {
  sphereGroup = new THREE.Group(); scene.add(sphereGroup)
  const geo = new THREE.SphereGeometry(RADIUS, 48, 36)
  const mat = new THREE.MeshPhongMaterial({ color: 0x1a1a2e, emissive: 0x0a0a1e, emissiveIntensity: 0.3, specular: 0x444488, shininess: 20, transparent: true, opacity: 0.55, depthWrite: false })
  const mesh = new THREE.Mesh(geo, mat); mesh.raycast = () => {}; sphereGroup.add(mesh)
  const wg = new THREE.SphereGeometry(RADIUS * 1.001, 24, 18)
  const wm = new THREE.MeshBasicMaterial({ color: 0x2a2a5e, wireframe: true, transparent: true, opacity: 0.08, depthWrite: false })
  const w = new THREE.Mesh(wg, wm); w.raycast = () => {}; sphereGroup.add(w)
  const gg = new THREE.SphereGeometry(RADIUS * 1.02, 32, 24)
  const gm = new THREE.MeshBasicMaterial({ color: 0x6688cc, transparent: true, opacity: 0.08, side: THREE.BackSide })
  const gl = new THREE.Mesh(gg, gm); gl.raycast = () => {}; sphereGroup.add(gl)
  // Outer halo
  const hg = new THREE.SphereGeometry(RADIUS * 1.12, 32, 24)
  const hm = new THREE.MeshBasicMaterial({ color: 0x4466aa, transparent: true, opacity: 0.04, side: THREE.BackSide })
  const hl = new THREE.Mesh(hg, hm); hl.raycast = () => {}; sphereGroup.add(hl)
}

function addStars() {
  const count = 2000
  const positions = new Float32Array(count * 3), colors = new Float32Array(count * 3), sizes = new Float32Array(count)
  for (let i = 0; i < count; i++) {
    const r = RADIUS * (4 + Math.random() * 20)
    const theta = Math.random() * Math.PI * 2
    const phi = Math.acos(2 * Math.random() - 1)
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta)
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta)
    positions[i * 3 + 2] = r * Math.cos(phi)
    const c = 0.5 + Math.random() * 0.5
    const tint = Math.random()
    if (tint < 0.1) { colors[i * 3] = c; colors[i * 3 + 1] = c * 0.7; colors[i * 3 + 2] = c * 0.5 }
    else if (tint < 0.2) { colors[i * 3] = c * 0.6; colors[i * 3 + 1] = c * 0.7; colors[i * 3 + 2] = c }
    else { colors[i * 3] = c; colors[i * 3 + 1] = c; colors[i * 3 + 2] = c }
    sizes[i] = 0.5 + Math.random() * 1.5
  }
  const geo = new THREE.BufferGeometry()
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geo.setAttribute('color', new THREE.BufferAttribute(colors, 3))
  geo.setAttribute('size', new THREE.BufferAttribute(sizes, 1))
  const mat = new THREE.PointsMaterial({ size: 1.2, vertexColors: true, transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true })
  const stars = new THREE.Points(geo, mat); stars.name = 'starfield'; stars.visible = !isLightMode; scene.add(stars)
}

function addPlanets() {
  const orbits = []
  for (let i = 0; i < 8; i++) {
    const radius = RADIUS * (1.8 + Math.random() * 4), speed = 0.0003 + Math.random() * 0.0006
    const angle = Math.random() * Math.PI * 2, tilt = (Math.random() - 0.5) * 0.8, size = 2 + Math.random() * 6
    const r = 0.3 + Math.random() * 0.5, g = 0.2 + Math.random() * 0.4, b = 0.4 + Math.random() * 0.5
    const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(r, g, b), transparent: true, opacity: 0.6 + Math.random() * 0.3 })
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(size, 10, 10), mat)
    mesh.position.set(radius * Math.cos(angle), radius * tilt, radius * Math.sin(angle))
    scene.add(mesh); orbits.push({ mesh, radius, speed, tilt, angle })
  }
  ;(function animate() {
    for (const o of orbits) { o.angle += o.speed; o.mesh.position.x = o.radius * Math.cos(o.angle); o.mesh.position.z = o.radius * Math.sin(o.angle); o.mesh.position.y = o.radius * o.tilt; o.mesh.rotation.y += 0.005; o.mesh.rotation.x += 0.002 }
    orbitsFrame = requestAnimationFrame(animate)
  })()
}

function addSnowfall() {
  const count = 600
  const positions = new Float32Array(count * 3)
  const velocities = new Float32Array(count)
  for (let i = 0; i < count; i++) {
    const r = RADIUS * (2.5 + Math.random() * 8)
    const theta = Math.random() * Math.PI * 2
    const phi = Math.acos(2 * Math.random() - 1)
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta)
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta)
    positions[i * 3 + 2] = r * Math.cos(phi)
    velocities[i] = 0.3 + Math.random() * 1.2
  }
  const geo = new THREE.BufferGeometry()
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  const mat = new THREE.PointsMaterial({
    size: 2.5,
    color: isLightMode ? 0x4466aa : 0xaaccff,
    transparent: true,
    opacity: isLightMode ? 0.4 : 0.6,
    blending: isLightMode ? THREE.NormalBlending : THREE.AdditiveBlending,
    depthWrite: false,
    sizeAttenuation: true,
  })
  const snow = new THREE.Points(geo, mat)
  snow.name = 'snowfield'
  scene.add(snow)
  ;(function animateSnow() {
    const pos = snow.geometry.attributes.position.array
    for (let i = 0; i < count; i++) {
      pos[i * 3 + 1] -= velocities[i] * 0.15
      pos[i * 3] += Math.sin(Date.now() * 0.0005 + i) * 0.05
      pos[i * 3 + 2] += Math.cos(Date.now() * 0.0006 + i) * 0.05
      if (pos[i * 3 + 1] < -RADIUS * 6) {
        const r = RADIUS * (2.5 + Math.random() * 8)
        const theta = Math.random() * Math.PI * 2
        const phi = Math.acos(2 * Math.random() - 1)
        pos[i * 3] = r * Math.sin(phi) * Math.cos(theta)
        pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta)
        pos[i * 3 + 2] = r * Math.cos(phi)
        velocities[i] = 0.3 + Math.random() * 1.2
      }
    }
    snow.geometry.attributes.position.needsUpdate = true
    snowFrame = requestAnimationFrame(animateSnow)
  })()
}

function buildNodes() {
  const nodes = props.graphData.nodes; if (!nodes.length) return
  nodeGroup = new THREE.Group(); scene.add(nodeGroup)

  // ── 1. 百分位裁剪抗离群点：取 [5%, 95%] 分位作 min/max，离群点后续钳制 ──
  const xs = nodes.map(n => n.x || 0), ys = nodes.map(n => n.y || 0)
  const zs = nodes.map(n => (n.z !== undefined ? n.z : 0))  // 兼容旧产物缺 z

  function percentile(arr, p) {
    const s = [...arr].sort((a, b) => a - b)
    const idx = Math.min(s.length - 1, Math.max(0, Math.round((p / 100) * (s.length - 1))))
    return s[idx]
  }
  const xMin = percentile(xs, 5), xMax = percentile(xs, 95)
  const yMin = percentile(ys, 5), yMax = percentile(ys, 95)
  const zMin = percentile(zs, 5), zMax = percentile(zs, 95)

  // ── 2. 统一缩放保形状：三轴共用最大 span，不做非等比拉伸 ──
  const xCenter = (xMin + xMax) / 2, yCenter = (yMin + yMax) / 2, zCenter = (zMin + zMax) / 2
  const xSpan = (xMax - xMin) || 1, ySpan = (yMax - yMin) || 1, zSpan = (zMax - zMin) || 1
  const maxSpan = Math.max(xSpan, ySpan, zSpan)
  const R = RADIUS * 0.9

  nodes.forEach(n => {
    // 统一缩放（保 UMAP 真实几何），归一到 [-1,1] 区间
    let nx = ((n.x - xCenter) / maxSpan) * 2
    let ny = ((n.y - yCenter) / maxSpan) * 2
    let nz = (((n.z !== undefined ? n.z : 0) - zCenter) / maxSpan) * 2
    // 越界点（含离群点）钳制回单位球
    const dist = Math.sqrt(nx * nx + ny * ny + nz * nz)
    if (dist > 1) { nx /= dist; ny /= dist; nz /= dist }
    const px = nx * R, py = ny * R, pz = nz * R

    // ── 3. 3D 配色：用球坐标方位角，z 维度参与配色 ──
    // theta = xy 平面方位角，phi = 与 z 轴夹角；hue 由 theta 定，phi 微调亮度
    const theta = Math.atan2(ny, nx)
    const phi = Math.acos(Math.max(-1, Math.min(1, nz / (dist || 1))))
    const hue = ((theta + Math.PI) / (Math.PI * 2)) * 300
    const phiFactor = 1 - phi / Math.PI  // 北极=1 南极=0
    const baseLight = isLightMode ? 0.4 : 0.55
    const lightness = baseLight + (phiFactor - 0.5) * 0.15  // ±7.5% 亮度差

    const mat = new THREE.SpriteMaterial({ map: dotTexture, color: new THREE.Color(`hsl(${hue}, 85%, ${lightness * 100}%)`), transparent: true, depthTest: false, depthWrite: false, blending: isLightMode ? THREE.NormalBlending : THREE.AdditiveBlending })
    const sprite = new THREE.Sprite(mat)
    sprite.position.set(px, py, pz); sprite.scale.set(NODE_SCALE, NODE_SCALE, 1); sprite.renderOrder = 1
    sprite.userData = { id: n.id }
    nodeGroup.add(sprite); nodeSprites.push(sprite); nodeInfo.push({ ...n, px, py, pz, hue, sprite })
  })
}

function showLinks(nodeId) {
  if (linkGroup) { scene.remove(linkGroup); linkGroup.traverse(c => { if (c.geometry) c.geometry.dispose(); if (c.material) c.material.dispose() }); linkGroup = null }
  const src = nodeInfo.find(n => n.id === nodeId); if (!src) return
  const links = props.graphData.links.filter(l => l.source === nodeId || l.target === nodeId); if (!links.length) return
  const positions = [], colors = []
  for (const l of links) {
    const tgtId = l.source === nodeId ? l.target : l.source
    const tgt = nodeInfo.find(n => n.id === tgtId); if (!tgt) continue
    positions.push(src.px, src.py, src.pz, tgt.px, tgt.py, tgt.pz)
    const c = new THREE.Color(`hsl(${tgt.hue}, 85%, 55%)`); colors.push(c.r, c.g, c.b, c.r, c.g, c.b)
  }
  const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3)); geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
  const mat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0 })
  linkGroup = new THREE.LineSegments(geo, mat); scene.add(linkGroup)
  linkOpacity = 0
  ;(function step() { linkOpacity = Math.min(1, linkOpacity + 0.04); mat.opacity = linkOpacity * 0.7; if (linkOpacity < 1) requestAnimationFrame(step) })()
}

function hideLinks() {
  if (!linkGroup) return
  const mat = linkGroup.material
  ;(function step() { mat.opacity = Math.max(0, mat.opacity - 0.05); if (mat.opacity > 0) requestAnimationFrame(step); else { scene.remove(linkGroup); linkGroup.traverse(c => { if (c.geometry) c.geometry.dispose(); if (c.material) c.material.dispose() }); linkGroup = null } })()
}

function focusNode(id) {
  const node = nodeInfo.find(n => n.id === id); if (!node) return
  controls.autoRotate = false; selectNode(id)
  const target = new THREE.Vector3(node.px, node.py, node.pz)
  const dir = target.clone().normalize()
  const pos = dir.multiplyScalar(RADIUS * 2.2)
  const start = camera.position.clone(); let frame = 0
  ;(function step() { frame++; const t = Math.min(1, frame / 60); const ease = 1 - Math.pow(1 - t, 3); camera.position.lerpVectors(start, pos, ease); controls.target.copy(target); controls.update(); if (t < 1) requestAnimationFrame(step) })()
}
defineExpose({ focusNode })

function selectNode(id) {
  if (selectedNodeId === id) { deselectNode(); return }
  if (selectedNodeId) { const p = nodeInfo.find(n => n.id === selectedNodeId); if (p) p.sprite.scale.set(NODE_SCALE, NODE_SCALE, 1) }
  selectedNodeId = id
  const node = nodeInfo.find(n => n.id === id)
  if (node) { node.sprite.scale.set(HIGHLIGHT_SCALE, HIGHLIGHT_SCALE, 1); emit('node-click', node) }
  showLinks(id)
}

function deselectNode() {
  if (selectedNodeId) { const node = nodeInfo.find(n => n.id === selectedNodeId); if (node) node.sprite.scale.set(NODE_SCALE, NODE_SCALE, 1) }
  selectedNodeId = null; emit('node-click', null); hideLinks()
}

function onCanvasClick(e) {
  const rect = renderer.domElement.getBoundingClientRect()
  const mouse = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1)
  raycaster.setFromCamera(mouse, camera)
  const hits = raycaster.intersectObjects(nodeSprites)
  if (hits.length) selectNode(hits[0].object.userData.id)
}

function animate() {
  if (document.hidden) { animFrame = null; return }
  animFrame = requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera)
}

function onVisibilityChange() {
  if (!document.hidden && !animFrame) animate()
}

function onResize() { const w = container.value.clientWidth, h = container.value.clientHeight; camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h) }

function rebuild() {
  if (linkGroup) { scene.remove(linkGroup); linkGroup.traverse(c => { if (c.geometry) c.geometry.dispose(); if (c.material) c.material.dispose() }); linkGroup = null }
  nodeSprites.forEach(s => { s.material.dispose(); nodeGroup.remove(s) }); nodeSprites = []; nodeInfo = []; selectedNodeId = null
  if (sphereGroup) { scene.remove(sphereGroup); sphereGroup.traverse(c => { if (c.geometry) c.geometry.dispose(); if (c.material) c.material.dispose() }); sphereGroup = null }
  buildGlobe(); buildNodes()
}

onMounted(() => {
  nextTick(() => init())
  themeObserver = new MutationObserver(() => applyTheme())
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})
onUnmounted(() => {
  if (themeObserver) themeObserver.disconnect()
  if (animFrame) cancelAnimationFrame(animFrame)
  if (orbitsFrame) cancelAnimationFrame(orbitsFrame)
  if (snowFrame) cancelAnimationFrame(snowFrame)
  if (renderer) renderer.dispose()
  window.removeEventListener('resize', onResize)
})

watch(() => props.highlightNodes, (ids) => {
  if (!nodeGroup) return
  nodeSprites.forEach(s => { const h = ids.has(s.userData.id); s.scale.set(h ? HIGHLIGHT_SCALE : NODE_SCALE, h ? HIGHLIGHT_SCALE : NODE_SCALE, 1); s.material.opacity = h ? 1 : 0.2 })
  if (ids.size > 0) controls.autoRotate = false
}, { deep: true })

watch(() => props.graphData, () => { if (renderer) rebuild() }, { deep: true })
</script>

<style scoped>
.graph-container { width: 100%; height: 100%; }
</style>
