<script setup lang="ts">
import { computed } from 'vue'
import { Empty, Table, type TableProps } from 'ant-design-vue'

const props = withDefaults(defineProps<{
  dataSource?: TableProps['dataSource']
  emptyText?: string
  pagination?: false | Record<string, unknown>
}>(), {
  dataSource: () => [],
  pagination: () => ({ pageSize: 20, showSizeChanger: false }),
})

const tableData = computed(() => props.dataSource ?? [])
const effectivePagination = computed(() => tableData.value.length === 0 ? false : props.pagination)
</script>

<template>
  <div
    class="admin-table admin-table-scroll"
    role="region"
    :aria-label="$t('admin.table_label')"
    tabindex="0"
  >
    <Table
      v-bind="$attrs"
      size="middle"
      :data-source="tableData"
      :pagination="effectivePagination"
    >
      <template #bodyCell="slotProps">
        <slot
          name="bodyCell"
          v-bind="slotProps"
        />
      </template>
      <template #emptyText>
        <slot name="emptyText">
          <Empty
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            :description="emptyText"
          />
        </slot>
      </template>
    </Table>
  </div>
</template>

<style scoped>
.admin-table-scroll {
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-inline: contain;
  scrollbar-width: thin;
  padding: 2px;
  margin: -2px;
}

.admin-table-scroll:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: -2px;
}

@media (max-width: 768px) {
  .admin-table-scroll :deep(.ant-table) {
    min-width: 600px;
  }
}
</style>
