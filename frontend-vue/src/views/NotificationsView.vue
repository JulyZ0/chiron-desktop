<script setup lang="ts">
/**
 * 通知中心（`/notifications`）。
 *
 * 这个页面此前**不存在**：账号菜单里有「通知中心」，`accountMenuRoute()` 也返回
 * `/notifications`，但路由表里没有这条路径 —— 点进去必然落到 404 兜底页。
 * 文案是从 SaaS 分支裁剪时残留的（支付成功 / 套餐到期 / 配额变更 / 加入团队），
 * 而本仓没有任何代码产生它们。
 *
 * 现在补齐的是**真实存在的事件**：子 Agent 运行失败（网关 `subagent_events_relay.go`
 * 的终态回调）与分享被管理员撤销（`admin_shares.go`）。两类都是用户"该知道但
 * 不一定在现场"的事 —— 这正是站内通知的边界，把每条终态都塞进来会让它变成日志流。
 *
 * ## 文案在客户端渲染
 *
 * 服务端只存 `kind` + `payload`，本页按**当前界面语言**套模板。所以历史通知也会
 * 跟着语言切换，代价是新增 kind 必须同时加三份文案 —— 缺失时会退回显示原始 key
 * （`unknown_title` + kind 本身），而不是白屏。
 *
 * ## 未读数来自服务端
 *
 * 不在这里 `items.filter(unread).length`：列表默认只取最近 50 条，本地数会随
 * 历史增长而少算。服务端那条 `COUNT(*)` 走部分索引，是常数级。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Card, Empty, Popconfirm, Segmented, Space, Spin, Tag, message } from 'ant-design-vue'
import { BellOutlined, ReloadOutlined, CheckOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  clearNotifications,
  deleteNotification,
  listNotifications,
  markNotificationsRead,
  type NotificationItem,
} from '../api/notifications'
import { serverErrorMessage } from '../utils/apiError'
import { announceNotificationsChanged } from '../composables/useUnreadNotifications'

const { t } = useI18n()
const router = useRouter()

type Filter = 'all' | 'unread'

const filter = ref<Filter>('all')
const loading = ref(false)
const marking = ref(false)
const items = ref<NotificationItem[]>([])
const unreadCount = ref(0)
/** 行级 loading：只标一条时要能看出点的是哪一条 */
const markingId = ref('')
/** 行级删除 loading */
const deletingId = ref('')
/** 批量清理已读 loading */
const clearing = ref(false)

/** 空态文案随筛选变化：'暂无通知' 与 '没有未读通知' 是两件事。 */
const emptyText = computed(() =>
  filter.value === 'unread' ? t('notifications.empty_unread') : t('notifications.empty'),
)

async function load() {
  loading.value = true
  try {
    const res = await listNotifications({ unreadOnly: filter.value === 'unread' })
    items.value = res.items
    unreadCount.value = res.unread_count
  } catch (err) {
    message.error(serverErrorMessage(err, t('notifications.load_failed')))
  } finally {
    loading.value = false
  }
}

function switchFilter(next: Filter) {
  if (filter.value === next) return
  filter.value = next
  void load()
}

/**
 * 渲染一条通知的正文。
 *
 * payload 是自由 JSON，缺字段是**正常情况**（例如引擎没给 error_code）。
 * 每个模板都有 `*_anonymous` / `*_untitled` 这一档兜底，所以这里只做"有就用"，
 * 不为缺失字段报错 —— 一条通知显示得含糊，好过整页错误态。
 */
function bodyText(item: NotificationItem): string {
  const p = item.payload
  const str = (k: string): string => (typeof p[k] === 'string' ? (p[k] as string) : '')
  const status = str('status') || '—'

  switch (item.kind) {
    case 'subagent_failed': {
      const profile = str('profile')
      const errorCode = str('error_code') || '—'
      return profile
        ? t('notifications.body.subagent_failed', { profile, status, error_code: errorCode })
        : t('notifications.body.subagent_failed_anonymous', { status, error_code: errorCode })
    }
    case 'share_revoked': {
      const title = str('title')
      return title
        ? t('notifications.body.share_revoked', { title })
        : t('notifications.body.share_revoked_untitled')
    }
    default:
      // 未知 kind：服务端比前端新时的必然结果。显示原始 key，便于排查而不用猜。
      return t('notifications.body.no_detail')
  }
}

/**
 * 分类标签。未知 kind 原样显示 —— 从前端视角它就是"一个新种类"，
 * 掩盖掉只会让排查的人以为后端没发。
 */
function kindLabel(item: NotificationItem): string {
  const key = `notifications.kind.${item.kind}`
  const label = t(key)
  return label === key ? item.kind : label
}

/** 颜色只区分"该处理"与"被告知"：失败是警告色，撤销是中性色。 */
function kindColor(item: NotificationItem): string {
  switch (item.kind) {
    case 'subagent_failed':
      return 'warning'
    case 'share_revoked':
      return 'default'
    default:
      return 'default'
  }
}

function formatTime(value: string): string {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

/** 只有指向某个会话的通知才给"查看会话" —— 分享撤销也可能没有会话。 */
function canOpenSession(item: NotificationItem): boolean {
  return Boolean(item.session_id)
}

function openSession(item: NotificationItem) {
  if (!item.session_id) return
  void router.push({ path: '/chat', query: { session: item.session_id } })
}

async function markOne(item: NotificationItem) {
  if (item.read) return
  markingId.value = item.id
  try {
    const updated = await markNotificationsRead({ ids: [item.id] })
    if (updated > 0) {
      item.read = true
      item.read_at = new Date().toISOString()
      unreadCount.value = Math.max(0, unreadCount.value - updated)
      announceNotificationsChanged()
      // 在"仅未读"筛选下，标完的这一条已不属于当前视图 —— 就地移除。
      if (filter.value === 'unread') {
        items.value = items.value.filter(it => it.id !== item.id)
      }
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('notifications.mark_read_failed')))
  } finally {
    markingId.value = ''
  }
}

async function markAll() {
  marking.value = true
  try {
    const updated = await markNotificationsRead({ all: true })
    if (updated === 0) {
      message.info(t('notifications.nothing_to_mark'))
      return
    }
    message.success(t('notifications.marked_read', { n: updated }))
    announceNotificationsChanged()
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('notifications.mark_read_failed')))
  } finally {
    marking.value = false
  }
}

/** 行内删除一条（已读/未读都可删）。 */
async function removeOne(item: NotificationItem) {
  deletingId.value = item.id
  try {
    const deleted = await deleteNotification(item.id)
    if (deleted > 0) {
      items.value = items.value.filter(it => it.id !== item.id)
      if (!item.read) unreadCount.value = Math.max(0, unreadCount.value - 1)
      announceNotificationsChanged()
      message.success(t('notifications.deleted'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('notifications.delete_failed')))
  } finally {
    deletingId.value = ''
  }
}

/** 批量清理已读 —— 通知中心长期只增不减的主要来源就是这些已读行。 */
async function clearRead() {
  clearing.value = true
  try {
    const deleted = await clearNotifications('read')
    if (deleted > 0) message.success(t('notifications.cleared', { n: deleted }))
    else message.info(t('notifications.nothing_to_delete'))
    if (deleted > 0) announceNotificationsChanged()
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('notifications.delete_failed')))
  } finally {
    clearing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="notifications-view">
    <div class="page-header">
      <div class="title">
        <BellOutlined />
        <span>{{ $t('notifications.title') }}</span>
        <Tag
          v-if="unreadCount > 0"
          color="red"
        >
          {{ $t('notifications.unread_badge') }} {{ unreadCount }}
        </Tag>
        <span class="subtitle">{{ $t('notifications.subtitle') }}</span>
      </div>
      <Space>
        <Segmented
          :value="filter"
          :options="[
            { label: $t('notifications.filter_all'), value: 'all' },
            { label: $t('notifications.filter_unread'), value: 'unread' },
          ]"
          @change="(v) => switchFilter(v as Filter)"
        />
        <Button
          :disabled="unreadCount === 0"
          :loading="marking"
          @click="markAll"
        >
          <template #icon>
            <CheckOutlined />
          </template>
          {{ $t('notifications.mark_all_read') }}
        </Button>
        <Button
          :loading="clearing"
          @click="clearRead"
        >
          <template #icon>
            <DeleteOutlined />
          </template>
          {{ $t('notifications.clear_read') }}
        </Button>
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('notifications.refresh') }}
        </Button>
      </Space>
    </div>

    <Card :bordered="false">
      <Spin :spinning="loading">
        <Empty
          v-if="items.length === 0 && !loading"
          :description="emptyText"
        />
        <ul
          v-else
          class="notification-list"
        >
          <li
            v-for="item in items"
            :key="item.id"
            class="notification-row"
            :class="{ 'is-unread': !item.read }"
          >
            <div class="row-main">
              <div class="row-head">
                <Tag :color="kindColor(item)">
                  {{ kindLabel(item) }}
                </Tag>
                <span class="row-time">{{ formatTime(item.created_at) }}</span>
                <Tag
                  v-if="!item.read"
                  color="blue"
                >
                  {{ $t('notifications.filter_unread') }}
                </Tag>
              </div>
              <p class="row-body">
                {{ bodyText(item) }}
              </p>
            </div>
            <Space class="row-actions">
              <Button
                v-if="canOpenSession(item)"
                size="small"
                @click="openSession(item)"
              >
                {{ $t('notifications.view_detail') }}
              </Button>
              <Button
                v-if="!item.read"
                size="small"
                :loading="markingId === item.id"
                @click="markOne(item)"
              >
                {{ $t('notifications.mark_read') }}
              </Button>
              <Popconfirm
                :title="$t('notifications.delete_confirm')"
                :ok-text="$t('common.delete')"
                :cancel-text="$t('common.cancel')"
                @confirm="removeOne(item)"
              >
                <Button
                  size="small"
                  danger
                  :loading="deletingId === item.id"
                >
                  <template #icon>
                    <DeleteOutlined />
                  </template>
                  {{ $t('common.delete') }}
                </Button>
              </Popconfirm>
            </Space>
          </li>
        </ul>
      </Spin>
    </Card>
  </div>
</template>

<style scoped>
.notifications-view {
  padding: 24px;
  max-width: 1080px;
  margin: 0 auto;
}

.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 20px;
  font-weight: var(--fw-semibold);
}

.title .subtitle {
  font-size: 13px;
  font-weight: normal;
  color: var(--text-secondary);
  margin-inline-start: 4px;
}

.notification-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.notification-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 4px;
  border-bottom: 1px solid var(--border-color);
}

.notification-row:last-child {
  border-bottom: none;
}

/* 未读用左侧色条标记，而不是整行换底色：整行着色在暗色主题下会盖掉正文对比度 */
.notification-row.is-unread {
  border-inline-start: 3px solid var(--node-indigo);
  padding-inline-start: 12px;
}

.row-main {
  min-width: 0;
  flex: 1;
}

.row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.row-time {
  font-size: 12px;
  color: var(--text-secondary);
}

.row-body {
  margin: 6px 0 0;
  font-size: 14px;
  line-height: 1.55;
  color: var(--text-primary);
  word-break: break-word;
}

.row-actions {
  flex-shrink: 0;
}
</style>
