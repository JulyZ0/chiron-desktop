/**
 * 资源市场（/market）。
 *
 * 三条口径决定了这里的文案：
 *   1) **来源是第一等概念** —— 同名条目在不同源是不同东西，所以每个源一个 Tab，
 *      文案里反复出现「来源」而不是笼统的「市场」；
 *   2) **签名不强制但要说明** —— `unsigned_*` 只做提醒，措辞不能像错误（不阻止安装）；
 *   3) **装完默认禁用** —— 外部源装成功 ≠ 可用，`installed_disabled_hint` 必须说清
 *      「去哪里启用」，否则用户会以为装坏了。
 */
export default {
  title: '资源市场',
  // 导航菜单里的短标签：菜单是单行文字，用 title 会被截断
  nav: '市场',
  subtitle: '按来源浏览 agents / skills / knowledge / mcp，装到本机工作台',
  search_placeholder: '搜索名称、描述或 ID',
  tags_all: '全部标签',

  // 四类资源（与后端 market.Kind 一一对应）
  kind: {
    all: '全部',
    agent: 'Agent',
    skill: '技能',
    knowledge: '文档合集',
    mcp: 'MCP',
  },

  // 来源：名字与信任级别分开展示 —— 名字可能重名，类型不会
  source: {
    builtin: '内置源',
    type_builtin: '内置',
    type_operator: '运维',
    type_user: '用户',
    count: '{n} 个条目',
    disabled_source: '已停用',
    error_source: '拉取失败',
    stale_source: '内容可能已过期',
  },

  // 签名：只提醒，不阻止
  unsigned_badge: '未签名',
  unsigned_hint: '该条目没有发布方签名，无法校验内容完整性 —— 安装前请自行确认来源可信。',

  installed_badge: '已安装',
  installed_disabled_badge: '已安装（未启用）',

  // 启用 / 停用（H75：装完 inert，显式信任才可用）
  enable: '启用',
  disable: '停用',
  enable_confirm_title: '启用「{name}」？',
  enable_confirm_hint:
    '启用后会重新校验内容完整性（外部条目会重新抓取清单并核对摘要与签名），并检查所需授权；通过后这条内容才会被注入、执行或连接。',
  disable_confirm_title: '停用「{name}」？',
  disable_confirm_hint: '停用立即生效：相关授权会一并撤回，需要时再重新启用并授权。',
  enable_success: '已启用「{name}」',
  disable_success: '已停用「{name}」',
  quarantine_hint: '已安装但未启用：目前不会被注入、执行或连接。确认来源与权限需求后再启用。',

  // 权限需求摘要（详情抽屉）
  permission: {
    env: '需要环境变量：{keys}',
    model: '需要可用模型',
    approval: '需要在插件页逐条授权',
    command: '会在本机执行 MCP 命令',
    none: '无需额外权限',
  },

  install: '安装',
  installing: '安装中…',
  install_confirm_title: '确认安装「{name}」？',
  install_confirm_hint: '安装后可随时卸载；需要凭据的条目请先在下方填写环境变量。',
  uninstall: '卸载',
  uninstalling: '卸载中…',
  uninstall_confirm_title: '确认卸载「{name}」？',
  install_success: '已安装「{name}」',
  uninstall_success: '已卸载「{name}」',
  already_installed: '该条目已安装，无需重复安装',
  installed_disabled_hint: '已安装但处于禁用状态，需在对应页面启用',

  detail: {
    title: '条目详情',
    version: '版本',
    author: '作者',
    homepage: '主页',
    manifest_url: '清单地址',
    permissions_label: '来源与权限需求',
    requires_model: '该条目需要已配置可用的模型',
    requires_approval: '安装后仍需在插件页授权才能真正执行',
    requires_env: '需要填写的环境变量',
    requires_env_hint: '只保存在本次会话的内存中，不会写入本地存储或磁盘。',
    collection_documents: '合集包含 {n} 份文档',
    collection_hint: '安装时会从下列地址抓取原文导入知识库，不会自动构建索引 —— 索引请在知识库页自行触发。',
    no_description: '该条目未提供描述',
  },

  empty: {
    title: '这个分类下暂无条目',
    hint: '换个来源或类型，或者调整搜索关键词',
  },

  load_failed: '市场目录加载失败',
  load_failed_hint: '内置源不依赖网络，通常仍可用；外部源可能需要检查网络或配置。',

  // 非致命告警（后端 warnings[].reason）。**一律本地化**，不要把原因码甩给用户。
  warn: {
    source_unreachable: '来源「{source}」当前无法访问，本次没有取到内容',
    source_invalid: '来源「{source}」返回的不是合法的市场目录',
    source_timeout: '来源「{source}」响应超时',
    source_rate_limited: '来源「{source}」请求过于频繁，稍后会自动重试',
    payload_too_large: '来源「{source}」的目录体积超过上限，已拒绝',
    remote_disabled: '外部源功能未启用，来源「{source}」不会被拉取',
    stale_snapshot: '来源「{source}」本次拉取失败，展示的是上次成功的内容',
    unsigned_items: '来源「{source}」有 {n} 条未签名条目',
    items_truncated: '来源「{source}」条目过多，仅展示前 {n} 条',
    invalid_item: '来源「{source}」有条目字段不合法，已跳过',
    unknown_item_field: '来源「{source}」有条目含未知字段，已跳过',
    unsupported_kind: '来源「{source}」有条目类型不受支持，已跳过',
    missing_manifest_url: '来源「{source}」有条目缺少清单地址，无法安装，已跳过',
    generic: '来源「{source}」有提醒',
  },

  error: {
    user_sources_forbidden: '当前部署不允许自行添加市场来源',
    source_limit_reached: '市场来源数量已达上限，请先删除一个再添加',
    conflict: '操作冲突：该来源已存在',
    enable_denied: '还不能启用：外部源可能未开启，或该内容尚未在插件页获得授权',
    enable_conflict: '该类型不支持原地更新，请卸载后重新安装',
    integrity_failed: '内容完整性校验未通过（摘要或签名不匹配），已拒绝安装/启用',
    request_failed: '市场请求失败，请稍后重试',
  },
}
