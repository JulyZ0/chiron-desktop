<script setup lang="ts">
/**
 * 后台「引擎功能开关」。
 *
 * 原先是用户设置里的一个页签，但它改的是**整台机器的引擎进程环境**（白名单内的
 * 功能开关），属于部署/运维面而不是个人偏好 —— 因此搬到后台，与其它系统配置相邻。
 * 具体读写仍由桌面壳的 IPC 完成（`engine-env.json` + 重启本地后端），组件原样复用。
 */
import { Card } from 'ant-design-vue'
import EngineEnvPanel from '../../components/settings/EngineEnvPanel.vue'
</script>

<template>
  <div class="engine-toggles-view">
    <Card :bordered="false">
      <EngineEnvPanel />
    </Card>
  </div>
</template>

<style scoped>
.engine-toggles-view {
  max-width: 980px;
  margin: 0 auto;
  padding: 24px;
}
</style>
