<script setup lang="ts">
/**
 * 后台「用户管理」：团队模式的建号与生命周期操作。
 *
 * 三条硬规则：
 *   1. 初始/临时密码**只展示一次**，关闭弹窗后无法再取回（库里只有 bcrypt 摘要）；
 *   2. 新建/重置后账号进入 must_change_password，首次登录必须改密；
 *   3. 禁用/删除会同时吊销该用户所有会话（后端 revokeUserSessions）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Alert, Button, Empty, Input, Modal, Popconfirm, Select, Space, Tag, message,
} from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  createAdminUser,
  createAdminUserInvite,
  deleteAdminUser,
  forceLogoutAdminUser,
  listAdminUsers,
  resetAdminUserPassword,
  setAdminUserDisabled,
  updateAdminUser,
  type AdminManagedUser,
} from '@/api/admin'
import { serverErrorMessage } from '../../utils/apiError'
import AdminPage from '@/components/admin/AdminPage.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminErrorState from '@/components/admin/AdminErrorState.vue'
import { useAuthStore } from '../../stores/auth'

const { t } = useI18n()
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref('')
const users = ref<AdminManagedUser[]>([])
const page = ref(1)
const perPage = ref(20)
const total = ref(0)

const canAssignAdmin = computed(() => auth.user?.role === 'owner')

const createOpen = ref(false)
const creating = ref(false)
const createForm = reactive({
  email: '',
  username: '',
  nickname: '',
  role: 'user',
  initial_password: '',
})

const editOpen = ref(false)
const editing = ref(false)
const editTarget = ref<AdminManagedUser | null>(null)
const editForm = reactive({ email: '', username: '', nickname: '', role: 'user' })

const credentialsOpen = ref(false)
const credentials = reactive({ title: '', account: '', password: '' })

const inviteOpen = ref(false)
const invite = reactive({ url: '', token: '', expiresAt: '' })

const columns = computed(() => [
  { title: t('admin.userAdmin.col_account'), key: 'account' },
  { title: t('admin.userAdmin.col_nickname'), key: 'nickname' },
  { title: t('admin.userAdmin.col_role'), key: 'role', width: 110 },
  { title: t('admin.userAdmin.col_status'), key: 'status', width: 110 },
  { title: t('admin.userAdmin.col_last_login'), key: 'last_login_at', width: 160 },
  { title: t('admin.userAdmin.col_actions'), key: 'actions', width: 300 },
])

function statusColor(status: string) {
  if (status === 'active') return 'green'
  if (status === 'disabled') return 'orange'
  return 'default'
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await listAdminUsers({ page: page.value, per_page: perPage.value })
    users.value = data?.users || []
    total.value = data?.total || 0
  } catch (err) {
    loadError.value = serverErrorMessage(err, t('admin.userAdmin.load_failed'))
    message.error(loadError.value)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(createForm, {
    email: '', username: '', nickname: '', role: 'user', initial_password: '',
  })
  createOpen.value = true
}

async function submitCreate() {
  if (creating.value) return
  if (!createForm.email.trim()) {
    message.warning(t('admin.userAdmin.email_required'))
    return
  }
  creating.value = true
  try {
    const result = await createAdminUser({
      email: createForm.email.trim(),
      username: createForm.username.trim() || undefined,
      nickname: createForm.nickname.trim() || undefined,
      role: createForm.role,
      initial_password: createForm.initial_password || undefined,
    })
    createOpen.value = false
    showCredentials(
      t('admin.userAdmin.credentials_created'),
      result.user?.email || createForm.email.trim(),
      result.initial_password,
    )
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.create_failed')))
  } finally {
    creating.value = false
  }
}

function showCredentials(title: string, account: string, password: string) {
  credentials.title = title
  credentials.account = account
  credentials.password = password
  credentialsOpen.value = true
}

async function copyPassword() {
  try {
    await navigator.clipboard.writeText(credentials.password)
    message.success(t('common.copied'))
  } catch {
    message.warning(t('errors.copy_failed_please_copy_manually'))
  }
}

function openEdit(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  editTarget.value = user
  Object.assign(editForm, {
    email: user.email,
    username: user.username || '',
    nickname: user.nickname || user.name,
    role: user.role,
  })
  editOpen.value = true
}

async function submitEdit() {
  if (!editTarget.value || editing.value) return
  editing.value = true
  try {
    await updateAdminUser(editTarget.value.id, {
      email: editForm.email.trim(),
      username: editForm.username.trim(),
      nickname: editForm.nickname.trim(),
      role: editForm.role,
    })
    editOpen.value = false
    message.success(t('admin.userAdmin.updated'))
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.update_failed')))
  } finally {
    editing.value = false
  }
}

async function resetPassword(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  try {
    const result = await resetAdminUserPassword(user.id)
    showCredentials(t('admin.userAdmin.credentials_reset'), user.email, result.temporary_password)
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.reset_failed')))
  }
}

async function inviteUser(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  try {
    const result = await createAdminUserInvite(user.id)
    invite.url = result.invite_url || ''
    invite.token = result.token || ''
    invite.expiresAt = result.expires_at || ''
    inviteOpen.value = true
    if (result.email_sent) {
      message.success(t('admin.userAdmin.invite_email_sent', { email: user.email || user.username }))
    } else if (result.email_note && result.email_note !== 'mail_not_configured') {
      message.warning(t('admin.userAdmin.invite_email_failed'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.invite_failed')))
  }
}

async function copyInvite() {
  const text = invite.url || invite.token
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('common.copied'))
  } catch {
    message.warning(t('admin.userAdmin.invite_copy_failed'))
  }
}

async function toggleDisabled(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  try {
    await setAdminUserDisabled(user.id, user.status === 'active')
    message.success(user.status === 'active' ? t('admin.userAdmin.disabled_done') : t('admin.userAdmin.enabled_done'))
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.status_failed')))
  }
}

async function forceLogout(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  try {
    await forceLogoutAdminUser(user.id)
    message.success(t('admin.userAdmin.force_logout_done'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.force_logout_failed')))
  }
}

async function remove(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  try {
    await deleteAdminUser(user.id)
    message.success(t('admin.userAdmin.deleted_done'))
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.userAdmin.delete_failed')))
  }
}

function accountOf(row: AdminManagedUser | Record<string, any>) {
  const user = row as AdminManagedUser
  return user.email || user.username || user.id
}

onMounted(load)
</script>

<template>
  <AdminPage
    :title="$t('admin.userAdmin.page_title')"
    :description="$t('admin.userAdmin.page_desc')"
  >
    <template #actions>
      <Button
        type="primary"
        data-test="user-create"
        @click="openCreate"
      >
        {{ $t('admin.userAdmin.new') }}
      </Button>
    </template>

    <Alert
      type="info"
      show-icon
      class="users-hint"
      :message="$t('admin.userAdmin.password_hint')"
    />

    <AdminErrorState
      v-if="loadError"
      :message="loadError"
      :retrying="loading"
      @retry="load"
    />

    <AdminTable
        v-else
        :columns="columns"
        :data-source="users"
        :loading="loading"
        :pagination="{
          current: page,
          pageSize: perPage,
          total,
          showSizeChanger: false,
          onChange: (p: number) => { page = p; load() },
        }"
        :row-key="(row: AdminManagedUser) => row.id"
      >
        <template #emptyText>
          <Empty>
            <template #description>
              <div class="admin-table-empty-hint">
                <p>{{ $t('admin.userAdmin.empty') }}</p>
                <p>{{ $t('admin.userAdmin.empty_hint') }}</p>
              </div>
            </template>
          </Empty>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'account'">
            <div class="user-account">
              <span class="user-email">{{ accountOf(record) }}</span>
              <span
                v-if="record.username"
                class="user-username"
              >@{{ record.username }}</span>
            </div>
          </template>
          <template v-else-if="column.key === 'nickname'">
            {{ record.nickname || record.name || '—' }}
          </template>
          <template v-else-if="column.key === 'role'">
            {{ $t(`admin.userAdmin.role_${record.role}`) }}
          </template>
          <template v-else-if="column.key === 'status'">
            <Tag :color="statusColor(record.status)">
              {{ $t(`admin.userAdmin.status_${record.status}`) }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'last_login_at'">
            {{ record.last_login_at || $t('admin.userAdmin.never') }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <Space
              v-if="record.status !== 'deleted'"
              :size="4"
              wrap
            >
              <Button
                type="link"
                size="small"
                @click="openEdit(record)"
              >
                {{ $t('common.edit') }}
              </Button>
              <Button
                type="link"
                size="small"
                @click="inviteUser(record)"
              >
                {{ $t('admin.userAdmin.invite_button') }}
              </Button>
              <Popconfirm
                :title="$t('admin.userAdmin.reset_confirm')"
                :ok-text="$t('common.confirm')"
                :cancel-text="$t('common.cancel')"
                @confirm="resetPassword(record)"
              >
                <Button
                  type="link"
                  size="small"
                >
                  {{ $t('admin.userAdmin.reset') }}
                </Button>
              </Popconfirm>
              <Button
                type="link"
                size="small"
                @click="toggleDisabled(record)"
              >
                {{ record.status === 'active' ? $t('admin.userAdmin.disable') : $t('admin.userAdmin.enable') }}
              </Button>
              <Button
                type="link"
                size="small"
                @click="forceLogout(record)"
              >
                {{ $t('admin.userAdmin.force_logout') }}
              </Button>
              <Popconfirm
                :title="$t('admin.userAdmin.delete_confirm')"
                :ok-text="$t('common.confirm')"
                :cancel-text="$t('common.cancel')"
                @confirm="remove(record)"
              >
                <Button
                  type="link"
                  size="small"
                  danger
                >
                  {{ $t('common.delete') }}
                </Button>
              </Popconfirm>
            </Space>
          </template>
        </template>
      </AdminTable>

    <Modal
      v-model:open="createOpen"
      :title="$t('admin.userAdmin.create_title')"
      :confirm-loading="creating"
      :ok-text="$t('common.create')"
      :cancel-text="$t('common.cancel')"
      @ok="submitCreate"
    >
      <div class="form-grid">
        <label>{{ $t('admin.userAdmin.email') }}</label>
        <Input
          v-model:value="createForm.email"
          placeholder="name@company.com"
        />
        <label>{{ $t('admin.userAdmin.username') }}</label>
        <Input
          v-model:value="createForm.username"
          :placeholder="$t('admin.userAdmin.username_hint')"
        />
        <label>{{ $t('admin.userAdmin.nickname') }}</label>
        <Input
          v-model:value="createForm.nickname"
          :placeholder="$t('admin.userAdmin.nickname_hint')"
        />
        <label>{{ $t('admin.userAdmin.role') }}</label>
        <Select
          v-model:value="createForm.role"
          :disabled="!canAssignAdmin"
        >
          <Select.Option value="user">{{ $t('admin.userAdmin.role_user') }}</Select.Option>
          <Select.Option value="admin">{{ $t('admin.userAdmin.role_admin') }}</Select.Option>
          <Select.Option value="owner">{{ $t('admin.userAdmin.role_owner') }}</Select.Option>
        </Select>
        <label>{{ $t('admin.userAdmin.initial_password') }}</label>
        <Input.Password
          v-model:value="createForm.initial_password"
          :placeholder="$t('admin.userAdmin.initial_password_hint')"
        />
      </div>
    </Modal>

    <Modal
      v-model:open="editOpen"
      :title="$t('admin.userAdmin.edit_title')"
      :confirm-loading="editing"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="submitEdit"
    >
      <div class="form-grid">
        <label>{{ $t('admin.userAdmin.email') }}</label>
        <Input v-model:value="editForm.email" />
        <label>{{ $t('admin.userAdmin.username') }}</label>
        <Input
          v-model:value="editForm.username"
          :placeholder="$t('admin.userAdmin.username_hint')"
        />
        <label>{{ $t('admin.userAdmin.nickname') }}</label>
        <Input v-model:value="editForm.nickname" />
        <label>{{ $t('admin.userAdmin.role') }}</label>
        <Select
          v-model:value="editForm.role"
          :disabled="!canAssignAdmin"
        >
          <Select.Option value="user">{{ $t('admin.userAdmin.role_user') }}</Select.Option>
          <Select.Option value="admin">{{ $t('admin.userAdmin.role_admin') }}</Select.Option>
          <Select.Option value="owner">{{ $t('admin.userAdmin.role_owner') }}</Select.Option>
        </Select>
      </div>
    </Modal>

    <Modal
      v-model:open="credentialsOpen"
      :title="credentials.title"
      :footer="null"
    >
      <Alert
        type="warning"
        show-icon
        :message="$t('admin.userAdmin.credentials_once')"
      />
      <div class="credential-box">
        <div class="credential-row">
          <span class="credential-label">{{ $t('admin.userAdmin.col_account') }}</span>
          <code>{{ credentials.account }}</code>
        </div>
        <div class="credential-row">
          <span class="credential-label">{{ $t('admin.userAdmin.password') }}</span>
          <code>{{ credentials.password }}</code>
          <Button
            type="link"
            size="small"
            @click="copyPassword"
          >
            {{ $t('common.copy') }}
          </Button>
        </div>
      </div>
      <p class="credential-hint">
        {{ $t('admin.userAdmin.credentials_share') }}
      </p>
    </Modal>

    <Modal
      v-model:open="inviteOpen"
      :title="$t('admin.userAdmin.invite_modal_title')"
      :footer="null"
    >
      <Alert
        type="info"
        show-icon
        :message="$t('admin.userAdmin.invite_hint')"
      />
      <div class="credential-box">
        <div class="credential-row">
          <span class="credential-label">{{ $t('admin.userAdmin.invite_url') }}</span>
          <code class="invite-text">{{ invite.url || invite.token }}</code>
          <Button
            type="link"
            size="small"
            @click="copyInvite"
          >
            {{ $t('common.copy') }}
          </Button>
        </div>
        <div v-if="invite.expiresAt" class="credential-row">
          <span class="credential-label">{{ $t('admin.userAdmin.invite_expires') }}</span>
          <code>{{ invite.expiresAt }}</code>
        </div>
      </div>
      <p v-if="!invite.url" class="credential-hint">
        {{ $t('admin.userAdmin.invite_no_base_url') }}
      </p>
    </Modal>
  </AdminPage>
</template>

<style scoped>
.users-hint { border-radius: var(--radius-lg); }
.user-account { display: flex; flex-direction: column; gap: 2px; }
.user-email { color: var(--text-primary); }
.user-username { font-size: var(--fs-sm); color: var(--text-tertiary); }
.form-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 10px 12px;
  align-items: center;
}
.form-grid label { font-size: var(--fs-sm); color: var(--text-secondary); }
.credential-box {
  margin-top: 12px;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-secondary);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.credential-row { display: flex; align-items: center; gap: 10px; }
.credential-label { font-size: var(--fs-sm); color: var(--text-tertiary); min-width: 72px; }
.credential-hint { margin: 12px 0 0; font-size: var(--fs-sm); color: var(--text-tertiary); }
.invite-text { word-break: break-all; }
</style>
