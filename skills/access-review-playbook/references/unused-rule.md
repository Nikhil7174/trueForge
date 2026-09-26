# Unused-access rule

An attachment is **unused** when `lastUsedAt` is null or older than the window returned by `get_access_last_used` (default 90 days).

Keep break-glass and admin prefixes off the revoke list even if they look unused. The MCP deny list is the source of truth; do not override it in the prompt.
