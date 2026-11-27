/**
 * Notification center.
 *
 * ## Only events this repository actually produces
 *
 * The previous version copied four event kinds from the SaaS branch (payment
 * succeeded / package expiring / quota changed / member invited) plus migration
 * `0011` — but nothing here produces them: there is no payment, no package, no
 * quota management. The account-menu entry pointed at a page that never existed,
 * so "Notifications" was a guaranteed 404.
 *
 * The server (`internal/api/notifications.go`: the `notifications` table and its
 * two writers) produces exactly the two kinds below. Adding a kind requires all
 * three locale files, because the server stores `kind` + `payload`, never the
 * rendered text.
 *
 * ## In-app notifications cannot be turned off
 *
 * Hence there is no "disable notifications" switch, only "mark as read". The
 * former `prefs_*` (email preference) group is gone along with the email
 * channel — this repository has no mail path, so leaving it in would send users
 * looking for a setting that does not exist.
 */
export default {
  title: 'Notifications',
  subtitle: 'Failed sub-agents and share takedowns show up here.',
  unread_badge: 'Unread notifications',
  native_body: '{n} new unread notification | {n} new unread notifications',
  empty: 'No notifications yet',
  empty_unread: 'No unread notifications',
  filter_all: 'All',
  filter_unread: 'Unread only',
  mark_all_read: 'Mark all as read',
  mark_read: 'Mark as read',
  mark_read_failed: 'Failed to mark as read',
  marked_read: '{n} marked as read',
  nothing_to_mark: 'Nothing to mark as read',
  load_failed: 'Failed to load notifications',
  view_detail: 'Open session',
  refresh: 'Refresh',
  unknown_title: 'Notification',
  delete_confirm: 'Permanently delete this notification?',
  deleted: 'Deleted',
  delete_failed: 'Failed to delete notification',
  clear_read: 'Clear read',
  cleared: 'Cleared {n} notifications',
  nothing_to_delete: 'Nothing to clear',

  // Event kinds: category labels in the list and the filter basis.
  // One-to-one with NotifSubagentFailed / NotifShareRevoked in notifications.go.
  kind: {
    subagent_failed: 'Sub-agent failed',
    share_revoked: 'Share revoked',
  },

  // Body templates, rendered against the *current* UI language, so historical
  // notifications follow a language switch too.
  body: {
    subagent_failed: 'Sub-task "{profile}" failed ({status}). Error code: {error_code}.',
    subagent_failed_anonymous: 'A sub-task failed ({status}). Error code: {error_code}.',
    share_revoked: 'The share link for "{title}" was revoked by an administrator; the old link no longer works.',
    share_revoked_untitled: 'A conversation share link of yours was revoked by an administrator; the old link no longer works.',
    no_detail: 'No further details.',
  },
}
