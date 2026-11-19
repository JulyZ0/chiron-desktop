/**
 * 通知中心。
 *
 * ## 这里只列**本仓真实存在**的事件
 *
 * 旧版本这份文案照抄了 SaaS 分支的四类事件（支付成功 / 套餐到期 / 配额变更 /
 * 加入团队）与 `0011` 迁移号 —— 而本仓没有任何代码产生它们（没有支付、没有套餐、
 * 没有配额管理）。随之而来的账号菜单入口指向了一个不存在的页面，于是"通知中心"
 * 一直是一条必然 404 的死链。
 *
 * 现在服务端（`internal/api/notifications.go` 的 `notifications` 表 + 两个写入方）
 * 只产生下面两种，文案也一一对应。新增一种 kind 必须同时加三份文案，
 * 否则界面会退回显示原始 key —— 服务端存的是 kind + payload，不是渲染结果。
 *
 * ## 站内通知不可关闭
 *
 * 因此这里没有"关闭通知"的开关，只有"标为已读"。曾经属于邮件偏好的
 * `prefs_*` 一组已随邮件通道一起删除：本仓没有邮件出口，留着只会让用户
 * 去找一个根本不存在的设置。
 */
export default {
  title: '通知中心',
  subtitle: '子 Agent 失败与分享处置会出现在这里。',
  unread_badge: '未读通知',
  empty: '暂无通知',
  empty_unread: '没有未读通知',
  filter_all: '全部',
  filter_unread: '仅未读',
  mark_all_read: '全部标为已读',
  mark_read: '标为已读',
  mark_read_failed: '标记已读失败',
  marked_read: '已标为 {n} 条已读',
  nothing_to_mark: '没有需要标记的通知',
  load_failed: '通知加载失败',
  view_detail: '查看会话',
  refresh: '刷新',
  unknown_title: '通知',

  // 事件类型：既是列表里的分类标签，也是筛选的依据。
  // 与后端 notifications.go 的 NotifSubagentFailed / NotifShareRevoked 一一对应。
  kind: {
    subagent_failed: '子 Agent 失败',
    share_revoked: '分享被撤销',
  },

  // 正文模板。按**当前界面语言**实时渲染，所以历史通知也会跟着语言切换。
  body: {
    subagent_failed: '子任务「{profile}」运行失败（{status}）。错误码：{error_code}。',
    subagent_failed_anonymous: '有一个子任务运行失败（{status}）。错误码：{error_code}。',
    share_revoked: '会话「{title}」的分享链接已被管理员撤销，原链接即刻失效。',
    share_revoked_untitled: '你分享的一个会话链接已被管理员撤销，原链接即刻失效。',
    no_detail: '无更多信息。',
  },
}
