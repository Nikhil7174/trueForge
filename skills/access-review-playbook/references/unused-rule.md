# Unused-access rule

An attachment is **inactive** when `lastUsedAt` is null or older than the window returned by `get_access_last_used` (default 90 days). It is **active** when last-used is inside that window.

If Access Advisor has no timestamps, access-key last-used marks only AdministratorAccess / `*` policies as active. Service-specific read-only policies stay inactive until that service itself is used.

Keep break-glass and admin prefixes off the revoke list even if they look unused. The MCP deny list is the source of truth; do not override it in the prompt.
