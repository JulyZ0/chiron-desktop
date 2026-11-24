<script setup lang="ts">
/**
 * 后台「人格管理」。
 *
 * 原「行为塑形」在用户设置里维护"按模型前缀追加的提示词"，用户既看不见也无法切换。
 * 这里把它收敛成一份**人格库**：管理员增删改（名称/描述/系统提示词/启用/默认），
 * 普通用户在对话里显式选择。预设「猫娘」随迁移写入，可直接编辑。
 *
 * 默认人格每租户最多一条（后端口径）；它只在用户**没有**选择人格时生效。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Empty, Input, Modal, Popconfirm, Space, Switch, Tag, message } from 'ant-design-vue'
import AdminPage from '@/components/admin/AdminPage.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  createPersona,
  deletePersona,
  listAdminPersonas,
  setDefaultPersona,
  updatePersona,
  type Persona,
  type PersonaDraft,
} from '@/api/personas'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const personas = ref<Persona[]>([])

const modalOpen = ref(false)
const editingId = ref('')
const form = reactive<PersonaDraft>({
  name: '',
  description: '',
  system_prompt: '',
  enabled: true,
  is_default: false,
  sort_order: 0,
})

const columns = computed(() => [
  { title: t('admin.persona_col_name'), key: 'name' },
  { title: t('admin.persona_col_description'), key: 'description' },
  { title: t('admin.persona_col_prompt'), key: 'prompt' },
  { title: t('admin.persona_col_enabled'), key: 'enabled', width: 110 },
  { title: t('admin.persona_col_actions'), key: 'actions', width: 260 },
])

async function load() {
  loading.value = true
  try {
    personas.value = await listAdminPersonas()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.persona_load_failed')))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = ''
  Object.assign(form, {
    name: '', description: '', system_prompt: '', enabled: true, is_default: false, sort_order: 0,
  })
  modalOpen.value = true
}

function openEdit(row: Persona) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    description: row.description,
    system_prompt: row.system_prompt || '',
    enabled: row.enabled,
    is_default: row.is_default,
    sort_order: row.sort_order,
  })
  modalOpen.value = true
}

async function submit() {
  if (saving.value) return
  if (!form.name.trim()) {
    message.warning(t('admin.persona_name_required'))
    return
  }
  if (!form.system_prompt.trim()) {
    message.warning(t('admin.persona_prompt_required'))
    return
  }
  saving.value = true
  try {
    const draft: PersonaDraft = {
      name: form.name.trim(),
      description: form.description.trim(),
      system_prompt: form.system_prompt,
      enabled: form.enabled,
      is_default: form.is_default,
      sort_order: Number(form.sort_order) || 0,
    }
    if (editingId.value) await updatePersona(editingId.value, draft)
    else await createPersona(draft)
    message.success(t('admin.persona_saved'))
    modalOpen.value = false
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.persona_save_failed')))
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(row: Persona, next: boolean) {
  try {
    await updatePersona(row.id, { enabled: next })
    row.enabled = next
    message.success(next ? t('admin.persona_enabled_done') : t('admin.persona_disabled_done'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.persona_save_failed')))
  }
}

async function makeDefault(row: Persona) {
  try {
    await setDefaultPersona(row.id)
    message.success(t('admin.persona_default_done'))
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.persona_save_failed')))
  }
}

async function remove(row: Persona) {
  try {
    await deletePersona(row.id)
    message.success(t('admin.persona_deleted'))
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.persona_delete_failed')))
  }
}

function promptPreview(row: Persona): string {
  const text = (row.system_prompt || '').replace(/\s+/g, ' ').trim()
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

onMounted(load)
</script>

<template>
  <AdminPage
    :title="$t('admin.persona_title')"
    :description="$t('admin.persona_hint')"
  >
    <template #actions>
      <Space>
        <Button :loading="loading" @click="load">
          {{ $t('notifications.refresh') }}
        </Button>
        <Button type="primary" @click="openCreate">
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('admin.persona_add') }}
        </Button>
      </Space>
    </template>

    <AdminTable
      row-key="id"
      :columns="columns"
      :data-source="personas"
      :loading="loading"
      :pagination="false"
    >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div class="name-cell">
              <strong>{{ record.name }}</strong>              <Tag v-if="record.is_default" color="blue">{{ $t('admin.persona_default') }}</Tag>
              <Tag v-if="record.preset" color="purple">{{ $t('admin.persona_preset') }}</Tag>
            </div>
          </template>
          <template v-else-if="column.key === 'description'">
            <span class="muted">{{ record.description || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'prompt'">
            <span class="muted">{{ promptPreview(record as Persona) || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <Switch
              :checked="record.enabled"
              size="small"
              @change="(v: any) => toggleEnabled(record as Persona, Boolean(v))"
            />
          </template>
          <template v-else-if="column.key === 'actions'">
            <Space>
              <Button size="small" @click="openEdit(record as Persona)">
                {{ $t('common.edit') }}
              </Button>
              <Button
                size="small"
                :disabled="record.is_default"
                @click="makeDefault(record as Persona)"
              >
                {{ $t('admin.persona_set_default') }}
              </Button>
              <Popconfirm
                :title="$t('admin.persona_delete_confirm')"
                :ok-text="$t('common.delete')"
                :cancel-text="$t('common.cancel')"
                @confirm="remove(record as Persona)"
              >
                <Button size="small" danger>
                  {{ $t('common.delete') }}
                </Button>
              </Popconfirm>
            </Space>
          </template>
        </template>
        <template #emptyText>
          <Empty :description="$t('admin.persona_empty')" />
        </template>
      </AdminTable>

    <Modal
      v-model:open="modalOpen"
      :title="editingId ? $t('admin.persona_edit') : $t('admin.persona_add')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      width="640px"
      @ok="submit"
    >
      <div class="form-block">
        <label>{{ $t('admin.persona_col_name') }}</label>
        <Input
          v-model:value="form.name"
          :maxlength="100"
          :placeholder="$t('admin.persona_name_placeholder')"
        />
      </div>
      <div class="form-block">
        <label>{{ $t('admin.persona_col_description') }}</label>
        <Input
          v-model:value="form.description"
          :maxlength="500"
          :placeholder="$t('admin.persona_description_placeholder')"
        />
      </div>
      <div class="form-block">
        <label>{{ $t('admin.persona_col_prompt') }}</label>
        <Input.TextArea
          v-model:value="form.system_prompt"
          :rows="10"
          :maxlength="20000"
          show-count
          :placeholder="$t('admin.persona_prompt_placeholder')"
        />
      </div>
      <div class="form-row">
        <label>
          <Switch v-model:checked="form.enabled" />
          {{ $t('admin.persona_col_enabled') }}
        </label>
        <label>
          <Switch v-model:checked="form.is_default" />
          {{ $t('admin.persona_default') }}
        </label>
      </div>
    </Modal>
  </AdminPage>
</template>

<style scoped>
.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.muted {
  color: var(--text-secondary);
}
.form-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}
.form-block label,
.form-row label {
  font-size: 13px;
  color: var(--text-secondary);
}
.form-row {
  display: flex;
  gap: 24px;
  align-items: center;
}
</style>
