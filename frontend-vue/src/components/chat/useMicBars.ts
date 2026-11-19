/**
 * 录音时的 6 频段电平（CDS v1 §5.1.3 的 LiveMicActivityBars）。
 *
 * **一次只开一路麦克风**：录音本身（`MediaRecorder`）要用 `getUserMedia`，这里
 * 接受调用方把**同一个** `MediaStream` 传进来画波形。此前是各开一路 —— 等于为纯视觉
 * 效果多占一个麦克风实例，某些驱动会因此让录音拿到静音轨（"波形在动、转写是空的"）。
 *
 * 数值全部取自手册：512 点 FFT、smoothing 0.5、−70/−10 dB 截断、8% 噪声门、
 * 0.7 次幂 gamma 压缩（放大细语、压制爆音，让动态范围分布均匀），映射到 4–24px。
 */
import { onBeforeUnmount } from 'vue'

const FFT_SIZE = 512
const SMOOTHING = 0.5
const MIN_DB = -70
const MAX_DB = -10
/** 8% 噪声门：彻底滤掉空调底噪引起的虚假微动 */
const NOISE_FLOOR = 0.08
/** gamma 压缩指数 */
const GAMMA = 0.7
/** 压缩后的增益（手册 1.5） */
const GAIN = 1.5

export const MIC_BAND_COUNT = 6

export interface MicBars {
  /**
   * @param stream 录音那一路麦克风流。**必须传**：再开一路会让两个消费者抢同一设备，
   *   部分驱动下录音轨变成静音。不传时退化为自己开一路（仅向后兼容）。
   */
  start: (stream?: MediaStream) => Promise<void>
  stop: () => void
}

/**
 * @param onLevels 每帧回调一次，参数是 6 个 0..1 的电平。
 *   刻意不给响应式数组：60fps 改 6 个数字不该触发整个 Composer 重渲染，
 *   调用方应直接用 DOM 写 `--level`（见 ChatInput 的 barEls）。
 */
export function useMicBars(onLevels: (levels: number[]) => void): MicBars {
  let stream: MediaStream | null = null
  /**
   * 当前这条流是不是本模块自己 `getUserMedia` 拿到的。
   *
   * 只能靠这个标志决定 `stop()` 要不要关轨道：外部传入的流属于录音
   * （`MediaRecorder`），关掉它等于"停止画波形顺带把录音掐了"。
   */
  let ownsStream = false
  let ctx: AudioContext | null = null
  let analyser: AnalyserNode | null = null
  let data: Uint8Array<ArrayBuffer> | null = null
  let raf = 0

  function tick() {
    if (!analyser || !data) return
    analyser.getByteFrequencyData(data)
    const bands = new Array<number>(MIC_BAND_COUNT).fill(0)
    const per = Math.max(1, Math.floor(data.length / MIC_BAND_COUNT))
    for (let b = 0; b < MIC_BAND_COUNT; b++) {
      let sum = 0
      for (let i = b * per; i < (b + 1) * per; i++) sum += data[i] ?? 0
      const raw = sum / per / 255
      const gated = Math.max(0, raw - NOISE_FLOOR) / (1 - NOISE_FLOOR)
      bands[b] = Math.min(1, Math.pow(gated, GAMMA) * GAIN)
    }
    onLevels(bands)
    raf = requestAnimationFrame(tick)
  }

  async function start(external?: MediaStream) {
    try {
      // 优先用调用方给的流（录音自己开的那一路）：不复制、不另开，停止时不关它的轨
      // —— 那些轨属于录音，由录音在收尾时统一停。
      if (external) {
        stream = external
        ownsStream = false
      } else {
        if (!navigator.mediaDevices?.getUserMedia) return
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        ownsStream = true
      }
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) {
        // 没有 AudioContext：画不了波形。自己开的流要还回去，外部的不动。
        if (ownsStream) stop()
        else stream = null
        return
      }
      ctx = new Ctor()
      analyser = ctx.createAnalyser()
      analyser.fftSize = FFT_SIZE
      analyser.smoothingTimeConstant = SMOOTHING
      analyser.minDecibels = MIN_DB
      analyser.maxDecibels = MAX_DB
      ctx.createMediaStreamSource(stream).connect(analyser)
      data = new Uint8Array(analyser.frequencyBinCount)
      raf = requestAnimationFrame(tick)
    } catch {
      // 拿不到流：静默退化为"没有波形"。录音与转写不依赖这里。
      if (ownsStream) stop()
      else stream = null
    }
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    analyser = null
    data = null
    // 只停**自己开的**流：外部传入的那路属于录音（MediaRecorder），
    // 在这里关掉会让"停止画波形"顺带把录音也掐了。
    if (ownsStream) stream?.getTracks().forEach(track => track.stop())
    stream = null
    ownsStream = false
    if (ctx) {
      void ctx.close().catch(() => { /* 已关闭 */ })
      ctx = null
    }
    // 归零，避免波形停在最后一帧的高度上
    onLevels(new Array<number>(MIC_BAND_COUNT).fill(0))
  }

  onBeforeUnmount(stop)
  return { start, stop }
}
