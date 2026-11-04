<script setup lang="ts">
/**
 * Hero 3D 粒子背景（three.js）：
 * - 双层粒子场（外球壳 + 内近景晕），主色系渐变、加法混合、圆形光晕贴图
 * - 慢速自转 + 粒子正弦波动 + 鼠标视差（lerp 平滑）
 * - 性能：IntersectionObserver 懒启动、移动端降采样/降粒子数、
 *   prefers-reduced-motion 静态化、卸载时 dispose 全部 GPU 资源
 * - WebGL 不可用时静默渲染空（HomeView 的 CSS 渐变光晕兜底）
 */
import { onMounted, onUnmounted, ref } from 'vue'
import * as THREE from 'three'

const el = ref<HTMLDivElement | null>(null)

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
let points: THREE.Points | null = null
let innerPoints: THREE.Points | null = null
let basePositions: Float32Array | null = null
let phases: Float32Array | null = null
let glowTexture: THREE.Texture | null = null
let raf = 0
let disposed = false
let reduced = false
let io: IntersectionObserver | null = null
let ro: ResizeObserver | null = null
let targetX = 0
let targetY = 0
let mouseX = 0
let mouseY = 0

function webglAvailable(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')))
  } catch {
    return false
  }
}

/** 圆形光晕贴图：Canvas 生成，无外部资源 */
function makeGlowTexture(): THREE.Texture {
  const canvas = document.createElement('canvas')
  canvas.width = 64
  canvas.height = 64
  const ctx = canvas.getContext('2d')
  if (ctx) {
    const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32)
    g.addColorStop(0, 'rgba(255,255,255,1)')
    g.addColorStop(0.35, 'rgba(255,255,255,0.45)')
    g.addColorStop(1, 'rgba(255,255,255,0)')
    ctx.fillStyle = g
    ctx.fillRect(0, 0, 64, 64)
  }
  return new THREE.CanvasTexture(canvas)
}

/** 球壳随机点 + 主色系随机插值颜色 */
function buildShell(count: number, rMin: number, rMax: number): { positions: Float32Array; colors: Float32Array } {
  const positions = new Float32Array(count * 3)
  const colors = new Float32Array(count * 3)
  const c1 = new THREE.Color('#4176e6')
  const c2 = new THREE.Color('#679efe')
  for (let i = 0; i < count; i++) {
    const r = rMin + Math.random() * (rMax - rMin)
    const theta = Math.random() * Math.PI * 2
    const phi = Math.acos(2 * Math.random() - 1)
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta)
    positions[i * 3 + 1] = r * Math.cos(phi)
    positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta)
    const col = c1.clone().lerp(c2, Math.random())
    colors[i * 3] = col.r
    colors[i * 3 + 1] = col.g
    colors[i * 3 + 2] = col.b
  }
  return { positions, colors }
}

function buildScene(container: HTMLDivElement): boolean {
  if (!webglAvailable()) return false
  try {
    return buildSceneUnsafe(container)
  } catch {
    // WebGL 初始化/渲染异常：静默降级（HomeView CSS 光晕兜底），绝不外泄
    cleanupScene()
    return false
  }
}

function buildSceneUnsafe(container: HTMLDivElement): boolean {

  const isMobile = window.innerWidth <= 768
  const outerCount = isMobile ? 900 : 4200
  const innerCount = isMobile ? 200 : 700

  renderer = new THREE.WebGLRenderer({
    alpha: true,
    antialias: !isMobile,
    powerPreference: 'low-power',
  })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, isMobile ? 1.5 : 2))
  renderer.setSize(container.clientWidth || window.innerWidth, container.clientHeight || 560)
  const canvasEl = renderer.domElement
  canvasEl.style.position = 'absolute'
  canvasEl.style.inset = '0'
  canvasEl.style.display = 'block'
  canvasEl.style.pointerEvents = 'none'
  container.appendChild(canvasEl)

  scene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(60, (container.clientWidth || 1) / (container.clientHeight || 1), 0.1, 100)
  camera.position.z = 24

  glowTexture = makeGlowTexture()

  // 外层：大球壳（慢自转 + 波动）
  const outer = buildShell(outerCount, 9, 19)
  const outerGeo = new THREE.BufferGeometry()
  outerGeo.setAttribute('position', new THREE.BufferAttribute(outer.positions, 3))
  outerGeo.setAttribute('color', new THREE.BufferAttribute(outer.colors, 3))
  const outerMat = new THREE.PointsMaterial({
    size: 0.32,
    map: glowTexture,
    vertexColors: true,
    transparent: true,
    opacity: 0.9,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    sizeAttenuation: true,
  })
  points = new THREE.Points(outerGeo, outerMat)
  scene.add(points)

  // 内层：近景小晕（反向旋转，更亮）
  const inner = buildShell(innerCount, 3, 8)
  const innerGeo = new THREE.BufferGeometry()
  innerGeo.setAttribute('position', new THREE.BufferAttribute(inner.positions, 3))
  innerGeo.setAttribute('color', new THREE.BufferAttribute(inner.colors, 3))
  const innerMat = new THREE.PointsMaterial({
    size: 0.16,
    map: glowTexture,
    vertexColors: true,
    transparent: true,
    opacity: 0.85,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    sizeAttenuation: true,
  })
  innerPoints = new THREE.Points(innerGeo, innerMat)
  scene.add(innerPoints)

  // 波动相位（每粒子一个随机相位）
  basePositions = outer.positions.slice()
  phases = new Float32Array(outerCount)
  for (let i = 0; i < outerCount; i++) phases[i] = Math.random() * Math.PI * 2

  reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!reduced) {
    window.addEventListener('pointermove', onPointerMove, { passive: true })
    startLoop()
  } else {
    renderOnce()
  }
  return true
}

function onPointerMove(e: PointerEvent) {
  targetX = (e.clientX / window.innerWidth) * 2 - 1
  targetY = (e.clientY / window.innerHeight) * 2 - 1
}

function renderOnce() {
  if (renderer && scene && camera) renderer.render(scene, camera)
}

function startLoop() {
  const tick = () => {
    if (disposed) return
    raf = requestAnimationFrame(tick)
    const t = performance.now() * 0.00012
    if (points && innerPoints) {
      points.rotation.y = t * 0.7
      innerPoints.rotation.y = -t * 0.9
      innerPoints.rotation.x = Math.sin(t * 0.5) * 0.1

      // 鼠标视差（lerp 平滑）
      mouseX += (targetX - mouseX) * 0.045
      mouseY += (targetY - mouseY) * 0.045
      points.rotation.y += mouseX * 0.08
      points.rotation.x += mouseY * 0.05
      innerPoints.rotation.y += mouseX * 0.05

      // 外层粒子 z 轴波动（CPU 更新 attribute，顶点数小开销可忽略）
      const pos = points.geometry.attributes.position as THREE.BufferAttribute
      const arr = pos.array as Float32Array
      const base = basePositions as Float32Array
      const ph = phases as Float32Array
      const wave = Math.sin(t * 1.4)
      for (let i = 0, j = 0; i < arr.length; i += 3, j++) {
        arr[i + 2] = base[i + 2] + wave * Math.sin(t * 2.2 + ph[j]) * 0.28
      }
      pos.needsUpdate = true
    }
    renderOnce()
  }
  raf = requestAnimationFrame(tick)
}

function onResize() {
  const container = el.value
  if (!container || !renderer || !camera) return
  const w = container.clientWidth
  const h = container.clientHeight
  if (!w || !h) return
  renderer.setSize(w, h)
  camera.aspect = w / h
  camera.updateProjectionMatrix()
}

/** 释放全部 GPU 资源（卸载与初始化失败共用） */
function cleanupScene() {
  points?.geometry.dispose()
  innerPoints?.geometry.dispose()
  ;(points?.material as THREE.Material | undefined)?.dispose()
  ;(innerPoints?.material as THREE.Material | undefined)?.dispose()
  glowTexture?.dispose()
  renderer?.dispose()
  renderer?.domElement.remove()
  renderer = null
  points = null
  innerPoints = null
  scene = null
  camera = null
  basePositions = null
  phases = null
}

onMounted(() => {
  const container = el.value
  if (!container) return

  ro = new ResizeObserver(onResize)
  ro.observe(container)

  // 懒启动：进入视口才构建 WebGL 场景
  io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && !renderer) {
          buildScene(container)
          io?.disconnect()
        }
      }
    },
    { rootMargin: '200px' },
  )
  io.observe(container)
})

onUnmounted(() => {
  disposed = true
  cancelAnimationFrame(raf)
  io?.disconnect()
  ro?.disconnect()
  window.removeEventListener('pointermove', onPointerMove)
  cleanupScene()
})
</script>

<template>
  <div
    ref="el"
    class="hero-scene"
    aria-hidden="true"
  />
</template>

<style scoped>
.hero-scene {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
}
</style>
