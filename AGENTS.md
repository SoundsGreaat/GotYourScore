# AGENTS.md

## Notifications and error handling

- Reuse `gysToast()` in `app/static/js/common.js` for plain-text notifications. Prefer inline feedback when it can stay next to the affected field. Use `gysFloatNote()` with HTML only for markup assembled from fixed copy and validated values.
- Keep expected business errors actionable. Tell the user which operation failed and, when known, what to change or retry. Select text owned by the client using a documented public error code or an unambiguous status for that operation; legacy exact-message allowlists must be scoped to the endpoint. Keep useful feedback for validation, duplicate records, quotas, and retry windows.
- Include only the minimum relevant context already intentionally visible in the current UI, such as the display name on the selected agent chip (not a name or email returned by an error response). For batch operations prefer row numbers to case identifiers.
- Never render arbitrary error `detail`, exception messages, raw HTML, upstream responses, secrets, email addresses, private case contents, request payloads, or stack traces. Use a generic `Server error` or `Request error` when there is no safe, actionable reason. A safe operation label may accompany it.
- Log diagnostic context server-side using stable reason codes and internal IDs. Do not log credentials, raw URLs or queries, cookies, case content, uploaded values, prompts, or provider bodies.
- Preserve the shared stack and dialog layering, do not move focus when a toast appears or expires, and respect reduced motion. Cover both useful feedback and redaction in regression cases.
