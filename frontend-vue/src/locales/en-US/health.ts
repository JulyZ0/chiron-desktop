/** health 域（en-US）：系统自检面板，键与 zh-CN/health.ts 一一对应。 */
export default {
  title: 'System self-check',
  entry: 'System self-check',
  refresh: 'Refresh',
  fix: 'Fix',
  expand: 'Expand self-check details',
  collapse: 'Collapse self-check details',
  updated: 'Updated {time}',
  loading: 'Reading self-check results…',
  empty: 'No self-check items to show',
  error: 'Some self-check data could not be read: {msg}',
  hint: 'Read-only check — no configuration is modified',

  status_ok: 'OK',
  status_warn: 'Attention',
  status_error: 'Error',
  status_unknown: 'Unknown',

  item_schema: 'Schema version',
  item_tables: 'Required tables',
  item_knowledge: 'Knowledge service',
  item_checkpoints: 'Checkpoint protection',
  item_deps: 'Python dependencies',
  item_models: 'Model configuration',
  item_pricing: 'Price coverage',

  detail_models_none: 'No model is enabled yet',
  detail_models_count: '{count} models enabled',
  detail_models_unknown: 'Could not read the model list',
  detail_pricing_all: '{priced}/{total} models have a price',
  detail_pricing_partial: '{unpriced} of {total} models have no price',
  detail_pricing_empty: 'No billable calls in the last {days} days',
}
