<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import AdminPage from '@/components/admin/AdminPage.vue'
import RedisManagementView from './RedisManagementView.vue'
import DatabaseManagementView from './DatabaseManagementView.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// 数据存储：Redis（Keyspace/慢日志/flush）+ 数据库（状态/查询/备份恢复/优化）
const tabs = [
  { key: 'redis', label: 'Redis', comp: RedisManagementView },
  { key: 'database', label: t('admin.database'), comp: DatabaseManagementView },
]
</script>

<template>
  <AdminPage :title="t('common.data_storage')">
    <AdminTabbedView
      route-path="/admin/datastores"
      default-tab="redis"
      :tabs="tabs"
    />
  </AdminPage>
</template>
