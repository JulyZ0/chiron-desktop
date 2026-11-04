/**
 * 朗读（Web Speech API 封装）。
 *
 * 走浏览器原生 `speechSynthesis`：不需要后端往返、无网络延迟、离线可用。
 * 代价是**音色列表来自用户操作系统** —— 同一份偏好换台机器后可能匹配不到同名
 * 音色，所以 resolveVoice 在找不到时回退到默认音色，而不是静默不发声。
 *
 * 音色列表是异步就绪的（Chrome 首次 getVoices() 常返回空数组，要等
 * `voiceschanged`），因此这里把"是否已有音色"也暴露出去，让设置界面能正确
 * 显示加载态，而不是渲染一个空下拉框。
 */
import { computed, onUnmounted, ref } from 'vue'

export interface SpeechPrefs {
  /** 系统音色标识（voiceURI）；空 = 用系统默认 */
  voiceURI?: string
  /** 语速 0.5–2.0 */
  rate?: number
  /** 音调 0–2 */
  pitch?: number
}

const isSupported = typeof window !== 'undefined' && 'speechSynthesis' in window

export function useSpeech() {
  const voices = ref<SpeechSynthesisVoice[]>([])
  const speaking = ref(false)
  const supported = isSupported

  function loadVoices() {
    if (!supported) return
    voices.value = window.speechSynthesis.getVoices()
  }

  if (supported) {
    loadVoices()
    // 音色列表异步就绪；重复注册由调用方的组件生命周期保证只发生一次
    window.speechSynthesis.addEventListener('voiceschanged', loadVoices)
  }

  onUnmounted(() => {
    if (!supported) return
    window.speechSynthesis.removeEventListener('voiceschanged', loadVoices)
    window.speechSynthesis.cancel()
  })

  /** 按偏好挑音色：找不到同名（换设备/系统更新）时回退到系统默认 */
  function resolveVoice(prefs: SpeechPrefs): SpeechSynthesisVoice | null {
    if (!prefs.voiceURI) return null
    return voices.value.find(v => v.voiceURI === prefs.voiceURI) ?? null
  }

  /**
   * 朗读一段文本。返回是否真的发起（被拦、文本为空、不支持都是 false）。
   * 朗读前先 cancel：连续点击不该把多段内容排进队列。
   */
  function speak(text: string, prefs: SpeechPrefs = {}): boolean {
    const content = (text || '').trim()
    if (!supported || !content) return false
    window.speechSynthesis.cancel()
    const utter = new SpeechSynthesisUtterance(content)
    const voice = resolveVoice(prefs)
    if (voice) utter.voice = voice
    if (prefs.rate) utter.rate = prefs.rate
    if (prefs.pitch) utter.pitch = prefs.pitch
    utter.onstart = () => { speaking.value = true }
    utter.onend = () => { speaking.value = false }
    utter.onerror = () => { speaking.value = false }
    window.speechSynthesis.speak(utter)
    return true
  }

  function stop() {
    if (!supported) return
    window.speechSynthesis.cancel()
    speaking.value = false
  }

  /** 按语言分组，便于设置界面里扫读（系统音色动辄上百个） */
  const voicesByLang = computed(() => {
    const groups = new Map<string, SpeechSynthesisVoice[]>()
    for (const v of voices.value) {
      const lang = v.lang || 'other'
      const list = groups.get(lang) ?? []
      list.push(v)
      groups.set(lang, list)
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  })

  return { supported, voices, voicesByLang, speaking, speak, stop, resolveVoice }
}
