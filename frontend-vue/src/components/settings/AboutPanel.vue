<script setup lang="ts">
/**
 * 设置页「关于」标签页。
 *
 * 只放**可以公开查看的事实**：产品定位、两个源码仓库的地址。
 * 不放版本号：构建期没有注入版本常量（`vite.config.ts` 没有 `define`），
 * 为了显示一个版本号去改构建配置不值得 —— 版本在安装包文件名与
 * 安装器里已经能查到。
 *
 * ## 链接为什么用普通 `<a>` 而不是 `shell.openExternal`
 *
 * 主进程已经给主窗口装了 `setWindowOpenHandler` 与 `will-navigate`：
 * 任何 `https?:` 的 `target="_blank"` 或页面跳转都会被交给系统浏览器，
 * 应用内不会开新窗口。所以这里不需要 IPC，也不会把用户困在应用里。
 */
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

/** 两个仓库：主仓库 + 桌面端仓库 */
const REPOS = [
  { key: 'main', url: 'https://github.com/Athenavi/chiron' },
  { key: 'desktop', url: 'https://github.com/JulyZ0/chiron-desktop' },
] as const
</script>

<template>
  <div class="about-panel">
    <div class="about-block">
      <div class="about-title">
        {{ $t('settings.about_title') }}
      </div>
      <p class="about-desc">
        {{ $t('settings.about_desc') }}
      </p>
    </div>

    <div class="about-block">
      <div class="about-title">
        {{ $t('settings.about_repos') }}
      </div>
      <p class="about-desc">
        {{ $t('settings.about_repos_hint') }}
      </p>
      <ul class="about-links">
        <li v-for="repo in REPOS" :key="repo.key">
          <span class="about-link-label">{{ $t(`settings.about_repo_${repo.key}`) }}</span>
          <a
            :href="repo.url"
            target="_blank"
            rel="noopener noreferrer"
            class="about-link"
          >{{ repo.url }}</a>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.about-block { padding-block: 4px 20px; }
.about-block + .about-block { border-top: var(--hairline-width) solid var(--border-card, var(--border)); padding-top: 20px; }
.about-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 12px; }
.about-desc { margin: 0 0 12px; color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.about-links { margin: 0; padding-inline-start: 20px; color: var(--text-secondary); font-size: 13px; }
.about-links li { margin-bottom: 8px; }
.about-link-label { margin-inline-end: 8px; color: var(--text-primary); }
.about-link {
  color: var(--color-primary, #1677ff);
  overflow-wrap: anywhere;
  text-decoration: none;
}
.about-link:hover { text-decoration: underline; }
</style>
