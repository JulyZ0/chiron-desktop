import { computed, onBeforeUnmount, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { Modal } from 'ant-design-vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

/** Protect a settings-style editor without blocking same-page query updates (for example Tabs). */
export function useUnsavedChangesGuard(isDirty: MaybeRefOrGetter<boolean>): void {
  useRouter()
  const { t } = useI18n()
  const dirty = computed(() => Boolean(toValue(isDirty)))

  function onBeforeUnload(event: BeforeUnloadEvent): void {
    if (!dirty.value) return
    event.preventDefault()
    event.returnValue = ''
  }

  watch(dirty, (value) => {
    if (value) window.addEventListener('beforeunload', onBeforeUnload)
    else window.removeEventListener('beforeunload', onBeforeUnload)
  }, { immediate: true })

  onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

  onBeforeRouteLeave((to, from) => {
    if (!dirty.value || to.path === from.path) return true
    return new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: t('settings.unsaved_leave_title'),
        content: t('settings.unsaved_leave_body'),
        okText: t('settings.leave_confirm'),
        cancelText: t('settings.stay_on_page'),
        closable: true,
        maskClosable: false,
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      })
    })
  })
}
