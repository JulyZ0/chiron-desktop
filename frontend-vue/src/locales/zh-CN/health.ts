/**
 * health 域：系统自检面板（P1-9）。
 *
 * 面板在三个入口复用（首页抽屉 / 设置顶部 / 欢迎引导第一步之后），因此文案
 * 不放在任何单个页面里。域前缀 `health.` 已登记在 scripts/check-i18n-keys.mjs
 * 的 DOMAIN_PREFIXES 中 —— 三语必须一一对应，缺键会直接失败。
 */
export default {
  title: '系统自检',
  entry: '系统自检',
  refresh: '刷新',
  fix: '去修复',
  expand: '展开自检详情',
  collapse: '收起自检详情',
  updated: '更新于 {time}',
  loading: '正在读取自检结果…',
  empty: '暂无可显示的自检项',
  error: '部分自检数据读取失败：{msg}',
  hint: '只读检查，不会改动任何配置',

  status_ok: '正常',
  status_warn: '注意',
  status_error: '异常',
  status_unknown: '未知',

  item_schema: 'Schema 版本',
  item_tables: '必需数据表',
  item_knowledge: '知识库服务',
  item_checkpoints: '检查点保护',
  item_deps: 'Python 依赖',
  item_models: '模型配置',
  item_pricing: '价格覆盖率',

  detail_models_none: '尚未启用任何模型',
  detail_models_count: '已启用 {count} 个模型',
  detail_models_unknown: '模型列表读取失败',
  detail_pricing_all: '{priced}/{total} 个模型已有单价',
  detail_pricing_partial: '{total} 个模型中 {unpriced} 个缺单价',
  detail_pricing_empty: '近 {days} 天没有可计价的调用',
}
