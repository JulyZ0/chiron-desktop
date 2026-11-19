<script setup lang="ts">
/**
 * SKILL.md 正文预览（Markdown 渲染）。
 *
 * 复用仓库既有的渲染链路 —— `markdown-it` + `DOMPurify`，与 `ShareView.vue`
 * 的公开分享页取自同一套配置（不引入 KaTeX / mermaid：技能正文是说明文档，
 * 不需要公式与图表，少两个重量级依赖的运行时开销）。
 *
 * 为什么单独抽成组件：技能详情是**只读正文**，与聊天消息（流式、思考折叠、
 * 工具卡片）不是一类东西。直接把 `MessageItem` 拖进来会带上一整套会话状态，
 * 而把 `renderMarkdown` 复制一份又会把安全白名单散成两处 —— 抄一遍就意味着
 * 下次收紧白名单时会漏掉这处。
 */
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = withDefaults(defineProps<{
  /** Markdown 正文（SKILL.md 的 body 部分） */
  source: string
  /** 正文为空时的占位文案 */
  emptyText?: string
}>(), {
  emptyText: '',
})

// 模块级实例：组件在同一时刻只会有一个，没必要每次挂载都重建渲染器与规则表
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

/** 外链统一新窗口打开，并补 rel —— 只放行 http(s)，javascript: 等伪协议由 DOMPurify 拦下 */
const defaultLinkOpen = md.renderer.rules.link_open
  ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, idx, options, env, self)
}

function renderMarkdown(src: string): string {
  try {
    return DOMPurify.sanitize(md.render(src), {
      ALLOWED_TAGS: [
        'p', 'br', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'blockquote',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'table', 'thead', 'tbody',
        'tr', 'th', 'td', 'hr', 'del', 'sup', 'sub',
      ],
      ALLOWED_ATTR: ['href', 'target', 'rel', 'class'],
      ALLOW_DATA_ATTR: false,
      FORBID_TAGS: [
        'style', 'script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea',
        'select', 'marquee', 'link', 'meta', 'img', 'button',
      ],
      FORBID_ATTR: [
        'style', 'onerror', 'onload', 'onclick', 'onmouseover', 'onfocus',
        'onblur', 'onchange', 'onsubmit', 'onreset', 'onkeydown', 'onkeyup', 'onkeypress',
      ],
    })
  } catch {
    return md.utils.escapeHtml(src)
  }
}

const body = computed(() => (props.source || '').trim())
const hasBody = computed(() => body.value.length > 0)
const html = computed(() => (hasBody.value ? renderMarkdown(body.value) : ''))
</script>

<template>
  <div class="skill-doc">
    <!-- 正文来自技能包，经 DOMPurify 白名单过滤后再 v-html（同 ShareView 的口径） -->
    <div
      v-if="hasBody"
      class="skill-doc-body"
      v-html="html"
    />
    <p
      v-else
      class="skill-doc-empty"
    >
      {{ emptyText }}
    </p>
  </div>
</template>

<style scoped>
.skill-doc {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  padding: var(--space-4) var(--space-4) var(--space-5);
  overflow-wrap: anywhere;
}
.skill-doc-empty {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-tertiary);
}

.skill-doc-body {
  font-size: var(--fs-base);
  line-height: var(--lh-relaxed);
  color: var(--text-primary);
}
.skill-doc-body :deep(> *:first-child) { margin-top: 0; }
.skill-doc-body :deep(> *:last-child) { margin-bottom: 0; }
.skill-doc-body :deep(p) { margin: 0 0 var(--space-3); }
.skill-doc-body :deep(h1),
.skill-doc-body :deep(h2),
.skill-doc-body :deep(h3),
.skill-doc-body :deep(h4),
.skill-doc-body :deep(h5),
.skill-doc-body :deep(h6) {
  margin: var(--space-5) 0 var(--space-2);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
  color: var(--text-primary);
}
.skill-doc-body :deep(h1) { font-size: var(--fs-xl); }
.skill-doc-body :deep(h2) { font-size: var(--fs-lg); }
.skill-doc-body :deep(h3) { font-size: var(--fs-md); }
.skill-doc-body :deep(ul),
.skill-doc-body :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-inline-start: var(--space-6);
}
.skill-doc-body :deep(li) { margin: var(--space-1) 0; }
.skill-doc-body :deep(li::marker) { color: var(--text-tertiary); }
.skill-doc-body :deep(a) {
  color: var(--primary);
  text-decoration: none;
  border-bottom: var(--hairline-width) solid color-mix(in srgb, var(--primary) 40%, transparent);
}
.skill-doc-body :deep(a:hover) { border-bottom-color: var(--primary); }
.skill-doc-body :deep(code) {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  padding: 1px var(--space-1);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-primary);
}
.skill-doc-body :deep(pre) {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
  overflow-x: auto;
}
.skill-doc-body :deep(pre code) {
  padding: 0;
  background: transparent;
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
}
.skill-doc-body :deep(blockquote) {
  margin: 0 0 var(--space-3);
  padding: var(--space-1) var(--space-3);
  border-inline-start: 2px solid var(--border-strong);
  color: var(--text-secondary);
}
.skill-doc-body :deep(table) {
  width: 100%;
  margin: 0 0 var(--space-3);
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.skill-doc-body :deep(th),
.skill-doc-body :deep(td) {
  padding: var(--space-2) var(--space-3);
  border: var(--hairline-width) solid var(--border-card);
  text-align: start;
}
.skill-doc-body :deep(th) {
  background: var(--bg-secondary);
  font-weight: var(--fw-medium);
}
.skill-doc-body :deep(hr) {
  margin: var(--space-4) 0;
  border: none;
  border-top: var(--hairline-width) solid var(--border-subtle);
}
</style>
