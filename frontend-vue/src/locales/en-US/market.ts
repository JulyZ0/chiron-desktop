/**
 * Resource market (/market). Mirrors `zh-CN/market.ts` key for key.
 *
 * Same three rules drive the wording: the source is a first-class concept, an
 * unsigned item is a notice (not a blocker), and an item installed from an
 * external source starts disabled — the user must be told where to enable it.
 */
export default {
  title: 'Resource market',
  // Short label for the nav menu: `title` would be truncated in a one-line item
  nav: 'Market',
  subtitle: 'Browse agents / skills / knowledge / mcp by source and install locally',
  search_placeholder: 'Search name, description or ID',
  tags_all: 'All tags',

  kind: {
    all: 'All',
    agent: 'Agents',
    skill: 'Skills',
    knowledge: 'Collection',
    mcp: 'MCP',
  },

  source: {
    builtin: 'Built-in source',
    type_builtin: 'built-in',
    type_operator: 'operator',
    type_user: 'user',
    count: '{n} items',
    disabled_source: 'Disabled',
    error_source: 'Fetch failed',
    stale_source: 'May be out of date',
  },

  unsigned_badge: 'Unsigned',
  unsigned_hint: 'This item has no publisher signature, so its contents cannot be verified — make sure you trust the source before installing.',

  installed_badge: 'Installed',
  installed_disabled_badge: 'Installed (disabled)',

  enable: 'Enable',
  disable: 'Disable',
  enable_confirm_title: 'Enable “{name}”?',
  enable_confirm_hint:
    'Enabling re-verifies the content (external items re-fetch the manifest and check its digest and signature) and checks the required authorization. Only then will this content be injected, executed or connected.',
  disable_confirm_title: 'Disable “{name}”?',
  disable_confirm_hint: 'Disabling takes effect immediately and also revokes the related authorization. Re-enable and re-authorize when needed.',
  enable_success: 'Enabled “{name}”',
  disable_success: 'Disabled “{name}”',
  quarantine_hint: 'Installed but not enabled: it is not injected, executed or connected yet. Review the source and permissions, then enable it.',

  permission: {
    env: 'Requires env vars: {keys}',
    model: 'Requires a usable model',
    approval: 'Needs per-plugin approval on the plugins page',
    command: 'Runs an MCP command locally',
    none: 'No extra permission required',
  },

  install: 'Install',
  installing: 'Installing…',
  install_confirm_title: 'Install “{name}”?',
  install_confirm_hint: 'You can uninstall it at any time. If it needs credentials, fill in the environment variables below first.',
  uninstall: 'Uninstall',
  uninstalling: 'Uninstalling…',
  uninstall_confirm_title: 'Uninstall “{name}”?',
  install_success: 'Installed “{name}”',
  uninstall_success: 'Uninstalled “{name}”',
  already_installed: 'This item is already installed',
  installed_disabled_hint: 'Installed but disabled — enable it on the matching page',

  detail: {
    title: 'Item details',
    version: 'Version',
    author: 'Author',
    homepage: 'Homepage',
    manifest_url: 'Manifest URL',
    permissions_label: 'Source & permissions',
    requires_model: 'This item needs a configured model',
    requires_approval: 'It still needs approval on the plugins page before it can run',
    requires_env: 'Required environment variables',
    requires_env_hint: 'Kept in memory for this session only — never written to local storage or disk.',
    collection_documents: 'Collection contains {n} document(s)',
    collection_hint: 'Each source below is fetched and imported into the knowledge base on install. No index is built automatically — trigger it from the knowledge base page.',
    no_description: 'No description provided',
  },

  empty: {
    title: 'No items in this category',
    hint: 'Try another source or kind, or adjust your search',
  },

  load_failed: 'Failed to load the market catalog',
  load_failed_hint: 'The built-in source works offline, so it is usually still available; external sources may need network or configuration fixes.',

  warn: {
    source_unreachable: 'Source “{source}” is unreachable — no content was fetched',
    source_invalid: 'Source “{source}” did not return a valid catalog',
    source_timeout: 'Source “{source}” timed out',
    source_rate_limited: 'Source “{source}” is being rate limited; it will retry later',
    payload_too_large: 'The catalog from “{source}” exceeds the size limit and was rejected',
    remote_disabled: 'External sources are disabled, so “{source}” is not fetched',
    stale_snapshot: 'Fetching “{source}” failed; showing the last successful snapshot',
    unsigned_items: '“{source}” has {n} unsigned items',
    items_truncated: '“{source}” has too many items; only the first {n} are shown',
    invalid_item: '“{source}” had items with invalid fields; they were skipped',
    unknown_item_field: '“{source}” had items with unknown fields; they were skipped',
    unsupported_kind: '“{source}” had items of an unsupported kind; they were skipped',
    missing_manifest_url: '“{source}” had items without a manifest URL; they cannot be installed and were skipped',
    generic: '“{source}” reported a problem',
  },

  error: {
    user_sources_forbidden: 'This deployment does not allow adding market sources',
    source_limit_reached: 'The market source limit has been reached — remove one first',
    conflict: 'Conflict: this source already exists',
    enable_denied: 'Cannot enable yet: external sources may be off, or this content is not yet authorized on the plugins page',
    enable_conflict: 'This kind does not support in-place updates — uninstall and reinstall it',
    integrity_failed: 'The content failed its integrity check (digest or signature mismatch); install/enable was rejected',
    request_failed: 'Market request failed, please try again later',
  },
}
